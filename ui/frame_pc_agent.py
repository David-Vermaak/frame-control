#!/usr/bin/env python3
"""Frame Control's Windows/Linux host. Same ticket, H.264/JPEG, timing and
input protocol as frame-mac-view; serves the same ui/mac-view.html.

Only loopback is bound. Frame Control owns the master token, the Frame gets
one-source tickets and reconnect keys. Native libraries are bundled.
"""
import argparse
import base64
import ctypes as C
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import socket
import struct
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from frame_pc_capture import Native, Controller, Windows, WindowsInput, PortalInput, Encoded, GATE, dimensions, pipeline
from frame_stream_stats import Stats


class Grants:
    """Caller holds the agent lock, including replacement and Stop."""
    def __init__(self, token):
        self.token, self.tickets, self.keys = token, {}, {}

    def master(self, key):
        return isinstance(key, str) and secrets.compare_digest(key, self.token)

    def ticket(self, src):
        self.tickets = {k: v for k, v in self.tickets.items() if v[1] > time.monotonic()}
        if len(self.tickets) >= 128:
            raise ValueError('Too many pending viewers')
        ticket = secrets.token_urlsafe(24)
        self.tickets[ticket] = (src, time.monotonic()+60, None)
        return ticket

    def redeem(self, src, q):
        if self.master(q.get('k')):
            key = secrets.token_urlsafe(24)
            self.keys[key] = src
            return key
        entry = self.tickets.get(q.get('t'))
        if entry and entry[0] == src and entry[1] > time.monotonic():
            key = entry[2] or secrets.token_urlsafe(24)
            self.tickets[q['t']] = (src, entry[1], key)
            self.keys[key] = src
            return key
        key = q.get('r')
        return key if key and self.keys.get(key) == src else None

    def ack(self, key):
        self.tickets = {k: v for k, v in self.tickets.items() if v[2] != key}

    def revoke(self, src=None):
        self.tickets = {k: v for k, v in self.tickets.items() if src is not None and v[0] != src}
        self.keys = {k: v for k, v in self.keys.items() if src is not None and v != src}


class WebSocket:
    def __init__(self, handler):
        self.sock, self.reader = handler.connection, handler.rfile
        self.lock = threading.Lock()
        self.closed = False
        self.sock.settimeout(5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send(self, data, opcode=1):
        if not isinstance(data, bytes):
            data = json.dumps(data, separators=(',', ':')).encode()
        n = len(data)
        head = bytes([0x80 | opcode, n]) if n < 126 else bytes([0x80 | opcode, 126]) + struct.pack('!H', n) if n <= 65535 else bytes([0x80 | opcode, 127]) + struct.pack('!Q', n)
        with self.lock:
            if self.closed:
                raise ConnectionError('Viewer disconnected')
            self.sock.sendall(head + data)

    def exact(self, n):
        data = self.reader.read(n)
        if len(data) != n:
            raise ConnectionError('Viewer disconnected')
        return data

    def receive(self):
        a, b = self.exact(2)
        opcode, size = a & 15, b & 127
        if a & 0x70 or not a & 0x80 or not b & 0x80 or opcode not in (1, 8, 9, 10):
            raise ValueError('Unsupported WebSocket frame')
        if size == 126:
            size = struct.unpack('!H', self.exact(2))[0]
        elif size == 127:
            size = struct.unpack('!Q', self.exact(8))[0]
        if size > 65536 or (opcode >= 8 and size > 125):
            raise ValueError('WebSocket message too large')
        mask = self.exact(4)
        data = bytes(v ^ mask[i % 4] for i, v in enumerate(self.exact(size)))
        if opcode == 8:
            raise ConnectionError('Viewer closed')
        if opcode == 9:
            self.send(data, 10)
        if opcode != 1:
            return {}
        message = json.loads(data)
        if not isinstance(message, dict):
            raise ValueError('Expected an input object')
        return message

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class Session:
    def __init__(self, agent, ws, key, source, query):
        self.agent, self.native, self.ws, self.key, self.source = agent, agent.native, ws, key, source
        self.src = source['src']
        self.codec = query.get('codec', 'h264')
        if self.codec not in ('h264', 'jpeg'):
            raise ValueError('Unsupported codec')
        self.fps = min(120, max(5, int(query.get('fps', 60))))
        bpp = float(query.get('bpp', .1))
        if not math.isfinite(bpp):
            raise ValueError('Invalid bitrate')
        self.w, self.h = dimensions(source['w'], source['h'], min(3840, max(320, int(query.get('max', 1920)))))
        self.bitrate = max(300000, int(self.w*self.h*self.fps*min(.5, max(.02, bpp))))
        self.controller = Controller(self.native.lib, self.fps, self.bitrate)
        self.stats = Stats(self.native.now)
        self.stop_event, self.key_event = threading.Event(), threading.Event()
        self.lock, self.input_lock = threading.RLock(), threading.RLock()
        self.pending, self.last_submit = {}, 0
        self.input = None if self.src == 'test' else WindowsInput(agent.windows, source) if agent.windows else PortalInput(self.native, source)
        self.writer = None

    def gate(self, stage, pts, capture, arrived):
        # Exceptions cannot cross a ctypes callback boundary.
        try:
            with self.lock:
                if self.stop_event.is_set():
                    return 0
                if stage == 1:
                    if pts in self.pending:
                        self.pending[pts]['e0'] = self.native.now()
                    return 1
                self.stats.captured += 1
                self.controller.call('capture', arrived)
                state = self.controller.state()
                if self.pending or arrived-self.last_submit < 1000000/state['fps'] or not self.controller.call('gate', arrived, 1):
                    self.stats.skipped += 1
                    return 0
                self.pending[pts] = dict(cap=capture, arr=arrived, e0=arrived, tier=state['tier'], br=state['target'])
                self.last_submit = arrived
                return 1
        except Exception:
            self.stop_event.set()
            return 0

    def produce(self):
        native, capture = self.native.lib, None
        try:
            encoder = "x264enc" if self.src == "test" and self.native.has("x264enc") else self.agent.encoder
            description = pipeline(self.source, sys.platform, encoder, self.w, self.h, self.fps, self.bitrate, self.codec)
            self.callback = GATE(self.gate)
            error = C.create_string_buffer(1024)
            capture = native.fc_capture_open(description.encode(), self.callback, error, len(error))
            if not capture:
                raise RuntimeError(error.value.decode(errors='replace'))
            last_update = last_stats = self.native.now()
            last_output = last_update
            while not self.stop_event.is_set():
                output = Encoded()
                result = native.fc_capture_pull(capture, C.byref(output))
                now = self.native.now()
                if result < 0:
                    raise RuntimeError(native.fc_capture_error(capture).decode(errors='replace'))
                if result:
                    last_output = now
                    with self.lock:
                        # Exactly one raw frame is in flight and B-frames are disabled.
                        # x264 may offset PTS; associate by that single frame,
                        # keeping the actual pre-encode capture timestamp.
                        raw_pts = next(iter(self.pending), None)
                        record = self.pending.get(raw_pts)
                        if record is None:
                            raise RuntimeError('Encoder changed frame timestamps; timing cannot be matched')
                        data = C.string_at(output.data, output.size)
                        f = self.stats.add(**record, e1=now, snd=now, b=len(data), k=output.key,
                                           w=output.width or self.w, h=output.height or self.h)
                        self.controller.call('sent', f['s'], len(data)+17, now)
                    self.ws.send(struct.pack('!BQII', output.key, max(0, f['cap']), f['s'], f['echo'])+data, 2)
                    with self.lock:
                        f['wire'] = self.native.now()
                        self.pending.pop(raw_pts, None)
                if now-last_output > 10000000:
                    raise RuntimeError('No encoded frames for 10 seconds; check capture permissions and the encoder')
                if self.key_event.is_set():
                    self.key_event.clear()
                    if self.codec == 'h264':
                        native.fc_capture_key(capture)
                if now-last_update >= 100000:
                    target = self.controller.update(now)
                    # x264 supports live bitrate changes. Hardware elements
                    # advertise their mutability; the initial bitrate always
                    # applies. Frame gating remains active for every encoder.
                    if target and self.codec == 'h264' and encoder == 'x264enc':
                        native.fc_capture_bitrate(capture, target)
                    last_update = now
                if now-last_stats >= 1000000:
                    self.ws.send(dict(self.stats.summary(), t='stats', bitrate=self.bitrate,
                                      size='%dx%d' % (self.w, self.h), tier=self.controller.state()['tier']))
                    last_stats = now
        except Exception as e:
            try:
                self.ws.send({'t': 'error', 'message': str(e)})
            except OSError:
                pass
        finally:
            self.stop_event.set()
            if capture:
                native.fc_capture_close(capture)
            self.ws.close()

    def start(self):
        if self.stop_event.is_set():
            self.controller.close()
            return
        self.ws.send(dict(t='hello', r=self.key))
        self.ws.send(dict(t='info', src=self.src, title=self.source.get('title', self.source.get('name', 'Test pattern')),
                          app='PC', codec=self.codec, input=self.src == 'test' or self.source.get('devices', 3) == 3,
                          inputMessage='Allow pointer and keyboard control in the host sharing dialog.',
                          aspect=self.w/self.h, warm=0))
        with self.lock:
            if self.stop_event.is_set():
                self.controller.close()
                return
            self.writer = threading.Thread(target=self.produce, daemon=True)
            self.writer.start()
        try:
            while not self.stop_event.is_set():
                m = self.ws.receive()
                t = m.get('t')
                if t == 'ping':
                    self.ws.send(dict(t='pong', c=m.get('c', 0), a=self.native.now()))
                elif t == 'ack':
                    with self.agent.lock:
                        self.agent.grants.ack(self.key)
                elif t == 'key-frame':
                    self.key_event.set()
                elif t in ('rx', 'fd', 'clock'):
                    self.stats.report(m)
                    if t == 'rx' and isinstance(m.get('s'), int):
                        self.controller.call('ack', m['s'] & 0xffffffff, self.native.now())
                elif t in ('m', 'wheel', 'k', 'text', 'release'):
                    with self.input_lock:
                        if self.stop_event.is_set():
                            break
                        if self.input:
                            self.input.handle(m)
                        self.stats.input(m)
        finally:
            self.end()
            self.writer.join(6)
            if not self.writer.is_alive():
                self.controller.close()

    def end(self):
        with self.lock:
            self.stop_event.set()
        self.ws.close()
        with self.input_lock:
            if self.input:
                try:
                    self.input.release()
                except (RuntimeError, OSError):
                    pass


class Agent:
    def __init__(self, native, token, page):
        self.native, self.page = native, page
        self.lock = threading.RLock()
        self.grants = Grants(token)
        self.sessions, self.sources = {}, {}
        self.next_id, self.selecting, self.selection_error = 1, False, ''
        self.windows = Windows() if sys.platform == 'win32' else None
        self.encoder = 'mfh264enc' if self.windows else 'vah264enc' if native.has('vah264enc') else 'x264enc'
        self.shutting_down = False

    def lists(self):
        if self.windows:
            windows, displays = self.windows.sources()
            self.sources = {s['src']: s for s in windows + displays}
            return windows, displays
        return [{k: v for k, v in s.items() if k not in ('portal', 'fd', 'node')} for s in self.sources.values()], []

    def source(self, src):
        if src == 'test':
            return dict(src='test', title='Test pattern', w=1280, h=720)
        self.lists()
        if src not in self.sources:
            raise ValueError('Choose a window or screen on this computer first')
        return dict(self.sources[src])

    def select(self):
        if self.windows or self.selecting or self.shutting_down:
            return
        if len(self.sources) >= 8:
            raise ValueError('Stop a panel before sharing another source')
        self.selecting, self.selection_error = True, ''
        def choose():
            error = C.create_string_buffer(1024)
            portal = self.native.lib.fc_portal_select(error, len(error))
            with self.lock:
                self.selecting = False
                if not portal:
                    self.selection_error = error.value.decode(errors='replace')
                elif self.shutting_down:
                    self.native.lib.fc_portal_close(portal)
                else:
                    fd, node, w, h, devices = [self.native.lib.fc_portal_value(portal, i) for i in range(5)]
                    src = 'window:' + secrets.token_hex(8)
                    self.sources[src] = dict(src=src, title='Shared window or screen', app='Linux portal',
                                             w=w, h=h, fd=fd, node=node, portal=portal, devices=devices)
        threading.Thread(target=choose, daemon=True).start()

    def stop(self, src=None):
        with self.lock:
            self.grants.revoke(src)
            sessions = [s for s in self.sessions.values() if src is None or s.src == src]
            for session in sessions:
                try:
                    session.ws.send(dict(t='close'))
                except OSError:
                    pass
                session.end()
        for session in sessions:
            if session.writer and session.writer is not threading.current_thread():
                session.writer.join(6)
        # The capture is stopped before releasing its PipeWire fd/session.
        with self.lock:
            if not self.windows:
                for key, source in list(self.sources.items()):
                    if src is None or key == src:
                        if any(s.src == key and s.writer and s.writer.is_alive() for s in sessions):
                            continue
                        self.native.lib.fc_portal_close(source['portal'])
                        self.sources.pop(key, None)
        return {'closed': len(sessions)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass  # URLs contain credentials

    def reply(self, data, status=200, content='application/json'):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def dispatch(self, method):
        agent = self.server.agent
        url = urlsplit(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if method == 'GET' and url.path == '/ping':
                return self.reply(b'frame-mac-view', content='text/plain')
            if method == 'GET' and url.path == '/view':
                return self.reply(agent.page.read_bytes(), content='text/html; charset=utf-8')
            if method == 'GET' and url.path == '/stream':
                return self.stream(q)
            if not agent.grants.master(q.get('k', self.headers.get('X-Token'))):
                return self.reply({'error': 'forbidden'}, 403)
            if method == 'POST' and url.path == '/close':
                return self.reply(agent.stop(q.get('src')))
            with agent.lock:
                windows, displays = agent.lists()
                if method == 'GET' and url.path == '/status':
                    data = dict(version=1, host='windows' if agent.windows else 'linux', screen=True,
                                accessibility=True, selecting=agent.selecting, selectionError=agent.selection_error,
                                encoder=agent.encoder, finished=[], streams=[dict(id=i, src=s.src,
                                    title=s.source.get('title', ''), stats=s.stats.summary(), controller=s.controller.state())
                                    for i, s in agent.sessions.items() if not s.stop_event.is_set()])
                elif method == 'GET' and url.path in ('/windows', '/displays'):
                    data = dict(windows=windows, displays=displays, screen=True)
                elif method == 'POST' and url.path == '/ticket':
                    agent.source(q.get('src'))
                    data = {'ticket': agent.grants.ticket(q['src'])}
                elif method == 'POST' and url.path == '/permissions':
                    agent.select()
                    data = {'selecting': agent.selecting}
                elif method == 'GET' and url.path == '/stats':
                    data = dict(now=agent.native.now(), streams=[dict(id=i, src=s.src, controller=s.controller.state(),
                         events=list(s.controller.events), **s.stats.snapshot(max(0, int(q.get('since', 0))), max(0, int(q.get('settle', 1500000)))))
                         for i, s in agent.sessions.items() if q.get('id', str(i)) == str(i) and not s.stop_event.is_set()])
                elif method == 'POST' and url.path == '/bench':
                    data = {'sent': 0}
                    for s in agent.sessions.values():
                        if s.src == q.get('src'):
                            m = {k: v for k, v in q.items() if k not in ('k', 'src')}
                            for k in ('x', 'y', 'interval'):
                                if k in m:
                                    m[k] = float(m[k])
                            s.ws.send(dict(m, t='bench'))
                            data['sent'] += 1
                else:
                    return self.reply({'error': 'not found'}, 404)
            self.reply(data)
        except (ValueError, RuntimeError) as e:
            self.reply({'error': str(e)}, 400)
        except (OSError, ConnectionError):
            self.close_connection = True

    def stream(self, query):
        agent = self.server.agent
        with agent.lock:
            key = agent.grants.redeem(query.get('src'), query)
            if not key:
                return self.reply({'error': 'forbidden'}, 403)
            source = agent.source(query.get('src'))
            if self.headers.get('Upgrade', '').lower() != 'websocket' or self.headers.get('Sec-WebSocket-Version') != '13':
                return self.reply({'error': 'expected WebSocket'}, 400)
            wskey = self.headers.get('Sec-WebSocket-Key', '')
            if len(base64.b64decode(wskey, validate=True)) != 16:
                raise ValueError('Bad WebSocket key')
            if len(agent.sessions) >= 8:
                raise ValueError('At most eight panels may be open')
            # Stop can revoke and close only a registered session. Register
            # under the same lock as redemption, before any capture starts.
            session = Session(agent, WebSocket(self), key, source, query)
            for old in list(agent.sessions.values()):
                if old.key == key:
                    old.end()
            ident = agent.next_id
            agent.next_id += 1
            agent.sessions[ident] = session
            accept = base64.b64encode(hashlib.sha1((wskey+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
        try:
            session.start()
        except (OSError, ValueError, RuntimeError):
            session.end()
        finally:
            with agent.lock:
                agent.sessions.pop(ident, None)
            self.close_connection = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['serve'])
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--page', type=Path, required=True)
    parser.add_argument('--exit-on-eof', action='store_true')
    args = parser.parse_args()
    token = os.environ.get('FRAME_MAC_VIEW_TOKEN')
    if not token:
        raise SystemExit('Frame Control must provide a private token')
    native = Native()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    agent = server.agent = Agent(native, token, args.page)
    if args.exit_on_eof:
        def eof():
            sys.stdin.buffer.read()
            with agent.lock:
                agent.shutting_down = True
            agent.stop()
            server.shutdown()
        threading.Thread(target=eof, daemon=True).start()
    print('listening on 127.0.0.1:%d' % server.server_port, flush=True)
    try:
        server.serve_forever()
    finally:
        agent.shutting_down = True
        agent.stop()
        server.server_close()


if __name__ == '__main__':
    main()
