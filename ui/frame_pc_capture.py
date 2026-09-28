"""Native PC capture and input bindings. No desktop-streaming app required.

The packaged native library contains our adapter and the shared rate controller;
GStreamer supplies WGC/Media Foundation and PipeWire/VA-API/x264 libraries.
"""
import ctypes as C
import os
from pathlib import Path
import re
import sys
import threading

ROOT = Path(__file__).resolve().parent.parent
NATIVE = Path(os.environ.get('FRAME_PC_NATIVE', ROOT / 'desktop' / 'bundle'))
LIBRARY = NATIVE / ('pc-host.dll' if sys.platform == 'win32' else 'pc-host.so')


class Encoded(C.Structure):
    _fields_ = [('data', C.c_void_p), ('size', C.c_int), ('key', C.c_int),
                ('width', C.c_int), ('height', C.c_int), ('pts', C.c_int64)]


GATE = C.CFUNCTYPE(C.c_int, C.c_int, C.c_int64, C.c_int64, C.c_int64)


class Native:
    def __init__(self, path=LIBRARY):
        self.dll_dirs = []
        if sys.platform == 'win32':
            for folder in (path.parent, path.parent / 'bin'):
                self.dll_dirs.append(os.add_dll_directory(str(folder)))
        self.lib = C.CDLL(str(path))
        signatures = {
            'fc_now': (C.c_int64, []), 'fc_gst_init': (None, []),
            'fc_has_element': (C.c_int, [C.c_char_p]),
            'fc_new': (C.c_void_p, [C.c_int, C.c_int]), 'fc_free': (None, [C.c_void_p]),
            'fc_ceiling': (None, [C.c_void_p, C.c_int]),
            'fc_gate': (C.c_int, [C.c_void_p, C.c_int64, C.c_int]),
            'fc_capture': (None, [C.c_void_p, C.c_int64]),
            'fc_sent': (None, [C.c_void_p, C.c_uint32, C.c_int, C.c_int64]),
            'fc_ack': (C.c_int, [C.c_void_p, C.c_uint32, C.c_int64]),
            'fc_update': (C.c_int, [C.c_void_p, C.c_int64]),
            'fc_value': (C.c_int64, [C.c_void_p, C.c_int]),
            'fc_capture_open': (C.c_void_p, [C.c_char_p, GATE, C.c_char_p, C.c_int]),
            'fc_capture_pull': (C.c_int, [C.c_void_p, C.POINTER(Encoded)]),
            'fc_capture_error': (C.c_char_p, [C.c_void_p]),
            'fc_capture_bitrate': (None, [C.c_void_p, C.c_int]),
            'fc_capture_key': (None, [C.c_void_p]),
            'fc_capture_test': (None, [C.c_void_p, C.c_uint32]), 'fc_capture_close': (None, [C.c_void_p]),
        }
        if sys.platform.startswith('linux'):
            signatures.update({
                'fc_portal_select': (C.c_void_p, [C.c_char_p, C.c_int]),
                'fc_portal_close': (None, [C.c_void_p]),
                'fc_portal_refresh': (C.c_int, [C.c_void_p, C.c_char_p, C.c_int]),
                'fc_portal_value': (C.c_int, [C.c_void_p, C.c_int]),
                'fc_portal_input': (C.c_int, [C.c_void_p, C.c_int, C.c_double, C.c_double, C.c_int, C.c_int]),
            })
        for name, (result, args) in signatures.items():
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = result, args
        self.lib.fc_gst_init()

    def has(self, name):
        return bool(self.lib.fc_has_element(name.encode()))

    def now(self):
        return self.lib.fc_now()


class Controller:
    def __init__(self, lib, fps, ceiling):
        self.lib, self.lock = lib, threading.RLock()
        self.enabled = os.environ.get('FRAME_MAC_VIEW_ADAPT') != '0'
        self.ptr = lib.fc_new(fps, self.enabled)
        if not self.ptr:
            raise MemoryError('Unable to allocate the streaming controller')
        lib.fc_ceiling(self.ptr, ceiling)
        self.events = []

    def state(self):
        with self.lock:
            names = ['target', 'ceiling', 'tier', 'fps', 'scale', 'baseRtt', 'inFlight', 'slack']
            out = {k: self.lib.fc_value(self.ptr, i) for i, k in enumerate(names)}
            out.update(scale=out['scale'] / 100, baseRtt=out['baseRtt'] / 1000,
                       slack=out['slack'] / 1000, adapt=self.enabled)
            return out

    def call(self, name, *args):
        with self.lock:
            return getattr(self.lib, 'fc_' + name)(self.ptr, *args)

    def update(self, now):
        with self.lock:
            old = self.state()
            result = self.lib.fc_update(self.ptr, now)
            state = self.state()
            if state['target'] < old['target'] or state['tier'] != old['tier']:
                self.events.append({'t': now, 'e': 'target %s bit/s; tier %s' % (state['target'], state['tier'])})
                self.events = self.events[-200:]
            return result

    def close(self):
        with self.lock:
            if self.ptr:
                self.lib.fc_free(self.ptr)
                self.ptr = None


def dimensions(w, h, maximum):
    scale = min(1, maximum / max(w, h))
    return max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)


def pipeline(source, platform, encoder, w, h, fps, bitrate, codec='h264'):
    """Only locally constructed numeric source IDs enter the pipeline parser."""
    if source['src'] == 'test':
        capture = 'videotestsrc name=source is-live=true pattern=ball'
    elif platform == 'win32':
        kind, ident = source['src'].split(':')
        if kind not in ('window', 'display') or not re.fullmatch(r'[0-9]+', ident):
            raise ValueError('Invalid Windows capture source')
        prop = 'window-handle' if kind == 'window' else 'monitor-handle'
        capture = 'd3d11screencapturesrc capture-api=wgc show-cursor=true show-border=true %s=%d' % (prop, int(ident))
    else:
        capture = 'pipewiresrc fd=%d path=%d do-timestamp=true' % (source['fd'], source['node'])
    # The source gate runs before conversion/encoding. There is no leaky queue
    # of H.264 frames; every encoded reference frame reaches the socket.
    raw = '%s ! video/x-raw,framerate=%d/1 ! queue name=raw_queue leaky=downstream max-size-buffers=1 max-size-bytes=0 max-size-time=0 ! identity name=gate ! videoconvert ! videoscale add-borders=false ! video/x-raw,width=%d,height=%d' % (capture, fps, w, h)
    if codec == 'jpeg':
        enc = 'jpegenc name=enc quality=80'
        parse = ''
    else:
        choices = {
            'mfh264enc': 'mfh264enc name=enc low-latency=true bframes=0 gop-size=60',
            'vah264enc': 'vah264enc name=enc b-frames=0 key-int-max=60',
            'x264enc': 'x264enc name=enc tune=zerolatency speed-preset=ultrafast bframes=0 key-int-max=60',
        }
        enc = choices[encoder] + ' bitrate=%d' % max(1, bitrate // 1000)
        raw += ',format=NV12' if encoder != 'x264enc' else ',format=I420'
        parse = ' ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au'
    return raw + ' ! ' + enc + parse + ' ! appsink name=out sync=false max-buffers=2 drop=false'


# X11 keysyms work on Wayland through the RemoteDesktop portal too. The host's
# keyboard layout handles physical keys; Unicode text is sent as Unicode keysyms.
KEYSYMS = {'Enter': 0xff0d, 'Tab': 0xff09, 'Backspace': 0xff08, 'Escape': 0xff1b,
           'Delete': 0xffff, 'ArrowLeft': 0xff51, 'ArrowUp': 0xff52, 'ArrowRight': 0xff53,
           'ArrowDown': 0xff54, 'Home': 0xff50, 'End': 0xff57, 'PageUp': 0xff55,
           'PageDown': 0xff56, 'Shift': 0xffe1, 'Control': 0xffe3, 'Alt': 0xffe9, 'Meta': 0xffeb}


class PortalInput:
    def __init__(self, native, source):
        self.lib, self.source = native.lib, source
        self.buttons, self.keys = set(), set()

    def emit(self, kind, x=0, y=0, code=0, down=0):
        if not self.lib.fc_portal_input(self.source['portal'], kind, x, y, code, down):
            raise RuntimeError('The desktop portal refused input; check the sharing permission')

    def release(self):
        error = None
        for values, kind in ((self.buttons, 1), (self.keys, 3)):
            for code in list(values):
                try:
                    self.emit(kind, code=code, down=0)
                    values.discard(code)
                except RuntimeError as e:
                    error = e
        if error:
            raise error

    def handle(self, m):
        t = m['t']
        if t == 'release':
            return self.release()
        if t in ('m', 'wheel'):
            x, y = (max(0, min(1, float(m.get(k, 0)))) for k in ('x', 'y'))
            self.emit(0, x * (self.source['w'] - 1), y * (self.source['h'] - 1))
        if t == 'm' and m.get('e') in ('up', 'down'):
            b = {0: 0x110, 1: 0x112, 2: 0x111}.get(m.get('b', 0))
            if b is not None:
                down = m['e'] == 'down'
                self.emit(1, code=b, down=down)
                (self.buttons.add if down else self.buttons.discard)(b)
        elif t == 'wheel':
            self.emit(2, max(-4096, min(4096, float(m.get('dx', 0)))), max(-4096, min(4096, float(m.get('dy', 0)))))
        elif t == 'k':
            key = str(m.get('key', ''))
            k = KEYSYMS.get(key) or (ord(key) if len(key) == 1 and ord(key) < 256 else
                                     0x1000000 + ord(key) if len(key) == 1 else 0)
            if k:
                down = m.get('e') == 'down'
                self.emit(3, code=k, down=down)
                (self.keys.add if down else self.keys.discard)(k)
        elif t == 'text':
            for ch in str(m.get('s', ''))[:4096]:
                k = ord(ch) if ord(ch) < 256 else 0x1000000 + ord(ch)
                self.emit(3, code=k, down=1)
                self.emit(3, code=k, down=0)


class Windows:
    """WGC source handles and SendInput. No elevation or global input hook."""
    def __init__(self):
        from ctypes import wintypes as W
        self.W = W
        self.user = C.WinDLL('user32', use_last_error=True)
        self.dwm = C.WinDLL('dwmapi')
        self.user.SetProcessDpiAwarenessContext.argtypes = [C.c_void_p]
        self.user.SetProcessDpiAwarenessContext(C.c_void_p(-4))  # per-monitor v2
        self.user.IsWindow.argtypes = [W.HWND]
        self.user.IsWindowVisible.argtypes = [W.HWND]
        self.user.IsIconic.argtypes = [W.HWND]
        self.user.GetWindowTextLengthW.argtypes = [W.HWND]
        self.user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        self.user.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        self.user.SetForegroundWindow.argtypes = [W.HWND]
        self.dwm.DwmGetWindowAttribute.argtypes = [W.HWND, W.DWORD, C.c_void_p, W.DWORD]
        self.callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self.monitor_callback = C.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, C.POINTER(W.RECT), W.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback, W.LPARAM]
        self.user.EnumDisplayMonitors.argtypes = [W.HDC, C.c_void_p, self.monitor_callback, W.LPARAM]
        class Mouse(C.Structure):
            _fields_ = [('dx', W.LONG), ('dy', W.LONG), ('data', W.DWORD), ('flags', W.DWORD),
                        ('time', W.DWORD), ('extra', C.c_size_t)]
        class Key(C.Structure):
            _fields_ = [('vk', W.WORD), ('scan', W.WORD), ('flags', W.DWORD), ('time', W.DWORD), ('extra', C.c_size_t)]
        class Union(C.Union):
            _fields_ = [('mouse', Mouse), ('key', Key)]
        class Input(C.Structure):
            _fields_ = [('type', W.DWORD), ('data', Union)]
        self.Mouse, self.Key, self.Input = Mouse, Key, Input
        self.user.SendInput.argtypes = [W.UINT, C.POINTER(Input), C.c_int]
        self.user.SendInput.restype = W.UINT

    def rect(self, hwnd):
        r = self.W.RECT()
        if not self.user.IsWindow(hwnd) or self.user.IsIconic(hwnd):
            raise RuntimeError('The captured window closed or was minimized')
        # WGC captures the visible extended frame, excluding invisible resize borders.
        if self.dwm.DwmGetWindowAttribute(hwnd, 9, C.byref(r), C.sizeof(r)):
            if not self.user.GetWindowRect(hwnd, C.byref(r)):
                raise RuntimeError('Cannot locate the captured window')
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def sources(self):
        windows, displays = [], []
        def window(hwnd, _):
            if not self.user.IsWindowVisible(hwnd) or self.user.IsIconic(hwnd):
                return True
            n = self.user.GetWindowTextLengthW(hwnd)
            if n:
                title = C.create_unicode_buffer(n + 1)
                self.user.GetWindowTextW(hwnd, title, n + 1)
                try:
                    x, y, w, h = self.rect(hwnd)
                    if w > 0 and h > 0:
                        windows.append(dict(src='window:%d' % hwnd, id=hwnd, title=title.value,
                                            app='Windows', w=w, h=h, x=x, y=y))
                except RuntimeError:
                    pass
            return True
        def monitor(handle, dc, rect, data):
            r = rect.contents
            displays.append(dict(src='display:%d' % handle, name='Display %d' % (len(displays) + 1),
                                 x=r.left, y=r.top, w=r.right-r.left, h=r.bottom-r.top))
            return True
        self.user.EnumWindows(self.callback(window), 0)
        self.user.EnumDisplayMonitors(None, None, self.monitor_callback(monitor), 0)
        return windows, displays

    def send(self, mouse=None, key=None):
        inp = self.Input()
        inp.type = 0 if mouse else 1
        if mouse:
            inp.data.mouse = self.Mouse(*mouse, 0, 0)
        else:
            inp.data.key = self.Key(*key, 0, 0)
        if self.user.SendInput(1, C.byref(inp), C.sizeof(inp)) != 1:
            raise RuntimeError('Windows refused input (elevated apps and the secure desktop cannot be controlled)')


class WindowsInput:
    def __init__(self, host, source):
        self.host, self.source = host, source
        self.buttons, self.keys = set(), set()

    def release(self):
        error = None
        for button in list(self.buttons):
            try:
                self.host.send(mouse=(0, 0, 0, {0: 4, 1: 0x40, 2: 0x10}[button]))
                self.buttons.discard(button)
            except RuntimeError as e:
                error = e
        for vk in list(self.keys):
            try:
                extended = 1 if vk in (33, 34, 35, 36, 37, 38, 39, 40, 45, 46, 91, 92, 163, 165) else 0
                self.host.send(key=(vk, 0, 2 | extended))
                self.keys.discard(vk)
            except RuntimeError as e:
                error = e
        if error:
            raise error

    def handle(self, m):
        t, u = m['t'], self.host.user
        if t == 'release':
            return self.release()
        if t in ('m', 'wheel'):
            source = self.source
            if source['src'].startswith('window:'):
                hwnd = int(source['src'].split(':')[1])
                x, y, w, h = self.host.rect(hwnd)
                if m.get('e') == 'down':
                    u.SetForegroundWindow(hwnd)
            else:
                x, y, w, h = (source[k] for k in ('x', 'y', 'w', 'h'))
            x += max(0, min(1, float(m.get('x', 0)))) * (w - 1)
            y += max(0, min(1, float(m.get('y', 0)))) * (h - 1)
            left, top, width, height = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
            self.host.send(mouse=(round((x-left)*65535/max(1,width-1)), round((y-top)*65535/max(1,height-1)), 0, 0xc001))
        if t == 'm' and m.get('e') in ('up', 'down'):
            b = m.get('b', 0)
            if b in (0, 1, 2):
                down = m['e'] == 'down'
                self.host.send(mouse=(0, 0, 0, {0: 2, 1: 0x20, 2: 8}[b] * (1 if down else 2)))
                (self.buttons.add if down else self.buttons.discard)(b)
        elif t == 'wheel':
            for name, flag, direction in (('dy', 0x800, -1), ('dx', 0x1000, 1)):
                delta = int(max(-4096, min(4096, float(m.get(name, 0))))) * direction
                if delta:
                    self.host.send(mouse=(0, 0, delta & 0xffffffff, flag))
        elif t == 'k':
            code, down = str(m.get('code', '')), m.get('e') == 'down'
            special = {'Enter': 13, 'Escape': 27, 'Tab': 9, 'Backspace': 8, 'Space': 32,
                       'ArrowLeft': 37, 'ArrowUp': 38, 'ArrowRight': 39, 'ArrowDown': 40,
                       'Delete': 46, 'Home': 36, 'End': 35, 'PageUp': 33, 'PageDown': 34,
                       'ShiftLeft': 160, 'ShiftRight': 161, 'ControlLeft': 162, 'ControlRight': 163,
                       'AltLeft': 164, 'AltRight': 165, 'MetaLeft': 91, 'MetaRight': 92}
            vk = special.get(code, 0)
            if re.fullmatch(r'Key[A-Z]', code) or re.fullmatch(r'Digit[0-9]', code):
                vk = ord(code[-1])
            if vk:
                extended = 1 if vk in (33, 34, 35, 36, 37, 38, 39, 40, 45, 46, 91, 92, 163, 165) else 0
                self.host.send(key=(vk, 0, extended | (0 if down else 2)))
                (self.keys.add if down else self.keys.discard)(vk)
            elif down and len(str(m.get('key', ''))) == 1:
                self.handle({'t': 'text', 's': m['key']})
        elif t == 'text':
            raw = str(m.get('s', ''))[:4096].encode('utf-16-le')
            for i in range(0, len(raw), 2):
                unit = int.from_bytes(raw[i:i+2], 'little')
                self.host.send(key=(0, unit, 4))
                self.host.send(key=(0, unit, 6))
