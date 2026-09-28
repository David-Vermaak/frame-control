"""Keyboard and pointer: event checks, and the agent's KDE Connect protocol against a fake kdeconnectd.

The fake follows what KDE Connect 24.02 does (read from its source,
core/backends/lan/lanlinkprovider.cpp): it accepts the TCP connection, reads the
identity line, then starts TLS as the *client*.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))


class InputEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server

    def check(self, event):
        return self.server.input_event(event)

    def test_keeps_known_fields(self):
        self.assertEqual(self.check({"dx": 3, "dy": -1.234}), {"dx": 3.0, "dy": -1.23})
        self.assertEqual(self.check({"singleclick": True, "other": 1}), {"singleclick": True})
        self.assertEqual(self.check({"key": "héllo", "shift": True}), {"key": "héllo", "shift": True})
        self.assertEqual(self.check({"specialKey": 12, "ctrl": True}), {"specialKey": 12, "ctrl": True})
        self.assertEqual(self.check({"scroll": True, "dy": 1}), {"scroll": True, "dy": 1.0})

    def test_clamps_movement(self):
        self.assertEqual(self.check({"dx": 1e9})["dx"], self.server.INPUT_MOVE_LIMIT)
        self.assertEqual(self.check({"dy": -1e9})["dy"], -self.server.INPUT_MOVE_LIMIT)

    def test_rejects_bad_events(self):
        bad = [None, [], "a", {}, {"shift": True}, {"dx": "1"}, {"dx": True}, {"dx": float("nan")},
               {"key": ""}, {"key": 5}, {"key": "x" * 501}, {"specialKey": 0}, {"specialKey": 33},
               {"specialKey": True}, {"specialKey": 1.5}, {"singleclick": "yes"}]
        for event in bad:
            with self.assertRaises(self.server.Failure, msg=repr(event)):
                self.check(event)

    def test_send_while_link_is_down_says_not_sent(self):
        # The page keeps unsent keys and clicks and sends them once the agent is ready again.
        from types import SimpleNamespace

        class Probe(self.server.InputAgent):
            def start(self):
                self.proc, self.status = SimpleNamespace(poll=lambda: None, stdin=None), {"state": "starting"}

        agent = Probe()
        agent.status, agent.proc = {"state": "ready"}, SimpleNamespace(poll=lambda: 255)  # ssh has exited
        self.assertEqual(agent.send([{"key": "x"}]), {"state": "starting", "sent": False})

    def test_command_passes_client_quoted(self):
        os.environ["FRAME_CLIENT"] = "test-client-1"
        try:
            cmd = self.server.InputAgent().command()
        finally:
            del os.environ["FRAME_CLIENT"]
        self.assertTrue(cmd.startswith("python3 -u -c '"))
        self.assertIn(" test-client-1 ", cmd)

    def test_batch_limits(self):
        with self.assertRaises(self.server.Failure):
            self.server.remote_input({"events": "dx"})
        with self.assertRaises(self.server.Failure):
            self.server.remote_input({"events": [{"dx": 1}] * (self.server.INPUT_BATCH_LIMIT + 1)})


class FakeKdeConnect:
    """Just enough of kdeconnectd: pairs when asked and records remote-input packets."""

    def __init__(self, paired=False):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.paired, self.identity, self.received, self.pair_requests = paired, None, [], 0
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        conn, _ = self.listener.accept()
        line = b""
        while not line.endswith(b"\n"):
            line += conn.recv(1)
        self.identity = json.loads(line)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        tls = ctx.wrap_socket(conn)  # "Starting client ssl (but I'm the server TCP socket)"
        tls.sendall(b'{"id": 1, "type": "kdeconnect.mousepad.keyboardstate", "body": {"state": true}}\n')
        buf = b""
        while True:
            try:
                chunk = tls.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                p = json.loads(raw)
                if p["type"] == "kdeconnect.pair":
                    self.pair_requests += 1
                elif p["type"] == "kdeconnect.mousepad.request" and self.paired:
                    self.received.append(p["body"])


@unittest.skipIf(sys.platform == "win32", "the agent runs on the Frame (Linux)")
@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a certificate")
class AgentProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import frame_input_agent
        cls.agent = frame_input_agent
        cls.dir = tempfile.TemporaryDirectory()
        cls.cert, cls.key = Path(cls.dir.name, "cert.pem"), Path(cls.dir.name, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=framecontrol_test", "-keyout", str(cls.key), "-out", str(cls.cert)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def wait_for(self, check):
        for _ in range(100):
            if check():
                return
            time.sleep(0.02)
        self.fail("timed out")

    def test_pairs_then_forwards_events(self):
        fake = FakeKdeConnect()
        link = self.agent.Link("framecontrol_0123456789abcdef01234567", self.cert, self.key, port=fake.port)
        link.pair(lambda: fake.paired, lambda: setattr(fake, "paired", fake.pair_requests > 0), timeout=5)
        self.assertEqual(fake.pair_requests, 1)
        ident = fake.identity["body"]
        self.assertEqual(fake.identity["type"], "kdeconnect.identity")
        self.assertEqual(ident["protocolVersion"], 7)
        self.assertIn("kdeconnect.mousepad.request", ident["outgoingCapabilities"])
        link.read(0.5)
        self.assertTrue(link.keyboard)
        for body in self.agent.events(b'[{"dx": 4, "dy": -2}, {"key": "hi"}]'):
            link.send(body)
        self.wait_for(lambda: len(fake.received) == 2)
        self.assertEqual(fake.received, [{"dx": 4, "dy": -2}, {"key": "hi"}])

    def test_already_paired_sends_no_pair_request(self):
        # A pair request to a device that's already paired makes KDE Connect unpair it.
        fake = FakeKdeConnect(paired=True)
        link = self.agent.Link("framecontrol_0123456789abcdef01234567", self.cert, self.key, port=fake.port)
        link.pair(lambda: True, lambda: self.fail("accepted a pairing that wasn't needed"))
        link.send({"singleclick": True})
        self.wait_for(lambda: fake.received == [{"singleclick": True}])
        self.assertEqual(fake.pair_requests, 0)

    def test_client_args_are_folder_safe(self):
        old = sys.argv
        try:
            sys.argv = ["-c", "../../etc x", "Alex's Mac"]
            self.assertEqual(self.agent.client_args(), ("etcx", "Frame Control (Alex's Mac)"))
            sys.argv = ["-c"]
            self.assertEqual(self.agent.client_args(), ("default", "Frame Control"))
        finally:
            sys.argv = old

    def test_events_parsing(self):
        self.assertEqual(self.agent.events(b'{"dx": 1}'), [{"dx": 1}])
        self.assertEqual(self.agent.events(b'[{"dx": 1}, 5, {}]'), [{"dx": 1}])
        self.assertEqual(self.agent.events(b"not json"), [])


@unittest.skipUnless(shutil.which("node"), "needs node")
class PageQueue(unittest.TestCase):
    """The page's input queue (tests/page/pad_queue.mjs runs the real functions from index.html)."""

    def test_queue_rules(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "page" / "pad_queue.mjs")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
