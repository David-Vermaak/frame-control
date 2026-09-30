"""Remote desktop to the Frame (frame_host.open_rdp) on each computer, with the client
launch stubbed and a real socket standing in for the Frame's xrdp. Also the server
staying quiet when the page goes away mid-reply, which on Windows is
ConnectionAbortedError (WinError 10053).

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import email.message
import io
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_host  # noqa: E402
import server  # noqa: E402


def platform(name):
    """Patches frame_host to behave as on `name` ("mac", "windows" or "linux")."""
    return mock.patch.multiple(frame_host, MAC=name == "mac", WINDOWS=name == "windows",
                               LINUX=name == "linux")


class OpenRdp(unittest.TestCase):
    def setUp(self):
        self.xrdp = socket.socket()
        self.xrdp.bind(("127.0.0.1", 0))
        self.xrdp.listen(4)
        self.addCleanup(self.xrdp.close)
        port = mock.patch.object(frame_host, "RDP_PORT", self.xrdp.getsockname()[1])
        port.start()
        self.addCleanup(port.stop)
        self.spawned = []
        spawn = mock.patch.object(frame_host, "_spawn", self.spawned.append)
        spawn.start()
        self.addCleanup(spawn.stop)
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.cache = Path(cache.name)
        where = mock.patch.object(frame_host, "cache_dir", lambda *p: self.cache.joinpath(*p))
        where.start()
        self.addCleanup(where.stop)

    def test_windows_signs_in_as_steamos(self):
        # The report: mstsc /v:HOST alone offers the Windows account, which xrdp rejects.
        with platform("windows"):
            message = frame_host.open_rdp("frame", "127.0.0.1")
        self.assertEqual(len(self.spawned), 1)
        argv = self.spawned[0]
        self.assertEqual(argv[0], "mstsc.exe")
        self.assertNotIn("/v:127.0.0.1", argv)
        rdp = Path(argv[1])
        self.assertEqual(rdp.suffix, ".rdp")
        data = rdp.read_bytes()  # CRLF lines, as mstsc writes them, however this OS ends lines
        self.assertNotIn(b"\r\r", data)
        lines = data.decode("utf-8").split("\r\n")
        self.assertIn("full address:s:127.0.0.1", lines)
        self.assertIn("username:s:steamos", lines)
        self.assertIn("steamos", message)
        self.assertIn("Developer Mode password", message)
        self.assertIn("certificate", message)

    def test_nothing_listening_says_why_and_opens_nothing(self):
        self.xrdp.close()
        for name in ("windows", "mac", "linux"):
            with self.subTest(name), platform(name), self.assertRaises(frame_host.HostError) as cm:
                frame_host.open_rdp("frame", "127.0.0.1")
            self.assertIn("Developer Mode", str(cm.exception))
            self.assertIn(f"port {frame_host.RDP_PORT}", str(cm.exception))
        self.assertEqual(self.spawned, [])

    def test_address_cant_add_lines_to_the_file(self):
        with platform("windows"), self.assertRaises(frame_host.HostError):
            frame_host.rdp_file("frame\r\nusername:s:root")
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_linux_clients_get_the_user(self):
        with platform("linux"), mock.patch.object(frame_host, "which",
                                                  lambda n, *e: "/usr/bin/xfreerdp" if n == "xfreerdp" else None):
            message = frame_host.open_rdp("frame", "127.0.0.1")
        self.assertEqual(self.spawned, [["xfreerdp", "/v:127.0.0.1", "/u:steamos", "/dynamic-resolution"]])
        self.assertIn("steamos", message)


class PageGoneAway(unittest.TestCase):
    """The report's server log: the page closed while index.html was being sent, and the
    server logged it as a 500, tried to answer anyway, and filed an error diagnostic."""

    def handler(self, path="/"):
        h = server.Handler.__new__(server.Handler)
        h.command, h.path, h.request_version = "GET", path, "HTTP/1.1"
        h.requestline, h.client_address = f"GET {path} HTTP/1.1", ("127.0.0.1", 1)
        h.headers = email.message.Message()
        h.headers["Host"] = "127.0.0.1:1"
        h.wfile = mock.Mock(write=mock.Mock(side_effect=ConnectionAbortedError(10053, "aborted")))
        h.close_connection = True
        return h

    def test_not_a_server_error(self):
        h = self.handler()
        with mock.patch.object(server.frame_telemetry, "diagnostic") as diagnostic, \
                mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(server.ClientGone):
            h.do_GET()
        diagnostic.assert_not_called()
        self.assertEqual(h.wfile.write.call_count, 1)  # no second, 500 reply

    def test_server_logs_nothing(self):
        srv = server.LoopbackServer.__new__(server.LoopbackServer)
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            try:
                raise server.ClientGone()
            except server.ClientGone:
                srv.handle_error(None, ("127.0.0.1", 1))
            self.assertEqual(err.getvalue(), "")
            try:
                raise RuntimeError("real")
            except RuntimeError:
                srv.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("RuntimeError: real", err.getvalue())


if __name__ == "__main__":
    unittest.main()
