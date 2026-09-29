"""frame_apk against a small APK built here: binary manifest plus resource table."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import io
import os
import struct
import sys
import tempfile
import tracemalloc
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ui'))
import frame_apk  # noqa: E402


def pool(strings, utf8=False):
    """A ResStringPool chunk."""
    data, offsets = b'', []
    for s in strings:
        offsets.append(len(data))
        if utf8:
            b = s.encode()
            data += bytes([len(s), len(b)]) + b + b'\0'
        else:
            data += struct.pack('<H', len(s)) + s.encode('utf-16-le') + b'\0\0'
    data += b'\0' * (-len(data) % 4)
    start = 28 + 4 * len(strings)
    body = struct.pack(f'<{len(strings)}I', *offsets) + data
    return struct.pack('<HHIIIIII', 1, 28, 28 + len(body), len(strings), 0, 0x100 if utf8 else 0, start, 0) + body


def manifest(package, label_ref, version_ref, min_sdk, package_raw=True, foreign_label=False):
    """<manifest package versionName><uses-sdk minSdkVersion/><application label icon/></manifest>.

    package_raw=False drops the package's raw string (as some repackers do);
    foreign_label adds a non-android `label` attribute after android:label.
    """
    strings = ['label', 'icon', 'versionName', 'minSdkVersion', 'package', 'manifest', 'uses-sdk',
               'application', package, 'junk', 'label', 'versionCode']  # the second 'label' has no android id
    resmap = struct.pack('<4I', 0x01010001, 0x01010002, 0x0101021c, 0x0101020c)
    resmap = struct.pack('<HHI', 0x0180, 8, 8 + len(resmap)) + resmap

    def element(name, attrs):
        body = struct.pack('<IIHHHHHH', 0xffffffff, name, 20, 20, len(attrs), 0, 0, 0)
        for aname, raw, dtype, value in attrs:
            body += struct.pack('<IIIHBBI', 0xffffffff, aname, raw, 8, 0, dtype, value)
        return struct.pack('<HHIII', 0x0102, 16, 16 + len(body), 1, 0xffffffff) + body

    none = 0xffffffff
    chunks = (pool(strings) + resmap
              + element(5, [(4, 8 if package_raw else none, frame_apk.T_STRING, 8),
                            (2, none, frame_apk.T_REF, version_ref),
                            (11, none, frame_apk.T_INT_DEC, 210)])
              + element(6, [(3, none, frame_apk.T_INT_DEC, min_sdk)])
              + element(7, [(0, none, frame_apk.T_REF, label_ref), (1, none, frame_apk.T_REF, 0x7f020000)]
                        + ([(10, 9, frame_apk.T_STRING, 9)] if foreign_label else [])))
    return struct.pack('<HHI', 3, 8, 8 + len(chunks)) + chunks


def resources(values):
    """resources.arsc with package 0x7f; values: {(type id, entry, language, density): global string index}."""
    strings = ['French label', 'App label', '2.1', 'res/icon_lo.png', 'res/icon_hi.png', 'res/icon.xml']
    pkg_body = b''
    for (tid, lang, density), entries in values.items():
        cfg = struct.pack('<I4x2s4xH', 64, lang.encode().ljust(2, b'\0'), density).ljust(64, b'\0')
        count = max(entries) + 1
        offsets, data = [], b''
        for i in range(count):
            if i in entries:
                offsets.append(len(data))
                data += struct.pack('<HHI', 8, 0, 0) + struct.pack('<HBBI', 8, 0, frame_apk.T_STRING, entries[i])
            else:
                offsets.append(0xffffffff)
        header = 20 + 64
        estart = header + 4 * count
        body = struct.pack(f'<{count}I', *offsets) + data
        pkg_body += struct.pack('<HHIBBHII', 0x0201, header, header + len(body), tid, 0, 0, count, estart) + cfg + body
    pkg_header = struct.pack('<HHII', 0x0200, 288, 288 + len(pkg_body), 0x7f).ljust(288, b'\0')
    pkg = pkg_header + pkg_body
    table = pool(strings, utf8=True) + pkg
    return struct.pack('<HHII', 2, 12, 12 + len(table), 1) + table


def apk(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class ApkInfo(unittest.TestCase):
    def read(self, data):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'app.apk')
            with open(p, 'wb') as f:
                f.write(data)
            return frame_apk.apk_info(p)

    def test_resolves_references(self):
        # string type 1: label (entry 0), version (entry 1); mipmap type 2: icon at three densities.
        arsc = resources({(1, 'fr', 0): {0: 0}, (1, '', 0): {0: 1, 1: 2},
                          (2, '', 160): {0: 3}, (2, '', 640): {0: 4}, (2, '', 0xfffe): {0: 5}})
        info = self.read(apk({
            'AndroidManifest.xml': manifest('com.example.demo', 0x7f010000, 0x7f010001, 26),
            'resources.arsc': arsc,
            'res/icon_lo.png': b'lo', 'res/icon_hi.png': b'hi', 'res/icon.xml': b'<xml/>',
            'lib/arm64-v8a/libx.so': b'', 'lib/x86_64/libx.so': b'',
        }))
        self.assertEqual(info['package'], 'com.example.demo')
        self.assertEqual(info['label'], 'App label')  # the default, not French
        self.assertEqual(info['version'], '2.1')
        self.assertEqual(info['version_code'], 210)
        self.assertEqual(info['min_sdk'], 26)
        self.assertEqual(info['abis'], ['arm64-v8a', 'x86_64'])
        self.assertEqual(info['icon_png'], b'hi')  # largest-density PNG, skipping the XML icon

    def test_missing_label_falls_back_to_package(self):
        info = self.read(apk({'AndroidManifest.xml': manifest('com.example.bare', 0x7f010000, 0x7f010001, 21)}))
        self.assertEqual(info['label'], 'com.example.bare')
        self.assertEqual(info['version'], '')
        self.assertEqual(info['abis'], [])

    def test_repacked_manifest(self):
        # Package kept only as a typed value; a foreign `label` mustn't beat android:label.
        arsc = resources({(1, '', 0): {0: 1, 1: 2}})
        info = self.read(apk({
            'AndroidManifest.xml': manifest('com.example.repacked', 0x7f010000, 0x7f010001, 24,
                                            package_raw=False, foreign_label=True),
            'resources.arsc': arsc,
        }))
        self.assertEqual(info['package'], 'com.example.repacked')
        self.assertEqual(info['label'], 'App label')
        self.assertIsNone(info['icon_png'])

    def test_rejects_non_apks(self):
        for data in (b'not a zip', apk({'classes.dex': b''}), apk({'AndroidManifest.xml': b'<manifest/>'})):
            with self.assertRaises(frame_apk.ApkError):
                self.read(data)

    def test_refuses_oversized_members(self):
        # An APK from a website mustn't make the server inflate gigabytes.
        data = apk({'AndroidManifest.xml': manifest('com.example.big', 0x7f010000, 0x7f010001, 21)})
        limit, frame_apk.MAX_MANIFEST = frame_apk.MAX_MANIFEST, 16
        try:
            with self.assertRaises(frame_apk.ApkError):
                self.read(data)
        finally:
            frame_apk.MAX_MANIFEST = limit

    def test_forged_sizes_dont_inflate_everything(self):
        # The central directory claims 1 byte; the deflated data holds 16 MB of zeros.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('AndroidManifest.xml', bytes(16 * 1024**2))
        data = bytearray(buf.getvalue())
        for sig, field in ((b'PK\x01\x02', 24), (b'PK\x03\x04', 22)):
            at = data.index(sig)
            data[at + field:at + field + 4] = struct.pack('<I', 1)
        limit, frame_apk.MAX_MANIFEST = frame_apk.MAX_MANIFEST, 1024**2
        tracemalloc.start()
        try:
            with self.assertRaises(frame_apk.ApkError):
                self.read(bytes(data))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            frame_apk.MAX_MANIFEST = limit
        self.assertLess(peak, 8 * 1024**2)

    def test_refuses_compression_android_cant_read(self):
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', method) as z:
                z.writestr('AndroidManifest.xml', manifest('com.example.odd', 0x7f010000, 0x7f010001, 21))
            with self.assertRaisesRegex(frame_apk.ApkError, 'compression'):
                self.read(buf.getvalue())

    def test_reference_cycles_and_fan_out_are_bounded(self):
        res = frame_apk.Resources(b'')
        ref = frame_apk.T_REF
        res.entries = {1: [('', 0, ref, 1)] * 5}  # five references to itself
        self.assertEqual(res.values(1), [])
        # Five references at each of five hops: 3125 leaves without a budget.
        res.entries = {i: [('', 0, ref, i + 1)] * 5 for i in range(1, 6)}
        res.entries[6] = [('', 0, frame_apk.T_STRING, 0)]
        self.assertEqual(len(res.values(1)), frame_apk.MAX_VALUES)
        # Forty references at each hop round a four-id cycle: millions of dead ends.
        looked = []

        class Counting(dict):
            def get(self, key, default=None):
                looked.append(key)
                return dict.get(self, key, default)
        res.entries = Counting({i: [('', 0, ref, i % 4 + 1)] * 40 for i in range(1, 5)})
        self.assertEqual(res.values(1), [])
        self.assertLess(len(looked), frame_apk.MAX_STEPS + 10)


if __name__ == '__main__':
    unittest.main()
