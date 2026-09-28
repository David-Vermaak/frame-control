"""Frozen regression trace from the original mac-in-headset Swift controller.
The 600-state reference was produced at 1b90c64, before the C extraction:
60 seconds with a slow/congested middle, then recovery. No generated behavior
from the new implementation is used to calculate the expected digest.
"""
import ctypes as C
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'ui'))
from frame_pc_capture import Controller


class ControllerTrace(unittest.TestCase):
    def test_original_mac_congestion_and_recovery(self):
        cc = shutil.which('cc') or shutil.which('gcc')
        if not cc:
            self.skipTest('C compiler not installed; native build jobs cover this trace')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ('trace.dll' if sys.platform == 'win32' else 'trace.so')
            subprocess.run([cc, '-O2', '-shared', '-fPIC', str(ROOT/'desktop/controller.c'), '-o', str(path)], check=True, capture_output=True)
            lib = C.CDLL(str(path))
            sig = {'new': (C.c_void_p, [C.c_int, C.c_int]), 'free': (None, [C.c_void_p]),
                   'ceiling': (None, [C.c_void_p, C.c_int]), 'value': (C.c_int64, [C.c_void_p, C.c_int]),
                   'capture': (None, [C.c_void_p, C.c_int64]), 'gate': (C.c_int, [C.c_void_p, C.c_int64, C.c_int]),
                   'sent': (None, [C.c_void_p, C.c_uint32, C.c_int, C.c_int64]),
                   'ack': (C.c_int, [C.c_void_p, C.c_uint32, C.c_int64]), 'update': (C.c_int, [C.c_void_p, C.c_int64])}
            for name, (ret, args) in sig.items():
                fn = getattr(lib, 'fc_'+name)
                fn.restype, fn.argtypes = ret, args
            c = Controller(lib, 60, 8000000)
            due, states, seq = [], [], 0
            try:
                for tick in range(6000):
                    now = 10000000+tick*10000
                    for t, s in due:
                        if t <= now:
                            c.call('ack', s, now)
                    due = [(t, s) for t, s in due if t > now]
                    if tick % 2 == 0:
                        c.call('capture', now)
                        if c.call('gate', now, 1):
                            seq += 1
                            c.call('sent', seq, 8000 if tick < 1000 or tick > 4000 else 22000, now)
                            due.append((now+(240000 if 1500 < tick < 3500 else 20000), seq))
                    if tick % 10 == 0:
                        c.update(now)
                        x = c.state()
                        states.append([x[k] for k in ('target', 'ceiling', 'tier', 'fps', 'inFlight')] +
                                      [round(x['scale']*100), round(x['baseRtt']*1000), round(x['slack']*1000)])
                digest = hashlib.sha256(json.dumps(states, separators=(',', ':')).encode()).hexdigest()
                self.assertEqual(digest, '1932f16aafce5c9157bcd2aba5d90719a366f9999422920aaeae78a6cdec08f6')
            finally:
                c.close()
                if sys.platform == 'win32':
                    import _ctypes
                    _ctypes.FreeLibrary(lib._handle)
