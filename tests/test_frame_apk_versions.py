"""Offline version lookup with small index-v2 fixtures."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ui'))
import frame_apk_versions as versions
import frame_catalog
import frame_android


def build(code, sdk=30, abis=None):
    return {'manifest': {'versionName': str(code), 'versionCode': code,
                         'usesSdk': {'minSdkVersion': sdk}, 'nativecode': abis or []},
            'file': {'name': f'/example_{code}.apk', 'sha256': str(code).zfill(64)}}


class VersionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = os.path.join(self.tmp.name, 'data')
        os.mkdir(data)
        for name, builds in [('index-v2.json', [build(5, 33), build(4, abis=['x86_64']),
                                               build(3, abis=['arm64-v8a', 'x86_64']), build(2)]),
                             ('index-v2.archive.json', [build(1, 21), build(2)])]:
            with open(os.path.join(data, name), 'w') as f:
                json.dump({'packages': {'org.example.app': {'versions': {str(i): b for i, b in enumerate(builds)}}}}, f)
        self.enter_patch(patch.object(frame_catalog, 'CATALOG', self.tmp.name))
        self.enter_patch(patch.dict(os.environ, {'FRAME_CONTROL_APP': ''}))
        self.network = self.enter_patch(patch.object(frame_catalog.urllib.request, 'urlopen', side_effect=AssertionError('network used')))

    def enter_patch(self, p):
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def test_filter_order_archive_and_dedup(self):
        result = versions.alternatives('org.example.app')
        self.assertEqual([v['version_code'] for v in result['versions']], [3, 2, 1])
        self.assertEqual(result['versions'][-1]['source'], 'F-Droid archive')
        self.assertEqual(result['versions'][-1]['url'], 'https://f-droid.org/archive/example_1.apk')
        self.assertEqual(result['errors'], [])
        self.network.assert_not_called()

    def test_current_version(self):
        result = versions.alternatives('org.example.app', 3)
        self.assertEqual([v['version_code'] for v in result['versions']], [2, 1])

    def test_fallback(self):
        result = versions.alternatives('com.missing.app')
        self.assertEqual(result['versions'], [])
        self.assertEqual([v['source'] for v in result['links']], ['APKMirror', 'APKPure', 'Uptodown', 'F-Droid', 'GitHub'])
        self.assertTrue(all('com.missing.app' in v['url'] for v in result['links']))
        self.assertIn('Android 11', result['note'])
        self.assertIn('arm64-v8a', result['note'])

    def test_failed_indexes_keep_search_links(self):
        with patch.object(frame_catalog, 'load_index', side_effect=OSError('offline')):
            result = versions.alternatives('org.example.app')
        self.assertEqual(len(result['errors']), 2)
        self.assertEqual(len(result['links']), 5)

    def test_no_compatible_versions(self):
        with patch.object(frame_catalog, 'load_index', return_value={
                'packages': {'org.example.app': {'versions': {'x': build(10, 33)}}}}):
            result = versions.alternatives('org.example.app')
        self.assertEqual(result['versions'], [])
        self.assertEqual(len(result['links']), 5)

    def test_android_names_and_verdict(self):
        for sdk, name in [(23, 'Android 6.0'), (30, 'Android 11'), (32, 'Android 12L'), (33, 'Android 13'), (99, 'Android API 99')]:
            self.assertEqual(versions.android_name(sdk), name)
        info = {'package': 'org.example.app', 'label': 'Example', 'version': '5.0',
                'version_code': 50, 'min_sdk': 33, 'abis': ['arm64-v8a']}
        description = versions.describe(info)
        for text in ['org.example.app', '5.0', 'code 50', 'Android 13', 'arm64-v8a', 'cannot install']:
            self.assertIn(text, description)
        info.update(min_sdk=30, abis=[])
        self.assertIn('can install', versions.describe(info))
        info['abis'] = ['armeabi-v7a']
        self.assertIn('no arm64-v8a build', versions.describe(info))

    def test_install_resolves_index_hash(self):
        with patch.object(frame_catalog, 'fetch_apk', return_value='/tmp/example.apk') as fetch, \
                patch.object(frame_android, 'apk_info', return_value={'package': 'org.example.app', 'version_code': 1}), \
                patch.object(frame_android, 'install', return_value={'label': 'Example'}) as install:
            versions.install('org.example.app', 'https://f-droid.org/archive/example_1.apk')
            self.assertEqual(fetch.call_args[0][0]['h'], str(1).zfill(64))
            install.assert_called_once_with('/tmp/example.apk', source='F-Droid archive')
        with self.assertRaises(frame_android.FrameError):
            versions.install('org.example.app', 'https://evil.example/app.apk')


class UploadVersionsTest(unittest.TestCase):
    def test_blocked_uploads_return_alternatives_before_ssh(self):
        import server
        info = {'package': 'org.example.app', 'label': 'Example', 'version': '5',
                'version_code': 5, 'min_sdk': 33, 'abis': [], 'icon_png': None}
        result = {'package': info['package'], 'versions': [], 'links': versions.search_links(info['package'])}
        for mode in ('apkinfo', 'apk'):
            handler = object.__new__(server.Handler)
            handler.headers = {'X-Filename': 'app.apk', 'X-Mode': mode, 'Content-Length': '1'}
            handler.rfile = io.BytesIO(b'x')
            with patch.object(frame_android, 'apk_info', return_value=dict(info)), \
                    patch.object(versions, 'alternatives', return_value=result) as lookup, \
                    patch.object(server, 'ensure_master') as ssh:
                if mode == 'apkinfo':
                    reply = handler.upload()
                    self.assertEqual(reply['apk']['alternatives'], result)
                    self.assertIn('API 33', reply['apk']['blocker'])
                else:
                    with self.assertRaises(server.Failure) as error:
                        handler.upload()
                    self.assertEqual(error.exception.status, 400)
                    self.assertEqual(error.exception.alternatives, result)
                lookup.assert_called_once_with('org.example.app', 5)
                ssh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
