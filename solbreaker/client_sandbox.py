"""Version-pinned sandbox controls and rendering on the prepared PE32 client."""
import base64
import hashlib
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

from . import client_patch, client_recon

INPUT_SHA256 = '2dc4ab995f04313d93b998b366067538b035d45a696b4ced046de356ca7b5a3d'
OUTPUT_SHA256 = '533573b98a7f2f865c4efe7c079f7a237455179a947a14c2615885076e0f434f'
VA = 0x4B1EA00
RAW = 0x466E600
SECTION_USED = 0x2A00
CALLS = (
    (0x1DBD5AC, 0x1DD0190, 'sandbox_refresh_score'),
    (0x1DBD5CA, 0x1DD0190, 'sandbox_refresh_score'),
    (0x1DBD5F0, 0x1DD0190, 'sandbox_refresh_score'),
    (0x1DC0417, 0x1DD0190, 'sandbox_refresh_score'),
    (0x1DCB148, 0x1DD0190, 'sandbox_refresh_score'),
    (0x1DCB169, 0x1DD0190, 'sandbox_refresh_score'),
    (0x4B1C441, 0x4B1CC42, 'sandbox_message'),
    (0x4B1C55B, 0x4B1CE1D, 'sandbox_reset'),
    (0x4B1C79C, 0x4B1D6CD, 'sandbox_frame'),
    (0x7BC3A3, 0xD54070, 'sandbox_draw'),
    (0xAB4D6A, 0xABA090, 'sandbox_mouse_hook'),
    (0x1C08FB0, 0x1C0C700, 'sandbox_binding_menu'),
    (0x1C33B6B, 0x1216C00, 'sandbox_binding_caption'),
    (0x1C1A825, 0x1216BE0, 'sandbox_binding_name'),
    (0x1C1A89E, 0x1216BE0, 'sandbox_binding_name'),
    (0x1C1A851, 0x123C830, 'sandbox_set_binding'),
    (0x1C1A939, 0x123C830, 'sandbox_set_binding'),
    (0x1C27475, 0x1232AB0, 'sandbox_reset_bindings'),
)
SLOTS = (
    (0x385E1B0, 0x1E98010, 'room_header_description'),
    (0x385E0A0, 0x1E98000, 'room_load_description'),
    (0x385E0D0, 0x1E8A240, 'room_get_description'),
    (0x3801FC4 + 0xEC, 0x183BC70, 'sandbox_move'),
    (0x3801FC4 + 0xF0, 0x1845DD0, 'sandbox_stop'),
)
PREVIOUS_ENTRIES = {
    'b7970803a7eecc465735bc4e7ae68323dc0d109e95ae40151261679dafab5c81': {
        'sandbox_message': 0x4b1ec0b,
        'sandbox_reset': 0x4b1eca2,
        'sandbox_frame': 0x4b1ecf9,
        'sandbox_draw': 0x4b1f2dc,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_move': 0x4b1f168,
        'sandbox_stop': 0x4b1f262,
    },
    'fbed408957de348ae4eac882d11b20916c67c7842682774a91470f5915485a5d': {
        'sandbox_message': 0x4b1ed94,
        'sandbox_reset': 0x4b1ee2b,
        'sandbox_frame': 0x4b1ee9c,
        'sandbox_draw': 0x4b1f499,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_move': 0x4b1f325,
        'sandbox_stop': 0x4b1f41f,
    },
    '1f09221f2c14db8e6484c7299d72612a4c2bc49dee2b373c35e0cc3bbd89b13c': {
        'sandbox_message': 0x4b1ee0e,
        'sandbox_reset': 0x4b1eea5,
        'sandbox_frame': 0x4b1ef16,
        'sandbox_draw': 0x4b1f513,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_move': 0x4b1f39f,
        'sandbox_stop': 0x4b1f499,
    },
    'b0d8f03c84bd857ef7638e240a1ce13d87387ce9b935ac144203da7ecae9750e': {
        'sandbox_message': 0x4b1ee0e,
        'sandbox_reset': 0x4b1eea5,
        'sandbox_frame': 0x4b1ef1c,
        'sandbox_draw': 0x4b1f549,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_move': 0x4b1f3d5,
        'sandbox_stop': 0x4b1f4cf,
    },
    '8ffc1ae8b1eb48a0002c752895fb837486f66874c85d7e17ad792615e34fd9a9': {
        'sandbox_message': 0x4b1ee0e,
        'sandbox_reset': 0x4b1eec1,
        'sandbox_frame': 0x4b1ef3e,
        'sandbox_draw': 0x4b1f57e,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_move': 0x4b1f40a,
        'sandbox_stop': 0x4b1f504,
    },
    '8cc056a117e07e3c0a35dd8520a9b4d8f7d2ee4e7d677ede22b76f4e9c78855b': {
        'sandbox_message': 0x4b1f094,
        'sandbox_reset': 0x4b1f147,
        'sandbox_frame': 0x4b1f1ca,
        'sandbox_draw': 0x4b1f8cc,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1ef4a,
        'sandbox_binding_caption': 0x4b1ef0f,
        'sandbox_binding_name': 0x4b1eed4,
        'sandbox_move': 0x4b1f758,
        'sandbox_stop': 0x4b1f852,
    },
    '25351797c142551b2c81a3ae16f8847a45e19212f7b960c38f6eb4159784de4b': {
        'sandbox_message': 0x4b1f0e6,
        'sandbox_reset': 0x4b1f199,
        'sandbox_frame': 0x4b1f21c,
        'sandbox_draw': 0x4b1f924,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1ef9c,
        'sandbox_binding_caption': 0x4b1ef0f,
        'sandbox_binding_name': 0x4b1eed4,
        'sandbox_set_binding': 0x4b1ef4a,
        'sandbox_move': 0x4b1f7b0,
        'sandbox_stop': 0x4b1f8aa,
    },
    '1f71dccda24e0343944cdcd11bee0ad5b50ad457ec5c61e2ffb0972e660ff4a3': {
        'sandbox_message': 0x4b1f3bc,
        'sandbox_reset': 0x4b1f46f,
        'sandbox_frame': 0x4b1f4f2,
        'sandbox_draw': 0x4b1fc13,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1f26d,
        'sandbox_binding_caption': 0x4b1f0fa,
        'sandbox_binding_name': 0x4b1f0bf,
        'sandbox_set_binding': 0x4b1f182,
        'sandbox_reset_bindings': 0x4b1f135,
        'sandbox_move': 0x4b1fa9f,
        'sandbox_stop': 0x4b1fb99,
    },
    '9367824443dd7116bfad201fe6c81044503e725808cbfed4340c2504ce7630dc': {
        'sandbox_message': 0x4b1f3bc,
        'sandbox_reset': 0x4b1f46f,
        'sandbox_frame': 0x4b1f4f2,
        'sandbox_draw': 0x4b1fc29,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1f26d,
        'sandbox_binding_caption': 0x4b1f0fa,
        'sandbox_binding_name': 0x4b1f0bf,
        'sandbox_set_binding': 0x4b1f182,
        'sandbox_reset_bindings': 0x4b1f135,
        'sandbox_move': 0x4b1fab5,
        'sandbox_stop': 0x4b1fbaf,
    },
    '30fe28798626af150dd0b81861c9888fdec91e2a22d39fda43ed2816b6040a6a': {
        'sandbox_message': 0x4b1f1bc,
        'sandbox_reset': 0x4b1f26f,
        'sandbox_frame': 0x4b1f2d2,
        'sandbox_draw': 0x4b1f9e9,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1f06d,
        'sandbox_binding_caption': 0x4b1eefa,
        'sandbox_binding_name': 0x4b1eebf,
        'sandbox_set_binding': 0x4b1ef82,
        'sandbox_reset_bindings': 0x4b1ef35,
        'sandbox_move': 0x4b1f875,
        'sandbox_stop': 0x4b1f96f,
    },
    '0b9d00ac43b3724f7062873070ec26e4bb22ab8d1cf0b887b07848de5dcfe1e1': {
        'sandbox_message': 0x4b1f21f,
        'sandbox_reset': 0x4b1f2d2,
        'sandbox_frame': 0x4b1f335,
        'sandbox_draw': 0x4b1fa4c,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1f0d0,
        'sandbox_binding_caption': 0x4b1ef5d,
        'sandbox_binding_name': 0x4b1ef22,
        'sandbox_set_binding': 0x4b1efe5,
        'sandbox_reset_bindings': 0x4b1ef98,
        'room_load_description': 0x4b1fd3f,
        'room_get_description': 0x4b1fed6,
        'sandbox_move': 0x4b1f8d8,
        'sandbox_stop': 0x4b1f9d2,
    },
    'db1b35c8deb8f36780ac04a08b2537d8bd6e9a41da1eb38b80adc467dfc05d13': {
        'sandbox_refresh_score': 0x4b1f29a,
        'sandbox_message': 0x4b1f319,
        'sandbox_reset': 0x4b1f452,
        'sandbox_frame': 0x4b1f4c3,
        'sandbox_draw': 0x4b1fbda,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1f14b,
        'sandbox_binding_caption': 0x4b1efd8,
        'sandbox_binding_name': 0x4b1ef9d,
        'sandbox_set_binding': 0x4b1f060,
        'sandbox_reset_bindings': 0x4b1f013,
        'room_load_description': 0x4b1fecd,
        'room_get_description': 0x4b20064,
        'sandbox_move': 0x4b1fa66,
        'sandbox_stop': 0x4b1fb60,
    },
}
CFLAGS = ('-m32', '-Os', '-std=c11', '-ffreestanding', '-fno-builtin', '-fpie',
          '-fno-stack-protector', '-fno-asynchronous-unwind-tables', '-fno-unwind-tables',
          '-fno-jump-tables', '-fno-ident', '-msse2', '-mfpmath=sse',
          '-mpreferred-stack-boundary=2', '-Wall', '-Wextra', '-Werror')


def build_blob():
    """Rebuild the position-independent code and its embedded constants."""
    source = Path(__file__).with_name('native')/'sandbox_client.c'
    with tempfile.TemporaryDirectory(prefix='solbreaker-sandbox-build-') as temporary:
        work = Path(temporary)
        obj, script, elf, raw = (work/name for name in ('client.o', 'layout.ld', 'client.elf', 'code.bin'))
        subprocess.run(['gcc', *CFLAGS, '-c', str(source), '-o', str(obj)], check=True)
        listing = subprocess.check_output(['objdump', '-r', str(obj)], text=True)
        absolute = [line.split() for line in listing.splitlines() if 'R_386_32 ' in line]
        if absolute != [['00000007', 'R_386_32', '.text']]:
            raise ValueError('Unexpected absolute sandbox relocation')
        script.write_text(f'SECTIONS {{ . = {VA:#x}; .text : {{ *(.text*) *(.rodata*) *(.got*) }} '
                          '/DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) } }\n')
        subprocess.run(['ld', '-m', 'elf_i386', '-nostdlib', '-static', '--build-id=none',
                        '-T', str(script), '-o', str(elf), str(obj)], check=True)
        headers = subprocess.check_output(['objdump', '-h', str(elf)], text=True)
        sections = [line.split()[1] for line in headers.splitlines() if line.strip()[:1].isdigit()]
        if sections != ['.text']:
            raise ValueError('Unexpected sandbox executable sections')
        symbols = {name: int(address, 16) for address, _, name in
                   (line.split() for line in subprocess.check_output(['nm', str(elf)], text=True).splitlines())}
        subprocess.run(['objcopy', '-O', 'binary', '-j', '.text', str(elf), str(raw)], check=True)
        return raw.read_bytes(), {name: symbols[name] for _, _, name in (*CALLS, *SLOTS)}


def recognizes(current, recorded):
    versions = (INPUT_SHA256, *PREVIOUS_ENTRIES, OUTPUT_SHA256)
    return current in versions[1:] and recorded in versions[:versions.index(current)+1]


def patch(data, blob=None, entries=None):
    digest = hashlib.sha256(data).hexdigest()
    if digest not in PREVIOUS_ENTRIES and (digest != INPUT_SHA256 or len(data) != RAW):
        raise ValueError('Sandbox requires the pinned ATGM client')
    previous_entries = PREVIOUS_ENTRIES.get(digest, {})
    code = BLOB if blob is None else blob
    symbols = ENTRIES if entries is None else entries
    if (not code or len(code) > 0x3000 or set(symbols) != {name for _, _, name in (*CALLS, *SLOTS)}
            or any(not VA <= value < VA+len(code) for value in symbols.values())):
        raise ValueError('Invalid sandbox code layout')
    image = bytearray(data[:RAW])
    sections = [struct.unpack_from('<8sIIIIIIHHI', data, 0x2D8+i*40) for i in range(7)]
    for address, target, name in CALLS:
        rva = address-client_recon.BASE
        offset = client_recon._offset(sections, rva, 5)
        target = previous_entries.get(name, target)
        if image[offset:offset+5] != b'\xe8'+struct.pack('<i', target-address-5):
            raise ValueError('Sandbox call site differs from the pinned client')
        image[offset:offset+5] = b'\xe8'+struct.pack('<i', symbols[name]-address-5)
    for address, target, name in SLOTS:
        rva = address-client_recon.BASE
        offset = client_recon._offset(sections, rva, 4)
        target = previous_entries.get(name, target)
        if (struct.unpack_from('<I', data, offset)[0] != target
                or not client_recon._relocated(data, sections, [(rva, rva+4)])):
            raise ValueError('Sandbox vtable slot differs from the pinned client')
        struct.pack_into('<I', image, offset, symbols[name])
    virtual_size = SECTION_USED+len(code)
    raw_size = (virtual_size+511)&~511
    image_size = (client_recon.MKRAY_RVA+virtual_size+4095)&~4095
    struct.pack_into('<I', image, 0x3D0, virtual_size)
    struct.pack_into('<I', image, 0x3D8, raw_size)
    struct.pack_into('<I', image, 0x230, image_size)
    struct.pack_into('<I', image, 0x1FC, 0x32F0200+raw_size-SECTION_USED)
    image.extend(code.ljust(raw_size-SECTION_USED, b'\0'))
    if OUTPUT_SHA256 and hashlib.sha256(image).hexdigest() != OUTPUT_SHA256:
        raise ValueError('Unexpected sandbox executable')
    return image


def upgrade(lab, manifest, current):
    recorded = manifest.get('client_exe_sha256')
    if current == recorded and current != INPUT_SHA256 and current not in PREVIOUS_ENTRIES:
        return manifest
    if (manifest.get('exe_sha256') != client_patch.BUILD_SHA256
            or not (current == recorded == INPUT_SHA256 or recognizes(current, recorded))):
        raise ValueError('Sandbox requires the recorded prepared client')
    target = lab/'client/wotblitz.exe'
    if target.resolve(strict=True) != target or target.stat().st_nlink != 1:
        raise ValueError('Sandbox requires a private executable')
    if current != OUTPUT_SHA256:
        client_patch._write_client(target, patch(target.read_bytes()))
    updated = dict(manifest, client_exe_sha256=OUTPUT_SHA256)
    client_patch._write_manifest(lab/'manifest.json', updated)
    return updated


# Generated from sandbox_client.c by build_blob() with GCC 14.2.0.
ENTRIES = {
    'sandbox_refresh_score': 0x4b1f45b,
    'sandbox_message': 0x4b1f4da,
    'sandbox_reset': 0x4b1f613,
    'sandbox_frame': 0x4b1f684,
    'sandbox_draw': 0x4b1fd9b,
    'sandbox_mouse_hook': 0x4b1ea0c,
    'sandbox_binding_menu': 0x4b1f30c,
    'sandbox_binding_caption': 0x4b1f199,
    'sandbox_binding_name': 0x4b1f15e,
    'sandbox_set_binding': 0x4b1f221,
    'sandbox_reset_bindings': 0x4b1f1d4,
    'room_header_description': 0x4b2013e,
    'room_load_description': 0x4b2008e,
    'room_get_description': 0x4b201dd,
    'sandbox_move': 0x4b1fc27,
    'sandbox_stop': 0x4b1fd21,
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNqVOA1wU1W6N01CAwZu0IJdKVIe3Z3CgjTCrkRAQ8Ol1RINhNBCGwRpgJbS1uZeCgPFltsAh+MVtFCZeerj7WOG'
    b'jrIz8AaR5fkTWrYpu4UXoGqU6hZf1YuNGihbyp/3fd9JCl2fM+5Lp/ee853vfOf7/75zVQ5+BVOM3x4xtCzQJLMm'
    b'mVRV0zSXmjua4+RT5iWXS47duOUhX7lJi3H32vE6LRwIimalwf7cQY6cLPRqX1qDhd4WD58frGu2VI/Xka/y3UqN'
    b'ibQogipHLfxIQdUec2nneYugkgg/Uq/4+gufgx2AqH6VynF1oT1pHLeYtBqz2ibo3HK0iHj2E+cBIrweOC0lWy8S'
    b'74GeZcr2JuCrlFtTCxyX6ku5Ut0aGNW6tEb7lXI9aZH7NfGC9bQiHCfO46WcSxHaSie73FrjHlwW9sEovbdcTz37'
    b'AufFt+Sa45OlA3U1be7sPKv0r8p29UdNg6OEfcqmadS5n1wFQvnqr+FMQOWWSqsVx9MhoYkzcBwRDlmDcs0BGBwO'
    b'CUeHAhviGLro83YlW5P7a6p5OvfzsNzPVw8hnkOAowhN1LnPtbYlVdcmR1N5C0ddKdRuIYsyUsjcDFCRIQOOpMLr'
    b'+VrYId8y8IFqOFfxXStcNqCo9x5ERa0dA4oCDZnovpj2giFwUSqt/VIn+koNpHUNl8Vxa/agdjhtd8ZV1AmY6ill'
    b'u+c2GFRrfCkG0r8cRG00K9uXI3Ah8USJM6ado54odcaIY4ay/QW2oJ0jjlnF/54B5IievImnAT8kcpejxeSs202f'
    b'Bju3Cr0anZMVCIKtgpf/A3ZQjnyiNLguvaG3haUepeHwa7VJNpdd6lI8vfLJzYrMddYmLSzVlY7TwrU1veBH0p+B'
    b'1ZOUUxqOT30Td71T1+L6emuSrcAgvVX6G+0COQkI+0PconnuKSG7Yf6c7CmtdpOb5ljInKE0J8VOclLInOHkhZHy'
    b'1xZRT6szqWCiHjN5YVTrnF/ryJxfUcFCFowlc8Yf1wGLd5VrPQ2+vTNtWP1FMXnO40UmTbxqDdZfLPTye4JMUPLJ'
    b'YvVxCIa61u9+xXEQBmBCeUMKvz6JptbO5EJCDOlJyTDQ0GrbPgQFWs+HhChOidClbGsFCPF0E6GTerpIRN0OCyCO'
    b'T3EMURp34qKgqlVxoBEYcsaooCqNr8GKWhgHI/kkVC0u/AEXsu8t6GFhp74eFDTLK+odPb0w3OuonZku3uxRqKcT'
    b'jkyJYz+gOJKVxrdxfxLjrrunVWk8iyxE1NiPDOdxJQ/YOoawNuJU1U8BTJ3q9QgRovwHOsUVrr05tHqXXBOd9SJZ'
    b'64kOm6PMTQrDYs8lKsSUxo9wZzIVukmOgQpRkmOyBkmOmYCpIupL8TOe4D8A2ZFScvWjtZuSH5cm78wzKQsYYHzt'
    b'pqFeaYysZsktw2An4P4IEyAAIkkgX7/S2BlneXac3AggkS4a5ZbkHr3cYpJPpQ7yVDAgaXWrVSlgw5aJkHAgfkDF'
    b'KncLSFwhN5V9HGfnjBl/O2IAIUAEKkXd5KL2MVBgREL2yZgmraet50luFjm3Pde+3TW3RU1Dis2fQGTCMb28xWuC'
    b'h8cMD6eFz2zhM7/s5Zfgf0svX3CKz7wEw0s47OUXBPmpWzvAAutH9PLLIvAfhv+2nhF8/kXYvzMMS3zmhV5+ZKoX'
    b'Hi4DPHJNicD7mDlhJt1aj9EpdNCtO9ggcnwRuCJtfAn9xJwUAEdqF61KTad21FgLilIaagvrAC+cr52nznaloXJs'
    b'mIOx0pB+sIQjN7TzBVQIL52NVKTk4wXw6jkyOAstVq0PYCB0j04Egot4mpRt3Sy/NGB+CQT5+v/B8Gq8AsCFAGX5'
    b'RjiEC3/BhW1rBqXxpIE0boc0CcgskwtH7Xz9mwAMCSw7KzXtfxqG+g/2vv3hNBjkU2cTpvnPiHBAlW9oWh1kaAPG'
    b'xoElUgr1vEf23JmOTusJwmgGjHJk7TGpDcznuVcZNOmo1tABPJMQeNAYq8ZKwgMMXBsXZW+QPxYkIQbC2hHQpOGa'
    b'dFhrWAPrVq1nlLKNFaWhLiocwgWsjV9gnuwf0NliKpncat9I9L0/jgL5552R+w3r71e39oP3SaZCYxcU1WVerWtt'
    b'MFWp12MZKlw2aOt/sa1Vg7fmDGzlyga2ZuHWO3e3suys1rKtj8FWNaWf+XqL8fCkCVDCIXImAsQWkx4iIdIMKSei'
    b'liHKzXjcXB7B2BgwO5RzN/lRzWL0vknBCEqnyfTFNOLsgqSmHruOqSSi7MMCToVOWZ0m9+vXjyZb0mBGhlBn193i'
    b'oXXVPYnGkl6kOYZcLJfr5RdNfHWlAglD6NCkTsCmnoj6FhDN5y3zDFgysULOS6GLU7FeYt2M40FdCBWAVWZAXqVC'
    b'RM2/g+lAmqhJHb+488OkeBmI26o5nWOiutWnLSilDmM7mg4hQJRO5ksW6kyh0Nfs7cCpZCZKBAbqiT6YxWEMQALd'
    b'8DRyoaCu7ibrFgJdAJDul2/opeF0meG3D9fdUuHo9eMJEGSLBIizUwiQrTElQk7rooyWugWOIM3gWpY44E9YcNT3'
    b'ABoKXIM5xgT51BoMBfphNh1mJ28khQK3YfI7DKMAB076ezYyafFfKGBIjGxtyl4kIvJ0NE3ZSvYEzAC193yjzH0o'
    b'lexVmUhRzB7CNRLAEqEEEFiqX4PSufLjnMcF0cIJ3pstqE7mhTdHoD6P3A8sv/z3hBfu1n0OXngC5QDPG3OdJfGn'
    b'SHO84bAlSbPlD1y4KprIf2I667HY2FvCWrIVR/IJzCCc+IB84iU2GIb+5b386c1BUcBaJegDvGZaY6FSCoHk7kwN'
    b'XOTr0QKKw2ILSWV1M6GpsErFdZsM8xbOyZEKoQQw1tStCcXz9RdhOsAcHziDutyK5yMadZgSPBCWkanDTFhG5usP'
    b'sIodU+cCU5dfY0eabKGq8sSRq+BId/azBZIXkCA7OaMq38d0UV5MR7HTwOui0nJ5g4kTlypus+0TyW07K94X6BPz'
    b'lMUG2zlpLnWboKGdTVyWEzzuOCsa6Wj5y3FWjbg2kw3pPf9CQHYQvCZGQAPOVGN2O1Q68GSvuXBZ3NGWsp5oyUDE'
    b'u6nXBJcBtyKPPXvEoIWBtYgqgfEKlwJvT9/lTXoSVnajHshuVIB6HnIIJJPcFJJrIblmkmsguSaSm0lyp5PcdJKb'
    b'RnJTSe5mkruB7NiNuvHePt2ihs0cZ9wFYZevbM4wyd0xrfr2gPnKoDBgqoHSfNR45uMjhnzMxuGCpQGNr19iBBLV'
    b'lkAfX/8MDgHl1LhavW0IH3gC588YAqf5+mmJpYLKF/U2HR+YAHN5YxpHnIf4+tHxxbi9X70G9m5Be2uGu/bW84Er'
    b'WFych2x5Jslo8xtEG4nAZrX7OjZGcBcgfgNxpBCHhYDpHZnEMZ040okjjThSiWMzcWwgMkpL8kx0+ywddhIkzwya'
    b'fIjWHFVei/ywVQ+6HE7t3QAbQu1Awww8abuv9UGZgaIVAXgRdc8CO3sUZ2QB3KbQDgwnC+rSTCEizZI3zeDE36Og'
    b'y41vc6wUZcrHD8O5SdK42uMHWZCMZntuQ/mSN1k4EhInXN7KSmvIkToOWXRsuJupldX9z13mUEZHOiYfcnNic93N'
    b'S6ALftvfoOUMaMxreCEkR80gkiM9JEQ45vGRkNDJRp7OkNDFQmNRhgEKNiRjoQsyMUTlUfWdXrhcnTJfflWPQTOD'
    b'CPsRuvsKFpND7DrRd/kOo3doJlOgmEqaASWR/wAPVuKqRbFjuoTYbTS3MnAarhM6zZUOPQKA3gGEVSGhHUTtM7rG'
    b'vgpd0OtgN2EPFQ7zx+qHRKBwQF/Q7qLOA3APg2sJKNPM5CNXLnQNFfbMk380SOesQWsfEZp2OpvY2gUof/smODaL'
    b'LrmmyXAiGXkcQoUmxTVtIXiGIkSo56jaFcMWJYLpcon0G4q3gD02T5c0Bhh78P+caqTCvqGtmtAEJI3SXupIneDf'
    b'TPyp18PjY0060SD703RNSeyd1GRgb0uTCd55G9KbLOxtp+B7rfLXesB7uDSdCvu18KpAcKNePmsoHTl4aupzpFn4'
    b'+vUgjLwpEzJWGVa8/t86MmZ7YcRvL8KO0jEZrGGn7kU0rwBsjb1XSgaZn2Gg7iIwrEm9cg1tcRxaL7AncVeC0zrK'
    b'qBAk7iziEEOOyTrmZNsd9l5+UhvoHHrc/V1QL+C9L8LeS9qxZZ7612oeYLu7Wf/b3Ms/dxK3zYVtYT7zr4DWwU+9'
    b'UH1/nMS+brb1uc/YksVdycBF52C8o5NRuMxPPcco7lDjc8D+FqZ5IuKXMWHaB5zV2Smfhcb9UzgFOvKCz+ANXflI'
    b'LxgR5kV/gTH0OwnJ4SKHkl/vRbJn4GCQHDacAbq1YcZW0Um8CbRjsw8LbfAvdACJtZXwyCuDh0PseZDpVkyG/NqI'
    b'lPKDfKbKZ7bd599wvbUSC5450Cfx8qbpnFjek3z8MayFxuNvIbvudMgHJnJj4jnbyaoFiWgkV+LxCNEIJ6wLUw+K'
    b'4GynAjAyUugYFIcdA3E4JcbiEPi4T/763zCdTY/ngYEskCim6tRkLOVfD4s3gFBXayxwQaRSKtwjqWBmtyeosdCK'
    b'peFdV4Dgi1AhA9o6teE7vBRD/VWn/MAq3BrSMpBgpSK41c50GKRFUEqps5vaM206KQfujpSzOTKZdjZcRQ+L0nwD'
    b'hSSVSZOJez5x5NIcE3G46BC6GDLwIuIuoPMAAPRi8SxHHBnU00GdESKlUgkYi0ExJJ4MWhMleFXoNFa+J+uo1E2c'
    b'6aTGDNdcaKyJJ00+lR6vjAnJfzcEJY8OZZdHEBVac7ylAy0hqr4bZbK1qAu/Z6Jl3iuQY6knNtNlkEbREbbcTGmE'
    b'9by8PO0H4spUD2Kag8JcE1XksLdeB+fiF5ga89LCeyerfzbiZx9xaPyzzwzst2+pOfHzThor7R/htQ+v6d3qR9+x'
    b'wx8cpNf7INnaoJY4u6G7c2KHfK+19E/GsnlfEubOLnoYO10E3MR4H6K8MuuPfr0tLI5QXqn8DMpTmA98ASvg0Otn'
    b'wGPLLGXxDGXeXOJ5D7xK6kA4hPbImjAuzkVYO8JyEdaGsPngEAg+hZCg9goa525CAfdRBHUgsvKK4GVS34ihxbsh'
    b'FYFv8u+2nVQteL1WKZ45CULKUrgIHuui8DgYZRE36TTNywqchkiDYwow4nZ1xJPM9hh7ezEkd12LJ5xziTgvBC6X'
    b'QGJY4oxB0H6GiaEgHvpLHEWw7p4PD4cL1j6CwWv9ieCWINxHFi6Hh7u4B5Jd98S2AS4nwdGTkCIq4WA8GxScQXZu'
    b'x9m4hCkIthZhrgG9FHyKgOJ47mJEl4BOCzCR5a2JZxI4JSEXyrC2DJcqWchjJgEky0pRrumGcix0A1IP/EPCKugi'
    b'jizKVP8Q/Gfjd4SFJnhkwv/y+FcF+eVc8AJdT5K1L+4kUET7RAdQZIatCScQAJkZH007csssXGpDmB1haFc0vKUm'
    b'iDA0/Jb59z6ynSdfQb5QrT3MR0dR12TIXSOoxxzTuexwt4LX3IFvDGeS8BtDLUQcuHwWcZ4iveqiy3Dv8LSRFqSy'
    b'Kop9V5vSeAc/AoWocEpN+R5BkOVOKY3DoZaregYIM8DDCLj6HQI6GMCKgEsMEGGAbAAoUqd6BmAQjvg15Eq+FqbO'
    b'U4rM1V7W1bXgF5LFcHG+ol0YuOIY4jdGdSzjN2wczO8BdRC/x3oS/LrvDPBb/N099p5HbtyD2BMRkD2IvfoEe4/8'
    b'DHuWX2BPeX86JFy36tdhBpuE+STabsFrbRbA1Vvf4MUVhxqktFaj/Q8q3MyUhj145DZ21I2FcFQAUWiuIV/ZVtt9'
    b'BvoscoMwmLKNfTwKZCKJMN2LsMB5vv571pWZ6jZYOAs2El/g7ef9+WxH7PC7HDkHEsjvLwcAT96fDK/1ZhrAdRLA'
    b'GexlX1mNWGe4+FlxLpSGI0xB37K7/ia6twiB+Ol9S/U6a1B5fxXM7TtfSg0B2g5zstxvlmIAV/xI4g0kvjNNvyNN'
    b'Z7c1Sz/AlV7JNtM3GP1nLRMvKHtxv+4k+W9bSBxm3zlf37vDnNRzVdm7GuD51GkCAZ4p5ZhWUGpFmR+XXTDd0z01'
    b'ZLTQFPhPg//p8D8ro0Xy+6qmPfpIcVkZl+MT5/g3lq/M8210iytEHwLyS8qLK6oXranyrSh2VVWs9Pn9TxVzvrKS'
    b'dSXlK8SSinIv55cqq3zrVqzc6OVWV61YXyLCQCpHDNFX7OWKfatWSGWilytcV1HsS+cKy0rW+/zwFkvW4fT5ChFn'
    b'5RWiz+/l1vqqyn1lCYacFet980rKfMKGOVxVRcW6p8pLxJIVZU6gM3g+Hwn+A0JJuST+IygbTrH+FPAoA+SCaL6q'
    b'u0QT00Ek4pB7FO7NH+XcPlEsKV/tnzpp0tT5FStXlC0EhGcrUTN+zl1R9jwobm0ceSq3ajf+7S5EIlNWVVSt86Zj'
    b'j2dKXlvKDfoxOlPdz2dXVPhFmA/HCw338z9+EP68spLVa2DDiH8S31ni94N2GeyfwV+0omq17/9B/2lpHXaLnPkX'
    b'8H/6A7UNiM5+pgQewAdEZL+h9+ADorDfsHvwAZZ/ij/AGvsl/4SPRx4pKq5YKa3zlYv+Iv9dG05Z69v4PISD/xFx'
    b'0Oaf+f3ifniCL8AVsfbJivvlx6dNm/bkuMDaJ6fXPjYTxh/C+ENYy9nV/PfZHLf/yaJdzU+caW+fNfiM/wW48g+6'
))
