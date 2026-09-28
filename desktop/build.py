#!/usr/bin/env python3
"""Build our native PC adapter and bundle its ordinary shared libraries.

Run on the target architecture after installing GStreamer development packages.
No GStreamer executable or desktop streaming application is shipped or invoked.
Linux libc, display/GPU drivers and the portal service remain platform pieces.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def build(destination):
    win = sys.platform == 'win32'
    destination.mkdir(parents=True, exist_ok=True)
    modules = ['gstreamer-1.0', 'gstreamer-app-1.0', 'gstreamer-video-1.0']
    if not win:
        modules += ['gio-unix-2.0']
    import shlex
    flags = shlex.split(output('pkg-config', '--cflags', '--libs', *modules))
    lib = destination / ('pc-host.dll' if win else 'pc-host.so')
    cmd = [os.environ.get('CC', 'gcc' if win else 'cc'), '-std=c11', '-O2', '-shared', '-Wall', '-Wextra']
    if not win:
        cmd += ['-fPIC', '-Wl,-rpath,$ORIGIN/lib']
    subprocess.run(cmd + [str(ROOT / f) for f in ('controller.c', 'capture.c', 'portal.c')] + flags + ['-o', str(lib)], check=True)
    prefix = Path(output('pkg-config', '--variable=prefix', 'gstreamer-1.0'))
    plugin_dir = Path(output('pkg-config', '--variable=pluginsdir', 'gstreamer-1.0'))
    plugins_out, libs_out = destination / 'lib' / 'gstreamer-1.0', destination / ('bin' if win else 'lib')
    plugins_out.mkdir(parents=True, exist_ok=True)
    libs_out.mkdir(parents=True, exist_ok=True)
    names = ['coreelements', 'videotestsrc', 'videoconvertscale', 'app', 'jpeg', 'videoparsersbad', 'x264']
    names += ['d3d11', 'mediafoundation'] if win else ['pipewire', 'va']
    pending = [lib]
    for name in names:
        matches = list(plugin_dir.glob('*gst' + name + ('.dll' if win else '.so')))
        if not matches:
            raise SystemExit('Missing GStreamer library: ' + name)
        for source in matches:
            target = plugins_out / source.name
            shutil.copy2(source, target)
            pending.append(source)
    copied = set()
    while pending:
        binary = pending.pop()
        if win:
            imported = re.findall(r'DLL Name:\s*(\S+)', output('objdump', '-p', str(binary)))
            dependencies = [prefix / 'bin' / name for name in imported]
        else:
            dependencies = [Path(p) for p in re.findall(r'=>\s+(/\S+)', output('ldd', str(binary)))]
        for dep in dependencies:
            if not dep.is_file() or dep.name in copied:
                continue
            if not win and re.match(r'lib(c|m|dl|rt|pthread|resolv)\.so', dep.name):
                continue
            shutil.copy2(dep, libs_out / dep.name)
            copied.add(dep.name)
            pending.append(dep)
    # License texts and package provenance accompany the dynamically linked
    # libraries. Distribution builders keep the upstream package/source URLs.
    licenses = destination / 'licenses'
    licenses.mkdir(exist_ok=True)
    if win:
        source = prefix / 'share' / 'licenses'
        if source.exists():
            shutil.copytree(source, licenses, dirs_exist_ok=True)
        packages = output('pacman', '-Q') if shutil.which('pacman') else 'See MSYS2 build log'
    else:
        packages = output('dpkg-query', '-W', '-f=${Package} ${Version}\n') if shutil.which('dpkg-query') else ''
        # Debian copyright files contain licenses and source homepage details.
        for path in Path('/usr/share/doc').glob('*/copyright'):
            shutil.copy2(path, licenses / (path.parent.name + '.copyright'))
    (destination / 'libraries.json').write_text(json.dumps(dict(gstreamer=output('pkg-config', '--modversion', 'gstreamer-1.0'),
        libraries=sorted(copied), packages=packages), indent=2))
    print(lib)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'bundle')
    build(parser.parse_args().out.resolve())
