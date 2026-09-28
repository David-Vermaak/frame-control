import hashlib, io, json, os, sys, tempfile, unittest, urllib.error, urllib.request, zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
from apk_sources import SourceError, github, itch, _web

FIX = Path(__file__).parent / 'fixtures' / 'more_sources'


class PublisherSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = patch.object(_web, 'cache', return_value=self.tmp.name)
        self.cache.start()
        self.addCleanup(self.cache.stop)
        self.network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network in test'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_curated_search_is_offline(self):
        self.assertEqual(github.search(github.sources()[0], 'hello')[0]['id'], 'KhronosGroup/OpenXR-SDK-Source')
        self.assertEqual(github.search(github.sources()[0], '', 0), [])

    def test_real_releases(self):
        for key, repo in [('khronos', 'KhronosGroup/OpenXR-SDK-Source'),
                          ('brush', 'icosa-foundation/open-brush'), ('tux', 'SgtBilko76/SuperTux-3D')]:
            with patch.object(github, '_api', return_value=json.loads((FIX / (key + '-releases.json')).read_text())):
                e = github.details(github.sources()[0], repo)
                self.assertTrue(e['downloadable'])
                self.assertTrue(e['versions'][0]['name'].endswith('.apk'))
                self.assertIsNone(e['version_code'])
                with self.assertRaises(SourceError):
                    github.download(github.sources()[0], repo, 123)

    def test_topic_results_need_approval(self):
        data = json.loads((FIX / 'topic.json').read_text())
        with patch.object(github, '_api', return_value=data):
            entries = github.search(github.sources()[0], 'topic:openxr')
        self.assertTrue(entries)
        self.assertTrue(any(not e['downloadable'] for e in entries))
        self.assertFalse(github.details(github.sources()[0], 'unknown/project')['downloadable'])
        with self.assertRaises(SourceError):
            github.search(github.sources()[0], 'topic:piracy')

    def test_feed_free_android_only_and_deduplicated(self):
        with patch.object(_web, 'read', return_value=(FIX / 'itch-feed.txt').read_bytes()):
            entries = itch.search(itch.sources()[0], '')
            self.assertEqual(len(entries), 9)
            e = itch.details(itch.sources()[0], entries[0]['id'])
            self.assertTrue(e['vr'])
            self.assertTrue(e['images']['banner'])
            self.assertFalse(e['downloadable'])
            self.assertEqual(itch.search(itch.sources()[0], 'off nominal')[0]['name'], 'Off Nominal')
        with self.assertRaises(SourceError):
            itch.download(itch.sources()[0], entries[0]['id'])
        with self.assertRaises(SourceError):
            itch._parse(itch.sources()[0], b'not xml')

    def test_paid_and_unsafe_feed(self):
        raw = (FIX / 'itch-feed.txt').read_bytes().replace(b'$0.00', b'$1.00')
        self.assertEqual(itch._parse(itch.sources()[0], raw), [])
        raw = (FIX / 'itch-feed.txt').read_bytes().replace(b'https://absyo.itch.io', b'http://localhost')
        self.assertFalse(any(e['name'] == 'Off Nominal' for e in itch._parse(itch.sources()[0], raw)))

    def test_download_hash_and_cleanup(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, 'w') as z:
            z.writestr('AndroidManifest.xml', b'fixture')
        raw = b.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        with patch.object(_web, 'open_url', return_value=io.BytesIO(raw)):
            result = _web.apk('https://github.com/owner/repo/file.apk', github.HOSTS, digest)
        self.assertTrue(result['verified'])
        self.assertEqual(Path(result['apk']).read_bytes(), raw)
        with patch.object(_web, 'open_url', return_value=io.BytesIO(raw)):
            self.assertFalse(_web.apk('https://github.com/file.apk', github.HOSTS)['verified'])
        for content, expected in [(raw, '0' * 64), (b'html challenge', None)]:
            with patch.object(_web, 'open_url', return_value=io.BytesIO(content)), self.assertRaises(SourceError):
                _web.apk('https://github.com/file.apk', github.HOSTS, expected)
        self.assertFalse(list(Path(self.tmp.name).glob('*.part')))

    def test_origin_and_redirect(self):
        for url in ['http://github.com/x', 'https://evil.test/x', 'https://user@github.com/x']:
            with self.assertRaises(SourceError):
                _web.checked_url(url, github.HOSTS)
        req = urllib.request.Request('https://api.github.com/x', headers={'Authorization': 'Bearer secret'})
        handler = _web.Redirect(('api.github.com', 'github.com'))
        redirected = handler.redirect_request(req, None, 302, '', {}, 'https://github.com/x')
        self.assertFalse(redirected.has_header('Authorization'))
        with self.assertRaises(SourceError):
            handler.redirect_request(req, None, 302, '', {}, 'https://evil.test/x')

    def test_cache_rate_limit_and_invalid_json(self):
        with patch.object(_web, 'open_url', return_value=io.BytesIO(b'index')) as op:
            self.assertEqual(_web.read('https://itch.io/test', ('itch.io',)), b'index')
            self.assertEqual(_web.read('https://itch.io/test', ('itch.io',)), b'index')
            self.assertEqual(op.call_count, 1)
        error = urllib.error.HTTPError('https://api.github.com/x', 403, 'limited', {}, None)
        with patch.object(_web, 'open_url', side_effect=error), self.assertRaisesRegex(SourceError, 'rate limit'):
            _web.read('https://api.github.com/x', ('api.github.com',))
        with patch.object(_web, 'read', return_value=b'<html>'), self.assertRaises(SourceError):
            github._api('/x')


if __name__ == '__main__':
    unittest.main()
