import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
from apk_sources import search, SourceError
import server

ENTRIES = json.loads((Path(__file__).parent / 'fixtures/apk-search/entries.json').read_text())


def fake(source_id='one', fn=None):
    return types.SimpleNamespace(KIND=source_id, sources=lambda: [dict(id=source_id, name=source_id,
                                 enabled=True, trust='official', builtin=True)],
                                 search=fn or (lambda s, q, limit=50: [e for e in ENTRIES if e['source'] == source_id]),
                                 details=lambda s, i: ENTRIES[0],
                                 download=Mock(return_value={'apk': '/fake.apk', 'obb': []}))


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(search, 'settings_path', return_value=Path(self.tmp.name) / 'enabled.json')
        p.start()
        self.addCleanup(p.stop)
        search._running.clear()
        search._status.clear()


class SearchTests(SettingsTest):
    def test_group_does_not_merge_distinct_or_unknown_packages(self):
        result = search.group(ENTRIES)
        self.assertEqual(len(result), 3)
        self.assertEqual(sorted(len(a['offers']) for a in result), [1, 2, 2])
        same_name = dict(ENTRIES[0], package=None)
        self.assertEqual(len(search.group(ENTRIES[:1] + [same_name])), 2)

    def test_rank_exact_and_installable_and_verified(self):
        result = search.group(ENTRIES, 'Open Brush')
        self.assertEqual(result[0]['package'], 'org.brush')
        self.assertEqual(result[0]['offers'][0]['source'], 'one')
        self.assertEqual(result[1]['package'], 'org.other')
        self.assertEqual(len(search.group(ENTRIES, vr=False)), 1)
        self.assertEqual(len(search.group(ENTRIES, installable=True)), 1)

    def test_fit_unknown_and_native_free_and_vr_hints(self):
        self.assertIsNone(search.fit({})['installable'])
        self.assertTrue(search.fit({'min_sdk': 23, 'abis': []})['installable'])
        self.assertFalse(search.fit({'min_sdk': 23, 'abis': ['x86']})['installable'])
        self.assertIn('Legacy VrApi', search.fit({'engine': 'VrApi'})['reasons'][0])

    def test_timeout_and_failure_leave_other_results(self):
        release = threading.Event()
        calls = []
        def slow(s, q, limit=50):
            calls.append(q)
            release.wait(2)
            return []
        mods = [fake(), fake('slow', slow), fake('broken', Mock(side_effect=SourceError('offline')))]
        try:
            with patch.object(search, 'modules', return_value=(mods, [])):
                started = time.monotonic()
                result = search.search(timeout=.03)
                self.assertLess(time.monotonic() - started, .3)
                self.assertTrue(result['apps'])
                self.assertEqual([s['status'] for s in result['sources']], ['ok', 'loading', 'error'])
                search.search('other', timeout=.03)
                self.assertEqual(calls, [''])
        finally:
            release.set()

    def test_disable_persists_and_prevents_queries_and_installs(self):
        mod = fake()
        with patch.object(search, 'modules', return_value=([mod], [])):
            search.set_enabled('one', False)
            self.assertFalse(search.sources()[0]['enabled'])
            self.assertEqual(search.search()['apps'], [])
            with self.assertRaisesRegex(SourceError, 'disabled'):
                search.install('one', 'brush')

    def test_install_passes_metadata_artwork_and_obb(self):
        mod = fake()
        mod.download.return_value.update(obb=['main.obb'], artwork={'hero': '/hero.png'}, icon_png=b'png')
        def install(apk, name=None, icon_png=None, source=None, artwork=None):
            self.assertEqual((apk, name, icon_png, source, artwork),
                             ('/fake.apk', 'Open Brush', b'png', 'one', {'hero': '/hero.png'}))
            return {'package': 'org.brush'}
        with patch.object(search, 'modules', return_value=([mod], [])), \
             patch.object(server.frame_android, 'install_obb', create=True) as obb:
            # An actual function exposes the future signature for inspection.
            with patch.object(server.frame_android, 'install', install):
                search.install('one', 'brush', 1)
            obb.assert_called_once_with('org.brush', ['main.obb'])
            mod.download.assert_called_once_with(mod.sources()[0] | {'status': 'not searched'}, 'brush', version_code=1)

    def test_discovery_and_demo_are_opt_in(self):
        module = fake()
        with patch.object(search.pkgutil, 'iter_modules', return_value=[types.SimpleNamespace(name='example')]), \
             patch.object(search.importlib, 'import_module', return_value=module), \
             patch.dict(search.os.environ, {'FRAME_APK_SEARCH_DEMO': '0'}):
            self.assertEqual(search.modules(), ([module], []))
        with patch.object(search.pkgutil, 'iter_modules', return_value=[]), \
             patch.dict(search.os.environ, {'FRAME_APK_SEARCH_DEMO': '0'}):
            self.assertEqual(search.modules(), ([], []))

    def test_newest_compatible_then_official_offer(self):
        first = dict(ENTRIES[0], verified=False, trust='community')
        newer = dict(first, source='new', version_code=2, updated='2026-01-01')
        official = dict(newer, source='official', trust='official')
        incompatible = dict(newer, source='blocked', verified=True, min_sdk=40)
        offers = search.group([first, incompatible, newer, official])[0]['offers']
        self.assertEqual([e['source'] for e in offers], ['official', 'new', 'one', 'blocked'])

    def test_missing_obb_support_stops_before_install(self):
        mod = fake()
        mod.download.return_value['obb'] = ['main.obb']
        with patch.object(search, 'modules', return_value=([mod], [])), \
             patch.object(server.frame_android, 'install') as install, \
             patch.dict(server.frame_android.__dict__):
            server.frame_android.__dict__.pop('install_obb', None)
            with self.assertRaisesRegex(SourceError, 'OBB'):
                search.install('one', 'brush')
            install.assert_not_called()

    def test_listing_cannot_download(self):
        mod = fake()
        mod.details = lambda s, i: dict(ENTRIES[0], downloadable=False)
        with patch.object(search, 'modules', return_value=([mod], [])):
            with self.assertRaisesRegex(SourceError, 'developer page'):
                search.install('one', 'brush')
            mod.download.assert_not_called()


class EndpointTests(SettingsTest):
    def setUp(self):
        super().setUp()
        self.mod = fake()
        p = patch.object(search, 'modules', return_value=([self.mod], []))
        p.start()
        self.addCleanup(p.stop)
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {'X-Frame-UI': '1', 'Content-Type': 'application/json'})
        r = c.getresponse()
        result = r.status, json.loads(r.read())
        c.close()
        return result

    def test_http_search_and_validation(self):
        self.assertEqual(self.request('GET', '/api/sources')[1]['sources'][0]['id'], 'one')
        self.assertTrue(self.request('GET', '/api/search?q=Brush&vr=true')[1]['apps'])
        self.assertEqual(self.request('GET', '/api/search?vr=invalid')[0], 400)
        self.assertEqual(self.request('GET', '/api/search?source=missing')[0], 400)
        for body in ({'source': 'one'}, {'source': 'one', 'id': 'brush', 'version_code': True}):
            self.assertEqual(self.request('POST', '/api/sources/install', body)[0], 400)

    def test_http_install_background_job(self):
        with patch.object(server.frame_android, 'install', return_value={'package': 'org.brush'}) as install:
            status, reply = self.request('POST', '/api/sources/install', {'source': 'one', 'id': 'brush'})
            self.assertEqual(status, 200)
            for _ in range(100):
                job = self.request('GET', '/api/job?id=' + reply['job'])[1]
                if job['done']:
                    break
                time.sleep(.01)
            self.assertTrue(job['done'])
            self.assertIsNone(job['error'])
            install.assert_called_once_with('/fake.apk', name='Open Brush', icon_png=None, source='one')

    def test_details_endpoint_and_real_install_stages(self):
        code, entry = self.request('GET', '/api/sources/details?source=one&id=brush')
        self.assertEqual(code, 200)
        self.assertEqual(entry['name'], 'Open Brush')
        self.assertEqual(entry['verdict']['label'], 'Ready to try on the Frame')
        self.assertIn('artwork', entry)
        self.assertEqual(self.request('GET', '/api/sources/details?source=one')[0], 400)
        stages = []
        with patch.object(server.frame_android, 'install', return_value={'package':'org.brush'}):
            search.install('one', 'brush', progress=lambda stage, percent: stages.append((stage,percent)))
        self.assertEqual(stages, [('Downloading',None),('Installing',None)])

    def test_http_repository_management(self):
        self.assertEqual(self.request('POST', '/api/sources', {'action': 'enable', 'source': 'one', 'enabled': False})[0], 200)
        code, reply = self.request('POST', '/api/sources', {'action': 'add', 'url': 'https://repo.example/repo'})
        self.assertEqual(code, 400)
        self.assertIn('not available', reply['error'])
        mod = fake('fdroid')
        mod.add_repo, mod.remove_repo, mod.set_enabled = Mock(), Mock(), Mock()
        with patch.object(search, 'modules', return_value=([mod], [])):
            self.assertEqual(self.request('POST', '/api/sources', {'action': 'add', 'url': 'https://repo.example/repo'})[0], 200)
            mod.add_repo.assert_called_once_with(url='https://repo.example/repo', fingerprint=None, name=None)
            self.assertEqual(self.request('POST', '/api/sources', {'action': 'remove', 'source': 'fdroid'})[0], 200)
            mod.remove_repo.assert_called_once_with(source_id='fdroid')
