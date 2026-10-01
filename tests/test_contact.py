"""A contact email (ui/frame_contact.py): kept only with a matching choice, sent privately,
withdrawn when removed, never lost offline, and the one-time prompt stays dismissed.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import frame_compat_db as db  # noqa: E402
import frame_contact as fc  # noqa: E402
import frame_report as fr  # noqa: E402
import frame_telemetry as tm  # noqa: E402
from test_telemetry import Base, ReportProblem  # noqa: E402

REPORT = {"title": "RDP not working", "message": "It never connects on Windows."}


class Contact(Base):
    """Base's temp telemetry state, ReportProblem's PostHog stand-in, and a temp contact file."""
    serve = ReportProblem.serve

    def setUp(self):
        super().setUp()
        self.addCleanup(fc._removed.clear)
        for name, value in (("STATE", tm.STATE / "contact"), ("FILE", tm.STATE / "contact" / "contact.json")):
            p = mock.patch.object(fc, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.got = self.serve()

    def events(self):
        return [body["batch"][0] for _, body in self.got]

    def offline(self):
        return mock.patch.object(tm, "post", side_effect=tm.SendError("couldn't reach PostHog"))

    # ---- storage and consent flags

    def test_nothing_is_kept_or_sent_until_chosen(self):
        s = fc.state()
        self.assertEqual((s["email"], s["updates"], s["followup"], s["waiting"]), ("", False, False, False))
        self.assertFalse(fc.FILE.exists())
        self.assertEqual(self.got, [])

    def test_an_address_needs_a_choice_and_a_real_address(self):
        with self.assertRaisesRegex(ValueError, "tick"):
            fc.save({"email": "me@example.com"})
        with self.assertRaisesRegex(ValueError, "email address"):
            fc.save({"email": "not an address", "updates": True})
        self.assertEqual(fc.load()["email"], "")
        self.assertEqual(self.got, [])

    def test_only_a_real_true_counts_as_consent(self):
        for wrong in ("false", "true", 1, 0, [], {}):
            with self.assertRaisesRegex(ValueError, "true or false"):
                fc.save({"email": "me@example.com", "updates": wrong, "followup": True})
            with self.assertRaisesRegex(ValueError, "true or false"):
                fr.send({**REPORT, "contact": "me@example.com", "contactFollowup": wrong})
        self.assertEqual((fc.load()["email"], self.got), ("", []))
        fc.save({"email": "me@example.com", "updates": True})  # left out is no
        self.assertEqual((fc.load()["updates"], fc.load()["followup"]), (True, False))

    def test_each_choice_is_sent_privately_on_its_own(self):
        fc.save({"email": " me@example.com ", "updates": True})
        fc.save({"email": "me@example.com", "updates": False, "followup": True})
        first, second = self.events()
        self.assertEqual(first["event"], "contact_consent")
        self.assertEqual({k: first["properties"][k] for k in ("email", "updates", "followup", "action")},
                         {"email": "me@example.com", "updates": True, "followup": False, "action": "set"})
        self.assertEqual((second["properties"]["updates"], second["properties"]["followup"]), (False, True))
        self.assertEqual(first["distinct_id"], second["distinct_id"])  # one contact id, newest wins
        self.assertNotEqual(first["distinct_id"], tm.settings()["id"])  # not the analytics id
        self.assertEqual((first["properties"]["$process_person_profile"], first["properties"]["$geoip_disable"]),
                         (False, True))
        self.assertEqual([e["event"] for e in tm._read_lines(tm.SENT)], ["contact_consent"] * 2)

    def test_sent_whatever_the_analytics_settings(self):
        tm.update_settings({"usage": False})
        fc.save({"email": "me@example.com", "followup": True})
        self.assertEqual(len(self.got), 1)

    def test_saving_the_same_choice_again_sends_nothing(self):
        fc.save({"email": "me@example.com", "updates": True})
        fc.save({"email": "me@example.com", "updates": True})
        self.assertEqual(len(self.got), 1)

    # ---- withdrawal

    def test_removing_the_address_sends_a_withdrawal_without_it(self):
        fc.save({"email": "me@example.com", "updates": True, "followup": True})
        s = fc.save({"email": "", "updates": True, "followup": True})
        self.assertEqual((s["email"], s["updates"], s["followup"]), ("", False, False))
        withdrawal = self.events()[-1]["properties"]
        self.assertEqual((withdrawal["action"], withdrawal["email"], withdrawal["updates"], withdrawal["followup"]),
                         ("withdraw", "", False, False))
        self.assertNotIn("me@example.com", fc.FILE.read_text())

    def test_an_address_still_waiting_is_withdrawn_too(self):
        with self.offline():
            fc.save({"email": "me@example.com", "updates": True})  # may already be on its way
        with mock.patch.object(tm, "post") as post:
            fc.save({"email": ""})
        self.assertEqual([c.args[0][0]["properties"]["action"] for c in post.call_args_list], ["withdraw"])
        self.assertFalse(fc.state()["waiting"])

    def test_offline_the_newest_choice_waits_and_a_withdrawal_is_never_lost(self):
        fc.save({"email": "me@example.com", "updates": True})
        with self.offline():
            s = fc.save({"email": ""})
            self.assertTrue(s["waiting"])
            self.assertFalse(fc._send_pending())
        self.assertEqual(fc.load()["pending"]["properties"]["action"], "withdraw")
        self.assertTrue(fc._send_pending())
        self.assertFalse(fc.state()["waiting"])
        self.assertEqual([e["properties"]["action"] for e in self.events()], ["set", "withdraw"])

    def test_removing_the_address_wipes_it_from_the_sent_log_too(self):
        fc.save({"email": "me@example.com", "followup": True})
        fr.send({**REPORT, "contact": "me@example.com", "contactFollowup": True})
        self.assertIn("me@example.com", tm.SENT.read_text())
        fc.save({"email": ""})
        self.assertNotIn("me@example.com", tm.SENT.read_text())
        self.assertEqual([e["properties"].get("action") for e in tm._read_lines(tm.SENT)
                          if e["event"] == "contact_consent"], ["set", "withdraw"])

    def test_each_change_has_a_higher_rev_so_the_newest_wins_whatever_the_clock(self):
        fc.save({"email": "me@example.com", "updates": True})
        fc.save({"email": "new@example.com", "updates": True})
        fc.save({"email": ""})
        self.assertEqual([e["properties"]["rev"] for e in self.events()], [1, 2, 3])

    def test_a_withdrawal_during_a_send_goes_after_it(self):
        started, release, order = threading.Event(), threading.Event(), []
        real = tm.post

        def slow(batch, timeout=20):
            order.append(batch[0]["properties"]["action"])
            if len(order) == 1:
                started.set()
                release.wait(5)
            real(batch, timeout)

        with mock.patch.object(tm, "post", side_effect=slow):
            t = threading.Thread(target=fc.save, args=({"email": "me@example.com", "updates": True},))
            t.start()
            self.assertTrue(started.wait(5))
            w = threading.Thread(target=fc.save, args=({"email": ""},))
            w.start()
            for _ in range(500):  # the withdrawal is saved while the first send is still out
                if fc.load()["rev"] == 2:
                    break
                time.sleep(0.01)
            self.assertEqual(fc.load()["pending"]["properties"]["action"], "withdraw")
            release.set()
            t.join(5)
            w.join(5)
        self.assertEqual(order, ["set", "withdraw"])
        self.assertEqual([e["properties"]["action"] for e in self.events()], ["set", "withdraw"])
        self.assertFalse(fc.state()["waiting"])
        self.assertNotIn("me@example.com", tm.SENT.read_text())

    def test_a_report_still_sending_when_its_address_is_removed_is_logged_without_it(self):
        fc.save({"email": "me@example.com", "followup": True})
        real = tm.post

        def remove_meanwhile(batch, timeout=20):
            real(batch, timeout)
            fc.save({"email": ""})  # removed while the report is on its way, before it's logged

        with mock.patch.object(tm, "post", side_effect=remove_meanwhile):
            fr.send({**REPORT, "contact": "me@example.com", "contactFollowup": True})
        self.assertNotIn("me@example.com", tm.SENT.read_text())
        fr.send({**REPORT, "contact": "me@example.com", "contactFollowup": True})
        self.assertIn("me@example.com", tm.SENT.read_text())  # sent again after removal: logged as sent

    def test_only_reports_started_before_the_removal_are_redacted_even_within_a_second(self):
        fc._removed["me@example.com"] = 1790000000.3
        event = lambda: {"timestamp": "2026-09-21T12:53:20Z", "properties": {"contact": "me@example.com"}}
        before, after = event(), event()  # the same whole second as the removal
        fc.redact_removed(before, 1790000000.1)
        fc.redact_removed(after, 1790000000.6)
        self.assertEqual((before["properties"]["contact"], after["properties"]["contact"]),
                         ("<removed>", "me@example.com"))

    def test_saving_during_a_slow_send_returns_at_once(self):
        busy = fc._send_lock
        busy.acquire()
        try:
            s = fc.save({"email": "me@example.com", "updates": True})
        finally:
            busy.release()
        self.assertTrue(s["waiting"])  # left for the send under way (or the retry) to take
        self.assertEqual(self.got, [])
        self.assertTrue(fc._send_pending())
        self.assertEqual(len(self.got), 1)

    def test_a_change_saved_as_a_send_finishes_is_not_left_behind(self):
        real = fc._send_lock

        class Lock:  # a Save lands after the sender found nothing waiting, before it lets go
            saved = False

            def acquire(self, blocking=True):
                return real.acquire(blocking)

            def release(self):
                if not Lock.saved:
                    Lock.saved = True
                    s = threading.Thread(target=fc.save, args=({"email": "me@example.com", "updates": True},))
                    s.start()
                    s.join(5)
                    assert not s.is_alive()  # the change is saved while the sender still holds the lock
                real.release()

        with mock.patch.object(fc, "_send_lock", Lock()):
            self.assertTrue(fc._send_pending())
        self.assertEqual([e["properties"]["email"] for e in self.events()], ["me@example.com"])
        self.assertFalse(fc.state()["waiting"])

    # ---- the one-time prompt

    def test_the_prompt_waits_for_a_working_setup_then_stays_dismissed(self):
        self.assertFalse(fc.state()["showPrompt"])  # a new install: the Frame hasn't connected yet
        tm.frame_seen("20260901.1", "3.8")
        self.assertTrue(fc.state()["showPrompt"])
        fc.prompt({"prompt": "dismissed"})
        fc.prompt({"prompt": "shown"})  # a later session can't bring it back
        self.assertEqual(fc.load()["prompt"], "dismissed")
        self.assertFalse(fc.state()["showPrompt"])
        self.assertEqual(self.got, [])  # No thanks sends nothing
        with self.assertRaises(ValueError):
            fc.prompt({"prompt": "reset"})

    def test_the_prompt_is_shown_once_and_saving_answers_it(self):
        tm.frame_seen("20260901.1", "3.8")
        fc.prompt({"prompt": "shown"})
        self.assertFalse(fc.state()["showPrompt"])
        fc.save({"email": "me@example.com", "followup": True, "fromPrompt": True})
        self.assertEqual(fc.load()["prompt"], "answered")

    # ---- reports and the maintainer's list

    def test_a_report_carries_the_address_only_with_follow_up_consent(self):
        fr.send({**REPORT, "contact": "me@example.com"})
        fr.send({**REPORT, "contact": "me@example.com", "contactFollowup": True})
        without, with_ = (e["properties"] for e in self.events())
        self.assertEqual((without["contact"], without["contact_followup"]), ("", False))
        self.assertEqual((with_["contact"], with_["contact_followup"]), ("me@example.com", True))
        with self.assertRaisesRegex(ValueError, "email address"):
            fr.send({**REPORT, "contact": "discord:me", "contactFollowup": True})

    def test_a_report_with_follow_up_carries_the_kept_contact_id(self):
        fr.send({**REPORT, "contact": "me@example.com"})
        self.assertFalse(fc.FILE.exists())  # no follow-up, nothing kept or linked
        fr.send({**REPORT, "contact": "me@example.com", "contactFollowup": True})
        without, with_ = (e["properties"] for e in self.events())
        self.assertEqual(without["contact_id"], "")
        self.assertEqual(with_["contact_id"], fc.load()["id"])
        self.assertNotEqual(with_["contact_id"], tm.settings()["id"])  # not the analytics id
        fc.save({"email": "me@example.com", "updates": True})
        self.assertEqual(self.events()[-1]["distinct_id"], with_["contact_id"])  # same copy, same id

    def report_row(self, ts, contact="me@example.com", followup=True, cid="copy"):
        return [ts, "AB12CD34", "bug", "RDP", "It never connects.", contact, "0.4.0", "Windows", "", "", followup, cid]

    def test_a_later_withdrawal_takes_back_a_reports_follow_up_permission(self):
        reports = [self.report_row("2026-09-10T10:00:00Z"),                      # removed later
                   self.report_row("2026-09-12T10:00:00Z", cid="other"),         # another copy
                   self.report_row("2026-09-20T10:00:00Z"),                      # after the withdrawal
                   self.report_row("2026-09-11T10:00:00Z", cid="", followup=True),  # sent before contact_id
                   self.report_row("2026-09-15T10:00:00Z", cid="same-second")]
        consents = [["copy", "me@example.com", True, 1, "2026-09-01T10:00:00Z"],
                    ["copy", "", False, 2, "2026-09-14T10:00:00Z"],
                    ["other", "me@example.com", True, 1, "2026-09-13T10:00:00Z"],   # still agrees
                    ["same-second", "", False, 1, "2026-09-15T10:00:00Z"], ["short"]]
        fr.mark_withdrawn(reports, consents)
        self.assertEqual([r[10] for r in reports], ["withdrawn", True, True, True, "withdrawn"])

    def test_changing_the_address_or_unticking_follow_up_takes_it_back_too(self):
        reports = [self.report_row("2026-09-10T10:00:00Z", cid="moved"),
                   self.report_row("2026-09-10T10:00:00Z", cid="news-only"),
                   self.report_row("2026-09-10T10:00:00Z", contact="Me@Example.com", cid="case")]
        consents = [["moved", "new@example.com", True, 1, "2026-09-11T10:00:00Z"],
                    ["news-only", "me@example.com", False, 1, "2026-09-11T10:00:00Z"],
                    ["case", "me@example.com", True, 1, "2026-09-11T10:00:00Z"],
                    # rev, not the clock, decides which later choice is newest
                    ["case", "", False, 2, "2026-09-11T09:59:00Z"]]
        fr.mark_withdrawn(reports, consents)
        self.assertEqual([r[10] for r in reports], ["withdrawn", "withdrawn", "withdrawn"])
        reports[2][10] = True
        fr.mark_withdrawn(reports[2:], consents[2:3])
        self.assertIs(reports[2][10], True)  # same address, any case, still agrees

    def test_the_inbox_shows_withdrawn_follow_up_without_the_address(self):
        reports = [self.report_row("2026-09-10T10:00:00Z"), ["short"]]
        consents = [["copy", "", False, 2, "2026-09-14T10:00:00Z"]]
        with mock.patch.object(db, "_posthog_query", side_effect=[{"results": reports}, {"results": consents}]) as q, \
             mock.patch.object(sys, "argv", ["frame_report.py", "inbox", "30"]), \
             mock.patch("builtins.print") as out:
            fr.main()
        self.assertIn("event = 'contact_consent'", q.call_args_list[1].args[0])
        printed = " ".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertIn("follow-up permission since withdrawn", printed)
        self.assertNotIn("me@example.com", printed)
        with mock.patch.object(db, "_posthog_query", return_value={"results": [self.report_row("x", followup=False)]}) as q:
            fr.inbox()
        self.assertEqual(q.call_count, 1)  # nothing to reconcile, no second query

    def test_contacts_lists_the_newest_choice_per_copy_by_consent(self):
        rows = [["a", "both@example.com", True, "true", "2026-09-01T10:00:00Z"],
                ["b", "news@example.com", "true", False, "2026-09-02T10:00:00Z"],
                ["c", "", False, False, "2026-09-03T10:00:00Z"],  # withdrawn
                ["d", "not-an-address", True, True, "2026-09-03T10:00:00Z"], ["short"]]
        with mock.patch.object(db, "_posthog_query", return_value={"results": rows}) as q:
            found = fr.contacts()
        self.assertIn("argMax(properties.email, tuple(ifNull(toInt(properties.rev), 0), timestamp))",
                      q.call_args.args[0])
        self.assertEqual(found, {"updates": [("both@example.com", "2026-09-01"), ("news@example.com", "2026-09-02")],
                                 "followup": [("both@example.com", "2026-09-01")]})
        with mock.patch.object(fr, "contacts", return_value=found), \
             mock.patch.object(sys, "argv", ["frame_report.py", "contacts", "followup"]), \
             mock.patch("builtins.print") as out:
            fr.main()
        printed = " ".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertIn("both@example.com", printed)
        self.assertNotIn("news@example.com", printed)

    def test_the_page_can_reach_it(self):
        import server
        self.assertIs(server.POST["/api/contact"], fc.save)
        self.assertIs(server.POST["/api/contact/prompt"], fc.prompt)

    def test_saving_is_not_headset_work(self):
        """A slow send mustn't hold up switching headsets, nor be refused after a switch."""
        import io
        import server
        seen = []
        for path in ("/api/contact", "/api/contact/prompt"):
            h = server.Handler.__new__(server.Handler)
            body = b'{"prompt": "shown"}' if path.endswith("prompt") else b'{"email": "me@example.com", "updates": true}'
            h.path, h.rfile = path, io.BytesIO(body)
            h.headers = {"Content-Length": str(len(body)), "X-Frame-Device": "a-headset-switched-away-from"}
            h.local_request = lambda: True
            h.send_json = lambda obj, status=200: seen.append((status, server._work[0]))
            with mock.patch.object(fc, "_send_pending", side_effect=lambda block=True: seen.append(("send", server._work[0]))):
                h.do_POST()
        self.assertEqual(seen, [("send", 0), (200, 0), (200, 0)])


# Run these once, in test_telemetry, not again through the import above.
del Base, ReportProblem

if __name__ == "__main__":
    unittest.main()
