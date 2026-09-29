"""Lossless ZIP repacking and APK v2 RSA/SHA-256 signing, Python 3.9 stdlib.

Spec: https://source.android.com/docs/security/features/apksigning/v2
Sections: APK Signing Block; APK Signature Scheme v2 Block; Integrity-protected
contents; Verification. No verity algorithm is used, so no verity padding.
"""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import struct
import tempfile
import zipfile
import zlib

import frame_host

MAGIC = b'APK Sig Block 42'
V2 = 0x7109871a
ALG = 0x0103
SHA256_DER = bytes.fromhex('3031300d060960864801650304020105000420')


def u32(n):
    return struct.pack('<I', n)


def lp(b):
    return u32(len(b)) + b


def der(tag, b):
    n = len(b)
    length = bytes([n]) if n < 128 else bytes([128 + (n.bit_length() + 7) // 8]) + n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([tag]) + length + b


def integer(n):
    b = n.to_bytes((n.bit_length() + 7) // 8 or 1, 'big')
    return der(2, (b'\0' if b[0] & 128 else b'') + b)


def sequence(*items):
    return der(0x30, b''.join(items))


RSA_ALG = bytes.fromhex('300d06092a864886f70d0101010500')
CERT_ALG = bytes.fromhex('300d06092a864886f70d01010b0500')


def public_key(key):
    return sequence(RSA_ALG, der(3, b'\0' + sequence(integer(key['n']), integer(key['e']))))


def encoded_hash(data, size):
    digest = SHA256_DER + hashlib.sha256(data).digest()
    return b'\0\1' + b'\xff' * (size - len(digest) - 3) + b'\0' + digest


def rsa_sign(data, key):
    size = (key['n'].bit_length() + 7) // 8
    return pow(int.from_bytes(encoded_hash(data, size), 'big'), key['d'], key['n']).to_bytes(size, 'big')


def rsa_verify(data, sig, n, e):
    size = (n.bit_length() + 7) // 8
    if len(sig) != size or int.from_bytes(sig, 'big') >= n:
        raise ValueError('invalid RSA signature size/value')
    actual = pow(int.from_bytes(sig, 'big'), e, n).to_bytes(size, 'big')
    if actual != encoded_hash(data, size):
        raise ValueError('RSA signature mismatch')


def certificate(key):
    name = sequence(der(0x31, sequence(bytes.fromhex('0603550403'), der(12, b'Frame Control APK signer'))))
    validity = sequence(der(0x17, b'200101000000Z'), der(0x18, b'21200101000000Z'))
    tbs = sequence(der(0xa0, integer(2)), integer(1), CERT_ALG, name, validity, name, public_key(key))
    return sequence(tbs, CERT_ALG, der(3, b'\0' + rsa_sign(tbs, key)))


def _prime(bits):
    small = (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47)
    while True:
        n = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if any(n % p == 0 for p in small) or (n - 1) % 65537 == 0:
            continue
        d, s = n - 1, 0
        while d % 2 == 0:
            d //= 2
            s += 1
        for _ in range(40):  # Miller-Rabin error bound <= 2^-80
            x = pow(secrets.randbelow(n - 3) + 2, d, n)
            if x in (1, n - 1):
                continue
            for _ in range(s - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                break
        else:
            return n


def signing_key(path=None):
    """Persistent identity in app data, never an evictable cache. Atomic publication.

    Hard-linking a fully written private temp file prevents concurrent first-use
    callers from selecting different identities or reading a partial key.
    """
    path = Path(path) if path is not None else frame_host.data_dir('apk-signing-key.json')
    if not path.exists():
        p, q = _prime(1024), _prime(1024)
        while q == p:
            q = _prime(1024)
        key = {'n': p * q, 'e': 65537, 'd': pow(65537, -1, math.lcm(p - 1, q - 1))}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.apk-key-', dir=str(path.parent))
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(key, f)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)  # never replaces a key another process wrote first
            except FileExistsError:
                pass
            except OSError:  # no hard links (FAT/exFAT): plain rename
                if not path.exists():
                    os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    os.chmod(path, 0o600)  # Windows ignores this; the per-user app-data folder is the protection there
    key = json.loads(path.read_text())
    if key['n'].bit_length() != 2048 or key['e'] != 65537:
        raise ValueError(f'invalid cached APK signing key; delete {path} to make a new one '
                         '(re-signed apps then need reinstalling)')
    rsa_verify(b'key check', rsa_sign(b'key check', key), key['n'], key['e'])
    return key


def _eocd(data):
    # ZIP comments can contain the EOCD signature; accept only an exact EOF fit.
    for at in range(len(data) - 22, max(-1, len(data) - 65558), -1):
        if data[at:at + 4] == b'PK\5\6' and at + 22 + struct.unpack_from('<H', data, at + 20)[0] == len(data):
            disk, cd_disk, count_disk, count, size, cd = struct.unpack_from('<HHHHII', data, at + 4)
            if disk or cd_disk or count_disk != count or count == 65535 or cd + size != at:
                raise ValueError('multi-disk/ZIP64 or invalid APK directory')
            return at, cd
    raise ValueError('missing ZIP end record')


def content_digest(sections):
    chunks = []
    for section in sections:
        for off in range(0, len(section), 1024 * 1024):
            part = section[off:off + 1024 * 1024]
            chunks.append(hashlib.sha256(b'\xa5' + u32(len(part)) + part).digest())
    return hashlib.sha256(b'\x5a' + u32(len(chunks)) + b''.join(chunks)).digest()


def sign_apk(data, key):
    eo, cd = _eocd(data)
    digest = content_digest((memoryview(data)[:cd], memoryview(data)[cd:eo], data[eo:]))
    signed = lp(lp(u32(ALG) + lp(digest))) + lp(lp(certificate(key))) + lp(b'')
    signer = lp(signed) + lp(lp(u32(ALG) + lp(rsa_sign(signed, key)))) + lp(public_key(key))
    value = lp(lp(signer))
    pair = struct.pack('<Q', 4 + len(value)) + u32(V2) + value
    size = len(pair) + 24
    block = struct.pack('<Q', size) + pair + struct.pack('<Q', size) + MAGIC
    end = bytearray(data[eo:])
    struct.pack_into('<I', end, 16, cd + len(block))
    return data[:cd] + block + data[cd:eo] + end


def _parts(data):
    result, off = [], 0
    while off < len(data):
        if off + 4 > len(data):
            raise ValueError('truncated length prefix')
        size = struct.unpack_from('<I', data, off)[0]
        off += 4
        if off + size > len(data):
            raise ValueError('length prefix outside block')
        result.append(data[off:off + size])
        off += size
    return result


def _der_parts(data):
    result, off = [], 0
    while off < len(data):
        start = off
        tag, size = data[off:off + 2]
        off += 2
        if size & 128:
            count = size & 127
            if not count or count > 4:
                raise ValueError('invalid DER length')
            size = int.from_bytes(data[off:off + count], 'big')
            off += count
        if off + size > len(data):
            raise ValueError('truncated DER')
        result.append((tag, data[off:off + size], data[start:off + size]))
        off += size
    return result


def _cert_key(cert):
    outer = _der_parts(cert)
    if len(outer) != 1 or outer[0][0] != 0x30:
        raise ValueError('invalid certificate')
    fields = _der_parts(outer[0][1])
    tbs = _der_parts(fields[0][1])
    spki = tbs[6 if tbs[0][0] == 0xa0 else 5][2]
    pub = _der_parts(_der_parts(spki)[0][1])
    if pub[0][2] != RSA_ALG or pub[1][1][:1] != b'\0':
        raise ValueError('certificate is not RSA')
    numbers = _der_parts(_der_parts(pub[1][1][1:])[0][1])
    n, e = [int.from_bytes(item[1], 'big') for item in numbers]
    return n, e, spki


def verify(path):
    """Verify this v2-only format; return True or raise ValueError on corruption.

    A valid signature establishes integrity, not trust in the APK publisher.
    """
    data = Path(path).read_bytes()
    try:
        eo, cd = _eocd(data)
        if data[cd - 16:cd] != MAGIC:
            raise ValueError('no APK signing block')
        size = struct.unpack_from('<Q', data, cd - 24)[0]
        start = cd - size - 8
        if start < 0 or struct.unpack_from('<Q', data, start)[0] != size:
            raise ValueError('invalid signing block size')
        off, values = start + 8, []
        while off < cd - 24:
            length = struct.unpack_from('<Q', data, off)[0]
            if length < 4 or off + 8 + length > cd - 24:
                raise ValueError('invalid signing pair')
            ident = struct.unpack_from('<I', data, off + 8)[0]
            if ident == V2:
                values.append(data[off + 12:off + 8 + length])
            off += 8 + length
        if off != cd - 24 or len(values) != 1:
            raise ValueError('missing or duplicate v2 signer block')
        end = bytearray(data[eo:])
        struct.pack_into('<I', end, 16, start)
        digest = content_digest((memoryview(data)[:start], memoryview(data)[cd:eo], end))
        wrappers = _parts(values[0])
        if len(wrappers) != 1:
            raise ValueError('invalid signers sequence')
        signers = _parts(wrappers[0])
        if not signers:
            raise ValueError('no signers')
        for signer in signers:
            signed, signatures, pub = _parts(signer)
            digests, certs, attrs = _parts(signed)
            cert = _parts(certs)[0]
            n, e, cert_pub = _cert_key(cert)
            if cert_pub != pub:
                raise ValueError('public key differs from certificate')
            sigs, digs = _parts(signatures), _parts(digests)
            if len(sigs) != 1 or len(digs) != 1 or sigs[0][:4] != u32(ALG) or digs[0][:4] != u32(ALG):
                raise ValueError('unsupported signature algorithm')
            if _parts(digs[0][4:]) != [digest]:
                raise ValueError('APK content digest mismatch')
            sig = _parts(sigs[0][4:])
            if len(sig) != 1:
                raise ValueError('invalid signature sequence')
            rsa_verify(signed, sig[0], n, e)
        return True
    except (IndexError, struct.error, OverflowError) as exc:
        raise ValueError('malformed APK signature: ' + str(exc)) from exc


def repack(src, dst, replace=None, add=None, sign=True):
    """Copy raw compressed members; reconstruct ZIP headers without descriptors.

    Reject ZIP64/encrypted archives. Output is atomically replaced after signing.
    """
    replace, add = dict(replace or {}), dict(add or {})
    central, output = [], io.BytesIO()
    with open(src, 'rb') as raw, zipfile.ZipFile(raw) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError('duplicate ZIP member names')
        if set(replace) - set(names) or set(add) & set(names) or set(add) & set(replace):
            raise ValueError('replace must exist and add must be new')
        items = archive.infolist() + [zipfile.ZipInfo(name) for name in add]
        for info in items:
            name = info.filename
            if re.match(r'^META-INF/(?:[^/]+\.(?:SF|RSA|EC|DSA)|MANIFEST\.MF)$', name, re.I):
                continue
            if info.flag_bits & 1 or info.compress_type not in (0, 8):
                raise ValueError('unsupported ZIP encryption/compression')
            method = info.compress_type
            content = add.get(name) if name in add else replace.get(name)
            if content is None:
                raw.seek(info.header_offset)
                header = raw.read(30)
                if header[:4] != b'PK\3\4':
                    raise ValueError('invalid local ZIP header')
                nl, el = struct.unpack_from('<HH', header, 26)
                raw.seek(nl + el, 1)
                compressed = raw.read(info.compress_size)
                crc, usize = info.CRC, info.file_size
                if len(compressed) != info.compress_size:
                    raise ValueError('truncated ZIP member')
            else:
                crc, usize = zlib.crc32(content), len(content)
                if method == 8:
                    compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
                    compressed = compressor.compress(content) + compressor.flush()
                else:
                    compressed = content
            encoded = name.encode('utf-8')
            offset = output.tell()
            align = 16384 if name.endswith('.so') else 4
            needs_alignment = method == 0 or name.endswith('.so')
            padding = (-(offset + 30 + len(encoded) + 6) % align) if needs_alignment else 0
            # Android zipalign extra: alignment (uint16), then padding bytes.
            extra = struct.pack('<HHH', 0xd935, padding + 2, align) + bytes(padding) if needs_alignment else b''
            dt = info.date_time
            dos_time = (dt[3] << 11) | (dt[4] << 5) | (dt[5] // 2)
            dos_date = ((dt[0] - 1980) << 9) | (dt[1] << 5) | dt[2]
            csize = len(compressed)
            if max(offset, csize, usize) >= 0xffffffff:
                raise ValueError('ZIP64 APKs are unsupported')
            output.write(struct.pack('<IHHHHHIIIHH', 0x04034b50, 20, 0x800, method, dos_time, dos_date,
                                     crc, csize, usize, len(encoded), len(extra)) + encoded + extra + compressed)
            central.append(struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, 0x314, 20, 0x800, method,
                                       dos_time, dos_date, crc, csize, usize, len(encoded), 0, len(info.comment),
                                       0, info.internal_attr, info.external_attr, offset) + encoded + info.comment)
        cd = output.tell()
        directory = b''.join(central)
        if len(central) >= 65535 or cd + len(directory) >= 0xffffffff:
            raise ValueError('ZIP64 APKs are unsupported')
        output.write(directory)
        output.write(struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(central), len(central), len(directory), cd, len(archive.comment)) + archive.comment)
    data = output.getvalue()
    if sign:
        data = sign_apk(data, signing_key())
    dst = Path(dst)
    fd, tmp = tempfile.mkstemp(prefix='.apk-', dir=str(dst.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        if sign:
            verify(tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
