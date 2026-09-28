"""Anonymous analytics (ui/frame_telemetry.py): what's collected at each level,
what's scrubbed, and that nothing is sent without a key, the notice, or consent.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_compat_db as db  # noqa: E402
import frame_report as fr  # noqa: E402
import frame_telemetry as tm  # noqa: E402


class Base(unittest.TestCase):
    """A packaged build with a key, its state in a temp folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        for name, value in (("STATE", state), ("SETTINGS", state / "settings.json"),
                            ("OUTBOX", state / "outbox.jsonl"), ("SENT", state / "sent.jsonl")):
            p = mock.patch.object(tm, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"FRAME_CONTROL_POSTHOG_KEY": "phc_test", "FRAME_CONTROL_PACKAGED": "1",
                                           "FRAME_CONTROL_POSTHOG_HOST": "http://127.0.0.1:9",
                                           "FRAME_CONTROL_VERSION": "9.9.9"})
        env.start()
        self.addCleanup(env.stop)
        for k in ("DO_NOT_TRACK", "FRAME_CONTROL_TELEMETRY"):
            os.environ.pop(k, None)
        tm._seen_errors.clear()

    def queued(self):
        return tm._read_lines(tm.OUTBOX)


class Gates(Base):
    def test_blocked_without_key_or_in_a_checkout_or_by_do_not_track(self):
        self.assertIsNone(tm.blocked())
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_POSTHOG_KEY": ""}), \
                mock.patch.object(tm, "HERE", Path(tempfile.gettempdir()) / "no-config-here"):
            self.assertIn("key", tm.blocked())
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_PACKAGED": ""}):
            self.assertIn("source checkout", tm.blocked())
        with mock.patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            self.assertIn("DO_NOT_TRACK", tm.blocked())
            self.assertFalse(tm.capture("app_opened"))
        self.assertFalse(tm.OUTBOX.exists())

    def test_usage_is_on_by_default_the_others_are_opt_in(self):
        self.assertTrue(tm.capture("app_opened"))
        self.assertFalse(tm.capture("compat_report", {}, level="compat"))
        self.assertFalse(tm.capture("$exception", {}, level="diagnostics"))
        self.assertEqual([e["event"] for e in self.queued()], ["app_opened"])

    def test_events_are_anonymous(self):
        tm.capture("app_opened")
        e = self.queued()[0]
        self.assertEqual(e["distinct_id"], tm.settings()["id"])
        self.assertIs(e["properties"]["$process_person_profile"], False)
        self.assertIs(e["properties"]["$geoip_disable"], True)
        self.assertEqual(e["properties"]["app_version"], "9.9.9")

    def test_turning_a_level_off_drops_its_unsent_events(self):
        tm.update_settings({"diagnostics": True})
        tm.capture("app_opened")
        tm.diagnostic("somewhere", RuntimeError("boom"))
        self.assertEqual(len(self.queued()), 2)
        tm.update_settings({"diagnostics": False})
        self.assertEqual([e["event"] for e in self.queued()], ["app_opened"])
        tm.update_settings({"usage": False})
        self.assertEqual(self.queued(), [])
        self.assertFalse(tm.capture("app_opened"))

    def test_the_same_error_is_sent_once_in_a_while(self):
        tm.update_settings({"diagnostics": True})
        for _ in range(3):
            tm.diagnostic("POST /api/android install", RuntimeError("boom"))
        self.assertEqual(len(self.queued()), 1)

    def test_page_events_are_checked(self):
        self.assertTrue(tm.page_event({"event": "tab_viewed", "properties": {"tab": "android", "extra": "x"}})["queued"])
        self.assertEqual(self.queued()[0]["properties"].get("extra"), None)
        with self.assertRaises(ValueError):
            tm.page_event({"event": "anything_else"})
        with self.assertRaises(ValueError):
            tm.page_event({"event": "tab_viewed", "properties": {"tab": "/Users/me/secret"}})


class Lifecycle(Base):
    def test_install_update_and_one_open_a_day(self):
        tm.app_started()
        tm.app_started()
        self.assertEqual([e["event"] for e in self.queued()], ["app_installed", "app_opened"])
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_VERSION": "10.0.0"}):
            tm.app_started()
        e = self.queued()[-1]
        self.assertEqual((e["event"], e["properties"]["from_version"]), ("app_updated", "9.9.9"))

    def test_frame_build_once(self):
        tm.frame_seen("20260922.1", "3.8")
        tm.frame_seen("20260922.1", "3.8")
        self.assertEqual(len(self.queued()), 1)


class Sending(Base):
    def serve(self, status=200):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'{"status": 1}')

            def log_message(self, *a):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        os.environ["FRAME_CONTROL_POSTHOG_HOST"] = f"http://127.0.0.1:{httpd.server_port}"
        return got

    def test_nothing_is_sent_before_the_notice_was_shown(self):
        got = self.serve()
        tm.capture("app_opened")
        self.assertEqual(tm.flush(), 0)
        self.assertEqual(got, [])
        tm.update_settings({"noticeShown": True})
        self.assertEqual(tm.flush(), 1)
        path, body = got[0]
        self.assertEqual((path, body["api_key"], body["batch"][0]["event"]), ("/batch/", "phc_test", "app_opened"))
        self.assertEqual(self.queued(), [])
        self.assertEqual([e["event"] for e in tm.state()["sent"]], ["app_opened"])

    def test_a_failed_send_keeps_the_events(self):
        self.serve(status=500)
        tm.update_settings({"noticeShown": True})
        tm.capture("app_opened")
        self.assertEqual(tm.flush(), 0)
        self.assertEqual(len(self.queued()), 1)


class Scrub(unittest.TestCase):
    def test_personal_details_are_removed(self):
        home = str(Path.home())
        text = (f"open {home}/Downloads/My Game.apk failed; ssh alex@192.168.1.20 (frame.local) "
                "key ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIM steam 76561198000000000 mac 3c:22:fb:12:34:56 "
                "url https://example.com/private/path?token=abc phc_abcdefghijklmnopqrstu C:\\Users\\Bob\\x "
                "/home/carol/y")
        out = tm.scrub(text)
        for leaked in (home, "192.168.1.20", "frame.local", "AAAAC3Nza", "76561198000000000", "3c:22:fb",
                       "private/path", "phc_abcdefghijklmnopqrstu", "Bob", "carol", "alex@"):
            self.assertNotIn(leaked, out)
        self.assertIn("https://example.com/…", out)
        self.assertIn("~/Downloads", out)

    def test_categories(self):
        self.assertEqual(tm.categorize("adb: failed to install: INSTALL_FAILED_NO_MATCHING_ABIS: x"),
                         ("android_installer", "INSTALL_FAILED_NO_MATCHING_ABIS"))
        self.assertEqual(tm.categorize("X has no arm64-v8a build (armeabi-v7a)")[0], "apk_wrong_abi")
        self.assertEqual(tm.categorize("ssh: connect to host 10.0.0.2 port 22: Connection refused")[0],
                         "frame_unreachable")
        self.assertEqual(tm.categorize("something new")[0], "other")


class Compat(Base):
    def test_reports_are_shared_only_after_opting_in_without_file_names(self):
        r = {"id": "r1", "package": "org.example", "version": "1.0", "rating": "works", "via": "user",
             "notes": f"from {Path.home()}/x", "source": "MyPrivateBuild.apk", "date": "2026-09-28T10:00:00"}
        self.assertFalse(tm.compat_report(r))
        tm.update_settings({"compat": True})
        self.assertTrue(tm.compat_report(r))
        p = self.queued()[-1]["properties"]
        self.assertEqual((p["package"], p["rating"], p["id"]), ("org.example", "works", "r1"))
        self.assertNotIn("source", p)
        self.assertNotIn(str(Path.home()), p["notes"])
        r2 = dict(r, id="r2", source="https://f-droid.org/repo/org.example_1.apk")
        tm.compat_report(r2)
        self.assertEqual(self.queued()[-1]["properties"]["source"], "https://f-droid.org/…")

    def test_opting_in_shares_earlier_local_reports(self):
        with mock.patch.object(db, "shared", return_value=False), \
                mock.patch.object(db, "_outbox", return_value=[{"id": "old1", "package": "org.a", "rating": "works",
                                                                "date": "2026-09-01T00:00:00"}]):
            tm.update_settings({"compat": True})
            tm.update_settings({"compat": True})  # already sent: not again
        self.assertEqual([e["properties"]["id"] for e in self.queued() if e["event"] == "compat_report"], ["old1"])


class ApkInstallReports(unittest.TestCase):
    """server.apk_installed: an APK that won't install is reported; connection trouble isn't."""

    def setUp(self):
        import server
        self.server = server
        for target, name in ((server.frame_catalog, "add_report"), (server.frame_telemetry, "install_finished")):
            p = mock.patch.object(target, name)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)

    def test_wrong_abi_is_an_install_failed_report(self):
        info = {"package": "org.x", "version": "2.0", "label": "X"}
        self.server.apk_installed(info, None, self.server.frame_android.FrameError(
            "X has no arm64-v8a build (armeabi-v7a); Lepton is 64-bit ARM only"), 3.0)
        args, kw = self.add_report.call_args
        self.assertEqual((args[0], args[1], kw["result"], kw["via"]), ("org.x", "2.0", "install_failed", "install"))
        self.assertIs(self.install_finished.call_args[0][1], False)

    def test_connection_trouble_is_not_reported(self):
        self.server.apk_installed({"package": "org.x", "version": "2.0"}, None,
                                  self.server.frame_android.FrameError("timed out talking to frame"), 3.0)
        self.add_report.assert_not_called()

    def test_private_package_names_stay_here(self):
        with mock.patch.dict(self.server.frame_catalog._cache, {"by_pkg": {"org.public": {}}}):
            self.server.apk_installed({"package": "com.private.thing", "version": "1"}, {"package": "com.private.thing"},
                                      None, 2.0)
            self.assertIsNone(self.install_finished.call_args[1]["package"])
            self.server.apk_installed({"package": "org.public", "version": "1"}, {"package": "org.public"}, None, 2.0)
            self.assertEqual(self.install_finished.call_args[1]["package"], "org.public")


class CommunitySync(unittest.TestCase):
    def ev(self, i, who="a", day="2026-09-28", **kw):
        return ({"id": f"id{i}", "package": "org.x", "rating": "works", "date": f"{day}T00:00:00",
                 "via": "probe", **kw}, who, f"{day} 10:00:00")

    def test_rows_are_validated_marked_and_capped_per_reporter(self):
        events = [self.ev(i) for i in range(5)] + [self.ev(9, who="b", rating="nonsense"), self.ev(10, who="b")]
        rows, skipped = db.community_rows(events, {}, cap=3)
        self.assertEqual([r["id"] for r in rows], ["id0", "id1", "id2", "id10"])
        self.assertTrue(all(r["via"] == "community-probe" for r in rows))
        self.assertEqual(len(skipped), 3)

    def test_the_cap_and_duplicates_hold_across_syncs(self):
        state = {}
        rows, _ = db.community_rows([self.ev(i) for i in range(3)], state, cap=3)
        self.assertEqual(len(rows), 3)
        rows, skipped = db.community_rows([self.ev(i) for i in range(6)], state, cap=3)  # overlapping re-read
        self.assertEqual(rows, [])
        self.assertEqual([why for _, why in skipped], ["over the daily limit for one reporter"] * 3)

    def test_a_malformed_event_is_skipped_not_fatal(self):
        rows, skipped = db.community_rows([self.ev(1, via=["probe"]), ("not json", "a", "2026-09-28"), self.ev(2)], {})
        self.assertEqual([r["id"] for r in rows], ["id2"])
        self.assertEqual(len(skipped), 2)


class Regressions(Base):
    """Findings from the cross-provider review."""

    def test_urls_lose_credentials_paths_and_private_hosts(self):
        for text, leaked in (("https://alice:secret@example.com/private.apk?token=credential", ("alice", "secret", "private", "credential")),
                             ("https://alice:secret@192.168.1.4/private.apk", ("alice", "192.168", "private")),
                             ("fe80::1234 and 2001:db8::5", ("fe80", "2001:db8")),
                             ("sk-proj-abcdefghijklmnopqrstuv", ("abcdefghijk",)),
                             ("http://frame.local:8080/x", ("frame.local", "8080"))):
            out = tm.scrub(text)
            for s in leaked:
                self.assertNotIn(s, out, (text, out))

    def test_compat_labels_versions_and_sources_are_scrubbed(self):
        tm.update_settings({"compat": True})
        tm.compat_report({"id": "r9", "package": "org.x", "rating": "works", "date": "2026-09-28T00:00:00",
                          "label": "alice@example.com build", "version": "1.0-alice@example.com",
                          "source": "https://alice:secret@192.168.1.4/private.apk"})
        p = self.queued()[-1]["properties"]
        self.assertNotIn("alice", json.dumps(p))
        self.assertNotIn("source", p)

    def test_an_unsent_report_is_shared_again_after_opting_out_and_in(self):
        with mock.patch.object(db, "shared", return_value=False), \
                mock.patch.object(db, "_outbox", return_value=[{"id": "q1", "package": "org.a", "rating": "works",
                                                                "date": "2026-09-01T00:00:00"}]):
            tm.update_settings({"compat": True})
            tm.update_settings({"compat": False})
            self.assertEqual(self.queued(), [])
            tm.update_settings({"compat": True})
        self.assertEqual([e["properties"]["id"] for e in self.queued() if e["event"] == "compat_report"], ["q1"])

    def test_opting_out_waits_for_a_send_in_progress(self):
        tm.update_settings({"noticeShown": True})
        tm.capture("app_opened")
        order = []
        started = threading.Event()

        def slow_open(req, timeout):
            started.set()
            time.sleep(0.3)
            order.append("sent")
            return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False, read=lambda: b"{}")

        with mock.patch.object(tm.urllib.request, "urlopen", side_effect=slow_open):
            th = threading.Thread(target=tm.flush)
            th.start()
            started.wait(2)
            tm.update_settings({"usage": False})
            order.append("opted out")
            th.join()
        self.assertEqual(order, ["sent", "opted out"])

    def test_project_id_comes_from_the_config(self):
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_POSTHOG_PROJECT": "12345"}):
            self.assertEqual(tm.config()["project"], "12345")


class ReportProblem(Base):
    """Report a problem: diagnostics are scrubbed and bounded; the report goes privately to PostHog."""

    def serve(self, status=200):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'{"status":"Ok"}')

            def log_message(self, *a):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        p = mock.patch.dict(os.environ, {"FRAME_CONTROL_POSTHOG_HOST": f"http://127.0.0.1:{httpd.server_port}"})
        p.start()
        self.addCleanup(p.stop)
        return got

    def test_diagnostics_are_scrubbed_and_include_the_log(self):
        log = tm.STATE / "server.log"
        log.write_text("GET /api/status 200\nTraceback: ssh alice@192.168.1.9 failed in %s/x\n" % Path.home())
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_LOG": str(log)}):
            text = fr.diagnostics(["16:00  Install failed: https://bob:pw@example.com/a.apk"], include_logs=True, limit=5000)
        self.assertIn("Frame Control 9.9.9", text)
        self.assertIn("Traceback", text)
        self.assertNotIn("GET /api/status", text)
        for leaked in ("alice", "192.168.1.9", str(Path.home()), "bob", "pw@"):
            self.assertNotIn(leaked, text)

    def test_a_report_is_bounded_in_utf16_units(self):
        body = {"title": "Live view stops", "message": "It stops 😀 " * 800, "diagnostics": "log 😀 line\n" * 2000}
        title, text, diag = fr.compose(body)
        self.assertLessEqual(fr.u16(text), fr.TEXT_MAX)
        self.assertLessEqual(fr.u16(diag), fr.DIAG_MAX)
        self.assertTrue(text.startswith("It stops"))
        with self.assertRaises(ValueError):
            fr.compose({"title": "hi", "message": "It stops after a minute."})

    def test_logs_only_when_asked_and_environment_is_kept_first(self):
        log = tm.STATE / "server.log"
        log.write_text("".join(f"old line {i}\n" for i in range(200)) + "newest line\n")
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_LOG": str(log)}):
            plain = fr.diagnostics(["Copy Jane Doe tax return.pdf to ~/Downloads"])
            full = fr.diagnostics(["Install failed"], include_logs=True, limit=400)
        self.assertNotIn("Jane Doe", plain)
        self.assertNotIn("line", plain)
        self.assertTrue(full.startswith("Frame Control 9.9.9"))
        self.assertIn("Install failed", full)
        self.assertIn("newest line", full)
        self.assertLessEqual(fr.u16(full), 400)

    def test_the_previewed_diagnostics_are_what_is_sent(self):
        got = self.serve()
        fr.send({"title": "Live view stops", "message": "It stops after a minute.",
                 "diagnostics": "Frame Control 9.9.9\nssh janes-mac.tail12345.ts.net failed"})
        diag = got[0][1]["batch"][0]["properties"]["diagnostics"]
        self.assertIn("Frame Control 9.9.9", diag)
        self.assertNotIn("janes-mac", diag)

    def test_send_is_a_private_posthog_event_whatever_the_settings(self):
        got = self.serve()
        tm.update_settings({"usage": False})  # analytics off: a deliberate report still goes
        res = fr.send({"kind": "idea", "title": "Live view stops", "message": "It stops after a minute.",
                       "contact": "me@example.com"})
        path, body = got[0]
        event = body["batch"][0]
        self.assertEqual((path, body["api_key"], event["event"]), ("/batch/", "phc_test", "problem_report"))
        props = event["properties"]
        self.assertEqual((props["kind"], props["title"], props["message"], props["contact"], props["report_id"]),
                         ("idea", "Live view stops", "It stops after a minute.", "me@example.com", res["id"]))
        self.assertEqual((props["$process_person_profile"], props["$geoip_disable"]), (False, True))
        self.assertNotEqual(event["distinct_id"], tm.settings()["id"])  # not linked to the analytics
        self.assertIn(res["id"], res["message"])
        self.assertEqual([e["event"] for e in tm._read_lines(tm.SENT)], ["problem_report"])

    def test_a_sent_report_is_not_an_error_if_the_local_log_fails(self):
        self.serve()
        with mock.patch.object(tm, "record_sent", side_effect=OSError("disk full")):
            res = fr.send({"title": "Live view stops", "message": "It stops after a minute."})
        self.assertTrue(res["id"])

    def test_events_queued_by_older_versions_get_the_placeholder_address(self):
        got = self.serve()
        tm.update_settings({"noticeShown": True})
        tm._write_lines(tm.OUTBOX, [{"event": "app_opened", "distinct_id": "x", "uuid": "u1",
                                     "properties": {"level": "usage"}}])
        self.assertEqual(tm.flush(), 1)
        self.assertEqual(got[0][1]["batch"][0]["properties"]["$ip"], "0.0.0.0")
        self.assertEqual(tm._read_lines(tm.SENT)[0]["properties"]["$ip"], "0.0.0.0")

    def test_the_inbox_skips_malformed_reports(self):
        good = ["2026-09-28T09:50:00Z", "AB12CD34", "bug", "Live view stops", "It stops.", None,
                "0.4.0", "macOS", "", ""]
        rows = [["2026-09-28T10:00:00Z", "X", "bug", "Hand-made", None, None, None, None, None, None], ["short"], good]
        with mock.patch.object(db, "_posthog_query", return_value={"results": rows}), \
             mock.patch.object(sys, "argv", ["frame_report.py", "inbox"]), \
             mock.patch("builtins.print") as out:
            fr.main()
        printed = " ".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertIn("AB12CD34", printed)
        self.assertIn("Hand-made", printed)

    def test_a_refused_report_is_an_error(self):
        self.serve(status=401)
        with self.assertRaisesRegex(fr.ReportError, "HTTP 401"):
            fr.send({"title": "Live view stops", "message": "It stops after a minute."})
        self.assertEqual(tm._read_lines(tm.SENT), [])

    def test_no_key_means_no_report(self):
        with mock.patch.dict(os.environ, {"FRAME_CONTROL_POSTHOG_KEY": ""}), \
             mock.patch.object(tm, "HERE", tm.STATE):
            with self.assertRaisesRegex(fr.ReportError, "no PostHog project key"):
                fr.send({"title": "Live view stops", "message": "It stops after a minute."})


if __name__ == "__main__":
    unittest.main()
