"""Get games checks that need no headset or network.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_store  # noqa: E402
import test_server  # noqa: E402  (not `from … import`, or unittest runs ServerGuards twice)


class SteamRoutes(test_server.ServerGuards):
    """Reuses ServerGuards' server (unresolvable SSH alias); validation runs before any SSH."""

    def test_steam_input_validation(self):
        for body in ({"action": "install", "appid": "620; reboot"}, {"action": "install", "appid": ""},
                     {"action": "uninstall", "appid": "620"}, {"appid": "620"}):
            status, payload = self.post("/api/steam", body)
            self.assertEqual(status, 400, f"{body} -> {payload}")

    def test_search_needs_country(self):
        for query in ("q=portal", "q=portal&cc=A", "q=portal&cc=AU1", "q=portal&cc=%27x"):
            status, _, _ = self.request("GET", f"/api/steam/search?{query}", headers={"X-Frame-UI": "1"})
            self.assertEqual(status, 400, query)

    def test_steam_routes_need_custom_header(self):
        self.assertEqual(self.request("GET", "/api/steam/owned")[0], 403)
        self.assertEqual(self.request("GET", "/api/steam/search?q=x&cc=AU")[0], 403)
        self.assertEqual(self.request("POST", "/api/steam", {"action": "install", "appid": "620"})[0], 403)


# Don't rerun the inherited ServerGuards tests under this module.
for name in [n for n in dir(test_server.ServerGuards) if n.startswith("test_")]:
    setattr(SteamRoutes, name, None)


class FrameSteamHelper(unittest.TestCase):
    def test_bad_usage_prints_json_error(self):
        for args in ([], ["install"], ["install", "12x"], ["remove", "620"]):
            out = subprocess.run([sys.executable, str(ROOT / "ui" / "frame_steam.py"), *args],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 1, args)
            self.assertIn("error", json.loads(out.stdout), args)


class HelperErrors(unittest.TestCase):
    def test_json_error_found_despite_ssh_stderr(self):
        import server
        noise = server.Failure("Warning: Permanently added 'frame' to the list of known hosts.")
        noise.stdout = '{"error": "Steam\'s UI isn\'t answering"}\n'
        with mock.patch.object(server, "ssh", side_effect=noise):
            with self.assertRaises(server.Failure) as cm:
                server.steam_frame("owned")
        self.assertEqual(str(cm.exception), "Steam's UI isn't answering")

    def test_other_failures_pass_through(self):
        import server
        with mock.patch.object(server, "ssh", side_effect=server.Failure("Timed out talking to frame")):
            with self.assertRaises(server.Failure) as cm:
                server.steam_frame("owned")
        self.assertEqual(str(cm.exception), "Timed out talking to frame")


class StoreSearch(unittest.TestCase):
    def setUp(self):
        frame_store._compat.clear()

    def test_keeps_apps_and_attaches_frame_rating(self):
        def fake_get(path, params, timeout=10):
            if path == "api/storesearch":
                self.assertEqual(params["cc"], "AU")
                return {"items": [{"type": "app", "id": 620, "name": "Portal 2", "price": {"final": 1450}},
                                  {"type": "sub", "id": 7, "name": "Bundle"}]}
            return {"results": {"frame_resolved_category": 3}}
        with mock.patch.object(frame_store, "_get", side_effect=fake_get):
            results = frame_store.search("portal", "AU")
        self.assertEqual([(r["id"], r["frame"]) for r in results], [(620, 3)])

    def test_rating_failure_is_unknown_and_not_cached(self):
        with mock.patch.object(frame_store, "_get", side_effect=OSError("offline")):
            self.assertEqual(frame_store.frame_rating(620), 0)
        self.assertNotIn(620, frame_store._compat)

    def test_malformed_rating_is_unknown(self):
        for payload in ({"results": []}, {"results": {"frame_resolved_category": {}}},
                        {"results": {"frame_resolved_category": 9}}):
            frame_store._compat.clear()
            with mock.patch.object(frame_store, "_get", return_value=payload):
                self.assertEqual(frame_store.frame_rating(620), 0, payload)

    def test_blank_query_makes_no_request(self):
        with mock.patch.object(frame_store, "_get") as get:
            self.assertEqual(frame_store.search("   ", "AU"), [])
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
