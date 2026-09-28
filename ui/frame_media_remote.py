"""Frame-side library and process ownership for Frame Control media.

Only the dedicated systemd user unit is controlled. No SteamVR settings change.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

import frame_media
from frame_media_player import probe

ROOT = Path.home() / 'Videos' / 'FrameControl'
RUNTIME = Path.home() / '.local' / 'share' / 'frame-control' / 'media'
UNIT = 'frame-control-media.service'
STATUS = RUNTIME / 'status.json'


def media_path(identity):
    if not isinstance(identity, str) or '\\' in identity or '\x00' in identity:
        raise ValueError('Invalid media id')
    parts = Path(identity).parts
    if len(parts) != 2 or not re.fullmatch('[0-9a-f]{32}', parts[0]) or parts[1].startswith('.'):
        raise ValueError('Invalid media id')
    candidate = ROOT / identity
    if candidate.is_symlink() or candidate.parent.is_symlink():
        raise ValueError('Media links are not supported')
    path = candidate.resolve(strict=True)
    if not path.is_file() or ROOT.resolve() not in path.parents:
        raise ValueError('Media file is outside the library')
    return path


def active():
    return subprocess.run(['systemctl', '--user', 'is-active', '--quiet', UNIT]).returncode == 0


def status():
    running = active()
    try:
        state = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        state = {'state': 'idle'}
    if not running and state.get('state') in ('playing', 'starting', 'paused'):
        state = {'state': 'stopped', 'message': 'Player exited; check the media log if this was unexpected'}
    return dict(state, running=running)


def run(body):
    action = body.get('action')
    if action == 'list':
        files = []
        if ROOT.exists():
            for folder in sorted(ROOT.iterdir()):
                if not re.fullmatch('[0-9a-f]{32}', folder.name) or not folder.is_dir() or folder.is_symlink():
                    continue
                for path in sorted(folder.iterdir()):
                    if path.is_file() and not path.is_symlink() and not path.name.startswith('.'):
                        files.append({'id': folder.name+'/'+path.name, 'name': path.name, 'bytes': path.stat().st_size})
        return {'files': files, 'player': status()}
    if action == 'status':
        return status()
    if action == 'stop':
        subprocess.run(['systemctl', '--user', 'stop', UNIT], check=True, timeout=15)
        return {'message': 'Media player stopped', **status()}
    if action != 'play':
        raise ValueError('Media action must be list, status, play or stop')
    path = media_path(body.get('id'))
    if type(body.get('theatre', False)) is not bool:
        raise ValueError('theatre must be true or false')
    info = {} if path.suffix.lower() == '.splat' else probe(path)[0]
    plan = frame_media.plan(path.name, body.get('layout', 'auto'), info.get('tags'))
    if active():
        raise ValueError('Stop the current media before starting another file')
    # systemd owns the process group and refuses a concurrent start of this name.
    # The runtime cap also cleans up if the controlling computer disconnects.
    subprocess.run(['systemctl', '--user', 'reset-failed', UNIT], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=10)
    STATUS.write_text(json.dumps({'state': 'starting', 'file': path.name}))
    command = ['systemd-run', '--user', '--quiet', '--collect', '--unit='+UNIT,
               '--property=RuntimeMaxSec=14400', '--property=TimeoutStopSec=8',
               '--property=StandardOutput=append:'+str(RUNTIME/'player.log'),
               '--property=StandardError=append:'+str(RUNTIME/'player.log'),
               'python3', str(RUNTIME/'frame_media_player.py'), str(path),
               '--layout', plan['layout'], '--status', str(STATUS)]
    if body.get('theatre'):
        command.append('--theatre')
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
    return {'message': 'Starting Frame Control media', 'plan': plan}


def main():
    try:
        print(json.dumps(run(json.load(sys.stdin))))
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)


if __name__ == '__main__':
    main()
