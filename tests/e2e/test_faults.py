"""What Frame Control does when the headset misbehaves: Steam not running,
the headset asleep or with sshd off, and a full disk."""
import os
import subprocess
import sys
import time
import unittest
import zipfile

import harness
import tiny_programs
from harness import HOME, ROOT, api, ctl, exists, install_title, ok, state, wait_for

harness.require()


class Faults(harness.FrameTestCase):
    def exe(self):
        return tiny_programs.write(self.tmp, 'exe')

    def test_steam_not_running_then_install_again(self):
        ctl('steam', 'off')
        _, job = install_title(self.exe())
        # steam-client-create-shortcut's own words, passed on by frame_titles.
        self.assertEqual(job['error'], "Uploaded, but Steam didn't register it: The Steam client is not running. "
                                       "Registration did not complete. With Steam running on the Frame, "
                                       "install it again.")
        self.assertTrue(exists(f'{HOME}/devkit-game/fc_smoke_exe/fc-smoke-exe.exe'))   # the files stay
        self.assertEqual(state()['devkit_games'], {})
        status, out, _ = api('GET', '/api/steam/owned')
        self.assertEqual(status, 502)
        self.assertIn("Steam's UI isn't answering", out['error'])

        ctl('steam', 'on')
        wait_for(lambda: harness._ctl_request('/ping')['ok'], 20, 'Steam to start')
        _, job = install_title(self.exe())
        self.assertIsNone(job['error'], job)
        self.assertIn('fc_smoke_exe', state()['devkit_games'])

    def test_launch_with_steam_stopped(self):
        _, job = install_title(self.exe())
        self.assertIsNone(job['error'], job)
        ctl('steam', 'off')
        status, out, _ = api('POST', '/api/titles', {'action': 'launch', 'id': 'fc_smoke_exe'})
        self.assertEqual(status, 502, out)
        self.assertIn('steam.pid', out['error'])
        self.assertEqual(harness.launches(), [])

    def test_headset_asleep(self):
        ok('GET', '/api/status')                        # a shared connection is up
        ctl('sleep', 'on')
        t0 = time.monotonic()
        status, out, _ = api('GET', '/api/status', timeout=90)
        took = time.monotonic() - t0
        self.assertEqual(status, 502, out)
        self.assertRegex(out['error'], r'[Tt]imed out')
        self.assertLess(took, 40)                       # ConnectTimeout, not a hang
        ctl('sleep', 'off')
        # The next request after waking gets through again.
        wait_for(lambda: api('GET', '/api/status', timeout=60)[0] == 200, 60, 'status after waking')

    def test_sshd_stopped(self):
        ok('GET', '/api/titles')                        # the server's shared connection is up
        ctl('sshd', 'off')
        # Stopping sshd keeps open sessions (Arch's sshd.service kills only the
        # listener), so the server carries on over its shared connection...
        self.assertEqual(api('GET', '/api/titles')[0], 200)
        # ...while anything that connects afresh is refused.
        out = subprocess.run([sys.executable, str(ROOT / 'ui' / 'frame_titles.py'), 'list'],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 1)
        self.assertIn('Connection refused', out.stderr)
        ctl('sshd', 'on')
        wait_for(lambda: subprocess.run([sys.executable, str(ROOT / 'ui' / 'frame_titles.py'), 'list'],
                                        capture_output=True, timeout=60).returncode == 0, 30, 'sshd to be back')

    def test_disk_full(self):
        path = self.path('Big Game.zip')
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('Big Game/BigGame.exe', tiny_programs.pe_x86_64())
            z.writestr('Big Game/data.pak', os.urandom(2 * 1024 * 1024))
        ctl('disk-full', 'on')
        _, job = install_title(path)
        self.assertIn('No space left on device', job['error'] or '', job)
        # A first install that failed part-way leaves nothing behind.
        self.assertFalse(exists(f'{HOME}/devkit-game/Big_Game'))
        self.assertEqual(state()['devkit_games'], {})
        ctl('disk-full', 'off')
        _, job = install_title(path)
        self.assertIsNone(job['error'], job)


if __name__ == '__main__':
    unittest.main()
