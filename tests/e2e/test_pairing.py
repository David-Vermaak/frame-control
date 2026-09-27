"""Setting up the connection: ui/frame_connect.py against Valve's real
steamos-devkit-service on the fake Frame, and its password fallback."""
import json
import os
import stat
import subprocess
import sys
import unittest
import urllib.request

import harness
from harness import FAKE_HOST, ROOT, ctl, state, wait_for

harness.require()


class Pairing(harness.FrameTestCase):
    def setUp(self):
        super().setUp()
        ctl('keys', 'none')             # a computer the headset doesn't know yet

    def connect(self, askpass=None):
        """Start frame_connect.py FAKE_HOST with no terminal, as the app's setup window would."""
        env = {k: v for k, v in os.environ.items() if not k.startswith('SSH_ASKPASS')}
        if askpass:
            script = self.path('askpass')
            with open(script, 'w') as f:
                f.write(f'#!/bin/sh\necho {askpass}\n')
            os.chmod(script, stat.S_IRWXU)
            env.update(SSH_ASKPASS=script, SSH_ASKPASS_REQUIRE='force')
        return subprocess.Popen([sys.executable, str(ROOT / 'ui' / 'frame_connect.py'), FAKE_HOST],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, start_new_session=True)

    def finish(self, proc, timeout=120):
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out

    def authorized_keys(self):
        return ctl('authorized-keys')['text']

    def test_service_properties_and_announcement(self):
        # docs/ssh.md: properties.json answers "login": "steamos" on the Frame.
        with urllib.request.urlopen(f'http://{FAKE_HOST}:32000/properties.json', timeout=10) as r:
            props = json.load(r)
        self.assertEqual(props['login'], 'steamos')
        self.assertEqual(props['devkit1'], ['devkit-1'])
        # At start the service announces _steamos-devkit._tcp under the Frame's hostname.
        ctl('devkit-service', 'off')
        ctl('devkit-service', 'on')
        reg = wait_for(lambda: [c for c in harness.calls('resolve1') if c['method'] == 'RegisterService'],
                       15, 'the mDNS registration')
        self.assertEqual(reg[0]['args'][:3], ['frame', 'frame', '_steamos-devkit._tcp'])
        self.assertEqual(reg[0]['args'][3], 32000)

    def test_pairing_mode_refusal_then_approval(self):
        proc = self.connect()
        # The first /register is refused: "Pair new host" isn't open.
        wait_for(lambda: any(r['answer'] == 'not in pairing mode' for r in state()['pairing_requests']),
                 30, 'a refused pairing request')
        ctl('pairing', 'on')            # the user opens Settings > Developer > Pair new host
        rc, out = self.finish(proc)
        self.assertEqual(rc, 0, out)
        self.assertIn('paired; key login OK', out)
        answers = [r['answer'] for r in state()['pairing_requests']]
        self.assertEqual(answers[-1], 'approve')
        self.assertIn('not in pairing mode', answers)
        self.assertIn('frame-control@', state()['pairing_requests'][-1]['request'])
        # The hook turned sshd on and installed the RSA key for steamos.
        self.assertTrue(harness.calls('steamos-enable-sshd'))
        keys = self.authorized_keys()
        self.assertRegex(keys, r'(?m)^ssh-rsa \S+ frame-control@\S+$')
        self.assertNotIn('900b919520e4cf601998a71eec318fec', keys)  # the magic phrase isn't stored

    def test_denied_request_falls_back_to_the_password(self):
        ctl('pairing', 'on')
        ctl('answer', 'deny')
        rc, out = self.finish(self.connect())   # no password to give: the fallback can't finish
        self.assertEqual(rc, 1, out)
        self.assertIn('devkit pairing failed: the pairing request was denied', out)
        self.assertIn('falling back to the password', out)
        self.assertNotIn('ssh-rsa', self.authorized_keys())

    def test_service_down_uses_the_password(self):
        ctl('devkit-service', 'off')
        rc, out = self.finish(self.connect(askpass='frame'))
        self.assertEqual(rc, 0, out)
        self.assertIn('devkit service not reachable on port 32000', out)
        self.assertIn('key login OK', out)
        with open(os.path.expanduser('~/.ssh/id_ed25519_frame.pub')) as f:
            ours = f.read().split()[1]
        self.assertIn(ours, self.authorized_keys())

    def test_prompt_left_unanswered_times_out(self):
        # approve-ssh-key waits 30 s for Steam, then says so.
        ctl('pairing', 'on')
        ctl('answer', 'timeout')
        rc, out = self.finish(self.connect(), timeout=150)
        self.assertEqual(rc, 1, out)
        self.assertIn('timeout - Steam did not respond to the pairing request', out)

    def test_steam_not_running(self):
        ctl('pairing', 'on')
        ctl('steam', 'off')
        rc, out = self.finish(self.connect())
        self.assertEqual(rc, 1, out)
        self.assertIn('devkit pairing failed: Steam is not running', out)


if __name__ == '__main__':
    unittest.main()
