"""Local-only VR manifest, raw ZIP and independent signature checks."""
import io
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
import frame_apk
import frame_apk_vr as vr
import frame_apk_sign as signing
import frame_android
from test_frame_apk import pool


def manifest(utf8=False, launcher=False, category=None, samsung=False, split=False):
    strings = ['manifest', 'package', 'org.test.vr', 'application', 'activity', 'intent-filter',
               'action', 'category', 'name', vr.MAIN, category or next(iter(sorted(vr.VR))),
               vr.LAUNCHER, 'http://schemas.android.com/apk/res/android', 'meta-data', 'value',
               'com.samsung.android.vr.application.mode', 'vr_only']
    def start(tag, attrs=()):
        body = struct.pack('<IIHHHHHH', 0xffffffff, strings.index(tag), 20, 20, len(attrs), 0, 0, 0)
        for name, value in attrs:
            ns = 0xffffffff if name == 'package' else 12
            body += struct.pack('<IIIHBBI', ns, strings.index(name), strings.index(value), 8, 0, 3, strings.index(value))
        return struct.pack('<HHIII', 0x102, 16, 16 + len(body), 1, 0xffffffff) + body
    def end(tag):
        return struct.pack('<HHIIIII', 0x103, 16, 24, 1, 0xffffffff, 0xffffffff, strings.index(tag))
    def leaf(tag, attrs):
        return start(tag, attrs) + end(tag)
    b = pool(strings, utf8) + start('manifest', [('package', 'org.test.vr')]) + start('application')
    if samsung:
        b += leaf('meta-data', [('name', strings[15]), ('value', 'vr_only')])
    b += start('activity') + start('intent-filter') + leaf('action', [('name', vr.MAIN)])
    b += leaf('category', [('name', strings[10])])
    if split:
        b += end('intent-filter') + start('intent-filter')
    if launcher:
        b += leaf('category', [('name', vr.LAUNCHER)])
    b += end('intent-filter') + end('activity') + end('application') + end('manifest')
    return struct.pack('<HHI', 3, 8, len(b) + 8) + b


class VRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.keypath = Path(cls.tmp.name) / 'key.json'
        cls.key = signing.signing_key(cls.keypath)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_manifest_patch(self):
        for utf8 in (False, True):
            for category in vr.VR:
                original = manifest(utf8, category=category)
                result = vr.add_launcher_category(original)
                info, filters = vr.inspect(result)
                self.assertTrue(info['launchable'])
                self.assertTrue(info['vr'])
                self.assertIn(vr.LAUNCHER, filters[0]['categories'])
                self.assertEqual(struct.unpack_from('<I', result, 4)[0], len(result))
                self.assertEqual(vr.add_launcher_category(result), result)
                self.assertEqual(vr.add_launcher_category(manifest(utf8, True)), manifest(utf8, True))

    def test_filter_boundaries(self):
        self.assertFalse(vr.inspect(manifest(launcher=True, split=True))[0]['launchable'])
        self.assertTrue(vr.inspect(vr.add_launcher_category(manifest(launcher=True, split=True)))[0]['launchable'])

    def test_styled_pool(self):
        for utf8 in (False, True):
            p = bytearray(pool(['styled'], utf8))
            old_start = struct.unpack_from('<I', p, 20)[0]
            p[old_start:old_start] = struct.pack('<I', 0)
            style_start = len(p)
            p += struct.pack('<III', 0, 0, 2) + b'\xff' * 12
            struct.pack_into('<I', p, 4, len(p))
            struct.pack_into('<I', p, 16, (0x100 if utf8 else 0) | 1)
            struct.pack_into('<I', p, 12, 1)
            struct.pack_into('<II', p, 20, old_start + 4, style_start)
            result, idx = vr._append_string(bytes(p), vr.LAUNCHER)
            self.assertEqual(frame_apk._string_pool(result, 0), ['styled', vr.LAUNCHER])
            new_style = struct.unpack_from('<I', result, 24)[0]
            self.assertEqual(result[new_style:], p[style_start:])
            self.assertEqual(len(result) % 4, 0)

    def test_detection(self):
        names = {'lib/arm64-v8a/' + n for n in ('libvrapi.so', 'libopenxr_loader.so', 'libovrplatformloader.so')}
        info = vr.detection(manifest(category='android.intent.category.LAUNCHER'), names)
        self.assertTrue(info['vr'])
        self.assertTrue(info['launchable'])
        self.assertEqual(len(info['vr_issues']), 3)
        self.assertFalse(vr.detection(manifest(category=vr.LAUNCHER), set())['vr'])
        self.assertTrue(vr.detection(manifest(category=vr.LAUNCHER, samsung=True), set())['vr'])

    def test_key_cache(self):
        with patch.object(signing, '_prime', side_effect=AssertionError('regenerated')):
            self.assertEqual(signing.signing_key(self.keypath), self.key)
        if os.name != 'nt':
            self.assertEqual(self.keypath.stat().st_mode & 0o777, 0o600)

    def test_repack_sign_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / 'in.apk', Path(d) / 'out.apk'
            with zipfile.ZipFile(src, 'w') as z:
                z.writestr('AndroidManifest.xml', manifest(), compress_type=8)
                z.writestr('classes.dex', b'compress me' * 10000, compress_type=8)
                z.writestr('resources.arsc', b'1234')
                z.writestr('lib/arm64-v8a/libx.so', b'ELF' * 500000)
                z.writestr('META-INF/OLD.RSA', b'old')
                z.writestr('META-INF/MANIFEST.MF', b'old')
            with patch.object(signing, 'signing_key', return_value=self.key):
                result = frame_android.patch(src, dst, {'assets/layer.json': b'{}', 'lib/arm64-v8a/liblayer.so': b'layer'})
            self.assertEqual(result['patched'], ['launcher'])
            self.assertTrue(frame_apk.apk_info(dst)['launchable'])
            self.assertTrue(signing.verify(dst))
            def compressed(path, info):
                data = path.read_bytes()
                nl, el = struct.unpack_from('<HH', data, info.header_offset + 26)
                start = info.header_offset + 30 + nl + el
                return data[start:start + info.compress_size], start
            with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
                self.assertNotIn('META-INF/OLD.RSA', b.namelist())
                self.assertNotIn('META-INF/MANIFEST.MF', b.namelist())
                for i in b.infolist():
                    raw, start = compressed(dst, i)
                    if i.compress_type == 0:
                        self.assertEqual(start % (16384 if i.filename.endswith('.so') else 4), 0)
                    if i.filename in ('classes.dex', 'resources.arsc', 'lib/arm64-v8a/libx.so'):
                        self.assertEqual(raw, compressed(src, a.getinfo(i.filename))[0])
                _, off = compressed(dst, b.getinfo('resources.arsc'))
            original = dst.read_bytes()
            data = bytearray(original)
            eo, cd = signing._eocd(data)
            size = struct.unpack_from('<Q', data, cd - 24)[0]
            block_start = cd - size - 8
            value = data[block_start + 20:cd - 24]
            signer = signing._parts(signing._parts(value)[0])[0]
            signed, signatures, pub = signing._parts(signer)
            signature = signing._parts(signing._parts(signatures)[0][4:])[0]
            sig_offset = data.index(signature, block_start)
            data[sig_offset] ^= 1
            dst.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'RSA signature'):
                signing.verify(dst)
            data = bytearray(original)
            data[off] ^= 1
            dst.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'digest'):
                signing.verify(dst)

    @unittest.skipUnless(Path('/usr/bin/openssl').exists(), 'openssl absent')
    def test_openssl(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / 'cert.der').write_bytes(signing.certificate(self.key))
            (d / 'message').write_bytes(b'independent signature check')
            (d / 'signature').write_bytes(signing.rsa_sign(b'independent signature check', self.key))
            def run(*args):
                return subprocess.run(['/usr/bin/openssl', *args], check=True, capture_output=True).stdout
            run('x509', '-inform', 'DER', '-in', str(d / 'cert.der'), '-out', str(d / 'cert.pem'))
            run('verify', '-check_ss_sig', '-CAfile', str(d / 'cert.pem'), str(d / 'cert.pem'))
            (d / 'pub.pem').write_bytes(run('x509', '-in', str(d / 'cert.pem'), '-pubkey', '-noout'))
            output = run('dgst', '-sha256', '-verify', str(d / 'pub.pem'), '-signature', str(d / 'signature'), str(d / 'message'))
            self.assertIn(b'Verified OK', output)

    def test_install_auto_and_override(self):
        base = {'package': 'org.test.vr', 'label': 'VR', 'abis': [], 'min_sdk': None,
                'vr': True, 'vr_activity': True, 'launchable': False}
        with patch.object(frame_android, 'apk_info', return_value=base), \
                patch.object(frame_android, 'xr_compat_files', return_value={}), \
                patch.object(frame_android, 'patch', return_value={'patched': ['launcher']}) as repair, \
                patch.object(frame_android, '_install', return_value={}) as install:
            frame_android.install('original.apk')
            repair.assert_called_once()
            self.assertFalse(install.call_args.args[3])
            self.assertEqual(install.call_args.args[1]['patched'], ['launcher'])
            frame_android.install('original.apk', flatscreen=True)
            self.assertTrue(install.call_args.args[3])

    def test_xr_compat_layer(self):
        with tempfile.TemporaryDirectory() as d:
            apk = Path(d) / 'a.apk'
            with zipfile.ZipFile(apk, 'w') as z:
                z.writestr('lib/arm64-v8a/libopenxr_loader.so', b'')
            add = frame_android.xr_compat_files(str(apk))
            self.assertEqual(set(add), set(frame_android.XR_COMPAT_FILES))
            self.assertIn(b'XR_APILAYER_FRAME_compat', add['assets/openxr/1/api_layers/implicit.d/XrApiLayer_FRAME_compat.json'])
            self.assertTrue(add['lib/arm64-v8a/libXrApiLayer_FRAME_compat.so'].startswith(b'\x7fELF'))
            with zipfile.ZipFile(apk, 'a') as z:  # already injected: nothing more to add
                z.writestr('lib/arm64-v8a/libXrApiLayer_FRAME_compat.so', b'')
            self.assertEqual(frame_android.xr_compat_files(str(apk)), {})
            flat = Path(d) / 'flat.apk'
            with zipfile.ZipFile(flat, 'w') as z:
                z.writestr('classes.dex', b'')
            self.assertEqual(frame_android.xr_compat_files(str(flat)), {})

    def test_install_adds_layer_to_vr_apps(self):
        base = {'package': 'org.test.vr', 'label': 'VR', 'abis': [], 'min_sdk': None,
                'vr': True, 'vr_activity': True, 'launchable': True}
        layer = {'x': b''}
        with patch.object(frame_android, 'apk_info', return_value=dict(base)), \
                patch.object(frame_android, 'xr_compat_files', return_value=layer), \
                patch.object(frame_android, 'patch', return_value={'patched': ['openxr-compat']}) as repair, \
                patch.object(frame_android, '_install', return_value={}) as install:
            frame_android.install('game.apk')
            self.assertIs(repair.call_args.args[2], layer)
            self.assertEqual(install.call_args.args[1]['patched'], ['openxr-compat'])
            repair.reset_mock()
            frame_android.install('game.apk', xr_compat=False)  # launchable, no layer: install as is
            repair.assert_not_called()


if __name__ == '__main__':
    unittest.main()
