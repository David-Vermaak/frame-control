#!/usr/bin/env python3
"""Open Frame Control's assistant as a Chromium panel. Ctrl-C closes it and its SSH tunnel.

Start ui/server.py first. Requires the platform Chromium Flatpak and zsh on the
computer (the existing panel launcher). No model endpoint or key is configured.
"""
import argparse
import os
from pathlib import Path
import re
import shlex
import signal
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=47810, help='local Frame Control port')
    parser.add_argument('--frame-port', type=int, default=47812, help='Frame loopback tunnel port')
    args = parser.parse_args()
    alias = os.environ.get('FRAME_ALIAS', 'frame')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', alias) or any(not 1 <= p <= 65535 for p in (args.port, args.frame_port)):
        parser.error('Invalid alias or port')
    if not shutil.which('zsh'):
        parser.error('The panel launcher requires zsh on this computer')
    sys.path.insert(0, str(ROOT / 'ui'))
    from frame_mcp import Client
    Client('http://127.0.0.1:' + str(args.port), os.environ.get('FRAME_UI_KEY', '1')).request('/api/host')
    profile = '/tmp/frame-control-assistant-' + uuid.uuid4().hex
    log_path = ''
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    tunnel = subprocess.Popen(['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                               '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
                               '-o', 'ServerAliveCountMax=2', '-R',
                               f'127.0.0.1:{args.frame_port}:127.0.0.1:{args.port}', alias])
    try:
        # Check the forwarded page before starting a browser; no arbitrary sleeps.
        probe = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', alias,
                                'curl --retry 5 --retry-connrefused --retry-delay 1 --max-time 10 -fsS ' +
                                shlex.quote(f'http://127.0.0.1:{args.frame_port}/assistant')],
                               stdout=subprocess.DEVNULL, timeout=30)
        if probe.returncode or tunnel.poll() is not None:
            raise RuntimeError('Could not forward Frame Control to the Frame')
        launched = subprocess.run(['zsh', str(ROOT / 'scripts/panel-on-frame.sh'), '--name', 'Frame Control Assistant',
                        'org.chromium.Chromium', '--user-data-dir=' + profile, '--no-first-run',
                        '--disable-background-networking', '--disable-sync',
                        f'--app=http://127.0.0.1:{args.frame_port}/assistant'], check=True, timeout=45, stdout=subprocess.PIPE, text=True)
        print(launched.stdout, end='', flush=True)
        match = re.search(r'log (/tmp/panel-on-frame\.[A-Za-z0-9]+)', launched.stdout)
        if match:
            log_path = match.group(1)
        print('Assistant panel open. Ctrl-C closes this panel and its tunnel.', flush=True)
        tunnel.wait()
        raise RuntimeError('SSH tunnel ended')
    except KeyboardInterrupt:
        return 0
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tunnel.kill()
            tunnel.wait()
        # Only this unique browser profile, never a shared Chromium instance.
        cleanup = '''import os, pathlib, signal, shutil, sys, time
profile = sys.argv[1]
needle = ('--user-data-dir=' + profile).encode()
owned = []
for p in pathlib.Path('/proc').iterdir():
    try:
        if p.name.isdigit() and p.stat().st_uid == os.getuid() and needle in (p / 'cmdline').read_bytes().split(b'\\0'):
            owned.append(int(p.name))
    except OSError:
        pass
for sig in (signal.SIGTERM, signal.SIGKILL):
    for pid in owned:
        try: os.kill(pid, sig)
        except ProcessLookupError: pass
    time.sleep(.3)
shutil.rmtree(profile, ignore_errors=True)
if sys.argv[2]:
    pathlib.Path(sys.argv[2]).unlink(missing_ok=True)
'''
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', alias,
                                 'python3 - ' + shlex.quote(profile) + ' ' + shlex.quote(log_path)], input=cleanup, text=True, timeout=20)
        if result.returncode:
            print('Cleanup failed; close the assistant panel and remove ' + profile + ' on the Frame.', file=sys.stderr)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
