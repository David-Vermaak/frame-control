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

Everything it installs is removed again, also when a step fails: the titles
(named fc_smoke_*, and leftovers of an interrupted run first), their Steam
shortcuts, the paired key, and ~/devkit-utils if it wasn't there before (if it
was, it stays, synced to this checkout as Frame Control always does). A
cleanup that fails is a failed step.

Usage: python3 tests/smoke/frame_smoke.py [--pair]     (or scripts/frame-smoke.sh)
Env:   FRAME_ALIAS (default frame), FRAME_SMOKE_RESULTS (default tests/smoke/results)
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

RESULTS = Path(os.environ.get('FRAME_SMOKE_RESULTS') or HERE / 'results')
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


def steam_log_lines(*patterns):
    """Steam log lines holding any of the fixed strings (which files: not verified, so all)."""
    pats = ' '.join(f'-e {shlex.quote(p)}' for p in patterns)
    out = ssh(f"grep -rshF {pats} ~/.local/share/Steam/logs/ 2>/dev/null || true")
    return out.strip().splitlines()


# What a launch must show, per kind. The x86-64 runtime isn't installed on the
# Frame, so Steam acknowledges that launch and logs "... is not installed"
# instead (docs/sideloading.md, 2026-09-26): recorded, not a failure.
EXPECTED = {'arm64': ('running',), 'x86_64': ('running', 'runtime missing'), 'exe': ('running', 'started')}


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
        mine = (f'devkit-game/{gid}', f'"{gid}"')
        # The missing-runtime line names Steam's app id, which we don't know, so
        # any new one counts; nothing else is launching during the test.
        seen, seen_missing = len(steam_log_lines(*mine)), len(steam_log_lines('but is not installed'))
        frame_titles.launch(gid)       # raises unless Steam confirmed it
        outcome, procs, new = 'no evidence', [], []
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            # [d]: the pattern mustn't match the shell running pgrep, whose command line holds it.
            procs = ssh(f"pgrep -af -- '[d]evkit-game/{gid}/' || true").strip().splitlines()
            new = steam_log_lines(*mine)[seen:]
            missing = steam_log_lines('but is not installed')[seen_missing:]
            if procs:
                outcome = 'running'
            elif missing:
                outcome, new = 'runtime missing', new + missing
            elif any('started devkit game' in line or 'chdir' in line for line in new):
                outcome = 'started'
            if outcome != 'no evidence':
                break
            time.sleep(0.5)
        detail = {'outcome': outcome, 'processes': procs, 'steam_log': new[-12:]}
        if outcome not in EXPECTED[kind]:
            raise AssertionError(f"Steam acknowledged the launch, but {outcome} (expected "
                                 f"{' or '.join(EXPECTED[kind])}): {json.dumps(detail)[:400]}")
        return detail

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
    """Remove this run's titles and any fc_smoke_* an interrupted run left: the folder,
    its json files, and (through steamos-delete) Steam's shortcut. Raises if anything stays."""
    problems = []
    try:
        ids = {t['id'] for t in frame_titles.list_titles() if t['id'].startswith((PREFIX, OLD_PREFIX))}
    except frame_android.FrameError as e:
        ids = set()
        problems.append(f'could not list titles: {e}')
    ids |= smoke.installed
    for gid in sorted(ids):
        try:
            if ssh(f'test -d ~/devkit-game/{gid} && echo yes || true').strip() == 'yes':
                frame_titles.remove(gid)
            # Also when remove() stopped part-way, or only the json files are left.
            ssh(f"rm -f {' '.join(f'~/devkit-game/{gid}-{k}.json' for k in ('argv', 'env', 'settings', 'framecontrol'))}")
            if ssh(f'ls -d ~/devkit-game/{gid} ~/devkit-game/{gid}-*.json 2>/dev/null || true').strip():
                problems.append(f'{gid} is still on the Frame')
            else:
                smoke.installed.discard(gid)
        except frame_android.FrameError as e:
            problems.append(f'{gid}: {e}')
    if ids:
        # steamos-delete with no title only syncs Steam's shortcuts with ~/devkit-game.
        try:
            ssh('python3 ~/devkit-utils/steamos-delete 2>&1 || true', timeout=120)
        except frame_android.FrameError as e:
            problems.append(f'syncing Steam shortcuts: {e}')
    if problems:
        raise AssertionError('; '.join(problems))
    return {'removed': sorted(ids)}


# Runs on the Frame: drop the lines holding one key from authorized_keys, writing a
# copy with the same mode and swapping it in, so a failure can't truncate the file.
DROP_KEY = r"""
import os, sys, tempfile
path = os.path.expanduser('~/.ssh/authorized_keys')
with open(path) as f:
    lines = f.readlines()
keep = [line for line in lines if sys.argv[1] not in line]
if len(keep) < len(lines):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.authorized_keys.')
    try:
        with os.fdopen(fd, 'w') as f:
            f.writelines(keep)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
print(len(lines) - len(keep))
"""


def pair(folder):
    """Pair a throwaway RSA key through the devkit service, check it, then remove it."""
    key = os.path.join(folder, 'id_rsa_smoke')
    subprocess.run(['ssh-keygen', '-q', '-t', 'rsa', '-b', '3072', '-N', '', '-C', 'frame-control-smoke',
                    '-f', key], check=True)
    pub = Path(key + '.pub').read_text()
    host, user, port = ssh_config('hostname') or ALIAS, ssh_config('user'), ssh_config('port') or '22'
    known = ssh_config('userknownhostsfile') or '~/.ssh/known_hosts'
    comment = frame_connect.key_comment(platform.node()).replace('frame-control@', 'frame-control-smoke@')
    print('    In the headset: Steam Settings > Developer > Pair new host, then approve '
          f'"{comment}" (waits up to {frame_connect.PAIRING_MODE_WAIT} s)', flush=True)
    b64 = pub.split()[1]
    try:
        reason = frame_connect.devkit_pair(host, pub, comment)
        if reason:
            raise AssertionError(reason)
        # Only this key: no ssh_config (whose IdentityFile lines IdentitiesOnly would
        # still offer), no agent, no passwords.
        r = subprocess.run(['ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                            '-o', 'IdentityAgent=none', '-o', 'PasswordAuthentication=no',
                            '-o', 'KbdInteractiveAuthentication=no', '-o', 'ConnectTimeout=8',
                            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={known}',
                            '-p', port, '-i', key, f'{user}@{host}', 'true'], capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(f'paired, but the key does not log in: {r.stderr.strip()}')
        return {'comment': comment}
    finally:
        dropped = frame_android.ssh(f'python3 - {shlex.quote(b64)}', input=DROP_KEY).strip()
        if b64 in ssh('cat ~/.ssh/authorized_keys'):
            raise AssertionError('the smoke key is still in authorized_keys')
        print(f'    removed the smoke key from authorized_keys ({dropped} line(s))', flush=True)


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
    # Frame Control copies Valve's devkit tools to ~/devkit-utils on the first install
    # (as Valve's client does). If they weren't there before, they go again at the end.
    had_utils = ssh('test -d ~/devkit-utils && echo yes || true').strip() == 'yes'
    smoke.step('cleanup: leftovers from earlier runs', cleanup, smoke)
    folder = tempfile.mkdtemp(prefix='frame-smoke-')
    try:
        smoke.step('devkit service: properties.json', properties)
        smoke.step('status', status, build)
        for kind in ('arm64', 'x86_64', 'exe'):
            title_cycle(smoke, kind, folder)
        if args.pair:
            smoke.step('devkit pairing (throwaway key)', pair, folder)
    finally:
        smoke.step('cleanup: everything this run installed', cleanup, smoke)
        if not had_utils:
            smoke.step('cleanup: ~/devkit-utils (not there before)', ssh,
                       'rm -rf ~/devkit-utils ~/.devkit-utils.frame-control && echo removed')
        subprocess.run(['rm', '-rf', folder])
    passed = sum(s['ok'] for s in smoke.steps)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{started.replace(':', '')}-{build}.json"
    out.write_text(json.dumps({'started': started, 'alias': ALIAS, 'os_release': release, 'paired': args.pair,
                               'passed': passed, 'failed': len(smoke.steps) - passed,
                               'devkit_utils_there_before': had_utils, 'steps': smoke.steps}, indent=1))
    print(f'==> {passed}/{len(smoke.steps)} steps passed on build {build}; results in {out}')
    return 0 if passed == len(smoke.steps) else 1


if __name__ == '__main__':
    sys.exit(main())
