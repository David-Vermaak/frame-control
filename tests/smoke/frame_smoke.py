"""Headset smoke test: the fake Frame's core cases against a real Frame.

Runs on your computer through the `frame` SSH alias (or FRAME_ALIAS), with
Frame Control's own modules, and records what happened with the headset's
BUILD_ID in tests/smoke/results/<time>-<BUILD_ID>.json (git-ignored):

- properties.json from Valve's devkit service names the login user;
- status (ui/frame_status.py) reads the build, battery and storage;
- three tiny titles (tests/smoke/tiny_programs.py: ARM64 and x86-64 static
  Linux programs, an x86-64 .exe) are installed, launched and removed with
  ui/frame_titles.py, and Steam's logs about each are kept;
- with --pair, a throwaway RSA key is paired through the devkit service
  (someone has to open Settings > Developer > Pair new host and approve it in
  the headset), checked, and taken out of authorized_keys again.

Everything it installs is removed again, also when a step fails. Titles are
named fc_smoke_*; leftovers from an interrupted run are removed first.

Usage: python3 tests/smoke/frame_smoke.py [--pair]     (or scripts/frame-smoke.sh)
Exit status: 0 all passed, 1 a step failed, 2 the headset isn't reachable.
"""
import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(ROOT / 'ui'), str(HERE)]
ALIAS = os.environ.setdefault('FRAME_ALIAS', 'frame')   # read by frame_android at import

import frame_android  # noqa: E402
import frame_connect  # noqa: E402
import frame_titles  # noqa: E402
import tiny_programs  # noqa: E402

RESULTS = HERE / 'results'
PREFIX = 'fc_smoke_'
# Earlier runs named titles fc-smoke*, which Steam refused to register but left on disk.
OLD_PREFIX = 'fc-smoke'


class Smoke:
    def __init__(self):
        self.steps = []
        self.installed = set()

    def step(self, name, fn, *args):
        """Run one step; record ok/failed, its detail and how long it took."""
        t0 = time.monotonic()
        rec = {'step': name}
        try:
            detail = fn(*args)
            rec.update(ok=True, detail=detail)
        except Exception as e:  # a failed step is a result, not the end of the run
            rec.update(ok=False, error=f'{type(e).__name__}: {e}', trace=traceback.format_exc(limit=3))
        rec['seconds'] = round(time.monotonic() - t0, 1)
        self.steps.append(rec)
        print(f"  {'ok  ' if rec['ok'] else 'FAIL'} {name} ({rec['seconds']} s)"
              + ('' if rec['ok'] else f": {rec['error']}"), flush=True)
        return rec


def ssh(cmd, timeout=60):
    return frame_android.ssh(cmd, timeout=timeout)


def ssh_config(key):
    out = subprocess.run(['ssh', '-G', ALIAS], capture_output=True, text=True, timeout=10).stdout
    for line in out.splitlines():
        k, _, v = line.partition(' ')
        if k == key:
            return v.strip()
    return None


def os_release():
    fields = {}
    for line in ssh('cat /etc/os-release').splitlines():
        k, _, v = line.partition('=')
        fields[k] = v.strip('"')
    return {k: fields.get(k) for k in ('VERSION_ID', 'VARIANT_ID', 'BUILD_ID')}


def properties():
    host = ssh_config('hostname') or ALIAS
    with urllib.request.urlopen(frame_connect.devkit_url(host, 32000, '/properties.json'), timeout=5) as r:
        props = json.load(r)
    user = ssh_config('user')
    if props.get('login') != user:
        raise AssertionError(f"properties.json says login {props.get('login')!r}; the alias logs in as {user!r}")
    return props


def status(build):
    s = json.loads(frame_android.ssh('python3 -', input=(ROOT / 'ui' / 'frame_status.py').read_text(), timeout=30))
    if s['os']['build'] != build:
        raise AssertionError(f"status says build {s['os']['build']}, /etc/os-release {build}")
    for key in ('battery', 'disk', 'memory'):
        if not s.get(key):
            raise AssertionError(f'status has no {key}')
    return {k: s.get(k) for k in ('os', 'battery', 'power', 'temp', 'services', 'volume')}


def steam_log_lines(gid):
    """What Steam logged about a title (log folder layout not verified; best effort)."""
    out = ssh(f"grep -rsh -- {shlex.quote(gid)} ~/.local/share/Steam/logs/ 2>/dev/null | tail -n 12 || true")
    return out.strip().splitlines()


def title_cycle(smoke, kind, folder):
    path = tiny_programs.write(folder, kind)
    gid = PREFIX + kind                 # named outright: the file names would share one id

    def install():
        smoke.installed.add(gid)
        meta = frame_titles.install(path, name=gid)
        listed = {t['id']: t for t in frame_titles.list_titles()}
        if gid not in listed:
            raise AssertionError(f'{gid} is not in the title list after installing')
        return {'id': gid, 'runtime': meta['runtime'], 'steam': meta.get('steam')}

    def launch():
        frame_titles.launch(gid)       # raises unless Steam confirmed it
        time.sleep(4)
        procs = ssh(f"pgrep -af -- {shlex.quote('devkit-game/' + gid)} || true").strip()
        return {'processes': procs.splitlines(), 'steam_log': steam_log_lines(gid)}

    def remove():
        frame_titles.remove(gid)
        left = ssh(f'ls -d ~/devkit-game/{gid} ~/devkit-game/{gid}-*.json 2>/dev/null || true').strip()
        if left:
            raise AssertionError(f'left behind: {left}')
        if gid in {t['id'] for t in frame_titles.list_titles()}:
            raise AssertionError(f'{gid} is still listed')
        smoke.installed.discard(gid)
        return {'removed': gid}

    if smoke.step(f'{kind}: install', install)['ok']:
        smoke.step(f'{kind}: launch', launch)
        smoke.step(f'{kind}: remove', remove)


def cleanup(smoke):
    """Remove fc-smoke-* titles: this run's, and any an interrupted run left."""
    try:
        ids = {t['id'] for t in frame_titles.list_titles() if t['id'].startswith((PREFIX, OLD_PREFIX))}
    except frame_android.FrameError:
        ids = set(smoke.installed)      # can't list: try what this run installed
    for gid in sorted(ids):
        try:
            frame_titles.remove(gid)
        except frame_android.FrameError as e:
            print(f'  could not remove {gid}: {e}')
    return sorted(ids)


def pair(folder):
    """Pair a throwaway RSA key through the devkit service, check it, then remove it."""
    key = os.path.join(folder, 'id_rsa_smoke')
    subprocess.run(['ssh-keygen', '-q', '-t', 'rsa', '-b', '3072', '-N', '', '-C', 'frame-control-smoke',
                    '-f', key], check=True)
    pub = Path(key + '.pub').read_text()
    host = ssh_config('hostname') or ALIAS
    user = ssh_config('user')
    comment = frame_connect.key_comment(platform.node()).replace('frame-control@', 'frame-control-smoke@')
    print('    In the headset: Steam Settings > Developer > Pair new host, then approve '
          f'"{comment}" (waits up to {frame_connect.PAIRING_MODE_WAIT} s)', flush=True)
    b64 = pub.split()[1]
    try:
        reason = frame_connect.devkit_pair(host, pub, comment)
        if reason:
            raise AssertionError(reason)
        r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=8',
                            '-o', 'ControlPath=none', '-i', key, f'{user}@{host}', 'true'], capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(f'paired, but the key does not log in: {r.stderr.strip()}')
        return {'comment': comment}
    finally:
        f = '~/.ssh/authorized_keys'
        ssh(f'grep -vF {b64} {f} > {f}.smoke; cat {f}.smoke > {f}; rm -f {f}.smoke')
        if b64 in ssh(f'cat {f}'):
            raise AssertionError('the smoke key is still in authorized_keys')


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--pair', action='store_true', help='also pair through the devkit service (needs you in the headset)')
    args = ap.parse_args()

    r = subprocess.run(['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes', ALIAS, 'true'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f'{ALIAS} is not reachable over SSH: {r.stderr.strip()}', file=sys.stderr)
        return 2

    smoke = Smoke()
    started = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    release = os_release()
    build = release['BUILD_ID'] or 'unknown'
    print(f'==> Frame smoke test on {ALIAS}: SteamOS {release["VERSION_ID"]} ({release["VARIANT_ID"]}), build {build}')
    left = cleanup(smoke)
    if left:
        print(f'  removed leftovers from an earlier run: {", ".join(left)}')
    folder = tempfile.mkdtemp(prefix='frame-smoke-')
    try:
        smoke.step('devkit service: properties.json', properties)
        smoke.step('status', status, build)
        for kind in ('arm64', 'x86_64', 'exe'):
            title_cycle(smoke, kind, folder)
        if args.pair:
            smoke.step('devkit pairing (throwaway key)', pair, folder)
    finally:
        removed = cleanup(smoke)
        subprocess.run(['rm', '-rf', folder])
    passed = sum(s['ok'] for s in smoke.steps)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{started.replace(':', '')}-{build}.json"
    out.write_text(json.dumps({'started': started, 'alias': ALIAS, 'os_release': release, 'paired': args.pair,
                               'passed': passed, 'failed': len(smoke.steps) - passed,
                               'cleanup_removed': removed, 'steps': smoke.steps}, indent=1))
    print(f'==> {passed}/{len(smoke.steps)} steps passed on build {build}; results in {out.relative_to(ROOT)}')
    return 0 if passed == len(smoke.steps) else 1


if __name__ == '__main__':
    sys.exit(main())
