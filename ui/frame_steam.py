#!/usr/bin/env python3
"""Runs ON the Frame (piped over SSH): read and drive the Steam client.

The Frame's Steam client starts with -cef-enable-debugging, so its UI's
JavaScript context ("SharedJSContext") answers the Chrome DevTools protocol on
127.0.0.1:8080. That context holds the library (appStore), downloads
(downloadsStore) and the SteamClient API. This file is a stdlib-only WebSocket
client for it, plus the few actions Frame Control needs.

Usage: python3 - owned            # owned games + download status, JSON
       python3 - live             # battery, download queue, controllers, running game
       python3 - install APPID    # start an install; reports the wizard state
       python3 - store APPID      # open the app's store page in the headset
Prints one JSON object. Errors are {"error": "..."} with exit status 1.
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

CDP = "http://127.0.0.1:8080/json"

# EInstallMgrState values from the client's own JS (steamui, 2026-09).
STATE = {0: "none", 1: "setup", 2: "waiting for license", 3: "free license", 4: "CD key",
         5: "waiting for app info", 6: "password", 7: "config", 8: "EULA", 9: "creating apps",
         10: "reading media", 11: "change media", 12: "legacy CD keys", 13: "signup",
         14: "complete", 15: "failed", 16: "canceled"}
# States where Steam is waiting for someone to answer a dialog in the headset.
NEEDS_HEADSET = {3, 4, 6, 8, 11, 13}
BUSY = {1, 2, 5, 9, 10, 12}


class Fail(Exception):
    pass


class Page:
    """Minimal CDP-over-WebSocket client (text frames, no extensions)."""

    def __init__(self, title="SharedJSContext"):
        try:
            pages = json.load(urllib.request.urlopen(CDP, timeout=5))
        except OSError as e:
            raise Fail(f"Steam's UI isn't answering on {CDP} ({e}); is Steam running?")
        url = next((p["webSocketDebuggerUrl"] for p in pages if p.get("title") == title), None)
        if not url:
            raise Fail(f"no {title} page; Steam may still be starting")
        hostport, path = url.split("://", 1)[1].split("/", 1)
        host, port = hostport.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Fail("DevTools closed the connection during the handshake")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise Fail("DevTools refused the WebSocket upgrade")
        self.next_id = 0

    def _send(self, text):
        data, mask = text.encode(), os.urandom(4)
        n = len(data)
        if n < 126:
            head = struct.pack(">BB", 0x81, 0x80 | n)
        elif n < 1 << 16:
            head = struct.pack(">BBH", 0x81, 0x80 | 126, n)
        else:
            head = struct.pack(">BBQ", 0x81, 0x80 | 127, n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _take(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise Fail("DevTools closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _recv(self):
        message = b""
        while True:
            b0, b1 = self._take(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            payload = self._take(n)
            if b0 & 0x0F in (0x1, 0x0):  # text or continuation; skip ping/pong/close
                message += payload
                if b0 & 0x80:
                    return message.decode()

    def eval(self, expr):
        """Evaluate JS (awaiting promises) and return its JSON-serialisable value."""
        self.next_id += 1
        self._send(json.dumps({"id": self.next_id, "method": "Runtime.evaluate", "params": {
            "expression": expr, "awaitPromise": True, "returnByValue": True}}))
        while True:
            msg = json.loads(self._recv())
            if msg.get("id") != self.next_id:
                continue  # events
            if "error" in msg:  # protocol error, e.g. the page is reloading
                raise Fail(f"DevTools: {msg['error'].get('message', msg['error'])}")
            res = msg.get("result", {})
            if "exceptionDetails" in res:
                d = res["exceptionDetails"]
                raise Fail(d.get("exception", {}).get("description") or d.get("text") or "JS error")
            return res.get("result", {}).get("value")


# steam_hw_compat_category_packed: 2 bits per device; the client reads the
# Frame's rating as `packed >> 8 & 3` (0 unknown, 1 unsupported, 2 playable, 3 verified).
OWNED_JS = r"""
(async () => {
  const country = await SteamClient.User.GetIPCountry().catch(() => null);
  const dl = new Map(downloadsStore.m_DownloadOverview || []).get("0") || null;
  const games = appStore.allApps.filter(a => a.app_type == 1).map(a => {
    const c = a.local_per_client_data || {};
    return { id: a.appid, name: a.display_name, sort: a.sort_as || a.display_name,
             installed: !!c.installed, status: c.display_status ?? null, pct: c.status_percentage ?? null,
             frame: (a.steam_hw_compat_category_packed >> 8) & 3, deck: a.steam_hw_compat_category_packed & 3,
             vr: !!a.vr_supported, vrOnly: !!a.vr_only, size: Number(a.size_on_disk || 0),
             minutes: a.minutes_playtime_forever || 0, lastPlayed: a.rt_last_time_played || 0 };
  });
  return { games, country, download: dl && dl.update_appid ? {
    appid: dl.update_appid, state: dl.update_state, paused: dl.paused, install: dl.update_is_install,
    percent: dl.overall_percent_complete, eta: dl.overall_estimated_time_remaining_sec,
    bps: dl.update_network_bytes_per_second } : null };
})()
"""

# Software has app_type 2, so utility ownership must not use OWNED_JS's games filter.
# Steam prices checked 2026-09-28: OVR Advanced Settings is paid on Steam too.
UTILITY_IDS = (1009850, 1173510, 1068820, 908520, 1494460)
FREE_UTILITIES = (1494460,)
UTILITIES_JS = """(() => {
  const apps = appStore.allApps;
  if (!apps || !apps.length) throw new Error("Steam library is not loaded; ownership is unknown");
  return [1009850,1173510,1068820,908520,1494460].map(id => {
    const a = apps.find(a => a.appid === id);
    return {id, owned: !!a, installed: !!a?.local_per_client_data?.installed,
            frame: a ? (a.steam_hw_compat_category_packed >> 8) & 3 : 0};
  });
})()"""


# What the headset itself shows, for the Device card: Steam's battery level
# (the kernel's capacity reads lower; Steam scales it), the download queue,
# controller batteries and the running game. Steam pushes the first three
# through Register* callbacks that fire with the current value when registered
# (as FrameMate, github.com/nailuj05/framemate, relies on; verified on the Frame
# 2026-10-03); each is awaited once,
# briefly, then unregistered. One that doesn't fire in time is null, not an error.
# OpenVR property ids (openvr.h): 1001 model, 1002 serial, 1011 charging,
# 1012 battery (0-1), 1029 device class (2 controller, 3 tracker).
LIVE_JS = r"""
(async () => {
  const first = (register, ms = 1500) => new Promise(resolve => {
    let handle = null, done = false, timer = null;
    const finish = value => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      // A callback can fire inside register(), before handle is assigned.
      Promise.resolve().then(() => { try { handle?.unregister(); } catch {} });
      resolve(value);
    };
    timer = setTimeout(() => finish(null), ms);
    try { handle = register((...args) => finish(args)); } catch { finish(null); }
  });
  const name = id => { try { return appStore.GetAppOverviewByAppID(Number(id))?.display_name ?? null; } catch { return null; } };
  const S = SteamClient;
  const [batt, items, paths] = await Promise.all([
    first(cb => S.System.RegisterForBatteryStateChanges(cb)),
    first(cb => S.Downloads.RegisterForDownloadItems(cb)),
    first(cb => S.OpenVR.RegisterForVRTrackedDevices(cb)),
  ]);

  const b = batt?.[0];
  const battery = b && b.bHasBattery && Number.isFinite(b.flLevel) ? {
    percent: Math.round(b.flLevel * 100),
    secondsLeft: b.nSecondsRemaining > 0 ? b.nSecondsRemaining : null,
    onAC: b.eACState === 2 } : null;

  // With Remote Downloads the account's other PCs are listed too; client "0" is this one.
  const local = c => String(c?.remote_client_id) === "0";
  const queue = items ? (items[1] || []).filter(local).flatMap(c => c.item_data || []).map(i => ({
    appid: i.appid, name: name(i.appid), position: i.queue_index ?? null, active: !!i.active,
    paused: !!i.paused, completed: !!i.completed, completedAt: i.completed_time || null,
    // An update Steam put off until later (queue_index -1); seen on the Frame 2026-10-03.
    scheduledFor: !i.completed && i.deferred_time > Date.now() / 1000 ? i.deferred_time : null,
    percent: i.overall_percent_complete ?? null, error: i.update_error || null })) : null;
  const o = new Map(downloadsStore.m_DownloadOverview || []).get("0");
  const current = o && o.update_appid ? {
    appid: o.update_appid, paused: !!o.paused, percent: o.overall_percent_complete ?? null,
    eta: o.overall_estimated_time_remaining_sec ?? null, bps: o.update_network_bytes_per_second ?? null } : null;

  const prop = async (fn, path, id) => {
    try { return await S.OpenVR.DeviceProperties[fn](path, id); } catch { return null; }
  };
  const device = async path => {
    let connected = null;
    try { connected = !!(await S.OpenVR.Device.BIsConnected(path)); } catch {}
    return { serial: await prop("GetStringDeviceProperty", path, 1002),
             model: await prop("GetStringDeviceProperty", path, 1001),
             kind: await prop("GetInt32DeviceProperty", path, 1029),
             battery: await prop("GetFloatDeviceProperty", path, 1012),
             charging: await prop("GetBoolDeviceProperty", path, 1011), connected };
  };
  const devices = Array.isArray(paths?.[0])
    ? (await Promise.all(paths[0].map(device))).filter(d => d.kind === 2 || d.kind === 3) : null;

  let running = null;
  try {
    running = Array.from(SteamUIStore.RunningApps ?? [], a => ({ appid: a.appid, name: a.display_name ?? name(a.appid) }));
  } catch {}
  return { battery, downloads: queue && { queue, current }, devices, running };
})()
"""


WIZARD_JS = """SteamClient.Installs.GetInstallManagerInfo().then(i => ({
  state: i?.eInstallState ?? 0, app: i?.currentAppID ?? 0, need: i?.nDiskSpaceRequired || 0,
  free: i?.nDiskSpaceAvailable || 0, error: i?.eAppError, detail: i?.errorDetail }))"""


def steam_url(url):
    """Hand a steam:// URL to the running client (the `steam` wrapper forwards it)."""
    subprocess.Popen(["steam", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def owned():
    return Page().eval(OWNED_JS)


def live():
    return Page().eval(LIVE_JS)


def install(appid):
    page = Page()
    if appid in UTILITY_IDS and appid not in FREE_UTILITIES:
        rows = page.eval(UTILITIES_JS)
        if not any(r['id'] == appid and r['owned'] for r in rows):
            raise Fail("This paid utility is not owned by the Frame account. No install or store action was taken.")
    app = page.eval(f"(a => a && {{name: a.display_name, installed: !!a.local_per_client_data?.installed}})"
                    f"(appStore.GetAppOverviewByAppID({appid}))")
    if app and app["installed"]:
        return {"state": "installed", "message": f"{app['name']} is already installed"}
    name = app["name"] if app else str(appid)
    # The URL goes through Steam's own handler, which marks the install as
    # expected and skips the options dialog when there is one library folder
    # and enough space (verified 2026-09-25). A bare OpenInstallWizard doesn't.
    steam_url(f"steam://install/{appid}")
    w = None
    deadline = time.time() + 12
    while time.time() < deadline:
        time.sleep(0.4)
        w = page.eval(WIZARD_JS)
        if w["app"] == appid and w["state"] not in BUSY:
            break
    if not w or w["app"] != appid:
        # Steam didn't open a wizard for this app in time, or another app's is open.
        if not app:
            return {"state": "unknown", "message": f"Asked Steam to install {appid}. It isn't in this account's "
                    "library, so Steam may be showing a store or license dialog in the headset"}
        other = f" Another install dialog (app {w['app']}) is open in the headset." if w and w["app"] else ""
        return {"state": "unknown", "message": f"Asked Steam to install {name}, but it didn't start within 12 s.{other}"}
    s = w["state"]
    # Steam stops at the options dialog when it wants to show a compatibility
    # note or the disk settings. Accept its defaults (the default library
    # folder) when the game fits, as the headset's own Install button does.
    if s == 7 and 0 < w["need"] < w["free"]:
        page.eval("SteamClient.Installs.ContinueInstall()")
        time.sleep(1.5)
        w = page.eval(WIZARD_JS)
        s = 14 if w["state"] in (0, 14) else w["state"]
    label = STATE.get(s, str(s))
    if s == 14:
        return {"state": "downloading", "message": f"{name} is queued to download on the Frame"}
    if s == 7:
        return {"state": "headset", "message": f"Steam is showing install options for {name} in the headset "
                f"(needs {w['need'] / 1e9:.1f} GB, {w['free'] / 1e9:.0f} GB free)"}
    if s in NEEDS_HEADSET:
        return {"state": "headset", "message": f"Steam needs you to accept the {label} for {name} in the headset"}
    if s == 15:
        raise Fail(f"Steam couldn't install {name}: {w.get('detail') or 'error ' + str(w.get('error'))}")
    if not app:
        return {"state": "unknown", "message": f"Asked Steam to install {appid}. It isn't in this account's "
                "library, so Steam may be showing a store or license dialog in the headset"}
    return {"state": label, "message": f"Asked Steam to install {name} (wizard: {label})"}


def store(appid):
    steam_url(f"steam://store/{appid}")
    return {"message": "Opened the store page in the headset"}


def main():
    try:
        cmd = sys.argv[1] if len(sys.argv) > 1 else ""
        if cmd == "owned":
            out = owned()
        elif cmd == "live":
            out = live()
        elif cmd == "utilities":
            out = {"utilities": Page().eval(UTILITIES_JS)}
        elif cmd in ("install", "store") and len(sys.argv) == 3 and sys.argv[2].isdigit():
            out = (install if cmd == "install" else store)(int(sys.argv[2]))
        else:
            raise Fail("usage: owned | live | install APPID | store APPID")
    except (Fail, OSError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    except Exception as e:  # keep the one-JSON-object contract for the server
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
