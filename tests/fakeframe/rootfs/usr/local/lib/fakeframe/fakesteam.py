#!/usr/bin/env python3
"""A stand-in for the Frame's Steam client, run as steamos by the supervisor.

What it copies, and from where (BUILD_ID 20260922.6101926 unless noted):
- The devkit IPC Valve's devkit-utils and the devkit service's hooks use:
  ~/.steam/steam.pid (validate_steam_client), ~/.steam/steam.token, and
  "devkit-1 steam://devkit-1/<token>/<command>?<query>" lines on the
  ~/.steam/steam.pipe FIFO, answered by writing <response> or
  <response>.error (with <response>.lock while writing), as
  devkit_utils.wait_on_file_response describes. Commands: approve-ssh-key,
  create-shortcut, run-game, list-shortcuts and delete-shortcut (all that
  devkit-utils and the hooks send).
- steam:// URLs that the `steam` wrapper forwards (rungameid, install, store).
- The DevTools endpoint on 127.0.0.1:8080 with a SharedJSContext target
  (docs/steam-games.md, docs/apks.md). Expressions run in node against
  cef_shim.js.

State lives in fakeframe_state (the "steam", "devkit_games", "launches" and
"pairing_requests" keys). Anything not seen on a headset is marked "guess".
"""
import base64
import hashlib
import json
import os
import re
import secrets
import signal
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakeframe_state as fs  # noqa: E402

HOME = fs.HOME
STEAM_DIR = HOME + '/.steam'
PID_FILE, TOKEN_FILE, PIPE = (f'{STEAM_DIR}/steam.{n}' for n in ('pid', 'token', 'pipe'))
CONSOLE_LOG = fs.STEAM_ROOT + '/logs/console_log.txt'  # guess: which file Steam logs devkit launches to
DEVTOOLS_PORT = 8080
TARGET_ID = 'F0CA11ED5EA4ED0000000000000000AB'
GAME_LOGS = '/var/log/fakeframe'

# The 403 text /register returned while Steam wasn't on "Pair new host"
# (docs/ssh.md, verified 2026-09-26). approve-ssh-key passes the Steam
# client's error file through as {"error": ...}, so this is what Steam writes.
PAIRING_MODE_ERROR = 'please put the Steam client in pairing mode: Settings -> Developer -> Pair new host'
# Guess: what Steam writes when the prompt is declined hasn't been seen.
PAIRING_DENIED = 'the pairing request was denied on the device'
# Steam refused create-shortcut for ids like "fc-smoke-exe" with this text, and
# took the same program as "FCSmokeProbe" (headset smoke test, 2026-09-27,
# BUILD_ID 20260922.6101926). Valve's client only allows ids matching
# GAMEID_ALLOWED_PATTERN (devkit_client/gui2/gui2.py), so the fake checks that.
INVALID_ARGUMENTS = 'missing/invalid arguments\n'
GAMEID_ALLOWED = re.compile(r'[A-Za-z_][A-Za-z0-9_.]+')

_lock = threading.RLock()   # one state change at a time within this process (the file lock covers others)
TOKEN = secrets.token_hex(16)


def console(line):
    os.makedirs(os.path.dirname(CONSOLE_LOG), exist_ok=True)
    with open(CONSOLE_LOG, 'a') as f:
        f.write(time.strftime('[%Y-%m-%d %H:%M:%S] ') + line + '\n')


def respond(path, text=None, error=None):
    """Answer a devkit request the way devkit_utils.wait_on_file_response expects."""
    lock = path + '.lock'
    open(lock, 'w').close()
    with open(path + '.error' if error is not None else path, 'w') as f:
        f.write(error if error is not None else text)
    os.unlink(lock)


# ---- the Node side of the DevTools endpoint ----------------------------------

class JS:
    def __init__(self):
        self.proc = None
        self.next = 0

    def _start(self):
        shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cef_shim.js')
        self.proc = subprocess.Popen(['node', shim], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def evaluate(self, expression, await_promise):
        with _lock:
            if not self.proc or self.proc.poll() is not None:
                self._start()
            self.next += 1
            with fs.update() as state:
                self.proc.stdin.write(json.dumps({'id': self.next, 'expression': expression,
                                                  'awaitPromise': await_promise, 'steam': state['steam']}) + '\n')
                self.proc.stdin.flush()
                reply = json.loads(self.proc.stdout.readline())
                state['steam'] = reply['steam']
            return reply['result']


JS_WORKER = JS()


# ---- devkit commands ----------------------------------------------------------

def shortcut_for(state, gameid):
    return next((s for s in state['steam']['shortcuts'] if s.get('devkit_gameid') == gameid), None)


def new_shortcut_id(steam):
    steam['next_shortcut'] = steam.get('next_shortcut', 0) + 1
    return (0x80000000 + ((steam['next_shortcut'] * 2654435761 & 0xFFFFFFFF) >> 1)) & 0xFFFFFFFF


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def cmd_approve_ssh_key(q):
    with fs.update() as state:
        sw = state['switches']
        answer = sw['pairing_answer'] if sw['pairing_mode'] else 'not in pairing mode'
        state['pairing_requests'].append({'time': time.time(), 'request': q.get('request', ''), 'answer': answer})
    if answer == 'not in pairing mode':
        respond(q['response'], error=PAIRING_MODE_ERROR)
    elif answer == 'approve':
        respond(q['response'], text='approved')   # guess: the hook only checks that the file appears
    elif answer == 'deny':
        respond(q['response'], error=PAIRING_DENIED)
    # 'timeout': never answer; the hook gives up after 30 s.


def cmd_create_shortcut(q):
    gameid = q.get('gameid', '')
    folder = q.get('directory') or fs.DEVKIT_GAMES
    path = os.path.join(folder, gameid)
    if not GAMEID_ALLOWED.fullmatch(gameid):
        respond(q['response'], error=INVALID_ARGUMENTS)
        return
    if not os.path.isdir(path):
        respond(q['response'], error=f'no devkit game folder {path}')   # guess: wording
        return
    argv = read_json(f'{folder}/{gameid}-argv.json', [])
    settings = read_json(f'{folder}/{gameid}-settings.json', {})
    env = read_json(f'{folder}/{gameid}-env.json', {})
    with fs.update() as state:
        steam = state['steam']
        sc = shortcut_for(state, gameid)
        if not sc:
            sc = {'appid': new_shortcut_id(steam), 'name': gameid, 'exe': '', 'start_dir': path, 'icon': '',
                  'launch_options': '', 'devkit_gameid': gameid}
            steam['shortcuts'].append(sc)
        sc['exe'] = argv[0] if argv else ''
        tool = settings.get('compat_tool')
        # Steam maps the title to the chosen compat tool (CompatToolMapping and
        # compat_log.txt on the Frame, docs/sideloading.md, 2026-09-26).
        if tool:
            steam['compat_tools'][str(sc['appid'])] = tool
        else:
            steam['compat_tools'].pop(str(sc['appid']), None)
        state['devkit_games'][gameid] = {'appid': sc['appid'], 'directory': path, 'argv': argv,
                                         'settings': settings, 'env': env, 'registered': time.time()}
    console(f'devkit create-shortcut: registered devkit game "{gameid}"')
    respond(q['response'], text='')     # Steam's answer was empty on the Frame (2026-09-27)


def cmd_list_shortcuts(q):
    # The reply format devkit_utils.resolve.resolve_shortcuts asserts.
    with fs.update() as state:
        ids = sorted(state['devkit_games'])
    respond(q['response'], text=json.dumps({'version': 2, 'gameids': ids}))


def cmd_delete_shortcut(q):
    gameid = q.get('gameid', '')
    with fs.update() as state:
        sc = shortcut_for(state, gameid)
        state['devkit_games'].pop(gameid, None)
        if sc:
            state['steam']['shortcuts'].remove(sc)
            state['steam']['compat_tools'].pop(str(sc['appid']), None)
            # Remove also deleted the title's Proton prefix on the Frame (docs/sideloading.md).
            subprocess.run(['rm', '-rf', f"{fs.STEAM_ROOT}/steamapps/compatdata/{sc['appid']}"])
    console(f'devkit delete-shortcut: removed devkit game "{gameid}"')
    respond(q['response'], text=f'deleted {gameid}\n')


def cmd_run_game(q):
    gameid = q.get('gameid', '')
    with fs.update() as state:
        game = state['devkit_games'].get(gameid)
        tool = game and state['steam']['compat_tools'].get(str(game['appid']))
        runtimes = state['runtimes']
    if not game:
        respond(q['response'], error=f'unknown devkit game "{gameid}"')   # guess: wording
        return
    target = game['argv'][0] if game['argv'] else ''
    full = os.path.join(game['directory'], target.strip('"'))
    record = {'kind': 'devkit', 'gameid': gameid, 'appid': game['appid'], 'tool': tool, 'time': time.time()}
    if tool and not runtimes.get(tool, False):
        # Seen for the x86-64 runtime (docs/sideloading.md, 2026-09-26): Steam
        # logs this and the game doesn't start.
        name = fs.RUNTIME_NAMES.get(tool, tool)
        record.update(started=False, message=f'Tool {fs.RUNTIME_APPIDS.get(tool, 0)} "{name}" is found for '
                                              f'appID {game["appid"]}, but is not installed')
    elif tool and tool.startswith('proton'):
        # How Steam ran a sideloaded .exe on the Frame (docs/sideloading.md).
        prefix = f"{fs.STEAM_ROOT}/steamapps/compatdata/{game['appid']}/pfx"
        os.makedirs(prefix, exist_ok=True)
        record.update(started=True, command=f'proton waitforexitandrun "{full}"', prefix=prefix)
    else:
        # An aarch64 title ran natively, without SteamLinuxRuntime_4-arm64's
        # _v2-entry-point prefix, even though Steam recorded the mapping
        # (docs/sideloading.md, 2026-09-26). So does the fake, for real.
        record.update(started=True, command=full)
        record['run'] = (full, game['directory'], {**game.get('env', {})}, f'devkit-{gameid}')
    add_launch(record)
    console(record['message'] if not record['started'] else f'devkit run-game: started devkit game "{gameid}"')
    respond(q['response'], text='OK\n')   # guess: steam-devkit-rpc only checks that it arrives


DEVKIT = {'approve-ssh-key': cmd_approve_ssh_key, 'create-shortcut': cmd_create_shortcut,
          'list-shortcuts': cmd_list_shortcuts, 'delete-shortcut': cmd_delete_shortcut,
          'run-game': cmd_run_game}


def add_launch(record):
    """Log a launch in the state. record['run'] = (path, cwd, env, log name) also starts the
    program the way Steam would, and notes its pid and, once it ends, its exit status."""
    run, proc = record.pop('run', None), None
    if run:
        path, cwd, env, label = run
        os.makedirs(GAME_LOGS, exist_ok=True)
        with open(f'{GAME_LOGS}/{label}.log', 'ab') as log:
            try:
                proc = subprocess.Popen([path], cwd=cwd if os.path.isdir(cwd) else HOME, env={**os.environ, **env},
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                record['pid'] = proc.pid
            except OSError as e:
                record.update(pid=None, exec_error=str(e))
    with fs.update() as state:              # before the reaper looks for it, however fast the program is
        record['n'] = len(state['launches'])
        state['launches'].append(record)
    if proc:
        def reap():
            code = proc.wait()
            with fs.update() as state:
                mine = state['launches'][record['n']:record['n'] + 1]
                if mine and mine[0].get('pid') == proc.pid:   # not a reset's fresh list
                    mine[0]['exit'] = code
        threading.Thread(target=reap, daemon=True).start()


# ---- steam:// URLs from the `steam` wrapper ----------------------------------

def url_rungameid(gid):
    gid = int(gid)
    record = {'kind': 'rungameid', 'gameid': gid, 'time': time.time()}
    if gid >= 1 << 32:
        # A non-Steam shortcut: (appid << 32) | 0x02000000 (docs/apks.md).
        appid = gid >> 32
        with fs.update() as state:
            sc = next((s for s in state['steam']['shortcuts'] if s['appid'] == appid), None)
        if not sc:
            record.update(started=False, message=f'no shortcut {appid}')
        else:
            # Steam sets STEAM_FOSSILIZE_DUMP_PATH for shortcut launches but not
            # STEAM_COMPAT_SHADER_PATH (docs/apks.md, 2026-09-25).
            env = {'SteamAppId': str(appid), 'SteamGameId': str(gid),
                   'STEAM_FOSSILIZE_DUMP_PATH': f'{fs.STEAM_ROOT}/steamapps/shadercache/{appid}/fozpipelinesv6'}
            record.update(appid=appid, started=True, command=sc['exe'],
                          run=(sc['exe'], sc.get('start_dir') or HOME, env, f'shortcut-{appid}'))
    else:
        with fs.update() as state:
            app = next((a for a in state['steam']['apps'] if a['appid'] == gid), None)
        record.update(appid=gid, started=bool(app and app['installed']),
                      message=None if app and app['installed'] else 'not installed')
    add_launch(record)


def url_install(appid):
    appid = int(appid)
    free = os.statvfs(HOME)
    with fs.update() as state:
        steam = state['steam']
        app = next((a for a in steam['apps'] if a['appid'] == appid), None)
        im = steam['install_manager']
        if not app:
            return                      # not owned: Steam shows a store or license dialog, state stays 0
        im.update(currentAppID=appid, nDiskSpaceRequired=app['size'],
                  nDiskSpaceAvailable=free.f_bavail * free.f_frsize, eInstallState=app.get('wizard', 14))
        if im['eInstallState'] == 14:
            steam['download'] = {'update_appid': appid, 'update_state': 'Downloading', 'paused': False,
                                 'update_is_install': True, 'overall_percent_complete': 0,
                                 'overall_estimated_time_remaining_sec': 7,
                                 'update_network_bytes_per_second': 9500000}


def url_store(appid):
    # A store page showed up in the DevTools page list (docs/steam-games.md).
    names = {1145360: 'Hades'}
    with fs.update() as state:
        state['steam']['pages'].append({'title': f"{names.get(int(appid), 'App ' + appid)} on Steam",
                                        'url': f'https://store.steampowered.com/app/{appid}/'})


URLS = [(re.compile(r'steam://rungameid/(\d+)$'), url_rungameid),
        (re.compile(r'steam://install/(\d+)$'), url_install),
        (re.compile(r'steam://store/(\d+)$'), url_store)]


def handle_line(line):
    line = line.strip()
    if not line:
        return
    fs.log('steam.pipe', line=line)
    try:
        if line.startswith('devkit-1 '):
            m = re.fullmatch(r'steam://devkit-1/([^/]*)/([^?]*)\??(.*)', line.split(' ', 1)[1])
            if not m or m[1] != TOKEN:
                console('devkit-1: rejected a command with a bad token')
                return
            cmd = m[2].rstrip('/')               # steam-devkit-rpc sends "run-game/?..."
            q = {k: v[0] for k, v in parse_qs(m[3], keep_blank_values=True).items()}
            handler = DEVKIT.get(cmd)
            if not handler:
                console(f'devkit-1: unknown command {cmd}')
                if 'response' in q:
                    respond(q['response'], error=f'unknown command {cmd}')
                return
            handler(q)
            return
        for pat, fn in URLS:
            m = pat.match(line.split()[-1])
            if m:
                fn(*m.groups())
                return
        console(f'ignored: {line}')
    except Exception as e:  # keep reading the pipe whatever one command did
        console(f'error handling {line!r}: {type(e).__name__}: {e}')


def read_pipe():
    # O_RDWR: there is always a writer, so reads never hit EOF between clients.
    fd = os.open(PIPE, os.O_RDWR)
    with os.fdopen(fd, 'rb', buffering=0) as f:
        buf = b''
        while True:
            chunk = f.read(4096)
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                threading.Thread(target=handle_line, args=(line.decode('utf-8', 'replace'),), daemon=True).start()


# ---- DevTools HTTP + WebSocket -------------------------------------------------

def targets():
    base = {'type': 'page', 'description': '', 'faviconUrl': ''}
    out = [{**base, 'id': TARGET_ID, 'title': 'SharedJSContext',
            'url': 'https://steamloopback.host/index.html',
            'devtoolsFrontendUrl': f'/devtools/inspector.html?ws=127.0.0.1:{DEVTOOLS_PORT}/devtools/page/{TARGET_ID}',
            'webSocketDebuggerUrl': f'ws://127.0.0.1:{DEVTOOLS_PORT}/devtools/page/{TARGET_ID}'}]
    for i, p in enumerate(fs.read()['steam'].get('pages', [])):
        tid = f'{i + 1:032X}'
        out.append({**base, 'id': tid, 'title': p['title'], 'url': p['url'],
                    'webSocketDebuggerUrl': f'ws://127.0.0.1:{DEVTOOLS_PORT}/devtools/page/{tid}'})
    return out


class DevTools(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/')
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            if path != f'/devtools/page/{TARGET_ID}':
                self.send_error(404)
                return
            self.websocket()
            return
        if path in ('/json', '/json/list'):
            body = json.dumps(targets(), indent=2).encode()
        elif path == '/json/version':
            body = json.dumps({'Browser': 'Chrome/126.0.6478.183', 'Protocol-Version': '1.3',
                               'User-Agent': 'Valve Steam Client (fakeframe)'}).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=UTF-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def websocket(self):
        key = self.headers.get('Sec-WebSocket-Key', '')
        accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        self.wfile.write(b'HTTP/1.1 101 WebSocket Protocol Handshake\r\nUpgrade: WebSocket\r\n'
                         b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        self.wfile.flush()
        self.close_connection = True
        try:
            while True:
                op, data = self.read_frame()
                if op == 8:
                    return
                if op == 9:
                    self.send_frame(data, 10)
                    continue
                if op != 1:
                    continue
                msg = json.loads(data)
                reply = {'id': msg.get('id')}
                if msg.get('method') == 'Runtime.evaluate':
                    params = msg.get('params') or {}
                    reply['result'] = JS_WORKER.evaluate(str(params.get('expression', '')),
                                                         bool(params.get('awaitPromise')))
                else:
                    reply['error'] = {'code': -32601, 'message': f"'{msg.get('method')}' wasn't found"}
                self.send_frame(json.dumps(reply).encode(), 1)
        except (EOFError, OSError, ValueError):
            return

    def read_exact(self, n):
        data = self.rfile.read(n)
        if len(data) < n:
            raise EOFError
        return data

    def read_frame(self):
        b0, b1 = self.read_exact(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack('>H', self.read_exact(2))[0]
        elif n == 127:
            n = struct.unpack('>Q', self.read_exact(8))[0]
        mask = self.read_exact(4) if b1 & 0x80 else None
        data = self.read_exact(n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return b0 & 0x0F, data

    def send_frame(self, data, op):
        n = len(data)
        head = bytes([0x80 | op]) + (bytes([n]) if n < 126 else
                                     bytes([126]) + struct.pack('>H', n) if n < 1 << 16 else
                                     bytes([127]) + struct.pack('>Q', n))
        self.wfile.write(head + data)
        self.wfile.flush()


def main():
    os.makedirs(STEAM_DIR, exist_ok=True)
    if not os.path.exists(PIPE):
        os.mkfifo(PIPE, 0o600)
    with open(TOKEN_FILE, 'w') as f:
        f.write(TOKEN)
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    def stop(*_):
        # Guess: whether Steam removes steam.pid on exit. Either way
        # validate_steam_client then says Steam isn't running.
        try:
            os.unlink(PID_FILE)
        except OSError:
            pass
        if JS_WORKER.proc:
            JS_WORKER.proc.kill()
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    httpd = ThreadingHTTPServer(('127.0.0.1', DEVTOOLS_PORT), DevTools)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    console('fakesteam: started (-cef-enable-debugging on 127.0.0.1:8080)')
    read_pipe()


if __name__ == '__main__':
    main()
