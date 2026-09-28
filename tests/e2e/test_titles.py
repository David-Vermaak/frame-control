"""Sideloaded titles (ui/frame_titles.py) through the server's HTTP API, against
Valve's devkit-utils talking to the fake Steam client."""
import hashlib
import http.server
import json
import os
import platform
import subprocess
import sys
import threading
import unittest
import zipfile

import harness
import tiny_programs
from harness import HOME, ROOT, ctl, exists, install_title, launches, ok, ssh, state, wait_for

harness.require()

GAMES = f'{HOME}/devkit-game'
# The host container shares the fake Frame's kernel, so a program of this machine's
# architecture really runs there. The other one fails to exec, unless QEMU is
# registered with binfmt_misc, which runs it emulated.
NATIVE = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x86_64'}.get(platform.machine())


class Titles(harness.FrameTestCase):
    def game_zip(self, name='Cool Game-v1.2-win64.zip', extra=b''):
        path = self.path(name)
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('Cool Game/CoolGame.exe', tiny_programs.pe_x86_64())
            z.writestr('Cool Game/CoolGame_Data/level0', b'level data' + extra)
        return path

    def folder(self, kind, name):
        os.makedirs(self.path(name))
        return tiny_programs.write(self.path(name), kind), self.path(name)

    def assert_installed(self, gid, runtime, target):
        game = state()['devkit_games'][gid]
        self.assertEqual(game['settings']['compat_tool'], runtime)
        self.assertEqual(game['argv'], [target])
        steam = state()['steam']
        shortcut = next(s for s in steam['shortcuts'] if s['devkit_gameid'] == gid)
        self.assertEqual(steam['compat_tools'][str(shortcut['appid'])], runtime)
        self.assertEqual(ssh(f'stat -c %a {GAMES}/{gid}/{target}').strip(), '755')
        listed = {t['id']: t for t in ok('GET', '/api/titles')['titles']}
        self.assertEqual(listed[gid]['runtime'], runtime)
        self.assertTrue(listed[gid]['frame_control'])
        return shortcut

    def test_zip_with_a_windows_exe(self):
        plan, job = install_title(self.game_zip())
        self.assertEqual((plan['name'], plan['id'], plan['target']), ('Cool Game', 'Cool_Game', 'CoolGame.exe'))
        self.assertEqual(plan['runtime'], 'proton-experimental')
        self.assertIsNone(job['error'], job)
        self.assertEqual(job['title']['runtime_label'], 'Proton Experimental')
        self.assert_installed('Cool_Game', 'proton-experimental', 'CoolGame.exe')
        game = state()['devkit_games']['Cool_Game']
        self.assertEqual(game['settings'], {'steam_play': '1', 'steam_play_debug': '0',
                                            'steam_play_debug_version': '2019', 'compat_tool': 'proton-experimental'})
        self.assertTrue(exists(f'{GAMES}/Cool_Game/CoolGame_Data/level0'))
        self.assertTrue(exists(f'{HOME}/devkit-utils/.frame-control-stamp'))

    def test_folder_with_an_arm64_build_runs_natively(self):
        _, folder = self.folder('arm64', 'Tiny Arm Game')
        plan, job = install_title(folder)
        self.assertEqual(plan['runtime'], 'SteamLinuxRuntime_4-arm64')
        self.assertIsNone(job['error'], job)
        self.assert_installed('Tiny_Arm_Game', 'SteamLinuxRuntime_4-arm64', 'fc-smoke-arm64')
        ok('POST', '/api/titles', {'action': 'launch', 'id': 'Tiny_Arm_Game'})
        run = launches('devkit')[-1]
        # No runtime prefix: Steam ran the aarch64 build directly on the Frame.
        self.assertEqual(run['command'], f'{GAMES}/Tiny_Arm_Game/fc-smoke-arm64')
        if NATIVE == 'arm64':
            self.assertIsNotNone(run['pid'], run)
            done = wait_for(lambda: launches('devkit')[-1].get('exit') is not None and launches('devkit')[-1],
                            30, 'the arm64 test program to exit')
            self.assertEqual(done['exit'], 0)

    def test_single_exe_upload_launch_and_remove(self):
        exe = tiny_programs.write(self.tmp, 'exe')
        plan, job = install_title(exe)
        self.assertEqual(plan['id'], 'fc_smoke_exe')
        self.assertIsNone(job['error'], job)
        shortcut = self.assert_installed('fc_smoke_exe', 'proton-experimental', 'fc-smoke-exe.exe')

        ok('POST', '/api/titles', {'action': 'launch', 'id': 'fc_smoke_exe'})
        run = launches('devkit')[-1]
        self.assertTrue(run['started'])
        self.assertEqual(run['command'], f'proton waitforexitandrun "{GAMES}/fc_smoke_exe/fc-smoke-exe.exe"')
        prefix = f"{HOME}/.local/share/Steam/steamapps/compatdata/{shortcut['appid']}"
        self.assertTrue(exists(prefix))

        ok('POST', '/api/titles', {'action': 'remove', 'id': 'fc_smoke_exe'})
        after = state()
        self.assertNotIn('fc_smoke_exe', after['devkit_games'])
        self.assertEqual(after['steam']['shortcuts'], [])
        for gone in (f'{GAMES}/fc_smoke_exe', prefix, *(f'{GAMES}/fc_smoke_exe-{k}.json'
                                                        for k in ('argv', 'env', 'settings', 'framecontrol'))):
            self.assertFalse(exists(gone), gone)
        self.assertEqual(ok('GET', '/api/titles')['titles'], [])

    def test_names_steam_would_refuse_are_made_safe(self):
        # Steam's create-shortcut takes ^[A-Za-z_][A-Za-z0-9_.]+$ only (device, 2026-09-27).
        exe = self.path('2048-Deluxe.exe')
        with open(exe, 'wb') as f:
            f.write(tiny_programs.pe_x86_64())
        plan, job = install_title(exe)
        self.assertEqual(plan['id'], '_2048_Deluxe')
        self.assertIsNone(job['error'], job)
        self.assert_installed('_2048_Deluxe', 'proton-experimental', '2048-Deluxe.exe')

    def test_x86_64_linux_build_needs_a_runtime_the_frame_lacks(self):
        _, folder = self.folder('x86_64', 'Tiny PC Game')
        plan, job = install_title(folder)
        self.assertEqual(plan['runtime'], 'SteamLinuxRuntime_4')
        self.assertIsNone(job['error'], job)
        appid = self.assert_installed('Tiny_PC_Game', 'SteamLinuxRuntime_4', 'fc-smoke-x86_64')['appid']
        # Steam answers the launch, then doesn't start it (docs/sideloading.md).
        ok('POST', '/api/titles', {'action': 'launch', 'id': 'Tiny_PC_Game'})
        run = launches('devkit')[-1]
        self.assertFalse(run['started'])
        self.assertEqual(run['message'], f'Tool 4183110 "Steam Linux Runtime 4.0" is found for appID {appid}, '
                                         'but is not installed')
        # The headset smoke test finds this in Steam's logs, where compat_log.txt has
        # binary bytes in it: only grep -a returns the line (Frame, 2026-09-27).
        self.assertIn(run['message'], ssh('grep -arshF "but is not installed" ~/.local/share/Steam/logs/'))
        # With the runtime installed, it starts.
        ctl('runtime', 'SteamLinuxRuntime_4', 'installed')
        ok('POST', '/api/titles', {'action': 'launch', 'id': 'Tiny_PC_Game'})
        run = launches('devkit')[-1]
        self.assertTrue(run['started'])
        if NATIVE == 'x86_64':
            done = wait_for(lambda: launches('devkit')[-1].get('exit') is not None and launches('devkit')[-1],
                            30, 'the x86-64 test program to exit')
            self.assertEqual(done['exit'], 0)

    def test_reinstall_with_another_runtime_keeps_one_shortcut(self):
        zip_path = self.game_zip()
        _, first = install_title(zip_path)
        self.assertIsNone(first['error'], first)
        appid = state()['devkit_games']['Cool_Game']['appid']
        _, second = install_title(zip_path, runtime='proton-stable')
        self.assertIsNone(second['error'], second)
        self.assert_installed('Cool_Game', 'proton-stable', 'CoolGame.exe')
        steam = state()['steam']
        self.assertEqual([s['appid'] for s in steam['shortcuts']], [appid])
        meta = json.loads(ssh(f'cat {GAMES}/Cool_Game-framecontrol.json'))
        self.assertEqual(meta['runtime'], 'proton-stable')

    def test_install_link_with_a_local_manifest(self):
        # frame-control://install?manifest=... as a website would link it, served from
        # this computer (FRAME_CONTROL_LOCAL_LINKS=1 lets http://127.0.0.1 through).
        site = self.path('site')
        os.makedirs(site)
        with open(self.game_zip('linkgame-win64.zip'), 'rb') as f:
            data = f.read()
        with open(os.path.join(site, 'linkgame-win64.zip'), 'wb') as f:
            f.write(data)
        class Files(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args):
                super().__init__(*args, directory=site)

            def log_message(self, *args):
                pass
        httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Files)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        base = f'http://127.0.0.1:{httpd.server_address[1]}'
        with open(os.path.join(site, 'manifest.json'), 'w') as f:
            json.dump({'schema': 'framedrop.install/v1', 'name': 'Link Game',
                       'files': [{'url': f'{base}/linkgame-win64.zip', 'sha256': hashlib.sha256(data).hexdigest(),
                                  'size': len(data), 'exe': 'Cool Game/CoolGame.exe'}]}, f)

        check = ok('POST', '/api/webinstall/check', {'manifest': f'{base}/manifest.json'})
        self.assertEqual((check['name'], check['kind'], check['size']), ('Link Game', 'title', len(data)))
        job_id = ok('POST', '/api/webinstall/start', {'id': check['id']})['job']
        job = wait_for(lambda: (lambda j: j if j['phase'] in ('done', 'error') else None)(
            ok('GET', f'/api/webinstall/job?id={job_id}')), 120, 'the link install')
        self.assertEqual(job['phase'], 'done', job)
        self.assert_installed('Link_Game', 'proton-experimental', 'CoolGame.exe')

    def test_command_line_lists_what_the_app_installed(self):
        install_title(self.game_zip())
        out = subprocess.run([sys.executable, str(ROOT / 'ui' / 'frame_titles.py'), 'list'],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([t['id'] for t in json.loads(out.stdout)], ['Cool_Game'])


if __name__ == '__main__':
    unittest.main()
