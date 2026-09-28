"""Frame Control's side of the APK catalogue (../apk-catalog): the rated F-Droid
list, verified downloads, installs into per-app Lepton instances, and
compatibility reports. Python stdlib only.
"""
import hashlib, json, os, shutil, sys, tempfile, threading, time, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, 'apk-catalog')
sys.path.insert(0, CATALOG)
import build as catalog_build  # noqa: E402
import reports  # noqa: E402
import frame_android  # noqa: E402
import frame_host  # noqa: E402
import frame_compat_db as compat_db  # noqa: E402

# Inside the installed app the catalogue folder is read-only, so downloads go to
# the per-user cache (FRAME_CONTROL_APP is set by app/main.js).
CACHE = (str(frame_host.cache_dir('apk')) if os.environ.get('FRAME_CONTROL_APP') or '.app/Contents/Resources' in CATALOG
         else os.path.join(CATALOG, 'data', 'cache'))
APK_HOSTS = ('https://f-droid.org/repo/', 'https://f-droid.org/archive/')
_lock = threading.Lock()
_cache = {'mtime': None, 'sig': None, 'apps': None, 'by_pkg': None}
_env = {}


_index_lock = threading.Lock()
_indexes = {}


class _IndexReader:
    """Decode one object member at a time; never retain the whole raw index."""
    def __init__(self, stream):
        self.stream, self.buffer = stream, ''
        self.decoder = json.JSONDecoder()

    def fill(self):
        chunk = self.stream.read(1 << 16)
        if not chunk:
            raise ValueError('incomplete F-Droid index')
        self.buffer += chunk

    def peek(self):
        self.buffer = self.buffer.lstrip()
        while not self.buffer:
            self.fill()
            self.buffer = self.buffer.lstrip()
        return self.buffer[0]

    def expect(self, char):
        if self.peek() != char:
            raise ValueError('invalid F-Droid index')
        self.buffer = self.buffer[1:]

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer)
                self.buffer = self.buffer[end:]
                return value
            except json.JSONDecodeError:
                self.fill()

    def members(self):
        self.expect('{')
        if self.peek() != '}':
            while True:
                key = self.value()
                if not isinstance(key, str):
                    raise ValueError('invalid F-Droid index key')
                self.expect(':')
                yield key
                if self.peek() == '}':
                    break
                self.expect(',')
        self.expect('}')


def _reduce_index(path):
    from pick import installable
    packages = {}
    found = False
    with open(path, encoding='utf-8') as f:
        reader = _IndexReader(f)
        for key in reader.members():
            if key != 'packages':
                reader.value()
                continue
            found = True
            for package in reader.members():
                records = []
                for v in reader.value().get('versions', {}).values():
                    if not installable(v):
                        continue
                    m, file = v['manifest'], v['file']
                    records.append({'version': m.get('versionName', ''),
                                    'version_code': m.get('versionCode', 0),
                                    'min_sdk': m.get('usesSdk', {}).get('minSdkVersion', 1),
                                    'abis': m.get('nativecode') or [],
                                    'name': file['name'], 'sha256': file.get('sha256')})
                if records:
                    packages[package] = records
        if reader.buffer.strip() or f.read().strip():
            raise ValueError('trailing data in F-Droid index')
    if not found:
        raise ValueError('invalid F-Droid index')
    return packages


def load_index(repo, cached_only=False):
    """Compact installable records by package, cached on disk and by mtime in memory."""
    if repo not in APK_HOSTS:
        raise ValueError('unexpected index URL')
    directory = CACHE if os.environ.get('FRAME_CONTROL_APP') or '.app/Contents/Resources' in CATALOG else os.path.join(CATALOG, 'data')
    filename = 'index-v2.json' if repo == APK_HOSTS[0] else 'index-v2.archive.json'
    raw = os.path.join(directory, filename)
    path = raw + '.installable-v1'
    with _index_lock:
        mtime = os.stat(path).st_mtime_ns if os.path.exists(path) else None
        if mtime is not None and (cached_only or time.time() - mtime / 1e9 < 86400):
            cached = _indexes.get(path)
            if cached is None or cached[0] != mtime:
                with open(path) as f:
                    cached = (mtime, json.load(f))
                _indexes[path] = cached
            return cached[1]
        if cached_only:
            return {}
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=filename, suffix='.part', dir=directory)
        os.close(fd)
        try:
            if os.path.exists(raw) and time.time() - os.path.getmtime(raw) < 86400:
                index = _reduce_index(raw)
                refreshed = os.stat(raw).st_mtime_ns
            else:
                with open(tmp, 'wb') as f, urllib.request.urlopen(repo + 'index-v2.json', timeout=30) as r:
                    shutil.copyfileobj(r, f, 1 << 20)
                index = _reduce_index(tmp)
                refreshed = time.time_ns()
            with open(tmp, 'w') as f:
                json.dump(index, f, separators=(',', ':'))
            os.utime(tmp, ns=(refreshed, refreshed))
            os.replace(tmp, path)
            _indexes[path] = (os.stat(path).st_mtime_ns, index)
            if os.path.exists(raw):
                os.remove(raw)
            return index
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def catalog():
    """Rated apps with the database's reports applied."""
    path = os.path.join(CATALOG, 'site', 'apps.js')
    reps = compat_db.load()
    sig = (len(reps), max((r.get('date') or '' for r in reps), default=''))
    with _lock:
        mtime = os.path.getmtime(path)
        if _cache['mtime'] != mtime:
            _cache.update(mtime=mtime, sig=None, apps=catalog_build.load_catalog())
        if _cache['sig'] != sig:
            by = reports.by_package(reps)
            for a in _cache['apps']:
                catalog_build.finalize(a, by.get(a['p']))
            _cache['apps'].sort(key=lambda a: (catalog_build.RANK[a['r']], a['n'].lower()))
            _cache.update(sig=sig, by_pkg={a['p']: a for a in _cache['apps']})
        return _cache['apps']


def app(pkg):
    catalog()
    a = _cache['by_pkg'].get(pkg)
    if not a:
        raise frame_android.FrameError(f'{pkg} is not in the catalogue')
    return a


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def fetch_apk(a):
    """Download (or reuse) the APK and verify it against the F-Droid index's SHA-256."""
    if not a['a'].startswith(APK_HOSTS):
        raise frame_android.FrameError('unexpected APK URL')
    if not a.get('h'):
        raise frame_android.FrameError(f"{a['n']}: the F-Droid index has no SHA-256 for this APK, so it can't be verified")
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, os.path.basename(a['a']))
    if os.path.exists(path) and _sha256(path) == a['h']:
        return path
    # Own temp file per download: concurrent downloads can't clobber or delete each other's.
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + '.', suffix='.part', dir=CACHE)
    os.close(fd)
    try:
        with urllib.request.urlopen(a['a'], timeout=60) as r, open(tmp, 'wb') as f:
            shutil.copyfileobj(r, f, 1 << 20)
        if _sha256(tmp) != a['h']:
            raise frame_android.FrameError('SHA-256 mismatch against the F-Droid index; download discarded')
        os.replace(tmp, path)  # only a verified file ever reaches the cache name
        return path
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise frame_android.FrameError(f"couldn't download {a['n']}: {e}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def fetch_icon(a):
    if not a.get('i', '').startswith('https://f-droid.org/repo/'):
        return None
    try:
        with urllib.request.urlopen(a['i'], timeout=15) as r:
            data = r.read(2_000_000)
        return data if data[:8] == b'\x89PNG\r\n\x1a\n' else None
    except Exception:
        return None


def install(pkg):
    a = app(pkg)
    if a['r'] == 'no' and not a['t']:
        raise frame_android.FrameError(f"{a['n']} can't run on the Frame: {a['why'][0]}")
    return frame_android.install(fetch_apk(a), name=a['n'], icon_png=fetch_icon(a), source='F-Droid')


def environment():
    """SteamOS and Lepton build ids, recorded with every report."""
    if not _env:
        out = frame_android.ssh('. /etc/os-release; echo "$BUILD_ID"; '
                                'sed -n \'s/.*"buildid"[[:space:]]*"\\([0-9]*\\)".*/\\1/p\' '
                                '~/.local/share/Steam/steamapps/appmanifest_3056000.acf')
        lines = out.split()
        _env.update(steamos=lines[0] if lines else None, lepton=lines[1] if len(lines) > 1 else None)
    return _env


RUNTIMES = ('instance', 'lepton-dev', 'other')


def add_report(pkg, version, result=None, rating=None, notes='', via='user', runtime='instance',
               label=None, source=None):
    """One report for any APK (F-Droid or not): did it work, and how was it run."""
    if not frame_android.PKG_RE.match(pkg or '') or len(pkg) > 200:
        raise ValueError('a report needs a valid package name, e.g. org.example.app')
    if rating not in (None, 'works', 'issues', 'broken'):
        raise ValueError('rating must be works, issues or broken')
    if result not in (None, 'runs', 'crashes', 'install_failed', 'instance_failed'):
        raise ValueError('bad result')
    if not rating and not result:
        raise ValueError('say whether it worked')
    if runtime not in RUNTIMES:
        raise ValueError(f"runtime must be one of {', '.join(RUNTIMES)}")
    try:
        env = environment()
    except frame_android.FrameError:
        env = {'steamos': None, 'lepton': None}  # Frame asleep: still record the report
    clean = lambda v, n: (str(v).strip()[:n] or None) if v not in (None, '') else None
    return compat_db.add({'package': pkg, 'version': clean(version, 80), 'result': result, 'rating': rating,
                          'notes': clean(notes, 1000), 'via': via, 'runtime': runtime,
                          'label': clean(label, 120), 'source': clean(source, 300),
                          'date': time.strftime('%Y-%m-%dT%H:%M:%S'), **env})


def recent_reports(limit=200):
    """Newest first, with a display name from the report or the catalogue."""
    catalog()
    names = {p: a['n'] for p, a in (_cache['by_pkg'] or {}).items()}
    out = []
    for r in sorted(compat_db.load(), key=lambda r: r.get('date') or '', reverse=True)[:limit]:
        out.append({**r, 'name': r.get('label') or names.get(r['package']) or r['package'],
                    'inCatalog': r['package'] in names})
    return out


def probe_and_report(pkg):
    r = frame_android.probe(pkg)
    add_report(pkg, r.get('version'), result=r['result'], notes=r.get('detail', ''), via='probe')
    return r
