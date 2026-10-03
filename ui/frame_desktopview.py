#!/usr/bin/env python3
"""Linux desktop in the headset: the Linux side of ui/frame_macview.py.

Streams a screen or window of this Linux desktop to viewers on the Steam Frame
and plays their pointer and key input back here. It speaks the same HTTP and
WebSocket protocol as the Mac's frame-mac-view (mac/frame-mac-view/Sources/
main.swift), so ui/mac-view.html and the tunnel and launch code in
frame_macview.py serve both.

    python3 frame_desktopview.py serve --port 0 --page ui/mac-view.html --exit-on-eof
    (Frame Control's key in FRAME_MAC_VIEW_TOKEN)

Wayland lets no program list or grab windows, so the desktop's own portal
(xdg-desktop-portal: KDE, GNOME, ...) does it: POST /pick shows its dialog
here, where you choose a screen or window, and that becomes a source,
"portal:<n>". The same RemoteDesktop session carries input, so the portal asks
once for both; it may remember the answer (a restore token, kept in Frame
Control's data folder) so later picks skip the dialog.

Needs the system's python3 with PyGObject and GStreamer (pipewiresrc; an
H.264 encoder: nvh264enc, vah264enc or openh264enc, in that order), which
Fedora-family KDE desktops have. Not stdlib-only, unlike the rest of ui/:
it runs as its own process, never inside the server.

HTTP, as frame-mac-view. ?k= is Frame Control's key; the Frame only ever sees
single-use tickets and per-viewer reconnect keys, each tied to one source.
  GET  /ping, /view                    open: tunnel check, viewer page
  GET  /stream?src=...&t=TICKET|r=KEY  WebSocket (&codec=h264|jpeg&max=&fps=&bpp=)
  GET  /status, /windows, /displays    ?k=
  POST /pick                           ?k=: the portal's dialog; {"src": "portal:<n>", ...}
  POST /ticket?src=...                 ?k=
  POST /close[?src=...]                ?k=: end those viewers (and those shares)
  POST /permissions                    ?k=: nothing to do here; the portal asks per share
src is portal:<n> or test.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frame_host  # noqa: E402

VERSION = "1"
TICKET_LIFE = 60  # seconds a ticket may open its viewer
MAX_MESSAGE = 1 << 20

# ---- keys ------------------------------------------------------------------


def random_key():
    return secrets.token_urlsafe(24)


def same_secret(a, b):
    return bool(a) and hmac.compare_digest(a.encode(), b.encode())


# KeyboardEvent.code -> Linux evdev key code (<linux/input-event-codes.h>).
KEYCODES = {
    "Escape": 1, "Digit1": 2, "Digit2": 3, "Digit3": 4, "Digit4": 5, "Digit5": 6, "Digit6": 7, "Digit7": 8,
    "Digit8": 9, "Digit9": 10, "Digit0": 11, "Minus": 12, "Equal": 13, "Backspace": 14, "Tab": 15,
    "KeyQ": 16, "KeyW": 17, "KeyE": 18, "KeyR": 19, "KeyT": 20, "KeyY": 21, "KeyU": 22, "KeyI": 23, "KeyO": 24,
    "KeyP": 25, "BracketLeft": 26, "BracketRight": 27, "Enter": 28, "ControlLeft": 29, "KeyA": 30, "KeyS": 31,
    "KeyD": 32, "KeyF": 33, "KeyG": 34, "KeyH": 35, "KeyJ": 36, "KeyK": 37, "KeyL": 38, "Semicolon": 39,
    "Quote": 40, "Backquote": 41, "ShiftLeft": 42, "Backslash": 43, "KeyZ": 44, "KeyX": 45, "KeyC": 46,
    "KeyV": 47, "KeyB": 48, "KeyN": 49, "KeyM": 50, "Comma": 51, "Period": 52, "Slash": 53, "ShiftRight": 54,
    "NumpadMultiply": 55, "AltLeft": 56, "Space": 57, "CapsLock": 58, "F1": 59, "F2": 60, "F3": 61, "F4": 62,
    "F5": 63, "F6": 64, "F7": 65, "F8": 66, "F9": 67, "F10": 68, "NumLock": 69, "ScrollLock": 70,
    "Numpad7": 71, "Numpad8": 72, "Numpad9": 73, "NumpadSubtract": 74, "Numpad4": 75, "Numpad5": 76,
    "Numpad6": 77, "NumpadAdd": 78, "Numpad1": 79, "Numpad2": 80, "Numpad3": 81, "Numpad0": 82,
    "NumpadDecimal": 83, "IntlBackslash": 86, "F11": 87, "F12": 88, "NumpadEnter": 96, "ControlRight": 97,
    "NumpadDivide": 98, "PrintScreen": 99, "AltRight": 100, "Home": 102, "ArrowUp": 103, "PageUp": 104,
    "ArrowLeft": 105, "ArrowRight": 106, "End": 107, "ArrowDown": 108, "PageDown": 109, "Insert": 110,
    "Delete": 111, "Pause": 119, "MetaLeft": 125, "MetaRight": 126, "ContextMenu": 127,
}
BUTTONS = {0: 0x110, 1: 0x112, 2: 0x111, 3: 0x113, 4: 0x114}  # left, middle, right, back, forward (BTN_*)


def keysym(ch):
    """X keysym of one character (for typed text with no key code of its own)."""
    cp = ord(ch)
    if ch == "\n":
        return 0xFF0D  # Return
    return cp if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF else 0x01000000 + cp


def fit_size(w, h, longest):
    """(w, h) scaled to fit `longest` on its long side, both even; never enlarged."""
    s = min(1.0, longest / max(w, h, 1))
    return max(2, round(w * s / 2) * 2), max(2, round(h * s / 2) * 2)


def frame_message(key, pts_us, seq, data, echo=0):
    """A video message: flags (1 = keyframe), pts µs, sequence, echoed input; big-endian."""
    return struct.pack(">BQII", 1 if key else 0, max(0, pts_us), seq & 0xFFFFFFFF, echo) + data


# ---- the desktop portal (GLib main loop thread) ----------------------------

class PortalError(Exception):
    pass


class Portal:
    """xdg-desktop-portal RemoteDesktop sessions with a screen or window each."""

    DEST, PATH = "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
    RD, SC = "org.freedesktop.portal.RemoteDesktop", "org.freedesktop.portal.ScreenCast"

    def __init__(self, gio, glib):
        self.Gio, self.GLib = gio, glib
        self.bus = gio.bus_get_sync(gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.token_file = frame_host.data_dir("desktop-view-restore-token")

    def _v(self, sig, value):
        return self.GLib.Variant(sig, value)

    def _request(self, iface, method, args_sig, args, options, timeout=180):
        """Call a portal method that answers with a Request's Response signal."""
        token = "fc" + secrets.token_hex(6)
        path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        answer = {}

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            answer["code"], answer["results"] = params.unpack()
        # Gio delivers a signal in the subscribing thread's main context: this
        # thread's own, turned here while waiting (the main loop is another thread's).
        ctx = self.GLib.MainContext.new()
        ctx.push_thread_default()
        sub = self.bus.signal_subscribe(self.DEST, "org.freedesktop.portal.Request", "Response", path, None,
                                        self.Gio.DBusSignalFlags.NO_MATCH_RULE, on_response)
        try:
            opts = {"handle_token": self._v("s", token), **options}
            self.bus.call_sync(self.DEST, self.PATH, iface, method, self._v(f"({args_sig}a{{sv}})", (*args, opts)),
                               None, self.Gio.DBusCallFlags.NONE, 30000, None)
            deadline = time.monotonic() + timeout
            while "code" not in answer:
                if time.monotonic() > deadline:
                    raise PortalError("The screen-sharing dialog wasn't answered in time.")
                if not ctx.iteration(False):
                    time.sleep(0.02)
        finally:
            self.bus.signal_unsubscribe(sub)
            ctx.pop_thread_default()
        if answer["code"] == 1:
            raise PortalError("Sharing was cancelled.")
        if answer["code"] != 0:
            raise PortalError("The desktop refused to share the screen.")
        return answer["results"]

    def pick(self):
        """Ask (or reuse a remembered answer) for one screen or window, with input. A dict per share."""
        session = self._request(self.RD, "CreateSession", "", (), {
            "session_handle_token": self._v("s", "fc" + secrets.token_hex(6))})["session_handle"]
        try:
            devices = {"types": self._v("u", 1 | 2), "persist_mode": self._v("u", 2)}  # keyboard, pointer; until revoked
            token = self._restore_token()
            if token:
                devices["restore_token"] = self._v("s", token)
            self._request(self.RD, "SelectDevices", "o", (session,), devices)
            self._request(self.SC, "SelectSources", "o", (session,), {
                "types": self._v("u", 1 | 2), "multiple": self._v("b", False),  # monitor | window
                "cursor_mode": self._v("u", 2)})  # the pointer drawn into the picture
            started = self._request(self.RD, "Start", "os", (session, ""), {})
            if started.get("restore_token"):
                self._save_restore_token(started["restore_token"])
            streams = started.get("streams") or []
            if not streams:
                raise PortalError("Nothing was chosen to share.")
            node, props = streams[0]
            fd = self._open_remote(session)
        except Exception:
            self.close(session)
            raise
        w, h = props.get("size") or (0, 0)
        kind = "Window" if props.get("source_type") == 2 else "Screen"
        return {"session": session, "node": node, "fd": fd, "w": w, "h": h,
                "input": bool(started.get("devices", 0) & 2), "kind": kind}

    def _open_remote(self, session):
        res, fds = self.bus.call_with_unix_fd_list_sync(
            self.DEST, self.PATH, self.SC, "OpenPipeWireRemote", self._v("(oa{sv})", (session, {})), None,
            self.Gio.DBusCallFlags.NONE, 10000, None, None)
        return fds.get(res.unpack()[0])

    def _restore_token(self):
        try:
            return self.token_file.read_text().strip()
        except OSError:
            return ""

    def _save_restore_token(self, token):
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.token_file.with_suffix(".tmp")
            tmp.write_text(token)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.token_file)
        except OSError:
            pass

    def watch_closed(self, session, callback):
        """callback() once the desktop ends the session (sharing stopped from its tray, say).
        Subscribed from the main loop, so the signal arrives there for as long as it lasts."""
        def subscribe():
            self.bus.signal_subscribe(self.DEST, "org.freedesktop.portal.Session", "Closed", session, None,
                                      self.Gio.DBusSignalFlags.NO_MATCH_RULE,
                                      lambda *a: threading.Thread(target=callback, daemon=True).start())
            return False
        self.GLib.idle_add(subscribe)

    def close(self, session):
        try:
            self.bus.call_sync(self.DEST, session, "org.freedesktop.portal.Session", "Close", None, None,
                               self.Gio.DBusCallFlags.NONE, 5000, None)
        except Exception:  # noqa: BLE001 - already gone
            pass

    def _notify(self, session, method, sig, *args):
        try:
            self.bus.call_sync(self.DEST, self.PATH, self.RD, method, self._v(f"(oa{{sv}}{sig})", (session, {}, *args)),
                               None, self.Gio.DBusCallFlags.NONE, 1000, None)
        except Exception as e:  # noqa: BLE001 - one lost event mustn't end the stream
            print(f"frame_desktopview: {method}: {e}", file=sys.stderr)

    def move(self, session, node, x, y):
        self._notify(session, "NotifyPointerMotionAbsolute", "udd", node, x, y)

    def button(self, session, code, down):
        self._notify(session, "NotifyPointerButton", "iu", code, 1 if down else 0)

    def scroll(self, session, dx, dy):
        self._notify(session, "NotifyPointerAxis", "dd", dx, dy)

    def key(self, session, code, down):
        self._notify(session, "NotifyKeyboardKeycode", "iu", code, 1 if down else 0)

    def keysym(self, session, sym, down):
        self._notify(session, "NotifyKeyboardKeysym", "iu", sym, 1 if down else 0)


# ---- encoding --------------------------------------------------------------

# (element, its settings for kbit/s bitrate, gop, and the raw format it takes)
ENCODERS = {
    "nvh264enc": ("nvh264enc preset=p1 tune=ultra-low-latency rc-mode=cbr bitrate={kbps} gop-size={gop} "
                  "bframes=0 zerolatency=true repeat-sequence-header=true", "NV12"),
    "vah264enc": ("vah264enc rate-control=cbr bitrate={kbps} key-int-max={gop} b-frames=0 target-usage=7", "NV12"),
    "openh264enc": ("openh264enc bitrate={bps} complexity=low gop-size={gop} usage-type=screen", "I420"),
}


class Media:
    """GStreamer, loaded on first use (the agent's other endpoints don't need it)."""

    def __init__(self, gst):
        self.Gst = gst
        self.h264 = self._pick_encoder()

    def _pick_encoder(self):
        want = os.environ.get("FRAME_DESKTOP_VIEW_ENCODER")
        for name in ([want] if want else ENCODERS):
            if name in ENCODERS and self.Gst.ElementFactory.find(name) and self._works(name):
                return name
        return None

    def _works(self, name):
        """A real encoder can be installed but unusable (no GPU access): try one frame."""
        try:
            p = self.Gst.parse_launch(f"videotestsrc num-buffers=2 ! video/x-raw,width=320,height=240 ! videoconvert ! "
                                      f"video/x-raw,format={ENCODERS[name][1]} ! "
                                      f"{ENCODERS[name][0].format(kbps=1000, bps=1000000, gop=30)} ! fakesink")
            p.set_state(self.Gst.State.PLAYING)
            msg = p.get_bus().timed_pop_filtered(5 * self.Gst.SECOND, self.Gst.MessageType.EOS | self.Gst.MessageType.ERROR)
            p.set_state(self.Gst.State.NULL)
            return msg is not None and msg.type == self.Gst.MessageType.EOS
        except Exception:  # noqa: BLE001
            return False

    def pipeline(self, source, codec, size, fps, bps):
        """source: a GStreamer source description. Ends in an appsink named "out"."""
        w, h = size
        scale = f"videoconvertscale ! video/x-raw,width={w},height={h},pixel-aspect-ratio=1/1 ! " if w and h else ""
        # Raw pictures may be dropped (the newest kept) while the network or the
        # encoder is behind; encoded ones never are, so no reference goes missing.
        head = (f"{source} ! videorate drop-only=true max-rate={fps} ! queue leaky=downstream max-size-buffers=1 "
                f"max-size-time=0 max-size-bytes=0 ! {scale}videoconvert")
        if codec == "jpeg":
            tail = "jpegenc quality=80"
        else:
            if not self.h264:
                raise RuntimeError("No working H.264 encoder (nvh264enc, vah264enc or openh264enc) on this computer.")
            element, fmt = ENCODERS[self.h264]
            tail = (f"video/x-raw,format={fmt} ! {element.format(kbps=bps // 1000, bps=bps, gop=fps * 2)} ! "
                    "h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au")
        return self.Gst.parse_launch(f"{head} ! {tail} ! appsink name=out sync=false max-buffers=2 drop=false")


# ---- sources ---------------------------------------------------------------

class Share:
    """One screen or window the portal handed over."""

    def __init__(self, n, info):
        self.n, self.src = n, f"portal:{n}"
        self.session, self.node, self.fd = info["session"], info["node"], info["fd"]
        self.w, self.h, self.kind, self.input = info["w"], info["h"], info["kind"], info["input"]
        self.title = f"{self.kind} {self.w}×{self.h}" if self.w else self.kind

    def gst_source(self):
        # Each pipeline its own copy of the PipeWire connection: pipewiresrc closes it.
        return f"pipewiresrc fd={os.dup(self.fd)} path={self.node} do-timestamp=true keepalive-time=1000"


TEST_SOURCE = "videotestsrc is-live=true pattern=smpte ! video/x-raw,width=1920,height=1080,framerate=60/1"


# ---- HTTP and WebSocket ----------------------------------------------------

class WebSocket:
    """Server side of RFC 6455 on a blocking socket; sends from any thread."""

    def __init__(self, sock):
        self.sock, self.lock, self.closed = sock, threading.Lock(), False

    def send(self, opcode, payload):
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else struct.pack(">BH", 126, n) if n < 65536
                                         else struct.pack(">BQ", 127, n))
        with self.lock:
            if self.closed:
                return False
            try:
                self.sock.sendall(head + payload)
                return True
            except OSError:
                self.closed = True
                return False

    def send_json(self, obj):
        return self.send(1, json.dumps(obj).encode())

    def _read(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError
            out += chunk
        return out

    def messages(self):
        """Text messages from the viewer, until it goes."""
        fragments = b""
        try:
            while True:
                b0, b1 = self._read(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                if n > MAX_MESSAGE:
                    return
                mask = self._read(4) if b1 & 0x80 else b"\0\0\0\0"
                data = bytes(c ^ mask[i & 3] for i, c in enumerate(self._read(n)))
                op = b0 & 0x0F
                if op == 8:
                    return
                if op == 9:
                    self.send(10, data)
                elif op in (0, 1):
                    fragments += data
                    if b0 & 0x80:
                        yield fragments.decode("utf-8", "replace")
                        fragments = b""
        except (OSError, ConnectionError, ValueError):
            return

    def close(self):
        if not self.closed:
            self.send(8, b"\x03\xe8")
        with self.lock:
            self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()


class Viewer:
    """One viewer watching one source: a pipeline whose output goes to its WebSocket."""

    def __init__(self, agent, src, ws, codec, size, fps, bps, key, share):
        self.agent, self.src, self.ws, self.codec, self.key, self.share = agent, src, ws, codec, key, share
        self.id = agent.next_id()
        self.size, self.fps, self.bps = size, fps, bps
        self.stopped = threading.Event()
        self.pipe = None
        self.frames, self.bytes, self.sent_at = 0, 0, []

    @property
    def title(self):
        return self.share.title if self.share else "Test pattern"

    def start(self):
        try:
            source = self.share.gst_source() if self.share else TEST_SOURCE
            self.pipe = self.agent.media().pipeline(source, self.codec, self.size, self.fps, self.bps)
        except Exception as e:  # noqa: BLE001 - its words go to the viewer
            self.ws.send_json({"t": "error", "message": str(e)})
            return self.end()
        self.ws.send_json({"t": "hello", "r": self.key})
        w, h = self.size if self.size[0] else (self.share.w, self.share.h) if self.share else (1920, 1080)
        self.ws.send_json({"t": "info", "src": self.src, "title": self.title, "app": "", "codec": self.codec,
                           "input": bool(self.share and self.share.input), "warm": 0, "aspect": w / max(h, 1)})
        self.pipe.set_state(self.agent.Gst.State.PLAYING)
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._stats, daemon=True).start()
        for text in self.ws.messages():
            if self.stopped.is_set():
                break
            try:
                self.handle(json.loads(text))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        self.end()

    def _pump(self):
        Gst, sink, seq = self.agent.Gst, self.pipe.get_by_name("out"), 0
        bus = self.pipe.get_bus()
        while not self.stopped.is_set():
            sample = sink.emit("try-pull-sample", 200 * Gst.MSECOND)
            if sample is None:
                msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if msg is not None:
                    if msg.type == Gst.MessageType.EOS:
                        self.ws.send_json({"t": "closed", "reason": "the window or screen is no longer shared"})
                    else:
                        self.ws.send_json({"t": "error", "message": msg.parse_error()[0].message})
                    break
                continue
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            data = bytes(info.data)
            buf.unmap(info)
            seq += 1
            key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
            pts = buf.pts // 1000 if buf.pts != Gst.CLOCK_TIME_NONE else int(time.monotonic() * 1e6)
            if not self.ws.send(2, frame_message(key, pts, seq, data)):
                break
            self.frames += 1
            self.bytes += len(data)
        self.end()

    def _stats(self):
        last_frames, last_bytes = 0, 0
        while not self.stopped.wait(1):
            fps, mbps = self.frames - last_frames, (self.bytes - last_bytes) * 8 / 1e6
            last_frames, last_bytes = self.frames, self.bytes
            self.last = {"fps": fps, "mbps": round(mbps, 1)}
            self.ws.send_json({"t": "stats", "size": "{}×{}".format(*self.size) if self.size[0] else "",
                               "fps": fps, "sentFps": fps, "mbps": round(mbps, 1), "bitrate": self.bps})

    def key_frame(self):
        self.pipe.send_event(GI["GstVideo"].video_event_new_upstream_force_key_unit(
            self.agent.Gst.CLOCK_TIME_NONE, True, 0))

    def handle(self, m):
        t = m.get("t")
        if t == "ping":
            self.ws.send_json({"t": "pong", "c": m.get("c", 0), "a": int(time.monotonic() * 1e6)})
        elif t == "ack":
            self.agent.acknowledged(self.key)
        elif t == "key-frame" and self.codec == "h264":
            self.key_frame()
        elif self.share and self.share.input:
            self.input(t, m)

    def input(self, t, m):
        portal, s = self.agent.portal(), self.share
        x, y = m.get("x"), m.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and t in ("m", "wheel") and s.w:
            portal.move(s.session, s.node, min(max(x, 0), 1) * s.w, min(max(y, 0), 1) * s.h)
        if t == "m" and m.get("e") in ("down", "up"):
            code = BUTTONS.get(m.get("b", 0))
            if code:
                portal.button(s.session, code, m["e"] == "down")
                self.agent.held(self, ("b", code), m["e"] == "down")
        elif t == "wheel":
            portal.scroll(s.session, float(m.get("dx") or 0), float(m.get("dy") or 0))
        elif t == "k" and m.get("e") in ("down", "up"):
            down, code = m["e"] == "down", KEYCODES.get(m.get("code", ""))
            if code:
                portal.key(s.session, code, down)
                self.agent.held(self, ("k", code), down)
            elif isinstance(m.get("key"), str) and len(m["key"]) == 1:
                portal.keysym(s.session, keysym(m["key"]), down)
        elif t == "text" and isinstance(m.get("s"), str) and len(m["s"]) <= 4096:
            for ch in m["s"]:
                portal.keysym(s.session, keysym(ch), True)
                portal.keysym(s.session, keysym(ch), False)
        elif t == "release":
            self.agent.release(self)

    def end(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        if self.pipe:
            self.pipe.set_state(self.agent.Gst.State.NULL)
        self.agent.release(self)
        self.ws.close()
        self.agent.ended(self)


class Agent:
    def __init__(self, token, page):
        self.token, self.page = token, page
        self.lock = threading.RLock()
        self.viewers, self.shares, self.finished = {}, {}, set()
        self.tickets = {}  # ticket -> [src, expiry, reconnect key once redeemed]
        self.reconnect = {}  # reconnect key -> src, until stopped
        self.pressed = {}  # viewer id -> {("b"|"k", code)} held down by it
        self._ids, self._shares = 0, 0
        self._media = self._portal = None
        self.Gst = None

    # gi's modules are imported by main(), on the main thread: importing them
    # first from a server thread while GLib's loop runs breaks PyGObject.
    def media(self):
        with self.lock:
            if self._media is None:
                if GI is None:
                    raise RuntimeError("Streaming needs PyGObject and GStreamer (python3-gobject, gstreamer1).")
                self.Gst = GI["Gst"]
                self._media = Media(self.Gst)
            return self._media

    def portal(self):
        with self.lock:
            if self._portal is None:
                if GI is None:
                    raise PortalError("Choosing a screen needs PyGObject (python3-gobject).")
                self._portal = Portal(GI["Gio"], GI["GLib"])
            return self._portal

    def next_id(self):
        with self.lock:
            self._ids += 1
            return self._ids

    # ---- keys and tickets, as frame-mac-view ----

    def may_stream(self, query, src):
        with self.lock:
            now = time.time()
            self.tickets = {t: e for t, e in self.tickets.items() if e[1] > now}
            if same_secret(query.get("k", ""), self.token):
                return random_key()
            t = query.get("t")
            if t and t in self.tickets and self.tickets[t][0] == src:
                entry = self.tickets[t]
                entry[2] = entry[2] or random_key()  # a retry before the viewer had its key gets the same one
                self.reconnect[entry[2]] = src
                return entry[2]
            r = query.get("r")
            if r and self.reconnect.get(r) == src:
                return r
        return None

    def acknowledged(self, key):
        with self.lock:
            self.tickets = {t: e for t, e in self.tickets.items() if e[2] != key}

    # ---- input held down, released when a viewer goes ----

    def held(self, viewer, what, down):
        with self.lock:
            keys = self.pressed.setdefault(viewer.id, set())
            (keys.add if down else keys.discard)(what)

    def release(self, viewer):
        with self.lock:
            keys = self.pressed.pop(viewer.id, set())
        if viewer.share and keys:
            portal = self.portal()
            for kind, code in keys:
                (portal.button if kind == "b" else portal.key)(viewer.share.session, code, False)

    def ended(self, viewer):
        with self.lock:
            if self.viewers.get(viewer.id) is viewer:
                del self.viewers[viewer.id]

    # ---- shares ----

    def pick(self):
        info = self.portal().pick()
        with self.lock:
            self._shares += 1
            share = Share(self._shares, info)
            self.shares[share.src] = share
        self.portal().watch_closed(share.session, lambda: self.share_ended(share.src))
        return share

    def share_ended(self, src, reason="sharing was stopped on the computer"):
        with self.lock:
            share = self.shares.pop(src, None)
            self.finished.add(src)
            self.reconnect = {k: s for k, s in self.reconnect.items() if s != src}
            viewers = [v for v in self.viewers.values() if v.src == src]
        for v in viewers:
            v.ws.send_json({"t": "closed", "reason": reason})
            v.end()
        if share:
            self.portal().close(share.session)
            try:
                os.close(share.fd)
            except OSError:
                pass

    def close(self, src=None):
        with self.lock:
            matching = [v for v in self.viewers.values() if src is None or v.src == src]
            self.reconnect = {k: s for k, s in self.reconnect.items() if src is not None and s != src}
            self.tickets = {t: e for t, e in self.tickets.items() if src is not None and e[0] != src}
            shares = [s for s in self.shares if src is None or s == src]
        for v in matching:
            v.ws.send_json({"t": "close"})
        threading.Timer(0.3, lambda: [v.end() for v in matching]).start()
        for s in shares:
            self.share_ended(s, "stopped")
        return len(matching)

    def quit(self):
        self.close()
        time.sleep(0.5)
        os._exit(0)

    # ---- HTTP ----

    def status(self):
        with self.lock:
            streams = [{"id": v.id, "src": v.src, "title": v.title, "app": "", "bitrate": v.bps,
                        "stats": getattr(v, "last", {})} for v in self.viewers.values()]
            return {"version": VERSION, "screen": True, "accessibility": True, "streams": streams,
                    "finished": sorted(self.finished), "picker": True}

    def sources(self):
        with self.lock:
            return [{"id": s.n, "src": s.src, "app": s.kind, "title": s.title, "w": s.w, "h": s.h, "pid": 0}
                    for s in self.shares.values()]

    def route(self, method, path, query, headers, conn):
        """(status, content type, body) for plain HTTP, or None once a WebSocket took the connection."""
        if (method, path) == ("GET", "/ping"):
            return 200, "text/plain", b"frame-mac-view"
        if (method, path) == ("GET", "/view"):
            try:
                return 200, "text/html; charset=utf-8", Path(self.page).read_bytes()
            except (OSError, TypeError):
                return 404, "text/plain", b"no viewer page"
        if (method, path) == ("GET", "/stream"):
            return self.stream(query, headers, conn)
        if not same_secret(query.get("k") or headers.get("x-token", ""), self.token):
            return 403, "text/plain", b"forbidden"
        if (method, path) == ("GET", "/status"):
            return self.json(self.status())
        if (method, path) == ("GET", "/windows"):
            return self.json({"windows": self.sources(), "screen": True})
        if (method, path) == ("GET", "/displays"):
            return self.json({"displays": []})
        if (method, path) == ("POST", "/pick"):
            try:
                s = self.pick()
            except Exception as e:  # noqa: BLE001 - PortalError, D-Bus errors: told to the person
                return self.json({"error": str(e)}, 409)
            return self.json({"src": s.src, "title": s.title, "w": s.w, "h": s.h, "input": s.input})
        if (method, path) == ("POST", "/ticket"):
            src = query.get("src", "")
            with self.lock:
                if src != "test" and src not in self.shares:
                    return 400, "text/plain", b"bad src"
                t = random_key()
                self.tickets[t] = [src, time.time() + TICKET_LIFE, None]
                self.finished.discard(src)
            return self.json({"ticket": t})
        if (method, path) == ("POST", "/close"):
            return self.json({"closed": self.close(query.get("src"))})
        if (method, path) == ("POST", "/permissions"):
            return self.json({"screen": True, "accessibility": True})
        return 404, "text/plain", b"not found"

    @staticmethod
    def json(obj, status=200):
        return status, "application/json", json.dumps(obj, sort_keys=True).encode()

    def stream(self, query, headers, conn):
        src = query.get("src", "")
        with self.lock:
            share = self.shares.get(src)
        if src != "test" and share is None:
            return 400, "text/plain", b"bad src"
        key = self.may_stream(query, src)
        if not key:
            return 403, "text/plain", b"forbidden"
        if headers.get("upgrade", "").lower() != "websocket" or "sec-websocket-key" not in headers:
            return 400, "text/plain", b"expected a WebSocket upgrade"
        codec = "jpeg" if query.get("codec") == "jpeg" else "h264"

        def number(name, default, lo, hi, kind=int):
            try:
                return min(max(kind(query.get(name, default)), lo), hi)
            except ValueError:
                return default
        longest, fps, bpp = number("max", 1920, 320, 3840), number("fps", 60, 5, 120), number("bpp", 0.1, 0.02, 0.5, float)
        w, h = (share.w, share.h) if share else (1920, 1080)
        size = fit_size(w, h, longest) if w and h else (0, 0)
        bps = int(max(size[0], 640) * max(size[1], 360) * fps * bpp)
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept_key(headers['sec-websocket-key'])}\r\n\r\n").encode())
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        viewer = Viewer(self, src, WebSocket(conn), codec, size, fps, bps, key, share)
        with self.lock:
            replaced = [v for v in self.viewers.values() if v.key == key]  # one live viewer per key
            self.viewers[viewer.id] = viewer
        for old in replaced:
            old.end()
        viewer.start()  # until the viewer goes
        return None


def parse_head(head):
    """(method, path, query, lower-cased headers) of an HTTP request head, or None."""
    try:
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
    except ValueError:
        return None
    url = urlparse(target)
    query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return method, url.path, query, headers


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        conn, head = self.request, b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(16384)
            if not chunk or len(head) > 16384:
                return
            head += chunk
        req = parse_head(head.split(b"\r\n\r\n", 1)[0])
        if not req:
            return self.respond(400, "text/plain", b"bad request")
        try:
            out = self.server.agent.route(*req, conn)
        except Exception as e:  # noqa: BLE001
            print(f"frame_desktopview: {req[1]}: {e!r}", file=sys.stderr)
            out = (500, "text/plain", str(e).encode())
        if out:
            self.respond(*out)

    def respond(self, status, ctype, body):
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict"}.get(status, "Error")
        self.request.sendall(f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                             "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


GI = None  # PyGObject's modules once main() has loaded them, or None without it


def load_gi():
    global GI
    try:
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gio, GLib, Gst, GstVideo
    except (ImportError, ValueError):
        return
    Gst.init(None)
    GI = {"Gio": Gio, "GLib": GLib, "Gst": Gst, "GstVideo": GstVideo}


def main(argv):
    if not argv or argv[0] != "serve":
        sys.exit(__doc__)
    args = dict(zip(argv[1::2], argv[2::2]))
    token = os.environ.get("FRAME_MAC_VIEW_TOKEN", "")
    if not token:
        sys.exit("frame_desktopview: set FRAME_MAC_VIEW_TOKEN")
    load_gi()
    agent = Agent(token, args.get("--page"))
    # SIGTERM (Frame Control quitting) closes the viewers first, as frame-mac-view does.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=agent.quit, daemon=True).start())
    server = Server(("127.0.0.1", int(args.get("--port", "0"))), Handler)
    server.agent = agent
    print(f"frame-mac-view listening on 127.0.0.1:{server.server_address[1]}", flush=True)
    if "--exit-on-eof" in argv:
        def watch():
            while sys.stdin.buffer.read1(1024):
                pass
            agent.quit()
        threading.Thread(target=watch, daemon=True).start()
    # The portal's signals arrive through GLib's main loop; it runs here once gi
    # is there, or the server alone runs (it then can't pick, but serves tests).
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if GI:
        GI["GLib"].MainLoop().run()
    else:
        threading.Event().wait()


if __name__ == "__main__":
    main(sys.argv[1:])
