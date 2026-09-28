"""Bounded artwork inputs for Steam library canvas rendering."""
import struct
import time
import zlib

SLOTS = {'grid': (600, 900), 'wide': (920, 430), 'hero': (3840, 1240),
         'logo': (1280, 480), 'icon': (256, 256)}
MAX_IMAGE = 12 * 1024 * 1024
MAX_PIXELS = 4096 * 4096  # a 4K screenshot; Chromium decodes it on the Frame
PNG = b'\x89PNG\r\n\x1a\n'


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png_size(data):
    """IHDR dimensions of a PNG of any bit depth or interlace; Steam's Chromium decodes the pixels."""
    if len(data) < 33 or data[12:16] != b'IHDR' or struct.unpack_from('>I', data, 8)[0] != 13:
        raise ValueError('invalid PNG header')
    if zlib.crc32(data[12:29]) != struct.unpack_from('>I', data, 29)[0]:
        raise ValueError('invalid PNG checksum')
    w, h, depth, color = struct.unpack_from('>IIBB', data, 16)
    if color not in (0, 2, 3, 4, 6) or depth not in (1, 2, 4, 8, 16):
        raise ValueError('unsupported PNG encoding')
    if not w or not h or w * h > MAX_PIXELS or w > 8192 or h > 8192:
        raise ValueError('PNG dimensions exceed limits')
    return w, h


def image_type(data):
    if not isinstance(data, bytes) or len(data) > MAX_IMAGE:
        raise ValueError('artwork must be image bytes or an HTTP(S) URL, at most 12 MiB')
    if data.startswith(PNG):
        png_size(data)
        return 'png'
    if data[:6] in (b'GIF87a', b'GIF89a') and len(data) >= 10:
        # Gameplay GIFs are common source screenshots; the Frame's Chromium draws their first frame.
        w, h = struct.unpack_from('<HH', data, 6)
        if w and h and w * h <= MAX_PIXELS and max(w, h) <= 8192:
            return 'gif'
        raise ValueError('artwork GIF has unsupported dimensions')
    if data.startswith(b'\xff\xd8'):
        # Check JPEG SOF dimensions without depending on an image library; trailing padding is fine.
        pos = 2
        while pos + 4 <= len(data) and data[pos] == 255:
            marker = data[pos + 1]
            pos += 2
            if marker == 255:
                pos -= 1
                continue
            size = struct.unpack_from('>H', data, pos)[0]
            if size < 2 or pos + size > len(data):
                break
            if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc) and size >= 8:
                h, w = struct.unpack_from('>HH', data, pos + 3)
                if w and h and w * h <= MAX_PIXELS and max(w, h) <= 8192:
                    return 'jpg'
                break
            pos += size
    raise ValueError('artwork must be a supported PNG, JPEG or GIF')


def fetch(value, deadline=None):
    if isinstance(value, str):
        from apk_sources import _images
        # Public addresses only, at most three redirects, within the overall deadline.
        value = _images.fetch(value, deadline=deadline, limit=MAX_IMAGE)[0]
    return image_type(value), value


def prepare(label, icon_png=None, artwork=None, budget=90):
    """Gather inputs; the Frame's Chromium canvas renders every final slot.

    Every source is optional: any failure falls back to generated art, within budget seconds overall."""
    import frame_steamgriddb
    artwork = artwork or {}
    allowed = set(SLOTS) | {'banner', 'feature_graphic', 'screenshots', 'screenshot'}
    if not isinstance(artwork, dict) or set(artwork) - allowed:
        raise ValueError('unknown artwork slot')
    deadline = time.monotonic() + budget
    supplied, warnings = {}, []

    def get(value):
        try:
            return fetch(value, deadline)
        except Exception:  # an optional source never blocks the install; generated art covers it
            return None
    for slot, value in artwork.items():
        values = value if slot == 'screenshots' and isinstance(value, (list, tuple)) else [value]
        for candidate in values[:4]:
            image = get(candidate)
            if image:
                supplied['screenshot' if slot == 'screenshots' else slot] = image
                break
        else:
            warnings.append('Source ' + slot + ' unavailable; using fallback art')
    if 'icon' not in supplied and icon_png:
        image = get(icon_png)
        if image:
            supplied['icon'] = image
    try:
        provider, provider_warnings = frame_steamgriddb.lookup(label, deadline)
    except Exception:
        provider, provider_warnings = {}, ['SteamGridDB unavailable; using source or generated art']
    warnings.extend(provider_warnings)
    for slot, value in provider.items():
        image = get(value)
        if image:
            supplied[slot] = image
        else:
            warnings.append('SteamGridDB ' + slot + ' download failed; using fallback art')
    return supplied, warnings
