"""The Linux desktop agent (ui/frame_desktopview.py) without a desktop: its
HTTP routes, tickets and keys, WebSocket framing, input mapping, and how
frame_macview.py runs it. The portal and GStreamer are stood in for.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import json
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_desktopview as dv  # noqa: E402
import frame_macview  # noqa: E402


class Pieces(unittest.TestCase):
    def test_fit_size(self):
        self.assertEqual(dv.fit_size(3840, 2160, 1920), (1920, 1080))
        self.assertEqual(dv.fit_size(1280, 720, 1920), (1280, 720))  # never enlarged
        self.assertEqual(dv.fit_size(1001, 3001, 1000), (334, 1000))  # long side, even

    def test_frame_message_header_is_what_the_viewer_reads(self):
        m = dv.frame_message(True, 123456, 7, b"\x00\x00\x00\x01\x65")
        self.assertEqual(len(m), 17 + 5)
        flags, pts, seq, echo = struct.unpack(">BQII", m[:17])
        self.assertEqual((flags, pts, seq, echo), (1, 123456, 7, 0))  # mac-view.html: b[0] & 1, getUint32(9)
        self.assertEqual(dv.frame_message(False, 0, 1, b"")[0], 0)

    def test_keysyms(self):
        self.assertEqual(dv.keysym("a"), 0x61)
        self.assertEqual(dv.keysym("é"), 0xE9)
        self.assertEqual(dv.keysym("€"), 0x010020AC)
        self.assertEqual(dv.keysym("\n"), 0xFF0D)

    def test_parse_head(self):
        method, path, query, headers = dv.parse_head(
            b"GET /stream?src=portal%3A1&t=abc HTTP/1.1\r\nUpgrade: websocket\r\nSec-WebSocket-Key: k==")
        self.assertEqual((method, path, query["src"], query["t"]), ("GET", "/stream", "portal:1", "abc"))
        self.assertEqual(headers["upgrade"], "websocket")
        self.assertIsNone(dv.parse_head(b"nonsense"))

    def test_accept_key_matches_rfc_6455(self):
        self.assertEqual(dv.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


class FakePortal:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a))

    def pick(self):
        return {"session": "/s/1", "node": 42, "fd": -1, "w": 2560, "h": 1440, "input": True, "kind": "Window"}


def agent():
    a = dv.Agent("secret", str(ROOT / "ui" / "mac-view.html"))
    a._portal = FakePortal()
    return a


class Routes(unittest.TestCase):
    def call(self, a, method, path, **query):
        status, ctype, body = a.route(method, path, query, {}, None)
        return status, json.loads(body) if ctype == "application/json" else body

    def test_open_and_keyed_routes(self):
        a = agent()
        self.assertEqual(self.call(a, "GET", "/ping"), (200, b"frame-mac-view"))
        self.assertIn(b"VideoDecoder", self.call(a, "GET", "/view")[1])
        for path in ("/status", "/windows", "/ticket", "/close"):
            self.assertEqual(self.call(a, "GET" if path in ("/status", "/windows") else "POST", path)[0], 403, path)
            self.assertEqual(self.call(a, "GET", path, k="wrong")[0], 403, path)
        status, s = self.call(a, "GET", "/status", k="secret")
        self.assertEqual((s["screen"], s["accessibility"], s["picker"], s["streams"]), (True, True, True, []))

    def test_pick_becomes_a_source_with_tickets(self):
        a = agent()
        status, picked = self.call(a, "POST", "/pick", k="secret")
        self.assertEqual(status, 200)
        self.assertEqual(picked, {"src": "portal:1", "title": "Window 2560×1440", "w": 2560, "h": 1440, "input": True})
        windows = self.call(a, "GET", "/windows", k="secret")[1]["windows"]
        self.assertEqual([w["src"] for w in windows], ["portal:1"])
        self.assertEqual(self.call(a, "POST", "/ticket", k="secret", src="portal:9")[0], 400)  # never shared
        ticket = self.call(a, "POST", "/ticket", k="secret", src="portal:1")[1]["ticket"]
        key = a.may_stream({"t": ticket}, "portal:1")
        self.assertTrue(key)
        self.assertIsNone(a.may_stream({"t": ticket}, "test"))  # tied to its source
        self.assertEqual(a.may_stream({"t": ticket}, "portal:1"), key)  # a retry before the ack: the same key
        a.acknowledged(key)
        self.assertIsNone(a.may_stream({"t": ticket}, "portal:1"))  # spent
        self.assertEqual(a.may_stream({"r": key}, "portal:1"), key)  # reconnects with the key
        self.call(a, "POST", "/close", k="secret", src="portal:1")
        self.assertIsNone(a.may_stream({"r": key}, "portal:1"))
        self.assertIn(("close", "/s/1"), a._portal.calls)  # the share ends at the desktop too
        self.assertEqual(self.call(a, "GET", "/status", k="secret")[1]["finished"], ["portal:1"])

    def test_pick_failure_is_explained(self):
        a = agent()

        def cancelled():
            raise dv.PortalError("Sharing was cancelled.")
        a._portal.pick = cancelled
        self.assertEqual(self.call(a, "POST", "/pick", k="secret"), (409, {"error": "Sharing was cancelled."}))

    def test_tickets_expire(self):
        a = agent()
        ticket = self.call(a, "POST", "/ticket", k="secret", src="test")[1]["ticket"]
        with mock.patch.object(dv.time, "time", return_value=time.time() + dv.TICKET_LIFE + 1):
            self.assertIsNone(a.may_stream({"t": ticket}, "test"))


class Input(unittest.TestCase):
    def viewer(self):
        a = agent()
        share = dv.Share(1, FakePortal().pick())
        return a, dv.Viewer(a, share.src, mock.Mock(), "h264", (1920, 1080), 60, 1, "key", share)

    def test_pointer_buttons_wheel_keys(self):
        a, v = self.viewer()
        v.handle({"t": "m", "e": "down", "b": 0, "x": 0.5, "y": 0.25})
        v.handle({"t": "m", "e": "up", "b": 2, "x": 2, "y": -1})
        v.handle({"t": "wheel", "dx": 0, "dy": 120, "x": 0.1, "y": 0.1})
        v.handle({"t": "k", "e": "down", "code": "KeyA", "key": "a"})
        v.handle({"t": "k", "e": "down", "code": "", "key": "ß"})  # no key code: by keysym
        self.assertEqual(a._portal.calls, [
            ("move", "/s/1", 42, 1280.0, 360.0), ("button", "/s/1", 0x110, True),
            ("move", "/s/1", 42, 2560, 0), ("button", "/s/1", 0x111, False),  # clamped to the picture
            ("move", "/s/1", 42, 256.0, 144.0), ("scroll", "/s/1", 0.0, 120.0),
            ("key", "/s/1", 30, True), ("keysym", "/s/1", 0xDF, True)])

    def test_held_input_is_let_go_when_the_viewer_goes(self):
        a, v = self.viewer()
        v.handle({"t": "m", "e": "down", "b": 0, "x": 0.5, "y": 0.5})
        v.handle({"t": "k", "e": "down", "code": "ShiftLeft", "key": "Shift"})
        a._portal.calls.clear()
        v.handle({"t": "release"})
        self.assertEqual(sorted(a._portal.calls), [("button", "/s/1", 0x110, False), ("key", "/s/1", 42, False)])
        a._portal.calls.clear()
        v.handle({"t": "release"})
        self.assertEqual(a._portal.calls, [])

    def test_look_only_share_ignores_input(self):
        a, v = self.viewer()
        v.share.input = False
        v.handle({"t": "m", "e": "down", "b": 0, "x": 0.5, "y": 0.5})
        self.assertEqual(a._portal.calls, [])


class WebSocketFraming(unittest.TestCase):
    def test_masked_text_in_frames_out(self):
        here, there = socket.socketpair()
        ws = dv.WebSocket(here)
        got = []
        t = threading.Thread(target=lambda: got.extend(ws.messages()))
        t.start()
        mask = b"\x01\x02\x03\x04"
        for text in (b'{"t":"ack"}', b"x" * 300):
            head = bytes([0x81, 0x80 | len(text)]) if len(text) < 126 else bytes([0x81, 0x80 | 126]) + struct.pack(">H", len(text))
            there.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(text)))
        there.sendall(bytes([0x88, 0x80]) + mask)  # close
        t.join(5)
        self.assertEqual(got, ['{"t":"ack"}', "x" * 300])
        self.assertTrue(ws.send(2, b"\x00" * 70000))
        head = there.recv(10)
        self.assertEqual(head[:2], bytes([0x82, 127]))
        self.assertEqual(struct.unpack(">Q", head[2:10])[0], 70000)
        here.close(), there.close()


class MacViewOnLinux(unittest.TestCase):
    def test_runs_the_desktop_agent_on_the_system_python(self):
        with mock.patch.multiple(frame_macview, MAC=False, LINUX=True), \
             mock.patch.object(frame_macview, "system_python", return_value="/usr/bin/python3"):
            cmd = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")._command(0)
        self.assertEqual(cmd[:3], ["/usr/bin/python3", str(ROOT / "ui" / "frame_desktopview.py"), "serve"])

    def test_system_env_drops_the_apps_python(self):
        with mock.patch.dict(frame_macview.os.environ, {"PYTHONHOME": "/app/python", "PYTHONPATH": "/x", "HOME": "/h"}):
            env = frame_macview.system_env()
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["HOME"], "/h")

    def test_missing_packages_are_named(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        for out, words in (("ok", None), ("missing pipewiresrc", "pipewiresrc"), ("", "PyGObject")):
            mv._linux_reason = None
            with mock.patch.object(frame_macview, "system_python", return_value="/usr/bin/python3"), \
                 mock.patch.object(frame_macview.subprocess, "run", return_value=mock.Mock(stdout=out)):
                reason = mv._linux_unavailable()
            if words:
                self.assertIn(words, reason)
            else:
                self.assertIsNone(reason)

    def test_show_picks_first_then_launches_the_share(self):
        calls = []

        def call(path, method="GET", timeout=10, **q):
            calls.append((path, q.get("src")))
            return {"/pick": {"src": "portal:3", "title": "Screen 2560×1440", "w": 2560, "h": 1440},
                    "/status": {"screen": True, "streams": []}, "/ticket": {"ticket": "T"}}[path]
        run = mock.Mock(return_value="panel valve.steam.desktopgame.1 window 0x1")
        mv = frame_macview.MacView(["ssh"], run, "frame")
        with mock.patch.multiple(frame_macview, MAC=False, LINUX=True, HOST="computer"), \
             mock.patch.object(mv, "call", side_effect=call), mock.patch.object(mv, "ensure_tunnel"):
            mv.remote_port = 47900
            r = mv.show("pick")
        self.assertEqual(calls, [("/pick", None), ("/status", None), ("/ticket", "portal:3")])
        self.assertEqual((r["src"], r["title"]), ("portal:3", "Screen 2560×1440"))
        url = run.call_args.args[0]
        self.assertIn("src=portal%3A3", url)
        self.assertIn("host=computer", url)
        with mock.patch.multiple(frame_macview, MAC=False, LINUX=True):
            with self.assertRaises(frame_macview.MacViewError):
                mv.show("window:12")  # a Mac source means nothing here


if __name__ == "__main__":
    unittest.main()
