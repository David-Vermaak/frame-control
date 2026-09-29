#!/usr/bin/env python3
"""Runs ON the Frame (piped over SSH): UEVR, a flat-to-VR mod for Unreal games.

Usage: python3 - status APPID     # can UEVR run here, is it installed, is the game running
       python3 - install APPID    # download the pinned official release into the game's prefix
       python3 - start APPID      # launch the game if needed, inject UEVR, confirm SteamVR frames
       python3 - uninstall APPID  # remove exactly what install (and UEVR's first run) created
Prints one JSON object. Errors are {"error": "..."} with exit status 1.

Verified 2026-09-29 on SteamOS 0.4.1 (build 20260925.6191901), Proton 11.0
ARM64 and SteamVR 2.18.1 with Gravitas (1067310): UEVR 1.05's injector runs in
the game's own Wine session, and after OpenVR + Inject the game loads
UEVRBackend.dll and SteamVR counts frame submits for it. Stereo image, head
tracking and controls were not seen: the headset was unworn (docs/mods.md).

Nothing here is bundled. UEVR comes from praydog's GitHub release, and the
Windows .NET 6 runtime it needs from Microsoft's release metadata URLs, each
checked against a pinned hash. Files live in <prefix>/drive_c/frame-control,
never in the game's own folder, and a receipt records what to remove.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HOME = Path.home()
STEAM = HOME / ".local/share/Steam"
CACHE = HOME / ".cache/frame-control/mods"
RECEIPTS = HOME / ".local/share/frame-control/mods"
VRCMD = "/opt/steamvr/bin/linuxarm64/vrcmd"
MANAGED = "frame-control"  # drive_c/<this> inside the game's prefix

UEVR = {"version": "1.05", "dir": "uevr-1.05",
        "url": "https://github.com/praydog/UEVR/releases/download/1.05/UEVR.zip",
        "sha256": "af4f2f91306802d7ee4e8497d483a547ac8e9a3067dbafb81324100524215d3c"}
# UEVRInjector.exe is a .NET 6 WPF app; Proton ships no .NET. These are the
# win-x64 ZIPs listed in builds.dotnet.microsoft.com/dotnet/release-metadata/6.0/releases.json.
DOTNET = {"version": "6.0.36", "dir": "dotnet-6.0.36", "files": [
    ("https://builds.dotnet.microsoft.com/dotnet/Runtime/6.0.36/dotnet-runtime-6.0.36-win-x64.zip",
     "935db5c6cee19f2c016e67168bfae7b491044735de76c673abb3b125dd325fd5e779d7efe12ba80178d46689ae70a25e558a3fa846417d44c5f4ca256e7f4bf2"),
    ("https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/windowsdesktop-runtime-6.0.36-win-x64.zip",
     "cee88fef07643dceb3d34ea71b64eeae85b8e39e7042bc4d35bc3a1f1df29373681dc48e31c01c9f593ec412699f6762b6fc5817433283c89df35a27ce8885bf")]}
MAX_UNPACKED = 600 << 20  # the three archives unpack to about 190 MB

# UEVR 1.05's injector window (625 px wide), in window coordinates. Under
# gamescope the full-screen game keeps the pointer and focus, so real clicks
# can't reach the injector; XSendEvent to its window does. It doesn't restore
# its saved runtime choice under Wine, and --attach didn't inject, so choose
# OpenVR (SteamVR's own API) and press Inject every time.
INJECTOR_WIDTH = 625
CLICK_OPENVR, CLICK_INJECT = (125, 137), (365, 113)
# The game's environment the injector needs to join its Wine session. Not
# WINESERVERSOCKET and friends: those are inherited file descriptors.
GAME_ENV = re.compile(r"(WINEPREFIX|WINEDLLPATH|WINEDLLOVERRIDES|WINEFSYNC|WINEDEBUG|PATH|DISPLAY|"
                      r"XDG_RUNTIME_DIR|PROTON_VR_RUNTIME|FEX_APP_CONFIG|FEX_APP_CONFIG_LOCATION|"
                      r"SteamAppId|SteamGameId|STEAM_COMPAT_DATA_PATH|WINE_LARGE_ADDRESS_AWARE)=")


class Fail(Exception):
    pass


def libraries():
    """Steam library folders: the default one plus any in libraryfolders.vdf."""
    libs = [STEAM]
    try:
        text = (STEAM / "steamapps/libraryfolders.vdf").read_text(errors="replace")
        libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
    except OSError:
        pass
    seen, out = set(), []
    for lib in libs:
        key = os.path.realpath(lib)
        if key not in seen:
            seen.add(key)
            out.append(lib)
    return out


def game(appid):
    """Where the game is installed and its Proton prefix, from its app manifest."""
    for lib in libraries():
        manifest = lib / f"steamapps/appmanifest_{appid}.acf"
        try:
            text = manifest.read_text(errors="replace")
        except OSError:
            continue
        m = re.search(r'"installdir"\s+"([^"]+)"', text)
        if not m:
            continue
        name = re.search(r'"name"\s+"([^"]*)"', text)
        return {"name": name.group(1) if name else str(appid),
                "dir": lib / "steamapps/common" / m.group(1),
                "prefix": lib / f"steamapps/compatdata/{appid}/pfx"}
    raise Fail(f"app {appid} isn't installed on this Frame")


def shipping_exe(game_dir):
    """The Unreal Engine game binary (…/Binaries/Win64/*-Win64-Shipping.exe), or None."""
    found = [p for p in game_dir.glob("*/Binaries/Win64/*.exe") if p.name.lower().endswith("-win64-shipping.exe")]
    found += [p for p in game_dir.glob("Binaries/Win64/*.exe") if p.name.lower().endswith("-win64-shipping.exe")]
    return max(found, key=lambda p: p.stat().st_size) if found else None


def game_pid(exe_name):
    """The running Wine process for this .exe (its cmdline is the Windows path)."""
    want = "\\" + exe_name.lower()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().split(b"\0")[0].decode(errors="replace").lower()
        except OSError:
            continue
        if cmd.endswith(want):
            return int(d.name)
    return None


def loaded(pid, name):
    try:
        return any(line.rstrip().endswith("/" + name) for line in open(f"/proc/{pid}/maps"))
    except OSError:
        return False


def receipt_path(appid):
    return RECEIPTS / f"{appid}-uevr.json"


def read_receipt(appid):
    try:
        return json.loads(receipt_path(appid).read_text())
    except (OSError, ValueError):
        return None


def managed_dir(g):
    return g["prefix"] / "drive_c" / MANAGED


def user_dirs(g, exe):
    """What UEVR itself writes into the prefix when it first runs."""
    user = g["prefix"] / "drive_c/users/steamuser/AppData"
    return {"profile": user / "Roaming/UnrealVRMod" / exe.stem,  # per-game settings and log.txt
            "injector": user / "Local/praydog"}                  # the injector's own settings


def status(appid):
    g = game(appid)
    exe = shipping_exe(g["dir"])
    r = read_receipt(appid)
    pid = game_pid(exe.name) if exe else None
    return {"appid": appid, "name": g["name"], "mod": "UEVR", "version": UEVR["version"],
            "eligible": bool(exe), "exe": exe.name if exe else None,
            "prefix": g["prefix"].is_dir(), "installed": bool(r),
            "running": bool(pid), "injected": bool(pid and loaded(pid, "UEVRBackend.dll"))}


def fetch(url, digest, algo):
    """Download into the cache once, and check its hash every time it's used."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / url.rsplit("/", 1)[1]
    if not path.exists():
        part = path.with_suffix(path.suffix + ".part")
        try:
            with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        except OSError as e:
            part.unlink(missing_ok=True)
            raise Fail(f"download failed: {url}: {e}")
        part.rename(path)
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    if h.hexdigest() != digest:
        path.unlink()
        raise Fail(f"{path.name} doesn't match its published {algo}; deleted it, try again")
    return path


def unpack(archive, dest, budget):
    """Extract a ZIP into dest, refusing anything that would land outside it."""
    root = os.path.realpath(dest)
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = os.path.realpath(os.path.join(dest, info.filename))
            if target != root and not target.startswith(root + os.sep):
                raise Fail(f"{archive.name} has an entry outside its folder: {info.filename}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise Fail(f"{archive.name} contains a symlink: {info.filename}")
            budget -= info.file_size
            if budget < 0:
                raise Fail(f"{archive.name} unpacks to more than expected")
        z.extractall(dest)
    return budget


def install(appid):
    g = game(appid)
    exe = shipping_exe(g["dir"])
    if not exe:
        raise Fail(f"{g['name']} doesn't look like an Unreal Engine game (no *-Win64-Shipping.exe), so UEVR can't hook it")
    if not g["prefix"].is_dir():
        raise Fail(f"{g['name']} has no Proton prefix yet; play it once, then add the mod")
    if game_pid(exe.name):
        raise Fail(f"{g['name']} is running; quit it first")
    if read_receipt(appid):
        return {"message": f"UEVR {UEVR['version']} is already installed for {g['name']}", **status(appid)}
    archives = [(fetch(UEVR["url"], UEVR["sha256"], "sha256"), UEVR["dir"])]
    archives += [(fetch(url, digest, "sha512"), DOTNET["dir"]) for url, digest in DOTNET["files"]]
    base = managed_dir(g)
    dirs = user_dirs(g, exe)
    fresh = [str(d) for d in (dirs["profile"].parent, base) if not d.exists()]  # remove on uninstall if empty
    stage = base / f".staging-{os.getpid()}"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        budget = MAX_UNPACKED
        for archive, sub in archives:
            budget = unpack(archive, stage / sub, budget)
        if not (stage / UEVR["dir"] / "UEVRInjector.exe").is_file():
            raise Fail("UEVR.zip has no UEVRInjector.exe")
        for sub in (UEVR["dir"], DOTNET["dir"]):
            shutil.rmtree(base / sub, ignore_errors=True)
            (stage / sub).rename(base / sub)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if str(base) in fresh:
            try:
                base.rmdir()  # only succeeds if the install failed and left it empty
            except OSError:
                pass
    receipt = {"appid": appid, "mod": "UEVR", "version": UEVR["version"], "exe": exe.name,
               "installed": int(time.time()), "prefix": str(g["prefix"]),
               "remove": [str(base / UEVR["dir"]), str(base / DOTNET["dir"])],
               # Parents that didn't exist yet (UEVR makes UnrealVRMod), deepest first.
               "remove_if_empty": fresh,
               # UEVR creates these on first injection. Remove them on uninstall
               # only if they weren't there before, so earlier settings survive.
               "remove_if_created": {k: str(v) for k, v in dirs.items() if not v.exists()},
               "sources": [UEVR["url"]] + [u for u, _ in DOTNET["files"]]}
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    tmp = receipt_path(appid).with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, indent=1))
    tmp.rename(receipt_path(appid))
    return {"message": f"Installed UEVR {UEVR['version']} for {g['name']}", **status(appid)}


def x11_windows(display):
    """(id, name, width) for each named top-level window, via xwininfo."""
    try:
        out = subprocess.run(["xwininfo", "-root", "-tree"], env={**os.environ, "DISPLAY": display},
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [(int(m[1], 16), m[2], int(m[3])) for m in re.finditer(r'(0x[0-9a-f]+) "([^"]*)":.*?\)\s+(\d+)x\d+', out)]


def x_click(display, window, points):
    """Send ButtonPress/Release straight to a window (gamescope blocks real input to it)."""
    import ctypes as C
    x11 = C.CDLL("libX11.so.6")
    x11.XOpenDisplay.restype = C.c_void_p

    class Button(C.Structure):
        _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int), ("display", C.c_void_p),
                    ("window", C.c_ulong), ("root", C.c_ulong), ("subwindow", C.c_ulong), ("time", C.c_ulong),
                    ("x", C.c_int), ("y", C.c_int), ("x_root", C.c_int), ("y_root", C.c_int),
                    ("state", C.c_uint), ("button", C.c_uint), ("same_screen", C.c_int)]

    class Event(C.Union):
        _fields_ = [("xbutton", Button), ("pad", C.c_long * 24)]

    d = x11.XOpenDisplay(display.encode())
    if not d:
        raise Fail(f"can't open X display {display}")
    d = C.c_void_p(d)
    root = x11.XDefaultRootWindow(d)
    ox, oy, child = C.c_int(), C.c_int(), C.c_ulong()
    x11.XTranslateCoordinates(d, C.c_ulong(window), C.c_ulong(root), 0, 0, C.byref(ox), C.byref(oy), C.byref(child))
    for x, y in points:
        for kind, mask, state in ((4, 1 << 2, 0), (5, 1 << 3, 1 << 8)):  # press, then release with Button1 held
            e = Event()
            b = e.xbutton
            b.type, b.send_event, b.window, b.root, b.x, b.y = kind, 1, window, root, x, y
            b.x_root, b.y_root, b.state, b.button, b.same_screen = x + ox.value, y + oy.value, state, 1, 1
            x11.XSendEvent(d, C.c_ulong(window), 1, C.c_long(mask), C.byref(e))
            x11.XFlush(d)
            time.sleep(0.15)
        time.sleep(1.5)
    x11.XCloseDisplay(d)


def frame_submits(appid):
    """SteamVR's count of frames the app has submitted, or None if it isn't a scene app."""
    try:
        stats = json.loads(subprocess.run([VRCMD, "--stats"], capture_output=True, text=True, timeout=15).stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return next((s.get("frame_submits") for s in stats if s.get("key") == f"steam.app.{appid}"), None)


def wait(what, seconds, check, step=2):
    end = time.time() + seconds
    while time.time() < end:
        v = check()
        if v:
            return v
        time.sleep(step)
    raise Fail(f"timed out waiting for {what}")


def start(appid):
    g = game(appid)
    r = read_receipt(appid)
    if not r:
        raise Fail(f"UEVR isn't installed for {g['name']}")
    exe = r["exe"]
    pid = game_pid(exe)
    if pid and loaded(pid, "UEVRBackend.dll"):
        return {"message": f"UEVR is already running in {g['name']}", "frame_submits": frame_submits(appid), **status(appid)}
    if not pid:
        subprocess.Popen(["steam", f"steam://rungameid/{appid}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")}, start_new_session=True)
        pid = wait(f"{g['name']} to start", 120, lambda: game_pid(exe))
    env = dict(line.split("=", 1) for line in open(f"/proc/{pid}/environ").read().split("\0")
               if GAME_ENV.match(line))
    display = env.get("DISPLAY")
    if not display or "WINEPREFIX" not in env:
        raise Fail(f"{exe} doesn't look like a Proton game process")
    # The game needs its D3D11 window before UEVR can hook it. Proton's own
    # helper windows ("Steam", "Default IME", …) are small.
    wait("the game window", 90, lambda: any(w[2] >= 640 and w[1] != "UEVR" for w in x11_windows(display)))
    time.sleep(10)
    root = f"C:\\{MANAGED}\\{DOTNET['dir']}"
    # Our own environment (HOME and the rest) under the game's Wine settings:
    # with only the game's variables, fontconfig had no cache directory.
    env = {**os.environ, **env, "DOTNET_ROOT": root, "DOTNET_ROOT_X64": root}
    # The .NET injector sometimes dies at startup under Wine and FEX (an
    # AccessViolationException, 2026-09-29), so give it a few tries.
    for attempt in range(3):
        before = {w[0] for w in x11_windows(display) if w[1] == "UEVR"}
        log = open(CACHE / f"injector-{appid}.log", "w") if CACHE.is_dir() else subprocess.DEVNULL
        injector = subprocess.Popen(["wine", f"C:\\{MANAGED}\\{UEVR['dir']}\\UEVRInjector.exe"], env=env,
                                    stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                                    cwd=str(Path(r["prefix"]) / "drive_c" / MANAGED / UEVR["dir"]))
        try:
            win = wait("the UEVR injector window", 90,
                       lambda: injector.poll() is not None or next(
                           (w for w in x11_windows(display) if w[1] == "UEVR" and w[0] not in before), None))
        except Fail:
            os.killpg(injector.pid, signal.SIGKILL)
            raise
        if win is not True:
            break
    else:
        raise Fail(f"the UEVR injector crashed on start {attempt + 1} times; "
                   f"its log is in ~/.cache/frame-control/mods/injector-{appid}.log")
    try:
        # It maps at a default size, then WPF lays it out (seen: 960 px, then 625).
        try:
            wait("the UEVR window's layout", 30,
                 lambda: any(w[0] == win[0] and w[2] == INJECTOR_WIDTH for w in x11_windows(display)), step=1)
        except Fail:
            width = next((w[2] for w in x11_windows(display) if w[0] == win[0]), "?")
            raise Fail(f"the UEVR window is {width} px wide, not {INJECTOR_WIDTH}; "
                       "its layout changed, so not clicking blind") from None
        time.sleep(5)  # let it fill its process list
        x_click(display, win[0], [CLICK_OPENVR, CLICK_INJECT])
        try:
            wait("UEVRBackend.dll to load in the game", 45, lambda: loaded(pid, "UEVRBackend.dll"))
        except Fail:
            raise Fail(f"UEVR didn't inject into {exe}. Its log is in ~/.cache/frame-control/mods/injector-{appid}.log") from None
    except Fail:
        os.killpg(injector.pid, signal.SIGKILL)  # don't leave an injector window behind a failed start
        raise
    submits = None
    try:
        submits = wait("SteamVR frames", 30, lambda: frame_submits(appid))
    except Fail:
        pass
    return {"message": f"UEVR injected into {g['name']}" + ("; SteamVR is receiving its frames" if submits else
                                                            "; SteamVR hasn't reported frames from it yet"),
            "frame_submits": submits, **status(appid)}


def uninstall(appid):
    r = read_receipt(appid)
    if not r:
        raise Fail(f"UEVR isn't installed for app {appid}")
    if game_pid(r["exe"]):
        raise Fail("the game is running; quit it first")
    prefix = os.path.realpath(r["prefix"])
    targets = list(r.get("remove", [])) + list(r.get("remove_if_created", {}).values())
    for t in targets:  # a receipt only ever points inside this game's prefix
        real = os.path.realpath(t)
        if not real.startswith(prefix + os.sep):
            raise Fail(f"receipt lists a path outside the game's prefix: {t}")
    for t in targets:
        shutil.rmtree(t, ignore_errors=True)
    for t in r.get("remove_if_empty", []):
        if os.path.realpath(t).startswith(prefix + os.sep):
            try:
                os.rmdir(t)
            except OSError:
                pass  # something else lives there now
    receipt_path(appid).unlink()
    if not any(RECEIPTS.glob("*-uevr.json")):  # no other game uses the downloads
        shutil.rmtree(CACHE, ignore_errors=True)
        try:
            RECEIPTS.rmdir()
        except OSError:
            pass
    return {"message": "Removed UEVR", "appid": appid, "removed": targets}


def main(argv):
    if len(argv) != 2 or argv[0] not in ("status", "install", "start", "uninstall") or not argv[1].isdigit():
        raise Fail("usage: status|install|start|uninstall APPID")
    return {"status": status, "install": install, "start": start, "uninstall": uninstall}[argv[0]](int(argv[1]))


if __name__ == "__main__":
    try:
        print(json.dumps(main(sys.argv[1:])))
    except Fail as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
