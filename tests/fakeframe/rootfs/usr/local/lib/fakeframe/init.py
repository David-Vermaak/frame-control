#!/usr/bin/env python3
"""The fake Frame's supervisor, run as root under docker's init.

It starts and stops sshd, Valve's steamos-devkit-service (as steamos),
fakesteam (as steamos) and a few named placeholder processes the status page
looks for, following the switches in the state file. It also serves the
control port (9999) that fakeframe-ctl and the e2e tests use, so a test can
flip a fault switch even while SSH is down.

Control: GET /state, GET /calls, GET /ping, POST /ctl {"args": ["pairing", "on"]}.
See `fakeframe-ctl help` for the commands.
"""
import glob
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakeframe_state as fs  # noqa: E402

LIB = os.path.dirname(os.path.abspath(__file__))
USER = 'steamos'
PW = pwd.getpwnam(USER)
HOME = fs.HOME
KEYS = '/keys'                                  # shared with the host container
HARNESS_KEY = KEYS + '/id_ed25519_frame'
AUTH_KEYS = HOME + '/.ssh/authorized_keys'
BALLAST = fs.DEVKIT_GAMES + '/.fakeframe-ballast'
# Written here; compose mounts the same volumes over /sys/class/power_supply and /sys/class/thermal.
POWER = '/var/lib/fakeframe/sys/power_supply'
THERMAL = '/var/lib/fakeframe/sys/thermal'
CONTROL_PORT = 9999
LOGS = '/var/log/fakeframe'
USAGE = """fakeframe-ctl COMMAND
  pairing on|off              Steam's "Pair new host" screen open or not
  answer approve|deny|timeout how the pairing prompt is answered
  steam on|off                Steam client running (steam.pid, pipe, DevTools on 8080)
  sleep on|off                headset asleep: 22 and 32000 accept but never answer
  sshd on|off                 sshd running (off: connection refused)
  devkit-service on|off       steamos-devkit-service on 32000
  disk-full on|off            fill the small ~/devkit-game filesystem
  runtime NAME installed|missing   NAME: proton-experimental, proton-stable,
                              SteamLinuxRuntime_4-arm64, SteamLinuxRuntime_4, lepton
  battery KEY=VALUE...        e.g. capacity=15 status=Discharging current_now=-900000
  keys harness|none           authorized_keys: only the harness key, or empty
  authorized-keys             what ~/.ssh/authorized_keys holds now
  reset                       default state, nothing installed, harness key only
  state | calls [TOOL] | ping"""

_lock = threading.RLock()
_procs = {}
_blackhole = {'socks': [], 'conns': []}


def log(msg):
    print(time.strftime('%H:%M:%S ') + msg, flush=True)


def as_user():
    env = {'HOME': HOME, 'USER': USER, 'LOGNAME': USER, 'SHELL': '/bin/bash',
           'PATH': '/usr/local/bin:/usr/bin:/bin', 'XDG_RUNTIME_DIR': f'/run/user/{PW.pw_uid}',
           'LANG': 'C.UTF-8'}
    return {'user': PW.pw_uid, 'group': PW.pw_gid, 'extra_groups': [], 'env': env, 'cwd': HOME}


def chown(path):
    os.chown(path, PW.pw_uid, PW.pw_gid)


# ---- processes ------------------------------------------------------------------

def spawn(name, argv, user=True, env=None):
    os.makedirs(LOGS, exist_ok=True)
    out = open(f'{LOGS}/{name}.log', 'ab')
    kw = as_user() if user else {'env': dict(os.environ)}
    kw['env'].update(env or {})
    _procs[name] = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                    start_new_session=True, **kw)
    out.close()
    log(f'started {name} (pid {_procs[name].pid})')


def stop(name, sig=15):
    p = _procs.pop(name, None)
    if p and p.poll() is None:
        p.send_signal(sig)
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        log(f'stopped {name}')


def running(name):
    p = _procs.get(name)
    return bool(p and p.poll() is None)


def kill_ssh_sessions():
    """Drop every SSH connection, as losing Wi-Fi does."""
    for comm_file in glob.glob('/proc/[0-9]*/comm'):
        try:
            with open(comm_file) as f:
                comm = f.read().strip()
            if comm.startswith('sshd'):
                os.kill(int(comm_file.split('/')[2]), 9)
        except (OSError, ValueError):
            pass


class Blackhole:
    """Accept on a port and never answer, so ssh waits and times out. An unreachable
    Frame times out rather than refusing (seen over Tailscale on 2026-09-27);
    a closed port would fail at once, which hides timeout bugs."""

    @staticmethod
    def start(port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', port))
        s.listen(64)
        _blackhole['socks'].append(s)

        def accept():
            while True:
                try:
                    c, _ = s.accept()
                except OSError:
                    return
                _blackhole['conns'].append(c)
        threading.Thread(target=accept, daemon=True).start()

    @staticmethod
    def stop():
        for s in _blackhole['socks'] + _blackhole['conns']:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        _blackhole['socks'].clear()
        _blackhole['conns'].clear()


def reconcile():
    """Make the running processes match the switches."""
    with _lock:
        sw = fs.read()['switches']
        asleep = sw['asleep']
        if asleep and not _blackhole['socks']:
            stop('sshd')
            stop('devkit-service')
            kill_ssh_sessions()
            Blackhole.start(22)
            Blackhole.start(32000)
        if not asleep and _blackhole['socks']:
            Blackhole.stop()
        want = {'sshd': sw['sshd'] and not asleep, 'devkit-service': sw['devkit_service'] and not asleep,
                'steam': sw['steam'], 'vrserver': True, 'plasmashell': True}
        for name, on in want.items():
            if on and not running(name):
                start(name)
            elif not on and running(name):
                stop(name)


def start(name):
    if name == 'sshd':
        spawn('sshd', ['/usr/bin/sshd', '-D', '-e'], user=False)
    elif name == 'devkit-service':
        # Valve's service, unmodified, with a stand-in dbus module (no systemd-resolved here).
        spawn('devkit-service', ['python3', '/usr/lib/steamos-devkit/steamos-devkit-service.py'],
              env={'PYTHONPATH': f'{LIB}/pystubs'})
    elif name == 'steam':
        spawn('steam', ['python3', f'{LIB}/fakesteam.py'])
    else:
        # frame_status.py looks for these by process name (vrserver: SteamVR,
        # plasmashell: the headset desktop, which /api/clipboard also needs).
        spawn(name, [f'{LIB}/bin/{name}', 'infinity'],
              env={'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{PW.pw_uid}/bus'})


# ---- the device's files -----------------------------------------------------------

def write(path, text, owner=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    if owner:
        chown(path)


def sysfs(state):
    """Battery, charger and thermal zones, in the files frame_status.py reads.

    /sys is read-only in a container, so compose mounts a volume over each of
    the two folders and again here, where it can be written
    (tests/fakeframe/compose.yaml); without those this does nothing.
    """
    b = state['battery']
    if not os.path.isdir(POWER) or not os.path.isdir(THERMAL):
        log('no fake /sys: see the volumes in tests/fakeframe/compose.yaml')
        return
    try:
        for d in glob.glob(POWER + '/*') + glob.glob(THERMAL + '/thermal_zone*'):
            shutil.rmtree(d, ignore_errors=True)
        bat = POWER + '/max1720x_battery'   # the fuel gauge frame_status.py was written against
        for k in ('capacity', 'status', 'voltage_now', 'current_now', 'time_to_full_now', 'temp', 'health'):
            write(f'{bat}/{k}', f'{b[k]}\n', owner=False)
        write(f'{bat}/type', 'Battery\n', owner=False)
        c = b.get('charger') or {}
        usb = POWER + '/usb'
        write(f'{usb}/type', 'USB\n', owner=False)
        write(f'{usb}/online', f"{c.get('online', 0)}\n", owner=False)
        for k in ('usb_type', 'voltage_now', 'current_now'):
            if k in c:
                write(f'{usb}/{k}', f'{c[k]}\n', owner=False)
        for i, t in enumerate(state['thermal_mc']):
            write(f'{THERMAL}/thermal_zone{i}/temp', f'{t}\n', owner=False)
    except OSError as e:
        log(f'no fake /sys ({e}); see the volumes in tests/fakeframe/compose.yaml')


def runtimes(state):
    """Installed compat tools as Steam app manifests, and the Lepton launcher itself."""
    # Verified 2026-09-28, BUILD_ID 20260925.6191901: both parents are
    # steamos:steamos 0755. The root supervisor must not leave them root-owned
    # when creating Steam's fake manifests; user-account app installs need them.
    for directory in (HOME + '/.local', HOME + '/.local/share'):
        os.makedirs(directory, mode=0o755, exist_ok=True)
        chown(directory)
        os.chmod(directory, 0o755)
    apps = fs.STEAM_ROOT + '/steamapps'
    for alias, installed in state['runtimes'].items():
        acf = f'{apps}/appmanifest_{fs.RUNTIME_APPIDS[alias]}.acf'
        if installed:
            write(acf, '"AppState"\n{\n\t"appid"\t\t"%d"\n\t"name"\t\t"%s"\n\t"SizeOnDisk"\t\t"%d"\n}\n'
                  % (fs.RUNTIME_APPIDS[alias], fs.RUNTIME_NAMES[alias], 900000000))
        elif os.path.exists(acf):
            os.unlink(acf)
    if state['runtimes'].get('lepton'):
        os.makedirs(os.path.dirname(fs.LEPTON), exist_ok=True)
        shutil.copy(f'{LIB}/lepton.py', fs.LEPTON)
        os.chmod(fs.LEPTON, 0o755)
    elif os.path.exists(fs.LEPTON):
        os.unlink(fs.LEPTON)
    for app in state['steam']['apps']:
        acf = f"{apps}/appmanifest_{app['appid']}.acf"
        if app['installed']:
            write(acf, '"AppState"\n{\n\t"appid"\t\t"%d"\n\t"name"\t\t"%s"\n\t"SizeOnDisk"\t\t"%d"\n}\n'
                  % (app['appid'], app['display_name'], app['size']))
    subprocess.run(['chown', '-R', f'{USER}:{USER}', fs.STEAM_ROOT])


def disk_full(on):
    if on:
        if os.path.ismount(fs.DEVKIT_GAMES) is False:
            raise ValueError(f'disk-full needs {fs.DEVKIT_GAMES} to be its own small filesystem (compose tmpfs)')
        st = os.statvfs(fs.DEVKIT_GAMES)
        size = max(0, st.f_bavail * st.f_frsize - 64 * 1024)
        fd = os.open(BALLAST, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.posix_fallocate(fd, 0, size)
        finally:
            os.close(fd)
    elif os.path.exists(BALLAST):
        os.unlink(BALLAST)


def set_keys(which):
    os.makedirs(os.path.dirname(AUTH_KEYS), mode=0o700, exist_ok=True)
    chown(os.path.dirname(AUTH_KEYS))
    text = ''
    if which == 'harness':
        with open(HARNESS_KEY + '.pub') as f:
            text = f.read()
    write(AUTH_KEYS, text)
    os.chmod(AUTH_KEYS, 0o600)


def harness_key():
    """The key the host container logs in with, shared through the /keys volume."""
    os.makedirs(KEYS, exist_ok=True)
    if not os.path.exists(HARNESS_KEY):
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'fakeframe-harness',
                        '-f', HARNESS_KEY], check=True)
    os.chmod(HARNESS_KEY, 0o644)        # the host container copies it into its own ~/.ssh with 0600


def clean_home():
    """Everything Frame Control or a test put on the "headset"."""
    for c in list(fs.read()['lepton'].values()):
        try:
            os.kill(c['pid'], 15)
        except (OSError, KeyError, TypeError):
            pass
    for pattern in (fs.DEVKIT_GAMES + '/*', fs.DEVKIT_GAMES + '/.[!.]*', HOME + '/devkit-utils',
                    HOME + '/.devkit-utils.frame-control', HOME + '/Applications', HOME + '/Downloads/*',
                    fs.STEAM_ROOT + '/steamapps/compatdata', fs.STEAM_ROOT + '/steamapps/shadercache',
                    fs.STEAM_ROOT + '/logs', '/var/log/fakeframe/devkit-*', '/var/log/fakeframe/shortcut-*'):
        for p in glob.glob(pattern):
            subprocess.run(['rm', '-rf', p])
    os.makedirs(HOME + '/Downloads', exist_ok=True)
    chown(HOME + '/Downloads')
    chown(fs.DEVKIT_GAMES)


def apply_files():
    state = fs.read()
    sysfs(state)
    runtimes(state)


def reset():
    with _lock:
        stop('steam')
        clean_home()
        fs.reset()
        env_switches()
        set_keys('harness')
        disk_full(False)
        apply_files()
        reconcile()


def env_switches():
    """FAKEFRAME_PAIRING_MODE=1 and the like set a switch's value at start."""
    with fs.update() as s:
        for k in s['switches']:
            v = os.environ.get('FAKEFRAME_' + k.upper())
            if v is not None:
                s['switches'][k] = v if k == 'pairing_answer' else v in ('1', 'on', 'true', 'yes')


# ---- commands ----------------------------------------------------------------------

ON_OFF = {'on': True, 'off': False}
SWITCH = {'pairing': 'pairing_mode', 'steam': 'steam', 'sleep': 'asleep', 'sshd': 'sshd',
          'devkit-service': 'devkit_service'}


def command(args):
    if not args or args[0] == 'help':
        return {'usage': USAGE}
    cmd, rest = args[0], args[1:]
    if cmd == 'state':
        return fs.read()
    if cmd == 'calls':
        return fs.calls(rest[0] if rest else None)
    if cmd == 'ping':
        return ping()
    if cmd == 'reset':
        reset()
        return {'ok': True}
    if cmd in SWITCH and len(rest) == 1 and rest[0] in ON_OFF:
        with fs.update() as s:
            s['switches'][SWITCH[cmd]] = ON_OFF[rest[0]]
        reconcile()
        return {'ok': True, SWITCH[cmd]: ON_OFF[rest[0]]}
    if cmd == 'answer' and rest and rest[0] in ('approve', 'deny', 'timeout'):
        with fs.update() as s:
            s['switches']['pairing_answer'] = rest[0]
        return {'ok': True}
    if cmd == 'disk-full' and len(rest) == 1 and rest[0] in ON_OFF:
        with _lock:
            disk_full(ON_OFF[rest[0]])
            with fs.update() as s:
                s['switches']['disk_full'] = ON_OFF[rest[0]]
        return {'ok': True}
    if cmd == 'runtime' and len(rest) == 2 and rest[1] in ('installed', 'missing'):
        with fs.update() as s:
            if rest[0] not in s['runtimes']:
                raise ValueError(f'unknown runtime {rest[0]}')
            s['runtimes'][rest[0]] = rest[1] == 'installed'
        apply_files()
        return {'ok': True}
    if cmd == 'battery' and rest:
        with fs.update() as s:
            for kv in rest:
                k, _, v = kv.partition('=')
                if k not in s['battery'] or k == 'charger':
                    raise ValueError(f'unknown battery field {k}')
                s['battery'][k] = int(v) if v.lstrip('-').isdigit() else v
        apply_files()
        return {'ok': True}
    if cmd == 'keys' and rest and rest[0] in ('harness', 'none'):
        set_keys(rest[0])
        return {'ok': True}
    if cmd == 'authorized-keys':
        with open(AUTH_KEYS) as f:
            return {'text': f.read()}
    raise ValueError(f'unknown command {" ".join(args)!r}; try help')


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def ping():
    sw = fs.read()['switches']
    checks = {}
    if sw['sshd'] and not sw['asleep']:
        checks['sshd'] = port_open(22)
    if sw['devkit_service'] and not sw['asleep']:
        checks['devkit_service'] = port_open(32000)
    if sw['steam']:
        checks['devtools'] = port_open(8080) and fs.steam_pid() is not None
    return {'ok': all(checks.values()), 'checks': checks}


class Control(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition('?')
        args = {'/state': ['state'], '/calls': ['calls'] + ([query] if query else []), '/ping': ['ping']}.get(path)
        if not args:
            self.reply({'error': 'not found'}, 404)
            return
        self.reply(command(args))

    def do_POST(self):
        if self.path != '/ctl':
            self.reply({'error': 'not found'}, 404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
            self.reply(command([str(a) for a in body.get('args', [])]))
        except (ValueError, OSError) as e:
            self.reply({'error': str(e)}, 400)


def main():
    os.umask(0o022)
    harness_key()
    os.makedirs(fs.DIR, mode=0o777, exist_ok=True)
    os.chmod(fs.DIR, 0o777)
    os.makedirs(f'/run/user/{PW.pw_uid}', exist_ok=True)
    chown(f'/run/user/{PW.pw_uid}')
    reset()
    httpd = ThreadingHTTPServer(('0.0.0.0', CONTROL_PORT), Control)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f'fake Frame up; control on :{CONTROL_PORT}')
    while True:                         # restart anything that died unasked, as systemd would
        time.sleep(1)
        try:
            reconcile()
        except Exception as e:  # keep supervising
            log(f'reconcile: {type(e).__name__}: {e}')


if __name__ == '__main__':
    main()
