#!/usr/bin/env python3
"""Frame Control: a small local web UI for managing the Steam Frame from a computer.

Stdlib only; runs on macOS, Linux and Windows (differences live in frame_host.py).
Listens on 127.0.0.1 and talks to the headset through the `frame` SSH alias set
up by scripts/connect.sh or ui/frame_connect.py.

Usage: ui/server.py [--port 47810] [--exit-on-eof]   (normally started by the app)
Env:   FRAME_ALIAS (default frame)
"""
import argparse
import base64
import http.client
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# The app runs Python with -I, which leaves the script's own folder off
# sys.path, so add it for the sibling modules below.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import frame_android  # noqa: E402
import frame_catalog  # noqa: E402
import frame_host  # noqa: E402
import frame_store  # noqa: E402
import frame_titles  # noqa: E402
import frame_webinstall  # noqa: E402

frame_host.trust_bundled_cas()

HERE = Path(__file__).resolve().parent
FRAME = os.environ.get("FRAME_ALIAS", "frame")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", FRAME):
    sys.exit(f"FRAME_ALIAS must be a plain host alias, not {FRAME!r}")
# Reuse one SSH connection for the frequent status/screenshot calls, where ssh
# supports it (not on Windows: there every command connects on its own).
CONTROL = frame_host.control_path()
MUX = ["ssh", "-o", "BatchMode=yes", *(["-o", f"ControlPath={CONTROL}"] if CONTROL else [])]
# Commands use the master when it's up and connect directly when it isn't.
SSH = [*MUX, *(["-o", "ControlMaster=no"] if CONTROL else []), "-o", "ConnectTimeout=5"]

# Android helpers share the multiplexed connection when it's up.
frame_android.SSH_OPTS = SSH[1:]

APPID = re.compile(r"^\d{1,10}$")
FLATPAK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}$")
MAX_UPLOAD = 8 * 1024**3
MAX_JSON = 1024**2

# gamescope writes the PNG asynchronously; wait until its size stops changing.
SCREENSHOT = r"""
set -eu
f=$(mktemp /tmp/frame-ui-XXXXXX.png)
trap 'rm -f "$f"' EXIT
XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=gamescope-0 gamescopectl screenshot "$f" >/dev/null 2>&1
last=-1
for i in $(seq 1 50); do
  sleep 0.1
  size=$(stat -c %s "$f" 2>/dev/null || echo 0)
  if [ "$size" -gt 0 ] && [ "$size" = "$last" ]; then cat "$f"; exit 0; fi
  last=$size
done
echo "gamescope did not write a screenshot" >&2
exit 1
"""


class Failure(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


# What ssh prints when it never reached the Frame, and what to tell the user
# instead. Only ssh's own wording is matched, so a command that ran on the Frame
# and failed keeps its real error.
UNREACHABLE = [
    (re.compile(r"Could not resolve hostname"),
     "Can't find the Frame on the network. Check it's on and connected, or run Set Up Connection."),
    (re.compile(r"port \d+: (Operation timed out|Connection timed out|Host is down|No route to host|"
                r"Network is unreachable)"),
     "The Frame isn't answering. It may be asleep, switched off, or on another network."),
    (re.compile(r"[Tt]imed out talking to "),
     "The Frame took too long to answer. It may be asleep or busy; try again."),
    (re.compile(r"port \d+: Connection refused"),
     "The Frame refused the connection. Check Developer Mode is still on."),
    (re.compile(r"Permission denied \(publickey"),
     "The Frame didn't accept this computer's SSH key. Run Set Up Connection again."),
    (re.compile(r"Host key verification failed"),
     "The Frame's SSH identity changed (after a reinstall, or a different device). Run Set Up Connection again."),
    (re.compile(r"kex_exchange_identification|Connection closed by .* port \d+|Connection reset by .* port \d+"),
     "The connection to the Frame dropped. Try again."),
]


def unreachable(message):
    """The plain-language reason the Frame couldn't be reached, or None if it was."""
    for pattern, friendly in UNREACHABLE:
        if pattern.search(message):
            return friendly
    return None


def error_body(message):
    """A JSON error body and status; SSH connection failures become one clear offline message."""
    friendly = unreachable(message)
    if friendly:
        return {"error": friendly, "offline": True, "detail": message}, 503
    return {"error": message}, None


# ---- Background jobs: installs that can outlast a request ------------------
#
# Flatpak and Android installs can take many minutes. The request starts the
# work and returns a job id at once; the page polls /api/job for the outcome.

JOB_TTL = 3600
_jobs_lock = threading.Lock()
_jobs = {}  # id -> {"label", "done", "error", "message", "result", "time"}


def start_job(label, work):
    """Run work() in the background. It returns a dict with a "message"."""
    now = time.time()
    with _jobs_lock:
        for j in [j for j, v in _jobs.items() if v["done"] and now - v["time"] > JOB_TTL]:
            del _jobs[j]
        job = secrets.token_hex(8)
        _jobs[job] = {"label": label, "done": False, "error": None, "message": None, "result": None, "time": now}

    def run():
        fields = {}
        try:
            result = work()
            fields = {"message": result.get("message") or f"{label}: done", "result": result}
        except (Failure, frame_android.FrameError) as e:
            fields = {"error": unreachable(str(e)) or str(e)}
        except Exception as e:
            fields = {"error": f"{type(e).__name__}: {e}"}
        finally:
            with _jobs_lock:
                _jobs[job].update(fields, done=True, time=time.time())

    threading.Thread(target=run, daemon=True).start()
    return {"message": f"{label}…", "job": job}


def job_status(query):
    with _jobs_lock:
        job = _jobs.get((parse_qs(query).get("id") or [""])[0])
        snapshot = job and {k: v for k, v in job.items() if k != "time"}
    if not snapshot:
        raise Failure("no such job (the app may have restarted)", 404)
    return snapshot


_master_lock = threading.Lock()
_master = None


def ensure_master():
    """Start the shared SSH connection if it isn't up (one attempt at a time).

    No ConnectTimeout here: with it, OpenSSH's master takes ~5s to open its socket.
    """
    global _master
    if not CONTROL:
        return

    def up():
        try:
            return subprocess.run([*MUX, "-O", "check", FRAME], capture_output=True, stdin=subprocess.DEVNULL,
                                  timeout=5).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    with _master_lock:
        if up() or (_master and _master.poll() is None):
            return
        # Keepalives make a dead link (Frame asleep, off Wi-Fi) exit within ~10s,
        # so the next request starts a fresh master.
        _master = subprocess.Popen([*MUX, "-o", "ControlMaster=yes", "-o", "ServerAliveInterval=5",
                                    "-o", "ServerAliveCountMax=2", "-N", FRAME],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **frame_host.DETACHED)
        for _ in range(60):
            if up() or _master.poll() is not None:
                return
            time.sleep(0.05)


def ssh(remote, *, stdin=None, timeout=30, text=True):
    try:
        ensure_master()
        # Never let ssh inherit our stdin: under the app it's the pipe held open for
        # --exit-on-eof, and Windows' ssh.exe waits on it forever.
        feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        r = subprocess.run([*SSH, FRAME, remote], capture_output=True, **feed,
                           text=text, errors="replace" if text else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Failure(f"Timed out talking to {FRAME}")
    if r.returncode != 0:
        err = (r.stderr or r.stdout) if text else (r.stderr or r.stdout).decode(errors="replace")
        failure = Failure(strip_ansi(err).strip() or f"ssh exited {r.returncode}")
        failure.stdout = r.stdout if text else r.stdout.decode(errors="replace")
        raise failure
    return r.stdout


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\r", "", s)


def terminal(argv):
    """Open a terminal window running argv (for anything needing a password)."""
    try:
        return frame_host.open_terminal(argv)
    except frame_host.HostError as e:
        raise Failure(str(e), 500)


# ---- actions ---------------------------------------------------------------

def status(_body):
    return json.loads(ssh("python3 -", stdin=(HERE / "frame_status.py").read_text(), timeout=20))


def headset_view():
    """Both eyes as SteamVR composites them (see frame_vrshot.py); PNG bytes."""
    # `timeout`: VR_Init can block if SteamVR is restarting.
    out = ssh("timeout 15 python3 -", stdin=(HERE / "frame_vrshot.py").read_text(), timeout=30)
    # SteamVR prints its own notices (e.g. about vrwebhelper) on stdout too, so
    # take the last line that is our result object.
    result = {"error": out.strip() or "no output"}
    for line in out.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and ("path" in obj or "error" in obj):
            result = obj
    if "error" in result:
        raise Failure(str(result["error"]))
    path = str(result["path"])
    if not re.fullmatch(r"/tmp/frame-vrcap/shot-\d+-vr\.png", path):
        raise Failure(f"unexpected capture path {path!r}")
    # Captures show whatever was on screen, so delete even if the copy fails.
    try:
        return ssh(f"cat {path}; rc=$?; rm -f {path}; exit $rc", timeout=20, text=False)
    finally:
        try:
            ssh(f"rm -f {path}", timeout=10)
        except Failure:
            pass  # frame_vrshot.py sweeps leftovers on the next capture


# Screenshots taken in the headset with Steam's shortcut. Steam files each under the app
# it was taken in: userdata/<account>/760/remote/<appid>/screenshots/<file>,
# with a smaller copy in screenshots/thumbnails/. A shot's id is
# "<account>/<appid>/<file>", checked here before it goes near a shell.
SHOT_ROOT = ".local/share/Steam/userdata"
SHOT_ID = re.compile(r"(\d{1,12})/(\d{1,20})/(\d{14}_\d{1,4}\.(?:jpg|png))")
SHOTS_DIR = Path.home() / "Pictures" / "SteamFrame"
LIST_SHOTS = f"""cd ~/{SHOT_ROOT} 2>/dev/null || exit 0
find . -mindepth 6 -maxdepth 6 -path './*/760/remote/*/screenshots/*' -type f \\
  \\( -name '*.jpg' -o -name '*.png' \\) -printf '%P\\t%s\\t%T@\\n'"""


def shot_path(shot_id, thumb=False):
    m = SHOT_ID.fullmatch(shot_id) if isinstance(shot_id, str) else None
    if not m:
        raise Failure("bad screenshot id", 400)
    return f"{SHOT_ROOT}/{m[1]}/760/remote/{m[2]}/screenshots/{'thumbnails/' if thumb else ''}{m[3]}"


def list_shots():
    shots = []
    for line in ssh(LIST_SHOTS, timeout=20).splitlines():
        rel, _, rest = line.partition("\t")
        parts = rel.split("/")  # account/760/remote/appid/screenshots/file
        size, _, mtime = rest.partition("\t")
        shot_id = f"{parts[0]}/{parts[3]}/{parts[-1]}" if len(parts) == 6 else ""
        if not SHOT_ID.fullmatch(shot_id) or not size.isdigit():
            continue
        try:
            when = float(mtime)
        except ValueError:
            continue
        local = SHOTS_DIR / parts[-1]
        shots.append({"id": shot_id, "appid": parts[3], "file": parts[-1], "size": int(size), "time": when,
                      "saved": local.exists() and local.stat().st_size == int(size)})
    shots.sort(key=lambda s: s["time"], reverse=True)
    return {"shots": shots, "folder": str(SHOTS_DIR)}


def shot_image(query):
    q = parse_qs(query)
    shot_id = (q.get("id") or [""])[0]
    full = shot_path(shot_id)
    if q.get("thumb") == ["1"]:
        # Steam writes the thumbnail a moment after the shot; fall back to the full image.
        thumb = shot_path(shot_id, thumb=True)
        remote = f"if [ -s {thumb} ]; then cat {thumb}; else cat {full}; fi"
    else:
        remote = f"cat {full}"
    ctype = "image/png" if shot_id.endswith(".png") else "image/jpeg"
    return ssh(remote, timeout=30, text=False), ctype


def save_shots(body):
    """Copy screenshots to ~/Pictures/SteamFrame, skipping ones already there."""
    ids = body.get("ids")
    if not isinstance(ids, list) or not 0 < len(ids) <= 1000:
        raise Failure("ids must be a list of 1-1000 screenshot ids", 400)
    paths = [shot_path(i) for i in ids]
    todo = [p for p in paths if not (SHOTS_DIR / p.rsplit("/", 1)[-1]).exists()]
    if todo:
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ensure_master()
        # Copy into a hidden folder and move complete files in, so a cut-off
        # copy never looks saved. -p keeps the time the shot was taken.
        incoming = Path(tempfile.mkdtemp(prefix=".incoming-", dir=SHOTS_DIR))
        try:
            try:
                r = subprocess.run(["scp", "-p", *SSH[1:], *(f"{FRAME}:{p}" for p in todo), str(incoming)],
                                   capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                raise Failure("Copying screenshots timed out")
            if r.returncode != 0:
                raise Failure(strip_ansi(r.stderr).strip() or f"scp exited {r.returncode}")
            for f in incoming.iterdir():
                os.replace(f, SHOTS_DIR / f.name)
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
    n, skipped = len(todo), len(ids) - len(todo)
    msg = f"Saved {n} screenshot{'s' * (n != 1)} to ~/Pictures/SteamFrame"
    return {"message": msg + (f" ({skipped} already there)" if skipped else ""), "saved": n}


# Live video of the headset view. SteamVR's steamvr-v4l2cam.service copies the
# headset view (one undistorted 1920x1080 image) into the v4l2loopback device
# /dev/video99. ffmpeg encodes it with x264 (the hardware encoder crashes
# ffmpeg) and the raw H.264 comes back over SSH for the page to decode with
# WebCodecs. An access unit delimiter starts every frame so the page can split
# the stream, and repeated SPS/PPS let it start at any keyframe. ffmpeg runs in
# the background while the shell waits for our stdin to close: when the local
# ssh goes, the channel closes and the shell kills ffmpeg, even one that has
# stopped writing (and so would never get SIGPIPE).
STREAM_DEVICE = "/dev/video99"
STREAM_HEIGHTS = (720, 1080)
STREAM_FPS = (30, 60)
STREAM_STALL = 10  # seconds without video before the stream is dropped
_stream_lock = threading.Lock()
_stream_proc = None


def stream_command(query):
    q = parse_qs(query)
    try:
        height = int((q.get("h") or ["720"])[0])
        fps = int((q.get("fps") or ["30"])[0])
    except ValueError:
        raise Failure("h and fps must be integers", 400)
    if height not in STREAM_HEIGHTS or fps not in STREAM_FPS:
        raise Failure(f"h must be one of {STREAM_HEIGHTS} and fps one of {STREAM_FPS}", 400)
    rate = 3 if height == 720 else 6  # Mbit/s
    return (f"[ -e {STREAM_DEVICE} ] || {{ echo 'No headset view device ({STREAM_DEVICE}). Is SteamVR running?' >&2; exit 3; }}; "
            f"ffmpeg -hide_banner -loglevel error -nostdin -f v4l2 -video_size 1920x1080 -i {STREAM_DEVICE} "
            f"-vf fps={fps},scale=-2:{height},format=yuv420p -c:v libx264 -preset ultrafast -tune zerolatency "
            f"-g {fps * 2} -bf 0 -b:v {rate}M -maxrate {rate}M -bufsize {rate // 2 or 1}M "
            f"-x264-params aud=1:repeat-headers=1 -f h264 - & p=$!; "
            f"exec >&-; cat >/dev/null; kill $p 2>/dev/null; wait $p")


def launch(body):
    appid = str(body.get("appid", ""))
    if not APPID.match(appid):
        raise Failure("bad appid", 400)
    ssh(f"steam steam://rungameid/{appid} >/dev/null 2>&1 &")
    return {"message": f"Launching {appid}"}


def steam_frame(*args, timeout=40):
    """Run frame_steam.py on the Frame (it drives the Steam client) and return its JSON."""
    try:
        out = ssh("python3 - " + " ".join(map(shlex.quote, args)),
                  stdin=(HERE / "frame_steam.py").read_text(), timeout=timeout)
    except Failure as e:
        # frame_steam.py prints {"error": ...} on stdout when it fails, but ssh()
        # reports stderr instead if there was any, so look in both.
        for line in [*reversed(getattr(e, "stdout", "").splitlines()), *reversed(str(e).splitlines())]:
            try:
                raise Failure(json.loads(line)["error"]) from None
            except (ValueError, KeyError, TypeError):
                continue
        raise
    return json.loads(out)


def steam(body):
    """Steam games: install an owned game, or open its store page in the headset."""
    appid, action = str(body.get("appid", "")), body.get("action")
    if not APPID.match(appid):
        raise Failure("bad appid", 400)
    if action not in ("install", "store"):
        raise Failure("action must be install or store", 400)
    return steam_frame(action, appid)


def steam_search(query):
    q = parse_qs(query)
    cc = (q.get("cc") or [""])[0].upper()
    if not re.fullmatch(r"[A-Z]{2}", cc):
        raise Failure("cc must be a two-letter country code", 400)
    try:
        return {"results": frame_store.search((q.get("q") or [""])[0], cc)}
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException) as e:
        raise Failure(f"Steam store search failed: {e}")


def set_volume(body):
    # Validate everything before touching the headset.
    level = None
    if "level" in body:
        level = float(body["level"])
        if not 0 <= level <= 1:
            raise Failure("level must be 0..1", 400)
    if "muted" in body:
        ssh(f"wpctl set-mute @DEFAULT_AUDIO_SINK@ {1 if body['muted'] else 0}")
    if level is not None:
        ssh(f"wpctl set-volume @DEFAULT_AUDIO_SINK@ {level:.2f}")
    return {"message": "Volume updated"}


# Runs on the Frame, clipboard text on stdin. Verified 2026-09-25 (SteamOS 0.3.0
# vr, build 20260922): the headset desktop is a nested Plasma Wayland session
# inside gamescope with its own D-Bus bus, and wl-copy/xclip are not installed.
# Klipper (org.kde.klipper, served by plasmashell) is reachable with qdbus6, so
# borrow plasmashell's bus address. Same as scripts/paste-to-frame.sh.
PASTE = r"""set -u
text=$(cat; printf x); text=${text%x}
pid=$(pgrep -u "$(id -u)" -x plasmashell | head -n 1)
if [ -z "$pid" ]; then
  echo "plasmashell is not running: open the desktop in the headset first." >&2
  exit 2
fi
bus=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^DBUS_SESSION_BUS_ADDRESS=//p')
if DBUS_SESSION_BUS_ADDRESS=$bus qdbus6 org.kde.klipper /klipper \
     org.kde.klipper.klipper.setClipboardContents "$text" >/dev/null; then
  echo "copied via Klipper (${#text} chars)"
else
  echo "Klipper call failed (bus: ${bus:-none})" >&2
  exit 2
fi
"""
# base64 keeps the script intact through every local shell's quoting rules.
PASTE_CMD = 'bash -c "$(echo %s | base64 -d)"' % base64.b64encode(PASTE.encode()).decode()


def clipboard(body):
    if body.get("fromMac") or body.get("fromComputer"):
        try:
            text = frame_host.clipboard_text()
        except frame_host.HostError as e:
            raise Failure(str(e), 500)
        if not text:
            raise Failure("The clipboard is empty (or holds something other than text)", 400)
    else:
        text = body.get("text")
        if not isinstance(text, str) or not text:
            raise Failure("nothing to send", 400)
    return {"message": ssh(PASTE_CMD, stdin=text, timeout=30).strip()}


def flatpak(body):
    app, action = str(body.get("id", "")), body.get("action")
    if not FLATPAK_ID.match(app):
        raise Failure("bad Flatpak app ID", 400)
    if action == "install":
        def work():
            # Per-user, so it survives SteamOS updates and needs no sudo (as install-apps.sh).
            ssh("flatpak remote-add --user --if-not-exists flathub "
                "https://dl.flathub.org/repo/flathub.flatpakrepo && "
                f"flatpak install --user -y --noninteractive flathub {shlex.quote(app)}", timeout=1800)
            return {"message": f"Installed {app}"}
        return start_job(f"Install {app}", work)
    if action == "uninstall":
        out = ssh(f"flatpak uninstall --user -y -- {shlex.quote(app)}", timeout=300)
        return {"message": strip_ansi(out).strip() or f"Removed {app}"}
    raise Failure("action must be install or uninstall", 400)


def open_thing(body):
    what = body.get("what")
    try:
        if what == "terminal":
            return {"message": f"Opened an SSH session in {terminal(['ssh', FRAME])}"}
        if what in ("reboot", "poweroff", "suspend"):
            # logind answers "challenge" over SSH, so sudo (and the password) is needed.
            where = terminal(["ssh", "-t", FRAME, "sudo", "systemctl", what])
            return {"message": f"Confirm with the Developer Mode password in {where} to {what}"}
        if what == "steamlink":
            return {"message": frame_host.open_steam_link()}
        if what == "rdp":
            return {"message": frame_host.open_rdp(FRAME)}
        if what == "sftp":
            return {"message": f"Opened an SFTP session in {terminal(['sftp', FRAME])}"}
        if what == "shots":
            SHOTS_DIR.mkdir(parents=True, exist_ok=True)
            frame_host.open_path(SHOTS_DIR)
            return {"message": f"Opened {SHOTS_DIR} in {frame_host.FILE_MANAGER}"}
    except frame_host.HostError as e:
        raise Failure(str(e), 500)
    raise Failure("unknown target", 400)


def android(body):
    """Android apps, each in its own persistent Lepton instance (frame_android.py)."""
    action, pkg = body.get("action"), str(body.get("package", ""))
    ensure_master()
    try:
        if action == "install":
            frame_catalog.app(pkg)  # an unknown package fails now, not in the background

            def work():
                m = frame_catalog.install(pkg)
                return {"message": f"Installed {m['label']}. It's in the Steam library; launching it opens its own panel.",
                        "app": m}
            return start_job(f"Install {pkg}", work)
        if action in ("launch", "stop"):
            m = getattr(frame_android, action)(pkg)
            return {"message": f"{'Launching' if action == 'launch' else 'Stopped'} {m['label']}"}
        if action == "remove":
            m = frame_android.remove(pkg, keep_data=bool(body.get("keepData")))
            return {"message": f"Removed {m['label']}"}
        if action == "probe":
            r = frame_catalog.probe_and_report(pkg)
            word = {"runs": "runs", "crashes": "crashed", "instance_failed": "didn't start"}.get(r["result"], r["result"])
            return {"message": f"{pkg} {word}" + (f": {r['detail']}" if r.get("detail") else ""), "probe": r}
        if action == "report":
            # Any APK, not only catalogue or installed ones: package, did it work, how it was run.
            r = frame_catalog.add_report(pkg, body.get("version"), rating=body.get("rating"),
                                         notes=str(body.get("notes") or ""),
                                         runtime=body.get("runtime") or "instance",
                                         label=body.get("label"), source=body.get("source"))
            name = r.get("label") or pkg
            where = "" if frame_catalog.compat_db.shared() else " on this computer"
            return {"message": f"Saved your report for {name}{where}", "report": r}
    except frame_android.FrameError as e:
        raise Failure(str(e))
    raise Failure("unknown action", 400)


# ---- Sideloaded titles (Linux/Windows builds as Steam Devkit Games) --------
#
# Installing is two steps: inspect (a dropped file is uploaded and a zip
# unpacked here, once) returns a token and the detected target and runtime for
# the page to confirm; install then runs in the background with progress the
# page polls. Unconfirmed uploads are dropped after STAGE_TTL.

STAGE_TTL = 3600
_titles_lock = threading.Lock()
_staged = {}  # token -> {"plan", "dir" (an upload's temp dir or None), "time"}
_title_jobs = {}  # token -> {"stage", "fraction", "done", "error", "title", "time"}


def _drop_staged(entry):
    frame_titles.discard(entry["plan"])
    if entry.get("dir"):
        shutil.rmtree(entry["dir"], ignore_errors=True)


def _purge_titles(now=None):
    now = now or time.time()
    with _titles_lock:
        stale = [_staged.pop(t) for t in [t for t, e in _staged.items() if now - e["time"] > STAGE_TTL]]
        for t in [t for t, j in _title_jobs.items() if j["done"] and now - j["time"] > STAGE_TTL]:
            del _title_jobs[t]
    for e in stale:
        _drop_staged(e)


def stage_title(path, temp_dir=None, name=None):
    """Inspect a .zip, folder or program and keep it for install; returns the plan and a token."""
    _purge_titles()
    try:
        plan = frame_titles.inspect(path, name)
    except BaseException as e:
        # Whatever went wrong, nothing will ever claim this upload.
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if isinstance(e, frame_android.FrameError):
            raise Failure(str(e), 400)
        raise
    token = secrets.token_hex(12)
    with _titles_lock:
        _staged[token] = {"plan": plan, "dir": temp_dir, "time": time.time()}
    return {"message": f"Read {plan['source']}: {plan['target']} with {plan['runtime_label']}",
            "token": token, "plan": frame_titles.public(plan)}


def _run_title_install(token, entry, name, exe, runtime):
    def update(**fields):  # the page reads jobs from other threads; change them under the lock
        with _titles_lock:
            _title_jobs[token].update(fields)

    try:
        m = frame_titles.install_plan(entry["plan"], name=name, exe=exe, runtime=runtime,
                                      progress=lambda stage, fraction: update(stage=stage, fraction=fraction))
        update(title=m, message=f"Installed {m['id']} in the Steam library ({m['runtime_label']})")
    except frame_android.FrameError as e:
        update(error=str(e))
    except Exception as e:
        update(error=f"{type(e).__name__}: {e}")
    finally:
        _drop_staged(entry)
        update(done=True, time=time.time())


def titles(body):
    """Sideloaded titles (frame_titles.py): inspect a local path, install, discard, launch, remove."""
    action = body.get("action")
    if action == "inspect":
        # The app's page passes a dropped folder's path (Electron knows it); browsers upload instead.
        path = str(body.get("path") or "")
        if not os.path.isabs(path) or not os.path.exists(path):
            raise Failure("inspect needs the absolute path of a .zip, folder or program", 400)
        return stage_title(path, name=body.get("name") or None)
    if action in ("install", "discard"):
        token = str(body.get("token") or "")
        with _titles_lock:
            entry = _staged.pop(token, None)
        if not entry:
            raise Failure("that upload has expired; drop the file again", 400)
        if action == "discard":
            _drop_staged(entry)
            return {"message": "Discarded"}
        with _titles_lock:
            _title_jobs[token] = {"stage": "Starting", "fraction": 0, "done": False, "error": None,
                                  "message": None, "title": None, "time": time.time()}
        ensure_master()
        opt = lambda k: str(body.get(k) or "") or None  # noqa: E731
        threading.Thread(target=_run_title_install, daemon=True,
                         args=(token, entry, opt("name"), opt("exe"), opt("runtime"))).start()
        return {"message": f"Installing {entry['plan']['source']}", "job": token}
    if action not in ("launch", "remove"):
        raise Failure("unknown action", 400)
    gid = str(body.get("id", ""))
    if not frame_titles.ID_RE.match(gid):
        raise Failure("bad title id", 400)
    ensure_master()
    try:
        m = getattr(frame_titles, action)(gid)
    except frame_android.FrameError as e:
        raise Failure(str(e))
    return {"message": f"{'Launching' if action == 'launch' else 'Removed'} {m['id']}"}


def title_job(query):
    with _titles_lock:
        job = _title_jobs.get((parse_qs(query).get("token") or [""])[0])
        snapshot = job and {k: v for k, v in job.items() if k != "time"}
    if not snapshot:
        raise Failure("no such install", 404)
    return snapshot


# ---- Android display (wm size / wm density / font_scale over ADB) -----------
#
# Each running Lepton instance listens for ADB on the Frame (5555 is Lepton
# Development; own-instance apps get the next free port). ADB goes through a
# dedicated SSH forward that lives only for the request, like
# scripts/install-apk.sh, and is always torn down with an adb disconnect.

ADB_PORTS = range(5555, 5600)
SIZE_RE = re.compile(r"^(\d{3,4})x(\d{3,4})$")
DENSITY_RANGE = (120, 640)
WIDTH_RANGE, HEIGHT_RANGE = (640, 3840), (360, 2160)
FONT_RANGE = (0.5, 2.0)
KNOWN_LABELS = {"com.t3tools.t3code": "T3 Code", "org.fdroid.fdroid": "F-Droid"}
# One ADB session at a time: requests are rare, and it keeps adb's state simple.
_adb_lock = threading.Lock()
_live_tunnels = set()  # ssh processes (ADB forwards, live video) to kill if the server stops mid-request


def adb_path():
    try:
        return frame_host.adb()
    except frame_host.HostError as e:
        raise Failure(str(e), 500)


def adb(adb_bin, *args, timeout=20):
    try:
        r = subprocess.run([adb_bin, *args], capture_output=True, stdin=subprocess.DEVNULL, text=True,
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Failure(f"adb {' '.join(args[-2:])} timed out")
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise Failure(out or f"adb exited {r.returncode}")
    return out


def free_local_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AdbTunnel:
    """SSH forwards from local loopback to Frame ADB ports, plus adb connections.

    `with AdbTunnel([5555, 5557]) as t: t.shell(5555, "wm size")`. On exit it
    disconnects adb and kills the ssh process, whatever happened inside.
    """

    def __init__(self, ports):
        self.remote = list(ports)
        self.local = {}
        self.proc = None
        self.adb = adb_path()

    def __enter__(self):
        if not _adb_lock.acquire(timeout=60):
            raise Failure("another Android display request is still running; try again", 503)
        try:
            self._open()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _open(self):
        try:
            self._forward()
        except Failure:
            # A local port picked by free_local_port() can be taken before ssh
            # binds it (ExitOnForwardFailure turns that into an error): retry once.
            self._stop_ssh()
            self._forward()
        self.failed = {}
        for p in self.remote:
            out = adb(self.adb, "connect", self.serial(p), timeout=15)
            # adb connect exits 0 even when it fails; check what it says. Keep
            # going so one stuck port doesn't hide the healthy instances.
            if "connected to" not in out:
                self.failed[p] = f"adb couldn't connect to Frame port {p}: {out}"

    def _forward(self):
        self.local = {p: free_local_port() for p in self.remote}
        fwd = [a for p, lp in self.local.items() for a in ("-L", f"127.0.0.1:{lp}:127.0.0.1:{p}")]
        # Its own connection (ControlPath=none), so killing it drops the forwards.
        self.proc = _proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ControlPath=none",
             "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=5", "-N", *fwd, FRAME],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _live_tunnels.add(_proc)
        deadline = time.time() + 12
        pending = set(self.local.values())
        while pending:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode(errors="replace").strip()
                raise Failure(f"ADB tunnel failed: {err or 'ssh exited ' + str(self.proc.returncode)}")
            if time.time() > deadline:
                raise Failure("ADB tunnel didn't come up within 12s")
            for lp in list(pending):
                try:
                    socket.create_connection(("127.0.0.1", lp), timeout=0.5).close()
                    pending.discard(lp)
                except OSError:
                    pass
            if pending:
                time.sleep(0.1)

    def serial(self, port):
        return f"127.0.0.1:{self.local[port]}"

    def shell(self, port, command, timeout=20):
        if port in getattr(self, "failed", {}):
            raise Failure(self.failed[port])
        return adb(self.adb, "-s", self.serial(port), "shell", command, timeout=timeout)

    def _stop_ssh(self):
        proc, self.proc = self.proc, None
        if not proc:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
        except OSError:
            pass
        finally:
            _live_tunnels.discard(proc)
            if proc.stderr:
                proc.stderr.close()

    def __exit__(self, *exc):
        try:
            # Tunnel first: it's what could outlive us. `adb disconnect` only
            # talks to the local adb server, so it works without the tunnel.
            self._stop_ssh()
            for p in self.local:
                try:
                    subprocess.run([self.adb, "disconnect", self.serial(p)], capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        finally:
            _adb_lock.release()
        return False


DISPLAY_READ = "echo @@pkgs; pm list packages -3; echo @@size; wm size; echo @@density; wm density; " \
               "echo @@font; settings get system font_scale"


def parse_display(out):
    sec, parts = None, {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("@@"):
            sec = line[2:]
            parts[sec] = []
        elif sec and line:
            parts[sec].append(line)

    def pick(lines, key):
        for line in lines:
            if line.lower().startswith(key) and ":" in line:
                return line.split(":", 1)[1].strip()
        return None

    size, density = parts.get("size", []), parts.get("density", [])
    font = (parts.get("font") or ["null"])[0]
    try:
        font_scale = None if font == "null" else float(font)
    except ValueError:
        font_scale = None
    phys_d, over_d = pick(density, "physical density"), pick(density, "override density")
    return {
        "packages": sorted(l.split(":", 1)[1] for l in parts.get("pkgs", []) if l.startswith("package:")),
        "physicalSize": pick(size, "physical size"),
        "overrideSize": pick(size, "override size"),
        "physicalDensity": int(phys_d) if phys_d and phys_d.isdigit() else None,
        "overrideDensity": int(over_d) if over_d and over_d.isdigit() else None,
        # null means never set, which Android treats as 1.0.
        "fontScale": font_scale,
    }


def lepton_ports():
    """Frame ADB ports (5555-5599) that are listening now, with container names and app labels."""
    out = ssh("ss -ltnH; echo @@podman; podman ps --format '{{.Names}} {{.Labels.adb_port}}' 2>/dev/null; "
              f"echo @@meta; for f in {frame_android.APPS_DIR}/*/meta.json; do [ -f \"$f\" ] && cat \"$f\" && echo @@m; done; true",
              timeout=20)
    listen, _, rest = out.partition("@@podman")
    podman, _, meta = rest.partition("@@meta")
    ports = set()
    for line in listen.splitlines():
        cols = line.split()
        if len(cols) >= 4:
            port = cols[3].rsplit(":", 1)[-1]
            if port.isdigit() and int(port) in ADB_PORTS:
                ports.add(int(port))
    containers = {}
    for line in podman.splitlines():
        cols = line.split()
        if len(cols) == 2 and cols[1].isdigit():
            containers[int(cols[1])] = cols[0]
    labels = dict(KNOWN_LABELS)
    for chunk in meta.split("@@m"):
        try:
            m = json.loads(chunk)
            labels[str(m["package"])] = str(m["label"])
        except (ValueError, KeyError, TypeError):
            pass
    return sorted(ports), containers, labels


def android_displays():
    ports, containers, labels = lepton_ports()
    if not ports:
        return {"instances": []}
    instances = []
    with AdbTunnel(ports) as t:
        for p in ports:
            item = {"port": p, "container": containers.get(p)}
            try:
                item.update(parse_display(t.shell(p, DISPLAY_READ)))
            except Failure as e:
                item["error"] = str(e)
            item["labels"] = {pkg: labels[pkg] for pkg in item.get("packages", []) if pkg in labels}
            instances.append(item)
    return {"instances": instances}


def android_display(body):
    port = body.get("port")
    if type(port) is not int or port not in ADB_PORTS:
        raise Failure(f"port must be an integer {ADB_PORTS.start}-{ADB_PORTS.stop - 1}", 400)
    cmds = []

    size = body.get("size")
    if size is not None:
        if size == "reset":
            cmds.append("wm size reset")
        else:
            m = SIZE_RE.fullmatch(size) if isinstance(size, str) else None
            if not m:
                raise Failure("size must be WIDTHxHEIGHT (e.g. 2560x1440) or \"reset\"", 400)
            w, h = int(m[1]), int(m[2])
            if not (WIDTH_RANGE[0] <= w <= WIDTH_RANGE[1] and HEIGHT_RANGE[0] <= h <= HEIGHT_RANGE[1]):
                raise Failure(f"size must be {WIDTH_RANGE[0]}-{WIDTH_RANGE[1]} wide and "
                              f"{HEIGHT_RANGE[0]}-{HEIGHT_RANGE[1]} high", 400)
            cmds.append(f"wm size {w}x{h}")

    density = body.get("density")
    if density is not None:
        if density == "reset":
            cmds.append("wm density reset")
        elif type(density) is int and DENSITY_RANGE[0] <= density <= DENSITY_RANGE[1]:
            cmds.append(f"wm density {density}")
        else:
            raise Failure(f"density must be an integer {DENSITY_RANGE[0]}-{DENSITY_RANGE[1]} or \"reset\"", 400)

    font = body.get("fontScale")
    if font is not None:
        if font == "reset":
            # Applying a config change (e.g. the wm resets just before) writes
            # font_scale=1.0 back asynchronously, so delete again once it settles.
            cmds.append("settings delete system font_scale; sleep 1; settings delete system font_scale")
        elif type(font) in (int, float) and FONT_RANGE[0] <= font <= FONT_RANGE[1]:
            cmds.append(f"settings put system font_scale {round(float(font), 3):g}")
        else:
            raise Failure(f"fontScale must be a number {FONT_RANGE[0]}-{FONT_RANGE[1]} or \"reset\"", 400)

    if not cmds:
        raise Failure("nothing to change: give density, size or fontScale", 400)
    ports, _, _ = lepton_ports()
    if port not in ports:
        raise Failure(f"no Lepton instance is listening on Frame port {port}", 404)
    with AdbTunnel([port]) as t:
        for c in cmds:
            out = t.shell(port, c)
            # wm prints usage or an exception on failure but may still exit 0.
            if re.search(r"exception|error|usage", out, re.I):
                raise Failure(f"{c}: {out}")
        now = parse_display(t.shell(port, DISPLAY_READ))
    now["port"] = port
    return {"message": f"Port {port}: " + "; ".join(c.split(";")[0] for c in cmds), "display": now}


# ---- install links from websites (frame-control://install, docs/web-install.md) ----
# The app hands the link to the page, which asks /check (fetches the manifest,
# downloads nothing), shows what it found and waits for the user's click before
# /start. A website can't call these itself: like all of /api/* they need the
# Host and X-Frame-UI checks in Handler.local_request.
_web_lock = threading.Lock()
_web_plans = {}   # id -> checked plan waiting for the user to confirm
_web_jobs = {}    # id -> progress of the confirmed install (only the latest is kept)
_web_workers = set()  # threads running an install, joined on shutdown
_web_closing = False  # set on shutdown; no new installs after that
MAX_WEB_PLANS = 8
WEB_TMP_PREFIX = "frame-webinstall-"  # then the server's PID, for sweep_tmp


def webinstall_check(body):
    manifest, url = body.get("manifest"), body.get("url")
    for v in (manifest, url):
        if v is not None and not isinstance(v, str):
            raise Failure("manifest and url must be strings", 400)
    try:
        plan = frame_webinstall.plan(manifest=manifest, url=url)
    except frame_webinstall.WebInstallError as e:
        raise Failure(str(e), 400)
    pid = secrets.token_urlsafe(16)
    with _web_lock:
        while len(_web_plans) >= MAX_WEB_PLANS:
            _web_plans.pop(next(iter(_web_plans)))
        _web_plans[pid] = plan
    shown = ("name", "file", "kind", "kindLabel", "host", "linkHost", "size", "source")
    return {"id": pid, **{k: plan[k] for k in shown}, "sha256": bool(plan["sha256"])}


def webinstall_start(body):
    pid = body.get("id")
    with _web_lock:
        if any(j["phase"] in ("download", "install") for j in _web_jobs.values()):
            raise Failure("another install from a link is still running", 409)
        # One use per check: the page can only install what it showed.
        plan = _web_plans.pop(pid, None) if isinstance(pid, str) else None
        if not plan:
            raise Failure("unknown or already used install id; open the link again", 400)
        job = {"phase": "download", "done": 0, "total": plan["size"], "detail": "", "message": None,
               "error": None, "cancel": False}
        if _web_closing:
            raise Failure("Frame Control is quitting", 503)
        _web_jobs.clear()
        _web_jobs[pid] = job
        # Started under the lock, so shutdown never sees a thread it can't join.
        worker = threading.Thread(target=_webinstall_run, args=(plan, job), daemon=True)
        _web_workers.add(worker)
        worker.start()
    return {"job": pid}


def _webinstall_run(plan, job):
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix=f"{WEB_TMP_PREFIX}{os.getpid()}-")

        def progress(done, total):
            job["done"], job["total"] = done, total

        def detail(*args, **_kw):  # frame_titles may report its steps as text
            texts = [a for a in args if isinstance(a, str)]
            if texts:
                job["detail"] = texts[0][:200]

        def connected(conn):
            with _web_lock:
                job["_conn"] = conn
                stop = job["cancel"]  # cancelled before this connection existed
            if stop:
                frame_webinstall.abort(conn)

        path = frame_webinstall.download(plan, tmp, progress=progress, cancelled=lambda: job["cancel"],
                                         connected=connected)
        # Under the lock cancel uses, so a cancel it acknowledged is never followed by an install.
        with _web_lock:
            if job["cancel"]:
                raise frame_webinstall.Cancelled("download cancelled")
            job["phase"] = "install"
            job.pop("_conn", None)
        ensure_master()
        res = frame_webinstall.dispatch(path, name=plan["name"], exe=plan["exe"], progress=detail, source=plan["url"])
        job["message"], job["phase"] = res["message"], "done"
    except Exception as e:
        known = (frame_webinstall.WebInstallError, Failure, frame_android.FrameError)
        job["error"] = str(e) if isinstance(e, known) else f"{type(e).__name__}: {e}"
        job["phase"] = "error"
    finally:
        with _web_lock:
            job.pop("_conn", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        with _web_lock:
            _web_workers.discard(threading.current_thread())


def webinstall_job(query):
    job = _web_jobs.get((parse_qs(query).get("id") or [""])[0])
    if not job:
        raise Failure("unknown install job", 404)
    with _web_lock:  # the worker adds and drops _conn meanwhile
        return {k: v for k, v in job.items() if k != "cancel" and not k.startswith("_")}


def webinstall_cancel(body):
    jid = body.get("job")
    job = _web_jobs.get(jid) if isinstance(jid, str) else None
    if not job:
        raise Failure("unknown install job", 404)
    with _web_lock:
        if job["phase"] != "download":
            raise Failure("only the download can be cancelled", 409)
        job["cancel"] = True
        conn = job.get("_conn")
    if conn:
        frame_webinstall.abort(conn)
    return {"message": "Cancelling the download"}


def webinstall_shutdown():
    """Stop downloads and give workers a moment to delete their temporary files.

    An install already copying to the Frame may outlive this; sweep_tmp
    removes what it leaves on a later start.
    """
    global _web_closing
    with _web_lock:
        _web_closing = True
        conns = []
        for job in _web_jobs.values():
            job["cancel"] = True
            conns.append(job.get("_conn"))  # once: the worker may drop it any time
        workers = list(_web_workers)
    for conn in conns:
        if conn:
            frame_webinstall.abort(conn)
    deadline = time.time() + 4  # the app kills the server 5 s after asking it to stop
    for worker in workers:
        worker.join(max(0, deadline - time.time()))


def _pid_alive(pid):
    if frame_host.WINDOWS:
        # os.kill(pid, 0) would terminate the process there; ask the kernel instead.
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            return not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by someone else
    return True


def sweep_tmp():
    """Delete download and title staging folders left by a server killed mid-install.

    Folders carry the server's PID, so only a dead server's are taken.
    """
    for prefix in (WEB_TMP_PREFIX, frame_titles.TMP_PREFIX):
        for d in Path(tempfile.gettempdir()).glob(f"{prefix}*"):
            _sweep_one(prefix, d)


def _sweep_one(prefix, d):
    m = re.fullmatch(re.escape(prefix) + r"(\d+)-.*", d.name)
    if not m:
        return
    pid = int(m[1])
    try:
        if pid != os.getpid() and not _pid_alive(pid) and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


POST = {"/api/android/display": android_display, "/api/android": android, "/api/titles": titles, "/api/launch": launch, "/api/steam": steam, "/api/volume": set_volume, "/api/clipboard": clipboard,
        "/api/flatpak": flatpak, "/api/open": open_thing, "/api/shots/save": save_shots,
        "/api/webinstall/check": webinstall_check, "/api/webinstall/start": webinstall_start,
        "/api/webinstall/cancel": webinstall_cancel}


# ---- HTTP ------------------------------------------------------------------

def _pipe_reader(pipe):
    """Chunks from a pipe via a thread; select() can't wait on pipes on Windows."""
    chunks = queue.Queue()  # unbounded: the pump never blocks, so it ends at EOF

    def pump():
        try:
            while True:
                chunk = pipe.read1(1 << 16) if hasattr(pipe, "read1") else os.read(pipe.fileno(), 1 << 16)
                chunks.put(chunk)
                if not chunk:
                    return
        except (OSError, ValueError):
            chunks.put(b"")

    threading.Thread(target=pump, daemon=True).start()
    return chunks


def _next_chunk(chunks, timeout):
    """The next chunk, or b"" at end of stream or after `timeout` seconds of silence."""
    try:
        return chunks.get(timeout=timeout)
    except queue.Empty:
        return b""


def push_file(path, dest="Downloads/"):
    """Copy a file to the Frame (as scripts/push.sh): rsync where both ends have it, else scp."""
    name = Path(path).name
    try:
        # Not on Windows: a Windows rsync (cwRsync, MSYS2) wouldn't take our POSIX -e quoting.
        if not frame_host.WINDOWS and shutil.which("rsync") and ssh("command -v rsync >/dev/null && echo yes || true").strip() == "yes":
            cmd = ["rsync", "-a", "-e", shlex.join(SSH), str(path), f"{FRAME}:{shlex.quote(dest)}"]
        else:
            # Modern scp uses SFTP, so the remote path isn't parsed by a shell.
            cmd = ["scp", *SSH[1:], "-r", str(path), f"{FRAME}:{dest}"]
        r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, text=True, errors="replace", timeout=3600)
    except subprocess.TimeoutExpired:
        raise Failure(f"Copying {name} timed out")
    if r.returncode != 0:
        raise Failure(strip_ansi(r.stderr or r.stdout).strip() or f"copy exited {r.returncode}")
    return f"Sent {name} to ~/{dest}"


class Handler(BaseHTTPRequestHandler):
    server_version = "FrameControl/1"
    timeout = 60  # per socket operation, so a stalled client can't hold a thread

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, fmt % args))

    def local_request(self):
        # Blocks DNS rebinding (Host) and cross-site form posts (custom header
        # forces a CORS preflight, which this server never approves).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send_json({"error": "forbidden host"}, 403)
            return False
        # All of /api/*, not just POST: an <img> on any website could otherwise
        # trigger a headset capture and display it.
        api = urlparse(self.path).path.startswith("/api/")
        if (self.command == "POST" or api) and self.headers.get("X-Frame-UI") != "1":
            self.send_json({"error": "missing X-Frame-UI header"}, 403)
            return False
        return True

    def send_bytes(self, data, ctype, status=200, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # Nobody may frame the UI (clickjacking).
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json", status)

    def send_error_json(self, message, status):
        body, offline_status = error_body(message)
        self.send_json(body, offline_status or status)

    def do_GET(self):
        if not self.local_request():
            return
        url = urlparse(self.path)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                self.send_bytes((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/host":
                self.send_json({"os": frame_host.NAME, "fileManager": frame_host.FILE_MANAGER,
                                "computer": "Mac" if frame_host.MAC else "PC"})
            elif path == "/api/android":
                ensure_master()
                self.send_json({"apps": frame_android.list_apps()})
            elif path == "/api/titles":
                ensure_master()
                self.send_json({"titles": frame_titles.list_titles()})
            elif path == "/api/titles/job":
                self.send_json(title_job(url.query))
            elif path == "/api/job":
                self.send_json(job_status(url.query))
            elif path == "/api/android/displays":
                self.send_json(android_displays())
            elif path == "/api/android/reports":
                self.send_json({"reports": frame_catalog.recent_reports(),
                                "shared": frame_catalog.compat_db.shared()})
            elif path == "/api/android/catalog":
                self.send_json({"apps": frame_catalog.catalog()})
            elif path == "/api/status":
                self.send_json(status({}))
            elif path == "/api/steam/owned":
                self.send_json(steam_frame("owned"))
            elif path == "/api/steam/search":
                self.send_json(steam_search(url.query))
            elif path == "/api/webinstall/job":
                self.send_json(webinstall_job(url.query))
            elif path == "/api/shots":
                self.send_json(list_shots())
            elif path == "/api/shots/image":
                self.send_bytes(*shot_image(url.query))
            elif path == "/api/stream":
                self.stream_video(url.query)
            elif path == "/api/screenshot" and parse_qs(url.query).get("view") == ["headset"]:
                self.send_bytes(headset_view(), "image/png", headers=[("X-Capture-Source", "steamvr")])
            elif path == "/api/screenshot":
                self.send_bytes(ssh(SCREENSHOT, timeout=20, text=False), "image/png",
                                headers=[("X-Capture-Source", "gamescope")])
            else:
                self.send_json({"error": "not found"}, 404)
        except Failure as e:
            self.send_error_json(str(e), e.status)
        except frame_android.FrameError as e:
            self.send_error_json(str(e), 502)
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if not self.local_request():
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/upload":
                self.send_json(self.upload())
                return
            handler = POST.get(path)
            if not handler:
                self.send_json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_JSON:
                raise Failure("request body too large", 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise Failure("request body must be a JSON object", 400)
            self.send_json(handler(body))
        except Failure as e:
            self.send_error_json(str(e), e.status)
        except (ValueError, TypeError) as e:
            self.send_json({"error": f"bad request: {e}"}, 400)
        except frame_android.FrameError as e:
            self.send_error_json(str(e), 502)
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def stream_video(self, query):
        """Raw H.264 of the headset view until the page disconnects (see stream_command)."""
        global _stream_proc
        remote = stream_command(query)
        ensure_master()
        # stderr goes to a file: nothing reads it while streaming, and a full
        # pipe would stall ffmpeg. It's only read if the stream fails to start.
        errors = tempfile.TemporaryFile()
        proc = subprocess.Popen([*SSH, FRAME, remote], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=errors)
        try:
            _live_tunnels.add(proc)
            # One viewer at a time: a new stream (another tab, a reload) ends the last one.
            with _stream_lock:
                old, _stream_proc = _stream_proc, proc
            if old and old.poll() is None:
                old.terminate()
            chunks = _pipe_reader(proc.stdout)
            # Nothing is sent until the first bytes arrive, so a failure to
            # start still comes back as a JSON error.
            first = _next_chunk(chunks, 20)
            if not first:
                proc.kill()
                proc.wait()
                errors.seek(0)
                err = strip_ansi(errors.read().decode(errors="replace")).strip()
                raise Failure(err or "The headset view sent no video for 20 s")
            chunk = first
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/h264")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
                self.end_headers()
                self.close_connection = True  # the body ends when the connection does
                while chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    # A stalled headset view ends the stream rather than
                    # holding this thread (and the page) forever.
                    chunk = _next_chunk(chunks, STREAM_STALL)
            except OSError:
                pass  # the page stopped watching (or stopped reading); the body has started, so no JSON
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            for f in (proc.stdin, proc.stdout, errors):
                f.close()
            _live_tunnels.discard(proc)

    def upload(self):
        """Raw file body. X-Filename names it; X-Mode is 'push', 'apk' (install), 'apkinfo' (read only)
        or 'title' (a .zip or program to sideload: inspected and kept for /api/titles install)."""
        name = os.path.basename(unquote(self.headers.get("X-Filename", "")))
        mode = self.headers.get("X-Mode", "push")
        length = int(self.headers.get("Content-Length") or 0)
        if not name or name.startswith("."):
            raise Failure("missing filename", 400)
        if length <= 0 or length > MAX_UPLOAD:
            raise Failure("empty or too-large upload", 400)
        if mode in ("apk", "apkinfo") and not name.lower().endswith(".apk"):
            raise Failure("APK install needs a .apk file", 400)
        tmp = Path(tempfile.mkdtemp(prefix="frame-ui-"))
        keep = False
        try:
            dest = tmp / name
            with open(dest, "wb") as f:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise Failure("upload interrupted", 400)
                    f.write(chunk)
                    remaining -= len(chunk)
            if mode == "apkinfo":
                # Read an APK for a report without installing it.
                try:
                    info = frame_android.apk_info(str(dest))
                except frame_android.FrameError as e:
                    raise Failure(str(e), 400)
                info.pop("icon_png", None)
                try:
                    frame_android.check_installable(info)
                    info["blocker"] = None
                except frame_android.FrameError as e:
                    info["blocker"] = str(e)
                return {"message": f"Read {info['label']} {info['version']}", "apk": info}
            if mode == "title":
                keep = True  # stage_title owns tmp now, and removes it on failure
                return stage_title(str(dest), temp_dir=str(tmp))
            if mode == "apk":
                ensure_master()
                try:
                    m = frame_android.install(str(dest), source=name)
                except frame_android.FrameError as e:
                    raise Failure(str(e), 400)
                return {"message": f"Installed {m['label']} as its own app in the Steam library", "app": m}
            return {"message": push_file(dest)}
        finally:
            if not keep:
                shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 47810)))
    ap.add_argument("--exit-on-eof", action="store_true",
                    help="stop cleanly when stdin closes (the app closes it on quit; "
                         "Windows has no SIGTERM to catch)")
    args = ap.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    sweep_tmp()
    if not frame_host.WINDOWS:
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    if args.exit_on_eof:
        def watch_stdin():
            sys.stdin.buffer.read()
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        threading.Thread(target=watch_stdin, daemon=True).start()
    print(f"Frame Control on http://127.0.0.1:{args.port}  (alias: {FRAME}; Ctrl-C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # The app both closes stdin and sends SIGTERM on quit; a second signal
        # mid-cleanup would abort it and leave the SSH master running.
        if not frame_host.WINDOWS:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        webinstall_shutdown()
        # The master was started with -N, so it stays up until told to exit.
        if CONTROL:
            subprocess.run([*MUX, "-O", "exit", FRAME], capture_output=True, stdin=subprocess.DEVNULL)
        if _master and _master.poll() is None:
            _master.terminate()
        for proc in list(_live_tunnels):  # ADB forwards and video streams cut off mid-way
            if proc.poll() is None:
                proc.terminate()
        _purge_titles(now=float("inf"))  # unconfirmed title uploads


if __name__ == "__main__":
    main()
