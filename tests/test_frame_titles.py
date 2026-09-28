"""frame_titles without a headset: executable headers, launch targets, zips, runtimes."""
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ui'))
import frame_titles  # noqa: E402
from frame_titles import FrameError  # noqa: E402


def elf(machine, e_type=3, interp=True, pad=0):
    """A 64-bit little-endian ELF header plus one program header (PT_INTERP or PT_LOAD)."""
    ident = b'\x7fELF' + bytes([2, 1, 1]) + b'\0' * 9
    header = ident + struct.pack('<HHIQQQIHHHHHH', e_type, machine, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    phdr = struct.pack('<IIQQQQQQ', 3 if interp else 1, 4, 0, 0, 0, 0, 0, 0)
    return header + phdr + b'\0' * pad


def pe(machine, dll=False, pad=0):
    """An MZ stub pointing at a PE signature and COFF header."""
    mz = b'MZ' + b'\0' * 0x3A + struct.pack('<I', 0x40)
    coff = b'PE\0\0' + struct.pack('<HHIIIHH', machine, 1, 0, 0, 0, 0xF0, 0x2022 if dll else 0x0022)
    return mz + coff + b'\0' * pad


class Classify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def check(self, data, name='f'):
        p = os.path.join(self.dir, name)
        with open(p, 'wb') as f:
            f.write(data)
        return frame_titles.classify(p)

    def test_elf_machines(self):
        self.assertEqual(self.check(elf(0xB7)), {'format': 'elf', 'arch': 'arm64', 'exe': True})
        self.assertEqual(self.check(elf(0x3E))['arch'], 'x86_64')
        self.assertEqual(self.check(elf(0x3E, e_type=2, interp=False))['exe'], True)   # ET_EXEC

    def test_shared_library_is_not_a_program(self):
        self.assertFalse(self.check(elf(0xB7, interp=False))['exe'])

    def test_pe_machines(self):
        self.assertEqual(self.check(pe(0x8664)), {'format': 'pe', 'arch': 'x86_64', 'exe': True})
        self.assertEqual(self.check(pe(0xAA64))['arch'], 'arm64')
        self.assertEqual(self.check(pe(0x14C))['arch'], 'x86')
        self.assertFalse(self.check(pe(0x8664, dll=True))['exe'])

    def test_lying_headers_are_not_programs(self):
        bad = bytearray(elf(0xB7))
        struct.pack_into('<HH', bad, 54, 1, 1)         # one-byte program header entries
        self.assertFalse(self.check(bytes(bad))['exe'])
        self.assertIsNone(self.check(b'MZ' + b'\0' * 0x3A + struct.pack('<I', 0xFFFFFFF0)))

    def test_scripts_and_data(self):
        self.assertEqual(self.check(b'#!/bin/sh\necho hi\n')['format'], 'script')
        self.assertIsNone(self.check(b'MZ-not-really'))
        self.assertIsNone(self.check(b'just text'))


class Targets(unittest.TestCase):
    def tree(self, files):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        for rel, data in files.items():
            p = os.path.join(root, *rel.split('/'))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'wb') as f:
                f.write(data)
        return root

    def plan(self, files, name):
        return frame_titles.inspect(self.tree(files), name)

    def test_unity_windows_build(self):
        # UnityCrashHandler64.exe is bigger than the game's own exe; the name decides.
        p = self.plan({'MyGame/MyGame.exe': pe(0x8664, pad=600),
                       'MyGame/UnityCrashHandler64.exe': pe(0x8664, pad=5000),
                       'MyGame/UnityPlayer.dll': pe(0x8664, dll=True, pad=9000),
                       'MyGame/MyGame_Data/Plugins/x86_64/steam_api64.dll': pe(0x8664, dll=True)}, 'MyGame')
        self.assertEqual(p['target'], 'MyGame.exe')
        self.assertEqual(p['runtime'], 'proton-experimental')
        self.assertEqual(p['runtimes'], ['proton-experimental', 'proton-stable'])
        crash = next(c for c in p['candidates'] if c['path'] == 'UnityCrashHandler64.exe')
        self.assertTrue(crash['skip'])
        self.assertNotIn('UnityPlayer.dll', [c['path'] for c in p['candidates']])

    def test_unreal_prefers_top_level_bootstrap(self):
        p = self.plan({'Game.exe': pe(0x8664, pad=200),
                       'Game/Binaries/Win64/Game-Win64-Shipping.exe': pe(0x8664, pad=9000),
                       'Engine/Extras/Redist/en-us/UEPrereqSetup_x64.exe': pe(0x8664, pad=9000)}, 'Game')
        self.assertEqual(p['target'], 'Game.exe')

    def test_installers_lose_to_the_game(self):
        p = self.plan({'setup.exe': pe(0x8664, pad=9000), 'unins000.exe': pe(0x14C, pad=9000),
                       '_CommonRedist/vc_redist.x64.exe': pe(0x8664, pad=9000),
                       'Tool.exe': pe(0x8664)}, 'Something')
        self.assertEqual(p['target'], 'Tool.exe')

    def test_arm64_linux_build(self):
        p = self.plan({'game.arm64': elf(0xB7, pad=100), 'lib/libfoo.so': elf(0xB7, interp=False, pad=900)}, 'game')
        self.assertEqual((p['target'], p['runtime']), ('game.arm64', 'SteamLinuxRuntime_4-arm64'))
        self.assertEqual(p['runtimes'], ['SteamLinuxRuntime_4-arm64'])

    def test_x86_64_linux_build_warns_it_may_not_start(self):
        p = self.plan({'game.x86_64': elf(0x3E)}, 'game')
        self.assertEqual(p['runtime'], 'SteamLinuxRuntime_4')
        self.assertIn("won't start", p['note'])

    def test_native_arm64_beats_x86_64(self):
        p = self.plan({'game.x86_64': elf(0x3E, pad=900), 'game.arm64': elf(0xB7)}, 'game')
        self.assertEqual(p['target'], 'game.arm64')

    def test_top_level_script_beats_nested_binary(self):
        p = self.plan({'run.sh': b'#!/bin/sh\nexec bin/game\n', 'bin/game': elf(0xB7)}, 'game')
        self.assertEqual(p['target'], 'run.sh')
        self.assertEqual(p['runtime'], 'SteamLinuxRuntime_4-arm64')

    def test_binary_beside_script_wins(self):
        p = self.plan({'start.sh': b'#!/bin/sh\n', 'game': elf(0x3E)}, 'game')
        self.assertEqual(p['target'], 'game')

    def test_other_architectures_are_refused(self):
        with self.assertRaisesRegex(FrameError, 'x86 Linux'):
            self.plan({'game': elf(0x03)}, 'game')
        with self.assertRaisesRegex(FrameError, 'no Linux or Windows program'):
            self.plan({'readme.txt': b'hello'}, 'game')

    def test_runtime_override_and_exe_choice(self):
        p = self.plan({'A.exe': pe(0x8664), 'B.exe': pe(0x8664)}, 'A')
        frame_titles._choose(p, 'B.exe', 'proton-stable')
        self.assertEqual((p['target'], p['runtime']), ('B.exe', 'proton-stable'))
        with self.assertRaises(FrameError):
            frame_titles._choose(p, 'A.exe', 'SteamLinuxRuntime_4-arm64')
        with self.assertRaises(FrameError):
            frame_titles._choose(p, '../outside.exe')

    def test_exe_path_from_above_the_unwrapped_folder(self):
        # A manifest names the program as it is in the archive: Game/B.exe, not B.exe.
        p = self.plan({'Game/A.exe': pe(0x8664), 'Game/B.exe': pe(0x8664)}, 'Game')
        self.assertEqual(p['unwrapped'], 'Game')
        frame_titles._choose(p, 'Game/B.exe')
        self.assertEqual(p['target'], 'B.exe')
        frame_titles._choose(p, 'Game\\A.exe')
        self.assertEqual(p['target'], 'A.exe')
        with self.assertRaises(FrameError):
            frame_titles._choose(p, 'Game/../../outside.exe')

    def test_root_relative_path_wins_over_archive_prefix(self):
        # Game/Game/A.exe and Game/A.exe: 'Game/A.exe' is a real path under the root Game/.
        p = self.plan({'Game/Game/A.exe': pe(0x8664), 'Game/A.exe': pe(0x8664)}, 'Game')
        self.assertEqual(p['unwrapped'], 'Game')
        frame_titles._choose(p, 'Game/A.exe')
        self.assertEqual(p['target'], 'Game/A.exe')

    @unittest.skipIf(os.name == 'nt', 'needs symlinks')
    def test_prefix_is_taken_before_staging(self):
        # A folder with a link is staged into a temporary copy; the prefix still names
        # the folders stepped into in the original.
        d = self.tree({'Game/A.exe': pe(0x8664)})
        os.symlink('A.exe', os.path.join(d, 'Game', 'link.exe'))
        p = frame_titles.inspect(d, 'Game')
        try:
            self.assertEqual(p['unwrapped'], 'Game')
            self.assertTrue(p['work'])
        finally:
            frame_titles.discard(p)


class Zips(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def zip(self, members, name='Cool Game-v1.2-win64.zip'):
        p = os.path.join(self.dir, name)
        with zipfile.ZipFile(p, 'w') as z:
            for n, data in members.items():
                z.writestr(n, data)
        return p

    def test_wrapper_folder_and_name(self):
        plan = frame_titles.inspect(self.zip({'Cool Game/Cool Game.exe': pe(0x8664),
                                              '__MACOSX/Cool Game/._Cool Game.exe': b'x'}))
        try:
            self.assertEqual(plan['name'], 'Cool Game')
            self.assertEqual(plan['id'], 'Cool_Game')
            self.assertEqual(plan['target'], 'Cool Game.exe')
            self.assertTrue(os.path.isfile(os.path.join(plan['root'], 'Cool Game.exe')))
        finally:
            frame_titles.discard(plan)
        self.assertFalse(os.path.exists(plan['work']))

    def test_zip_slip_is_refused(self):
        for bad in ('../evil.exe', 'ok/../../evil.exe', '/abs/evil.exe', 'C:/evil.exe', '..\\evil.exe'):
            with self.subTest(bad=bad):
                out = tempfile.mkdtemp(dir=self.dir)
                with self.assertRaisesRegex(FrameError, 'absolute|climbs'):
                    frame_titles.extract_zip(self.zip({bad: pe(0x8664)}, 'bad.zip'), out)
                self.assertFalse(os.path.exists(os.path.join(self.dir, 'evil.exe')))

    def test_link_out_of_the_zip_is_refused(self):
        p = os.path.join(self.dir, 'link.zip')
        with zipfile.ZipFile(p, 'w') as z:
            info = zipfile.ZipInfo('game/escape')
            info.external_attr = (0o120777 << 16)
            z.writestr(info, '../../etc/passwd')
        with self.assertRaisesRegex(FrameError, 'outside'):
            frame_titles.extract_zip(p, tempfile.mkdtemp(dir=self.dir))

    def link_zip(self, members):
        """members: (name, data, is_link) in order."""
        p = os.path.join(self.dir, 'links.zip')
        with zipfile.ZipFile(p, 'w') as z:
            for name, data, is_link in members:
                info = zipfile.ZipInfo(name)
                info.external_attr = ((0o120777 if is_link else 0o100644) << 16)
                z.writestr(info, data)
        return p

    def test_chained_links_cannot_escape(self):
        # alias -> . ; alias/alias/escape -> ../.. ; escape/victim would land outside if links were real.
        p = self.link_zip([('alias', '.', True), ('alias/alias/escape', '../..', True), ('escape/victim', b'x', False)])
        out = tempfile.mkdtemp(dir=self.dir)
        frame_titles.extract_zip(p, out)
        self.assertTrue(os.path.isfile(os.path.join(out, 'escape', 'victim')))    # stayed inside
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'victim')))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.dir), 'victim')))
        for root, dirs, files in os.walk(out):
            self.assertFalse([n for n in dirs + files if os.path.islink(os.path.join(root, n))])

    def test_folder_links_are_dropped_and_order_does_not_matter(self):
        # b -> a/file listed before a -> dir; and a folder link that would contain itself.
        p = self.link_zip([('dir/file', b'data', False), ('b', 'a/file', True), ('a', 'dir', True),
                           ('dir/sub/loop', '../../a', True)])
        out = tempfile.mkdtemp(dir=self.dir)
        frame_titles.extract_zip(p, out)
        with open(os.path.join(out, 'b'), 'rb') as f:
            self.assertEqual(f.read(), b'data')
        self.assertFalse(os.path.lexists(os.path.join(out, 'a')))
        self.assertFalse(os.path.lexists(os.path.join(out, 'dir', 'sub', 'loop')))

    def test_link_components_resolve_before_parent_steps(self):
        # alias -> dirlink/../game.exe, dirlink -> deep/subdir: that's deep/game.exe, not game.exe.
        p = self.link_zip([('deep/subdir/x', b'', False), ('deep/game.exe', b'deep one', False),
                           ('game.exe', b'top one', False), ('dirlink', 'deep/subdir', True),
                           ('alias', 'dirlink/../game.exe', True)])
        out = tempfile.mkdtemp(dir=self.dir)
        frame_titles.extract_zip(p, out)
        with open(os.path.join(out, 'alias'), 'rb') as f:
            self.assertEqual(f.read(), b'deep one')

    def test_many_links_to_one_file_count_against_the_limit(self):
        # The zip (1 KB) and the copies (15 KB) each fit under the limit; together they don't.
        members = [('big', b'x' * 1000, False)] + [(f'alias{i}', 'big', True) for i in range(15)]
        old = frame_titles.MAX_UNPACKED
        frame_titles.MAX_UNPACKED = 15500
        out = tempfile.mkdtemp(dir=self.dir)
        try:
            with self.assertRaisesRegex(FrameError, 'links would copy'):
                frame_titles.extract_zip(self.link_zip(members), out)
        finally:
            frame_titles.MAX_UNPACKED = old
        self.assertEqual(os.listdir(out), ['big'])   # refused before copying any link

    def test_oversized_link_is_refused(self):
        p = self.link_zip([('big', 'x' * 5000, True)])
        with self.assertRaisesRegex(FrameError, 'oversized link'):
            frame_titles.extract_zip(p, tempfile.mkdtemp(dir=self.dir))

    @unittest.skipIf(os.name == 'nt', 'needs symlinks')
    def test_unwrap_never_steps_through_a_link(self):
        # A folder whose only entry links elsewhere (a junction on Windows) stays the boundary.
        outside, game = os.path.join(self.dir, 'outside'), os.path.join(self.dir, 'Game')
        os.makedirs(outside)
        os.makedirs(game)
        with open(os.path.join(outside, 'Other.exe'), 'wb') as f:
            f.write(pe(0x8664))
        os.symlink(outside, os.path.join(game, 'inner'))
        with self.assertRaisesRegex(FrameError, 'no Linux or Windows program'):
            frame_titles.inspect(game)

    @unittest.skipIf(os.name == 'nt', 'needs symlinks')
    def test_folder_with_outside_link_is_staged_without_it(self):
        game, secret = os.path.join(self.dir, 'Game'), os.path.join(self.dir, 'secret')
        os.makedirs(game)
        os.makedirs(secret)
        with open(os.path.join(secret, 'key'), 'wb') as f:
            f.write(b'private')
        with open(os.path.join(game, 'Game.exe'), 'wb') as f:
            f.write(pe(0x8664))
        os.symlink(secret, os.path.join(game, 'leak'))
        os.symlink(os.path.join(secret, 'key'), os.path.join(game, 'leak-file'))
        os.symlink('Game.exe', os.path.join(game, 'Alias.exe'))
        plan = frame_titles.inspect(game)
        try:
            self.assertNotEqual(os.path.realpath(plan['root']), os.path.realpath(game))
            self.assertEqual(sorted(os.listdir(plan['root'])), ['Alias.exe', 'Game.exe'])
            self.assertFalse(os.path.islink(os.path.join(plan['root'], 'Alias.exe')))
        finally:
            frame_titles.discard(plan)

    def test_links_become_copies(self):
        # No symlinks on disk (Windows may not allow them); the library a link names is still there.
        p = self.link_zip([('game/lib/libfoo.so.1.2', b'ELF-ish', False), ('game/lib/libfoo.so.1', 'libfoo.so.1.2', True),
                           ('game/lib/libfoo.so', 'libfoo.so.1', True), ('game/dangling', 'nowhere', True)])
        out = tempfile.mkdtemp(dir=self.dir)
        frame_titles.extract_zip(p, out)
        for name in ('libfoo.so.1', 'libfoo.so'):
            path = os.path.join(out, 'game', 'lib', name)
            self.assertFalse(os.path.islink(path))
            with open(path, 'rb') as f:
                self.assertEqual(f.read(), b'ELF-ish')
        self.assertFalse(os.path.lexists(os.path.join(out, 'game', 'dangling')))

    def test_drive_qualified_parts_are_refused(self):
        for bad in ('sub/C:../C:../victim.txt', 'game/file.exe:stream'):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(FrameError, 'drive or stream'):
                    frame_titles.extract_zip(self.zip({bad: b'x'}, 'drive.zip'), tempfile.mkdtemp(dir=self.dir))

    def test_absurd_size_is_refused(self):
        p = self.zip({'game.exe': pe(0x8664)}, 'bomb.zip')
        old = frame_titles.MAX_UNPACKED
        frame_titles.MAX_UNPACKED = 10
        try:
            with self.assertRaisesRegex(FrameError, 'looks wrong'):
                frame_titles.extract_zip(p, tempfile.mkdtemp(dir=self.dir))
        finally:
            frame_titles.MAX_UNPACKED = old

    def test_not_a_zip(self):
        p = os.path.join(self.dir, 'x.zip')
        with open(p, 'wb') as f:
            f.write(b'nope')
        with self.assertRaisesRegex(FrameError, 'not a readable zip'):
            frame_titles.inspect(p)


class Names(unittest.TestCase):
    def test_title_id(self):
        self.assertEqual(frame_titles.title_id('Hollow Knight: Silksong!'), 'Hollow_Knight_Silksong')
        self.assertEqual(frame_titles.title_id('steam'), 'steam_game')     # Valve's reserved sideload names
        self.assertEqual(frame_titles.title_id('Devkit Steam'), 'Devkit_Steam')
        self.assertEqual(frame_titles.title_id('--rm -rf /'), 'rm_rf')
        self.assertEqual(len(frame_titles.title_id('x' * 200)), 64)
        with self.assertRaises(FrameError):
            frame_titles.title_id('!!!')

    def test_title_id_is_one_steam_accepts(self):
        # The Frame's Steam refused "fc-smoke-exe" ("missing/invalid arguments") and took
        # "FCSmokeProbe" (2026-09-27): Valve's client allows ^[A-Za-z_][A-Za-z0-9_.]+$ only.
        self.assertEqual(frame_titles.title_id('Half-Life 2'), 'Half_Life_2')
        self.assertEqual(frame_titles.title_id('fc-smoke-exe'), 'fc_smoke_exe')
        self.assertEqual(frame_titles.title_id('2048'), '_2048')
        self.assertEqual(frame_titles.title_id('X'), 'X_game')
        self.assertEqual(frame_titles.title_id('devkit-steam'), 'devkit_steam')
        for name in ('Half-Life 2', '2048', 'X', 'steam', 'a' * 90, 'Ünïcödé game', '9' * 70):
            gid = frame_titles.title_id(name)
            self.assertRegex(gid, r'^[A-Za-z_][A-Za-z0-9_.]+$', name)
            self.assertTrue(frame_titles.NEW_ID_RE.match(gid) and frame_titles.ID_RE.match(gid), gid)

    def test_display_name(self):
        self.assertEqual(frame_titles.display_name('MyGame-linux-arm64.zip'), 'MyGame')
        self.assertEqual(frame_titles.display_name('Portal 2.zip'), 'Portal 2')
        self.assertEqual(frame_titles.display_name('Game_v1.0.3_Win64.zip'), 'Game')


class Parms(unittest.TestCase):
    def test_proton_parms(self):
        p = frame_titles.shortcut_parms('Cool_Game', '/home/steamos/devkit-game/Cool_Game',
                                        'Cool Game.exe', 'proton-experimental')
        self.assertEqual(p, {'gameid': 'Cool_Game', 'directory': '/home/steamos/devkit-game/Cool_Game',
                             'argv': ['"Cool Game.exe"'], 'env': {},
                             'settings': {'steam_play': '1', 'steam_play_debug': '0',
                                          'steam_play_debug_version': '2019',
                                          'compat_tool': 'proton-experimental'},
                             'clear_settings': True, 'force_appid': '', 'lepton_args': ''})
        json.dumps(p)

    def test_linux_parms(self):
        p = frame_titles.shortcut_parms('g', '/home/steamos/devkit-game/g', 'bin/game', 'SteamLinuxRuntime_4-arm64')
        self.assertEqual(p['argv'], ['bin/game'])
        self.assertEqual(p['settings'], {'steam_play': '0', 'compat_tool': 'SteamLinuxRuntime_4-arm64'})

    def test_cleanup_names_only_this_title(self):
        # A glob like Game-*.json would also delete Game-Deluxe's files.
        self.assertEqual(frame_titles._json_files('Game').split(),
                         ['devkit-game/Game-argv.json', 'devkit-game/Game-env.json',
                          'devkit-game/Game-settings.json', 'devkit-game/Game-framecontrol.json'])

    def test_launch_needs_steam_to_answer(self):
        # steam-devkit-rpc exits 0 after a timeout; only its 'success' line means Steam took it.
        calls = []
        old = frame_titles.ssh, frame_titles._check_id, frame_titles.ensure_utils
        frame_titles._check_id, frame_titles.ensure_utils = (lambda g: g), (lambda: False)
        try:
            frame_titles.ssh = lambda cmd, **kw: calls.append(cmd) or 'Found steam client pid 1\ntimeout\n'
            with self.assertRaisesRegex(FrameError, "didn't confirm"):
                frame_titles.launch('Game')
            frame_titles.ssh = lambda cmd, **kw: 'Found steam client pid 1\nsuccess\n{}'
            self.assertEqual(frame_titles.launch('Game'), {'id': 'Game'})
        finally:
            frame_titles.ssh, frame_titles._check_id, frame_titles.ensure_utils = old
        self.assertIn('steam-devkit-rpc run-game gameid=Game', calls[0])

    def test_remove_waits_for_installs(self):
        with frame_titles._install_lock:
            with self.assertRaisesRegex(FrameError, 'install is running'):
                frame_titles.remove('Game')

    def test_vendored_utils_are_present(self):
        for name in ('steamos-prepare-upload', 'steam-client-create-shortcut', 'steam-devkit-rpc',
                     'steamos-delete', 'devkit_utils/__init__.py', 'LICENSE'):
            self.assertTrue(os.path.isfile(os.path.join(frame_titles.UTILS_LOCAL, name)), name)
        self.assertEqual(len(frame_titles.utils_stamp()), 20)


if __name__ == '__main__':
    unittest.main()
