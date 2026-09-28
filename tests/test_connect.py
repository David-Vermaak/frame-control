"""Setup-script checks that need no headset: devkit pairing against a stub of Valve's
steamos-devkit-service, the ~/.ssh/config block, and the mDNS output parsers.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_connect as fc  # noqa: E402

PUB = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC+/x= frame-control@old\n"


class StubDevkit(BaseHTTPRequestHandler):
    """Answers like steamos-devkit-service; `reply` picks the /register outcome."""
    reply = (200, b"Registered\n")
    replies = []  # if set, each /register takes the next one instead of `reply`
    properties = {"txtvers": 1, "login": "steamos", "settings": "{}", "devkit1": ["devkit-1"]}
    bodies = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/properties.json":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.properties).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        StubDevkit.bodies.append((self.path, self.headers["Content-Type"], body))
        code, text = StubDevkit.replies.pop(0) if StubDevkit.replies else self.reply
        self.send_response(code)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(text)


class DevkitPairing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubDevkit)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        StubDevkit.reply = (200, b"Registered\n")
        StubDevkit.bodies = []
        StubDevkit.replies = []
        self.said = []
        self._say, fc.say = fc.say, self.said.append

    def tearDown(self):
        fc.say = self._say

    def test_register_body(self):
        body = fc.register_body(PUB, "frame-control@mac")
        self.assertEqual(body, "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC+/x= frame-control@mac "
                               "900b919520e4cf601998a71eec318fec\n")
        # approve-ssh-key shows split(' ')[2] as the key name.
        self.assertEqual(body.split(" ")[2], "frame-control@mac")
        with self.assertRaises(ValueError):
            fc.register_body("ssh-ed25519 AAAAC3Nz x", "c")

    def test_key_comment_is_one_word(self):
        self.assertEqual(fc.key_comment("Alex's MacBook Pro.local"), "frame-control@Alex-s-MacBook-Pro")
        self.assertEqual(fc.key_comment(""), "frame-control@computer")
        self.assertNotIn(" ", fc.key_comment(" a b\nc "))

    def test_parse_login(self):
        self.assertEqual(fc.parse_login(b'{"login": "steamos", "txtvers": 1}'), "steamos")
        for raw in (b'{"txtvers": 1}', b'{"login": "root"}', b'{"login": "x\\nHost *"}', b'{"login": 5}'):
            self.assertIsNone(fc.parse_login(raw), raw)
        for raw in (b"not json", b"[1]"):
            with self.assertRaises(ValueError):
                fc.parse_login(raw)

    def test_devkit_error(self):
        self.assertEqual(fc.devkit_error(403, b'{"error": "Steam is not running"}\n'), "Steam is not running")
        self.assertEqual(fc.devkit_error(500, b"install-ssh-key:\nboom"), "install-ssh-key:\nboom")
        self.assertEqual(fc.devkit_error(403, b""), "HTTP 403")

    def test_pair_ok(self):
        logins = []
        reason = fc.devkit_pair("127.0.0.1", PUB, "frame-control@test", self.port, logins.append)
        self.assertIsNone(reason)
        self.assertEqual(logins, ["steamos"])
        path, ctype, body = StubDevkit.bodies[0]
        self.assertEqual((path, ctype), ("/register", "text/plain"))
        self.assertEqual(body.decode(), fc.register_body(PUB, "frame-control@test"))
        self.assertTrue(any("Pair new host" in s for s in self.said))

    NOT_PAIRING = (403, b'{"error": "devkit approve-ssh-key: please put the Steam client in pairing mode: '
                        b'Settings -> Developer -> Pair new host"}')

    def test_pair_waits_for_pairing_mode(self):
        # The headset refuses until Steam is on "Pair new host", then prompts.
        StubDevkit.replies = [self.NOT_PAIRING, self.NOT_PAIRING, (200, b"Registered\n")]
        sleep, fc.time.sleep = fc.time.sleep, lambda s: None
        try:
            reason = fc.devkit_pair("127.0.0.1", PUB, "c", self.port)
        finally:
            fc.time.sleep = sleep
        self.assertIsNone(reason)
        self.assertEqual(len(StubDevkit.bodies), 3)

    def test_pair_gives_up_without_pairing_mode(self):
        StubDevkit.reply = self.NOT_PAIRING
        wait, fc.PAIRING_MODE_WAIT = fc.PAIRING_MODE_WAIT, 0
        try:
            reason = fc.devkit_pair("127.0.0.1", PUB, "c", self.port)
        finally:
            fc.PAIRING_MODE_WAIT = wait
        self.assertIn("pairing mode", reason)
        self.assertEqual(len(StubDevkit.bodies), 1)

    def test_pair_refused_falls_back(self):
        StubDevkit.reply = (403, b'{"error": "timeout - Steam did not respond to the pairing request"}')
        logins = []
        reason = fc.devkit_pair("127.0.0.1", PUB, "c", self.port, logins.append)
        self.assertIn("timeout - Steam did not respond", reason)
        # The login is still reported, so the password fallback uses the right user.
        self.assertEqual(logins, ["steamos"])

    def test_pair_without_service_falls_back(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        logins = []
        reason = fc.devkit_pair("127.0.0.1", PUB, "c", closed, logins.append)
        self.assertIn("not reachable", reason)
        self.assertEqual((logins, StubDevkit.bodies), ([], []))

    def test_pair_times_out(self):
        # Accepts the connection but never answers, like a prompt nobody taps.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            ok, msg = fc.register("127.0.0.1", "x", s.getsockname()[1], timeout=0.5)
        self.assertFalse(ok)
        self.assertIn("no answer", msg)


class ConfigBlock(unittest.TestCase):
    def test_both_keys(self):
        block = fc.config_block("frame.local", 22, "steamos")
        self.assertEqual(block[0], fc.BEGIN)
        self.assertEqual(block[-1], fc.END)
        self.assertIn("  User steamos", block)
        self.assertNotIn("  Port 22", block)
        files = [line for line in block if line.startswith("  IdentityFile")]
        self.assertEqual(files, ["  IdentityFile ~/.ssh/id_ed25519_frame", "  IdentityFile ~/.ssh/id_rsa_frame_devkit"])
        self.assertIn("  IdentitiesOnly yes", block)
        self.assertEqual(block[-2], "Host *")
        self.assertIn("  Port 2222", fc.config_block("10.0.0.5", 2222))

    def test_write_config_replaces_block(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            saved = fc.SSH_DIR, fc.CONFIG
            fc.SSH_DIR, fc.CONFIG = Path(d), Path(d) / "config"
            try:
                fc.CONFIG.write_text("Host other\n  User me\n", encoding="utf-8")
                fc.write_config("frame.local")
                self.assertEqual(fc.configured_user(), "steamos")
                fc.write_config("10.0.0.5", 22, "deck")
                text = fc.CONFIG.read_text(encoding="utf-8")
                self.assertEqual(fc.configured_user(), "deck")  # not "me" from Host other
            finally:
                fc.SSH_DIR, fc.CONFIG = saved
        self.assertEqual(text.count(fc.BEGIN), 1)
        self.assertIn("HostName 10.0.0.5", text)
        self.assertIn("User deck", text)
        self.assertNotIn("frame.local", text)
        self.assertTrue(text.endswith("Host other\n  User me\n"))


class MdnsParsers(unittest.TestCase):
    def test_dns_sd(self):
        browse = ("Browsing for _steamos-devkit._tcp\n"
                  "Timestamp     A/R    Flags  if Domain               Service Type         Instance Name\n"
                  "19:34:35.419  Add        3  15 local.               _steamos-devkit._tcp. frame\n"
                  "19:34:35.611  Add        2   1 local.               _steamos-devkit._tcp. frame\n"
                  "19:34:35.700  Add        2  15 local.               _steamos-devkit._tcp. My Frame\n"
                  "19:34:36.000  Rmv        0  15 local.               _steamos-devkit._tcp. gone\n")
        self.assertEqual(fc.parse_dns_sd_browse(browse), ["frame", "My Frame"])
        resolve = ("Lookup frame._steamos-devkit._tcp.local.\n"
                   "19:34:44.601  frame._steamos-devkit._tcp.local. can be reached at frame.local.:32000 (interface 15)\n")
        self.assertEqual(fc.parse_dns_sd_resolve(resolve), "frame.local")
        self.assertIsNone(fc.parse_dns_sd_resolve("Lookup frame\n"))

    def test_avahi(self):
        out = ('+;wlan0;IPv4;frame;_steamos-devkit._tcp;local\n'
               '=;wlan0;IPv6;frame;_steamos-devkit._tcp;local;frame.local;fe80::1;32000;"login=steamos"\n'
               '=;wlan0;IPv4;frame;_steamos-devkit._tcp;local;frame.local;192.168.1.50;32000;"login=steamos"\n')
        self.assertEqual(fc.parse_avahi(out), ["frame.local", "192.168.1.50"])


if __name__ == "__main__":
    unittest.main()
