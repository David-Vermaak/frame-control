"""Per-computer differences in ui/frame_host.py: which Linux family a computer
is, from /etc/os-release, and the install command that follows from it.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_host  # noqa: E402

OS_RELEASES = {
    'NAME="Nobara Linux"\nID=nobara\nID_LIKE="rhel centos fedora"\n': "fedora",
    'NAME="Fedora Linux"\nID=fedora\n': "fedora",
    'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n': "debian",
    'NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n': "debian",
    'NAME="Arch Linux"\nID=arch\n': "arch",
    'NAME="CachyOS Linux"\nID=cachyos\nID_LIKE=arch\n': "arch",
    'NAME="openSUSE Tumbleweed"\nID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n': "suse",
    'NAME="NixOS"\nID=nixos\n': None,
}


class LinuxFamily(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "os-release"

    def test_families(self):
        for text, family in OS_RELEASES.items():
            with self.subTest(text.splitlines()[0]):
                self.path.write_text(text)
                self.assertEqual(frame_host.linux_family(str(self.path)), family)

    def test_missing_file(self):
        self.assertIsNone(frame_host.linux_family(str(self.path)))


class InstallHint(unittest.TestCase):
    def linux(self):
        return mock.patch.multiple(frame_host, MAC=False, WINDOWS=False)

    def test_per_family(self):
        with self.linux():
            self.assertEqual(frame_host.install_hint("adb", "fedora"), "sudo dnf install android-tools")
            self.assertEqual(frame_host.install_hint("adb", "debian"), "sudo apt install adb")
            self.assertEqual(frame_host.install_hint("adb", "arch"), "sudo pacman -S android-tools")

    def test_unknown_family_is_generic(self):
        with self.linux(), mock.patch.object(frame_host, "linux_family", return_value=None):
            self.assertIn("distribution's adb package", frame_host.install_hint("adb"))

    def test_mac_and_windows_ignore_os_release(self):
        with mock.patch.multiple(frame_host, MAC=True, WINDOWS=False):
            self.assertEqual(frame_host.install_hint("adb", "fedora"), "brew install android-platform-tools")
        with mock.patch.multiple(frame_host, MAC=False, WINDOWS=True):
            self.assertEqual(frame_host.install_hint("adb", "fedora"), "winget install Google.PlatformTools")


if __name__ == "__main__":
    unittest.main()
