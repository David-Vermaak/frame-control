"""Every public sideload entry point reaches the mandatory five-slot writer."""
import contextlib
import io
import json
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ui'))
import frame_android as android
import frame_artwork as artwork
import frame_catalog as catalog
import frame_apk_versions as versions
import frame_titles as titles
import frame_steamgriddb as sgdb
import server
import frame_webinstall as webinstall


class EntryPoints(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.info = {'package':'org.example.game', 'label':'Example', 'version':'1', 'version_code':1,
                     'vr':False, 'repairable':False, 'launchable':True, 'min_sdk':24, 'abis':[], 'icon_png':None}
        self.stack.enter_context(patch.object(android, 'apk_info', return_value=self.info))
        self.stack.enter_context(patch.object(android, 'read_meta', return_value=None))
        self.stack.enter_context(patch.object(android, '_copy'))
        self.stack.enter_context(patch.object(android, 'ssh', return_value='/home/steamos'))
        self.stack.enter_context(patch.object(artwork, 'prepare', return_value=({}, [])))
        self.api = self.stack.enter_context(patch.object(android, 'shortcut_tool', side_effect=self.steam))

    def steam(self, *args, **kwargs):
        if args[0] == 'add': return '3346865537'
        if args[0] == 'render': return json.dumps({'paths': {s:'/tmp/'+s+'.png' for s in artwork.SLOTS}})
        if args[0] == 'list': return json.dumps([{'appid':3346865537,'name':'Example','devkit_gameid':'Example'}])
        return '{"warnings":[]}'

    def assert_art(self):
        calls = [c.args for c in self.api.call_args_list]
        self.assertEqual(sum(c[0] == 'render' for c in calls), 1)
        configs = [c for c in calls if c[0] == 'configure']
        self.assertEqual(len(configs), 1)
        self.assertEqual(set(json.loads(configs[0][7])), set(artwork.SLOTS))

    def test_cli_install(self):
        with patch.object(sys, 'argv', ['frame_android.py', 'install', 'game.apk']), patch('builtins.print'):
            android.main()
        self.assert_art()

    def test_file_upload(self):
        class Request:
            headers = {'X-Filename':'game.apk','X-Mode':'apk','Content-Length':'3'}
            rfile = io.BytesIO(b'apk')
        with patch.object(server, 'ensure_master'):
            result = server.Handler.upload(Request())
        self.assertEqual(result['app']['package'], self.info['package'])
        self.assert_art()

    def test_catalogue_install(self):
        with patch.object(catalog, 'app', return_value={'r':'yes','t':False,'n':'Example'}), \
                patch.object(catalog, 'fetch_apk', return_value='game.apk'), \
                patch.object(catalog, 'fetch_icon', return_value=None):
            catalog.install(self.info['package'])
        self.assert_art()

    def test_version_finder_install(self):
        record = {'url':'https://example.org/app.apk','sha256':'fixture','version_code':1,'source':'F-Droid'}
        with patch.object(versions, '_versions', return_value=([record], [])), \
                patch.object(catalog, 'fetch_apk', return_value='game.apk'):
            versions.install(self.info['package'], record['url'])
        self.assert_art()

    def test_web_download_install(self):
        result = webinstall.dispatch('game.apk', source='https://example.org/game.apk')
        self.assertEqual(result['kind'], 'apk')
        self.assert_art()

    def test_refresh_api(self):
        with patch.object(server, 'ensure_master'), \
                patch.object(server, 'start_job', side_effect=lambda label, work: work()), \
                patch.object(android, 'refresh_art', return_value=[]) as refresh:
            self.assertEqual(server.android({'action':'refresh-art', 'all':True}), {'apps':[]})
        refresh.assert_called_once_with(None)

    def test_source_search_shared_installer_contract(self):
        # Source workers hand their download and optional images to this public seam.
        android.install('game.apk', name='Example', source='source-search', artwork={'banner': b'fixture'})
        self.assert_art()

    def test_native_title_install(self):
        with tempfile.TemporaryDirectory() as root:
            plan = {'id':'Example','name':'Example','root':root,'size':3,'target':'game.exe',
                    'runtime':'proton-experimental','source':'example.zip'}
            def ssh(cmd, **kwargs):
                if 'test -d' in cmd: return ''
                if 'steamos-prepare-upload' in cmd: return '{"directory":"/home/steamos/devkit-game/Example"}'
                if 'steam-client-create-shortcut' in cmd: return '{"success":"registered"}'
                return ''
            with patch.object(titles, 'ssh', side_effect=ssh), patch.object(titles, 'ensure_utils'), \
                    patch.object(titles, '_copy_tree'), patch.object(titles, '_rsync', return_value=True):
                result = titles._install(plan, lambda *args: None)
        self.assertEqual(result['shortcut'], 3346865537)
        self.assert_art()
        config = next(c.args for c in self.api.call_args_list if c.args[0] == 'configure')
        self.assertEqual(json.loads(config[8])['category'], 'Sideloaded')
        self.assertEqual(config[3:5], ('',''))  # Never replace devkit's executable/runtime wiring.

    def test_native_renamed_shortcut_uses_saved_identity(self):
        self.api.side_effect = lambda *args, **kw: '[{"appid":42,"name":"Renamed"}]'
        with patch.object(titles, 'ssh', return_value='{"shortcut":42}'):
            self.assertEqual(titles._library_shortcut('Original', 'Original'), 42)

    def test_native_failure_removes_new_blank_shortcut(self):
        with tempfile.TemporaryDirectory() as root:
            plan = {'id':'Example','name':'Example','root':root,'size':3,'target':'game.exe',
                    'runtime':'proton-experimental','source':'example.zip'}
            def ssh(cmd, **kwargs):
                if 'steamos-prepare-upload' in cmd: return '{"directory":"/home/steamos/devkit-game/Example"}'
                if 'steam-client-create-shortcut' in cmd: return '{"success":"registered"}'
                return ''
            with patch.object(titles, 'ssh', side_effect=ssh), patch.object(titles, 'ensure_utils'), \
                    patch.object(titles, '_copy_tree'), patch.object(titles, '_rsync', return_value=True), \
                    patch.object(android, 'apply_library', side_effect=android.FrameError('render failed')):
                with self.assertRaisesRegex(android.FrameError, 'render failed'):
                    titles._install(plan, lambda *args: None)
        self.assertIn(('remove', '3346865537'), [c.args for c in self.api.call_args_list])

    def test_refresh_does_not_reinstall_or_stop(self):
        meta = {**self.info, 'instance':2800000001, 'shortcut':3346865537, 'flatscreen':False}
        with patch.object(android, '_meta_or_fail', return_value=meta), \
                patch.object(android, 'ssh', side_effect=lambda cmd, **kw: json.dumps({'icon_png':''}) if cmd == 'python3 -' else '/home/steamos'), \
                patch.object(android, 'stop') as stop, patch.object(android, '_copy') as copy:
            android.refresh_art(self.info['package'])
        stop.assert_not_called(); copy.assert_not_called(); self.assert_art()

    def test_all_refresh_reports_partial_failures(self):
        import http.client
        with patch.object(android, 'list_apps', return_value=[{'package':'org.a.game'},{'package':'org.b.game'}]), \
                patch.object(android, '_meta_or_fail', side_effect=[http.client.RemoteDisconnected('gone'),
                                                                    AttributeError('odd')]):
            result=android.refresh_art()
        self.assertEqual(len(result),2)
        self.assertTrue(all('error' in a for a in result))

    def test_render_failure_cannot_report_success(self):
        self.api.side_effect = lambda *args, **kw: '3346865537' if args[0]=='add' else '{"paths":{}}'
        with self.assertRaisesRegex(android.FrameError,'every slot'):
            android.install('game.apk')


class Renderer(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the canvas contract fixture')
    def test_canvas_slots_and_title_placement(self):
        subprocess.run(['node', str(ROOT / 'tests/fixtures/library/check-renderer.js')],
                       cwd=str(ROOT), check=True, capture_output=True, timeout=30)

    def test_desktop_packages_include_renderer_and_settings(self):
        config = json.loads((ROOT / 'app/package.json').read_text())
        resources = {r['from']: r['filter'] for r in config['build']['extraResources']}
        self.assertIn('*.js', resources['../ui'])
        self.assertIn('*.js', resources['../frame/android'])


class SteamGridDB(unittest.TestCase):
    def test_no_key_is_silent_and_offline(self):
        with patch.object(sgdb,'api_key',return_value=''), patch.object(sgdb,'_get') as get:
            self.assertEqual(sgdb.lookup('Game'),({},[]))
        get.assert_not_called()

    def test_exact_match_and_top_votes_per_slot(self):
        calls=[]
        def get(path,key,deadline=None):
            calls.append(path)
            if 'search' in path: return [{'id':1,'name':'Other Game'},{'id':2,'name':'Game'}]
            dims=(600,900) if '600x900' in path else (920,430)
            return [{'url':'https://example.org/low.png','score':2,'width':dims[0],'height':dims[1]},
                    {'url':'https://example.org/top.png','score':20,'width':dims[0],'height':dims[1]},
                    {'url':'https://example.org/nsfw.png','score':99,'nsfw':True,'width':dims[0],'height':dims[1]}]
        with patch.object(sgdb,'api_key',return_value='test-key'),patch.object(sgdb,'_get',side_effect=get):
            images,warnings=sgdb.lookup('Game VR')
        self.assertEqual(set(images),set(artwork.SLOTS));self.assertEqual(warnings,[])
        self.assertTrue(all(url.endswith('/top.png') for url in images.values()))
        self.assertTrue(all('/game/2?' in p for p in calls[1:]))

    def test_unicode_titles_match_exactly_and_symbols_never_match_all(self):
        self.assertEqual(sgdb._name('ビートセイバー VR!'), 'ビートセイバーvr')
        with patch.object(sgdb,'api_key',return_value='test-key'), \
                patch.object(sgdb,'_get',return_value=[{'id':1,'name':'Unrelated'},{'id':2,'name':'!!!'}]) as get:
            self.assertEqual(sgdb.lookup('★★★'),({},[]))
            get.assert_not_called()
            self.assertEqual(sgdb.lookup('ビートセイバー'),({},[]))
        with patch.object(sgdb,'api_key',return_value='test-key'), \
                patch.object(sgdb,'_get',side_effect=AttributeError("'list' object has no attribute 'get'")):
            self.assertEqual(sgdb.lookup('Game')[0],{})

    def test_wrong_title_and_failed_lookup_fall_back(self):
        with patch.object(sgdb,'api_key',return_value='test-key'),patch.object(sgdb,'_get',return_value=[{'id':1,'name':'Unrelated'}]):
            self.assertEqual(sgdb.lookup('Game'),({},[]))
        with patch.object(sgdb,'api_key',return_value='test-key'),patch.object(sgdb,'_get',side_effect=OSError('private-secret')):
            result=sgdb.lookup('Game')
        self.assertNotIn('private-secret',str(result));self.assertEqual(result[0],{})

    def test_settings_never_return_key(self):
        with tempfile.TemporaryDirectory() as root, patch.object(sgdb,'settings_path',return_value=Path(root)/'settings.json'), \
                patch.dict(sgdb.os.environ,{},clear=True):
            result=sgdb.save_settings({'steamgriddb_api_key':'secret-fixture'})
            self.assertTrue(result['steamgriddb_configured'])
            self.assertNotIn('secret-fixture',json.dumps(result))
            self.assertEqual(sgdb.api_key(),'secret-fixture')
            if sys.platform!='win32':self.assertEqual((Path(root)/'settings.json').stat().st_mode&0o777,0o600)
            sgdb.save_settings({'steamgriddb_api_key':''})
            self.assertFalse(sgdb.settings()['steamgriddb_configured'])


if __name__=='__main__':unittest.main()
