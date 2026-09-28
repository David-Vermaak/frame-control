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

    def test_curated_artwork_has_recorded_image_evidence(self):
        evidence = {r['url']: r for r in json.loads((FIX / 'artwork-check.json').read_text())}
        entries = github.search(github.sources()[0], '')
        self.assertEqual(len(entries), 4)
        for entry in entries:
            self.assertTrue(entry['summary'])
            images = entry['images']
            self.assertEqual(entry['icon'], images['icon'])
            for url in [images['icon'], images['banner']] + images['screenshots']:
                self.assertTrue(url.startswith('https://'))
                self.assertEqual(evidence[url]['status'], 200)
                self.assertTrue(evidence[url]['image'])
        self.assertTrue(entries[1]['images']['screenshots'])
        self.assertTrue(entries[2]['images']['screenshots'])

    def test_topic_keeps_curated_artwork(self):
        curated = github._curated()[1]
        repo = {'full_name': curated['repo'], 'name': 'open-brush',
                'owner': {'avatar_url': 'https://avatars.githubusercontent.com/u/1'}}
        with patch.object(github, '_api', return_value={'items': [repo]}):
            entry = github.search(github.sources()[0], 'topic:openxr')[0]
        self.assertEqual(entry['images'], curated['images'])
        unknown = github.details(github.sources()[0], 'unknown/project')
        self.assertEqual(unknown['icon'], 'https://github.com/unknown.png')
        self.assertEqual(unknown['images']['banner'],
                         'https://opengraph.githubassets.com/1/unknown/project')

    def test_real_releases(self):
        for key, repo in [('khronos', 'KhronosGroup/OpenXR-SDK-Source'),
                          ('brush', 'icosa-foundation/open-brush'), ('tux', 'SgtBilko76/SuperTux-3D')]:
            with patch.object(github, '_api', return_value=json.loads((FIX / (key + '-releases.json')).read_text())):
                e = github.details(github.sources()[0], repo)
                self.assertTrue(e['downloadable'])
                self.assertTrue(e['versions'][0]['name'].endswith('.apk'))
                self.assertIsNone(e['version_code'])
                search_entry = github.search(github.sources()[0], repo)[0]
                self.assertEqual(e['images'], search_entry['images'])
                self.assertEqual(e['icon'], e['images']['icon'])
                with self.assertRaises(SourceError):
                    github.download(github.sources()[0], repo, 123)

    def test_topic_results_need_approval(self):
        data = json.loads((FIX / 'topic.json').read_text())
        with patch.object(github, '_api', return_value=data):
            entries = github.search(github.sources()[0], 'topic:openxr')
        self.assertTrue(entries)
        self.assertTrue(any(not e['downloadable'] for e in entries))
        for entry, repo in zip(entries, data['items']):
            self.assertEqual(entry['icon'], repo['owner']['avatar_url'])
            self.assertEqual(entry['images']['icon'], entry['icon'])
            self.assertEqual(entry['images']['banner'],
                             'https://opengraph.githubassets.com/1/' + repo['full_name'])
            self.assertEqual(entry['images']['screenshots'], [])
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
            for item in entries:
                self.assertEqual(item['icon'], item['images']['icon'])
                self.assertEqual(item['icon'], item['images']['banner'])
                self.assertEqual(item['images']['screenshots'], [])
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
        with patch.object(_web, 'open_url', side_effect=error), self.assertRaisesRegex(SourceError, 'FRAME_GITHUB_TOKEN'):
            _web.read('https://api.github.com/x', ('api.github.com',))
        with patch.object(_web, 'open_url', side_effect=error), patch.object(_web.time, 'time', return_value=1e12):
            self.assertEqual(_web.read('https://itch.io/test', ('itch.io',)), b'index')  # throttled: stale copy
        with patch.object(_web, 'read', return_value=b'<html>'), self.assertRaises(SourceError):
            github._api('/x')


if __name__ == '__main__':
    unittest.main()
