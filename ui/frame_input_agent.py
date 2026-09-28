"""Keyboard and pointer for the Steam Frame. Frame Control's server runs this ON the Frame.

It speaks KDE Connect's LAN protocol (version 7, as in KDE Connect 24.02) to the
Frame's own kdeconnectd, as a phone would, and forwards remote-input events read
from stdin: one JSON object (or list of them) per line, each a KDE Connect
"mousepad" request body such as {"dx": 4, "dy": -2} or {"key": "hello"}.
KDE Connect does the typing and clicking.

KDE Connect isn't installed on the Frame, but Valve's package repository for it
has a build. The first run fetches that and the few libraries the Frame lacks
into ~/.local/share/frame-control/kdeconnect: no root, and SteamOS updates
leave it alone.

Status goes to stdout, one JSON object per line:
{"state": "installing" | "starting" | "pairing" | "ready" | "error", ...}.

Standard library only: this runs on the Frame's own Python.
"""
import fcntl
import json
import os
import selectors
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

BASE = Path.home() / ".local/share/frame-control/kdeconnect"
ROOT = BASE / "root"
BRIDGE = BASE / "bridge"
# kdeconnect plus the dependencies the Frame's image doesn't have (checked 2026-09-28,
# SteamOS 0.4.1); `pacman -Sp` adds any others still missing.
PACKAGES = ["kdeconnect", "kpeople", "libfakekey", "modemmanager-qt", "pulseaudio-qt"]
PORT = int(os.environ.get("FRAME_INPUT_PORT", "1716"))
UID = os.getuid()
MOUSEPAD = "kdeconnect.mousepad.request"


def say(state, **more):
    print(json.dumps({"state": state, **more}), flush=True)


def packet(kind, body):
    return (json.dumps({"id": int(time.time() * 1000), "type": kind, "body": body}) + "\n").encode()


# ---- KDE Connect on the Frame ------------------------------------------------

def daemon_path():
    for path in (Path("/usr/lib/kdeconnectd"), ROOT / "usr/lib/kdeconnectd"):
        if path.exists():
            return path
    return None


def install():
    say("installing", message="Fetching KDE Connect from the Frame's package repository")
    found = subprocess.run(["pacman", "-Sp", *PACKAGES], capture_output=True, text=True, timeout=120)
    urls = [u for u in found.stdout.split() if u.startswith("https://")]
    if found.returncode or not urls:
        raise RuntimeError("Couldn't find KDE Connect in the Frame's package repository: "
                           + (found.stderr.strip() or "no packages listed"))
    download, stage = BASE / "download", BASE / "root.new"
    for d in (download, stage):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    for url in urls:
        name = download / url.rsplit("/", 1)[1]
        subprocess.run(["curl", "-fsSL", "--retry", "2", "-o", str(name), url], check=True, timeout=600)
        if subprocess.run(["tar", "--zstd", "-xf", str(name), "-C", str(stage)], capture_output=True).returncode:
            subprocess.run(["bsdtar", "-xf", str(name), "-C", str(stage)], check=True, capture_output=True)
    shutil.rmtree(ROOT, ignore_errors=True)
    stage.rename(ROOT)
    shutil.rmtree(download, ignore_errors=True)


def app_display():
    """The X display that apps (not Steam's own VR menus) are on.

    gamescope runs two Xwayland servers: on 2026-09-28 :0 held Steam's VR bar and
    menus and ignored XTest pointer motion, while :1 held apps such as Chromium and
    took it. Inferred to hold in general.
    """
    return ":1" if Path("/tmp/.X11-unix/X1").exists() else ":0"


def daemon_env(daemon):
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{UID}/bus",
               XDG_RUNTIME_DIR=f"/run/user/{UID}", DISPLAY=app_display(), QT_QPA_PLATFORM="xcb")
    if str(daemon).startswith(str(ROOT)):
        env.update(LD_LIBRARY_PATH=str(ROOT / "usr/lib"), QT_PLUGIN_PATH=str(ROOT / "usr/lib/qt6/plugins"),
                   QML_IMPORT_PATH=str(ROOT / "usr/lib/qt6/qml"),
                   XDG_DATA_DIRS=f"{ROOT / 'usr/share'}:/usr/share")
    return env


def listening():
    try:
        socket.create_connection(("127.0.0.1", PORT), 1).close()
        return True
    except OSError:
        return False


def our_daemons():
    """Process ids of the kdeconnectd that Frame Control installed (never a system one)."""
    pids = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                if os.readlink(proc / "exe").startswith(str(ROOT) + "/"):
                    pids.append(int(proc.name))
            except OSError:
                pass
    return pids


def stop_daemon():
    """Stop our kdeconnectd and wait until it's gone (so its port is closed too)."""
    for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 30)):  # tenths of a second
        for pid in our_daemons():
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        for _ in range(wait):
            if not our_daemons() and not listening():
                return
            time.sleep(0.1)


def ensure_daemon():
    if listening():
        return
    BASE.mkdir(parents=True, exist_ok=True)
    daemon = daemon_path()
    if not daemon:
        install()
        daemon = daemon_path()
    say("starting", message="Starting KDE Connect on the Frame")
    log = open(BASE / "kdeconnectd.log", "ab")
    # Its own session, so it outlives this connection and serves the next one.
    subprocess.Popen([str(daemon)], env=daemon_env(daemon), cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True)
    for _ in range(40):
        if listening():
            return
        time.sleep(0.25)
    raise RuntimeError(f"KDE Connect didn't start; see {BASE / 'kdeconnectd.log'} on the Frame")


def qdbus(device, method):
    """Call a method on KDE Connect's D-Bus object for our device; its output, or None."""
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{UID}/bus")
    try:
        r = subprocess.run(["qdbus6", "org.kde.kdeconnect", f"/modules/kdeconnect/devices/{device}",
                            f"org.kde.kdeconnect.device.{method}"], capture_output=True, text=True, env=env, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# ---- our identity --------------------------------------------------------------

def identity(client):
    """A device id and certificate for this client, made once and kept (pairing is tied to them).

    Each computer or phone gets its own: KDE Connect keeps one connection per device,
    so a shared identity would make them knock each other off.
    """
    folder = BRIDGE / client
    folder.mkdir(parents=True, exist_ok=True)
    id_file, cert, key = folder / "id", folder / "cert.pem", folder / "key.pem"
    if not (id_file.exists() and cert.exists() and key.exists()):
        device = "framecontrol_" + os.urandom(12).hex()  # KDE Connect wants 32-38 of [A-Za-z0-9_]
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", "3650", "-subj", f"/O=KDE/OU=Kde connect/CN={device}",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        os.chmod(key, 0o600)
        id_file.write_text(device)
    return id_file.read_text().strip(), cert, key


# ---- the link ------------------------------------------------------------------

class Link:
    """One TLS connection to kdeconnectd, as a paired device that sends remote input."""

    def __init__(self, device, cert, key, port=PORT, name="Frame Control"):
        self.device, self.buf, self.keyboard = device, b"", None
        raw = socket.create_connection(("127.0.0.1", port), 5)
        raw.sendall(packet("kdeconnect.identity", {
            "deviceId": device, "deviceName": name, "deviceType": "phone", "protocolVersion": 7,
            "incomingCapabilities": [], "outgoingCapabilities": [MOUSEPAD], "tcpPort": port}))
        # KDE Connect's rule: whoever opened the TCP connection is the TLS server.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        ctx.verify_mode = ssl.CERT_NONE  # both sides are on this machine
        self.sock = ctx.wrap_socket(raw, server_side=True)
        self.sock.setblocking(False)

    def send(self, body):
        # Bounded: if KDE Connect stops reading, fail (and be restarted) rather than hang.
        self.sock.settimeout(5)
        try:
            self.sock.sendall(packet(MOUSEPAD, body))
        finally:
            self.sock.setblocking(False)

    def pair(self, paired, accept, timeout=15):
        """Ask to pair and accept it on KDE Connect's side (we control both ends).

        Only asks when not already paired: a pair request to a device that is
        already paired makes KDE Connect unpair it.
        """
        if paired():
            return
        self.sock.settimeout(5)
        self.sock.sendall(packet("kdeconnect.pair", {"pair": True}))
        self.sock.setblocking(False)
        end = time.time() + timeout
        while time.time() < end:
            accept()
            self.read(0.5)
            if paired():
                return
        raise RuntimeError("KDE Connect didn't accept the pairing")

    def read(self, wait=0.0):
        """Packets waiting from kdeconnectd; None once it has closed the connection."""
        if wait:
            sel = selectors.DefaultSelector()
            sel.register(self.sock, selectors.EVENT_READ)
            sel.select(wait)
            sel.close()
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return None
                self.buf += chunk
        except (ssl.SSLWantReadError, BlockingIOError):
            pass
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if line.strip():
                p = json.loads(line)
                if p.get("type") == "kdeconnect.mousepad.keyboardstate":
                    self.keyboard = bool(p.get("body", {}).get("state"))
                out.append(p)
        return out


def events(line):
    """The event bodies in one stdin line (an object or a list of objects)."""
    try:
        value = json.loads(line)
    except ValueError:
        return []
    return [e for e in (value if isinstance(value, list) else [value]) if isinstance(e, dict) and e]


def connect(device, cert, key, name):
    link = Link(device, cert, key, name=name)
    link.pair(lambda: qdbus(device, "isPaired") == "true", lambda: qdbus(device, "acceptPairing"))
    link.read(0.5)  # its hello, including whether it can type
    return link


def client_args():
    """argv: a folder-safe id for the computer or phone, and the name KDE Connect shows for it."""
    client = sys.argv[1] if len(sys.argv) > 1 else "default"
    client = "".join(c for c in client if c.isalnum() or c in "-_")[:64] or "default"
    name = (sys.argv[2] if len(sys.argv) > 2 else "")[:60].strip()
    return client, f"Frame Control ({name})" if name else "Frame Control"


def main():
    client, name = client_args()
    # A dropped ssh (the Frame slept, the app quit) hangs up on us: exit through the
    # clean-up below rather than dying on the spot.
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(0))
    BASE.mkdir(parents=True, exist_ok=True)
    # Every agent holds this lock shared while it runs. The last one out gets it
    # exclusively and stops KDE Connect, so it runs, and shows up on the network,
    # only while something is using the keyboard and trackpad.
    clients = open(BASE / "clients.lock", "w")
    fcntl.flock(clients, fcntl.LOCK_SH)
    try:
        return run(client, name)
    finally:
        # Finish the clean-up even if a second hang-up or TERM arrives meanwhile.
        for sig in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        fcntl.flock(clients, fcntl.LOCK_UN)
        try:
            fcntl.flock(clients, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pass  # another device is still using it
        else:
            with daemon_lock():
                stop_daemon()


class daemon_lock:
    """Installing, starting and restarting KDE Connect happen one agent at a time."""

    def __enter__(self):
        self.file = open(BASE / "daemon.lock", "w")
        fcntl.flock(self.file, fcntl.LOCK_EX)

    def __exit__(self, *_):
        self.file.close()


def run(client, name):
    try:
        with daemon_lock():
            ensure_daemon()
        device, cert, key = identity(client)
        say("pairing")
        seen = our_daemons()
        try:
            link = connect(device, cert, key, name)
        except (OSError, RuntimeError):
            if not seen:
                raise
            # Ours, but not answering (KDE Connect 24.02 can hang, for one after
            # unpairing a device that's offline): start it afresh, once. If another
            # agent already replaced it, just use the new one.
            say("starting", message="Restarting KDE Connect on the Frame")
            with daemon_lock():
                if set(our_daemons()) & set(seen):
                    stop_daemon()
                ensure_daemon()
            link = connect(device, cert, key, name)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        say("error", message=str(e))
        return 1
    say("ready", keyboard=link.keyboard is not False)
    stdin, pending = sys.stdin.fileno(), b""
    sel = selectors.DefaultSelector()
    sel.register(stdin, selectors.EVENT_READ)
    sel.register(link.sock, selectors.EVENT_READ)
    while True:
        for key_, _ in sel.select(30):
            if key_.fileobj == stdin:
                chunk = os.read(stdin, 65536)  # raw reads: a buffered readline could strand lines select can't see
                if not chunk:  # the server went away
                    return 0
                *lines, pending = (pending + chunk).split(b"\n")
                try:
                    for line in lines:
                        for body in events(line):
                            link.send(body)
                except OSError as e:
                    say("error", message=f"Lost KDE Connect: {e}")
                    return 1
            else:
                packets = link.read()
                if packets is None:
                    say("error", message="KDE Connect closed the connection")
                    return 1
                if any(p.get("type") == "kdeconnect.pair" and not p.get("body", {}).get("pair") for p in packets):
                    say("error", message="KDE Connect unpaired Frame Control")
                    return 1


if __name__ == "__main__":
    sys.exit(main())
