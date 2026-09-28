"""Linux and Windows builds on the Frame as Steam "Devkit Games".

A .zip, a folder or a single executable becomes a title in the Steam library,
through the same path as Valve's SteamOS Devkit Client: its devkit-utils
(vendored in frame/devkit-utils, synced to ~/devkit-utils on the Frame) make
~/devkit-game/<id>/, the files are copied there, and steam-client-create-shortcut
asks the running Steam client to register it with a runtime:

  Windows .exe      -> Proton Experimental (steam_play=1; x86-64 runs through FEX)
  aarch64 ELF       -> Steam Linux Runtime 4.0 ARM64 (steam_play=0)
  x86-64 ELF        -> Steam Linux Runtime 4.0 (steam_play=0; runs through FEX)

Everything device-side is inferred from Valve's steamos-devkit source until
checked on a headset; see docs/sideloading.md.

Python stdlib only. CLI:
  python3 ui/frame_titles.py inspect PATH
  python3 ui/frame_titles.py install PATH [--name N] [--exe REL] [--runtime R]
  python3 ui/frame_titles.py list | launch ID | remove ID
"""
import hashlib, json, os, posixpath, re, shlex, shutil, stat, struct, subprocess, sys, tempfile, threading, time, zipfile

import frame_android
import frame_host
from frame_android import FrameError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTILS_LOCAL = os.path.join(ROOT, 'frame', 'devkit-utils')
UTILS = 'devkit-utils'                    # on the Frame, relative to $HOME (where Valve's client puts it)
GAMES = 'devkit-game'                     # ditto; steamos-prepare-upload makes <id>/ in here
STAMP = '.frame-control-stamp'
PY = 'python3 ~/' + UTILS + '/'

# Valve's reserved sideload names: uploading one of these replaces the Steam client itself.
# devkit-steam is the trampoline file that switches SteamOS to a sideloaded client
# (select_steam.sh); a folder there breaks Valve's devkit tools.
RESERVED_IDS = ('steam', 'steamdeckard', 'steamvr', 'steamvrdeckard', 'devkit-steam')
# Any title folder on the Frame we'll list, launch or remove: safe in a shell and a path.
ID_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$')
# What a new install may use. Steam's create-shortcut refused "fc-smoke-exe" with
# "missing/invalid arguments" and took the same program as "FCSmokeProbe" (headset
# smoke test, 2026-09-27, build 20260922.6101926); Valve's client only allows
# GAMEID_ALLOWED_PATTERN, ^[A-Za-z_][A-Za-z0-9_.]+$ (devkit_client/gui2). No dots here.
NEW_ID_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{1,63}$')
DIR_RE = re.compile(r'^/[A-Za-z0-9_./-]+$')

# Zip limits: well above any real game, well below a zip bomb.
MAX_UNPACKED = 64 * 1024**3
MAX_ENTRIES = 200000
TMP_PREFIX = 'frame-title-'  # then the server's PID, so server.sweep_tmp can clear a killed run's
MAX_RATIO = 200                           # uncompressed / compressed, once past 1 GB

# The Steam compat tool aliases Valve's client uses (devkit_client RUNTIME_ALIASES).
RUNTIMES = {
    'proton-experimental': {'label': 'Proton Experimental', 'steam_play': True},
    'proton-stable': {'label': 'Proton (stable)', 'steam_play': True},
    'SteamLinuxRuntime_4-arm64': {'label': 'Steam Linux Runtime 4.0 (ARM64)', 'steam_play': False},
    'SteamLinuxRuntime_4': {'label': 'Steam Linux Runtime 4.0 (x86-64, through FEX)', 'steam_play': False},
}
# Experimental rather than stable: the Frame's ARM64 Proton + FEX stack is new,
# and Proton fixes reach Experimental first. --runtime proton-stable switches.
DEFAULT_PROTON = 'proton-experimental'

ELF_MACHINES = {0xB7: 'arm64', 0x3E: 'x86_64', 0x03: 'x86', 0x28: 'arm'}
PE_MACHINES = {0x8664: 'x86_64', 0xAA64: 'arm64', 0x14C: 'x86', 0x1C4: 'arm'}
# Executables that are never the game: crash reporters, installers, redistributables.
SKIP_RE = re.compile(r'crash|unins|setup|install|redist|dxsetup|dxwebsetup|dotnet|prereq|'
                     r'easyanticheat|eac_|updater|uploader|report|sandbox|helper', re.I)
SKIP_DIRS = re.compile(r'^(_*commonredist|redist|redistributables?|directx|vcredist|__installer|'
                       r'installers?|prereqs?|support|engine|__macosx)$', re.I)
# Trailing words of an archive name that describe the build, not the game.
BUILD_WORDS = re.compile(r'([ ._-]+(win(dows)?(32|64)?|linux(32|64)?|x64|x86(_64)?|amd64|arm64|aarch64|'
                         r'build|release|portable|steamos|v\d+([._]\d+)*|\d+([._]\d+)+))+$', re.I)


def classify(path):
    """{'format': 'elf'|'pe'|'script', 'arch', 'exe'} for an executable file, else None."""
    try:
        with open(path, 'rb') as f:
            head = f.read(4096)
            if head[:4] == b'\x7fELF':
                return _elf(f, head)
            if head[:2] == b'MZ':
                return _pe(f, head)
    except (OSError, struct.error, ValueError, OverflowError):
        return None                        # unreadable, or a header that lies about its sizes
    if head[:2] == b'#!' or path.lower().endswith('.sh'):
        return {'format': 'script', 'arch': None, 'exe': True}
    return None


def _elf(f, head):
    if len(head) < 64 or head[4] not in (1, 2) or head[5] not in (1, 2):
        return None
    wide, end = head[4] == 2, '<' if head[5] == 1 else '>'
    e_type, machine = struct.unpack_from(end + 'HH', head, 16)
    arch = ELF_MACHINES.get(machine, f'elf-0x{machine:x}')
    if e_type == 2:                        # ET_EXEC
        return {'format': 'elf', 'arch': arch, 'exe': True}
    if e_type != 3:                        # not ET_DYN either: object file, core dump
        return {'format': 'elf', 'arch': arch, 'exe': False}
    # ET_DYN is a PIE executable or a shared library; only executables ask for an interpreter.
    if wide:
        phoff, = struct.unpack_from(end + 'Q', head, 32)
        phentsize, phnum = struct.unpack_from(end + 'HH', head, 54)
    else:
        phoff, = struct.unpack_from(end + 'I', head, 28)
        phentsize, phnum = struct.unpack_from(end + 'HH', head, 42)
    if phentsize < (56 if wide else 32) or phnum > 256:
        return {'format': 'elf', 'arch': arch, 'exe': False}
    f.seek(phoff)
    table = f.read(phentsize * phnum)
    interp = any(struct.unpack_from(end + 'I', table, i * phentsize)[0] == 3   # PT_INTERP
                 for i in range(len(table) // phentsize))
    return {'format': 'elf', 'arch': arch, 'exe': interp}


def _pe(f, head):
    if len(head) < 0x40:
        return None
    lfanew, = struct.unpack_from('<I', head, 0x3C)
    f.seek(lfanew)
    coff = f.read(24)
    if len(coff) < 24 or coff[:4] != b'PE\0\0':
        return None                        # a DOS program, or not an executable at all
    machine, = struct.unpack_from('<H', coff, 4)
    characteristics, = struct.unpack_from('<H', coff, 22)
    return {'format': 'pe', 'arch': PE_MACHINES.get(machine, f'pe-0x{machine:x}'),
            'exe': not characteristics & 0x2000}   # IMAGE_FILE_DLL


def title_id(name):
    """The Devkit Game id Steam shows as the title's name, in the form Steam accepts
    (NEW_ID_RE): letters, digits and _, not starting with a digit, 2 to 64 long."""
    s = re.sub(r'[^A-Za-z0-9]+', '_', str(name or '').strip()).strip('_')[:64].strip('_')
    if not s:
        raise FrameError('the title needs a name with some letters or digits')
    if s[0].isdigit():
        s = '_' + s[:63]
    if len(s) < 2 or s.lower() in RESERVED_IDS:
        s += '_game'
    return s


def display_name(filename):
    """A title name from a zip, folder or exe name: no extension or build/platform words."""
    base = os.path.basename(str(filename).rstrip('/\\'))
    stem, ext = os.path.splitext(base)
    if ext.lower() in ('.zip', '.exe', '.sh', '.x86_64', '.arm64', '.aarch64', '.bin'):
        base = stem
    return BUILD_WORDS.sub('', base) or base


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())


# ---- payload: zip, folder or single file -> a local tree to upload ----------

def extract_zip(zpath, dest):
    """Unpack a zip into dest, refusing paths that escape it and absurd sizes."""
    try:
        z = zipfile.ZipFile(zpath)
    except (zipfile.BadZipFile, OSError) as e:
        raise FrameError(f'{os.path.basename(zpath)} is not a readable zip: {e}')
    with z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise FrameError(f'the zip has {len(infos)} entries; the limit is {MAX_ENTRIES}')
        total = sum(i.file_size for i in infos)
        packed = max(1, os.path.getsize(zpath))
        if total > MAX_UNPACKED or (total > 1024**3 and total > packed * MAX_RATIO):
            raise FrameError(f'the zip would unpack to {total / 1024**3:.1f} GB, which looks wrong')
        free = shutil.disk_usage(dest).free
        if total + 256 * 1024**2 > free:
            raise FrameError(f'not enough space on this computer to unpack the zip '
                             f'({total / 1024**3:.1f} GB needed, {free / 1024**3:.1f} GB free)')
        try:
            _extract_members(z, infos, os.path.realpath(dest))
        except FrameError:
            raise                            # a RuntimeError too, but already worded
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError) as e:
            # Encrypted or corrupt members, unsupported compression, clashing names, a full disk.
            raise FrameError(f'could not unpack {os.path.basename(zpath)}: {e}')


def _extract_members(z, infos, root):
    # No symlink is ever created here, so no write can be redirected through one
    # (chained links, Windows without the privilege). Links inside the zip are
    # resolved on paper and materialised as copies once the real files are out.
    links = {}
    for info in infos:
        rel = _safe_member(info.filename)
        if rel is None:
            continue
        target = _inside(root, rel, info.filename)
        mode = info.external_attr >> 16
        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if stat.S_ISLNK(mode):
            if info.file_size > 4096:          # a link's content is a path, never this big
                raise FrameError(f'the zip has an oversized link: {info.filename}')
            link = z.read(info).decode('utf-8', 'replace').replace('\\', '/')
            dest = posixpath.normpath(posixpath.join(posixpath.dirname(rel), link))
            if link.startswith('/') or ':' in link or dest == '..' or dest.startswith('../'):
                raise FrameError(f'the zip has a link that points outside it: {info.filename}')
            links[rel] = link                  # as written: resolved later, one component at a time
            continue
        with z.open(info) as src, open(target, 'wb') as out:
            shutil.copyfileobj(src, out, 1 << 20)
        if mode & 0o111:
            os.chmod(target, 0o755)
    _materialise_links(root, links)


def _inside(root, rel, name):
    """rel's path under root, refusing anything that resolves outside it."""
    target = os.path.join(root, *rel.split('/'))
    if not (os.path.realpath(target) + os.sep).startswith(root + os.sep):
        raise FrameError(f'the zip has a path that climbs out of it: {name}')
    return target


def _resolve_link(path, links):
    """path with every link in it followed, on paper; None if it loops or leaves the zip.

    Like the kernel: each component in turn, so 'dirlink/..' is the parent of
    where dirlink points, not the folder dirlink sits in.
    """
    todo, done, hops = path.split('/'), [], 0
    while todo:
        part = todo.pop(0)
        if part in ('', '.'):
            continue
        if part == '..':
            if not done:
                return None
            done.pop()
            continue
        done.append(part)
        text = links.get('/'.join(done))
        if text is not None:
            hops += 1
            if hops > 40:
                return None
            done.pop()                        # link text is relative to the link's folder
            todo = text.split('/') + todo
    return '/'.join(done)


def _materialise_links(root, links):
    """Copy the file each link names into its place (lib.so.1 -> lib.so.1.2.3 and the like).

    Only links to files: a folder link could hold itself, and game builds link
    libraries, not folders. Folder, looping and dangling links are dropped.
    """
    copies = []
    for rel in sorted(links):
        dest = _resolve_link(rel, links)
        if not dest:
            continue                        # loops, escapes, or the zip's own top folder
        src, target = _inside(root, dest, rel), _inside(root, rel, rel)
        if os.path.isfile(src) and not os.path.lexists(target):   # a real entry may have the name
            copies.append((src, target))
    # Many links to one big file could fill the disk: the same limits as the zip itself.
    need = sum(os.path.getsize(src) for src, _ in copies)
    if need + _tree_size(root) > MAX_UNPACKED:
        raise FrameError('the zip\'s links would copy more than it holds, which looks wrong')
    if need + 256 * 1024**2 > shutil.disk_usage(root).free:
        raise FrameError(f'not enough space on this computer for the zip\'s linked files ({need / 1024**3:.1f} GB)')
    for src, target in copies:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(src, target)


def _safe_member(name):
    """The member's relative path with / separators, None to skip it; raises if it escapes."""
    rel = name.replace('\\', '/')
    if rel.startswith('/') or re.match(r'^[A-Za-z]:', rel):
        raise FrameError(f'the zip has an absolute path: {name}')
    parts = [p for p in rel.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        raise FrameError(f'the zip has a path that climbs out of it: {name}')
    if any(':' in p for p in parts):
        # Drive-qualified parts ('C:..') climb out on Windows; ':' is an NTFS stream elsewhere.
        raise FrameError(f'the zip has a path with a drive or stream name: {name}')
    if not parts or parts[0] == '__MACOSX' or parts[-1] in ('.DS_Store', 'Thumbs.db'):
        return None
    return '/'.join(parts)


def _redirected(path, expected):
    """True if path is a symlink, or resolves somewhere else (a Windows junction isn't islink)."""
    return os.path.islink(path) or os.path.normcase(os.path.realpath(path)) != os.path.normcase(expected)


def _has_links(root):
    real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(real):
        for n in dirnames + filenames:
            if _redirected(os.path.join(dirpath, n), os.path.join(dirpath, n)):
                return True
    return False


def _stage_folder(src, dest):
    """Copy src to dest; links to files inside src become copies, all other links are left out."""
    real = os.path.realpath(src)
    inside = lambda p: os.path.normcase(p).startswith(os.path.normcase(real) + os.sep)  # noqa: E731
    folders, copies = [], []                 # decide everything first, so the space check sees the same files
    for dirpath, dirnames, filenames in os.walk(real):
        out = os.path.join(dest, os.path.relpath(dirpath, real))
        folders.append(out)
        # Only descend into real folders: not symlinked ones, not junctions (os.walk follows those).
        linked = [d for d in dirnames if _redirected(os.path.join(dirpath, d), os.path.join(dirpath, d))]
        dirnames[:] = [d for d in dirnames if d not in linked]
        for fn in filenames + linked:
            target = os.path.realpath(os.path.join(dirpath, fn))
            if inside(target) and os.path.isfile(target):
                copies.append((target, os.path.join(out, fn)))
    if shutil.disk_usage(os.path.dirname(dest)).free < sum(os.path.getsize(t) for t, _ in copies) + 256 * 1024**2:
        raise FrameError('not enough space on this computer to stage the folder')
    for out in folders:
        os.makedirs(out, exist_ok=True)
    for target, out in copies:
        shutil.copy2(target, out)
    return dest


def _below(top, root):
    """The folders _unwrap stepped through from top to root, as 'a/b', or ''. Taken
    before staging moves root elsewhere (possibly another drive on Windows)."""
    rel = os.path.relpath(root, os.path.realpath(top)).replace(os.sep, '/')
    return '' if rel == '.' else rel


def _unwrap(root):
    """Step into a single top-level folder, the usual shape of a zipped build.

    Never through a link or junction: the folder you chose (or unpacked) stays the boundary.
    """
    root = os.path.realpath(root)
    for _ in range(4):
        entries = [e for e in os.listdir(root) if e not in ('__MACOSX', '.DS_Store', 'Thumbs.db')]
        if len(entries) != 1:
            break
        only = os.path.join(root, entries[0])
        if not os.path.isdir(only) or _redirected(only, only):
            break
        root = only
    return root


def candidates(root, title=''):
    """Executables under root, best launch target first."""
    found = []
    want = _norm(title)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
        rel_dir = os.path.relpath(dirpath, root)
        parts = [] if rel_dir == '.' else rel_dir.split(os.sep)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            c = classify(full)
            if not c or not c['exe']:
                continue
            stem = _norm(os.path.splitext(fn)[0])
            skip = bool(SKIP_RE.search(fn)) or any(SKIP_DIRS.match(p) for p in parts)
            match = 2 if want and stem == want else 1 if want and stem and (want in stem or stem in want) else 0
            found.append({'path': '/'.join(parts + [fn]), 'format': c['format'], 'arch': c['arch'],
                          'size': os.path.getsize(full), 'depth': len(parts), 'skip': skip, 'match': match})
    found.sort(key=_rank)
    _prefer_launcher_script(found)
    return found


def _platform_rank(c):
    # Native ARM64 first, then Proton, then x86-64 Linux through FEX; scripts are placed separately.
    order = {('elf', 'arm64'): 0, ('pe', 'x86_64'): 1, ('elf', 'x86_64'): 2, ('pe', 'x86'): 3, ('pe', 'arm64'): 3}
    return order.get((c['format'], c['arch']), 5 if c['format'] == 'script' else 6)


def _rank(c):
    return (c['skip'], _platform_rank(c), -c['match'], c['depth'], -c['size'], c['path'])


def _prefer_launcher_script(found):
    """A top-level shell script beats a Linux binary one folder down (run.sh + bin/game)."""
    if not found or found[0]['format'] != 'elf' or found[0]['depth'] == 0:
        return
    for i, c in enumerate(found):
        if c['format'] == 'script' and c['depth'] == 0 and not c['skip']:
            found.insert(0, found.pop(i))
            return


def runtime_for(target, found=()):
    """(compat tool alias, note) for a launch target, or raise FrameError if it can't run."""
    fmt, arch = target['format'], target['arch']
    if fmt == 'script':
        # A script runs in the runtime of the binaries next to it; alone, natively.
        elf = next((c for c in found if c['format'] == 'elf' and not c['skip']), None)
        if elf:
            return runtime_for(elf)[0], 'A shell script; runtime chosen from the Linux binary next to it.'
        return 'SteamLinuxRuntime_4-arm64', 'A shell script with no Linux binary beside it; run natively.'
    if fmt == 'pe':
        if arch not in ('x86_64', 'x86', 'arm64'):
            raise FrameError(f"{target['path']} is a Windows program for {arch}, which Proton can't run")
        note = 'Windows x86-64 build: Proton runs it through FEX.' if arch == 'x86_64' else \
            f'Windows {arch} build under Proton.'
        return DEFAULT_PROTON, note
    if fmt == 'elf' and arch == 'arm64':
        return 'SteamLinuxRuntime_4-arm64', 'Native ARM64 Linux build.'
    if fmt == 'elf' and arch == 'x86_64':
        return 'SteamLinuxRuntime_4', ("x86-64 Linux build: probably won't start. It needs the x86-64 Steam "
                                       "Linux Runtime 4.0, which the Frame didn't install for a sideloaded title "
                                       "(2026-09-26). Use an ARM64 or Windows build if there is one.")
    raise FrameError(f"{target['path']} is a {arch} Linux program; the Frame runs ARM64 and x86-64 (through FEX) only")


def allowed_runtimes(target, found=()):
    alias, _ = runtime_for(target, found)
    return ['proton-experimental', 'proton-stable'] if RUNTIMES[alias]['steam_play'] else [alias]


def inspect(path, name=None):
    """Read a .zip, folder or executable into an install plan (a JSON-safe dict).

    A zip is unpacked into a temporary folder, plan['work']; pass the plan to
    discard() when done with it. Raises FrameError if nothing in it can run.
    """
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FrameError(f'{path} does not exist')
    work = None
    try:
        if os.path.isdir(path):
            root = _unwrap(path)
            unwrapped = _below(path, root)
            if _has_links(root):
                # scp -r follows links, so a link out of the folder could upload
                # anything; copy the folder with its links made safe first.
                work = tempfile.mkdtemp(prefix=f'{TMP_PREFIX}{os.getpid()}-')
                root = _stage_folder(root, os.path.join(work, os.path.basename(root)))
        elif path.lower().endswith('.zip'):
            work = tempfile.mkdtemp(prefix=f'{TMP_PREFIX}{os.getpid()}-')
            extract_zip(path, work)
            root = _unwrap(work)
            unwrapped = _below(work, root)
        elif classify(path):
            # A single executable is uploaded on its own; don't copy a whole Downloads folder.
            work = tempfile.mkdtemp(prefix=f'{TMP_PREFIX}{os.getpid()}-')
            shutil.copy2(path, os.path.join(work, os.path.basename(path)))
            root, unwrapped = work, ''
        else:
            raise FrameError(f'{os.path.basename(path)} is not a .zip, a folder or a program')
        title = name or display_name(os.path.basename(path.rstrip('/\\')))
        found = candidates(root, title)
        if not found:
            raise FrameError(f'no Linux or Windows program found in {os.path.basename(path)}')
        plan = {'source': os.path.basename(path.rstrip('/\\')), 'name': title, 'id': title_id(title),
                'root': root, 'work': work, 'candidates': found,
                'unwrapped': unwrapped,
                'size': _tree_size(root), 'warnings': []}
        _choose(plan, found[0]['path'])
        return plan
    except BaseException:
        if work:
            shutil.rmtree(work, ignore_errors=True)
        raise


def _tree_size(root):
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if os.path.isfile(full) and not os.path.islink(full):
                total += os.path.getsize(full)
    return total


def _choose(plan, rel, runtime=None):
    """Set plan's launch target (a path relative to root) and its runtime."""
    rel = rel.replace('\\', '/')
    # A manifest may name the program as it is in the archive, above the folder
    # _unwrap stepped into. A path that works as it is always wins.
    prefix = plan.get('unwrapped') and plan['unwrapped'] + '/'
    if (prefix and rel.startswith(prefix) and not any(c['path'] == rel for c in plan['candidates'])
            and not os.path.isfile(os.path.join(plan['root'], *rel.split('/')))):
        rel = rel[len(prefix):]
    target = next((c for c in plan['candidates'] if c['path'] == rel), None)
    if target is None:
        full = os.path.realpath(os.path.join(plan['root'], *rel.replace('\\', '/').split('/')))
        root = os.path.realpath(plan['root'])
        if not (full + os.sep).startswith(root + os.sep) or not os.path.isfile(full):
            raise FrameError(f'{rel} is not a file in the title')
        c = classify(full)
        if not c or not c['exe']:
            raise FrameError(f"{rel} isn't a program the Frame can start")
        target = {'path': os.path.relpath(full, root).replace(os.sep, '/'), 'format': c['format'],
                  'arch': c['arch'], 'size': os.path.getsize(full), 'depth': rel.count('/'),
                  'skip': False, 'match': 0}
    alias, note = runtime_for(target, plan['candidates'])
    allowed = allowed_runtimes(target, plan['candidates'])
    if runtime:
        if runtime not in allowed:
            raise FrameError(f"{target['path']} can't use {runtime}; choose one of {', '.join(allowed)}")
        alias = runtime
    plan.update(target=target['path'], format=target['format'], arch=target['arch'], runtime=alias,
                runtime_label=RUNTIMES[alias]['label'], runtimes=allowed, note=note)
    plan['warnings'] = (['This looks like an installer or helper, not the game itself.'] if target['skip'] else [])
    return plan


def discard(plan):
    if plan and plan.get('work'):
        shutil.rmtree(plan['work'], ignore_errors=True)


def argv_for(rel):
    # Valve's client sends the start command as one string (it may carry arguments),
    # so a path with spaces is quoted. How Steam splits it is inferred.
    return ['"' + rel + '"' if re.search(r'\s', rel) else rel]


def shortcut_parms(gameid, directory, rel, runtime):
    """The JSON steam-client-create-shortcut takes, as devkit_client.new_or_ensure_game builds it."""
    settings = {'steam_play': '1' if RUNTIMES[runtime]['steam_play'] else '0'}
    if RUNTIMES[runtime]['steam_play']:
        # gui2._update_game sends these with every Proton title; debugging stays off.
        settings.update(steam_play_debug='0', steam_play_debug_version='2019')
    settings['compat_tool'] = runtime
    return {'gameid': gameid, 'directory': directory, 'argv': argv_for(rel), 'env': {},
            'settings': settings, 'clear_settings': True, 'force_appid': '', 'lepton_args': ''}


# ---- the Frame side ----------------------------------------------------------

def ssh(cmd, input=None, timeout=120):
    return frame_android.ssh(cmd, input=input, timeout=timeout)


def _json_out(out, what):
    """The JSON object a devkit-utils script prints last (its logging goes to stderr)."""
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith('{') or line.startswith('['):
            try:
                return json.loads(line)
            except ValueError:
                break
    raise FrameError(f'{what} gave no usable answer: {out.strip()[-300:]!r}')


def utils_stamp():
    """Hash of the vendored devkit-utils, compared with the copy on the Frame."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(UTILS_LOCAL):
        dirnames[:] = sorted(d for d in dirnames if d != '__pycache__')
        for fn in sorted(filenames):
            if fn.endswith('.pyc'):
                continue
            full = os.path.join(dirpath, fn)
            h.update(os.path.relpath(full, UTILS_LOCAL).replace(os.sep, '/').encode() + b'\0')
            with open(full, 'rb') as f:
                h.update(f.read())
    return h.hexdigest()[:20]


def _json_files(gid):
    # Exact names: a glob like Game-*.json would also match another title called Game-Deluxe.
    return ' '.join(f'{GAMES}/{gid}-{k}.json' for k in ('argv', 'env', 'settings', 'framecontrol'))


_utils_lock = threading.Lock()


def ensure_utils():
    """Copy frame/devkit-utils to ~/devkit-utils on the Frame unless it's already this version."""
    with _utils_lock:                        # one sync at a time: they share a staging folder
        return _ensure_utils()


def _ensure_utils():
    stamp = utils_stamp()
    have = ssh(f'cat {UTILS}/{STAMP} 2>/dev/null || true', timeout=30).strip()
    if have == stamp:
        return False
    # Merge rather than replace: Valve's own client may have put newer files there.
    tmp = f'.{UTILS}.frame-control'
    ssh(f'rm -rf {tmp}', timeout=30)
    _copy_tree(UTILS_LOCAL, tmp, timeout=300)
    ssh(f'mkdir -p {UTILS} && cp -R {tmp}/. {UTILS}/ && rm -rf {tmp} {UTILS}/__pycache__ '
        f'&& echo {stamp} > {UTILS}/{STAMP}', timeout=60)
    return True


def _copy_tree(src, dest, timeout=3 * 3600, delete=False):
    """Copy a local folder's contents to dest on the Frame (dest ends up a copy of src).

    rsync where installed (not on Windows; see server.push_file), else scp -r
    into a fresh dest, which is what Windows has. dest must be a plain path.
    """
    name = os.path.basename(src.rstrip('/\\'))
    opts = frame_android.SSH_OPTS           # read now: the server swaps in its multiplexed options
    if _rsync():
        cmd = ['rsync', '-a', *(['--delete'] if delete else []), '-e', shlex.join(['ssh', *opts]),
               src.rstrip('/') + '/', f'{frame_android.FRAME}:{dest.rstrip("/")}/']
    else:
        # scp -r copies src *into* dest when dest exists, so dest must not.
        ssh(f'rm -rf {shlex.quote(dest)}', timeout=60)
        cmd = ['scp', *opts, '-r', src, f'{frame_android.FRAME}:{dest}']
    try:
        subprocess.run(cmd, check=True, capture_output=True, stdin=subprocess.DEVNULL, text=True,
                       errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FrameError(f'copying {name} to the Frame timed out')
    except subprocess.CalledProcessError as e:
        raise FrameError(f'copying {name} to the Frame failed: {(e.stderr or "").strip()[-300:]}')


def _rsync():
    # Not on Windows: a Windows rsync (cwRsync, MSYS2) wouldn't take the POSIX -e quoting.
    return not frame_host.WINDOWS and bool(shutil.which('rsync'))


_install_lock = threading.Lock()


def install(path, name=None, exe=None, runtime=None, progress=None):
    """Sideload a .zip, folder or executable as a Devkit Game; returns the title dict.

    name: the Steam name (sanitised to the title id), default from the file name.
    exe: launch target relative to the title's root, default the best candidate.
    runtime: a compat tool alias from RUNTIMES that suits the target (e.g.
    'proton-stable' instead of the default Proton Experimental).
    progress: optional callable(stage_text, fraction 0..1).
    """
    plan = inspect(path, name)
    try:
        return install_plan(plan, name=name, exe=exe, runtime=runtime, progress=progress)
    finally:
        discard(plan)


def install_plan(plan, name=None, exe=None, runtime=None, progress=None):
    """Install an inspect() plan, optionally with another name, target or runtime."""
    if name:
        plan['name'], plan['id'] = name, title_id(name)
    if exe or runtime:
        _choose(plan, exe or plan['target'], runtime)
    step = progress or (lambda *a: None)
    with _install_lock:
        return _install(plan, step)


def _install(plan, step):
    gid = plan['id']
    if not NEW_ID_RE.match(gid) or gid.lower() in RESERVED_IDS:
        raise FrameError(f'bad title id {gid!r}')
    step("Syncing Valve's devkit tools to the Frame", 0.02)
    ensure_utils()
    existed = ssh(f'test -d {GAMES}/{gid} && echo yes || true', timeout=30).strip() == 'yes'
    step('Preparing the title folder', 0.05)
    ready = _json_out(ssh(f'{PY}steamos-prepare-upload --gameid {gid}', timeout=60), 'steamos-prepare-upload')
    directory = str(ready.get('directory') or '')
    if not DIR_RE.match(directory) or not directory.endswith(f'/{GAMES}/{gid}'):
        raise FrameError(f'steamos-prepare-upload returned an unexpected folder {directory!r}')
    registered = False
    try:
        step(f"Copying {plan['size'] / 1e6:.0f} MB to the Frame", 0.1)
        if _rsync():
            _copy_tree(plan['root'], directory, delete=True)
        else:
            part = f"{directory.rsplit('/', 1)[0]}/.{gid}.upload"
            _copy_tree(plan['root'], part)
            ssh(f'rm -rf {directory} && mv {part} {directory}', timeout=120)
        # Same modes Valve's client gives an upload (rsync --chmod=Du=rwx,Dgo=rx,Fu=rwx,Fog=rx).
        ssh(f'chmod -R 755 {directory}', timeout=300)
        step('Registering with Steam', 0.9)
        parms = shortcut_parms(gid, directory, plan['target'], plan['runtime'])
        reply = _json_out(ssh(f'{PY}steam-client-create-shortcut --parms {shlex.quote(json.dumps(parms))}',
                              timeout=90), 'steam-client-create-shortcut')
        meta = {'id': gid, 'name': plan['name'], 'target': plan['target'], 'runtime': plan['runtime'],
                'source': plan['source'], 'size': plan['size'], 'installed': time.strftime('%Y-%m-%dT%H:%M:%S')}
        ssh(f'cat > {GAMES}/{gid}-framecontrol.json', input=json.dumps(meta, indent=1), timeout=30)
        registered = True                    # the files stay: Steam registers them once it's running
        if 'error' in reply:
            err = str(reply['error']).strip().rstrip('.')
            hint = ' With Steam running on the Frame, install it again.' if 'not running' in err else ''
            raise FrameError(f"Uploaded, but Steam didn't register it: {err}.{hint}")
        step('Done', 1.0)
        meta.update(runtime_label=RUNTIMES[plan['runtime']]['label'], steam=str(reply.get('success', '')).strip())
        return meta
    finally:
        if not registered and not existed:
            # A first install that failed part-way: don't leave an orphan folder behind.
            try:
                ssh(f'rm -rf {GAMES}/{gid} {GAMES}/.{gid}.upload {_json_files(gid)}', timeout=60)
            except FrameError:
                pass


LIST_SCRIPT = r'''
import json, os
root = os.path.expanduser('~/devkit-game')
reserved = %r
out = []
for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
    if d.startswith('.') or d.lower() in reserved or not os.path.isdir(os.path.join(root, d)):
        continue
    t = {'id': d}
    for key, suffix in (('settings', '-settings.json'), ('argv', '-argv.json'), ('meta', '-framecontrol.json')):
        try:
            with open(os.path.join(root, d + suffix)) as f:
                t[key] = json.load(f)
        except (OSError, ValueError):
            t[key] = None
    out.append(t)
print(json.dumps(out))
''' % (RESERVED_IDS,)


def list_titles():
    """The Devkit Games on the Frame (any uploaded by Valve's client too)."""
    raw = _json_out(ssh('python3 -', input=LIST_SCRIPT, timeout=30), 'the title list')
    titles = []
    for t in raw if isinstance(raw, list) else []:
        if not ID_RE.match(str(t.get('id', ''))):
            continue
        settings, meta = t.get('settings') or {}, t.get('meta') or {}
        argv = t.get('argv') if isinstance(t.get('argv'), list) else []
        alias = str(settings.get('compat_tool') or '')
        titles.append({'id': t['id'], 'name': str(meta.get('name') or t['id']),
                       'target': str(meta.get('target') or (argv[0] if argv else '')),
                       'runtime': alias, 'runtime_label': RUNTIMES.get(alias, {}).get('label', alias or 'not set'),
                       'source': str(meta.get('source') or ''), 'size': meta.get('size'),
                       'installed': meta.get('installed'), 'registered': t.get('settings') is not None,
                       'frame_control': bool(meta)})
    return titles


def _check_id(gid):
    gid = str(gid or '')
    if not ID_RE.match(gid) or gid.lower() in RESERVED_IDS:
        raise FrameError(f'bad title id {gid!r}')
    if ssh(f'test -d {GAMES}/{gid} && echo yes || true', timeout=30).strip() != 'yes':
        raise FrameError(f'{gid} is not installed')
    return gid


def launch(gid):
    gid = _check_id(gid)
    ensure_utils()
    # steam-devkit-rpc logs 'success' when Steam answers, but exits 0 after a
    # 5 s timeout too, so read its log (stderr) rather than trust the exit code.
    out = ssh(f'{PY}steam-devkit-rpc run-game gameid={gid} 2>&1', timeout=60)
    if 'success' not in [line.strip() for line in out.splitlines()]:
        raise FrameError(f"Steam didn't confirm the launch: {out.strip()[-300:] or 'no answer'}")
    return {'id': gid}


def remove(gid):
    # Not while an install runs: it could be this title, half copied or about to register.
    if not _install_lock.acquire(blocking=False):
        raise FrameError('an install is running; remove the title when it has finished')
    try:
        gid = _check_id(gid)
        ensure_utils()
        # steamos-delete removes the folder and syncs Steam's shortcuts; its json files stay, so clear them too.
        ssh(f'{PY}steamos-delete --delete-title {gid}', timeout=120)
        ssh(f'rm -f {_json_files(gid)}', timeout=30)
        return {'id': gid}
    finally:
        _install_lock.release()


def public(plan):
    """A plan without its local paths, for the UI, with the runtimes each candidate may use."""
    out = {k: v for k, v in plan.items() if k not in ('root', 'work', 'candidates')}
    out['candidates'] = []
    for c in plan['candidates']:
        c = dict(c)
        try:
            c['runtimes'] = allowed_runtimes(c, plan['candidates'])
        except FrameError as e:
            c['runtimes'], c['blocked'] = [], str(e)
        out['candidates'].append(c)
    out['runtime_labels'] = {k: v['label'] for k, v in RUNTIMES.items()}
    return out


def main():
    cmd, *args = sys.argv[1:] or ['help']

    def opt(flag):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None

    try:
        if cmd == 'inspect' and args:
            plan = inspect(args[0], opt('--name'))
            try:
                if opt('--exe') or opt('--runtime'):
                    _choose(plan, opt('--exe') or plan['target'], opt('--runtime'))
                r = public(plan)
            finally:
                discard(plan)
        elif cmd == 'install' and args:
            r = install(args[0], name=opt('--name'), exe=opt('--exe'), runtime=opt('--runtime'),
                        progress=lambda text, _: print(text + '…', file=sys.stderr))
        elif cmd == 'list':
            r = list_titles()
        elif cmd in ('launch', 'remove') and args:
            r = globals()[cmd](args[0])
        else:
            sys.exit(__doc__)
    except FrameError as e:
        sys.exit(f'error: {e}')
    print(json.dumps(r, indent=1))


if __name__ == '__main__':
    main()
