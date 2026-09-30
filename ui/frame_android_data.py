"""Expansion files and stopped-instance private-data backups. Python stdlib only."""
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import tempfile
import uuid

import frame_android as android
import frame_host

REMOTE = Path(android.ROOT) / 'frame/android/app-data.py'


def _stream(command, src=None, dst=None):
    try:
        result = frame_host.run_ssh(['ssh', *android.SSH_OPTS, android.FRAME, command],
                                    stdin=src if src else subprocess.DEVNULL,
                                    stdout=dst if dst else subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=1800)
    except subprocess.TimeoutExpired:
        raise android.FrameError('app-data transfer timed out')
    except OSError as error:
        raise android.FrameError('app-data transfer failed: ' + str(error))
    if result.returncode:
        raise android.FrameError(result.stderr.decode(errors='replace').strip()[-600:] or
                                 'app-data transfer failed')
    return result.stdout


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _meta(package):
    if not android.PKG_RE.fullmatch(package or ''):
        raise android.FrameError('invalid package name')
    meta = android._meta_or_fail(package)
    if meta['package'] != package:
        raise android.FrameError('installed app metadata has a different package')
    return meta


def install_obb(package, paths):
    if not android.PKG_RE.fullmatch(package or ''):
        raise android.FrameError('invalid package name')
    paths = [Path(p).resolve() for p in paths]
    if not paths:
        raise android.FrameError('select at least one OBB file')
    names = set()
    for path in paths:
        if (not re.fullmatch(r'(main|patch)\.[0-9]+\.' + re.escape(package) + r'\.obb', path.name)
                or not path.is_file() or path.stat().st_size == 0 or path.name in names):
            raise android.FrameError('OBB must be a nonempty main/patch.<version>.' + package + '.obb file')
        names.add(path.name)
    with android._install_lock:
        meta = _meta(package)
        container = 'lepton-steamlaunch-' + str(int(meta['instance']))
        # No implicit launch. Android resolves /sdcard, never an assumed host path.
        running = android.ssh('podman ps --format "{{.Names}}"').splitlines()
        if container not in running:
            raise android.FrameError('start this app instance before installing OBB data')
        dest = '/sdcard/Android/obb/' + package
        results = []
        for path in paths:
            digest = _sha256(path)
            part = dest + '/.frame-' + uuid.uuid4().hex + '.part'
            target = dest + '/' + path.name
            script = (f'set -eu; mkdir -p {dest}; umask 002; '
                      f'trap "rm -f {part}" EXIT; cat > {part}; '
                      f'test "$(sha256sum {part} | cut -d " " -f 1)" = {digest}; '
                      f'chmod 664 {part}; mv {part} {target}')
            command = shlex.join(['podman', 'exec', '-i', container, '/system/bin/sh', '-c', script])
            with path.open('rb') as src:
                _stream(command, src=src)
            results.append({'name': path.name, 'path': target, 'sha256': digest})
        return {'package': package, 'instance': meta['instance'], 'obb': results,
                'verified': True}


def _data_command(action, meta):
    container = 'lepton-steamlaunch-' + str(int(meta['instance']))
    # Fail closed if podman cannot enumerate containers; don't mistake errors for stopped.
    guard = ('running=$(podman ps --format "{{.Names}}") || exit 1; '
             f'if printf "%s\\n" "$running" | grep -Fxq {shlex.quote(container)}; then '
             'echo "stop the app before backup or restore" >&2; exit 1; fi; ')
    return guard + shlex.join(['podman', 'unshare', 'python3', '-c', REMOTE.read_text(),
                              action, meta['package'], str(int(meta['instance']))])


def backup_data(package, destination):
    destination = Path(destination).expanduser().absolute()
    if destination.exists():
        raise android.FrameError('backup destination already exists')
    with android._install_lock:
        meta = _meta(package)
        fd, temporary = tempfile.mkstemp(prefix='.frame-backup-', dir=str(destination.parent))
        try:
            with os.fdopen(fd, 'wb') as dst:
                _stream(_data_command('backup', meta), dst=dst)
            result = _inspect(temporary, meta)
            # Exclusive publication: a concurrently created backup is never overwritten.
            os.link(temporary, str(destination))
            return dict(result, path=str(destination), sha256=_sha256(destination))
        finally:
            os.unlink(temporary)


def _inspect(path, meta):
    import tarfile
    try:
        return runpy.run_path(str(REMOTE))['inspect_archive'](path, meta['package'], int(meta['instance']))
    except (OSError, EOFError, ValueError, tarfile.TarError) as error:
        raise android.FrameError('invalid app-data backup: ' + str(error))


def restore_data(package, archive):
    with android._install_lock:
        meta = _meta(package)
        _inspect(archive, meta)
        with open(archive, 'rb') as src:
            result = _stream(_data_command('restore', meta), src=src)
        try:
            return json.loads(result)
        except (ValueError, TypeError):
            raise android.FrameError('could not read restore result; inspect app data before retrying')
