"""Android apps on the Frame, each in its own persistent Lepton instance.

Every APK gets ~/Applications/Android/<package>/ on the Frame with app.apk,
launch.sh (frame/android/lepton-app.sh), instance.id, meta.json and, for 2D
apps, the lepton-show-flatscreen marker; plus a non-Steam shortcut, so it shows
in the Steam library and gets its own SteamVR panel. Nothing goes through
Lepton Development, which wipes its apps on exit. See docs/apks.md.

Python stdlib only. CLI: python3 ui/frame_android.py
  install APK [--vr|--flat] [--no-xr-compat] | info APK | versions APK-or-PKG
  install-obb PKG OBB [OBB ...] | backup-data PKG ARCHIVE | restore-data PKG ARCHIVE
  patch SRC DST [--add NAME=PATH ...] | list | launch PKG | stop PKG | remove PKG | probe PKG
"""
import json, os, re, shlex, shutil, struct, subprocess, sys, threading, time, zlib

import frame_apk
import frame_host
import tempfile
import zipfile
from frame_apk_vr import add_launcher_category
from frame_apk_sign import repack

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAME = os.environ.get('FRAME_ALIAS', 'frame')
APPS_DIR = 'Applications/Android'          # relative to the Frame's $HOME
COMPAT = '.local/share/Steam/steamapps/compatdata'
SHADERS = '.local/share/Steam/steamapps/shadercache'
LAUNCHER = os.path.join(ROOT, 'frame', 'android', 'lepton-app.sh')
SHORTCUTS = os.path.join(ROOT, 'frame', 'android', 'steam_shortcuts.py')
# OpenXR API layer that lets OpenXR 1.1 apps run on SteamVR's 1.0-only Android
# runtime (frame/openxr-compat, docs/vr-apks.md). Injected into VR APKs.
XR_COMPAT = os.path.join(ROOT, 'frame', 'openxr-compat')
XR_COMPAT_FILES = {
    'assets/openxr/1/api_layers/implicit.d/XrApiLayer_FRAME_compat.json': 'XrApiLayer_FRAME_compat.json',
    'lib/arm64-v8a/libXrApiLayer_FRAME_compat.so': 'prebuilt/arm64-v8a/libXrApiLayer_FRAME_compat.so',
}
PKG_RE = re.compile(r'^[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+$')
SSH_OPTS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8']


class FrameError(RuntimeError):
    pass


def ssh(cmd, input=None, timeout=120):
    try:
        # No inherited stdin (see server.ssh): Windows' ssh.exe would wait on it.
        feed = {'input': input} if input is not None else {'stdin': subprocess.DEVNULL}
        p = subprocess.run(['ssh', *SSH_OPTS, FRAME, cmd], capture_output=True, **feed,
                           timeout=timeout, text=isinstance(input, str) or input is None)
    except subprocess.TimeoutExpired:
        raise FrameError(f'timed out talking to {FRAME}')
    if p.returncode != 0:
        raise FrameError((p.stderr or p.stdout or f'ssh exited {p.returncode}').strip()[-600:])
    return p.stdout


def shortcut_tool(*args, timeout=60):
    with open(SHORTCUTS) as f:
        return ssh('python3 - ' + ' '.join(shlex.quote(a) for a in args), input=f.read(),
                   timeout=timeout).strip()


def instance_id(pkg):
    # Stable per package, well above real Steam app ids, below 2^32. T3 Code's
    # hand-picked 2873873873 sits outside this range.
    return 2800000000 + zlib.crc32(pkg.encode()) % 70000000


def game_id(shortcut_appid):
    return (int(shortcut_appid) << 32) | 0x02000000


def apk_info(path):
    """Package, label, version, native ABIs and the best PNG icon inside the APK."""
    try:
        return frame_apk.apk_info(path)
    except frame_apk.ApkError as e:
        raise FrameError(f'{os.path.basename(path)}: {e}')


def xr_compat_files(apk_path):
    """The layer's files to add, or {} if the APK has no OpenXR loader or already has the layer."""
    with zipfile.ZipFile(apk_path) as z:
        names = set(z.namelist())
    if 'lib/arm64-v8a/libopenxr_loader.so' not in names or names & set(XR_COMPAT_FILES):
        return {}
    add = {}
    for entry, rel in XR_COMPAT_FILES.items():
        try:
            with open(os.path.join(XR_COMPAT, rel), 'rb') as f:
                add[entry] = f.read()
        except OSError:
            raise FrameError("the OpenXR compatibility layer isn't built; run frame/openxr-compat/build.sh")
    return add


def check_installable(info):
    if info['min_sdk'] and info['min_sdk'] > 30:
        raise FrameError(f"{info['label']} needs Android API {info['min_sdk']}; Lepton is Android 11 (API 30)")
    if info['abis'] and 'arm64-v8a' not in info['abis']:
        raise FrameError(f"{info['label']} has no arm64-v8a build ({', '.join(info['abis'])}); Lepton is 64-bit ARM only")


_install_lock = threading.Lock()  # installs are rare; one at a time avoids every race


def _copy(src, dest, executable=False, timeout=600):
    """Copy a local file to the Frame: rsync where installed (not on Windows), else scp."""
    name = os.path.basename(src)
    rsync = None if frame_host.WINDOWS else shutil.which('rsync')  # see server.push_file
    if rsync:
        cmd = ['rsync', '-a', *(['--chmod=u+x'] if executable else []),
               '-e', shlex.join(['ssh', *SSH_OPTS]), src, f'{FRAME}:{dest}']
    else:
        cmd = ['scp', *SSH_OPTS, src, f'{FRAME}:{dest}']
    try:
        subprocess.run(cmd, check=True, capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FrameError(f'copying {name} to the Frame timed out')
    except subprocess.CalledProcessError as e:
        raise FrameError(f'copying {name} to the Frame failed: {(e.stderr or "").strip()[-300:]}')
    if executable and not rsync:
        ssh(f'chmod u+x {shlex.quote(dest)}')


def _shortcut_ids():
    try:
        return {int(x['appid']) for x in json.loads(shortcut_tool('list'))}
    except (ValueError, TypeError, KeyError) as e:
        raise FrameError(f'could not read the Steam shortcut list: {e}')


def _write_meta(d, meta):
    # Write then rename, so a dropped connection can't leave torn JSON behind.
    ssh(f'cat > {d}/meta.json.tmp && mv {d}/meta.json.tmp {d}/meta.json', input=json.dumps(meta, indent=1))


def install(apk_path, flatscreen=None, name=None, source=None, icon_png=None, xr_compat=None):
    info = apk_info(apk_path)
    if icon_png:
        info['icon_png'] = icon_png
    check_installable(info)
    pkg = info['package']
    if not PKG_RE.match(pkg):
        raise FrameError(f'unexpected package name {pkg!r}')
    if flatscreen is None:
        flatscreen = not info['vr']
    # VR apps get the OpenXR compatibility layer unless told otherwise; it only
    # changes calls SteamVR would otherwise reject.
    add = xr_compat_files(apk_path) if (info['vr'] if xr_compat is None else xr_compat) else {}
    with _install_lock:
        if add or info['repairable']:
            with tempfile.TemporaryDirectory(prefix='frame-vr-') as tmp:
                patched = os.path.join(tmp, 'app.apk')
                info['patched'] = patch(apk_path, patched, add)['patched']
                info['launchable'] = True
                return _install(patched, info, pkg, flatscreen, name, source or os.path.basename(apk_path))
        return _install(apk_path, info, pkg, flatscreen, name, source)


def _install(apk_path, info, pkg, flatscreen, name, source):
    iid = instance_id(pkg)
    d = f'{APPS_DIR}/{pkg}'
    existing = read_meta(pkg)
    ok = False
    try:
        ssh(f'mkdir -p {d}')
        _copy(apk_path, f'{d}/app.apk.part')
        _copy(LAUNCHER, f'{d}/launch.sh', executable=True, timeout=120)
        icon = ''
        if info['icon_png']:
            ssh(f'cat > {d}/icon.png', input=info['icon_png'])
            icon = f'$HOME/{d}/icon.png'
        marker = f'touch {d}/lepton-show-flatscreen' if flatscreen else f'rm -f {d}/lepton-show-flatscreen'
        ssh(f'mv {d}/app.apk.part {d}/app.apk && echo {iid} > {d}/instance.id && {marker}')
        home = ssh('echo $HOME').strip()
        shortcut = _int((existing or {}).get('shortcut'))
        if not shortcut or shortcut not in _shortcut_ids():
            reply = shortcut_tool('add', name or info['label'], f'{home}/{d}/launch.sh', f'{home}/{d}',
                                  icon.replace('$HOME', home))
            shortcut = _int(reply.strip().splitlines()[-1] if reply.strip() else None)
            if not shortcut:
                raise FrameError(f'Steam did not return a shortcut id (got {reply[:80]!r})')
        meta = {'package': pkg, 'label': name or info['label'], 'version': info['version'],
                'instance': iid, 'shortcut': shortcut, 'game_id': game_id(shortcut),
                'vr': info.get('vr', False), 'vr_issues': info.get('vr_issues', []),
                'launchable': info.get('launchable', False), 'patched': info.get('patched', []),
                'flatscreen': flatscreen, 'installed': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'source': source or os.path.basename(apk_path)}
        _write_meta(d, meta)
        ok = True
        return meta
    finally:
        if not ok and not existing:
            # A first install that failed part-way: don't leave an orphan folder behind.
            try:
                ssh(f'rm -rf {d}', timeout=30)
            except FrameError:
                pass


def _int(v):
    try:
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def read_meta(pkg):
    try:
        m = json.loads(ssh(f'cat {APPS_DIR}/{pkg}/meta.json 2>/dev/null || true') or 'null')
    except (ValueError, FrameError):
        return None
    return _clean_meta(m)


def _clean_meta(m):
    """A usable meta dict with integer ids, or None if it's missing what we need."""
    if not isinstance(m, dict) or not PKG_RE.match(str(m.get('package', ''))):
        return None
    iid, shortcut = _int(m.get('instance')), _int(m.get('shortcut'))
    if not iid:
        return None
    m.update(instance=iid, shortcut=shortcut, game_id=game_id(shortcut) if shortcut else None,
             label=str(m.get('label') or m['package']), version=str(m.get('version') or ''))
    return m


def running_instances():
    """Lepton container name -> adb port, for the instances that are running now."""
    out = ssh('podman ps --format "{{.Names}} {{.Labels.adb_port}}" 2>/dev/null || true')
    return dict(line.split()[:2] for line in out.splitlines() if len(line.split()) >= 2)


def list_apps():
    out = ssh(f'for f in {APPS_DIR}/*/meta.json; do [ -f "$f" ] && cat "$f" && echo; echo "@@"; done 2>/dev/null || true')
    running = running_instances()
    apps = []
    for chunk in out.split('@@'):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            m = _clean_meta(json.loads(chunk))
        except ValueError:
            continue
        if m:
            m['running'] = f"lepton-steamlaunch-{m['instance']}" in running
            apps.append(m)
    return sorted(apps, key=lambda m: m['label'].lower())


def _meta_or_fail(pkg):
    if not PKG_RE.match(pkg or ''):
        raise FrameError(f'bad package name {pkg!r}')
    m = read_meta(pkg)
    if not m:
        raise FrameError(f'{pkg} is not installed')
    return m


def launch(pkg):
    m = _meta_or_fail(pkg)
    if not m['game_id']:
        raise FrameError(f"{m['label']} has no Steam shortcut; reinstall it")
    ssh(f"steam steam://rungameid/{int(m['game_id'])} >/dev/null 2>&1 &")
    return m


def stop(pkg):
    m = _meta_or_fail(pkg)
    ssh(f"podman stop -t 5 lepton-steamlaunch-{int(m['instance'])} >/dev/null 2>&1 || true", timeout=60)
    return m


def remove(pkg, keep_data=False):
    m = _meta_or_fail(pkg)
    stop(pkg)
    if m['shortcut']:
        try:
            shortcut_tool('remove', str(int(m['shortcut'])))
        except FrameError:
            pass  # already gone from Steam
    iid = int(m['instance'])
    extra = '' if keep_data else f' {COMPAT}/{iid} {SHADERS}/{iid}'
    ssh(f'rm -rf {APPS_DIR}/{pkg}{extra}')
    return m


def probe(pkg, wait=20):
    """Launch the app's instance and report whether it stays up (for compat reports)."""
    m = _meta_or_fail(pkg)
    ctr = f"lepton-steamlaunch-{int(m['instance'])}"
    launch(pkg)
    t0 = time.time()
    while time.time() - t0 < 90 and ctr not in running_instances():
        time.sleep(3)
    if ctr not in running_instances():
        return {'package': pkg, 'version': m['version'], 'result': 'instance_failed',
                'detail': 'Lepton instance did not start within 90 s'}
    # Android boots inside the container; then give the app time to crash, or not.
    time.sleep(wait)
    sh = f'podman exec {ctr} /system/bin/sh -c'
    alive = ssh(f"{sh} 'pidof {pkg}' 2>/dev/null || true").strip()
    crash = ssh(f"{sh} 'logcat -d -b crash' 2>/dev/null | tail -n 60 || true")
    reason = next((l.split('AndroidRuntime: ', 1)[1] for l in crash.splitlines()
                   if 'AndroidRuntime: ' in l and ('Exception' in l or 'Error' in l)), '')
    if not reason and 'Fatal signal' in crash:
        reason = next(l[l.find('Fatal signal'):] for l in crash.splitlines() if 'Fatal signal' in l)
    if 'ClipboardManager' in reason:
        reason = 'no clipboard service: ' + reason
    return {'package': pkg, 'version': m['version'], 'result': 'runs' if alive else 'crashes',
            'detail': reason[:300], 'seconds': wait,
            'container_up': ctr in running_instances()}


def patch(src, dst, add=None):
    try:
        info = apk_info(src)
        with zipfile.ZipFile(src) as z:
            original = frame_apk._read(z, 'AndroidManifest.xml', frame_apk.MAX_MANIFEST)
        manifest = add_launcher_category(original) if info['repairable'] else original
        if not info['launchable'] and not info['repairable']:
            raise FrameError('APK has no MAIN/LAUNCHER activity that Frame Control can patch')
        repack(src, dst, replace={'AndroidManifest.xml': manifest}, add=add)
        result = apk_info(dst)
        result.pop('icon_png', None)
        result['patched'] = (['launcher'] if manifest != original else []) + \
            (['openxr-compat'] if add and set(XR_COMPAT_FILES) <= set(add) else [])
        return result
    except (OSError, ValueError, IndexError, struct.error, zipfile.BadZipFile, frame_apk.ApkError) as e:
        raise FrameError(str(e)) from e


def install_obb(pkg, paths):
    import frame_android_data
    return frame_android_data.install_obb(pkg, paths)


def backup_data(pkg, destination):
    import frame_android_data
    return frame_android_data.backup_data(pkg, destination)


def restore_data(pkg, archive):
    import frame_android_data
    return frame_android_data.restore_data(pkg, archive)


def main():
    cmd, *args = sys.argv[1:] or ['help']
    try:
        if cmd in ('info', 'versions'):
            import frame_apk_versions
            if cmd == 'info':
                info = apk_info(args[0])
                print(frame_apk_versions.describe(info))
                fix = '; Frame Control adds the LAUNCHER entry Lepton needs' if info.get('repairable') else ''
                if info.get('vr') or fix:
                    print(('VR app' if info.get('vr') else 'Android app') + fix)
                for note in info.get('vr_issues', []):
                    print(note)
                return
            info = apk_info(args[0]) if os.path.isfile(args[0]) or args[0].lower().endswith('.apk') else None
            r = frame_apk_versions.alternatives(
                info['package'] if info else args[0], info.get('version_code') if info else None)
        elif cmd == 'install':
            r = install(args[0], flatscreen=False if '--vr' in args else True if '--flat' in args else None,
                        xr_compat=False if '--no-xr-compat' in args else None)
        elif cmd == 'patch':
            import argparse
            parser = argparse.ArgumentParser(description='Patch and v2-sign an APK locally')
            parser.add_argument('src')
            parser.add_argument('dst')
            parser.add_argument('--add', action='append', default=[], metavar='NAME=PATH')
            opts = parser.parse_args(args)
            additions = {}
            for item in opts.add:
                if '=' not in item:
                    raise FrameError('--add requires NAME=PATH')
                entry, path = item.split('=', 1)
                with open(path, 'rb') as f:
                    additions[entry] = f.read()
            r = patch(opts.src, opts.dst, additions)
        elif cmd == 'install-obb':
            if len(args) < 2:
                raise FrameError('install-obb requires PACKAGE OBB [OBB ...]')
            r = install_obb(args[0], args[1:])
        elif cmd in ('backup-data', 'restore-data'):
            if len(args) != 2:
                raise FrameError(cmd + ' requires PACKAGE ARCHIVE.tar.gz')
            r = (backup_data if cmd == 'backup-data' else restore_data)(*args)
        elif cmd == 'list':
            r = list_apps()
        elif cmd in ('launch', 'stop', 'probe'):
            r = globals()[cmd](args[0])
        elif cmd == 'remove':
            r = remove(args[0], keep_data='--keep-data' in args)
        else:
            sys.exit(__doc__)
    except (FrameError, OSError) as e:
        sys.exit(f'error: {e}')
    print(json.dumps(r, indent=1))


if __name__ == '__main__':
    main()
