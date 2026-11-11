"""Version-pinned private Windows profile redirect for the owned client copy.

GetUserDocumentsPath returns lab/documents/ using the executable location.
The original DAVA setter appends DAVAProject/. The thunk is position-independent
and uses existing Unicode WinAPI imports. Its assembly is native/windows_profile.S.
"""
import hashlib
import struct

INPUT_SHA256 = 'a8cd7bd4e1c70e25f43f8a0d5b3c645325e0590cb255211212f307fbfdee039d'
OUTPUT_SHA256 = 'd0334eeb1cf0900da470a52a63663a38b689f93de87cc78692e3148b7134dd8f'
WINDOW_RVA = 0x5b96c0
WINDOW_SIZE = 0x2a9
WINDOW_SHA256 = '2831936b7c5810fd01761cc984570695d076c6a7ce0681d198ce88a36c231b4a'
BLOB = bytes.fromhex('5589e581ec20060000535657e8000000005b81ebd1965b0068040100008d85e0fdffff506a00ff9368f32e0385c00f84b20000003d040100000f83a70000008db5e0fdffff8d3c46ba0200000039f70f869100000083ef0266833f5c740666833f2f75e94a75e683c7028db3b2975b00b90b000000fc66f3a56a006a0068000400008d85e0f9ffff506aff8d85e0fdffff50688000000068e9fd0000ff93f8f22e0385c0744048508d85e0f9ffff508b4d0831d2891189510489510889510c895110c741140f000000c74118ffffffff8d83c0155c00ffd08b45085f5e5b81c4200600005dc36a746affff9364f12e030f0b64006f00630075006d0065006e00740073005c000000')
METADATA = {'version': 'windows-private-profile-v1', 'relative_path': 'documents/DAVAProject'}


def recognizes(current, recorded):
    return current == OUTPUT_SHA256 and recorded in (INPUT_SHA256, OUTPUT_SHA256)


def patch(data):
    if hashlib.sha256(data).hexdigest() != INPUT_SHA256:
        raise ValueError('Private Windows profile requires the pinned recon client')
    result = bytearray(data)
    header = struct.unpack_from('<I', data, 0x3c)[0]
    optional = header + 24
    count = struct.unpack_from('<H', data, header+6)[0]
    optional_size = struct.unpack_from('<H', data, header+20)[0]
    sections = tuple(struct.unpack_from('<IIII', data, optional+optional_size+40*i+8)
                     for i in range(count))

    def offset(rva):
        for _, start, size, raw in sections:
            if start <= rva < start+size:
                return raw+rva-start
        raise ValueError('Unmapped private profile RVA')

    start = offset(WINDOW_RVA)
    if hashlib.sha256(data[start:start+WINDOW_SIZE]).hexdigest() != WINDOW_SHA256:
        raise ValueError('Private profile instruction window differs')
    result[start:start+WINDOW_SIZE] = BLOB + b'\x90' * (WINDOW_SIZE-len(BLOB))
    # Existing HIGHLOW entries refer to operands in the replaced routine.
    # Mark them ABSOLUTE so ASLR never rewrites the PIC thunk or padding.
    rva, size = struct.unpack_from('<II', data, optional+96+5*8)
    cursor, end = offset(rva), offset(rva)+size
    while cursor < end:
        page, block_size = struct.unpack_from('<II', data, cursor)
        if block_size < 8 or block_size % 2 or cursor+block_size > end:
            raise ValueError('Invalid pinned relocation block')
        for at in range(cursor+8, cursor+block_size, 2):
            entry = struct.unpack_from('<H', data, at)[0]
            if WINDOW_RVA <= page+(entry & 0xfff) < WINDOW_RVA+WINDOW_SIZE:
                struct.pack_into('<H', result, at, entry & 0xfff)
        cursor += block_size
    if OUTPUT_SHA256 and hashlib.sha256(result).hexdigest() != OUTPUT_SHA256:
        raise ValueError('Unexpected private Windows profile image')
    return bytes(result)
