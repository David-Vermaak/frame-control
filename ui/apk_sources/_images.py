"""Bounded artwork cache. Only source-provided URLs get opaque image handles."""
from collections import OrderedDict
import http.client
import ipaddress
import secrets
import socket
import ssl
import threading
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


def fetch(url, redirects=3):
    if not valid_url(url):
        raise SourceError('Artwork URL is not allowed')
    p = urlsplit(url)
    port = p.port or (443 if p.scheme == 'https' else 80)
    addresses = socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise SourceError('Private network artwork is not allowed')
    # Connect to the checked IP, never resolve again between validation and use.
    sock = socket.create_connection((addresses[0][4][0], port), timeout=10)
    conn = http.client.HTTPConnection(p.hostname, port, timeout=10)
    try:
        if p.scheme == 'https':
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
        conn.sock = sock
        path = p.path or '/'
        if p.query:
            path += '?' + p.query
        conn.request('GET', path, headers={'User-Agent': 'FrameControl/0.3.1', 'Accept': 'image/png,image/jpeg,image/webp,image/gif'})
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308) and redirects:
            target = urljoin(url, response.getheader('Location', ''))
            conn.close()
            return fetch(target, redirects - 1)
        if response.status != 200:
            raise SourceError('Artwork is unavailable')
        data = response.read(MAX_IMAGE + 1)
        if len(data) > MAX_IMAGE:
            raise SourceError('Artwork is too large')
        return data, image_type(data)
    finally:
        conn.close()
        sock.close()


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
