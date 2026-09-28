"""Bounded artwork cache. Only source-provided URLs get opaque image handles."""
from collections import OrderedDict
import http.client
import ipaddress
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

from apk_sources import SourceError

_lock = threading.Lock()
_urls = OrderedDict()
_cache = OrderedDict()
MAX_IMAGE = 8 * 1024 * 1024
MAX_CACHE = 64 * 1024 * 1024


def valid_url(url):
    try:
        p = urlsplit(url)
        return (p.scheme in ('https', 'http') and bool(p.hostname) and not p.username and not p.password
                and p.port in (None, 80, 443) and len(url) <= 4096)
    except (ValueError, TypeError):
        return False


def register(url):
    if not isinstance(url, str) or not valid_url(url):
        return None
    with _lock:
        for token, known in _urls.items():
            if known == url:
                _urls.move_to_end(token)
                return '/source-image/' + token
        token = secrets.token_urlsafe(24)
        _urls[token] = url
        while len(_urls) > 4096:
            _urls.popitem(last=False)
    return '/source-image/' + token


def artwork(entry):
    images = entry.get('images') or {}
    if not isinstance(images, dict):
        images = {}
    return {'icon': register(images.get('icon') or entry.get('icon')),
            'banner': register(images.get('banner')),
            'screenshots': [path for path in (register(u) for u in (images.get('screenshots') or [])[:12]) if path]}


def image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    raise SourceError('Artwork is not a supported image')


def fetch(url, redirects=3, deadline=None, limit=MAX_IMAGE):
    """deadline: time.monotonic() value by which the whole fetch, redirects included, must finish."""
    data = get(url, {'Accept': 'image/png,image/jpeg,image/webp,image/gif'}, redirects, deadline, limit)
    return data, image_type(data)


# Lookups that outlast their deadline keep running; cap them so they can't pile up.
_resolvers = threading.BoundedSemaphore(4)


def _resolve(host, port, timeout):
    # getaddrinfo has no timeout of its own; a thread keeps a slow resolver inside the budget.
    if not _resolvers.acquire(blocking=False):
        raise SourceError('Too many slow artwork name lookups are still running; try again shortly')
    found = {}

    def run():
        try:
            found['addresses'] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as e:
            found['error'] = e
        finally:
            _resolvers.release()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if 'error' in found:
        raise found['error']
    if 'addresses' not in found:
        raise SourceError('Artwork download took too long')
    return found['addresses']


def get(url, headers=None, redirects=3, deadline=None, limit=MAX_IMAGE):
    """GET a public HTTP(S) URL within an overall deadline (default 60 s), redirects included.

    A watchdog shuts the socket at the deadline, so a server trickling bytes can't outlast it."""
    deadline = time.monotonic() + 60 if deadline is None else deadline

    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError('Artwork download took too long')
        return remaining
    if not valid_url(url):
        raise SourceError('Artwork URL is not allowed')
    p = urlsplit(url)
    port = p.port or (443 if p.scheme == 'https' else 80)
    addresses = _resolve(p.hostname, port, left())
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise SourceError('Private network artwork is not allowed')
    # Connect to the checked IP, never resolve again between validation and use.
    live = [socket.create_connection((addresses[0][4][0], port), timeout=min(10, left()))]

    def expire():
        try:  # the plain socket method: it also ends a TLS handshake in progress
            socket.socket.shutdown(live[0], socket.SHUT_RDWR)
        except OSError:
            pass
    watchdog = threading.Timer(left(), expire)
    watchdog.daemon = True
    watchdog.start()
    conn = http.client.HTTPConnection(p.hostname, port, timeout=10)
    try:
        if p.scheme == 'https':
            # Handshake only once the watchdog can reach the TLS socket, within the remaining time.
            live[0] = ssl.create_default_context().wrap_socket(live[0], server_hostname=p.hostname,
                                                               do_handshake_on_connect=False)
            live[0].settimeout(min(10, left()))
            live[0].do_handshake()
        conn.sock = live[0]
        path = p.path or '/'
        if p.query:
            path += '?' + p.query
        conn.request('GET', path, headers={'User-Agent': 'FrameControl/0.3.1', **(headers or {})})
        live[0].settimeout(min(10, left()))
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308) and redirects:
            target = urljoin(url, response.getheader('Location', ''))
            conn.close()
            return get(target, headers, redirects - 1, deadline, limit)
        if response.status != 200:
            raise SourceError('Artwork is unavailable')
        data = b''
        while len(data) <= limit:
            live[0].settimeout(min(10, left()))
            chunk = response.read1(min(16384, limit + 1 - len(data)))  # one receive at most
            if not chunk:
                break
            data += chunk
        if len(data) > limit:
            raise SourceError('Artwork is too large')
        left()
        return data
    except (OSError, http.client.HTTPException) as e:
        if time.monotonic() >= deadline:
            raise SourceError('Artwork download took too long') from e
        raise
    finally:
        watchdog.cancel()
        conn.close()
        live[0].close()


def remember(url, data):
    value = (data, image_type(data))
    if len(data) > MAX_IMAGE:
        raise SourceError('Artwork is too large')
    with _lock:
        _cache[url] = value
        _cache.move_to_end(url)
        while sum(len(v[0]) for v in _cache.values()) > MAX_CACHE:
            _cache.popitem(last=False)
    return value


def image(token):
    with _lock:
        url = _urls.get(token)
        if not url:
            raise SourceError('Unknown artwork')
        cached = _cache.get(url)
        if cached:
            _cache.move_to_end(url)
            return cached
    data, _ = fetch(url)
    return remember(url, data)
