"""Status, Steam library, volume, clipboard, Flatpaks and the desktop capture,
through the server against the fake Frame."""
import unittest

import harness
from harness import api, ctl, finished, launches, ok, state, wait_for

harness.require()


class Device(harness.FrameTestCase):
    def test_status(self):
        s = ok('GET', '/api/status')
        self.assertEqual(s['hostname'], 'frame')
        self.assertEqual(s['os'], {'version': '0.3.0', 'build': '20260922.6101926', 'variant': 'vr'})
        b = s['battery']
        self.assertEqual((b['percent'], b['status'], b['health']), (76, 'Charging', 'Good'))
        self.assertAlmostEqual(b['watts'], 9.62, places=1)
        self.assertAlmostEqual(b['tempC'], 31.2, places=3)
        self.assertEqual(s['power'], {'type': 'C PD [PD_PPS]', 'watts': 20.0})
        self.assertEqual(s['temp'], 41.5)
        self.assertEqual(s['wifi'], {'ssid': 'Fake:Frame Wi-Fi', 'signal': 72})
        self.assertEqual(s['volume'], {'level': 0.4, 'muted': False})
        self.assertEqual(s['services'], {'steamvr': True, 'desktop': True, 'lepton': False, 'rdp': False})
        # Runtimes and Lepton are Steam "apps" too; the status hides them.
        self.assertEqual([g['name'] for g in s['games']], ['Beat Saber'])
        self.assertIsNotNone(s['disk']['home'])

        ctl('battery', 'capacity=15', 'status=Discharging', 'current_now=-900000')
        b = ok('GET', '/api/status')['battery']
        self.assertEqual((b['percent'], b['status']), (15, 'Discharging'))
        self.assertLess(b['watts'], 0)

    def test_volume_and_mute(self):
        ok('POST', '/api/volume', {'level': 0.55, 'muted': True})
        self.assertEqual(state()['volume'], {'level': 0.55, 'muted': True})
        self.assertEqual(ok('GET', '/api/status')['volume'], {'level': 0.55, 'muted': True})
        status, out, _ = api('POST', '/api/volume', {'level': 2})
        self.assertEqual(status, 400, out)

    def test_clipboard_goes_to_klipper(self):
        text = 'héllo from the e2e tests\nline two, with a trailing newline\n'
        out = ok('POST', '/api/clipboard', {'text': text})
        # ${#text} counts bytes or characters depending on the session's locale.
        self.assertRegex(out['message'], r'^copied via Klipper \(\d+ chars\)$')
        self.assertEqual(state()['clipboard'], [text])

    def test_flatpak_install_and_remove(self):
        # Installs run as background jobs (server.start_job); uninstall answers at once.
        job = finished(ok('POST', '/api/flatpak', {'id': 'org.videolan.VLC', 'action': 'install'}))
        self.assertIsNone(job['error'], job)
        self.assertEqual([f['id'] for f in ok('GET', '/api/status')['flatpaks']], ['org.videolan.VLC'])
        ok('POST', '/api/flatpak', {'id': 'org.videolan.VLC', 'action': 'uninstall'})
        self.assertEqual(state()['flatpaks'], [])
        job = finished(ok('POST', '/api/flatpak', {'id': 'org.example.missing', 'action': 'install'}))
        self.assertIn('Nothing matches org.example.missing', job['error'])

    def test_desktop_capture(self):
        status, png, headers = api('GET', '/api/screenshot')
        self.assertEqual(status, 200, png)
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual(headers.get('X-Capture-Source'), 'gamescope')

    def test_steam_library_and_installs(self):
        owned = ok('GET', '/api/steam/owned')
        self.assertEqual(owned['country'], 'AU')
        games = {g['id']: g for g in owned['games']}
        self.assertEqual(set(games), {2379780, 274190, 620980})
        self.assertEqual((games[2379780]['frame'], games[620980]['installed']), (3, True))
        # Balatro queues at once; Broforce stops at the options dialog, which
        # frame_steam.py accepts with ContinueInstall().
        for appid, name in ((2379780, 'Balatro'), (274190, 'Broforce')):
            out = ok('POST', '/api/steam', {'appid': appid, 'action': 'install'})
            self.assertEqual(out, {'state': 'downloading', 'message': f'{name} is queued to download on the Frame'})
        self.assertEqual(ok('GET', '/api/steam/owned')['download']['appid'], 274190)

    def test_launch_and_store_page(self):
        ok('POST', '/api/launch', {'appid': 620980})
        run = wait_for(lambda: launches('rungameid'), 10, 'the launch to reach Steam')[-1]
        self.assertEqual((run['appid'], run['started']), (620980, True))
        ok('POST', '/api/steam', {'appid': 1145360, 'action': 'store'})
        wait_for(lambda: state()['steam']['pages'], 10, 'the store page')
        self.assertEqual(state()['steam']['pages'][0]['title'], 'Hades on Steam')


if __name__ == '__main__':
    unittest.main()


class VRUtilities(harness.FrameTestCase):
    def test_missing_vr_runtime_is_unavailable_not_zero_fps(self):
        data = ok('GET', '/api/status')
        self.assertIsNone(data['performance']['compositorFps'])
        self.assertIsNone(data['performance']['appFps'])
        self.assertEqual(data['temp'], 41.5)
        self.assertEqual(data['battery']['percent'], 76)

    def test_optional_paid_utilities_are_not_installed(self):
        # The fake library contains games but none of these paid software titles.
        for appid in (1009850, 1173510, 1068820, 908520):
            code, body, _ = api('POST', '/api/steam', {'action': 'install', 'appid': appid})
            self.assertEqual(code, 502, body)
            self.assertIn('not owned', body['error'])
        self.assertEqual(launches('install'), [])

    def test_unverified_controls_are_not_exposed(self):
        for action in ('recenter', 'adjust', 'restore'):
            code, body, _ = api('POST', '/api/vr', {'action': action, 'origin': 'seated', 'y': .1})
            self.assertEqual(code, 400, body)
