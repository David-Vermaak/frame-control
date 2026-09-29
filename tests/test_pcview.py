"""PC adapters: grants, input cleanup, launcher reuse and native codec contract.
Native tests require the bundled libraries; CI sets REQUIRE_NATIVE so a missing
bundle is a failure, never a silent skip. No desktop consent or GPU is faked as
successful real-host capture.
"""
import ctypes
import http.client
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'ui'))
import frame_pc_agent as agent
import frame_pc_capture as capture
import frame_pcview
import frame_macview
from frame_stream_stats import Stats
from test_macview import WS


class GrantsTests(unittest.TestCase):
    def test_source_bound_retry_ack_and_stop(self):
        g = agent.Grants('master')
        ticket = g.ticket('window:1')
        self.assertIsNone(g.redeem('window:2', {'t': ticket}))
        key = g.redeem('window:1', {'t': ticket})
        self.assertEqual(key, g.redeem('window:1', {'t': ticket}))
        g.ack(key)
        self.assertIsNone(g.redeem('window:1', {'t': ticket}))
        self.assertEqual(key, g.redeem('window:1', {'r': key}))
        self.assertIsNone(g.redeem('window:2', {'r': key}))
        g.revoke('window:1')
        self.assertIsNone(g.redeem('window:1', {'r': key}))

    def test_expiry_and_stop_before_redeem(self):
        g = agent.Grants('master')
        with mock.patch.object(agent.time, 'monotonic', return_value=1):
            ticket = g.ticket('test')
        with mock.patch.object(agent.time, 'monotonic', return_value=62):
            self.assertIsNone(g.redeem('test', {'t': ticket}))
        ticket = g.ticket('test')
        g.revoke()
        self.assertIsNone(g.redeem('test', {'t': ticket}))


class AdapterTests(unittest.TestCase):
    def test_windows_uses_wgc_and_media_foundation(self):
        for kind in ('window', 'display'):
            pipeline = capture.pipeline(dict(src=kind+':42'), 'win32', 'mfh264enc', 640, 360, 30, 1000000)
            self.assertIn('capture-api=wgc', pipeline)
            self.assertIn('window-handle=42' if kind == 'window' else 'monitor-handle=42', pipeline)
            self.assertIn('mfh264enc name=enc low-latency=true bframes=0', pipeline)
            self.assertNotIn('drop=true', pipeline)
        with self.assertRaises(ValueError):
            capture.pipeline(dict(src='window:42 ! fakesink'), 'win32', 'mfh264enc', 640, 360, 30, 1000000)

    def test_linux_uses_only_portal_fd_and_node(self):
        p = capture.pipeline(dict(src='window:consented', fd=19, node=47), 'linux', 'x264enc', 640, 360, 30, 1000000)
        self.assertIn('pipewiresrc fd=19 path=47', p)
        self.assertIn('tune=zerolatency', p)
        self.assertEqual(capture.dimensions(1920, 1080, 1280), (1280, 720))

    def test_portal_input_clamps_and_releases_held_state(self):
        native = mock.Mock()
        native.lib.fc_portal_input.return_value = 1
        inp = capture.PortalInput(native, dict(portal=123, w=100, h=50))
        inp.handle(dict(t='m', e='down', b=0, x=2, y=-1))
        inp.handle(dict(t='k', e='down', key='Control'))
        inp.release()
        calls = native.lib.fc_portal_input.call_args_list
        self.assertEqual(calls[0].args, (123, 0, 99, 0, 0, 0))
        self.assertIn(mock.call(123, 1, 0, 0, 0x110, 0), calls)
        self.assertIn(mock.call(123, 3, 0, 0, 0xffe3, 0), calls)
        self.assertFalse(inp.buttons or inp.keys)

    def test_windows_release_and_negative_monitor_origin(self):
        host = mock.Mock()
        host.user.GetSystemMetrics.side_effect = [-1920, 0, 3840, 1080]
        inp = capture.WindowsInput(host, dict(src='display:4', x=-1920, y=0, w=1920, h=1080))
        inp.handle(dict(t='m', e='down', b=2, x=0, y=0))
        self.assertEqual(host.send.call_args_list[0], mock.call(mouse=(0, 0, 0, 0xc001)))
        inp.handle(dict(t='k', e='down', code='ControlLeft', key='Control'))
        inp.release()
        self.assertIn(mock.call(mouse=(0, 0, 0, 0x10)), host.send.call_args_list)
        self.assertIn(mock.call(key=(162, 0, 2)), host.send.call_args_list)

    def test_refused_windows_focus_never_clicks_the_covering_app(self):
        host = mock.Mock()
        host.rect.return_value = (0, 0, 800, 600)
        host.user.GetForegroundWindow.return_value = 99
        host.user.SetForegroundWindow.return_value = False
        inp = capture.WindowsInput(host, dict(src='window:12', w=800, h=600))
        with self.assertRaisesRegex(RuntimeError, 'could not focus'):
            inp.handle(dict(t='m', e='down', b=0, x=.5, y=.5))
        host.send.assert_not_called()

    def test_pc_reuses_launcher_and_namespaces_panel_ids(self):
        view = frame_pcview.PCView(['ssh'], mock.Mock(return_value='panel created'), 'frame')
        view.call = mock.Mock(side_effect=lambda path, **kw: {'screen': True, 'ticket': 'one-use', 'streams': []})
        view.ensure_tunnel = mock.Mock()
        view.remote_port = 47900
        view.show('window:12')
        self.assertEqual(view.run.call_args.kwargs['stdin'], frame_macview.LAUNCH)
        self.assertNotEqual(frame_macview.panel_id('window:12'), frame_macview.panel_id('window:12', 'windows'))
        self.assertFalse(view.prefer_usb)
        with self.assertRaises(frame_macview.MacViewError):
            view.show('separate:12')

    def test_session_end_releases_native_input_only_once(self):
        session = object.__new__(agent.Session)
        session.lock, session.input_lock = threading.RLock(), threading.RLock()
        session.stop_event = threading.Event()
        session.ws, session.input = mock.Mock(), mock.Mock()
        session.released = False
        session.end()
        session.end()
        session.input.release.assert_called_once()
        self.assertTrue(session.stop_event.is_set())

    def test_idle_source_is_not_a_stall(self):
        # PipeWire/WGC deliver frames only on damage: a static desktop sends
        # its first frame and then nothing. That must not end the session.
        session = object.__new__(agent.Session)
        session.lock, session.pending = threading.RLock(), {}
        session.stats = Stats(lambda: 0)
        session.stats.captured = 1
        self.assertIsNone(session.stalled(60000000, 0, 0))
        # A frame that entered the encoder and never came out is a fault.
        session.pending[5] = dict(cap=0, arr=0, e0=1000000, tier=0, br=0)
        self.assertIn('encoder', session.stalled(12000000, 0, 0))
        self.assertIsNone(session.stalled(10000000, 0, 0))
        # No initial frame at all after the pipeline opened is a fault.
        session.pending.clear()
        self.assertIn('capture', session.stalled(12000000, 0, 1))
        self.assertIsNone(session.stalled(9000000, 0, 1))

    def test_controller_is_inert_after_close(self):
        lib = mock.Mock()
        lib.fc_new.return_value = 1234
        lib.fc_value.return_value = 100
        controller = capture.Controller(lib, 60, 5000000)
        controller.close()
        lib.fc_free.assert_called_once_with(1234)
        lib.reset_mock()
        state = controller.state()
        self.assertEqual((state['fps'], state['target'], state['scale']), (60, 0, 1))
        self.assertEqual(controller.call('gate', 1, 1), 0)
        self.assertEqual(controller.update(1), 0)
        controller.close()
        self.assertEqual(lib.method_calls, [])  # nothing touched the freed pointer

    def test_library_path_prepend_adds_no_empty_entry(self):
        env = {}
        frame_pcview.prepend(env, 'LD_LIBRARY_PATH', '/opt/fc/lib')
        self.assertEqual(env['LD_LIBRARY_PATH'], '/opt/fc/lib')
        frame_pcview.prepend(env, 'LD_LIBRARY_PATH', '/x')
        self.assertEqual(env['LD_LIBRARY_PATH'], '/x' + os.pathsep + '/opt/fc/lib')
        with mock.patch.dict(os.environ, clear=True):
            env = frame_pcview.PCView([], lambda *a: None, 'frame').agent_environment()
        for name in ('PATH', 'LD_LIBRARY_PATH'):
            parts = env.get(name, 'x').split(os.pathsep)
            self.assertNotIn('', parts, name)

    def test_malformed_viewer_input_is_dropped_not_fatal(self):
        session = object.__new__(agent.Session)
        session.lock, session.input_lock = threading.RLock(), threading.RLock()
        session.stop_event, session.key_event = threading.Event(), threading.Event()
        session.src, session.codec, session.key, session.w, session.h = 'display:1', 'h264', 'r', 100, 100
        session.source = dict(src='display:1', w=100, h=100, portal=1)
        session.input_enabled, session.released = True, False
        session.native = mock.Mock()
        session.native.now.return_value = 0
        session.controller, session.agent = mock.Mock(), mock.Mock()
        session.stats = Stats(lambda: 0)
        session.produce = lambda: None
        lib = mock.Mock()
        lib.fc_portal_input.return_value = 1
        session.input = capture.PortalInput(mock.Mock(lib=lib), session.source)
        session.ws = mock.Mock()
        session.ws.receive.side_effect = [
            dict(t='m', x='left', y=0), dict(t='wheel', dx=[1]), dict(t='k', e='down', key=None, i='x'),
            dict(t='fd', drop='many'), dict(t='rx', s=[1]), dict(t='m', x=.5, y=.5), ConnectionError('closed')]
        with self.assertRaises(ConnectionError):
            session.start()
        # Every malformed message was skipped; the good move after them still ran.
        moves = [c.args for c in lib.fc_portal_input.call_args_list if c.args[1] == 0]
        self.assertAlmostEqual(moves[-1][2], .5*99)
        self.assertEqual(session.ws.receive.call_count, 7)

    def test_stats_keep_capture_time_and_bound_records(self):
        stats = Stats(lambda: 10000000)
        stats.input(dict(t='m', i=1, tv=9999990))
        f = stats.add(cap=10000001, arr=10000002, e0=10000003, e1=10000004)
        self.assertEqual(f['echo'], 1)
        stats.report(dict(t='rx', s=f['s'], r=10000010))
        stats.report(dict(t='fd', f=[[f['s'], 10000011, 10000012, 10000013]]))
        self.assertEqual(stats.snapshot(settle=0)['inputs'][0]['frame'], f['s'])
        for _ in range(4100):
            stats.add(cap=10000001)
        self.assertEqual(len(stats.frames), 4096)


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not capture.LIBRARY.exists():
            if os.environ.get('FRAME_PC_REQUIRE_NATIVE') == '1':
                raise AssertionError('CI required native PC libraries but they were not built')
            raise unittest.SkipTest('native PC libraries not built on this host')
        cls.token = 'test-' + os.urandom(8).hex()
        env = frame_pcview.PCView([], lambda *a: None, 'frame').agent_environment()
        env['FRAME_MAC_VIEW_TOKEN'] = cls.token
        cls.proc = subprocess.Popen([sys.executable, str(ROOT/'ui/frame_pc_agent.py'), 'serve', '--port', '0',
            '--page', str(ROOT/'ui/mac-view.html'), '--exit-on-eof'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        line = cls.proc.stdout.readline()
        if 'listening' not in line:
            raise AssertionError('Native agent failed: ' + cls.proc.stderr.read())
        cls.port = int(line.rsplit(':', 1)[1])

    @classmethod
    def tearDownClass(cls):
        cls.proc.stdin.close()
        try:
            rc = cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            raise AssertionError('Native agent did not stop after EOF')
        error = cls.proc.stderr.read()
        cls.proc.stdout.close()
        cls.proc.stderr.close()
        if rc:
            raise AssertionError('Native agent exited %s: %s' % (rc, error))

    def request(self, path, method='GET'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def tearDown(self):
        self.request('/close?k='+self.token, 'POST')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux PipeWire client libraries')
    def test_bundled_pipewire_context_needs_no_system_modules(self):
        # No portal request, remote connection or desktop capture: create and
        # destroy a private client context to test the dynamically loaded SPA
        # and protocol modules that ldd cannot discover.
        code = """
import ctypes as C, os
lib = C.CDLL(os.path.join(os.environ['FRAME_PC_NATIVE'], 'lib', 'libpipewire-0.3.so.0'))
for name, ret, args in [
    ('pw_init', None, [C.c_void_p, C.c_void_p]),
    ('pw_main_loop_new', C.c_void_p, [C.c_void_p]),
    ('pw_main_loop_get_loop', C.c_void_p, [C.c_void_p]),
    ('pw_context_new', C.c_void_p, [C.c_void_p, C.c_void_p, C.c_size_t]),
    ('pw_context_find_factory', C.c_void_p, [C.c_void_p, C.c_char_p]),
    ('pw_context_find_spa_lib', C.c_char_p, [C.c_void_p, C.c_char_p]),
    ('pw_context_destroy', None, [C.c_void_p]),
    ('pw_main_loop_destroy', None, [C.c_void_p])]:
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = ret, args
lib.pw_init(None, None)
loop = lib.pw_main_loop_new(None)
assert loop, 'bundled SPA loop support did not load'
context = lib.pw_context_new(lib.pw_main_loop_get_loop(loop), None, 0)
assert context, 'bundled PipeWire client modules did not load'
assert lib.pw_context_find_factory(context, b'adapter'), 'video adapter factory is missing'
# The exported factory is video.adapt, which needs a live follower node to
# instantiate. Check its configured library and exported factory without
# pretending a headless context is a consented video stream.
plugin_name = lib.pw_context_find_spa_lib(context, b'video.adapt')
assert plugin_name, 'video.adapt has no bundled library mapping'
plugin = C.CDLL(os.path.join(os.environ['SPA_PLUGIN_DIR'], plugin_name.decode() + '.so'))
class Factory(C.Structure):
    _fields_ = [('version', C.c_uint32), ('name', C.c_char_p)]
plugin.spa_handle_factory_enum.argtypes = [C.POINTER(C.POINTER(Factory)), C.POINTER(C.c_uint32)]
plugin.spa_handle_factory_enum.restype = C.c_int
factory, index, names = C.POINTER(Factory)(), C.c_uint32(), []
while plugin.spa_handle_factory_enum(C.byref(factory), C.byref(index)) > 0:
    names.append(factory.contents.name)
assert b'video.adapt' in names, 'bundled SPA video adapter factory is missing'
lib.pw_context_destroy(context)
lib.pw_main_loop_destroy(loop)
"""
        env = frame_pcview.PCView([], lambda *a: None, 'frame').agent_environment()
        env['FRAME_PC_NATIVE'] = str(capture.NATIVE)
        result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_last_raw_picture_survives_a_closed_gate(self):
        # A source sends ONE changed frame, then becomes idle. It must reach
        # the encoder after congestion clears, without needing another update.
        code = """
import ctypes as C, sys, time
from frame_pc_capture import Native, GATE, Encoded, pipeline
native = Native()
start = None
closing = False
phases = []
def gate(stage, pts, capture, arrived):
    global start
    phases.append(stage)
    if closing: return -1
    if start is None: start = time.monotonic()
    return int(stage == 1 or time.monotonic()-start >= .15)
callback = GATE(gate)
p = pipeline(dict(src='test'), sys.platform, 'x264enc', 320, 180, 30, 300000)
p = p.replace('is-live=true', 'is-live=true num-buffers=1')
error = C.create_string_buffer(1024)
handle = native.lib.fc_capture_open(p.encode(), callback, error, len(error))
assert handle, error.value
try:
    frame = Encoded()
    result = 0
    deadline = time.monotonic()+5
    while not result and time.monotonic() < deadline:
        result = native.lib.fc_capture_pull(handle, C.byref(frame))
    assert result == 1, native.lib.fc_capture_error(handle)
    assert frame.key and frame.size > 17
    assert phases.count(0) == 1 and 2 in phases, phases
finally:
    closing = True
    native.lib.fc_capture_close(handle)
"""
        env = frame_pcview.PCView([], lambda *a: None, 'frame').agent_environment()
        env['PYTHONPATH'] = str(ROOT / 'ui')
        result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_auth_and_real_h264_stats(self):
        self.assertEqual(self.request('/status')[0], 403)
        status, body = self.request('/ticket?src=test&k='+self.token, 'POST')
        self.assertEqual(status, 200)
        ticket = json.loads(body)['ticket']
        ws = WS(self.port, '/stream?src=test&fps=30&max=640&t='+ticket)
        try:
            self.assertEqual(ws.status, 101)
            key, got, echo = None, 0, False
            ws.send_text(json.dumps(dict(t='m', e='down', i=7, tv=1, x=.2, y=.3)))
            deadline = time.monotonic()+10
            while time.monotonic() < deadline and (got < 5 or not echo):
                op, data = ws.recv()
                if op == 1:
                    m = json.loads(data)
                    self.assertNotEqual(m.get('t'), 'error', m)
                    if m.get('t') == 'hello':
                        key = m['r']
                        ws.send_text(json.dumps(dict(t='ack')))
                elif op == 2:
                    got += 1
                    seq, echoed = struct.unpack('!II', data[9:17])
                    now = struct.unpack('!Q', data[1:9])[0]
                    echo |= echoed == 7
                    self.assertIn(b'\x00\x00\x00\x01', data[17:])
                    ws.send_text(json.dumps(dict(t='rx', s=seq, r=now+100)))
                    ws.send_text(json.dumps(dict(t='fd', f=[[seq, now+200, now+300, now+400]])))
            self.assertGreaterEqual(got, 5)
            self.assertTrue(echo)
            time.sleep(.1)
            stats = json.loads(self.request('/stats?settle=0&k='+self.token)[1])['streams'][0]
            self.assertGreater(stats['frames'][0]['e1'], stats['frames'][0]['e0'])
            self.assertIn('controller', stats)
            used = WS(self.port, '/stream?src=test&t='+ticket)
            self.assertEqual(used.status, 403)
            used.close()
            self.request('/close?src=test&k='+self.token, 'POST')
            revoked = WS(self.port, '/stream?src=test&r='+key)
            self.assertEqual(revoked.status, 403)
            revoked.close()
        finally:
            ws.close()


if __name__ == '__main__':
    unittest.main()
