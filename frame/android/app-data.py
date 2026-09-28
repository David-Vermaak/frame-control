"""Private-data archives, run under podman unshare on the Frame. Stdlib only."""
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import tempfile
import time

MAX_BYTES = 20 * 1024 ** 3
MAX_FILES = 100000
MAX_MANIFEST = 1024 * 1024


def inspect_archive(path, package, instance):
    names, total, manifest = set(), 0, None
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = member.name
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith('/') or '..' in parts or
                    name != '/'.join(parts) or name in names or '\\' in name):
                raise ValueError('unsafe or duplicate archive path')
            if name == 'data' and not member.isdir():
                raise ValueError('data root must be a directory')
            names.add(name)
            if len(names) > MAX_FILES or not (member.isdir() or member.isfile()):
                raise ValueError('archive has too many files, links or special files')
            if member.uid < 0 or member.gid < 0 or member.uid > 65535 or member.gid > 65535:
                raise ValueError('archive owner outside Android user namespace')
            total += member.size
            if total > MAX_BYTES:
                raise ValueError('archive exceeds 20 GiB')
            if name == 'manifest.json' and member.isfile() and member.size <= MAX_MANIFEST:
                manifest = json.load(archive.extractfile(member))
            elif parts[0] != 'data':
                raise ValueError('unexpected archive member')
        if (not isinstance(manifest, dict) or manifest.get('format') != 1 or
                manifest.get('package') != package or manifest.get('instance') != instance or
                'data' not in names):
            raise ValueError('backup does not match this package and instance')
    return {'files': len(names) - 1, 'bytes': total, 'package': package, 'instance': instance,
            'skipped_links': manifest.get('skipped_link_count', 0)}


def backup(root, package, instance, output):
    import io
    source = root / package
    if source.is_symlink() or not source.is_dir():
        raise ValueError('private app data does not exist or is a symlink')
    count, total, links, skipped = 0, 0, [], 0

    def checked(member):
        nonlocal count, total, skipped
        if member.issym():  # never followed or restored; listed in the manifest instead
            skipped += 1
            if len(links) < 1000:
                links.append({'path': member.name[:512], 'target': member.linkname[:256]})
            return None
        if member.islnk():  # a second name for a file already archived: store its content again
            member.type, member.linkname = tarfile.REGTYPE, ''
            member.size = os.lstat(str(source / member.name[len('data/'):])).st_size
        count += 1
        total += member.size
        if not (member.isdir() or member.isfile()) or count > MAX_FILES or total > MAX_BYTES:
            raise ValueError('private data contains special files or exceeds backup limits')
        return member

    with tarfile.open(fileobj=output, mode='w|gz', dereference=False) as archive:
        archive.add(str(source), arcname='data', filter=checked)
        # Written last so that it can list what was skipped.
        manifest = json.dumps({'format': 1, 'package': package, 'instance': instance,
                               'skipped_links': links, 'skipped_link_count': skipped}).encode()
        member = tarfile.TarInfo('manifest.json')
        member.size, member.mode = len(manifest), 0o600
        archive.addfile(member, io.BytesIO(manifest))


def restore(root, package, instance, input_stream):
    source = root / package
    if source.is_symlink() or not source.is_dir():
        raise ValueError('private app data does not exist or is a symlink')
    with tempfile.TemporaryDirectory(prefix='.frame-restore-', dir=str(root)) as work:
        work = Path(work)
        archive_path = work / 'backup.tar.gz'
        with archive_path.open('wb') as output:
            size = 0
            while True:
                chunk = input_stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('compressed backup exceeds 20 GiB')
                output.write(chunk)
        result = inspect_archive(archive_path, package, instance)
        stage = work / 'stage'
        stage.mkdir(mode=0o700)
        with tarfile.open(archive_path, 'r:gz') as archive:
            directories = []
            for member in archive:
                if member.name == 'manifest.json':
                    continue
                target = stage / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    directories.append((target, member))
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, target.open('xb') as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    apply_metadata(target, member)
            for target, member in reversed(directories):
                apply_metadata(target, member)
        previous = root / ('.' + package + '.before-restore-' + str(time.time_ns()))
        source.rename(previous)
        try:
            (stage / 'data').rename(source)
        except BaseException:
            previous.rename(source)
            raise
        # Keep only the newest pre-restore copy of this package's data.
        for old in root.glob('.' + package + '.before-restore-*'):
            if old != previous and not old.is_symlink():
                shutil.rmtree(str(old), ignore_errors=True)
        result['previous'] = str(previous)
        return result


def apply_metadata(path, member):
    os.chown(str(path), member.uid, member.gid)
    os.chmod(str(path), member.mode & 0o777)
    os.utime(str(path), (member.mtime, member.mtime))


def main():
    action, package, instance = sys.argv[1:]
    instance = int(instance)
    root = Path.home() / '.local/share/Steam/steamapps/compatdata' / str(instance) / 'internal'
    if root.is_symlink() or root.resolve() != root.absolute():
        raise ValueError('private-data directory traverses a symlink')
    if action == 'backup':
        backup(root, package, instance, sys.stdout.buffer)
    elif action == 'restore':
        print(json.dumps(restore(root, package, instance, sys.stdin.buffer)))
    else:
        raise ValueError('unknown app-data action')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, tarfile.TarError) as error:
        sys.exit(str(error))
