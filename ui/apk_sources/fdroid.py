"""Signed F-Droid repositories. CLI: add|remove|list|search|download."""
import argparse
import base64
import contextlib
import errno
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apk_sources import SourceError, SourceLimited, _web
import frame_host
from frame_apk_sign import _der_parts, _cert_key, der
from frame_catalog import _IndexReader, _reduce_index, _sha256

KIND = 'fdroid'
CACHE_VERSION = 2
_LOCK = threading.RLock()  # settings only; never held while downloading
_load_locks = {}
_refreshing = {}  # source id -> background refresh thread
_retry_at = {}  # source id -> time before which a failed refresh isn't retried
_stale = set()  # source ids currently served from an expired index
MAX_AGE = 86400
# Published by the repository operators; a user repository without a pin uses TOFU.
FDROID_PIN = '43238d512c1e5eb2d6569f4a3afbf5523418b82e0a3ed1552770abb9a9c9ccab'
IZZY_PIN = '3bf0d6abfeae2f401707b6d966be743bf0eee49c2561b9ba39073711f628937a'
_DIGESTS = {
    '608648016503040201': ('sha256', '3031300d060960864801650304020105000420'),
    '608648016503040202': ('sha384', '3041300d060960864801650304020205000430'),
    '608648016503040203': ('sha512', '3051300d060960864801650304020305000440'),
    '2b0e03021a': ('sha1', '3021300906052b0e03021a05000414'),
}


def _fingerprint(value):
    value = re.sub(r'[:\s]', '', value or '').lower()
    if not re.fullmatch('[0-9a-f]{64}', value):
        raise SourceError('fingerprint must be a SHA-256 certificate fingerprint (64 hex digits)')
    return value


def _url(url, fingerprint=None):
    if url.startswith('fdroidrepos://'):
        url = 'https://' + url[len('fdroidrepos://'):]
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.fragment:
        raise SourceError('repository URL must use HTTPS without credentials or a fragment')
    params = urllib.parse.parse_qs(p.query)
    pins = params.pop('fingerprint', [])
    if params or len(pins) > 1:
        raise SourceError('only one fingerprint query parameter is supported')
    pin = _fingerprint(fingerprint) if fingerprint else None
    if pins:
        linked = _fingerprint(pins[0])
        if pin and pin != linked:
            raise SourceError('conflicting fingerprints')
        pin = linked
    return urllib.parse.urlunsplit(('https', p.netloc.lower(), p.path.rstrip('/') + '/', '', '')), pin


def _child(base, name):
    name = str(name).lstrip('/')
    decoded = urllib.parse.unquote(name)
    if not name or '\\' in decoded or any(x in ('.', '..') for x in decoded.split('/')):
        raise SourceError('unsafe repository file name')
    if '?' in decoded or urllib.parse.urlsplit(decoded).scheme:
        raise SourceError('repository file is outside its repository')
    url = urllib.parse.urljoin(base, urllib.parse.quote(decoded, safe='/'))  # names may contain '#' or spaces
    if not url.startswith(base) or urllib.parse.urlsplit(url).query or urllib.parse.urlsplit(url).fragment:
        raise SourceError('repository file is outside its repository')
    return url


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise SourceError('refusing non-HTTPS redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url, path, maximum):
    host = urllib.parse.urlsplit(url).hostname
    wait = _web.wait_time(url)
    if wait:
        raise _web.limited_error(host, wait)
    request = urllib.request.Request(url, headers={'User-Agent': 'FrameControl/1.0'})
    try:
        with urllib.request.build_opener(_HTTPSRedirect()).open(request, timeout=60) as r, open(path, 'wb') as f:
            total = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise SourceError('repository file exceeds size limit')
                f.write(chunk)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise _web.limited_error(host, _web.throttle(url, e.headers)) from e
        raise


def _limited(source, error):
    return _web.limited_error(source['name'], error.retry_after or _web.BACKOFF)


def _children(item):
    return _der_parts(item[1])


def _cms(data, content, strong=False):
    outer = _der_parts(data)
    if len(outer) != 1:
        raise ValueError('invalid CMS wrapper')
    wrapper = _children(outer[0])
    if wrapper[0][1].hex() != '2a864886f70d010702':
        raise ValueError('not CMS SignedData')
    fields = _children(_children(wrapper[1])[0])
    certs = next(_children(f) for f in fields[3:] if f[0] == 0xa0)
    signers = _children(fields[-1])
    if len(signers) != 1:
        raise ValueError('exactly one repository signer required')
    signer = _children(signers[0])
    sid = _children(signer[1])
    matching = []
    for cert in certs:
        tbs = _children(_children(cert)[0])
        offset = 1 if tbs[0][0] == 0xa0 else 0
        if tbs[offset][1] == sid[1][1] and tbs[offset + 2][2] == sid[0][2]:
            matching.append(cert[2])
    if len(matching) != 1:
        raise ValueError('missing or ambiguous signer certificate')
    cert = matching[0]
    digest, prefix = _DIGESTS[_children(signer[2])[0][1].hex()]
    if strong and digest == 'sha1':
        raise ValueError('SHA-1 signatures are not accepted for v2 indexes')
    at, signed = 3, content
    if signer[at][0] == 0xa0:
        attrs = {}
        for attr in _children(signer[at]):
            pair = _children(attr)
            oid = pair[0][1].hex()
            if oid in attrs:
                raise ValueError('duplicate CMS attribute')
            attrs[oid] = _children(pair[1])
        if attrs['2a864886f70d010904'][0][1] != hashlib.new(digest, content).digest():
            raise ValueError('CMS content digest mismatch')
        if attrs['2a864886f70d010903'][0][1].hex() != '2a864886f70d010701':
            raise ValueError('unexpected CMS content type')
        signed = der(0x31, signer[at][1])
        at += 1
    algorithm = _children(signer[at])[0][1].hex()
    allowed = {'sha1': '2a864886f70d010105', 'sha256': '2a864886f70d01010b',
               'sha384': '2a864886f70d01010c', 'sha512': '2a864886f70d01010d'}
    if algorithm not in ('2a864886f70d010101', allowed[digest]):
        raise ValueError('unsupported repository signature algorithm (RSA PKCS#1 required)')
    n, e, _ = _cert_key(cert)
    sig = signer[at + 1][1]
    size = (n.bit_length() + 7) // 8
    if not 256 <= size <= 1024 or n % 2 != 1 or not 3 <= e <= 0xffffffff or e % 2 != 1 or len(sig) != size or int.from_bytes(sig, 'big') >= n:
        raise ValueError('invalid RSA signature/key size')
    value = bytes.fromhex(prefix) + hashlib.new(digest, signed).digest()
    expected = b'\0\1' + b'\xff' * (size - len(value) - 3) + b'\0' + value
    if pow(int.from_bytes(sig, 'big'), e, n).to_bytes(size, 'big') != expected:
        raise ValueError('repository RSA signature mismatch')
    return hashlib.sha256(cert).hexdigest()


def _sections(data):
    sections = []
    for block in re.split(b'\r?\n\r?\n', data):
        if not block:
            continue
        attrs = {}
        for line in re.sub(b'\r?\n ', b'', block).splitlines():
            key, value = line.decode('utf-8').split(': ', 1)
            key = key.lower()
            if key in attrs:
                raise ValueError('duplicate manifest attribute')
            attrs[key] = value
        sections.append(attrs)
    return sections


def _digest_check(attrs, suffix, content, strong=False):
    for label, digest in (('sha-512', 'sha512'), ('sha-384', 'sha384'), ('sha-256', 'sha256'), ('sha1', 'sha1'), ('sha-1', 'sha1')):
        if label + suffix in attrs and not (strong and digest == 'sha1'):
            if base64.b64decode(attrs[label + suffix], validate=True) != hashlib.new(digest, content).digest():
                raise ValueError('JAR digest mismatch')
            return
    raise ValueError('missing supported JAR digest')


def _jar(path, member, pin, strong=False):
    """strong: SHA-2 only (v2 entry.jar); index-v1.jar may still be SHA-1 signed."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if len(names) > 64 or len(names) != len(set(names)) or any(
                    i.file_size > (1024 * 1024 if i.filename.upper().startswith('META-INF/') else 256 * 1024 * 1024)
                    for i in z.infolist()):
                raise ValueError('duplicate or oversized JAR member')
            blocks = [n for n in names if n.upper().startswith('META-INF/') and n.upper().endswith('.RSA')]
            if len(blocks) != 1:
                raise ValueError('exactly one RSA JAR signer required')
            sf = z.read(blocks[0][:-4] + '.SF')
            fingerprint = _cms(z.read(blocks[0]), sf, strong)
            if pin and fingerprint != pin:
                raise ValueError('repository fingerprint mismatch')
            manifest = z.read('META-INF/MANIFEST.MF')
            _digest_check(_sections(sf)[0], '-digest-manifest', manifest, strong)
            entries = [s for s in _sections(manifest)[1:] if s.get('name') == member]
            if len(entries) != 1:
                raise ValueError('index is not uniquely signed')
            content = z.read(member)
            _digest_check(entries[0], '-digest', content, strong)
            return content, fingerprint
    except (ValueError, KeyError, IndexError, StopIteration, RuntimeError, NotImplementedError, zipfile.BadZipFile) as e:
        raise SourceError('invalid signed repository: ' + str(e)) from e


def _storage():
    return frame_host.data_dir('apk-repos.json')


def _read():
    try:
        settings = json.loads(_storage().read_text())
        if not isinstance(settings, dict) or not isinstance(settings.get('repos'), list) or not isinstance(settings.get('enabled'), dict):
            raise ValueError('invalid settings structure')
        return settings
    except FileNotFoundError:
        return {'repos': [], 'enabled': {}}
    except (OSError, ValueError) as e:
        raise SourceError('cannot read repository settings: ' + str(e)) from e


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.part')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, separators=(',', ':'))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _state_path():
    return frame_host.data_dir('apk-repo-state.json')


def _states():
    try:
        states = json.loads(_state_path().read_text())
        if not isinstance(states, dict):
            raise ValueError('invalid state structure')
        return states
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SourceError('cannot read repository state: ' + str(e)) from e


def _state(source):
    """Newest accepted index timestamp and whether a v2 index was ever accepted (rollback protection)."""
    with _LOCK:
        state = _states().get(source['id'])
    return state if isinstance(state, dict) and state.get('url') == source['url'] else {}


@contextlib.contextmanager
def _state_file_lock():
    """Inter-process lock: the CLI and the app must not publish indexes out of order."""
    path = frame_host.data_dir('apk-repo-state.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), 'a+b') as f:
        if os.name == 'nt':
            import msvcrt
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError as e:  # LK_LOCK gives up after ~10 s of contention; keep waiting
                    if e.errno not in (errno.EACCES, errno.EDEADLK):
                        raise
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _check_timestamp(source, timestamp):
    last = _state(source).get('timestamp')
    if last is not None and (type(timestamp) is not int or timestamp < last):
        raise SourceError('repository index is older than the one already accepted (possible rollback); refused')


def _accept(source, timestamp, v2):
    with _LOCK:
        states = _states()
        state = _state(source)
        states[source['id']] = {'url': source['url'], 'v2': bool(v2 or state.get('v2')),
                                'timestamp': timestamp if type(timestamp) is int else state.get('timestamp')}
        _write(_state_path(), states)


def user_repos():
    with _LOCK:
        return _read()['repos']


def sources():
    builtins = [('fdroid', 'F-Droid', 'https://f-droid.org/repo/', FDROID_PIN),
                ('fdroid-archive', 'F-Droid archive', 'https://f-droid.org/archive/', FDROID_PIN),
                ('izzyondroid', 'IzzyOnDroid', 'https://apt.izzysoft.de/fdroid/repo/', IZZY_PIN)]
    settings = _read()
    return [dict(id=i, kind=KIND, name=n, url=u, fingerprint=p, builtin=True,
                 # The archive only holds superseded versions; it clutters search unless asked for.
                 enabled=settings['enabled'].get(i, i != 'fdroid-archive'), trust='community')
            for i, n, u, p in builtins] + settings['repos']


def _text(value):
    if isinstance(value, dict):
        return value.get('en-US') or next((v for v in value.values() if v), '')
    return value or ''


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif tag in ('br', 'p', 'div', 'li'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ('p', 'div', 'li'):
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _summary(value):
    parser = _PlainText()
    parser.feed(_text(value))
    parser.close()
    return ' '.join(''.join(parser.parts).split())


def _images(meta, base):
    def url(file):
        name = file.get('name') if isinstance(file, dict) else file
        try:
            return _child(base, name) if isinstance(name, str) and name else None
        except SourceError:
            return None  # one odd image name mustn't hide the app

    icon = url(_text(meta.get('icon')))
    banner = url(_text(meta.get('featureGraphic')))
    screenshots = []
    groups = meta.get('screenshots') or {}
    for device, legacy in (('phone', 'phoneScreenshots'), ('sevenInch', 'sevenInchScreenshots')):
        files = _text(groups.get(device)) or _text(meta.get(legacy)) or []
        for file in files if isinstance(files, list) else []:
            image = url(file)
            if image and image not in screenshots:
                screenshots.append(image)
            if len(screenshots) == 6:
                break
        if len(screenshots) == 6:
            break
    return {'icon': icon, 'banner': banner, 'screenshots': screenshots}


def _reduce(path, source):
    compatible = _reduce_index(path)
    result = {}
    with open(path, encoding='utf-8') as f:
        reader = _IndexReader(f)
        for key in reader.members():
            if key != 'packages':
                reader.value()
                continue
            for pkg in reader.members():
                item = reader.value()
                if pkg not in compatible:
                    continue
                meta = item.get('metadata', {})
                files = {v['file'].get('name'): v for v in item.get('versions', {}).values()
                         if isinstance(v, dict) and isinstance(v.get('file'), dict)}
                versions = []
                for v in compatible[pkg]:
                    original = files[v['name']]
                    versions.append(dict(v, size=original['file'].get('size'), updated=_date(original.get('added'))))
                versions.sort(key=lambda v: (v['version_code'], v['abis'] == ['arm64-v8a']), reverse=True)
                latest = versions[0]
                images = _images(meta, source['url'])
                result[pkg] = dict(source=source['id'], id=pkg, package=pkg,
                    name=_text(meta.get('name')) or pkg, summary=_summary(meta.get('summary')),
                    icon=images['icon'], images=images, developer=_text(meta.get('authorName')) or None,
                    page=meta.get('webSite') or source['url'], vr=None, free=True,
                    license=meta.get('license'), downloadable=bool(latest.get('sha256')),
                    versions=versions, **{k: latest[k] for k in ('version', 'version_code', 'min_sdk', 'abis', 'size', 'updated')})
    return result


def _date(value):
    return time.strftime('%Y-%m-%d', time.gmtime(value / 1000)) if isinstance(value, (int, float)) else None


def _v1(content, path):
    index = json.loads(content)
    apps = {a['packageName']: a for a in index['apps']}
    packages = {}
    for pkg, builds in index['packages'].items():
        app = apps.get(pkg, {})
        localized = app.get('localized', {})
        meta = {k: _text({locale: fields[k] for locale, fields in localized.items() if fields.get(k)}) or app.get(k)
                for k in ('name', 'summary', 'license', 'webSite', 'authorName')}
        for field in ('icon', 'featureGraphic', 'phoneScreenshots', 'sevenInchScreenshots'):
            images = {}
            for locale, fields in localized.items():
                value = fields.get(field)
                if not value:
                    continue
                prefix = pkg + '/' + locale + '/'
                if field.endswith('Screenshots'):
                    images[locale] = [{'name': prefix + field + '/' + name} for name in value[:6]]
                else:
                    images[locale] = {'name': prefix + value}
            meta[field] = images
        if not meta['icon'] and app.get('icon'):
            meta['icon'] = {'en-US': {'name': 'icons/' + app['icon']}}
        versions = {}
        for i, v in enumerate(builds):
            versions[str(i)] = {'manifest': {'versionName': v.get('versionName'), 'versionCode': v['versionCode'],
                'usesSdk': {'minSdkVersion': v.get('minSdkVersion', 1)}, 'nativecode': v.get('nativecode', [])},
                'file': {'name': v['apkName'], 'sha256': v.get('hash') if v.get('hashType') == 'sha256' else None,
                         'size': v.get('size')}, 'added': v.get('added')}
        packages[pkg] = {'metadata': meta, 'versions': versions}
    path.write_text(json.dumps({'packages': packages}))
    return (index.get('repo') or {}).get('timestamp')


def _cached(source, cache):
    """(apps, pin, expired) from a cache matching this source, or None."""
    try:
        saved = json.loads(cache.read_text())
        if (saved.get('version') == CACHE_VERSION and saved.get('fingerprint') == source.get('fingerprint')
                and saved.get('url') == source['url']):
            return saved['apps'], saved['fingerprint'], time.time() - cache.stat().st_mtime >= MAX_AGE
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    return None


def stale(source):
    """True while results come from an expired index that is being (or failed to be) refreshed."""
    return source['id'] in _stale


def _refresh_later(source):
    with _LOCK:
        if source['id'] in _refreshing or time.time() < _retry_at.get(source['id'], 0):
            return
        def run():
            try:
                _load(source, force=True)
            except Exception:
                with _LOCK:
                    _retry_at[source['id']] = time.time() + 600
            finally:
                with _LOCK:
                    _refreshing.pop(source['id'], None)
        _refreshing[source['id']] = thread = threading.Thread(target=run, daemon=True)
    thread.start()


def _source_lock(source_id):
    with _LOCK:
        return _load_locks.setdefault(source_id, threading.Lock())


def _load(source, force=False):
    if not re.fullmatch(r'[a-z0-9-]+', source['id']):
        raise SourceError('invalid source id')
    _url(source['url'], source.get('fingerprint'))
    cache = frame_host.cache_dir('apk-sources', source['id'] + '.json')
    found = None if force else _cached(source, cache)
    if found:
        if found[2]:  # expired: serve it now, marked stale, and refresh without blocking anyone
            _stale.add(source['id'])
            _refresh_later(source)
        return found[:2]
    with _source_lock(source['id']):
        found = None if force else _cached(source, cache)  # another caller may have just loaded it
        if found and not found[2]:
            return found[:2]
        cache.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(dir=str(cache.parent)) as tmp:
                jar, raw = Path(tmp) / 'index.jar', Path(tmp) / 'index.json'
                v2 = True
                try:
                    _fetch(source['url'] + 'entry.jar', jar, 8 * 1024 * 1024)
                except urllib.error.HTTPError as e:
                    if e.code not in (404, 410):
                        raise
                    if _state(source).get('v2'):
                        raise SourceError('repository no longer serves its signed v2 index; '
                                          'refusing to fall back to the older v1 index') from e
                    v2 = False
                    _fetch(source['url'] + 'index-v1.jar', jar, 256 * 1024 * 1024)
                    content, pin = _jar(jar, 'index-v1.json', source.get('fingerprint'))
                    timestamp = _v1(content, raw)
                    _check_timestamp(source, timestamp)
                else:
                    content, pin = _jar(jar, 'entry.json', source.get('fingerprint'), strong=True)
                    signed = json.loads(content)
                    timestamp, entry = signed.get('timestamp'), signed['index']
                    _check_timestamp(source, timestamp)
                    _fetch(_child(source['url'], entry['name']), raw, 512 * 1024 * 1024)
                    if _sha256(raw) != entry['sha256'] or (entry.get('size') is not None and raw.stat().st_size != entry['size']):
                        raise SourceError('index SHA-256 or size mismatch')
                apps = _reduce(raw, source)
                with _state_file_lock():  # recheck: another process may have accepted a newer index meanwhile
                    _check_timestamp(source, timestamp)
                    if not v2 and _state(source).get('v2'):  # a v2 index was accepted while we fetched v1
                        raise SourceError('repository now has a signed v2 index; refusing the older v1 index')
                    _write(cache, {'version': CACHE_VERSION, 'url': source['url'], 'fingerprint': pin, 'apps': apps})
                    _accept(source, timestamp, v2)
                _stale.discard(source['id'])
                return apps, pin
        except SourceLimited as e:
            raise _limited(source, e) from e
        except SourceError:
            raise
        except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
            raise SourceError('cannot load repository: ' + str(e)) from e


def add_repo(url, fingerprint=None, name=None):
    url, pin = _url(url, fingerprint)
    with _LOCK:
        existing = next((s for s in _read()['repos'] if s['url'] == url), None)
    if existing:
        if pin and pin != existing['fingerprint']:
            raise SourceError('repository already has a different pinned fingerprint; remove it first')
        pin = existing['fingerprint']
    source = dict(id='fdroid-user-' + hashlib.sha256(url.encode()).hexdigest()[:20], kind=KIND,
                  name=name or (existing or {}).get('name') or urllib.parse.urlsplit(url).hostname,
                  url=url, builtin=False, enabled=True, trust='user', fingerprint=pin)
    _, source['fingerprint'] = _load(source, force=True)  # network work outside the settings lock
    source['trust_on_first_use'] = existing.get('trust_on_first_use', False) if existing else pin is None
    with _LOCK:
        settings = _read()
        current = next((s for s in settings['repos'] if s['id'] == source['id']), None)
        if current and current['fingerprint'] != source['fingerprint']:
            raise SourceError('repository already has a different pinned fingerprint; remove it first')
        settings['repos'] = [s for s in settings['repos'] if s['id'] != source['id']] + [source]
        _write(_storage(), settings)
    return source


def remove_repo(source_id):
    with _LOCK:
        settings = _read()
        if not any(s['id'] == source_id for s in settings['repos']):
            raise SourceError('unknown user repository')
        settings['repos'] = [s for s in settings['repos'] if s['id'] != source_id]
        _write(_storage(), settings)
    with _state_file_lock(), _LOCK:  # lock order: state file, then _LOCK
        states = _states()
        if states.pop(source_id, None) is not None:  # re-adding is a deliberate new trust decision
            _write(_state_path(), states)


def set_enabled(source_id, enabled):
    if not isinstance(enabled, bool):
        raise SourceError('enabled must be a boolean')
    with _LOCK:
        settings = _read()
        source = next((s for s in sources() if s['id'] == source_id), None)
        if not source:
            raise SourceError('unknown repository')
        if source['builtin']:
            settings['enabled'][source_id] = enabled
        else:
            for s in settings['repos']:
                if s['id'] == source_id:
                    s['enabled'] = enabled
        _write(_storage(), settings)


def search(source, query, limit=50):
    if not source.get('enabled', True):
        return []
    apps, _ = _load(source)
    words = query.casefold().split()
    found = [a for a in apps.values() if all(w in (a['id'] + ' ' + a['name'] + ' ' + a['summary']).casefold() for w in words)]
    found.sort(key=lambda a: (a['id'].casefold() != query.casefold(), a['name'].casefold()))
    return [{k: v for k, v in a.items() if k != 'versions'} for a in found[:max(0, limit)]]


def details(source, entry_id):
    if not source.get('enabled', True):
        raise SourceError('repository is disabled')
    apps, _ = _load(source)
    if entry_id not in apps:
        raise SourceError('app has no Lepton-compatible version in this repository')
    return apps[entry_id]


def download(source, entry_id, version_code=None):
    entry = details(source, entry_id)
    version = next((v for v in entry['versions'] if version_code is None or str(v['version_code']) == str(version_code)), None)
    if not version or not re.fullmatch('[0-9a-f]{64}', version.get('sha256') or ''):
        raise SourceError('version is missing or has no SHA-256 digest')
    sha = version['sha256']
    path = frame_host.cache_dir('apk-sources', sha + '.apk')
    try:
        reuse = False
        if _web.touch(path):  # before hashing: a just-used APK is never pruned
            try:
                reuse = _sha256(path) == sha
            except FileNotFoundError:  # removed anyway (e.g. by hand): download again
                pass
        if not reuse:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.part')
            os.close(fd)
            try:
                _fetch(_child(source['url'], version['name']), tmp, 4 * 1024 ** 3)
                if _sha256(tmp) != sha:
                    raise SourceError('APK SHA-256 mismatch; download discarded')
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return {'apk': str(path), 'obb': [], 'sha256': sha, 'verified': True}
    except SourceLimited as e:
        raise _limited(source, e) from e
    except OSError as e:
        raise SourceError('cannot download APK: ' + str(e)) from e


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    add = sub.add_parser('add')
    add.add_argument('url')
    add.add_argument('--fingerprint')
    add.add_argument('--name')
    sub.add_parser('list')
    remove = sub.add_parser('remove')
    remove.add_argument('source')
    for command in ('search', 'download'):
        p = sub.add_parser(command)
        p.add_argument('source')
        p.add_argument('query' if command == 'search' else 'package')
    args = parser.parse_args()
    try:
        _cli(parser, args)
    finally:
        for thread in list(_refreshing.values()):  # daemon refreshes would die with the CLI, even on errors
            thread.join()


def _cli(parser, args):
    try:
        if args.command == 'add':
            result = add_repo(args.url, args.fingerprint, args.name)
        elif args.command == 'list':
            result = sources()
        elif args.command == 'remove':
            result = remove_repo(args.source)
        else:
            source = next((s for s in sources() if s['id'] == args.source), None)
            if not source:
                raise SourceError('unknown repository id; use list')
            result = search(source, args.query) if args.command == 'search' else download(source, args.package)
        print(json.dumps(result, indent=2))
    except SourceError as e:
        parser.exit(1, 'error: ' + str(e) + '\n')


if __name__ == '__main__':
    main()
