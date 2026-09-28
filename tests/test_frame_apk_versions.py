"""Offline version lookup with small index-v2 fixtures."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import io
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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
                             ('index-v2.archive.json', [build(1, 21), build(2)]),
                             ('index-v2.izzy.json', [])]:
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
        self.assertEqual(len(result['errors']), 3)
        self.assertEqual(len(result['links']), 5)

    def test_no_compatible_versions(self):
        with patch.object(frame_catalog, 'load_index', return_value={}):
            result = versions.alternatives('org.example.app')
        self.assertEqual(result['versions'], [])
        self.assertEqual(len(result['links']), 5)

    def test_reduction_memory_cache_and_refresh(self):
        repo = versions.REPOS[0][1]
        index = frame_catalog.load_index(repo)
        self.assertEqual([v['version_code'] for v in index['org.example.app']], [3, 2])
        self.assertEqual(set(index['org.example.app'][0]),
                         {'version', 'version_code', 'min_sdk', 'abis', 'name', 'sha256'})
        raw = os.path.join(self.tmp.name, 'data', 'index-v2.json')
        self.assertTrue(os.path.exists(raw))  # the catalogue build reads it
        os.utime(raw, ns=(1, 1))
        with patch.object(frame_catalog.json, 'load', side_effect=AssertionError('reparsed')):
            self.assertIs(frame_catalog.load_index(repo), index)
        path = raw + '.installable-v1'
        with open(path, 'w') as f:
            json.dump({}, f)
        os.utime(path, ns=(1, 1))
        self.assertEqual(frame_catalog.load_index(repo, cached_only=True), {})
        payload = json.dumps({'packages': {'org.example.app': {'versions': {'x': build(9)}}}}).encode()
        with patch.object(frame_catalog.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as fetch:
            self.assertEqual(frame_catalog.load_index(repo)['org.example.app'][0]['version_code'], 9)
            fetch.assert_called_once()
        self.assertTrue(os.path.exists(raw))

    def test_malformed_entries_are_skipped(self):
        raw = os.path.join(self.tmp.name, 'odd.json')
        with open(raw, 'w') as f:
            json.dump({'packages': {'a.b': {'versions': {'x': {'manifest': {}}, 'y': None, 'z': build(4)}},
                                    'c.d': {'versions': None}, 'e.f': []}}, f)
        self.assertEqual([v['version_code'] for v in frame_catalog._reduce_index(raw)['a.b']], [4])

    def test_one_failing_repo_keeps_the_others(self):
        real = frame_catalog.load_index
        def load(repo, cached_only=False):
            if 'izzy' in repo:
                raise KeyError('file')
            return real(repo, cached_only=cached_only)
        with patch.object(frame_catalog, 'load_index', side_effect=load):
            result = versions.alternatives('org.example.app')
        self.assertEqual([v['version_code'] for v in result['versions']], [3, 2, 1])
        self.assertEqual(len(result['errors']), 1)

    def test_newer_raw_index_outdates_reduced_copy(self):
        repo = versions.REPOS[0][1]
        frame_catalog.load_index(repo)
        raw = os.path.join(self.tmp.name, 'data', 'index-v2.json')
        with open(raw, 'w') as f:
            json.dump({'packages': {'org.example.app': {'versions': {'x': build(8)}}}}, f)
        os.utime(raw, ns=(time.time_ns() + 10**9,) * 2)
        self.assertEqual(frame_catalog.load_index(repo)['org.example.app'][0]['version_code'], 8)

    def test_concurrent_requests_share_download(self):
        raw = os.path.join(self.tmp.name, 'data', 'index-v2.json')
        os.remove(raw)
        payload = json.dumps({'packages': {'org.example.app': {'versions': {'x': build(7)}}}}).encode()
        with patch.object(frame_catalog.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(payload)) as fetch:
            with ThreadPoolExecutor(max_workers=4) as pool:
                indexes = list(pool.map(frame_catalog.load_index, [versions.REPOS[0][1]] * 4))
            fetch.assert_called_once()
        self.assertTrue(all(index is indexes[0] for index in indexes))

    def test_failed_refresh_preserves_cache(self):
        repo = versions.REPOS[0][1]
        index = frame_catalog.load_index(repo)
        path = os.path.join(self.tmp.name, 'data', 'index-v2.json.installable-v1')
        os.utime(path, ns=(1, 1))
        os.utime(os.path.join(self.tmp.name, 'data', 'index-v2.json'), ns=(1, 1))
        with patch.object(frame_catalog.urllib.request, 'urlopen', return_value=io.BytesIO(b'{')):
            with self.assertRaises(ValueError):
                frame_catalog.load_index(repo)
        self.assertEqual(frame_catalog.load_index(repo, cached_only=True), index)
        self.assertFalse(any(name.endswith('.part') for name in os.listdir(os.path.dirname(path))))

    def test_stream_boundaries_and_invalid_index(self):
        raw = os.path.join(self.tmp.name, 'stream.json')
        with open(raw, 'w') as f:
            json.dump({'repo': {'description': 'é' * 70000}, 'packages': {
                'org.example.app': {'metadata': {'text': 'escaped " packages { }' * 6000},
                                    'versions': {'x': build(7)}}}, 'tail': {}}, f)
        self.assertEqual(frame_catalog._reduce_index(raw)['org.example.app'][0]['version_code'], 7)
        for invalid in ('{}', '{"packages": []}', '{"packages": {', '{"packages": {}} trailing'):
            with open(raw, 'w') as f:
                f.write(invalid)
            with self.assertRaises(ValueError):
                frame_catalog._reduce_index(raw)

    def test_cap_and_preferred_build(self):
        records = [dict(version=str(i), version_code=i, min_sdk=21, abis=[],
                        name='/app_%s.apk' % i, sha256=str(i)) for i in range(20)]
        records += [dict(records[-1], version_code=21, abis=['arm64-v8a'], name='/arm.apk'),
                    dict(records[-1], version_code=22, abis=['arm64-v8a', 'x86_64'], name='/all.apk')]
        with patch.object(frame_catalog, 'load_index', return_value={'org.example.app': records}):
            result = versions.alternatives('org.example.app')
        self.assertEqual(result['total'], 22)
        self.assertEqual(len(result['versions']), 8)
        self.assertEqual(len({v['version'] for v in result['versions']}), 8)
        self.assertEqual([v['version_code'] for v in result['versions']], [21, 18, 17, 16, 15, 14, 13, 12])

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
        versions.alternatives('org.example.app')
        with patch.object(versions, 'alternatives', side_effect=AssertionError('recomputed')), \
                patch.object(frame_catalog, 'fetch_apk', return_value='/tmp/example.apk') as fetch, \
                patch.object(frame_android, 'apk_info', return_value={'package': 'org.example.app', 'version_code': 1}), \
                patch.object(frame_android, 'install', return_value={'label': 'Example'}) as install:
            versions.install('org.example.app', 'https://f-droid.org/archive/example_1.apk')
            self.assertEqual(fetch.call_args[0][0]['h'], str(1).zfill(64))
            install.assert_called_once_with('/tmp/example.apk', source='F-Droid archive')
        with self.assertRaises(frame_android.FrameError):
            versions.install('org.example.app', 'https://evil.example/app.apk')

    def test_install_checks_identity_without_network_refresh(self):
        versions.alternatives('org.example.app')
        for name in ('index-v2.json', 'index-v2.archive.json'):
            os.utime(os.path.join(self.tmp.name, 'data', name + '.installable-v1'), ns=(1, 1))
        for info in ({'package': 'wrong.package', 'version_code': 1},
                     {'package': 'org.example.app', 'version_code': 99}):
            with patch.object(frame_catalog, 'fetch_apk', return_value='/tmp/example.apk'), \
                    patch.object(frame_android, 'apk_info', return_value=info), \
                    patch.object(frame_android, 'install') as install:
                with self.assertRaises(frame_android.FrameError):
                    versions.install('org.example.app', 'https://f-droid.org/archive/example_1.apk')
                install.assert_not_called()
        self.network.assert_not_called()


class UploadVersionsTest(unittest.TestCase):
    def test_endpoint_validation(self):
        import server
        for query in ('', 'package=', 'package=foo', 'package=a..b', 'package=a.1b',
                      'package=a.b/path', 'package=a.b&package=c.d', 'package=a.b&code=-1',
                      'package=a.b&code=x', 'package=a.b&code=', 'package=a.b&code=1&code=2'):
            handler = object.__new__(server.Handler)
            handler.path = '/api/apk-versions?' + query
            with patch.object(handler, 'local_request', return_value=True), \
                    patch.object(handler, 'send_json') as reply, \
                    patch.object(versions, 'alternatives') as lookup:
                handler.do_GET()
                self.assertEqual(reply.call_args[0][1], 400, query)
                lookup.assert_not_called()
        handler.path = '/api/apk-versions?package=org.example_app.demo&code=123'
        with patch.object(handler, 'local_request', return_value=True), \
                patch.object(handler, 'send_json') as reply, \
                patch.object(versions, 'alternatives', return_value={'total': 0}) as lookup:
            handler.do_GET()
            lookup.assert_called_once_with('org.example_app.demo', 123)
            reply.assert_called_once_with({'total': 0})

    def test_blocked_uploads_do_not_lookup_before_reply(self):
        import server
        info = {'package': 'org.example.app', 'label': 'Example', 'version': '5',
                'version_code': 5, 'min_sdk': 33, 'abis': [], 'icon_png': None}
        for mode in ('apkinfo', 'apk'):
            handler = object.__new__(server.Handler)
            handler.headers = {'X-Filename': 'app.apk', 'X-Mode': mode, 'Content-Length': '1'}
            handler.rfile = io.BytesIO(b'x')
            with patch.object(frame_android, 'apk_info', return_value=dict(info)), \
                    patch.object(versions, 'alternatives', side_effect=AssertionError('lookup during upload')) as lookup, \
                    patch.object(server, 'ensure_master') as ssh:
                if mode == 'apkinfo':
                    reply = handler.upload()
                    self.assertNotIn('alternatives', reply['apk'])
                    self.assertIn('API 33', reply['apk']['blocker'])
                else:
                    with self.assertRaises(server.Failure) as error:
                        handler.upload()
                    self.assertEqual(error.exception.status, 400)
                    self.assertEqual(error.exception.apk['package'], info['package'])
                lookup.assert_not_called()
                ssh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
