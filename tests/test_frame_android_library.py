"""Offline artwork, Steam API and launcher supervision regressions."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ui'))
import frame_android as android
import frame_artwork as art

spec = importlib.util.spec_from_file_location('steam_shortcuts', ROOT / 'frame/android/steam_shortcuts.py')
shortcuts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shortcuts)


@unittest.skipIf(os.name == 'nt', 'POSIX launcher')
class LauncherTests(unittest.TestCase):
    def exercise(self, terminate, sig=signal.SIGTERM, blocked=None, orphan=False):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            app = d / 'Applications/Android/org.test.app'
            app.mkdir(parents=True)
            (app / 'launch.sh').write_bytes((ROOT / 'frame/android/lepton-app.sh').read_bytes())
            (app / 'app.apk').touch()
            (app / 'instance.id').write_text('2800000001')
            (app / 'shortcut.id').write_text('3346865537')
            bin_dir = d / 'bin'
            bin_dir.mkdir()
            lepton = d / '.local/share/Steam/steamapps/common/Lepton/lepton'
            lepton.parent.mkdir(parents=True)
            def script(path, body):
                path.write_text('#!' + sys.executable + '\n' + body)
                path.chmod(0o755)
            script(lepton, 'import os,time\nfrom pathlib import Path\n'
                   'assert os.environ["SteamAppId"] == "2800000001"\n'
                   'assert os.environ["LEPTON_ENV_SteamAppId"] == "3346865537"\n'
                   'Path(os.environ["HOME"],"started").write_text(str(os.getpid()))\n'
                   'try:\n os.fstat(9); Path(os.environ["HOME"],"inherited-lock").touch()\nexcept OSError: pass\n'
                   + ('time.sleep(30)\n' if terminate else 'raise SystemExit(23)\n'))
            script(bin_dir / 'setsid', 'import os,sys\nos.setsid()\nos.execv(sys.argv[2],sys.argv[2:])\n')
            script(bin_dir / 'flock', 'import os\nraise SystemExit(1 if os.environ.get("TEST_LOCKED") else 0)\n')  # lock semantics belong to Linux; no flock on macOS
            script(bin_dir / 'podman', 'import os,sys\nfrom pathlib import Path\n'
                   'p=Path(os.environ["HOME"],"podman-calls")\n'
                   'with p.open("a") as f: f.write(" ".join(sys.argv[1:])+"\\n")\n'
                   'if sys.argv[1:2]==["inspect"] and os.environ.get("TEST_RUNNING"): print("true")\n')
            env = {**os.environ, 'HOME': str(d), 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
            if blocked:
                env['TEST_' + blocked] = '1'
            if orphan:
                env['TEST_RUNNING'] = '1'
            saved = d / '.local/share/Steam/steamapps/compatdata/2800000001/internal/save'
            saved.parent.mkdir(parents=True)
            saved.write_text('saved game')
            proc = subprocess.Popen(['bash', str(app / 'launch.sh')], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                if blocked:
                    proc.communicate(timeout=5)
                    self.assertEqual(proc.returncode, 1)
                    self.assertFalse((d / 'started').exists())
                    calls = (d / 'podman-calls').read_text() if (d / 'podman-calls').exists() else ''
                    self.assertNotIn('stop ', calls)
                    return
                deadline = time.monotonic() + 5
                while not (d / 'started').exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((d / 'started').exists(), 'launcher did not start Lepton')
                self.assertFalse((d / 'inherited-lock').exists(), 'Lepton inherited the launch lock')
                if terminate:
                    self.assertIsNone(proc.poll(), 'Steam-tracked wrapper exited during the session')
                    proc.send_signal(sig)
                _, err = proc.communicate(timeout=5)
                calls = (d / 'podman-calls').read_text() if (d / 'podman-calls').exists() else ''
                self.assertIn('stop -t 5 lepton-steamlaunch-2800000001', calls, err.decode())
                self.assertEqual(calls.count('stop -t 5'), 2 if orphan else 1)
                self.assertEqual(proc.returncode, 128 + sig if terminate else 23)
                self.assertEqual(saved.read_text(), 'saved game')
                self.assertTrue((app / 'app.apk').exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                if (d / 'started').exists():
                    try:
                        os.kill(int((d / 'started').read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_steam_stop_cleans_container(self):
        self.exercise(True)

    def test_hangup_and_interrupt_cleanup(self):
        for sig in (signal.SIGHUP, signal.SIGINT):
            with self.subTest(sig=sig):
                self.exercise(True, sig)

    def test_duplicate_launch_leaves_existing_session_alone(self):
        self.exercise(False, blocked='LOCKED')

    def test_orphaned_container_is_stopped_and_play_proceeds(self):
        # Container running but the lock free: its launcher was SIGKILLed.
        self.exercise(False, orphan=True)

    def test_normal_exit_cleans_container_and_keeps_exit_code(self):
        self.exercise(False)


FIXTURES = ROOT / 'tests/fixtures/library'


class ArtworkTests(unittest.TestCase):
    def test_icon_roundtrip_and_transparency(self):
        w, h, pixels = art.decode((FIXTURES / 'icon.png').read_bytes())
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(pixels, bytes([255, 0, 0, 255, 0, 255, 0, 255,
                                       0, 0, 255, 128, 0, 0, 0, 0]))
        background = bytearray([10, 20, 30, 255] * 4)
        art.stamp(background, 2, (w, h, pixels), 0, 0, 2)
        self.assertEqual(background[:8], pixels[:8])
        self.assertEqual(background[8:12], bytes([4, 9, 142, 255]))
        self.assertEqual(background[12:], bytes([10, 20, 30, 255]))

    def test_source_inputs_and_url(self):
        import io
        data = (FIXTURES / 'icon.png').read_bytes()
        response = io.BytesIO(data)
        response.geturl = lambda: 'https://example.org/icon.png'
        with patch('frame_steamgriddb.lookup', return_value=({}, [])), \
                patch.object(art.urllib.request, 'urlopen', return_value=response) as fetch:
            images, warnings = art.prepare('Game', artwork={'banner': data, 'icon': 'https://example.org/icon.png'})
        self.assertEqual(images['banner'], ('png', data))
        self.assertEqual(images['icon'], ('png', data))
        self.assertEqual(warnings, [])
        self.assertEqual(fetch.call_count, 1)

    def test_provider_precedence_and_bad_source_fallback(self):
        data = (FIXTURES / 'icon.png').read_bytes()
        jpg = (FIXTURES / 'icon.jpg').read_bytes()
        with patch('frame_steamgriddb.lookup', return_value=({'hero': jpg}, [])):
            images, warnings = art.prepare('Game', data, {'hero': data, 'wide': b'bad', 'screenshots': [b'bad', data]})
        self.assertEqual(images['hero'], ('jpg', jpg))
        self.assertEqual(images['icon'], ('png', data))
        self.assertEqual(images['screenshot'], ('png', data))
        self.assertNotIn('wide', images)
        self.assertEqual(len(warnings), 2)

    def test_supplied_jpeg(self):
        data = (FIXTURES / 'icon.jpg').read_bytes()
        self.assertEqual(art.image_type(data), 'jpg')
        with self.assertRaises(ValueError):
            art.image_type(data[:30])

    def test_bad_artwork_and_expansion_limits(self):
        import zlib
        for value in ({'bad': b'bad'}, ['hero']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                art.prepare('Game', artwork=value)
        data = bytearray((FIXTURES / 'icon.png').read_bytes())
        data[45] ^= 1
        with self.assertRaisesRegex(ValueError, 'checksum'):
            art.decode(bytes(data))
        bomb = art.PNG + art.chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)) + \
            art.chunk(b'IDAT', zlib.compress(b'\0' * 1_000_000)) + art.chunk(b'IEND', b'')
        with self.assertRaisesRegex(ValueError, 'pixels'):
            art.decode(bomb)
        huge = art.PNG + art.chunk(b'IHDR', struct.pack('>IIBBBBB', 10000, 10000, 8, 6, 0, 0, 0)) + art.chunk(b'IEND', b'')
        with self.assertRaisesRegex(ValueError, 'dimensions'):
            art.decode(huge)

    def test_palette_and_filters(self):
        import zlib
        header = art.chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 1, 1, 3, 0, 0, 0))
        data = art.PNG + header + art.chunk(b'PLTE', b'\xff\0\0\0\xff\0') + art.chunk(b'tRNS', b'\xff\x80') + \
            art.chunk(b'IDAT', zlib.compress(b'\0\x40')) + art.chunk(b'IEND', b'')
        self.assertEqual(art.decode(data)[2], bytes([255, 0, 0, 255, 0, 255, 0, 128]))
        for method in range(5):
            # Two identical RGBA rows: exercise each predictor with known filtered bytes.
            first = bytes([10, 20, 30, 255] * 2)
            filtered = bytearray()
            for x, value in enumerate(first):
                a, b, c = first[x-4] if x >= 4 else 0, first[x], first[x-4] if x >= 4 else 0
                predictor = (0, a, b, (a+b)//2, b)[method]
                filtered.append((value - predictor) & 255)
            data = art.PNG + art.chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 6, 0, 0, 0)) + \
                art.chunk(b'IDAT', zlib.compress(b'\0' + first + bytes([method]) + filtered)) + art.chunk(b'IEND', b'')
            self.assertEqual(art.decode(data)[2], first * 2)

    def test_godot_project_icon(self):
        import io
        import zipfile
        data = (FIXTURES / 'icon.png').read_bytes()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('assets/icon.png', data)
        with zipfile.ZipFile(buffer) as archive:
            self.assertEqual(android.frame_apk._icon_png(archive, set(archive.namelist()), []), data)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.responses = json.loads((FIXTURES / 'steam-responses.json').read_text())
        self.info = {'package': 'org.test.vr', 'label': 'VR', 'version': '1', 'icon_png': None,
                     'vr': True, 'launchable': True, 'repairable': False, 'abis': [], 'min_sdk': 24}
        self.images = {slot: ('png', b'PNG ' + slot.encode()) for slot in art.SLOTS}
        self.existing = {'package': 'org.test.vr', 'instance': 2800000001,
                         'shortcut': 3346865537, 'label': 'Old name'}

    def install(self, existing, tool=None):
        def shortcut(*args, **kwargs):
            if args[0] == 'add':
                return '3346865537'
            if args[0] == 'render':
                return json.dumps({'paths': {slot: '/home/steamos/Applications/Android/org.test.vr/artwork/' + slot + '.png' for slot in art.SLOTS}})
            if args[0] == 'list':
                return json.dumps(self.responses['shortcuts'])
            return json.dumps(self.responses['configure'])
        with patch.object(android.frame_artwork, 'prepare', return_value=(self.images, [])), \
                patch.object(android, 'read_meta', return_value=existing), \
                patch.object(android, '_copy') as copy, \
                patch.object(android, 'ssh', return_value=self.responses['home']) as ssh, \
                patch.object(android, 'shortcut_tool', side_effect=tool or shortcut) as api, \
                patch.object(android, '_write_meta') as meta:
            result = android._install('game.apk', self.info, self.info['package'], False, 'New name', 'test')
        return result, ssh, api, meta

    def test_existing_shortcut_refreshes_name_vr_and_every_slot(self):
        result, ssh, api, meta = self.install(self.existing)
        calls = [c.args for c in api.call_args_list]
        self.assertNotIn('add', [c[0] for c in calls])
        configure = next(c for c in calls if c[0] == 'configure')
        self.assertEqual(configure[1:3], ('3346865537', 'New name'))
        self.assertEqual(configure[6], '1')
        self.assertEqual(set(json.loads(configure[7])), set(art.SLOTS))
        self.assertTrue(configure[5].endswith('/org.test.vr/artwork/icon.png'))
        self.assertEqual(result['shortcut'], self.existing['shortcut'])
        self.assertEqual(result['label'], 'New name')
        self.assertEqual(result['library_warnings'], [])
        self.assertEqual(len([c for c in ssh.call_args_list if isinstance(c.kwargs.get('input'), bytes)]), 5)
        meta.assert_called_once()

    def test_first_install_adds_shortcut(self):
        _, _, api, _ = self.install(None)
        self.assertEqual([c.args[0] for c in api.call_args_list], ['add', 'render', 'configure'])

    def test_artwork_forwarded_through_patch(self):
        artwork = {'hero': b'provided'}
        with patch.object(android, 'apk_info', return_value={**self.info, 'repairable': True}), \
                patch.object(android, 'xr_compat_files', return_value={}), \
                patch.object(android, 'patch', return_value={'patched': ['launcher']}), \
                patch.object(android, '_install', return_value={}) as install:
            android.install('x.apk', artwork=artwork)
        self.assertIs(install.call_args.args[-1], artwork)

    def test_failed_new_install_removes_shortcut(self):
        def tool(*args, **kwargs):
            if args[0] == 'add':
                return '3346865537'
            if args[0] == 'render':
                return json.dumps({'paths': {slot: '/tmp/' + slot + '.png' for slot in art.SLOTS}})
            if args[0] == 'configure':
                raise android.FrameError('write failed')
            return '{}'
        with patch.object(android.frame_artwork, 'prepare', return_value=(self.images, [])), \
                patch.object(android, 'read_meta', return_value=None), \
                patch.object(android, '_copy'), patch.object(android, 'ssh', return_value='/home/steamos') as ssh, \
                patch.object(android, 'shortcut_tool', side_effect=tool) as api:
            with self.assertRaisesRegex(android.FrameError, 'write failed'):
                android._install('x.apk', self.info, 'org.test.vr', False, None, None)
        self.assertIn(('remove', '3346865537'), [c.args for c in api.call_args_list])
        self.assertTrue(any(c.args[0] == 'rm -rf Applications/Android/org.test.vr' for c in ssh.call_args_list))

    def test_remove_keeps_data_when_requested_and_surfaces_api_failure(self):
        with patch.object(android, '_meta_or_fail', return_value=dict(self.existing)), \
                patch.object(android, 'stop'), \
                patch.object(android, 'shortcut_tool', return_value='{"warnings": []}') as api, \
                patch.object(android, 'ssh') as ssh:
            android.remove('org.test.vr', keep_data=True)
            api.assert_called_once_with('remove', '3346865537')
            ssh.assert_called_once_with('rm -rf Applications/Android/org.test.vr')
            api.side_effect = android.FrameError('CDP unavailable')
            ssh.reset_mock()
            with self.assertRaises(android.FrameError):
                android.remove('org.test.vr')
            ssh.assert_not_called()

    def test_stop_requests_steam_and_has_container_fallback(self):
        with patch.object(android, '_meta_or_fail', return_value=self.existing), \
                patch.object(android, 'shortcut_tool', side_effect=android.FrameError('offline')) as api, \
                patch.object(android, 'ssh') as ssh:
            android.stop('org.test.vr')
        api.assert_called_once_with('stop', '3346865537')
        self.assertIn('podman stop -t 5 lepton-steamlaunch-2800000001', ssh.call_args.args[0])


class SteamAPITests(unittest.TestCase):
    def test_artwork_api_enums_and_safe_serialization(self):
        with patch.object(shortcuts, 'evaluate', return_value={'warnings': []}) as evaluate:
            shortcuts.configure(42, 'A "name"\n', '/path', '/start', '/icon', True,
                                {slot: str(FIXTURES / 'icon.png') for slot in art.SLOTS})
        js = evaluate.call_args.args[0]
        self.assertIn('SetShortcutIsVR(id, true)', js)
        self.assertIn('SetShortcutName(id, "A \\"name\\"\\n")', js)
        self.assertIn('SetCustomArtworkForApp(id, data, ext, type)', js)
        self.assertEqual(shortcuts.ASSETS, {'grid': 0, 'hero': 1, 'logo': 2, 'wide': 3, 'icon': 4})
        self.assertIn('NewUnsavedCollection(name, undefined, [app])', js)

    def test_remove_clears_every_slot_before_shortcut(self):
        with patch.object(shortcuts, 'evaluate', return_value={}) as evaluate:
            shortcuts.remove(42)
        js = evaluate.call_args.args[0]
        self.assertIn('[0, 1, 2, 3]', js)
        self.assertLess(js.index('ClearCustomArtworkForApp(id, type)'), js.index('RemoveShortcut(id)'))
        self.assertIn('const wanted = []', js)

    def test_stop_uses_exact_64_bit_game_id_string(self):
        with patch.object(sys, 'argv', ['steam_shortcuts.py', 'stop', '3346865537']), \
                patch.object(shortcuts, 'evaluate') as evaluate, patch('builtins.print'):
            shortcuts.main()
        self.assertEqual(evaluate.call_args.args[0], 'SteamClient.Apps.TerminateApp("14374678025558032384", false)')


class SteamContextTests(unittest.TestCase):
    @unittest.skipUnless(__import__('shutil').which('node'), 'optional V8 fixture check requires node')
    def test_collection_lifecycle_and_native_artwork_calls(self):
        steam = {'apps': [], 'shortcuts': [{'appid': 42, 'name': 'Before'}], 'compat_tools': {},
                 'collections': [{'name': 'Android', 'apps': [999]}]}
        def evaluate(expression):
            nonlocal steam
            proc = subprocess.run(['node', str(ROOT / 'tests/fakeframe/rootfs/usr/local/lib/fakeframe/cef_shim.js')],
                                  input=json.dumps({'id': 1, 'expression': expression, 'awaitPromise': True,
                                                    'steam': steam}) + '\n',
                                  text=True, capture_output=True, timeout=10, check=True)
            reply = json.loads(proc.stdout)
            self.assertNotIn('exceptionDetails', reply['result'])
            steam = reply['steam']
            return reply['result']['result'].get('value')
        with patch.object(shortcuts, 'evaluate', side_effect=evaluate):
            result = shortcuts.configure(42, 'Game', '/exe', '/dir', '/icon', True,
                                         {slot: str(FIXTURES / 'icon.png') for slot in art.SLOTS})
            self.assertEqual(result['warnings'], [])
            self.assertTrue(steam['shortcuts'][0]['vr'])
            self.assertEqual(set(steam['shortcuts'][0]['artwork']), {'0', '1', '2', '3'})
            self.assertEqual(steam['collections'], [{'name': 'Android', 'apps': [999, 42]},
                                                   {'name': 'Android VR', 'apps': [42]}])
            shortcuts.configure(42, 'Renamed', '/exe', '/dir', '/icon', False,
                                {slot: str(FIXTURES / 'icon.png') for slot in art.SLOTS})
            self.assertEqual(steam['shortcuts'][0]['name'], 'Renamed')
            self.assertEqual(steam['collections'][1]['apps'], [])
            shortcuts.remove(42)
            self.assertEqual(steam['shortcuts'], [])
            self.assertEqual(steam['collections'][0]['apps'], [999])


if __name__ == '__main__':
    unittest.main()
