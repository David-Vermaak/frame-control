"""What the Frame's Steam client reports, folded into /api/status: Steam's battery
percentage, remembered controllers, and frame_steam.py's LIVE_JS run in Node
against stand-in Steam objects.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_live  # noqa: E402
import frame_steam  # noqa: E402

LEFT = {"serial": "L1", "model": "Left Controller", "kind": 2, "battery": 0.8, "charging": False, "connected": True}
RIGHT = {"serial": "R1", "model": "Right Controller", "kind": 2, "battery": 0.45, "charging": True, "connected": True}


def fresh_store():
    frame_live._store().unlink(missing_ok=True)


class Merge(unittest.TestCase):
    def setUp(self):
        fresh_store()

    def status(self):
        return {"hostname": "steamframe", "battery": {"percent": 72, "status": "Discharging"}}

    def test_steam_percent_replaces_the_kernels(self):
        s = frame_live.merge(self.status(), {"battery": {"percent": 80}, "devices": []})
        self.assertEqual(s["battery"]["percent"], 80)
        self.assertEqual(s["battery"]["rawPercent"], 72)
        self.assertEqual(s["battery"]["status"], "Discharging")

    def test_percent_is_clamped(self):
        s = frame_live.merge(self.status(), {"battery": {"percent": 104}})
        self.assertEqual(s["battery"]["percent"], 100)

    def test_no_steam_leaves_the_kernel_reading(self):
        for live in (None, "garbage", {"battery": None}, {"battery": {"percent": None}}):
            with self.subTest(live=live):
                s = frame_live.merge(self.status(), live)
                self.assertEqual(s["battery"]["percent"], 72)
                self.assertNotIn("rawPercent", s["battery"])

    def test_steam_section(self):
        dl = {"queue": [{"appid": 620}], "current": None}
        s = frame_live.merge(self.status(), {"downloads": dl, "running": [{"appid": 620, "name": "Portal 2"}],
                                             "devices": [LEFT]})
        self.assertEqual(s["steam"]["downloads"], dl)
        self.assertEqual(s["steam"]["running"][0]["name"], "Portal 2")
        self.assertEqual([c["model"] for c in s["steam"]["controllers"]], ["Left Controller"])
        self.assertIsNone(frame_live.merge(self.status(), None)["steam"])


class Controllers(unittest.TestCase):
    def setUp(self):
        fresh_store()

    def test_sleeping_controller_is_remembered(self):
        frame_live.controllers("frame-a", [LEFT, RIGHT], now=1000)
        # SteamVR restarted: only the left one has reconnected.
        out = frame_live.controllers("frame-a", [LEFT], now=2000)
        self.assertEqual([(c["model"], c["connected"]) for c in out],
                         [("Left Controller", True), ("Right Controller", False)])
        right = out[1]
        self.assertTrue(right["remembered"])
        self.assertEqual(right["battery"], 0.45)
        self.assertFalse(right["charging"])
        self.assertEqual(right["lastSeen"], 1000)

    def test_no_steamvr_lists_remembered_only(self):
        frame_live.controllers("frame-a", [LEFT], now=1000)
        out = frame_live.controllers("frame-a", None, now=2000)
        self.assertEqual([(c["model"], c["connected"]) for c in out], [("Left Controller", False)])

    def test_listed_but_disconnected_keeps_last_seen(self):
        frame_live.controllers("frame-a", [LEFT], now=1000)
        out = frame_live.controllers("frame-a", [{**LEFT, "connected": False, "battery": None}], now=2000)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["connected"])
        self.assertEqual(out[0]["lastSeen"], 1000)

    def test_per_headset(self):
        frame_live.controllers("frame-a", [LEFT], now=1000)
        self.assertEqual(frame_live.controllers("frame-b", [], now=1000), [])

    def test_no_serial_is_shown_but_not_remembered(self):
        out = frame_live.controllers("frame-a", [{**LEFT, "serial": None}], now=1000)
        self.assertEqual(len(out), 1)
        self.assertEqual(frame_live.controllers("frame-a", [], now=1001), [])

    def test_last_seen_alone_is_written_only_every_few_minutes(self):
        frame_live.controllers("frame-a", [LEFT], now=1000)
        with mock.patch.object(frame_live, "_save") as save:
            frame_live.controllers("frame-a", [LEFT], now=1100)
            save.assert_not_called()
            frame_live.controllers("frame-a", [LEFT], now=1400)
            save.assert_called_once()
        with mock.patch.object(frame_live, "_save") as save:
            frame_live.controllers("frame-a", [{**LEFT, "battery": 0.7}], now=1001)
            save.assert_called_once()

    def test_unreadable_store_is_empty(self):
        path = frame_live._store()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertEqual(frame_live.controllers("frame-a", [], now=1000), [])
        path.write_text("[]")
        self.assertEqual(frame_live.controllers("frame-a", [], now=1000), [])


# Stand-ins for Steam's SharedJSContext, shaped as FrameMate reads them.
STEAM_JS = r"""
let unregistered = 0;
const handle = () => ({ unregister() { unregistered++; } });
const appStore = { GetAppOverviewByAppID: id => ({ 620: { display_name: "Portal 2" }, 400: { display_name: "Portal" } })[id] };
const downloadsStore = { m_DownloadOverview: new Map([["0", { update_appid: 620, paused: false,
  overall_percent_complete: 41, overall_estimated_time_remaining_sec: 90, update_network_bytes_per_second: 12000000 }]]) };
const SteamUIStore = { RunningApps: [{ appid: 400, display_name: "Portal" }] };
const PROPS = {
  "/user/hand/left": { 1001: "Left Controller", 1002: "L1", 1029: 2, 1012: 0.8, 1011: false },
  "/user/hand/right": { 1001: "Right Controller", 1002: "R1", 1029: 2, 1012: 0.45, 1011: true },
  "/user/head": { 1001: "Steam Frame", 1002: "H1", 1029: 1, 1012: 0.9, 1011: false },
};
const prop = async (path, id) => { const v = PROPS[path][id]; if (v === undefined) throw Error("no prop"); return v; };
const SteamClient = {
  System: { RegisterForBatteryStateChanges(cb) {   // fires inside register()
    cb({ bHasBattery: true, flLevel: 0.796, nSecondsRemaining: 5400, eACState: 1 }); return handle(); } },
  Downloads: { RegisterForDownloadItems(cb) {
    setTimeout(() => cb(true, [
      { remote_client_id: "0", item_data: [
        { appid: 620, queue_index: 0, active: true, paused: false, completed: false, overall_percent_complete: 41 },
        { appid: 400, queue_index: 1, active: false, paused: false, completed: false },
        { appid: 4427310, queue_index: -1, active: false, paused: false, completed: false, deferred_time: 4e9 }] },
      { remote_client_id: "123456", item_data: [{ appid: 999, queue_index: 0, active: true }] }]), 5);
    return handle(); } },
  OpenVR: {
    RegisterForVRTrackedDevices(cb) {
      if (globalThis.NO_STEAMVR) return handle();
      setTimeout(() => cb(Object.keys(PROPS)), 5); return handle(); },
    Device: { async BIsConnected(path) { return path !== "/user/hand/right"; } },
    DeviceProperties: { GetStringDeviceProperty: prop, GetInt32DeviceProperty: prop,
                        GetFloatDeviceProperty: prop, GetBoolDeviceProperty: prop },
  },
};
"""


@unittest.skipUnless(shutil.which("node"), "Node exercises the fake Steam JS context")
class LiveJavaScript(unittest.TestCase):
    def run_js(self, prelude=""):
        js = STEAM_JS + prelude + ";\n" + frame_steam.LIVE_JS + \
            ".then(r => console.log(JSON.stringify({ r, unregistered })));"
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True, timeout=20)
        return json.loads(out.stdout)

    def test_reads_steam(self):
        got = self.run_js()
        r = got["r"]
        self.assertEqual(r["battery"], {"percent": 80, "secondsLeft": 5400, "onAC": False})
        # The other PC's download (Remote Downloads) is left out.
        self.assertEqual([(i["appid"], i["position"], i["scheduledFor"]) for i in r["downloads"]["queue"]],
                         [(620, 0, None), (400, 1, None), (4427310, -1, 4e9)])
        self.assertEqual(r["downloads"]["queue"][0]["name"], "Portal 2")
        self.assertEqual(r["downloads"]["current"], {"appid": 620, "paused": False, "percent": 41,
                                                     "eta": 90, "bps": 12000000})
        # Controllers only, not the headset itself.
        self.assertEqual([(d["serial"], d["battery"], d["charging"], d["connected"]) for d in r["devices"]],
                         [("L1", 0.8, False, True), ("R1", 0.45, True, False)])
        self.assertEqual(r["running"], [{"appid": 400, "name": "Portal"}])
        self.assertEqual(got["unregistered"], 3)  # every subscription let go, even the one fired in register()

    def test_missing_answers_are_null(self):
        r = self.run_js("globalThis.NO_STEAMVR = true; SteamClient.System.RegisterForBatteryStateChanges = "
                        "() => { throw Error('gone'); }")["r"]
        self.assertIsNone(r["battery"])
        self.assertIsNone(r["devices"])
        self.assertEqual(len(r["downloads"]["queue"]), 3)


class StatusRoute(unittest.TestCase):
    def setUp(self):
        fresh_store()
        import server
        self.server = server
        self.raw = {"hostname": "steamframe", "os": {}, "battery": {"percent": 72}}

    def test_steam_answer_is_merged(self):
        with mock.patch.object(self.server, "ssh", return_value=json.dumps(self.raw)), \
             mock.patch.object(self.server, "steam_frame", return_value={"battery": {"percent": 80}}) as steam:
            s = self.server.status({})
        steam.assert_called_once_with("live", timeout=10)
        self.assertEqual(s["battery"]["percent"], 80)
        self.assertEqual(s["steam"]["controllers"], [])

    def test_steam_failure_never_fails_the_status(self):
        with mock.patch.object(self.server, "ssh", return_value=json.dumps(self.raw)), \
             mock.patch.object(self.server, "steam_frame", side_effect=self.server.Failure("no SharedJSContext page")):
            s = self.server.status({})
        self.assertEqual(s["battery"]["percent"], 72)
        self.assertIsNone(s["steam"])


if __name__ == "__main__":
    unittest.main()
