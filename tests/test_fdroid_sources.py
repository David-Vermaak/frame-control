"""Offline authenticated repository fixtures; no tests contact a server."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
from apk_sources import SourceError, fdroid

FIXTURES = Path(__file__).parent / 'fixtures' / 'fdroid'
PIN = (FIXTURES / 'fingerprint.txt').read_text().strip()
URL = 'https://example.org/repo/'


class Repositories(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, value in [('data_dir', lambda *p: self.root.joinpath('data', *p)),
                            ('cache_dir', lambda *p: self.root.joinpath('cache', *p))]:
            mock = patch.object(fdroid.frame_host, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        net = patch.object(fdroid.urllib.request, 'build_opener', side_effect=AssertionError('network forbidden'))
        net.start()
        self.addCleanup(net.stop)
        mock = patch.object(fdroid, '_fetch', side_effect=self.fetch)
        self.fetch_mock = mock.start()
        self.addCleanup(mock.stop)
        self.v1 = False
        self.corrupt = None

    def fetch(self, url, path, maximum):
        name = url.rsplit('/', 1)[-1]
        if self.v1 and name == 'entry.jar':
            raise urllib.error.HTTPError(url, 404, 'missing', None, None)
        payload = (FIXTURES / ('example.apk' if name.endswith('.apk') else name)).read_bytes()
        if name == self.corrupt:
            payload += b'tampered'
        Path(path).write_bytes(payload)

    def add(self):
        return fdroid.add_repo(URL, PIN)

    def test_add_search_details_download_cache(self):
        source = self.add()
        self.assertEqual(source['fingerprint'], PIN)
        self.assertFalse(source['trust_on_first_use'])
        result = fdroid.search(source, 'example offline')
        self.assertEqual(len(result), 1)
        self.assertNotIn('versions', result[0])
        self.assertEqual(result[0]['version_code'], 2)
        self.assertEqual([v['version_code'] for v in fdroid.details(source, 'org.example.app')['versions']], [2, 1])
        downloaded = fdroid.download(source, 'org.example.app', 1)
        self.assertTrue(downloaded['verified'])
        self.assertEqual(Path(downloaded['apk']).read_bytes(), (FIXTURES / 'example.apk').read_bytes())
        count = self.fetch_mock.call_count
        fdroid.download(source, 'org.example.app', 1)
        self.assertEqual(self.fetch_mock.call_count, count)

    def test_wrong_pin_is_not_saved(self):
        with self.assertRaisesRegex(SourceError, 'fingerprint mismatch'):
            fdroid.add_repo(URL, '0' * 64)
        self.assertEqual(fdroid.user_repos(), [])

    def test_tampered_index_is_not_saved(self):
        self.corrupt = 'index-v2.json'
        with self.assertRaisesRegex(SourceError, 'SHA-256'):
            self.add()
        self.assertEqual(fdroid.user_repos(), [])

    def test_tampered_apk_is_not_cached(self):
        source = self.add()
        self.corrupt = 'example2.apk'
        with self.assertRaisesRegex(SourceError, 'APK SHA-256'):
            fdroid.download(source, 'org.example.app')
        self.assertEqual(list(self.root.rglob('*.apk')), [])
        self.assertEqual(list(self.root.rglob('*.part')), [])

    def test_v1_fallback(self):
        self.v1 = True
        source = self.add()
        self.assertEqual(fdroid.search(source, 'Example')[0]['version_code'], 1)
        self.assertTrue(fdroid.download(source, 'org.example.app')['verified'])

    def test_bad_v2_never_downgrades(self):
        self.corrupt = 'index-v2.json'
        with self.assertRaises(SourceError):
            self.add()
        self.assertFalse(any(c.args[0].endswith('index-v1.jar') for c in self.fetch_mock.call_args_list))

    def test_transient_error_never_downgrades(self):
        self.fetch_mock.side_effect = urllib.error.HTTPError(URL, 503, 'unavailable', None, None)
        with self.assertRaises(SourceError):
            self.add()
        self.assertEqual(self.fetch_mock.call_count, 1)

    def test_tofu_preserves_pin_and_settings(self):
        source = fdroid.add_repo('fdroidrepos://example.org/repo')
        self.assertTrue(source['trust_on_first_use'])
        self.assertEqual(source['fingerprint'], PIN)
        fdroid.add_repo(URL)
        self.assertEqual(len(fdroid.user_repos()), 1)
        with self.assertRaisesRegex(SourceError, 'different pinned'):
            fdroid.add_repo(URL, '0' * 64)
        fdroid.set_enabled(source['id'], False)
        self.assertEqual(fdroid.search(fdroid.user_repos()[0], ''), [])
        with self.assertRaisesRegex(SourceError, 'disabled'):
            fdroid.download(fdroid.user_repos()[0], 'org.example.app')
        fdroid.set_enabled('fdroid', False)
        self.assertFalse(fdroid.sources()[0]['enabled'])
        fdroid.remove_repo(source['id'])
        self.assertEqual(fdroid.user_repos(), [])
        with self.assertRaises(SourceError):
            fdroid.remove_repo('fdroid')

    def test_urls(self):
        self.assertEqual(fdroid._url(URL + '?fingerprint=' + PIN.upper()), (URL, PIN))
        for url in ['http://example.org/repo', 'fdroidrepo://example.org', 'https://u:p@example.org', URL+'?other=x']:
            with self.subTest(url=url), self.assertRaises(SourceError):
                fdroid._url(url)
        with self.assertRaisesRegex(SourceError, 'conflicting'):
            fdroid._url(URL + '?fingerprint=' + PIN, '0' * 64)
        self.assertEqual(fdroid._child(URL, '/app/en-US/phoneScreenshots/#0 a.png'),
                         URL + 'app/en-US/phoneScreenshots/%230%20a.png')  # real F-Droid screenshot name
        for name in ['../x.apk', '%2e%2e/x.apk', 'https://evil.org/a.apk', '//evil.org/../x', 'x?token=y', 'x\\y']:
            with self.subTest(name=name), self.assertRaises(SourceError):
                fdroid._child(URL, name)

    def test_recorded_real_signature(self):
        content, fingerprint = fdroid._jar(FIXTURES / 'izzy-entry.jar', 'entry.json', fdroid.IZZY_PIN)
        self.assertEqual(fingerprint, fdroid.IZZY_PIN)
        self.assertIn('index', json.loads(content))

    def test_tampering_each_signature_layer(self):
        for member in ['entry.json', 'META-INF/MANIFEST.MF', 'META-INF/TEST.SF', 'META-INF/TEST.RSA']:
            stream = io.BytesIO()
            with zipfile.ZipFile(FIXTURES / 'entry.jar') as src, zipfile.ZipFile(stream, 'w') as dst:
                for item in src.infolist():
                    data = src.read(item.filename)
                    if item.filename == member:
                        data = data[:-1] + bytes([data[-1] ^ 1])
                    dst.writestr(item.filename, data)
            with self.subTest(member=member), self.assertRaises(SourceError):
                fdroid._jar(io.BytesIO(stream.getvalue()), 'entry.json', PIN)

    def test_duplicate_jar_member_rejected(self):
        stream = io.BytesIO((FIXTURES / 'entry.jar').read_bytes())
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(stream, 'a') as z:
                z.writestr('entry.json', '{}')
        stream.seek(0)
        with self.assertRaisesRegex(SourceError, 'duplicate'):
            fdroid._jar(stream, 'entry.json', PIN)

    def test_corrupt_cache_refetches_verified_index(self):
        source = self.add()
        fdroid.frame_host.cache_dir('apk-sources', source['id'] + '.json').write_text('{')
        self.assertEqual(len(fdroid.search(source, 'example')), 1)
        self.assertEqual(self.fetch_mock.call_count, 4)

    def test_artwork_v1_and_v2_survives_source_cache(self):
        source = self.add()
        for version in ('v1', 'v2'):
            with self.subTest(version=version):
                raw = FIXTURES / ('artwork-' + version + '.json')
                if version == 'v1':
                    normalized = self.root / 'normalized.json'
                    fdroid._v1(raw.read_bytes(), normalized)
                    raw = normalized
                apps = fdroid._reduce(raw, source)
                cache = fdroid.frame_host.cache_dir('apk-sources', source['id'] + '.json')
                fdroid._write(cache, {'version': fdroid.CACHE_VERSION, 'url': URL,
                                     'fingerprint': PIN, 'apps': apps})
                before = self.fetch_mock.call_count
                result = fdroid.search(source, 'example offline')[0]
                self.assertEqual(result['developer'], 'Example Developer')
                self.assertEqual(result['summary'], 'Offline fixture & music. One line.')
                self.assertEqual(result['icon'], URL + 'org.example.app/en-US/icon.png')
                self.assertEqual(result['images'], {
                    'icon': result['icon'],
                    'banner': URL + 'org.example.app/fr/featureGraphic.png',
                    'screenshots': [URL + 'org.example.app/en-US/phoneScreenshots/' + str(i) + '.png' for i in range(1, 5)] +
                                   [URL + 'org.example.app/fr/sevenInchScreenshots/' + str(i) + '.png' for i in range(1, 3)]})
                self.assertEqual(fdroid.details(source, result['id'])['images'], result['images'])
                self.assertEqual(self.fetch_mock.call_count, before)

    def test_missing_artwork_is_not_invented(self):
        result = fdroid.search(self.add(), 'example')[0]
        self.assertEqual(result['images'], {'icon': None, 'banner': None, 'screenshots': []})
        self.assertIsNone(result['icon'])
        self.assertIsNone(result['developer'])

    def test_v1_legacy_icon_and_tablet_fallback(self):
        index = json.loads((FIXTURES / 'artwork-v1.json').read_text())
        app = index['apps'][0]
        app['localized'] = {'fr': {'sevenInchScreenshots': ['tablet.png']}}
        app['icon'] = 'legacy.1.png'
        raw = self.root / 'legacy.json'
        fdroid._v1(json.dumps(index).encode(), raw)
        result = fdroid._reduce(raw, {'id': 'test', 'url': URL})['org.example.app']
        self.assertEqual(result['icon'], URL + 'icons/legacy.1.png')
        self.assertEqual(result['images']['screenshots'], [URL + 'org.example.app/fr/sevenInchScreenshots/tablet.png'])

    def test_v2_legacy_screenshot_keys_and_limit(self):
        meta = {'phoneScreenshots': {'fr': [{'name': '/phone/' + str(i) + '.png'} for i in range(8)]},
                'sevenInchScreenshots': {'en-US': [{'name': '/tablet.png'}]}}
        images = fdroid._images(meta, URL)
        self.assertEqual(images['screenshots'], [URL + 'phone/' + str(i) + '.png' for i in range(6)])
        meta.pop('phoneScreenshots')
        self.assertEqual(fdroid._images(meta, URL)['screenshots'], [URL + 'tablet.png'])

    def test_old_cache_refreshes_for_artwork(self):
        source = self.add()
        path = fdroid.frame_host.cache_dir('apk-sources', source['id'] + '.json')
        saved = json.loads(path.read_text())
        saved.pop('version')
        for app in saved['apps'].values():
            app.pop('images')
        fdroid._write(path, saved)
        self.assertIn('images', fdroid.search(source, 'example')[0])
        self.assertEqual(self.fetch_mock.call_count, 4)

    def test_slow_download_blocks_neither_settings_nor_other_repos(self):
        import threading
        source = self.add()
        other = dict(source, id='other-repo')
        started, release = threading.Event(), threading.Event()
        def fetch(url, path, maximum):
            if threading.current_thread().name == 'slow':
                started.set()
                release.wait(5)
            self.fetch(url, path, maximum)
        self.fetch_mock.side_effect = fetch
        slow = threading.Thread(target=fdroid._load, args=(other, True), name='slow')
        slow.start()
        try:
            self.assertTrue(started.wait(2))
            results = []
            # The settings lock is free and another repo still loads while this one downloads.
            check = threading.Thread(target=lambda: results.append(
                (fdroid.set_enabled('fdroid', False), len(fdroid._load(source, force=True)[0]))))
            check.start()
            check.join(2)
            self.assertEqual(results, [(None, 1)])
        finally:
            release.set()
            slow.join()

    def test_cached_index_does_not_cross_pins(self):
        source = self.add()
        source['fingerprint'] = '0' * 64
        with self.assertRaisesRegex(SourceError, 'fingerprint mismatch'):
            fdroid.search(source, '')


if __name__ == '__main__':
    unittest.main()
