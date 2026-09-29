"""Compatibility reports without the maintainer's key: saved locally, never sent.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_compat_db as db  # noqa: E402


class NoKey(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = tmp.name
        for name, value in (("STATE", state), ("OUTBOX", os.path.join(state, "outbox.jsonl")),
                            ("MIRROR", os.path.join(state, "mirror.json"))):
            p = mock.patch.object(db, name, value)
            p.start()
            self.addCleanup(p.stop)
        db._mem.update(at=0, reports=None, source=None)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FRAME_CONTROL_KEY", None)
        # No Keychain entry, and any network use fails the test.
        no_key = mock.patch.object(db.subprocess, "run",
                                   return_value=mock.Mock(returncode=44, stdout=""))
        no_key.start()
        self.addCleanup(no_key.stop)
        net = mock.patch.object(db._opener, "open", side_effect=AssertionError("network used"))
        net.start()
        self.addCleanup(net.stop)

    def test_report_is_kept_locally(self):
        self.assertFalse(db.shared())
        r = db.add({"package": "org.example.app", "date": "2026-09-26T10:00:00", "rating": "works"})
        reports = db.load()
        self.assertEqual([x["id"] for x in reports], [r["id"]])
        self.assertEqual(db._mem["source"], "mirror")


if __name__ == "__main__":
    unittest.main()
