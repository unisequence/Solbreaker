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
OUTPUT_SHA256 = '439eee8ea03ebf176694d8f27a1ed51c9c588bebfedb80327f63aea9424dfc44'
VA = 0x4B1EA00
RAW = 0x466E600
SECTION_USED = 0x2A00
CALLS = (
    (0x177257A, 0x1751400, 'sandbox_keep_replay_markers'),
    (0x1BE0647, 0xCE3490, 'sandbox_graphics_page'),
    (0xEEA26C, 0xA84AF0, 'sandbox_blur_material'),
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
    (0x3851E20, 0x1DEB640, 'sandbox_notice_attach'),
    (0x3851E24, 0x1DEBE30, 'sandbox_notice_detach'),
    (0x371A36C, 0x82C280, 'sandbox_project_markers'),
    (0x380203C, 0x1846390, 'sandbox_gun_marker'),
    (0x3749420 + 0x24, 0xB7DD20, 'sandbox_blur_slider'),
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
    '533573b98a7f2f865c4efe7c079f7a237455179a947a14c2615885076e0f434f': {
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
    },
    'cd807cc1df2732a28805d8b68342dbdf0c2476b2665ae58d039922529db8ca01': {
        'sandbox_graphics_page': 0x4b207f3,
        'sandbox_blur_material': 0x4b1ff34,
        'sandbox_refresh_score': 0x4b1f5aa,
        'sandbox_message': 0x4b1f629,
        'sandbox_reset': 0x4b1f762,
        'sandbox_frame': 0x4b1f7f9,
        'sandbox_draw': 0x4b20018,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1f45b,
        'sandbox_binding_caption': 0x4b1f2e8,
        'sandbox_binding_name': 0x4b1f2ad,
        'sandbox_set_binding': 0x4b1f370,
        'sandbox_reset_bindings': 0x4b1f323,
        'sandbox_blur_slider': 0x4b208d4,
        'room_header_description': 0x4b205d8,
        'room_load_description': 0x4b20528,
        'room_get_description': 0x4b20677,
        'sandbox_move': 0x4b1fdc1,
        'sandbox_stop': 0x4b1feba,
    },
    'a6af849b04c29f807cc7a11a4bcd4139f5e545cdfbef652aa18ed71dc9363ff8': {
        'sandbox_graphics_page': 0x4b20939,
        'sandbox_blur_material': 0x4b20069,
        'sandbox_refresh_score': 0x4b1fbaf,
        'sandbox_message': 0x4b1fc2e,
        'sandbox_reset': 0x4b1fd78,
        'sandbox_frame': 0x4b1fe15,
        'sandbox_draw': 0x4b2014d,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fa60,
        'sandbox_binding_caption': 0x4b1f8e8,
        'sandbox_binding_name': 0x4b1f8ad,
        'sandbox_set_binding': 0x4b1f970,
        'sandbox_reset_bindings': 0x4b1f923,
        'sandbox_gun_marker': 0x4b1fe8e,
        'sandbox_blur_slider': 0x4b20a1c,
        'room_header_description': 0x4b2071e,
        'room_load_description': 0x4b2065d,
        'room_get_description': 0x4b207bd,
        'sandbox_move': 0x4b1fef6,
        'sandbox_stop': 0x4b1ffef,
    },
    'ee3d1f40c9e22ed0e3ba67a21dc91a1a96e21b58fd22473de0f60e883cd2a834': {
        'sandbox_keep_replay_markers': 0x4b203aa,
        'sandbox_graphics_page': 0x4b20b6d,
        'sandbox_blur_material': 0x4b2040c,
        'sandbox_refresh_score': 0x4b1fd8b,
        'sandbox_message': 0x4b1fe0a,
        'sandbox_reset': 0x4b1ff54,
        'sandbox_frame': 0x4b1fff1,
        'sandbox_draw': 0x4b204f0,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fc3c,
        'sandbox_binding_caption': 0x4b1fac4,
        'sandbox_binding_name': 0x4b1fa89,
        'sandbox_set_binding': 0x4b1fb4c,
        'sandbox_reset_bindings': 0x4b1faff,
        'sandbox_project_markers': 0x4b20241,
        'sandbox_gun_marker': 0x4b2006a,
        'sandbox_blur_slider': 0x4b20c50,
        'room_header_description': 0x4b20952,
        'room_load_description': 0x4b20891,
        'room_get_description': 0x4b209f1,
        'sandbox_move': 0x4b200d2,
        'sandbox_stop': 0x4b201cb,
    },
    '8fe72ee71f7308ac3a349a4cbb80005c7ad11fb76811f1187bdcdbf147a5a95a': {
        'sandbox_keep_replay_markers': 0x4b2057c,
        'sandbox_graphics_page': 0x4b20d3f,
        'sandbox_blur_material': 0x4b205de,
        'sandbox_refresh_score': 0x4b1fe93,
        'sandbox_message': 0x4b1ff97,
        'sandbox_reset': 0x4b20126,
        'sandbox_frame': 0x4b201c3,
        'sandbox_draw': 0x4b206c2,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd44,
        'sandbox_binding_caption': 0x4b1fbcc,
        'sandbox_binding_name': 0x4b1fb91,
        'sandbox_set_binding': 0x4b1fc54,
        'sandbox_reset_bindings': 0x4b1fc07,
        'sandbox_notice_attach': 0x4b1ff12,
        'sandbox_notice_detach': 0x4b1ff51,
        'sandbox_project_markers': 0x4b20413,
        'sandbox_gun_marker': 0x4b2023c,
        'sandbox_blur_slider': 0x4b20e22,
        'room_header_description': 0x4b20b24,
        'room_load_description': 0x4b20a63,
        'room_get_description': 0x4b20bc3,
        'sandbox_move': 0x4b202a4,
        'sandbox_stop': 0x4b2039d,
    },
    '36bf7eed6fb772376123b0575bb068d900718a6f894c9389de60b201c93f7419': {
        'sandbox_keep_replay_markers': 0x4b205f7,
        'sandbox_graphics_page': 0x4b20dba,
        'sandbox_blur_material': 0x4b20659,
        'sandbox_refresh_score': 0x4b1febe,
        'sandbox_message': 0x4b1ffc2,
        'sandbox_reset': 0x4b201a1,
        'sandbox_frame': 0x4b2023e,
        'sandbox_draw': 0x4b2073d,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd6f,
        'sandbox_binding_caption': 0x4b1fbf7,
        'sandbox_binding_name': 0x4b1fbbc,
        'sandbox_set_binding': 0x4b1fc7f,
        'sandbox_reset_bindings': 0x4b1fc32,
        'sandbox_notice_attach': 0x4b1ff3d,
        'sandbox_notice_detach': 0x4b1ff7c,
        'sandbox_project_markers': 0x4b2048e,
        'sandbox_gun_marker': 0x4b202b7,
        'sandbox_blur_slider': 0x4b20e9d,
        'room_header_description': 0x4b20b9f,
        'room_load_description': 0x4b20ade,
        'room_get_description': 0x4b20c3e,
        'sandbox_move': 0x4b2031f,
        'sandbox_stop': 0x4b20418,
    },
    'ac04e480152e126363a20d8d7757255d2b782610b07511ddbc127a24653d036f': {
        'sandbox_keep_replay_markers': 0x4b205f7,
        'sandbox_graphics_page': 0x4b20dba,
        'sandbox_blur_material': 0x4b20659,
        'sandbox_refresh_score': 0x4b1febe,
        'sandbox_message': 0x4b1ffc2,
        'sandbox_reset': 0x4b201a1,
        'sandbox_frame': 0x4b2023e,
        'sandbox_draw': 0x4b2073d,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd6f,
        'sandbox_binding_caption': 0x4b1fbf7,
        'sandbox_binding_name': 0x4b1fbbc,
        'sandbox_set_binding': 0x4b1fc7f,
        'sandbox_reset_bindings': 0x4b1fc32,
        'sandbox_notice_attach': 0x4b1ff3d,
        'sandbox_notice_detach': 0x4b1ff7c,
        'sandbox_project_markers': 0x4b2048e,
        'sandbox_gun_marker': 0x4b202b7,
        'sandbox_blur_slider': 0x4b20e9d,
        'room_header_description': 0x4b20b9f,
        'room_load_description': 0x4b20ade,
        'room_get_description': 0x4b20c3e,
        'sandbox_move': 0x4b2031f,
        'sandbox_stop': 0x4b20418,
    },
    '28acc84d77bf0c8cdc7a4f859a506e796ffc0ca9ec4e687fefc59fe17b5865c1': {
        'sandbox_keep_replay_markers': 0x4b205f7,
        'sandbox_graphics_page': 0x4b20dba,
        'sandbox_blur_material': 0x4b20659,
        'sandbox_refresh_score': 0x4b1febe,
        'sandbox_message': 0x4b1ffc2,
        'sandbox_reset': 0x4b201a1,
        'sandbox_frame': 0x4b2023e,
        'sandbox_draw': 0x4b2073d,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd6f,
        'sandbox_binding_caption': 0x4b1fbf7,
        'sandbox_binding_name': 0x4b1fbbc,
        'sandbox_set_binding': 0x4b1fc7f,
        'sandbox_reset_bindings': 0x4b1fc32,
        'sandbox_notice_attach': 0x4b1ff3d,
        'sandbox_notice_detach': 0x4b1ff7c,
        'sandbox_project_markers': 0x4b2048e,
        'sandbox_gun_marker': 0x4b202b7,
        'sandbox_blur_slider': 0x4b20e9d,
        'room_header_description': 0x4b20b9f,
        'room_load_description': 0x4b20ade,
        'room_get_description': 0x4b20c3e,
        'sandbox_move': 0x4b2031f,
        'sandbox_stop': 0x4b20418,
    },
    '2794a4951c1a4e08e1ce681f9d42f10863a81a3ff97e13c90e5b318246289dbb': {
        'sandbox_keep_replay_markers': 0x4b2067d,
        'sandbox_graphics_page': 0x4b20e40,
        'sandbox_blur_material': 0x4b206df,
        'sandbox_refresh_score': 0x4b1febe,
        'sandbox_message': 0x4b1ffc2,
        'sandbox_reset': 0x4b20227,
        'sandbox_frame': 0x4b202c4,
        'sandbox_draw': 0x4b207c3,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd6f,
        'sandbox_binding_caption': 0x4b1fbf7,
        'sandbox_binding_name': 0x4b1fbbc,
        'sandbox_set_binding': 0x4b1fc7f,
        'sandbox_reset_bindings': 0x4b1fc32,
        'sandbox_notice_attach': 0x4b1ff3d,
        'sandbox_notice_detach': 0x4b1ff7c,
        'sandbox_project_markers': 0x4b20514,
        'sandbox_gun_marker': 0x4b2033d,
        'sandbox_blur_slider': 0x4b20f23,
        'room_header_description': 0x4b20c25,
        'room_load_description': 0x4b20b64,
        'room_get_description': 0x4b20cc4,
        'sandbox_move': 0x4b203a5,
        'sandbox_stop': 0x4b2049e,
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
    from .client_battle_settings import EXTENSION_VA, SHARED_RULES_EXTENSION, SHARED_RULES_MAPS
    from .client_tls import THUNK_RAW
    extension = RAW - (VA - EXTENSION_VA)
    if extension + len(SHARED_RULES_EXTENSION) > THUNK_RAW:
        raise ValueError('Battle settings overlap the pinned TLS code')
    image[extension:THUNK_RAW] = SHARED_RULES_EXTENSION.ljust(THUNK_RAW-extension, b'\0')
    code = code.ljust(0x3000, b"\0") + SHARED_RULES_MAPS
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
    'sandbox_keep_replay_markers': 0x4b2067d,
    'sandbox_graphics_page': 0x4b20e40,
    'sandbox_blur_material': 0x4b206df,
    'sandbox_refresh_score': 0x4b1febe,
    'sandbox_message': 0x4b1ffc2,
    'sandbox_reset': 0x4b20227,
    'sandbox_frame': 0x4b202c4,
    'sandbox_draw': 0x4b207c3,
    'sandbox_mouse_hook': 0x4b1ea0c,
    'sandbox_binding_menu': 0x4b1fd6f,
    'sandbox_binding_caption': 0x4b1fbf7,
    'sandbox_binding_name': 0x4b1fbbc,
    'sandbox_set_binding': 0x4b1fc7f,
    'sandbox_reset_bindings': 0x4b1fc32,
    'sandbox_notice_attach': 0x4b1ff3d,
    'sandbox_notice_detach': 0x4b1ff7c,
    'sandbox_project_markers': 0x4b20514,
    'sandbox_gun_marker': 0x4b2033d,
    'sandbox_blur_slider': 0x4b20f23,
    'room_header_description': 0x4b20c25,
    'room_load_description': 0x4b20b64,
    'room_get_description': 0x4b20cc4,
    'sandbox_move': 0x4b203a5,
    'sandbox_stop': 0x4b2049e,
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNqdOg9cVFW6d2CQwSbvoFTUUmHRLpkFk+4maRs6XqB0cnQcQAGLBJX/BPeqPaXAyyDH0y3eQ8n3W2t9u76Xu7nv'
    b'R7s+Y1u1AXwOFtWIbE5KLRnaRagdwxU0877vO3dAaNtt98Fv7pzznXO+833nfP/vqBz8Zd4fdv4NY/tSTTJrkklV'
    b'NU1zqHdM5zj5iHlFf8GBy1+7yFknaQ9rKJpu0Hxuj2hWGpOffI0jrVk52hmrJyun3cVneGraLBumG8jZDKdSZSLt'
    b'iqDKgxY+UlC1hxxaF28RVOLnI0OV/JGsJ2EFTCQfpDsBcSG3rhroKAwt5AoN66BVvUxrSL5QGiqPaJLR6hmwkqNW'
    b'DxECihAo5BxAZEa61rgdJhAvNGKHSkPdmniSCqYkIcBv9/AHPEr+xaxVwV3U5fdwXI33q/s4Lt0pD4pE2B6W2HG3'
    b'gbjqiVDrPiaFW48Re8PAausxKmxXpBbl57VwBsQ+SE4Re4DY+4hdJRcK56lpAF4J/EdaPTVVLc4Fi6ywGxEaBuIL'
    b'Z8NSmH5BjdPnSD2FMxF0gZxSIwG0AkDHBpqUrcZrmnb4cWCSCNX8gV0/ffyqplHXdmqvV2yziPCCIuzVpBfU12Ga'
    b'XLWXWymtsXrkqgbOK+zkjLBK2kWE3V5hTwSgEH9Al398UFmgySNVG3i68GOPPMJvmERcOKfodLQi7HRQodrQTm0W'
    b'3hIqD0ZTZxRZGGchy+Oi+EhjHOxKhVrYT/OlyNccvLsQSJ1wdtFxeHYHZuhnZ6Y7A9ozRvdp3k3xns4Y+NoteHVG'
    b'cnQdl8hx61TscVpD3FdwO+1wVmuUrWsug0RpTS8ESkPpix68tjZl6zMIXEZcKpyzdpy6VArnbZujbN3CBrTjyssL'
    b'kRj7ELHNI4FCg/rON/rpT+EPCEPz8oBPuQHP8VUkCYgmfka2tvsqbITEp5P3nU76OMjiUWFIo/MT4Q7CrZ7+X8Iq'
    b'ypGTSqPj01dCk3zSgNLY/HJ1SJIjWepVXENy62ZF5nqqQ5YVGgrv1HzVVUMg69L/wt6tlFMaWxJexVX/U9PuOLcl'
    b'JCnTKP268IfaCdIKE3Z7ueUpzvu9ycbF8xfcfzTZ5KSpFjI/gqZGJZPUKDL/RvJMpHzOIobSDfEgsdRlJs/cdHT+'
    b'PQYy/1YqWMjS28n86S0GIHHsHnjLgjnkbDqtWOykK9NIeRSVYkimheZE85Ec6JWJrEzLIhWLV8nb0kI4zpDDRy6Y'
    b'0+4CqT7r3BYzufa0GD7/4WyTJn5l9dSezsoBodXV72S6OvsuuN+jH8ZzHCiiPBgjb4zi14fQ6Oq5IHEBJEMKh4aG'
    b'V1F3Eq7G2mU95hUGEUCEHqXuE4ARVy9qieCnrh7iV7fAGBxFvmKbpDT9DMcFVS3RgWFMkamgKk17YURN18G4Rwhe'
    b'Cw78DgceuT4QCgPbQmvhcOfliKG2gSFo7rBVz40Vrwwo1OWHLXl99jTFFq40HcT131zDffsGjipN55AEvzoAEKXJ'
    b'e5nNfBIApxCwaBL0puqLSId6lC3rHXiJdChN7yPMrqpvAJDa1WE/EQb5wwbF4au+EsFvXaehjg7O419aBa0i1+Dk'
    b'+crCEB9MGvgI0G+6hjtJTRQMV9MXiCqcCn0k1UiFQZJqgoNINZNUCxV6UTD86iI2X/wpfxjODbcI3/Bg9abwh6WZ'
    b'2xaZlKUMML16U0SO9ANZTZTbJ8N6mHsNOoAGjkOCsxlRmi7q7F79hqGbAihixTC5PXwgVG43yUdignqCJjuoJOQE'
    b'3DxcH97NIBw4DwcOhrtHvpIn3QiXjWJwVOixDoCllK9ErL8TOHh7MgCtXUqagfiHh61dw8MAlNsS65eH9bAx4rd2'
    b'HV1448f3DA8r80MAsVLH9bdwbMqNPYqrh9r9yzTfeGtDvEqN77UCLl2RepzkXXIByIIp5LIiV/e9x9W0x+IgAC4A'
    b'sdoJKvgnrD6ZTo461VV3gkS3G38I/mvQBPZbvQXP4wK5ouzkuGQu7N4/vWEEauBWqDToJKe1DwEDQ+JNnon+0HrM'
    b'2kXSEkl9L3JxfGta8lbHwnZ3F1/bBgDYKR0OTGk4cmd1aNI0EQ3Kf6HkVhrdl8SvlIbM8udDk26WVPm5GE488xae'
    b'nfrGFXYX3dTi7pIi8uhNcYjakkdvxEbScemtpHKTFAamRMxRJQ21jJSbiCOKOCzEYSaOeOKYTRyxxBFDHNHEsZk4'
    b'NpIGDyJpOMKeu9hzJ3tuh6e3ej+n/3mrm4MtkmnMWpXTbvW0qz++Aw6p7U9xjJ8h3pJjgofLDA+7hY9v5+PPDPEr'
    b'8NM+xGce4eM/hean2Bzil3r4hC3NQOP6KUP8Kj98fPDpGJjCZ5yG9dv2wRAff2KIj4zOgYfDCI80k+5N3rkdvck6'
    b'2BauCgKEUcnrowuW02WZxBgHnslIndnwZQJlUZ1oZjrus8UheSr/pq9VtUAzazk8SgaH+BkeaLy2HxXxCFA/4x3q'
    b'TARSgczMDvh+CemEGVtb2HdOK7RfOsjaK47DB6jPzEqDRj987IEhPvsUzHBmQg+YWmHLhnHnYnjYHDD2R2i87GGr'
    b's1vhImHDyKyn4OHMGygiHff6RumbAXvPOA8IOpG6fWxF5ntIzxGdDjhKyyJYmf0RAHtg8CME5AHgeBDnim6AHkfo'
    b'OtyNbRJkDJkoKobHatF9DAbuwFnlzGVAzwbzMwfg08svBDIiU+D8M9+BRrqJBMgiOJ3IUiR8DdAZ+UTUeAVKJx8y'
    b'JxBPt9SyAKWbbqlnDX/LckBPm15AQTeHuCFe6hStSlWPtj+sGq1oY3VWDczzZWhd1N6pNJbf7uOgrTSixpLLWlcm'
    b'FXwrH0EsUnhLJnwNvDEhzmj+AUrGE3fpcUYMsftp/e4vIdToEl3uY+JS0k4+VKpNd7hDk0Ikm/wvcZz4SJHLH63I'
    b'icd/xinbb9nzNZBR1bMsQ/OBTebEm8AxKHLzrZ8YyIWaNg52xCHdhABBJ0Z3z1A2x5nkvoC24So7A/UupOTokeno'
    b'EUE6i4mwn9ibtYaLl0pDQS1dflDkdOqcB5QtUez+pRDbAmQhFfZrDYkQis4V/NJP5E1zODERYGFPhb3OsaD5LtqC'
    b'ikiq9spaiBRV3fIa6qR4g7zJwol399eh5fF4bdHP4yTbxrGARlk78mR/LnBHbbEYe1JX871tNVc+hWCQr3PDU65q'
    b'5tjt84LQjM7Zj4ghWCO2WDCRXqEH++CavUIvMwOgYhCqQvQHnmch6tl+9b1LTIP6fwz4qG0OEXYh1HgRneE+cGKT'
    b'0TXs7v+aueh9c2U0PWI0aYNZv2cWrhGmwghhI8h3wBDk+39pWjnc4GFq0MCACXsA9FuYsMYrdALHWpjj9n8DydmJ'
    b'GzTwB2of2g6sOhSh00Hte6iwE8IvOEozY5BcPtEbITSkyppR6rZqpGq3VRsbgLD+bttm0fVWOHKvTRJv4A9sf2g7'
    b'hNdy26xl1L5Pwfhkv7p0SNNqqvwYTK+QfkSFHupqSHL1SjFAWPRfbTwJYvMI127YMlzaTjdFU2E37DJ8ktiip3uH'
    b'vbi5eBtdFEMb+3TG5QsGsihG/tSgxoEDHvZiVCPeQ20xciCE2GL2hmBIsxdDmuAS+1486uGrONmIk++CyfqYHLDA'
    b'ErnXIo8YYNKtx+TWMPU9NtOEM43yoo2xw14Ltu8eWzW7GhbdgxnV9WXhahNbljy6LFn+PFYMr1m0ETUDlhJvzecy'
    b'DAJ9dxSC2OzSfGvcnmdD5Q+MhZHju6ZLthgLX1sEy+RN8Rxfm4sIhGaw0Y/kQJPf6mAYZ4LUJFPncrooE0RREVpI'
    b'VBxZPGbc1Wcvo8C0UNdBkDviLAfVshVTwUOcicQmem0zDUwhttqSwbz9l0+34692MOfSrpvYFZ18wrsbeIA3dTN4'
    b'2xD/ZCuuWQgTfHz8u2hH+YQTbM6r3QzHk6cY3OIs162t5cVOtrafTzjO5r3o1/sw9TxaVhEnFzMeOkd1yd4jfwB+'
    b'8qOgCzml+4vIHBAw6GejubX3jjIMwTEyXD+CaMELRBaVM2e1AtqWun1Bf4KutxO9KwwgbwI4gMhFxWjRxYFb2HmK'
    b'4RDWZQwDmgwPH6/y8R03bNo47C1Ho2p2axIvb5rNidJAeMtDQORAWMuvkVZnLFiqCGpvvvd4UmtFxgRrAVzBBiU+'
    b'6oIgC8wF0t1JBaAjUugeZye6R+3Ef37F7ATGlTWf/xwsP6jtptm6uRo1VrpB/9NNaNCrYtDVv8/cigWSPWXrVZYM'
    b'NmIy6PbwtShzyssREB8tAyhLDoVBQF86WhQIGS0KJEOqn8xyfnlEE5+YUBDAwkWG1tgdwNwS9ZgKZmtXkhCQpgC4'
    b'OoCWG3bb0UZcfRl6xYC6+sCF3wBI17HhgZtgDELYwghNgqzEh+MesRfNsE93U7+fgpYGXIRSdVC9GoVe4lXwFeCu'
    b'MsGsWT1Dr0Pi7RVYXUB5FQ0PJEBgNfkDc6l9F+T1SgbYIdce9YMv0QLtRQuUKU1Bq7nwm4OgQvDlAftiS2qX3gWF'
    b'KZynvHgEjhhS+D1KE5Yk1F9cm5Cep1PJ5FRLkJT2aUCKkvKePDJp/VQ1DnYgkikrrLdoumFVjtZb5IlWakOx9oAR'
    b'4NjSB9lS/23jlvZ/EVzKFY8uTYSle66OLWUpgzo8DZe+AkvVA1+wALs9rHnG3QaIzv3quwBJCki3ES9pgzTUr4pI'
    b'0BXdofX/lpExGnQoVSYnuaa+xvDZb0PPFUvD6fMQA4BN71EzETtE+juxZgPmWlZnAaUbwuQRo3QzeS4GQGQStfeO'
    b'OUytt+ZRPFvpeZpqTMPSyHr5eRO/oVyB7Evo1iTIPHpBUdVUwJzBW1KM8mA0q4akRNH06NH6iD4P0npvJujWHFBj'
    b'cKbqJZZVSfdqUvf3rnw7RM/iyQiS1RbLMX6d6sGpyOrTt8JzMBZUkSg9mGC6LNQeRatMZEc3diUzUfzQUDMg2aY6'
    b'jAGIuw+eYZzXY6i5wmpC7l4ASFPly6HSjXSV8b47ar7GOsz66QQQskECyNkuBNBWmYIypPVShkv9AWzBFMeiA3Sf'
    b'/jZAve6L0J+Fkv8RhCfuEejNhl7r5RCv+yp0fowphpsD6fwJaxmhNYe1TNBKwvSmQ9mBSESe3kyjtpDtbjOMJA98'
    b'riy8LZrsUBlLgyjZwkXiDmDDjcDC0HXInSNDp1xnZCxhbLOwVAxFsTgSz3NqNJB830BQFBsMH4MosuwLxM/6Bcu/'
    b'HiNtef/BMq4Q6RH5sIN5bxP5LYa1A5Yk9i1hYr4FW/Jb1SxCmya/9QJrTEb5yuk/fvG6FuH2X1hw+8ZbYPt954Pb'
    b'Jx/4ZNz2vx1k20fIX5pIM7ro4HKnU21mi9NhMbGPqMXnWerXrlbjndtHWElj6UhSQ59OWRdhrZUQ9R7+xJAFF6iL'
    b'FBhXrCXlmKndQquiWIUlGg3pab7WDFKo2MxEUJM6pKqauctTnFZJqtlkTF/ymE0qplkm+eu7gLavl/DuSSHoywPW'
    b'LlUbQJL52ovAQjBfTbLw7rPo25ONOHIKm/Uj8HQf42s7sQcR3nKsgvG1h1hQAMbZGeZ4fT0cRf/rBiTDktQqFQdp'
    b'yAMaUpbNT5WyYKaeJ5MBXQ752s24b/CueHcp9Lxb8DpwGrWZgldCWKJCgTuWqPC1gr6vej8Eef1z2JampFZ+Szg0'
    b'9W15NwaxsLVzwZJM3v0lF+SY2PvUybj9Udz+G25s+0m8+88oDc+ZAP9Z5v37kuwq7/6ILR1M8opRbo2vfQeVPdWY'
    b'JAzy7sM6lYjpd2zFYNIH4jJ653B7MhAi2uemwFn+O4xcak8Wb72UstkkTqNNegQZ+D3Gduo58LT8W5+ZKiUYjhXL'
    b'rg9j5Vf1wvDAyj9gpQZ8dIpFMb6CB3L8vNybLJ+xRPjIqdaBEPkML38WG+GL8JOUzSDXKbEDP6QS1j5IFZiaALFb'
    b'iBQdtqDzDSPJmWAYoMGihFE77aQ5JvCzTkW+/f03jJoPDsyvnuoHTcCS7PoxEZGKUEirWR0Cr0h9DSw/JCVj5QiS'
    b'FkXSLCTNTNKMJM1E0uJJ2mySFkvSYkhaNEnbTNI2knpW2qhnpY16VtqoZ6WN+u0TCho05+ox3Yk4wZ3RKrNS996H'
    b'bxgzwH2vyHJrootmWEhAnY/OB2vRAnQ+7mcqJjPcrXTrPDTAToxoblGa/H/eEgo83EiT0flPosmQQ5nRgp8iAea6'
    b'3r8w0QEz/Tcz/Z+G+n8OLJRkQRMAhxXWsLrWAJEJuMSv4LTe0o3Qj8hR6KQDGbB9HZKBpoG8c2+bfCVvfQSbupZt'
    b'A24jp8086jXq2DYzp+kOErS9ykLsKpWisXwimFlJCzQfvFQMuE0qxGI9VogDj6fedI7xDHoRrzIa1pL2oHSHovap'
    b'c21GyQkCC6JNk+OTDFIKCDblkmzxLOb8DK0RADKMFFLTeBpOnIuJLY2mmojNQSfRdCOxLSfOTJoCgGzUvw2wD7HF'
    b'UVc3tftBxKjUy4QOYsA4WjVIpCjIR8PKD8oGKvUReyypAtOlQtxBXDHykVhdBoOMKzcg47OmsoIecApXTewBZF8Y'
    b'VB8+G7SYT3zOOIu7Lom3UldgrsMoRdIpSWnxkMJ2EUe8egu6uiqwkoOK7MupNcCe1A6UmVdm6bt+uyBArjnVyYyE'
    b'5kgkIRsidZ7Y92CsvEt9q4/t3xpW3bYF0tjdpIPYd6qt53TriWaEtEP8Bqc6Stg03n2OXTpf+wn2bcD5Tt7dxWL1'
    b'mVhm9GKzBSMDtCu/x1648q/zflMZmuQTpyj/Wn4KpNTHu3+O2DsUaa9qAuZBRltBvrcDf7xF2MsiE7mD2QzMsrxy'
    b'b9B8EhkLBIrgI42YzZArLIKhCKQ/Y5DFEMdkx0WpWz5Hcd4FMSmkxJoP0PpGAx6QM1cfC3gCGPKQK6rrc0wNEscl'
    b'Am1GDst2u+CkbmY59e5xg8HrBQWCkEf9VQQe8BJwhuq6z3BTM+pQRhgXJeErR02aIVeZOTGO1rHsMnkOTct2HxOj'
    b'FPmpL+EwDNLkQkNNe6zDx41VdtpMnPo4oA0b4bmgDi1V7Cb2loMuNVEr1reeNRIIBP9yBsMvP/jtwON7uWVoP1Bd'
    b'8MXCcB9LeOQqP1h/VhUD/yA+N+obJBFyMfkwvofCinmvFKZU9Q78iNbt0vX6wtwUszQIeNaAnN5Xt50l5as3nFIO'
    b'NzO/0K24esCZRzdyCrkJkqCl5DhxdWP20Q3gB96BKX5MgApDM5YBUR7CELPCd2DFd9evnjZhZmLkWWaymdiryZAa'
    b'dUY3ALVh5cl/hHhxo4YKXave2seUJmqcOQANqwaZFOHGqimbOOHSYBQCemUzIhgT0dcxbHDVgpRuY1LaAVK6jUlp'
    b'B+9+IYSVA2D7HZv1tzSi/AdWiBLv5C26hDKJpLo4Cr2QiMFZ+gaikMT7+3TRBnlVW/qZbKMQQn7E5BCicnsfSKAF'
    b'sovFILIfoD565CPRXjfupss77ib/Qb+k2mssRKhVuz9jJYUlafDIWMgy4eyMOfBYMg8OCoCpi6GTmqwI+0CAXZgj'
    b'o8qDQcPMHIQjmN6DfVsM6f2v1GAN2pK/D84i6Q87WT3WktPslcdK8fKo54IBaT+lKA+8e24whpqFrsh3b4eSpsmX'
    b'n+a33o3lFCzGd+qFeCwnY1l76TE+4cVerFLUoUIDrtf36jjlPYgw/iom9ciCCx4rRnDCPjZhxQnAcJKPvwbNb1gz'
    b'oamPIXqTjc/AuU2qXiCHHbO/HOJXnQRYFSICtNle6BzuYRNWtfLxHwC4DT51+EKQT/CyCsrhgF5pGYFxyt7Rwow/'
    b'wsB/40L+zQ69SI6Vjrr9bOqMy3wCVte9CLqob4/Fa9rM+HmPof3vINrzrIqyAmvi7GRBJAk7bdK4nV33Lr0S/ss9'
    b'+vfP9MOJ/B2ewUCIVRuTbM0nH0Ip4cQfgli1MLGqD9pDewfN6RyzhBvP4O1GT6hygESfBT+sdvcyqb6JOsB6i1Oo'
    b'yxwwOJIhnYOvhcEkV+00olaW3sC0cjaxe0Arn/sElMt1BB2ESS1CG2Q/ojTNADNAvFTwqD86i6AOaCpNDwNUvZkB'
    b'/AywCAFhDNDJAFkIuNiHAB8DlCLgMwboZoDnAaBIPUxHwPlhIfxCBtgau0eRuep+w7j3a9etqFG3neoNjIHfTB7P'
    b'wKGPxzHw60+DDDSMMbCm7zp5P0dqXOPIw3di6sJx5LUGyUv8DvIs30Oecmj2nzXNqeaGoh8Jn4ypdacFU+tEgKtR'
    b'H2PyjE0Ix8jRsORfqOBWlMbjuGUd2+oy2Ffqxik0zZih1OELR3z1SBhMqWPvDdzxiMJHdyAMXbWePphqNlo4C9Y+'
    b'0aMrhxazFYHmNzlyHN9HHHoKADw5NBO+1pupG8eJG3uwlr1xD8OAjtP30qlQGr/EA/oJEy5pE92RjUD8qcdzG0qs'
    b'HuXQGugnb3shWv5a0+rN4fKIWQoAXKlEFK8g8m0xofUxhuSkNunP4GOVBWb6CsO/xHLvCWUHrje0kg8gf5mcvG1x'
    b'6FC9OWTgK2XHWoBnULsJGHiikGOnglwrymKdd8E08ezVv2Bu5W00BX8igr+b+Vr9xWm82EGIOcMaZr/P6VEwVvb4'
    b'2leY5wsoL18DBq2X1JAzwfDcwqDZwI/65aeYiY3lhc/A/bkv8TtaWRGv9riY7T4tZo6+R23YjRgb2I9BBBUW8W++'
    b'03oeTQyapq1oYpQGy6oWNHDHyEf6K7RIORtQa10Q/cK2wdUB9VwvC3W/xeJUxmJzuM6ihUWgKnB58BT7YQAwFhb7'
    b'8Zsom0AlywRpypw0eSSE33pIz//whw06a9M+1X0xFhvVnT34E4I+CBL52sZgVEiv554yciZVwWI+4xiYwGqkmU9o'
    b'rSjEF5kjzOZ2VOQAR1uv4jvQ5+aEDvEzPfXCkJSkCEOFBuUlrMc4kCP2cxr1qfOathJ/jBShW2aYiqvwJzVS8Ac1'
    b'/AHXEKO4T/X/acz9w1FQY1w7jYJPDHxmw2deXLtUmV8x68EH8oqLudR8cX7ls6WrF+U/6xRzxXwEZBSU5pVtWL6u'
    b'Ij83z1FRtjq/svKxPC6/uKCkoDRXLCgrzeEqpfKK/JLc1c/mcGsrctcXiNCQSnGGmJ+Xw2XllpSUxXJ5+WtypWIR'
    b'+iVlefmxXFZxwfr8SvgWC0qw+3SZiL3SMjG/Mocryq8ozS8O0mUvW5+fUlCcL2ycz1WWFT8NtMC4LbckvyJ3QbFU'
    b'wVWUlZU8VlogFuQW2wH5+P5i3GU8YD5QM2FBQakkTpyyAEixfhvwIAOkwTHkV4xtEuyOQ6FDrmO43n+Qc+aLYkHp'
    b'2sqEGTMSFpetzi1eBhOWlOMpVnLOMcZwcgLnXF1RVlycXpC/IaGSNW1lpWJuQSkwDg0AJFxfYS9DHHgUCQtyVxet'
    b'rSiTSvMShLwCsaxi3DSc4CwuAHq4iUBHfsXq/FKRW9Pw7f8s5OH+NWUVJTmxGPaYwosKuXF/jI0E59MLysoqxUSO'
    b'wwo7/2249a/hKcXPJuJ681/Drd8Fh8tO/Bvw75y/uCw3Dxfc+B1w63fAneW5G0q/A46ilzcOzhlCQsMm/Q3+x8/7'
    b'jr+JfBasXSdeP5fvm28vqKwEDWCwf2T+8tyKtfn/BP7HpZLy8ef4ffOX5RfDSf7j+Jc7dNjk75n/7T+Q09GjZX+m'
    b'4DyAjx4h+4u4Dh89qgn7AXz0SL49f5R19hd+HT7K4rfnL88vzi8vq9Ax3fB3+MG/Bx7IzitbLZWAdlVmX7dd9xfl'
    b'P/s0WNbKB8Rxm/8T60uYxt//NCjv30Xxj6wHQv7/9OPiNeh7qx8tmyo/DN+pL7X95RGO2/1o9kttP32vs3PerFmz'
    b'Hr3TXfTo7OqH5kL7bWi/Pbh0ziMw9sjJX5Y/ynEN8zku+VGAHSqbGgo4OhbA1o+Op+P/ADWbu/Y='
))
