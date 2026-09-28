"""Bounded artwork inputs for Steam library canvas rendering."""
import struct
import urllib.parse
import urllib.request
import zlib

SLOTS = {'grid': (600, 900), 'wide': (920, 430), 'hero': (3840, 1240),
         'logo': (1280, 480), 'icon': (256, 256)}
MAX_IMAGE = 12 * 1024 * 1024
MAX_PIXELS = 8_000_000
PNG = b'\x89PNG\r\n\x1a\n'


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png(width, height, pixels):
    stride = width * 4
    raw = b''.join(b'\0' + pixels[y * stride:(y + 1) * stride] for y in range(height))
    return PNG + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)) + \
        chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b'')


def decode(data):
    """Non-interlaced PNG, including packed palette/grayscale APK icons."""
    if not isinstance(data, bytes) or not data.startswith(PNG) or len(data) > MAX_IMAGE:
        raise ValueError('expected a PNG image (at most 12 MiB)')
    pos, packed, palette, alpha, header = 8, bytearray(), b'', b'', None
    while pos + 12 <= len(data):
        size = struct.unpack_from('>I', data, pos)[0]
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + size]
        if pos + size + 12 > len(data):
            raise ValueError('truncated PNG')
        crc = struct.unpack_from('>I', data, pos + 8 + size)[0]
        if zlib.crc32(kind + body) != crc:
            raise ValueError('invalid PNG checksum')
        if kind == b'IHDR':
            if header is not None or size != 13:
                raise ValueError('invalid PNG header')
            header = struct.unpack('>IIBBBBB', body)
        elif kind == b'PLTE':
            palette = body
        elif kind == b'tRNS':
            alpha = body
        elif kind == b'IDAT':
            packed.extend(body)
        elif kind == b'IEND':
            break
        pos += size + 12
    else:
        raise ValueError('incomplete PNG')
    if header is None:
        raise ValueError('missing PNG header')
    w, h, depth, color, compression, filtering, interlace = header
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if not w or not h or w * h > MAX_PIXELS or w > 8192 or h > 8192:
        raise ValueError('PNG dimensions exceed limits')
    if not channels or compression or filtering or interlace or depth not in (1, 2, 4, 8) or (depth != 8 and color not in (0, 3)):
        raise ValueError('unsupported PNG encoding')
    stride, bpp = (w * channels * depth + 7) // 8, max(1, channels * depth // 8)
    expected = h * (stride + 1)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(bytes(packed), expected + 1)
    if len(raw) != expected or not decoder.eof:
        raise ValueError('invalid PNG pixels')
    pixels, prev = bytearray(), bytearray(stride)
    for y in range(h):
        start = y * (stride + 1)
        method, row = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        if method > 4:
            raise ValueError('invalid PNG filter')
        for x in range(stride):
            a, b, c = row[x - bpp] if x >= bpp else 0, prev[x], prev[x - bpp] if x >= bpp else 0
            if method == 1:
                row[x] = (row[x] + a) & 255
            elif method == 2:
                row[x] = (row[x] + b) & 255
            elif method == 3:
                row[x] = (row[x] + (a + b) // 2) & 255
            elif method == 4:
                p = a + b - c
                distances = (abs(p - a), abs(p - b), abs(p - c))
                row[x] = (row[x] + (a, b, c)[distances.index(min(distances))]) & 255
        for x in range(w):
            if depth < 8:
                value = (row[x * depth // 8] >> (8 - depth - x * depth % 8)) & ((1 << depth) - 1)
                values = [value]
            else:
                values = row[x * channels:(x + 1) * channels]
            if color == 3:
                i = values[0]
                if i * 3 + 3 > len(palette):
                    raise ValueError('invalid PNG palette')
                rgba = palette[i * 3:i * 3 + 3] + bytes([alpha[i] if i < len(alpha) else 255])
            elif color in (0, 4):
                gray = values[0] * 255 // ((1 << depth) - 1)
                opacity = values[1] if color == 4 else (0 if alpha == struct.pack('>H', values[0]) else 255)
                rgba = bytes([gray, gray, gray, opacity])
            else:
                opacity = values[3] if color == 6 else (0 if alpha == struct.pack('>HHH', *values) else 255)
                rgba = bytes(values[:3]) + bytes([opacity])
            pixels.extend(rgba)
        prev = row
    return w, h, pixels


def image_type(data):
    if not isinstance(data, bytes) or len(data) > MAX_IMAGE:
        raise ValueError('artwork must be image bytes or an HTTP(S) URL, at most 12 MiB')
    if data.startswith(PNG):
        try:
            decode(data)
        except (zlib.error, struct.error) as e:
            raise ValueError('invalid PNG image') from e
        return 'png'
    if data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'):
        # Check JPEG SOF dimensions without depending on an image library.
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
            if marker in (0xc0, 0xc1, 0xc2) and size >= 8:
                h, w = struct.unpack_from('>HH', data, pos + 3)
                if w and h and w * h <= MAX_PIXELS and max(w, h) <= 8192:
                    return 'jpg'
                break
            pos += size
    raise ValueError('artwork must be a supported PNG or JPEG')


def stamp(pixels, width, icon, x, y, size):
    iw, ih, source = icon
    dw, dh = max(1, size * iw // max(iw, ih)), max(1, size * ih // max(iw, ih))
    x, y = x + (size - dw) // 2, y + (size - dh) // 2
    for yy in range(dh):
        for xx in range(dw):
            src = ((yy * ih // dh) * iw + xx * iw // dw) * 4
            dst = ((y + yy) * width + x + xx) * 4
            a = source[src + 3]
            for c in range(3):
                pixels[dst + c] = (source[src + c] * a + pixels[dst + c] * (255 - a)) // 255
            pixels[dst + 3] = a + pixels[dst + 3] * (255 - a) // 255



def fetch(value):
    if isinstance(value, str):
        if urllib.parse.urlsplit(value).scheme not in ('http', 'https'):
            raise ValueError('artwork URLs must use HTTP(S)')
        request = urllib.request.Request(value, headers={'User-Agent': 'FrameControl/1.0'})
        with urllib.request.urlopen(request, timeout=20) as response:
            if urllib.parse.urlsplit(response.geturl()).scheme not in ('http', 'https'):
                raise ValueError('artwork redirect must use HTTP(S)')
            value = response.read(MAX_IMAGE + 1)
    return image_type(value), value


def prepare(label, icon_png=None, artwork=None):
    """Gather inputs; the Frame's Chromium canvas renders every final slot."""
    import frame_steamgriddb
    artwork = artwork or {}
    allowed = set(SLOTS) | {'banner', 'feature_graphic', 'screenshots', 'screenshot'}
    if not isinstance(artwork, dict) or set(artwork) - allowed:
        raise ValueError('unknown artwork slot')
    supplied, warnings = {}, []
    for slot, value in artwork.items():
        values = value if slot == 'screenshots' and isinstance(value, (list, tuple)) else [value]
        for candidate in values[:4]:
            try:
                supplied['screenshot' if slot == 'screenshots' else slot] = fetch(candidate)
                break
            except (ValueError, OSError):
                warnings.append('Source ' + slot + ' unavailable; using fallback art')
    if 'icon' not in supplied and icon_png:
        try:
            supplied['icon'] = fetch(icon_png)
        except (ValueError, OSError):
            pass
    provider, provider_warnings = frame_steamgriddb.lookup(label)
    warnings.extend(provider_warnings)
    for slot, value in provider.items():
        try:
            supplied[slot] = fetch(value)
        except (ValueError, OSError):
            warnings.append('SteamGridDB ' + slot + ' download failed; using fallback art')
    return supplied, warnings
