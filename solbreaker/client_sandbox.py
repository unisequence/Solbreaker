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
OUTPUT_SHA256 = '30fe28798626af150dd0b81861c9888fdec91e2a22d39fda43ed2816b6040a6a'
VA = 0x4B1EA00
RAW = 0x466E600
SECTION_USED = 0x2A00
CALLS = (
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
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNqVWA1QFFee72F6cFxHe3DRsAmuuLJbxJgIpxudk42jbSsJzm7rMAICaiJEBCJEpkVLJ4LNyL5924mJH6Fqszn3'
    b'1qpQWbbK1Lk5NpWqDIzn4AZToOztxJANGuK1C3UZgyH4Eft+r4dRKveRvanq1+/933v/9//8vX+PzuFX9Kjtb2/x'
    b'4fWG4jAUu64bhiHrx2dynHrGUXxtx9s3b/vIZ14Sth2unmcxeoMhv0M74t7yBkc6S8qMKzmhkrKwTygMNXU5G+ZZ'
    b'yGeFXi1gJ2FN0tURp5Ai6cZS2bggOCWdRIUUq1YxXrIFO7BQr8QpTZEbqRy3kZy1ZXfPt3jVkVLiO0E8J4n0WvCc'
    b'MiXnI1J2cniz1tIGuaq4ykZIXGWt4qosleg1ysZx9/WdVhJWxw3/xZxzmtRBPB1VnKxJ3VULZa9x/CibllrRyxjd'
    b'aaW+1uAF/5tqoGOhcrIp0O1dlZ+j/Epr0e8aBo6SWrV9i6nnBPkCjAr1H+JMLOU2Kds18amI1MbxHEek9pyQGjiJ'
    b'zqmIdHoqxPA/RAs+7tFWGep4oEGgqz/uVceFhmTia8caTWqjnla5Opxm6VZH0gQnR+VU6naSgsxUsjoTJuIzcSSV'
    b'Xis0ekX1Ni8EG3CuVnGjZHPCUCtSmKHe/y4MBQvZaWvMeJ4PfqRUNV6x+CuqeHK2ksvmuMpmZh3OOJz5BbMJXPWk'
    b'1rLnazjUOP7LGLR/McSs0aW1qIy4gfhGiCdm9FHfCPXEiLhMazlqThh9RMwt/00m2BEreZ2dBnlI9J5EG8kHXi99'
    b'Cn4+K40adGV2MARfha79FjsoR/6iHZEv/9rq6lWGtSOnXm1McsluZVDzjaqd+zWVG2hM2lBlqZpr9DYGRhFHyr9B'
    b'1E7KaUc6Fr3Odv2hKSxfPZjkKuKVN6t+ZFwknVhwIsIVrPE+GnHz61auevSs2+6la51k5VS6NtVN1qaSldPJ8ynq'
    b'VaffShuyqGSnPgd5ftbZlT+0kJXfo5KTrJ9DVs7rsEDEhHEj7oUsBXLO5VwgedmkryXP3SKvDuthAfbuKkF4QtlR'
    b'wVlmR+NzoPE4haywkHVlVChmT3hUKDojZF1G9zLrjgrrQ8Kig/1w4O4Zo8LmKJ5ePN3DM4TCj7D/F72YErIujgop'
    b'aWVoZB5Nnj1uVN3Lzj37KbyNhFNHZOJr0w793nTgEebAYEho/pTJf/wdEDeAajpUamcTf2IThyon5UlSIk/ciEMs'
    b'NlNFOu0Wml8HMSKZ4a8Fev74HWaE0Ojv3luMTiH1tLE8ukSkk/oP7hhGE1IAgU+lk8VKKvW9S45+vQT5Qn0h9Jah'
    b't1Y1lirdsKHvfuoZymnjSD9kJhH47qEcw8y575rkxrgqx0LC2yESMUksOYOGMt1QThlHKjGfYwzP0g6ZWT9VplI7'
    b'm2Dg81cWiOOJQNxIFbtXT5sBs4XbndB/zXl1nN89U5932zCIYi+xDQK1NpcZg9WhNK3ZyvK8ZPOkrR9PZ1ufn7z1'
    b'6q2JrVxNYms22/r1va1m+Ov/bG59HFv1f2FbrgMjTy2YD4wkUb0bFFdMeZBESFfOORCmMYFuxVPo2gxTjEQuAS+9'
    b'5K6+weR3FSGgjmTQKfRAOvEMEmlA9zHuUlRrZQhJpQFVX6yOW3fPJi+kY0SSqWfwXnYag00rmLOUA3Qtn8fwaLd6'
    b'wC401GnSEJH6DWUAq6kvqj8JpoWCcw3PMIlB0JpUujGNARIDpvg6JF6kCF5ZRn2DVIrqhTABEv1hQ+n/1p3vJcXz'
    b'LO6rrgzOVNWr1ziYlhwL9JEM5CLRBsxYclJPKsXFcayfDRUH0aLo6MU3MYrTTAIJDqG1cZGQpemWCcfBQRCUmepN'
    b'qzKdbuYf+X7TbR1H755HwNCcJGBunkLANmCfSH5jkJq89Dk4gnQhtJxxwh8ZQugloEaCNzBmOUE+zAlFguMYLcGo'
    b'82ZSJHgHgx+zNApyCNLHzZ7diP8iQX6i5+rWjjEmfoHOpqkHydGgA1T38H9oqx9MI8d0U6URtJp0gwRjrBNkxCpr'
    b'JdNOLoxLHlfE6J2Qvcs5Yc6NtMzBoM7jpEjNQFrwI/8uzWt39e2qbloOtMxRtjXt472rflakbEJ+emJ6+03mQ39l'
    b'OZ3F4N0Fu8eUTeoeO+cv0LwOV5/icd31TwuO+SVtI+/6i/IE9dpxZy4lsvMdge2467fR2eqVubgv5f2kKGM4nQTS'
    b'tMOret7iiZJKyhzE4ySSvdg08pVNJuAmgt1Ly+woNLyaOueDt3iWKTkX9O9BpOISCJV7TyglR9+KAIXIcioOJrKD'
    b'yDyR7UTOIvISImcQOZ3IaUwAeQ85zNCOlt05F9ZtgDLbbxBjhdr+TLs6FDMa7sRNpY7UAAVZXvHAQNv5f3+LL2TQ'
    b'01u0KWgIzcU2sGhwBseE5p+yLpacmdtodSULwSfY+Kd88JzQvHhiqqjugNVlEYLzMVb3pnPE0y40z45PvmPGz+px'
    b'eDbMcNmAH+N3qcsqBK8zJPW0u/Ltis1Vz/tdJIrN+i4W5x5UFqSeJ2IqEZ1EdBAxi4hLiJhBxHQiphFxPxH3EJVp'
    b'S/LttCXXwu4uku+A7R6kgdPaq9HPD1phvenUPQRaMnWDBwLktHH4xhgwFQgdBb2UenPhUp/mia5HbQbKU+aabIDw'
    b'cimq5Kr7lnH+x5miW22/40zczVI7TuHcJGVuY8cb7NL0zzb33AFWq/ucHIn45187aN4jETFtLhNR3HMPlrTt41uu'
    b'vQiTUDGDZRq59XBX063LsIVw6BMrxwUNM0wEKaKOOKCSmBGRouwUwE5EGjB7voGINMh6gBketxOQRxoE7Nghh779'
    b'K5RqZxzXXgEzKi4j0glGXfUlQ852szgZu/a1ya99uWlAfxrpwpJ4su/EOszETcvUjlkm1O6meXXBcyhOLAaCTjoJ'
    b'0h+w4NmI1ANVx2zynFdQCL4Gv0lHqXRKeLs5uZvd15rUI1PPSVR1KHJgTIepH7l+cXCqdHSNepdX+nJCOWNEavuF'
    b'p82cuwisb50v7vfLaqCNf2cKkzGZSm2avHgDIkOTotR3Wn/lBruPowwbipUfAf6p76jLN6g8BMEe+G+n2qjUOvWs'
    b'IbWBpU05RsW0+fX7SX3aV73zYm0WP6/Wp1vaksx3Uhtvvp1tdrzz92S0Oc23myL2zqpXrVj3/aoMKp0wep8NhvZa'
    b'1Q/4qpTJQ/uYmO4UmndDGXVfFic01zB4H39EzPxJGXpCS6mF+WYhvOGm3gKaXwRfs0IjNZOsy+SptxSOtesHxpgv'
    b'OlBnwJ/EW4egFWuoFCLebCL6I+JCixlkLaJ7VFjQDZujqjoxCHDEuzVqvot7WJG26P0GAbTDQ2bF1TUqbOlk21Zj'
    b'W6+Q9T6W9QuLLjbMjLNoHTK3brlkTjm9dSa5tA/9nw+YHK4Ji/pMjj/X42Os/huG+X62vsZUpicRrJ4B9QOUih/i'
    b'FNSARZfwRh2YUgYnYlz6J/RxuU9o7hs0NQ9+ydiex8HQHBvOg29jrylWaSerPXtYeYmJbjxSP1hU16HJr0Ej+ocf'
    b'MG3rn4JiI5NxKgwJWbqQ1T2tfs9XZ+sglOIIjimCum8J5985PKVjKUjDto43mbjeDOCBndx8uM/VuWv9RDaS6/F8'
    b'RDbihOd6qY+p4OmhEgRJkfon5WF/Ig8/GTXzEHJMU6/+E4OzJXEcSKBAeKIOKOdZHTCeHK92cIUFnMSjUyWNSENU'
    b'chBphEqpRELdkY4aiEpIviiVMlHD6NJ182aQYvonX8QvMxJOAKxSSiV9ucgrBVTEtThE3Vkui7KWgh3nErNM6yTf'
    b'YBE2Qgt5fKqIWXQK8a4jYh5dayeiTJPpRiBwAfEW0TUggF8sjnJEzKS+fuqJEiWNKhAsRiCzL5MGRgiriwdsde+q'
    b'FqoMEU8GCTioR0cVSXzp6pmMeL0xoXmVlWl+l90c+JSTHKhDcTEz/aURfUfM1C2s83HVsu5fiXOoL7Zc5pVZdIYr'
    b'L0uZkXNB3Zr+OW5E/ccwOmSBIJraW9Zswbnsey7g2FRy/2T9VhL7iDxki39ELmPF5W395ufmeZ22OvefAWRR0k08'
    b'Q3rLdfPwBybZdRrA1oW7xDOEUsbDysH7dVT9QnZtTkti2DlIT7GyjhFusXxP1l7O/X09Pur8M7SX6y7heuoVgn/F'
    b'DAJ69zI0L+RqG5dpa1YT37uIKqWf0ZHaKYFeNrma0XoYLY/RuhltHQKCkc8wSsh4mTnnHqAgfDRJT2RWfiledv2R'
    b'L5jHhwBFiE3hX7s7dSf7oNMpO3MBUspZUoDmuRE0b4yYGbfgHM3PDp5DpuGYIpZxL/XHQaYlZr7LWEq+dCMOOH0T'
    b'eV4CKYsBDMWeGJL2EgOGonjqF4ulmPeuQyPKmPszOq+OTyS3gnRPKdmKxls+DLAberg7IeUCHL2AcWRGeCOOBkXn'
    b'mTh34mJcZhCEraUMa2CXog8ZoTyOXSbTYti0iAFZfmUcSXDKhF5Mh+oaNlVnpjxDEixybvOrgSFcx9IQFg3jAWAV'
    b'DRIxm5qmfxDPKvblugEfxilZeLbGv2PVF/MQBZbhpJyxeJDgEh3zi+BoOjbQO7EAi03nM9emvJDLproZzc1ozK/M'
    b'8c5AiNGY419Yd+//EETrZ8AL/dP/NGN0FpUXArtm4Gs/ZpHd+JDAa3VZmPKZYZqKJx3PEjy5mWGlvmLX4n94rLym'
    b'hltb4V9Zv3fntvyKvV7/0/4KRijcsbO8tqGgclfF0+XyrtptFfX1T5Zz1RW7dlbUTGzz1O6uWLOjpkLas5J79rB9'
    b'SnUV93/81tVue7pmkfeZVbW19X6Mp7MS539ZK0xav6Zmx/ZKbJjxd6737Kivh1Am7e9ZX/D0ru0V/w/+TynPsfuD'
    b'c3zL+m/+vLU1CdXNn31iHegJFc3f1Pv0hCrm7zv36QmRv7k+IZr5m/INOR57rLS8dpvyXMVOf31pfW3NM/As3Plo'
    b'dcXeZ+Dr+sf8kzb/D79v3Y8WYXCY4xpX1M5U/3Hx4sUr5garVyxpXLoc/ffQfw9za1/q+vInHHdiRelLXU+c7+nJ'
    b'nXzGfwF/Ni3D'
))
