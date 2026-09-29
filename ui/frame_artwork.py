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


def _gif_blocks(data, pos):
    """Position after a run of GIF data sub-blocks and its terminator."""
    while True:
        if pos >= len(data):
            raise ValueError('truncated GIF')
        size = data[pos]
        pos += 1 + size
        if not size:
            return pos


def gif_frame(data):
    """A GIF's first frame as a minimal single-frame GIF, so no frame count or oversized frame
    reaches the Frame's Chromium. Raises ValueError for anything malformed or out of bounds."""
    if data[:6] not in (b'GIF87a', b'GIF89a') or len(data) < 13:
        raise ValueError('invalid GIF')
    sw, sh, flags = struct.unpack_from('<HHB', data, 6)
    if not sw or not sh or sw * sh > MAX_PIXELS or max(sw, sh) > 4096:
        raise ValueError('artwork GIF has unsupported dimensions')
    pos = 13 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)
    head, control = data[:pos], b''
    if len(head) != pos:
        raise ValueError('truncated GIF')
    while True:
        if pos >= len(data):
            raise ValueError('truncated GIF')
        if data[pos] == 0x21 and pos + 1 < len(data):  # extension: keep the frame's graphic control
            end = _gif_blocks(data, pos + 2)
            if data[pos + 1] == 0xf9 and end - pos == 8 and data[pos + 2] == 4:  # GIF89a: fixed 4-byte payload
                control = data[pos:end]
            pos = end
        elif data[pos] == 0x2c and pos + 10 <= len(data):  # the first image
            x, y, w, h, local = struct.unpack_from('<HHHHB', data, pos + 1)
            if not w or not h or x + w > sw or y + h > sh:
                raise ValueError('artwork GIF frame exceeds its screen')
            if not flags & 0x80 and not local & 0x80:
                raise ValueError('GIF has no colour table')
            start = pos
            pos += 10 + (3 << ((local & 7) + 1) if local & 0x80 else 0)
            if pos >= len(data) or not 2 <= data[pos] <= 8:  # the LZW minimum code size
                raise ValueError('invalid GIF image data')
            end = _gif_blocks(data, pos + 1)
            if end - pos <= 2:  # code size then the terminator: no pixels at all
                raise ValueError('invalid GIF image data')
            return head + control + data[start:end] + b'\x3b'
        else:
            raise ValueError('invalid GIF block')


def image_type(data):
    if not isinstance(data, bytes) or len(data) > MAX_IMAGE:
        raise ValueError('artwork must be image bytes or an HTTP(S) URL, at most 12 MiB')
    if data.startswith(PNG):
        png_size(data)
        return 'png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        gif_frame(data)
        return 'gif'
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
    kind = image_type(value)
    return kind, gif_frame(value) if kind == 'gif' else value


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
