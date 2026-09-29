"""Frame Control's optional performance HUD, using Frame platform tools only.

Controls remain blocked pending idle-headset verification; no playspace writes
are exposed. See docs/vr-utilities.md for the probe evidence and follow-up.
"""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from frame_status import battery, max_temp, performance

ROOT = Path.home() / '.local/share/frame-control/vr'
APPID = '2000250025'


def validate(body):
    action = body.get('action')
    if action not in ('hud-start', 'hud-stop'):
        raise ValueError('VR action must be hud-start or hud-stop; playspace controls are not yet verified')
    return action


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def process_identity(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if b'frame-control-hud' in args and b'xterm' in Path(f'/proc/{pid}/comm').read_bytes():
            return stat[19]  # field 22, starttime; protects against PID reuse
    except OSError:
        pass
    return None


def panel(action):
    path = ROOT / 'hud.json'
    saved = json.loads(path.read_text()) if path.exists() else {}
    pid = saved.get('pid')
    running = bool(pid and saved.get('start') and process_identity(pid) == saved['start'])
    if action == 'hud-stop':
        if running:
            os.kill(pid, signal.SIGTERM)  # xterm closes the PTY; child exits on HUP
        path.unlink(missing_ok=True)
        return {'message': 'HUD closed.'}
    if running:
        return {'message': 'HUD is already open. Find Frame Control HUD in the SteamVR dashboard.'}
    env = {**os.environ, 'DISPLAY': ':0'}
    p = subprocess.Popen(['xterm', '-name', 'frame-control-hud', '-title', 'Frame Control HUD',
                          '-fa', 'Monospace', '-fs', '20', '-geometry', '56x16',
                          '-bg', '#171d25', '-fg', '#d6d7d8', '-e',
                          sys.executable, str(ROOT / 'frame_vr.py'), 'hud'],
                         env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and p.poll() is None:
            tree = subprocess.check_output(['xwininfo', '-display', ':0', '-root', '-tree'],
                                           text=True, timeout=2)
            for w in re.findall(r'(0x[0-9a-f]+).*"frame-control-hud"', tree):
                owner = subprocess.check_output(['xprop', '-display', ':0', '-id', w, '_NET_WM_PID'],
                                                text=True, timeout=2)
                if owner.strip().endswith('= ' + str(p.pid)):
                    subprocess.run(['xprop', '-display', ':0', '-id', w, '-f', 'STEAM_GAME', '32c',
                                    '-set', 'STEAM_GAME', APPID], check=True, timeout=2)
                    identity = process_identity(p.pid)
                    if not identity:
                        raise RuntimeError('HUD process exited before its panel was ready')
                    save(path, {'pid': p.pid, 'start': identity})
                    return {'message': 'HUD opened. In SteamVR, select Frame Control HUD and Float in World or dock it to a controller.'}
            time.sleep(.1)
        raise RuntimeError('HUD did not create a window; is gamescope running?')
    except BaseException:
        if p.poll() is None:
            p.terminate()
            p.wait(timeout=3)
        raise


def hud():
    def fmt(v, unit=''):
        return 'unavailable' if v is None else f'{v:.1f}{unit}'
    while True:
        p, b = performance(), battery() or {}
        lines = ['FRAME CONTROL HUD', '',
                 'Compositor FPS    ' + fmt(p['compositorFps']),
                 'Compositor period ' + fmt(p['frameMs'], ' ms'),
                 'Application FPS   ' + fmt(p['appFps']),
                 'Render GPU time   ' + fmt(p['gpuMs'], ' ms'),
                 'Compositor CPU    ' + fmt(p['compositorCpuMs'], ' ms'),
                 'System CPU        ' + fmt(p['cpuPercent'], '%'),
                 'GPU clock         ' + fmt(p['gpuMHz'], ' MHz'),
                 'Hottest sensor    ' + fmt(max_temp(), ' C'),
                 'Battery           ' + fmt(b.get('percent'), '%'), '',
                 time.strftime('Updated %H:%M:%S'), 'Close this window to stop.']
        print('\033[2J\033[H' + '\n'.join(lines), flush=True)
        time.sleep(2)


def dispatch(body):
    import fcntl  # Frame only; validation is also imported by Windows hosts
    action = validate(body)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / 'control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return panel(action)


if __name__ == '__main__':
    if sys.argv[1:] == ['hud']:
        hud()
    else:
        try:
            print(json.dumps(dispatch(json.load(sys.stdin))))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
            print(json.dumps({'error': str(e)}))
            sys.exit(1)
