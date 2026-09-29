#!/usr/bin/env python3
"""Runs ON the Frame (piped over SSH): UEVR, a flat-to-VR mod for Unreal games.

Usage: python3 - status APPID     # can UEVR run here, is it installed, is the game running
       python3 - install APPID    # download the pinned official release into the game's prefix
       python3 - start APPID      # launch the game if needed, inject UEVR, confirm SteamVR frames
       python3 - uninstall APPID  # remove exactly what install (and UEVR's first run) created
Prints one JSON object. Errors are {"error": "..."} with exit status 1.

Verified 2026-09-29 on SteamOS 0.4.1 (build 20260925.6191901), Proton 11.0
ARM64 and SteamVR 2.18.1 with Gravitas (1067310): after injection the game has
UEVRBackend.dll loaded and SteamVR counts frame submits for it. Stereo image,
head tracking and controls were not seen: the headset was unworn (docs/mods.md).

UEVR's own injector (a .NET WPF app) is not used. Under Wine and FEX it hung
or ignored input in about one session in four, and gamescope keeps real input
away from it. Instead the official Windows embeddable Python runs INJECT_PY in
the game's Wine session, doing what the injector does on Inject.

Nothing here is bundled. UEVR comes from praydog's GitHub release and Python
from python.org, each checked against a pinned hash. Files live in
<prefix>/drive_c/frame-control, never in the game's own folder, and a receipt
records what to remove.
"""
import hashlib
import json
import os
import re
import shutil
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
LOCK = Path("/tmp/frame-control-mods.lock")  # tmpfs: gone at reboot, never left in ~
VRCMD = "/opt/steamvr/bin/linuxarm64/vrcmd"
MANAGED = "frame-control"  # drive_c/<this> inside the game's prefix

UEVR = {"version": "1.05", "dir": "uevr-1.05",
        "url": "https://github.com/praydog/UEVR/releases/download/1.05/UEVR.zip",
        "sha256": "af4f2f91306802d7ee4e8497d483a547ac8e9a3067dbafb81324100524215d3c"}
# Windows x64 embeddable Python, to run INJECT_PY. Hash from python.org's
# release-file API (sha256_sum).
PYTHON = {"version": "3.14.7", "dir": "python-3.14.7",
          "url": "https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip",
          "sha256": "d297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15"}
MAX_UNPACKED = 200 << 20  # the two archives unpack to about 50 MB
MAX_DOWNLOAD = 50 << 20  # each archive is 7–13 MB

# The game's environment a second program needs to join its Wine session. Not
# WINESERVERSOCKET and friends: those are inherited file descriptors.
GAME_ENV = re.compile(r"(WINEPREFIX|WINEDLLPATH|WINEDLLOVERRIDES|WINEFSYNC|WINEDEBUG|PATH|DISPLAY|"
                      r"XDG_RUNTIME_DIR|PROTON_VR_RUNTIME|FEX_APP_CONFIG|FEX_APP_CONFIG_LOCATION|"
                      r"SteamAppId|SteamGameId|STEAM_COMPAT_DATA_PATH|WINE_LARGE_ADDRESS_AWARE)=")


# Runs under Windows Python in the game's Wine session: the steps of UEVR
# 1.05's frontend (UEVR/MainWindow.xaml.cs Inject_Clicked, UEVR/Injector.cs)
# with "Nullify VR plugins" on and the OpenVR runtime. Prints one JSON line.
INJECT_PY = r"""
import ctypes, json, os, sys
from ctypes import wintypes as W
exe, uevr, profile = sys.argv[1:4]
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
H, P, SZ = W.HANDLE, ctypes.c_void_p, ctypes.c_size_t
for name, res, args in (
        ("OpenProcess", H, [W.DWORD, W.BOOL, W.DWORD]),
        ("CloseHandle", W.BOOL, [H]),
        ("VirtualAllocEx", P, [H, P, SZ, W.DWORD, W.DWORD]),
        ("VirtualFreeEx", W.BOOL, [H, P, SZ, W.DWORD]),
        ("WriteProcessMemory", W.BOOL, [H, P, P, SZ, P]),
        ("CreateRemoteThread", H, [H, P, SZ, P, P, W.DWORD, P]),
        ("WaitForSingleObject", W.DWORD, [H, W.DWORD]),
        ("GetModuleHandleW", W.HMODULE, [W.LPCWSTR]),
        ("LoadLibraryW", W.HMODULE, [W.LPCWSTR]),
        ("FreeLibrary", W.BOOL, [W.HMODULE]),
        ("GetProcAddress", P, [W.HMODULE, ctypes.c_char_p]),
        ("CreateToolhelp32Snapshot", H, [W.DWORD, W.DWORD]),
        ("Process32FirstW", W.BOOL, [H, P]), ("Process32NextW", W.BOOL, [H, P]),
        ("Module32FirstW", W.BOOL, [H, P]), ("Module32NextW", W.BOOL, [H, P])):
    fn = getattr(k32, name)
    fn.restype, fn.argtypes = res, args
INVALID = ctypes.c_void_p(-1).value

class Entry(ctypes.Structure):  # PROCESSENTRY32W
    _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD), ("heap", ctypes.c_void_p),
                ("module", W.DWORD), ("threads", W.DWORD), ("parent", W.DWORD), ("prio", ctypes.c_long),
                ("flags", W.DWORD), ("name", W.WCHAR * 260)]

class Module(ctypes.Structure):  # MODULEENTRY32W
    _fields_ = [("size", W.DWORD), ("mid", W.DWORD), ("pid", W.DWORD), ("glbl", W.DWORD), ("proc", W.DWORD),
                ("base", ctypes.c_void_p), ("bytes", W.DWORD), ("handle", W.HMODULE),
                ("name", W.WCHAR * 256), ("path", W.WCHAR * 260)]

def fail(msg):
    print(json.dumps({"error": f"{msg} (Windows error {ctypes.get_last_error()})"})); sys.exit(1)

def snapshot(flags, pid, entry, first, next_, match):
    snap = k32.CreateToolhelp32Snapshot(flags, pid)
    if not snap or snap == INVALID:
        fail("couldn't list processes")
    try:
        entry.size = ctypes.sizeof(entry)
        ok = first(snap, ctypes.byref(entry))
        while ok:
            if match(entry):
                return entry
            ok = next_(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)

def remote_call(proc, fn, arg, wait_ms):
    t = k32.CreateRemoteThread(proc, None, 0, fn, arg, 0, None)
    if not t:
        fail("CreateRemoteThread failed")
    try:
        return k32.WaitForSingleObject(t, wait_ms) == 0  # WAIT_OBJECT_0
    finally:
        k32.CloseHandle(t)

def inject(proc, pid, name):
    path = os.path.join(uevr, name)
    data = ctypes.create_unicode_buffer(path)
    mem = k32.VirtualAllocEx(proc, None, ctypes.sizeof(data), 0x3000, 0x04)  # commit|reserve, read/write
    if not mem:
        fail(f"couldn't allocate in {exe}")
    try:
        if not k32.WriteProcessMemory(proc, mem, data, ctypes.sizeof(data), None):
            fail(f"couldn't write into {exe}")
        load = k32.GetProcAddress(k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
        if not remote_call(proc, load, mem, 30000):
            fail(f"loading {name} into {exe} didn't finish in 30 s")
    finally:
        k32.VirtualFreeEx(proc, mem, 0, 0x8000)  # MEM_RELEASE
    m = snapshot(0x18, pid, Module(), k32.Module32FirstW, k32.Module32NextW,  # TH32CS_SNAPMODULE | SNAPMODULE32
                 lambda m: m.path.lower() == path.lower())
    if not m:
        fail(f"{name} didn't load into {exe}")
    return path, m.base

e = snapshot(2, 0, Entry(), k32.Process32FirstW, k32.Process32NextW,  # TH32CS_SNAPPROCESS
             lambda e: e.name.lower() == exe.lower())
if not e:
    fail(f"{exe} isn't running")
pid = e.pid
proc = k32.OpenProcess(0x1F0FFF, False, pid)  # PROCESS_ALL_ACCESS
if not proc:
    fail(f"couldn't open {exe}")
path, base = inject(proc, pid, "UEVRPluginNullifier.dll")
local = k32.LoadLibraryW(path)
fn = local and k32.GetProcAddress(local, b"nullify")
if not fn:
    fail("couldn't find UEVRPluginNullifier.dll's nullify")
offset = fn - local  # same DLL, same layout in both processes
k32.FreeLibrary(local)
# The frontend waits 2 s for nullify and carries on either way; so do we, but say so.
nullified = remote_call(proc, base + offset, None, 2000)
inject(proc, pid, "openvr_api.dll")
# The frontend saves the runtime choice in the per-game config before UEVRBackend reads it.
os.makedirs(profile, exist_ok=True)
cfg = os.path.join(profile, "config.txt")
lines = open(cfg).read().splitlines() if os.path.exists(cfg) else []
lines = [l for l in lines if not l.startswith("Frontend_RequestedRuntime=")] + ["Frontend_RequestedRuntime=openvr_api.dll"]
open(cfg, "w").write("\n".join(lines) + "\n")
inject(proc, pid, "UEVRBackend.dll")
k32.CloseHandle(proc)
print(json.dumps({"pid": pid, "nullified": nullified}))
"""


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
                for block in iter(lambda: r.read(1 << 20), b""):
                    if f.tell() + len(block) > MAX_DOWNLOAD:
                        raise Fail(f"{url} is bigger than expected; stopped the download")
                    f.write(block)
        except (OSError, Fail) as e:
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
    archives.append((fetch(PYTHON["url"], PYTHON["sha256"], "sha256"), PYTHON["dir"]))
    base = managed_dir(g)
    dirs = user_dirs(g, exe)
    fresh = [str(d) for d in (dirs["profile"].parent, base) if not d.exists()]  # remove on uninstall if empty
    stage = base / f".staging-{os.getpid()}"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        budget = MAX_UNPACKED
        for archive, sub in archives:
            budget = unpack(archive, stage / sub, budget)
        for need in (stage / UEVR["dir"] / "UEVRBackend.dll", stage / PYTHON["dir"] / "python.exe"):
            if not need.is_file():
                raise Fail(f"the download has no {need.name}")
        if game_pid(exe.name):  # it may have started during the download
            raise Fail(f"{g['name']} started; quit it first")
        (stage / PYTHON["dir"] / "frame_inject.py").write_text(INJECT_PY)
        for sub in (UEVR["dir"], PYTHON["dir"]):
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
               "remove": [str(base / UEVR["dir"]), str(base / PYTHON["dir"])],
               # Parents that didn't exist yet (UEVR makes UnrealVRMod), deepest first.
               "remove_if_empty": fresh,
               # UEVR creates these on first injection. Remove them on uninstall
               # only if they weren't there before, so earlier settings survive.
               "remove_if_created": {k: str(v) for k, v in dirs.items() if not v.exists()},
               "sources": [UEVR["url"], PYTHON["url"]]}
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


def frame_submits(appid):
    """SteamVR's count of frames the app has submitted, or None if it isn't a scene app."""
    try:
        stats = json.loads(subprocess.run([VRCMD, "--stats"], capture_output=True, text=True, timeout=15).stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if not isinstance(stats, list):
        return None
    return next((s.get("frame_submits") for s in stats if isinstance(s, dict) and s.get("key") == f"steam.app.{appid}"), None)


def wait(what, seconds, check, step=2):
    end = time.time() + seconds
    while time.time() < end:
        v = check()
        if v is not None and v is not False:  # 0 frames is still an answer
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
    try:
        environ = Path(f"/proc/{pid}/environ").read_text(errors="replace")
    except OSError:
        raise Fail(f"{exe} exited") from None
    game_env = dict(line.split("=", 1) for line in environ.split("\0") if GAME_ENV.match(line))
    display = game_env.get("DISPLAY")
    if not display or "WINEPREFIX" not in game_env:
        raise Fail(f"{exe} doesn't look like a Proton game process")
    # UEVR hooks the game's D3D11/12 device, so wait for its window. Proton's
    # own helper windows ("Steam", "Default IME", …) are small.
    wait("the game window", 90, lambda: any(w[2] >= 640 for w in x11_windows(display)))
    time.sleep(10)
    # Our own environment (HOME and the rest) under the game's Wine settings.
    env = {**os.environ, **game_env}
    base = f"C:\\{MANAGED}"
    profile = f"C:\\users\\steamuser\\AppData\\Roaming\\UnrealVRMod\\{Path(exe).stem}"
    try:
        out = subprocess.run(["wine", f"{base}\\{PYTHON['dir']}\\python.exe", f"{base}\\{PYTHON['dir']}\\frame_inject.py",
                              exe, f"{base}\\{UEVR['dir']}", profile], env=env, capture_output=True, text=True,
                             timeout=120, stdin=subprocess.DEVNULL,
                             cwd=str(Path(r["prefix"]) / "drive_c" / MANAGED))
    except subprocess.TimeoutExpired:
        raise Fail("the injection step didn't finish in 2 minutes") from None
    reply = None
    for line in reversed(out.stdout.splitlines()):
        try:
            reply = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(reply, dict):
        reply = None
    if not reply or "error" in reply:
        raise Fail("UEVR injection failed: " + (reply or {}).get("error", (out.stderr.strip().splitlines() or ["no output"])[-1]))
    try:
        wait("UEVRBackend.dll to load in the game", 20, lambda: loaded(pid, "UEVRBackend.dll"))
    except Fail:
        raise Fail(f"the injection reported success but {exe} has no UEVRBackend.dll") from None
    submits = None
    try:
        submits = wait("SteamVR frames", 30, lambda: frame_submits(appid))
    except Fail:
        pass
    return {"message": f"UEVR injected into {g['name']}" + ("; SteamVR is receiving its frames" if submits is not None
                                                            else "; SteamVR hasn't reported frames from it yet"),
            "frame_submits": submits, **status(appid)}


def uninstall(appid):
    r = read_receipt(appid)
    if not r:
        raise Fail(f"UEVR isn't installed for app {appid}")
    if game_pid(r["exe"]):
        raise Fail("the game is running; quit it first")
    prefix = os.path.realpath(r["prefix"])
    if not prefix.endswith(f"/steamapps/compatdata/{appid}/pfx"):
        raise Fail(f"receipt names a prefix that isn't app {appid}'s: {r['prefix']}")
    # Only ever our folder and UEVR's own settings folders for this game, even
    # if the receipt says otherwise.
    managed = os.path.join(prefix, "drive_c", MANAGED)
    uevr_dirs = {os.path.realpath(str(d)) for d in user_dirs({"prefix": Path(prefix)}, Path(r["exe"])).values()}
    targets = list(r.get("remove", [])) + list(r.get("remove_if_created", {}).values())
    for t in targets:
        real = os.path.realpath(t)
        if not (real.startswith(managed + os.sep) or real in uevr_dirs):
            raise Fail(f"receipt lists a path Frame Control didn't create: {t}")
    for t in targets:
        shutil.rmtree(t, ignore_errors=True)
    left = [t for t in targets if os.path.lexists(t)]
    if left:  # keep the receipt, so Remove can be tried again
        raise Fail("couldn't remove " + ", ".join(left))
    parents = {os.path.realpath(os.path.join(prefix, "drive_c/users/steamuser/AppData/Roaming/UnrealVRMod")), managed}
    for t in r.get("remove_if_empty", []):
        if os.path.realpath(t) in parents:
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
    action = {"status": status, "install": install, "start": start, "uninstall": uninstall}[argv[0]]
    if action is status:
        return status(int(argv[1]))
    import fcntl  # only on the Frame; the server and its tests also import this module on Windows
    # One change at a time, or an uninstall could delete what an install is moving in.
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Fail("another mod action is still running on the Frame; try again when it finishes") from None
        return action(int(argv[1]))


if __name__ == "__main__":
    try:
        print(json.dumps(main(sys.argv[1:])))
    except Fail as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    except Exception as e:  # still one JSON object, for the server to show
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
