"""VR mod (UEVR) checks that need no headset or network: a fake Steam library on disk.

Run: python3 -m unittest discover -s tests
"""
import hashlib
import io
import itertools
import json
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_mods  # noqa: E402
import test_server  # noqa: E402  (not `from … import`, or unittest runs ServerGuards twice)

APPID = 1067310


def zip_bytes(files, links=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
    return buf.getvalue()


@unittest.skipIf(sys.platform == "win32", "the helper runs on the Frame (Linux): /proc, flock and POSIX prefix paths")
class FakeLibrary(unittest.TestCase):
    """A Steam library with Gravitas's real layout, and pinned downloads served from memory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.steam = home / ".local/share/Steam"
        apps = self.steam / "steamapps"
        (apps / "common/Gravitas/SkyArk/Binaries/Win64").mkdir(parents=True)
        (apps / "common/Gravitas/SkyArk/Binaries/Win64/Drop-Win64-Shipping.exe").write_bytes(b"MZ" * 100)
        (apps / "common/Gravitas/Drop.exe").write_bytes(b"MZ")
        self.prefix = apps / f"compatdata/{APPID}/pfx"
        for sub in ("Roaming", "Local"):
            (self.prefix / "drive_c/users/steamuser/AppData" / sub).mkdir(parents=True)
        (apps / f"appmanifest_{APPID}.acf").write_text(
            f'"AppState"\n{{\n\t"appid"\t\t"{APPID}"\n\t"name"\t\t"Gravitas"\n\t"installdir"\t\t"Gravitas"\n}}\n')
        for name, value in (("STEAM", self.steam), ("CACHE", home / ".cache/frame-control/mods"),
                            ("RECEIPTS", home / ".local/share/frame-control/mods")):
            p = mock.patch.object(frame_mods, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.running = None
        p = mock.patch.object(frame_mods, "game_pid", side_effect=lambda exe: self.running)
        p.start()
        self.addCleanup(p.stop)
        self.downloads = {frame_mods.UEVR["url"]: zip_bytes({"UEVRInjector.exe": b"MZ", "UEVRBackend.dll": b"MZ",
                                                             "openvr_api.dll": b"MZ"})}
        self.downloads[frame_mods.PYTHON["url"]] = zip_bytes({"python.exe": b"MZ", "python314.zip": b"PK"})
        self.pin(self.downloads)

    def pin(self, downloads):
        """Serve these bytes and pin their real hashes in place of the published ones."""
        for name in ("UEVR", "PYTHON"):
            pinned = getattr(frame_mods, name)
            p = mock.patch.object(frame_mods, name,
                                  dict(pinned, sha256=hashlib.sha256(downloads[pinned["url"]]).hexdigest()))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(frame_mods.urllib.request, "urlopen",
                              side_effect=lambda url, timeout: io.BytesIO(downloads[url]))
        self.urlopen = p.start()
        self.addCleanup(p.stop)

    def managed(self):
        return self.prefix / "drive_c/frame-control"


class Status(FakeLibrary):
    def test_unreal_game_is_eligible_and_not_installed(self):
        st = frame_mods.status(APPID)
        self.assertEqual((st["eligible"], st["exe"], st["installed"], st["running"], st["prefix"]),
                         (True, "Drop-Win64-Shipping.exe", False, False, True))

    def test_game_without_unreal_binary_is_refused(self):
        (self.steam / "steamapps/common/Gravitas/SkyArk/Binaries/Win64/Drop-Win64-Shipping.exe").unlink()
        self.assertFalse(frame_mods.status(APPID)["eligible"])
        with self.assertRaisesRegex(frame_mods.Fail, "Unreal Engine"):
            frame_mods.install(APPID)
        self.urlopen.assert_not_called()

    def test_uninstalled_game(self):
        with self.assertRaisesRegex(frame_mods.Fail, "isn't installed"):
            frame_mods.status(620)


class InstallUninstall(FakeLibrary):
    def test_install_then_uninstall_leaves_prefix_as_found(self):
        before = sorted(p.relative_to(self.prefix) for p in self.prefix.rglob("*"))
        res = frame_mods.install(APPID)
        self.assertTrue(res["installed"])
        self.assertTrue((self.managed() / "uevr-1.05/UEVRBackend.dll").is_file())
        self.assertTrue((self.managed() / "python-3.14.7/python.exe").is_file())
        self.assertIn("UEVRBackend.dll", (self.managed() / "python-3.14.7/frame_inject.py").read_text())
        self.assertFalse(list(self.managed().glob(".staging-*")))
        # What UEVR writes on its first injection.
        roaming = self.prefix / "drive_c/users/steamuser/AppData/Roaming/UnrealVRMod/Drop-Win64-Shipping"
        roaming.mkdir(parents=True)
        (roaming / "log.txt").write_text("x")
        (self.prefix / "drive_c/users/steamuser/AppData/Local/praydog/x").mkdir(parents=True)
        frame_mods.uninstall(APPID)
        after = sorted(p.relative_to(self.prefix) for p in self.prefix.rglob("*"))
        self.assertEqual(after, before)
        self.assertFalse(frame_mods.CACHE.exists(), "the downloads go once no game uses them")
        self.assertIsNone(frame_mods.read_receipt(APPID))

    def test_existing_uevr_settings_survive_uninstall(self):
        profile = self.prefix / "drive_c/users/steamuser/AppData/Roaming/UnrealVRMod/Drop-Win64-Shipping"
        profile.mkdir(parents=True)
        (profile / "config.txt").write_text("mine")
        frame_mods.install(APPID)
        frame_mods.uninstall(APPID)
        self.assertEqual((profile / "config.txt").read_text(), "mine")

    def test_second_install_is_a_no_op(self):
        frame_mods.install(APPID)
        calls = self.urlopen.call_count
        self.assertIn("already installed", frame_mods.install(APPID)["message"])
        self.assertEqual(self.urlopen.call_count, calls)

    def test_refuses_while_game_runs(self):
        self.running = 4242
        with self.assertRaisesRegex(frame_mods.Fail, "running"):
            frame_mods.install(APPID)
        self.running = None
        frame_mods.install(APPID)
        self.running = 4242
        with self.assertRaisesRegex(frame_mods.Fail, "running"):
            frame_mods.uninstall(APPID)
        self.assertTrue((self.managed() / "uevr-1.05").is_dir())

    def test_needs_a_prefix(self):
        import shutil
        shutil.rmtree(self.prefix.parent)
        with self.assertRaisesRegex(frame_mods.Fail, "play it once"):
            frame_mods.install(APPID)

    def test_receipt_cannot_point_outside_the_prefix(self):
        frame_mods.install(APPID)
        r = frame_mods.read_receipt(APPID)
        victim = self.steam / "steamapps/common/Gravitas"
        r["remove"].append(str(self.prefix / "../../../common/Gravitas"))
        frame_mods.receipt_path(APPID).write_text(json.dumps(r))
        with self.assertRaisesRegex(frame_mods.Fail, "didn't create"):
            frame_mods.uninstall(APPID)
        self.assertTrue(victim.is_dir())
        self.assertTrue((self.managed() / "uevr-1.05").is_dir(), "nothing removed when the receipt is bad")


    def test_receipt_cannot_delete_other_things_in_the_prefix(self):
        frame_mods.install(APPID)
        r = frame_mods.read_receipt(APPID)
        users = self.prefix / "drive_c/users"
        r["remove"].append(str(users))
        frame_mods.receipt_path(APPID).write_text(json.dumps(r))
        with self.assertRaisesRegex(frame_mods.Fail, "didn't create"):
            frame_mods.uninstall(APPID)
        self.assertTrue(users.is_dir())

    def test_failed_removal_keeps_the_receipt(self):
        frame_mods.install(APPID)
        with mock.patch.object(frame_mods.shutil, "rmtree"):  # e.g. a permission error, swallowed
            with self.assertRaisesRegex(frame_mods.Fail, "couldn't remove"):
                frame_mods.uninstall(APPID)
        self.assertIsNotNone(frame_mods.read_receipt(APPID), "Remove can be tried again")
        frame_mods.uninstall(APPID)
        self.assertFalse(self.managed().exists())

    def test_receipt_cannot_name_another_prefix(self):
        frame_mods.install(APPID)
        r = frame_mods.read_receipt(APPID)
        r["prefix"] = "/"
        frame_mods.receipt_path(APPID).write_text(json.dumps(r))
        with self.assertRaisesRegex(frame_mods.Fail, "isn't app"):
            frame_mods.uninstall(APPID)
        self.assertTrue((self.managed() / "uevr-1.05").is_dir())


class Downloads(FakeLibrary):
    def test_hash_mismatch_deletes_and_installs_nothing(self):
        self.downloads[frame_mods.UEVR["url"]] = zip_bytes({"UEVRBackend.dll": b"tampered"})
        with self.assertRaisesRegex(frame_mods.Fail, "doesn't match"):
            frame_mods.install(APPID)
        self.assertFalse((frame_mods.CACHE / "UEVR.zip").exists())
        self.assertFalse(self.managed().exists())
        self.assertIsNone(frame_mods.read_receipt(APPID))

    def test_archive_escaping_its_folder_is_refused(self):
        for bad in (zip_bytes({"UEVRBackend.dll": b"MZ", "../../evil.dll": b"MZ"}),
                    zip_bytes({"UEVRBackend.dll": b"MZ"}, links=["openvr_api.dll"])):
            downloads = dict(self.downloads, **{frame_mods.UEVR["url"]: bad})
            self.pin(downloads)
            with self.subTest(), self.assertRaises(frame_mods.Fail):
                frame_mods.install(APPID)
            self.assertFalse((self.prefix / "drive_c/evil.dll").exists())
            self.assertFalse(list(self.managed().glob("*")) if self.managed().exists() else [])
            import shutil
            shutil.rmtree(frame_mods.CACHE, ignore_errors=True)

    def test_oversized_archive_is_refused(self):
        with mock.patch.object(frame_mods, "MAX_UNPACKED", 3):
            with self.assertRaisesRegex(frame_mods.Fail, "more than expected"):
                frame_mods.install(APPID)


    def test_oversized_download_is_stopped(self):
        with mock.patch.object(frame_mods, "MAX_DOWNLOAD", 10):
            with self.assertRaisesRegex(frame_mods.Fail, "bigger than expected"):
                frame_mods.install(APPID)
        self.assertFalse(list(frame_mods.CACHE.glob("*")))


class Actions(FakeLibrary):
    def test_one_change_at_a_time(self):
        import fcntl
        lock = Path(tempfile.mkdtemp()) / "mods.lock"
        with mock.patch.object(frame_mods, "LOCK", lock), open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaisesRegex(frame_mods.Fail, "another mod action"):
                frame_mods.main(["install", str(APPID)])
            self.assertFalse(frame_mods.status(APPID)["installed"])
            self.assertEqual(frame_mods.main(["status", str(APPID)])["appid"], APPID, "status never waits")

    def test_zero_frames_is_an_answer(self):
        stats = json.dumps([{"key": f"steam.app.{APPID}", "frame_submits": 0}, {"operation": "status"}])
        with mock.patch.object(frame_mods.subprocess, "run", return_value=mock.Mock(stdout=stats)):
            self.assertEqual(frame_mods.wait("frames", 5, lambda: frame_mods.frame_submits(APPID)), 0)
        with mock.patch.object(frame_mods.subprocess, "run", return_value=mock.Mock(stdout='{"odd": 1}')):
            self.assertIsNone(frame_mods.frame_submits(APPID))


class Start(FakeLibrary):
    def environ(self, text):
        """The game's /proc/<pid>/environ; every other file reads normally."""
        real = frame_mods.Path.read_text
        return mock.patch.object(frame_mods.Path, "read_text", autospec=True, side_effect=lambda path, *a, **k:
                                 text if str(path).startswith("/proc/") else real(path, *a, **k))

    def test_needs_install_first(self):
        with self.assertRaisesRegex(frame_mods.Fail, "isn't installed"):
            frame_mods.start(APPID)

    def run_start(self, reply, loaded=(False, True, True, True)):
        frame_mods.install(APPID)
        self.running = 4242
        env = "WINEPREFIX=/x\0DISPLAY=:1\0WINESERVERSOCKET=17\0PATH=/p\0"
        with self.environ(env), \
                mock.patch.object(frame_mods, "loaded", side_effect=list(loaded) + [True] * 5), \
                mock.patch.object(frame_mods, "x11_windows", return_value=[(1, "SkyArk", 1280)]), \
                mock.patch.object(frame_mods.subprocess, "run", return_value=mock.Mock(stdout=reply, stderr="")) as run, \
                mock.patch.object(frame_mods, "frame_submits", return_value=7), \
                mock.patch.object(frame_mods.time, "sleep"):
            return frame_mods.start(APPID), run

    def test_injects_with_the_games_wine_settings(self):
        res, run = self.run_start('noise\n{"pid": 320}\n')
        self.assertEqual((res["frame_submits"], res["message"]), (7, "UEVR injected into Gravitas; SteamVR is receiving its frames"))
        cmd, env = run.call_args.args[0], run.call_args.kwargs["env"]
        self.assertEqual(cmd[:2], ["wine", "C:\\frame-control\\python-3.14.7\\python.exe"])
        self.assertEqual(cmd[3:], ["Drop-Win64-Shipping.exe", "C:\\frame-control\\uevr-1.05",
                                   "C:\\users\\steamuser\\AppData\\Roaming\\UnrealVRMod\\Drop-Win64-Shipping"])
        self.assertEqual(env["WINEPREFIX"], "/x")
        self.assertNotIn("WINESERVERSOCKET", env, "an inherited fd number means nothing to a new process")
        self.assertIn("HOME", env, "fontconfig needs a HOME for its cache")

    def test_reports_the_injection_error(self):
        with self.assertRaisesRegex(frame_mods.Fail, "couldn't open"):
            self.run_start('{"error": "couldn\'t open Drop-Win64-Shipping.exe (5)"}\n')

    def test_success_is_checked_in_the_game(self):
        with self.assertRaisesRegex(frame_mods.Fail, "has no UEVRBackend.dll"):
            with mock.patch.object(frame_mods.time, "time", side_effect=itertools.count(0, 5)):
                self.run_start('{"pid": 320}\n', loaded=[False] * 40)

    def test_inject_script_is_valid_python(self):
        compile(frame_mods.INJECT_PY, "frame_inject.py", "exec")


class Helper(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "uninstall takes the flock lock, which only exists on the Frame (Linux)")
    def test_unexpected_errors_are_still_json(self):
        # A receipt from another version, without "exe": a KeyError, not a traceback.
        script = (ROOT / "ui" / "frame_mods.py").read_text().replace(
            "def read_receipt(appid):", "def read_receipt(appid):\n    return {'prefix': '/x'}\n\n\ndef _unused(appid):")
        out = subprocess.run([sys.executable, "-", "uninstall", "1"], input=script,
                             capture_output=True, text=True, timeout=30, env={"HOME": tempfile.mkdtemp()})
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("KeyError", json.loads(out.stdout)["error"])

    def test_bad_usage_prints_json_error(self):
        for args in ([], ["install"], ["install", "12x"], ["remove", "620"]):
            out = subprocess.run([sys.executable, str(ROOT / "ui" / "frame_mods.py"), *args],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 1, args)
            self.assertIn("error", json.loads(out.stdout), args)


class ModRoutes(test_server.ServerGuards):
    """Reuses ServerGuards' server (unresolvable SSH alias); validation runs before any SSH."""

    def test_mod_input_validation(self):
        for body in ({"action": "install", "appid": "1; reboot"}, {"action": "install", "appid": ""},
                     {"action": "delete", "appid": "1067310"}, {"appid": "1067310"}):
            status, payload = self.post("/api/mods", body)
            self.assertEqual(status, 400, f"{body} -> {payload}")

    def test_needs_custom_header(self):
        self.assertEqual(self.request("POST", "/api/mods", {"action": "status", "appid": "1"})[0], 403)

    def test_runs_the_mods_helper_with_its_timeout(self):
        import server
        with mock.patch.object(server, "steam_frame", return_value={"ok": 1}) as run:
            self.assertEqual(server.mods({"action": "install", "appid": "1067310"}), {"ok": 1})
        run.assert_called_once_with("install", "1067310", timeout=600, script="frame_mods.py")


for name in [n for n in dir(test_server.ServerGuards) if n.startswith("test_")]:
    setattr(ModRoutes, name, None)
