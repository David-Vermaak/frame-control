"""Opt-in session worker ON the Frame; no root, extra apps, or power actions.

One worker per user, shared by desktop and phone. State survives companion
connections, not headset reboots. See docs/family-comfort.md for guarantees.
"""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from frame_steam import Page
from frame_status import battery, thermal_alerts, activity_level

ROOT = Path.home() / '.local/state/frame-control/comfort'
VRCMD = '/opt/steamvr/bin/linuxarm64/vrcmd'
HOME_JS = """(async () => {
  SteamUIStore.Navigate('/library/home');
  await SteamClient.OpenVR.VROverlay.ShowDashboard('valve.steam.gamepadui.main');
  if (!await SteamClient.OpenVR.VROverlay.IsDashboardVisible()) throw Error('Steam dashboard did not open');
  return {path: location.pathname};
})()"""


def clock():
    # CLOCK_BOOTTIME includes headset suspend; wall-clock corrections don't alter limits.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def boot():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def validate(body):
    if not isinstance(body, dict) or body.get('action') not in ('status', 'start', 'cancel'):
        raise ValueError('Choose status, start or cancel')
    if body['action'] == 'start':
        for key, low, high in (('minutes', 1, 240), ('breakMinutes', 0, 120), ('stillMinutes', 0, 240)):
            n = body.get(key)
            if type(n) is not int or not low <= n <= high:
                raise ValueError(f'{key} must be a whole number from {low} to {high}')
        for key in ('batteryAlert', 'heatAlert'):
            if type(body.get(key)) is not bool:
                raise ValueError(f'{key} must be true or false')
    return body


def new_session(body, now, boot_id):
    return {'id': uuid.uuid4().hex, 'boot': boot_id, 'active': True,
            'options': {k: body[k] for k in ('minutes', 'breakMinutes', 'stillMinutes', 'batteryAlert', 'heatAlert')},
            'started': now, 'deadline': now + body['minutes'] * 60, 'lastSample': now,
            'used': 0, 'nextBreak': body['breakMinutes'] * 60, 'stillSent': False,
            'warned': None, 'events': [], 'seq': 0, 'latched': [], 'error': None}


def event(s, kind, message):
    s['seq'] += 1
    s['events'].append({'id': s['id'] + ':' + str(s['seq']), 'kind': kind,
                        'message': message, 'time': time.time()})
    s['events'] = s['events'][-40:]


def notify(message):
    r = subprocess.run([VRCMD, '--notify', 'Frame Control: ' + message],
                       capture_output=True, text=True, timeout=20)
    if r.returncode or 'succeeded' not in r.stdout:
        raise RuntimeError('SteamVR could not show the reminder: ' + (r.stderr or r.stdout)[-300:])


def home():
    page = Page()
    try:
        result = page.eval(HOME_JS)
        if result.get('path') != '/routes/library/home':
            raise RuntimeError('Steam did not navigate Home')
    finally:
        page.sock.close()


def tick(s, now, sample, warn=notify, go_home=home):
    """One deterministic step; injected actions/samples also exercise a fake Frame."""
    if not s.get('active'):
        return
    o = s['options']
    s['heartbeat'] = now
    delta = max(0, min(30, now - s['lastSample']))
    s['lastSample'] = now
    level = sample.get('activity')
    b = sample.get('battery') or {}
    s['unavailable'] = []
    if o['batteryAlert'] and b.get('percent') is None:
        s['unavailable'].append('battery')
    if o['heatAlert'] and sample.get('thermal') is None:
        s['unavailable'].append('temperature')
    if (o['breakMinutes'] or o['stillMinutes']) and level is None:
        s['unavailable'].append('activity')
    s['activity'] = level
    if level in (1, 2):
        s['used'] += delta
    elif level is not None:
        s['used'] = 0
        s['nextBreak'] = o['breakMinutes'] * 60
        s['stillSent'] = False
    # Missing samples never count as time worn. No catch-up burst after a disconnect.
    if now >= s['deadline'] - 60 and s['warned'] is None:
        warn('One minute left. Save your progress; Steam Home will open.')
        s['warned'] = now
        event(s, 'warning', 'One minute left. Save your progress; Steam Home will open.')
    if s['warned'] is not None and now >= max(s['deadline'], s['warned'] + 60):
        go_home()
        s['active'] = False
        event(s, 'finished', 'Session ended: Steam Home opened. Your game is still running.')
        return
    if o['breakMinutes'] and s['used'] >= s['nextBreak']:
        warn('Time for a break. Take off the headset and rest your eyes.')
        event(s, 'break', 'Time for a break. Take off the headset and rest your eyes.')
        s['nextBreak'] = s['used'] + o['breakMinutes'] * 60
    if o['stillMinutes'] and not s['stillSent'] and s['used'] >= o['stillMinutes'] * 60:
        event(s, 'still', f"Headset still active after {o['stillMinutes']} active minute(s). Check in with the wearer.")
        s['stillSent'] = True
    low = b.get('percent') is not None and b['percent'] <= 15 and b.get('status') == 'Discharging'
    hot = sample.get('thermal')
    for kind, enabled, value, message in (
        ('battery', o['batteryAlert'], low if b else None, 'Frame battery is low (15% or less).'),
        ('heat', o['heatAlert'], bool(hot) if hot is not None else None,
         'Frame reports a hot/critical thermal trip or battery overheat. Take a break.')):
        if enabled and value and kind not in s['latched']:
            event(s, kind, message)
            s['latched'].append(kind)
        elif value is False and kind in s['latched']:
            # Battery hysteresis prevents repeated alerts around 15%.
            if kind != 'battery' or b.get('status') == 'Charging' or (b.get('percent') or 0) >= 20:
                s['latched'].remove(kind)


@contextlib.contextmanager
def locked(name='state.lock', nonblocking=False):
    import fcntl  # only needed ON the Linux headset, not by desktop validation/tests
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / name).open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield f


def read_state():
    try:
        return json.loads((ROOT / 'session.json').read_text())
    except FileNotFoundError:
        return {'active': False, 'events': []}


def save(s):
    p = ROOT / 'session.tmp'
    p.write_text(json.dumps(s))
    p.chmod(0o600)
    p.replace(ROOT / 'session.json')


def current(s, now):
    if s.get('active') and s.get('boot') != boot():
        s['active'] = False
        s['error'] = 'Headset restarted. Start a new session.'
    out = dict(s)
    out['time'] = time.time()  # event age uses the Frame's clock, not the phone's
    out['remaining'] = max(0, max(s.get('deadline', now), (s.get('warned') or 0) + 60) - now) if s.get('active') else 0
    if s.get('active') and now - s.get('heartbeat', s['started']) > 90:
        out['error'] = 'Session worker is not responding. Timer enforcement is unverified; cancel and start again.'
    return out


def watch():
    try:
        with locked('worker.lock', nonblocking=True) as worker:
            while True:
                with locked():
                    s = current(read_state(), clock())
                    if not s.get('active'):
                        save(s)
                        # Release ownership before state.lock: a concurrent start
                        # cannot miss the gap between an old worker and its exit.
                        import fcntl
                        fcntl.flock(worker, fcntl.LOCK_UN)
                        return
                    try:
                        b = battery()
                        hot = thermal_alerts()
                        if b and b.get('health') == 'Overheat':
                            hot = (hot or []) + ['battery']
                        tick(s, clock(), {'battery': b, 'thermal': hot, 'activity': activity_level()})
                        s['error'] = None
                    except Exception as e:
                        error = str(e)
                        if s.get('error') != error:
                            event(s, 'error', 'Session action failed: ' + error)
                        s['error'] = error
                    save(s)
                time.sleep(5)
    except BlockingIOError:
        pass  # another connection already started the single worker


def command(body):
    validate(body)
    with locked():
        s = current(read_state(), clock())
        if body['action'] == 'start':
            if s.get('active'):
                raise ValueError('A session is already running. Cancel it before starting another.')
            s = new_session(body, clock(), boot())
            event(s, 'started', 'Session started. Steam Home opens at the limit; games are not closed.')
        elif body['action'] == 'cancel':
            s['active'] = False
            if s.get('id'):
                event(s, 'cancelled', 'Session timer and monitoring cancelled.')
        save(s)
        if body['action'] == 'start':
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--watch'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
        return current(s, clock())


if __name__ == '__main__':
    if sys.argv[1:] == ['--watch']:
        watch()
    else:
        try:
            print(json.dumps(command(json.loads(sys.argv[1]))))
        except Exception as e:
            print(json.dumps({'error': str(e)}))
            sys.exit(1)
