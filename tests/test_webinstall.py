"""Install links from websites (ui/frame_webinstall.py, app/install-link.js). No network:
name lookups are stubbed and downloads come from a server on 127.0.0.1, which
the localhost-testing rule allows.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_webinstall as wi  # noqa: E402

E = wi.WebInstallError
PAYLOAD = b"not really an apk, but bytes are bytes\n" * 1000


def fake_dns(*ips):
    return lambda host, port, **_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]


class Urls(unittest.TestCase):
    def test_https_ok(self):
        self.assertEqual(wi.check_url("https://cdn.example.com/g/mygame.apk"), ("https", "cdn.example.com", 443, False))
        self.assertEqual(wi.file_name("https://cdn.example.com/g/my%20game.apk?sig=1"), "my game.apk")

    def test_http_only_for_localhost(self):
        with self.assertRaises(E):
            wi.check_url("http://cdn.example.com/mygame.apk")
        self.assertTrue(wi.check_url("http://localhost:8000/mygame.apk", allow_local=True)[3])
        self.assertTrue(wi.check_url("http://127.0.0.1:8000/mygame.apk", allow_local=True)[3])

    def test_localhost_only_when_the_link_starts_there(self):
        for url in ("http://localhost/x.apk", "https://127.0.0.1/x.apk"):
            with self.assertRaises(E):
                wi.check_url(url, allow_local=False)

    def test_other_schemes_rejected(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x.apk", "javascript:alert(1)", "//example.com/x.apk", ""):
            with self.assertRaises(E, msg=url):
                wi.check_url(url)

    def test_private_and_local_addresses_rejected(self):
        for host in ("10.0.0.5", "192.168.1.20", "172.16.3.4", "127.0.0.2", "169.254.169.254", "100.64.1.1",
                     "0.0.0.0", "[::1]", "[fe80::1]", "[fd00::1]", "[fec0::1]", "[::ffff:192.168.1.1]",
                     "[2002:c0a8:101::1]", "224.0.0.1"):
            with self.assertRaises(E, msg=host):
                wi.check_url(f"https://{host}/x.apk")
        wi.check_url("https://93.184.216.34/x.apk")

    def test_names_resolving_to_private_addresses_rejected(self):
        with mock.patch.object(wi, "_getaddrinfo", fake_dns("192.168.1.9")):
            with self.assertRaises(E):
                wi._resolve("sneaky.example.com", 443, False)
        # Every address counts, not just the first.
        with mock.patch.object(wi, "_getaddrinfo", fake_dns("93.184.216.34", "10.1.2.3")):
            with self.assertRaises(E):
                wi._resolve("mixed.example.com", 443, False)
        with mock.patch.object(wi, "_getaddrinfo", fake_dns("93.184.216.34")):
            self.assertEqual(wi._resolve("cdn.example.com", 443, False), "93.184.216.34")

    def test_credentials_rejected(self):
        for url in ("https://user:pw@example.com/x.apk", "https://user@example.com/x.apk", "https://:pw@example.com/x.apk"):
            with self.assertRaises(E, msg=url):
                wi.check_url(url)

    def test_directory_urls_rejected(self):
        for url in ("https://example.com/", "https://example.com", "https://example.com/games/",
                    "https://example.com/%2e%2e", "https://example.com/.hidden.apk", "https://example.com/a%2Fb.apk"):
            with self.assertRaises(E, msg=url):
                wi.file_name(url)

    def test_file_types(self):
        self.assertEqual(wi.file_kind("Game.APK"), "apk")
        self.assertEqual(wi.file_kind("game.zip"), "title")
        self.assertEqual(wi.file_kind("setup.exe"), "title")
        for name in ("game.sh", "game.tar.gz", "game"):
            with self.assertRaises(E, msg=name):
                wi.file_kind(name)


class Manifests(unittest.TestCase):
    FILE = {"url": "https://cdn.example.com/mygame-arm64.apk"}

    def test_both_schemas(self):
        for schema in ("framedrop.install/v1", "frame-control.install/v1"):
            m = wi.parse_manifest({"schema": schema, "name": "My Game", "files": [dict(self.FILE, sha256="AB" * 32)]})
            self.assertEqual(m["name"], "My Game")
            self.assertEqual(m["file"]["url"], self.FILE["url"])
            self.assertEqual(m["file"]["sha256"], "ab" * 32)

    def test_bad_schema(self):
        for schema in (None, "framedrop.install/v2", "something"):
            with self.assertRaises(E, msg=schema):
                wi.parse_manifest({"schema": schema, "files": [self.FILE]})

    def test_missing_or_bad_fields(self):
        base = {"schema": "framedrop.install/v1"}
        for obj in ([], base, dict(base, files=[]), dict(base, files="x"), dict(base, files=[{}]),
                    dict(base, files=[{"url": ""}]), dict(base, files=[dict(self.FILE, sha256="abc")]),
                    dict(base, files=[dict(self.FILE, size=-1)]), dict(base, name=5, files=[self.FILE])):
            with self.assertRaises(E, msg=obj):
                wi.parse_manifest(obj)

    def test_name_optional_and_cleaned(self):
        self.assertIsNone(wi.parse_manifest({"schema": "framedrop.install/v1", "files": [self.FILE]})["name"])
        m = wi.parse_manifest({"schema": "framedrop.install/v1", "name": " A\x1b[31mB\n ", "files": [self.FILE]})
        self.assertEqual(m["name"], "A[31mB")

    def test_multiple_files_refused_clearly(self):
        with self.assertRaisesRegex(E, "2 files"):
            wi.parse_manifest({"schema": "framedrop.install/v1", "files": [self.FILE, self.FILE]})


class Stub(BaseHTTPRequestHandler):
    routes = {}

    def log_message(self, *_):
        pass

    def do_HEAD(self):
        self.do_GET(body=False)

    def do_GET(self, body=True):
        route = self.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, headers, data = route
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)


class Downloads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        sha = hashlib.sha256(PAYLOAD).hexdigest()
        Stub.routes = {
            "/game.apk": (200, {}, PAYLOAD),
            "/game.zip": (200, {}, PAYLOAD),
            "/redirect.apk": (302, {"Location": "/game.apk"}, b""),
            "/to-lan.apk": (302, {"Location": "https://192.168.1.5/game.apk"}, b""),
            "/to-http.apk": (302, {"Location": "http://cdn.example.com/game.apk"}, b""),
            "/loop.apk": (302, {"Location": "/loop.apk"}, b""),
            "/manifest.json": (200, {}, json.dumps({"schema": "framedrop.install/v1", "name": "Stub Game",
                                                    "files": [{"url": f"{cls.base}/game.apk", "sha256": sha}]}).encode()),
            "/bad-sha.json": (200, {}, json.dumps({"schema": "frame-control.install/v1", "name": "Bad",
                                                   "files": [{"url": f"{cls.base}/game.apk", "sha256": "0" * 64}]}).encode()),
            "/huge.json": (200, {}, b"{" + b" " * (wi.MAX_MANIFEST + 10) + b"}"),
            "/notjson.json": (200, {}, b"<html>"),
            "/short.apk": (200, {"Content-Length": str(len(PAYLOAD) + 100)}, PAYLOAD),
        }

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {wi.LOCAL_LINKS_ENV: "1"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_localhost_links_need_the_developer_switch(self):
        # Without it, a website's link can't make the app fetch from local services.
        with mock.patch.dict(os.environ, {wi.LOCAL_LINKS_ENV: ""}):
            for kw in ({"manifest": f"{self.base}/manifest.json"}, {"url": f"{self.base}/game.apk"}):
                with self.assertRaisesRegex(wi.WebInstallError, wi.LOCAL_LINKS_ENV):
                    wi.plan(**kw)

    def test_manifest_round_trip(self):
        p = wi.plan(manifest=f"{self.base}/manifest.json")
        self.assertEqual((p["name"], p["file"], p["kind"], p["host"], p["size"]),
                         ("Stub Game", "game.apk", "apk", "127.0.0.1", len(PAYLOAD)))
        seen = []
        path = wi.download(p, self.tmp, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(Path(path).read_bytes(), PAYLOAD)
        self.assertEqual(seen[-1], (len(PAYLOAD), len(PAYLOAD)))
        self.assertEqual(os.listdir(self.tmp), ["game.apk"])

    def test_direct_url_and_redirect(self):
        p = wi.plan(url=f"{self.base}/redirect.apk")
        self.assertEqual((p["name"], p["file"]), ("redirect.apk", "redirect.apk"))
        self.assertEqual(Path(wi.download(p, self.tmp)).read_bytes(), PAYLOAD)

    def test_redirects_checked_again(self):
        for path in ("/to-lan.apk", "/to-http.apk", "/loop.apk"):
            with self.assertRaises(E, msg=path):
                wi._open(f"{self.base}{path}", allow_local=True)

    def test_sha256_mismatch_leaves_nothing(self):
        p = wi.plan(manifest=f"{self.base}/bad-sha.json")
        with self.assertRaisesRegex(E, "sha256"):
            wi.download(p, self.tmp)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_size_cap(self):
        with mock.patch.object(wi, "MAX_FILE", 1000):
            with self.assertRaisesRegex(E, "limit"):
                wi.plan(url=f"{self.base}/game.apk")
            p = {"url": f"{self.base}/game.apk", "file": "game.apk", "allowLocal": True, "size": None, "sha256": None}
            with self.assertRaisesRegex(E, "limit"):
                wi.download(p, self.tmp)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_bad_manifests(self):
        for path in ("/huge.json", "/notjson.json", "/missing.json"):
            with self.assertRaises(E, msg=path):
                wi.plan(manifest=f"{self.base}{path}")

    def test_cut_off_download(self):
        p = {"url": f"{self.base}/short.apk", "file": "short.apk", "allowLocal": True, "size": None, "sha256": None}
        with self.assertRaises(E):
            wi.download(p, self.tmp)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_aborted_connection_never_connects(self):
        port = self.httpd.server_address[1]
        for cls in (wi._HTTPConnection, wi._HTTPSConnection):
            conn = cls("127.0.0.1", "127.0.0.1", port, 5)
            wi.abort(conn)  # before connect, e.g. cancelled while looking up the name
            with self.assertRaisesRegex(OSError, "aborted"):
                conn.connect()

    def test_cancel(self):
        p = wi.plan(url=f"{self.base}/game.apk")
        with self.assertRaises(wi.Cancelled):
            wi.download(p, self.tmp, cancelled=lambda: True)
        self.assertEqual(os.listdir(self.tmp), [])


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, name):
        path = os.path.join(self.tmp, name)
        Path(path).write_bytes(PAYLOAD)
        return path

    def test_apk_goes_to_the_android_installer(self):
        import frame_android
        with mock.patch.object(frame_android, "install", return_value={"label": "Stub"}) as install:
            res = wi.dispatch(self.file("game.apk"), name="Ignored", source="https://example.com/game.apk")
        install.assert_called_once_with(os.path.join(self.tmp, "game.apk"), source="https://example.com/game.apk")
        self.assertEqual(res["kind"], "apk")
        self.assertIn("Stub", res["message"])

    def test_titles_without_the_titles_module(self):
        with mock.patch.dict(sys.modules, {"frame_titles": None}):
            with self.assertRaisesRegex(E, "newer Frame Control"):
                wi.dispatch(self.file("game.zip"))

    def test_titles_go_to_frame_titles(self):
        fake = mock.Mock()
        fake.install.return_value = {"message": "Installed Stub"}
        with mock.patch.dict(sys.modules, {"frame_titles": fake}):
            res = wi.dispatch(self.file("game.exe"), name="Stub", exe=None)
        fake.install.assert_called_once_with(os.path.join(self.tmp, "game.exe"), name="Stub", exe=None, progress=None)
        self.assertEqual(res["message"], "Installed Stub")

    def test_other_files_refused(self):
        with self.assertRaises(E):
            wi.dispatch(self.file("game.sh"))


class ServerJobs(unittest.TestCase):
    """The server's install worker (ui/server.py), with download and dispatch stubbed."""

    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(os.environ, {"FRAME_ALIAS": "frame-control-test.invalid"}):
            import server
        cls.server = server

    def run_job(self, download=None, mkdtemp_error=None):
        s = self.server
        job = {"phase": "download", "done": 0, "total": None, "detail": "", "message": None, "error": None, "cancel": False}
        plan = {"name": "Stub", "exe": None, "url": "https://example.com/stub.apk"}
        with mock.patch.object(s, "ensure_master"), \
                mock.patch.object(s.frame_webinstall, "download", side_effect=lambda *a, **k: download(job, a[1])), \
                mock.patch.object(s.tempfile, "mkdtemp", side_effect=mkdtemp_error or tempfile.mkdtemp), \
                mock.patch.object(s.frame_webinstall, "dispatch", return_value={"message": "ok"}) as dispatch:
            s._webinstall_run(plan, job)
        return job, dispatch

    def test_cancel_after_the_last_chunk_still_stops_the_install(self):
        def download(job, tmp):
            job["cancel"] = True  # arrives after the downloader's last check
            return os.path.join(tmp, "stub.apk")
        job, dispatch = self.run_job(download)
        dispatch.assert_not_called()
        self.assertEqual(job["phase"], "error")

    def test_finished_download_is_dispatched(self):
        job, dispatch = self.run_job(lambda job, tmp: os.path.join(tmp, "stub.apk"))
        dispatch.assert_called_once()
        self.assertEqual((job["phase"], job["message"]), ("done", "ok"))

    def stall_then_shutdown(self, scheme, reply):
        if os.name == "nt":
            # shutdown() from another thread doesn't wake a blocked recv on Windows, and
            # closing the handle under a TLS read isn't safe; see web-install.md.
            self.skipTest("Windows: a stalled download is only dropped when the app stops the server")
        """Start a download from a server that stalls after sending reply; shutdown must stop it quickly."""
        stall = socket.socket()
        stall.bind(("127.0.0.1", 0))
        stall.listen(1)
        port = stall.getsockname()[1]
        stalled = threading.Event()

        def serve():
            c, _ = stall.accept()
            if reply is not None:
                c.recv(65536)
                c.sendall(reply)
            stalled.set()
            time.sleep(20)  # longer than the test may take; TIMEOUT is 30 s
            c.close()
        threading.Thread(target=serve, daemon=True).start()
        s = self.server
        pid = "shutdown-test"
        s._web_plans[pid] = {"name": "Stub", "exe": None, "url": f"{scheme}://127.0.0.1:{port}/stub.apk",
                             "file": "stub.apk", "allowLocal": True, "size": None, "sha256": None,
                             "sizeFromManifest": False}
        try:
            s.webinstall_start({"id": pid})
            job = s._web_jobs[pid]
            self.assertTrue(stalled.wait(5))
            time.sleep(0.1)  # let the client block
            t0 = time.time()
            s.webinstall_shutdown()
            self.assertLess(time.time() - t0, 3)
            self.assertEqual(s._web_workers, set())
            self.assertEqual((job["phase"], job["error"]), ("error", "download cancelled"))
            s._web_plans["late"] = {"size": None}
            with self.assertRaises(s.Failure) as caught:  # nothing new starts once quitting
                s.webinstall_start({"id": "late"})
            self.assertEqual(caught.exception.status, 503)
        finally:
            s._web_closing = False
            s._web_jobs.clear()
            s._web_plans.clear()
            stall.close()

    def test_shutdown_interrupts_a_stalled_body(self):
        self.stall_then_shutdown("http", b"HTTP/1.0 200 OK\r\nContent-Length: 1000000\r\n\r\npartial")

    def test_shutdown_interrupts_stalled_headers(self):
        self.stall_then_shutdown("http", b"HTTP/1.1 200 OK\r\n")

    def test_shutdown_interrupts_a_stalled_tls_handshake(self):
        self.stall_then_shutdown("https", None)

    def test_dead_servers_leftovers_swept(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        # Downloads and title staging (unzipped titles) are both swept.
        for prefix in (self.server.WEB_TMP_PREFIX, self.server.frame_titles.TMP_PREFIX):
            gone = tempfile.mkdtemp(prefix=f"{prefix}{dead.pid}-")
            live = tempfile.mkdtemp(prefix=f"{prefix}{os.getpid()}-")
            try:
                self.server.sweep_tmp()
                self.assertFalse(os.path.exists(gone), prefix)
                self.assertTrue(os.path.exists(live), prefix)
            finally:
                shutil.rmtree(gone, ignore_errors=True)
                shutil.rmtree(live, ignore_errors=True)

    def test_temp_dir_failure_ends_the_job(self):
        job, dispatch = self.run_job(mkdtemp_error=OSError("disk full"))
        dispatch.assert_not_called()
        self.assertEqual(job["phase"], "error")
        self.assertIn("disk full", job["error"])


@unittest.skipUnless(shutil.which("node"), "needs node")
class LinkParsing(unittest.TestCase):
    def parse(self, links):
        script = ("const { parseInstallLink, linkFromArgv } = require(process.argv[1]);"
                  "const links = JSON.parse(process.argv[2]);"
                  "console.log(JSON.stringify({ parsed: links.map(parseInstallLink),"
                  " argv: linkFromArgv(['/x/frame-control', '--flag', links[0]]) }));")
        out = subprocess.run(["node", "-e", script, str(ROOT / "app" / "install-link.js"), json.dumps(links)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_links(self):
        m = "https://example.com/m.json"
        good = ["frame-control://install?manifest=" + "https%3A%2F%2Fexample.com%2Fm.json",
                "frame-control://install/?url=https%3A%2F%2Fcdn.example.com%2Fg.apk",
                "FRAME-CONTROL://install?manifest=http%3A%2F%2Flocalhost%3A8000%2Fm.json"]
        bad = ["framedrop://install?manifest=" + m, "frame-control://uninstall?manifest=" + m,
               "frame-control://install?manifest=" + m + "&url=" + m, "frame-control://install?manifest=a&manifest=b",
               "frame-control://install?manifest=file%3A%2F%2F%2Fetc%2Fpasswd", "frame-control://install?other=" + m,
               "frame-control://install?url=https%3A%2F%2Fu%3Ap%40example.com%2Fg.apk", "frame-control://install",
               "frame-control://install/sub?url=" + m, "https://example.com"]
        res = self.parse(good + bad)
        self.assertEqual(res["parsed"][0], {"kind": "manifest", "target": m})
        self.assertEqual(res["parsed"][1], {"kind": "url", "target": "https://cdn.example.com/g.apk"})
        self.assertEqual(res["parsed"][2]["kind"], "manifest")
        self.assertEqual(res["parsed"][len(good):], [None] * len(bad))
        self.assertEqual(res["argv"], good[0])


if __name__ == "__main__":
    unittest.main()
