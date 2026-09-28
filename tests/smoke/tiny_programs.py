"""Tiny test programs for sideloading, built from bytes: no compiler needed.

- elf_arm64(), elf_x86_64(): static Linux executables that sleep for a few
  seconds (nanosleep) and exit 0, so a launch can be seen in `ps`.
- pe_x86_64(): a Windows x86-64 .exe with no imports whose entry point
  returns 0 straight away, which ends the process (Windows and Wine both
  exit when the main thread returns).

Each is a single loadable segment or section; they are what the headset
smoke test and the fake Frame's e2e tests install. Python stdlib only.
"""
import struct

SLEEP_SECONDS = 10
BASE = 0x400000


def _elf(machine, code):
    """ELF64 little-endian ET_EXEC with one PT_LOAD (R+X) covering the whole file."""
    ehsize, phsize = 64, 56
    entry = BASE + ehsize + phsize
    size = ehsize + phsize + len(code)
    ident = b'\x7fELF' + bytes([2, 1, 1, 0]) + b'\0' * 8
    header = ident + struct.pack('<HHIQQQIHHHHHH', 2, machine, 1, entry, ehsize, 0, 0,
                                 ehsize, phsize, 1, 0, 0, 0)
    phdr = struct.pack('<IIQQQQQQ', 1, 5, 0, BASE, BASE, size, size, 0x1000)
    return header + phdr + code


def elf_arm64(seconds=SLEEP_SECONDS):
    words = [
        0x10000100,                 # adr  x0, timespec (32 bytes ahead)
        0xD2800001,                 # mov  x1, #0
        0xD2800CA8,                 # mov  x8, #101        (nanosleep)
        0xD4000001,                 # svc  #0
        0xD2800000,                 # mov  x0, #0
        0xD2800BA8,                 # mov  x8, #93         (exit)
        0xD4000001,                 # svc  #0
        0xD503201F,                 # nop  (pads the timespec to offset 32)
    ]
    return _elf(0xB7, struct.pack('<8I', *words) + struct.pack('<qq', seconds, 0))


def elf_x86_64(seconds=SLEEP_SECONDS):
    code = (b'\x48\x8d\x3d\x12\x00\x00\x00'   # lea  rdi, [rip+18] (the timespec)
            b'\x31\xf6'                       # xor  esi, esi
            b'\xb8\x23\x00\x00\x00'           # mov  eax, 35       (nanosleep)
            b'\x0f\x05'                       # syscall
            b'\x31\xff'                       # xor  edi, edi
            b'\xb8\x3c\x00\x00\x00'           # mov  eax, 60       (exit)
            b'\x0f\x05')                      # syscall
    assert len(code) == 25
    return _elf(0x3E, code + struct.pack('<qq', seconds, 0))


def pe_x86_64():
    """PE32+ console program: one .text section holding `xor eax, eax; ret`."""
    file_align, sect_align, image_base = 0x200, 0x1000, 0x140000000
    dos = b'MZ' + b'\0' * 0x3A + struct.pack('<I', 0x40)
    coff = b'PE\0\0' + struct.pack('<HHIIIHH', 0x8664, 1, 0, 0, 0, 240, 0x0022)
    opt = struct.pack('<HBBIIIII', 0x20B, 14, 0, file_align, 0, 0, 0x1000, 0x1000)
    opt += struct.pack('<QIIHHHHHHIIIIHHQQQQII', image_base, sect_align, file_align,
                       6, 0, 0, 0, 6, 0, 0,          # OS, image and subsystem versions, Win32VersionValue
                       0x2000, file_align, 0,        # SizeOfImage, SizeOfHeaders, CheckSum
                       3, 0x8100,                    # console subsystem; NX compatible, terminal-server aware
                       0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    opt += b'\0' * (16 * 8)                          # no data directories: no imports, no relocations
    assert len(opt) == 240
    text = b'.text\0\0\0' + struct.pack('<IIIIIIHHI', 3, 0x1000, file_align, file_align, 0, 0, 0, 0, 0x60000020)
    headers = (dos + coff + opt + text).ljust(file_align, b'\0')
    return headers + b'\x31\xc0\xc3'.ljust(file_align, b'\xcc')


PROGRAMS = {                        # kind -> (file name, builder)
    'arm64': ('fc-smoke-arm64', elf_arm64),
    'x86_64': ('fc-smoke-x86_64', elf_x86_64),
    'exe': ('fc-smoke-exe.exe', pe_x86_64),
}


def write(folder, kind):
    """Write one program into folder; returns its path."""
    import os
    name, build = PROGRAMS[kind]
    path = os.path.join(folder, name)
    with open(path, 'wb') as f:
        f.write(build())
    os.chmod(path, 0o755)
    return path
