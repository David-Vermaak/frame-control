"""Binary-manifest VR inspection and minimal launcher repair (stdlib only)."""
import struct
import frame_apk

MAIN = 'android.intent.action.MAIN'
LAUNCHER = 'android.intent.category.LAUNCHER'
VR = {'com.oculus.intent.category.VR', 'org.khronos.openxr.intent.category.IMMERSIVE_HMD'}


def inspect(data):
    elements = iter(frame_apk.manifest_elements(data))
    stack, filters, samsung = [], [], False
    current = None
    for kind, hs, off, size in frame_apk._chunks(data, 8, len(data)):
        if kind == 0x0102:
            tag, attrs = next(elements)
            value = attrs.get('name', (None, None, None))[2]
            if tag == 'intent-filter' and stack and stack[-1] in ('activity', 'activity-alias'):
                current = {'actions': set(), 'categories': set(), 'templates': []}
            if current is not None and stack and stack[-1] == 'intent-filter':
                if tag == 'action':
                    current['actions'].add(value)
                if tag == 'category':
                    current['categories'].add(value)
                    current['templates'].append((off, size, hs))
            if tag == 'meta-data' and stack and stack[-1] == 'application':
                samsung |= value == 'com.samsung.android.vr.application.mode' and attrs.get('value', (0, 0, None))[2] == 'vr_only'
            stack.append(tag)
        elif kind == 0x0103 and stack:
            tag = stack.pop()
            if tag == 'intent-filter' and current is not None:
                current['end'] = off
                filters.append(current)
                current = None
    mains = [f for f in filters if MAIN in f['actions']]
    vr_filters = [f for f in mains if VR & f['categories']]
    return {'launchable': any(LAUNCHER in f['categories'] for f in mains),
            'vr_activity': bool(vr_filters), 'vr': bool(vr_filters) or samsung}, vr_filters


def _append_string(chunk, text):
    _, hs, size, count, styles, flags, start, style_start = struct.unpack_from('<HHIIIIII', chunk)
    strings_end = style_start or size
    encoded = text.encode('utf-8' if flags & 0x100 else 'utf-16-le')
    # LAUNCHER is short enough for both single-unit length encodings.
    new = (bytes([len(text), len(encoded)]) + encoded + b'\0' if flags & 0x100
           else struct.pack('<H', len(text)) + encoded + b'\0\0')
    string_data = chunk[start:strings_end] + new
    string_data += bytes(-len(string_data) % 4)
    header = bytearray(chunk[:hs])
    new_start = start + 4
    new_styles = new_start + len(string_data) if style_start else 0
    body = (chunk[hs:hs + count * 4] + struct.pack('<I', strings_end - start)
            + chunk[hs + count * 4:start] + string_data + (chunk[style_start:] if style_start else b''))
    struct.pack_into('<IIIII', header, 8, count + 1, styles, flags & ~1, new_start, new_styles)
    struct.pack_into('<I', header, 4, len(header) + len(body))
    return bytes(header) + body, count


def add_launcher_category(axml_bytes):
    info, filters = inspect(axml_bytes)
    if info['launchable']:
        return axml_bytes
    if not filters:
        raise frame_apk.ApkError('no VR activity with a MAIN intent filter to patch')
    target = filters[0]
    chunks = list(frame_apk._chunks(axml_bytes, 8, len(axml_bytes)))
    pool = next(c for c in chunks if c[0] == 1)
    _, _, po, ps = pool
    new_pool, index = _append_string(axml_bytes[po:po + ps], LAUNCHER)
    off, size, hs = target['templates'][0]
    start = bytearray(axml_bytes[off:off + size])
    attr_start, attr_size, count = struct.unpack_from('<HHH', start, hs + 8)
    strings = frame_apk._string_pool(axml_bytes, po)
    resmap = []
    for kind, header_size, offset, chunk_size in chunks:
        if kind == 0x180:
            resmap = struct.unpack_from('<%dI' % ((chunk_size - header_size) // 4),
                                        axml_bytes, offset + header_size)
    for i in range(count):
        a = hs + attr_start + i * attr_size
        name = struct.unpack_from('<I', start, a + 4)[0]
        if (name < len(resmap) and resmap[name] == 0x01010003) or strings[name] == 'name':
            struct.pack_into('<I', start, a + 8, index)
            struct.pack_into('<HBBI', start, a + 12, 8, 0, 3, index)
            break
    else:
        raise frame_apk.ApkError('VR category has no name attribute')
    end = struct.pack('<HHI', 0x0103, hs, hs + 8) + start[8:hs] + start[hs:hs + 8]
    result = bytearray(axml_bytes[:8])
    for _, _, off, size in chunks:
        if off == target['end']:
            result += start + end
        result += new_pool if off == po else axml_bytes[off:off + size]
    struct.pack_into('<I', result, 4, len(result))
    result = bytes(result)
    if not inspect(result)[0]['launchable']:
        raise frame_apk.ApkError('launcher repair failed verification')
    return result


def detection(data, names):
    info, _ = inspect(data)
    issues = []
    if 'lib/arm64-v8a/libvrapi.so' in names:
        issues.append("Uses Meta's legacy VrApi, which the Frame doesn't have; it won't run.")
    if any(n.endswith('/libovrplatformloader.so') for n in names):
        issues.append("Uses Meta's platform SDK; if it checks your Quest store licence it will quit.")
    if 'lib/arm64-v8a/libopenxr_loader.so' in names:
        info['vr'] = True
        issues.append('Uses OpenXR (good).')
    info['vr_issues'] = issues
    return info
