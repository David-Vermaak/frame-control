"""Touch: the live view's Control (ui/frame_touch.py on the Frame, and the server's checks).

Run: python3 -m unittest discover -s tests
"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))


class Mapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import frame_touch
        cls.t = frame_touch

    def test_panel_bigger_than_its_display_is_scaled(self):
        # Verified 2026-09-29: a 1920x1080 window on :1 (1280x720) took Accept at
        # window (1828, 1020) as pointer (1219, 680).
        panel = {"root": [1280, 720], "width": 1920, "height": 1080}
        x, y = self.t.to_root(panel, 1828 / 1920, 1020 / 1080)
        self.assertAlmostEqual(x, 1218.7, delta=1)
        self.assertAlmostEqual(y, 679.4, delta=1)

    def test_same_size_is_one_to_one_and_clamped(self):
        panel = {"root": [1280, 720], "width": 1280, "height": 720}
        self.assertEqual(self.t.to_root(panel, 0, 0), (0, 0))
        self.assertEqual(self.t.to_root(panel, 1, 1), (1279, 719))
        self.assertEqual(self.t.to_root(panel, -3, 7), (0, 719))

    def test_other_shapes_are_letterboxed(self):
        panel = {"root": [1280, 720], "width": 800, "height": 800}  # square: bars left and right
        x, y = self.t.to_root(panel, 0, 0.5)
        self.assertAlmostEqual(x, 280, delta=0.5)
        self.assertAlmostEqual(y, 359.5, delta=0.5)

    def test_ascii_table_covers_printable_characters(self):
        for code in range(0x20, 0x7F):
            self.assertIn(chr(code), self.t.ASCII, chr(code))
        self.assertEqual(self.t.ASCII["a"], (30, False))
        self.assertEqual(self.t.ASCII["A"], (30, True))
        self.assertEqual(self.t.ASCII["?"], (53, True))
        self.assertEqual(self.t.ASCII["1"], (2, False))
        self.assertEqual(self.t.ASCII["0"], (11, False))

    def test_events_parsing(self):
        self.assertEqual(self.t.events(b'{"dx": 1}'), [{"dx": 1}])
        self.assertEqual(self.t.events(b'[{"dx": 1}, 5]'), [{"dx": 1}])
        self.assertEqual(self.t.events(b"nope"), [])


class FakeGamescope:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a))


class Apply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import frame_touch
        cls.t = frame_touch

    def setUp(self):
        self.focused = {"window": 7, "display": ":1", "root": [1280, 720], "width": 1280, "height": 720, "name": "x"}
        self.old_focus, self.old_say = self.t.focus, self.t.say
        self.said = []
        self.t.focus = lambda: dict(self.focused)
        self.t.say = lambda state, **more: self.said.append((state, more))
        self.addCleanup(lambda: (setattr(self.t, "focus", self.old_focus), setattr(self.t, "say", self.old_say)))

    def test_tap_moves_then_clicks_in_order(self):
        gs, panel = FakeGamescope(), None
        for e in ({"fx": 0.5, "fy": 0.5, "window": 7}, {"button": "left", "down": True}, {"button": "left", "down": False}):
            panel = self.t.apply(gs, e, panel)
        self.assertEqual([c[0] for c in gs.calls], ["move_to", "button", "button"])
        self.assertEqual(gs.calls[1][1:], ("left", True))

    def test_a_tap_meant_for_another_panel_goes_nowhere(self):
        gs = FakeGamescope()
        self.t.apply(gs, {"fx": 0.5, "fy": 0.5, "window": 99}, None)
        self.assertEqual(gs.calls, [])
        self.assertEqual(self.said, [("ready", {"focus": 7, "stale": True})])

    def test_relative_scroll_keys_text(self):
        gs = FakeGamescope()
        for e in ({"dx": 5, "dy": -3}, {"scroll": [0, 120]}, {"key": 30, "down": True}, {"text": "hi"},
                  {"key": True}, {"button": "sideways"}):
            self.t.apply(gs, e, None)
        self.assertEqual([c[0] for c in gs.calls], ["move_by", "scroll", "key", "text"])

    def test_text_uses_shift_for_capitals(self):
        class Keys(self.t.Gamescope):
            def __init__(self):
                self.pressed = []

            def key(self, code, down):
                self.pressed.append((code, down))
        k = Keys()
        k.text("Hié")  # the é has no key on a US layout and is left out
        self.assertEqual(k.pressed, [(42, True), (35, True), (35, False), (42, False), (23, True), (23, False)])


class ServerChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.s = server

    def test_touch_event_keeps_known_fields(self):
        ev = self.s.touch_event
        self.assertEqual(ev({"fx": 0.5, "fy": 2, "window": 5}), {"fx": 0.5, "fy": 1.0, "window": 5})
        self.assertEqual(ev({"button": "right"}), {"button": "right", "down": True})
        self.assertEqual(ev({"key": 30, "down": False}), {"key": 30, "down": False})
        self.assertEqual(ev({"scroll": [0, 1e9]}), {"scroll": [0.0, 5000.0]})
        self.assertEqual(ev({"dx": 3, "other": 1}), {"dx": 3.0})

    def test_touch_event_rejects_bad_ones(self):
        for bad in (None, {}, {"fx": 0.5, "fy": 0.5}, {"fx": "1", "fy": 0, "window": 1}, {"button": "side"},
                    {"key": 0}, {"key": 999}, {"key": True}, {"scroll": [1]}, {"text": ""}, {"text": "x" * 501},
                    {"fx": 0.1, "fy": 0.1, "window": True}):
            with self.assertRaises(self.s.Failure, msg=repr(bad)):
                self.s.touch_event(bad)

    def test_panel_stream_is_the_window_and_checked(self):
        cmd = self.s.stream_command("src=panel&window=10485777&display=:1&h=720&fps=30")
        self.assertIn("DISPLAY=:1 ffmpeg", cmd)
        self.assertIn("-window_id 10485777 -i :1", cmd)
        for bad in ("src=panel&window=1;rm&display=:1", "src=panel&window=1&display=:1;x", "src=panel&display=:1"):
            with self.assertRaises(self.s.Failure):
                self.s.stream_command(bad + "&h=720&fps=30")

    def test_touch_agent_runs_the_helper_with_nothing_to_copy(self):
        agent = self.s.TouchAgent()
        self.assertEqual(agent.deliver(lambda m: None), "")
        cmd = agent.command()
        self.assertTrue(cmd.startswith("python3 -u -c '"))
        self.assertIn("frame_touch", cmd)

    def test_batch_limit(self):
        with self.assertRaises(self.s.Failure):
            self.s.remote_touch({"events": [{"dx": 1}] * (self.s.INPUT_BATCH_LIMIT + 1)})


@unittest.skipUnless(shutil.which("node"), "needs node")
class PageGestures(unittest.TestCase):
    """Control's gestures and queue (tests/page/ctrl_gestures.mjs runs the real functions from index.html)."""

    def test_gestures(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "page" / "ctrl_gestures.mjs")], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Script(unittest.TestCase):
    def test_helper_compiles_on_the_frames_python(self):
        # The Frame runs it with its own python3 (3.13 on SteamOS 0.4.1); stdlib and ctypes only.
        src = (ROOT / "ui/frame_touch.py").read_text()
        compile(src, "frame_touch.py", "exec")
        for mod in ("import ctypes", "import json", "import select"):
            self.assertIn(mod, src)
        self.assertNotIn("import requests", src)


if __name__ == "__main__":
    unittest.main()
