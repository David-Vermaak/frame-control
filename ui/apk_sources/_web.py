"""Small HTTPS cache and APK downloader for public publisher sources."""
import hashlib, os, tempfile, time, urllib.error, urllib.parse, urllib.request, zipfile

import frame_host
from . import SourceError

UA = 'FrameControl/0.1'


def cache():
    path = frame_host.cache_dir('apk-sources', 'publisher')
    os.makedirs(path, exist_ok=True)
    return str(path)


def checked_url(url, hosts):
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except (ValueError, TypeError) as e:
        raise SourceError('Source returned an invalid URL') from e
    if p.scheme != 'https' or p.username or p.password or port not in (None, 443) or p.hostname not in hosts:
        raise SourceError('Source returned an unexpected download URL')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl, self.hosts)
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result:
            result.remove_header('Authorization')
        return result


def open_url(url, hosts, headers=None):
    checked_url(url, hosts)
    return urllib.request.build_opener(Redirect(hosts)).open(
        urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})}), timeout=60)


def read(url, hosts, headers=None, ttl=3600):
    path = os.path.join(cache(), hashlib.sha256(url.encode()).hexdigest() + '.data')
    try:
        if os.path.isfile(path) and time.time() - os.path.getmtime(path) < ttl:
            with open(path, 'rb') as f:
                return f.read()
        with open_url(url, hosts, headers) as r:
            data = r.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise SourceError('Source index is too large')
        fd, tmp = tempfile.mkstemp(dir=cache(), suffix='.part')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return data
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise SourceError('Source refused access or reached its rate limit; try later (GitHub accepts FRAME_GITHUB_TOKEN)') from e
        raise SourceError('Source HTTP error: ' + str(e.code)) from e
    except (OSError, ValueError) as e:
        raise SourceError('Could not read source: ' + str(e)) from e


def apk(url, hosts, digest=None):
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=cache(), suffix='.part')
        h = hashlib.sha256()
        with os.fdopen(fd, 'wb') as f, open_url(url, hosts) as r:
            size = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > 2 * 1024 ** 3:
                    raise SourceError('APK exceeds the 2 GiB download limit')
                h.update(chunk)
                f.write(chunk)
        actual = h.hexdigest()
        if digest and actual != digest:
            raise SourceError('SHA-256 mismatch; download discarded')
        with zipfile.ZipFile(tmp) as z:
            if 'AndroidManifest.xml' not in z.namelist():
                raise SourceError('Download is not an APK')
        path = os.path.join(cache(), actual + '.apk')
        os.replace(tmp, path)
        return {'apk': path, 'obb': [], 'sha256': actual, 'verified': bool(digest)}
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        raise SourceError('Could not download APK: ' + str(e)) from e
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
