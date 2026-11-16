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
OUTPUT_SHA256 = 'd27f01710bd265d9e3596e07052c2e5836ab0dfe9a5856012881c79afdc2a3e6'
VA = 0x4B1EA00
RAW = 0x466E600
SECTION_USED = 0x2A00
SECTION_LIMIT = 0x9000
MAPS_OFFSET = 0x3000
BOSS_EXIT_OFFSET = 0x3900
CALLS = (
    (0xB00531, 0x7B70B0, 'sandbox_particle_update_queue'),
    (0x1323FBB, 0x132AD50, 'sandbox_tracer_update'),
    (0x1BC4C5A, 0x1314F10, 'sandbox_tracer_position'),
    (0xD540DD, 0xA95EB0, 'sandbox_scene_shader_clock'),
    (0x121FA7D, 0x11DE9F0, 'sandbox_load_keyboard'),
    (0x1C279C6, 0x120EBE0, 'sandbox_save_keyboard'),
    (0x1DCB58F, 0x1C61300, 'boss_stock_panel'),
    (0x17449D6, 0xCDDF80, 'boss_screen_root'),
    (0x1DF6D08, 0x194C490, 'sandbox_boss_menu_open'),
    (0x177257A, 0x1751400, 'sandbox_keep_replay_markers'),
    (0x1BE0647, 0xCE3490, 'sandbox_graphics_page'),
    (0xEEA26C, 0xA84AF0, 'sandbox_blur_material'),
    (0xEEA337, 0xA6A150, 'sandbox_scene_graded_blit'),
    (0xEEA356, 0xA6BC30, 'sandbox_scene_blit'),
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
    (0x3742660 + 0x24, 0xAF52B0, 'sandbox_scene_particles'),
    (0x37BAD30, 0x2020840, 'sandbox_open_replay_folder'),
    (0x3753574 + 0x24, 0xC2DC40, 'sandbox_scene_animation'),
    (0x374CB8C + 0x24, 0xBB1D70, 'sandbox_scene_wind'),
    (0x374CAD4 + 0x24, 0xBB1BC0, 'sandbox_scene_wave'),
    (0x3742F20 + 0x24, 0xAF6220, 'sandbox_scene_trees'),
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
INLINE_JUMPS = (
    (0xAF5450, bytes.fromhex('8b338b8e94000000'), 'sandbox_particle_lifetime'),
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
    '439eee8ea03ebf176694d8f27a1ed51c9c588bebfedb80327f63aea9424dfc44': {
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
    '2fbbf066bb74a01767aa0708a076a14587f59e305a14b7ef89d87fa2e038c265': {
        'sandbox_keep_replay_markers': 0x4b2073e,
        'sandbox_graphics_page': 0x4b20f01,
        'sandbox_blur_material': 0x4b207a0,
        'sandbox_refresh_score': 0x4b1ff41,
        'sandbox_message': 0x4b20045,
        'sandbox_reset': 0x4b202e8,
        'sandbox_frame': 0x4b20385,
        'sandbox_draw': 0x4b20884,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fe02,
        'sandbox_binding_caption': 0x4b1fc8a,
        'sandbox_binding_name': 0x4b1fc4f,
        'sandbox_set_binding': 0x4b1fd12,
        'sandbox_reset_bindings': 0x4b1fcc5,
        'sandbox_notice_attach': 0x4b1ffc0,
        'sandbox_notice_detach': 0x4b1ffff,
        'sandbox_project_markers': 0x4b205d5,
        'sandbox_gun_marker': 0x4b203fe,
        'sandbox_blur_slider': 0x4b20fe4,
        'room_header_description': 0x4b20ce6,
        'room_load_description': 0x4b20c25,
        'room_get_description': 0x4b20d85,
        'sandbox_move': 0x4b20466,
        'sandbox_stop': 0x4b2055f,
    },
    'e5d88c07d956c128d9713c53bb5cf69090459e30182ffabdb136e8787d3b504b': {
        'sandbox_keep_replay_markers': 0x4b2075d,
        'sandbox_graphics_page': 0x4b20f20,
        'sandbox_blur_material': 0x4b207bf,
        'sandbox_refresh_score': 0x4b1ff27,
        'sandbox_message': 0x4b2002b,
        'sandbox_reset': 0x4b20307,
        'sandbox_frame': 0x4b203a4,
        'sandbox_draw': 0x4b208a3,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fde8,
        'sandbox_binding_caption': 0x4b1fc70,
        'sandbox_binding_name': 0x4b1fc35,
        'sandbox_set_binding': 0x4b1fcf8,
        'sandbox_reset_bindings': 0x4b1fcab,
        'sandbox_notice_attach': 0x4b1ffa6,
        'sandbox_notice_detach': 0x4b1ffe5,
        'sandbox_project_markers': 0x4b205f4,
        'sandbox_gun_marker': 0x4b2041d,
        'sandbox_blur_slider': 0x4b21003,
        'room_header_description': 0x4b20d05,
        'room_load_description': 0x4b20c44,
        'room_get_description': 0x4b20da4,
        'sandbox_move': 0x4b20485,
        'sandbox_stop': 0x4b2057e,
    },
    '90c289d9213539a0ce2093cd4e9604483ffbd9705ec515978b218d93bc812790': {
        'sandbox_keep_replay_markers': 0x4b20781,
        'sandbox_graphics_page': 0x4b20f44,
        'sandbox_blur_material': 0x4b207e3,
        'sandbox_refresh_score': 0x4b1ff4b,
        'sandbox_message': 0x4b2004f,
        'sandbox_reset': 0x4b2032b,
        'sandbox_frame': 0x4b203c8,
        'sandbox_draw': 0x4b208c7,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fe0c,
        'sandbox_binding_caption': 0x4b1fc94,
        'sandbox_binding_name': 0x4b1fc59,
        'sandbox_set_binding': 0x4b1fd1c,
        'sandbox_reset_bindings': 0x4b1fccf,
        'sandbox_notice_attach': 0x4b1ffca,
        'sandbox_notice_detach': 0x4b20009,
        'sandbox_project_markers': 0x4b20618,
        'sandbox_gun_marker': 0x4b20441,
        'sandbox_blur_slider': 0x4b21027,
        'room_header_description': 0x4b20d29,
        'room_load_description': 0x4b20c68,
        'room_get_description': 0x4b20dc8,
        'sandbox_move': 0x4b204a9,
        'sandbox_stop': 0x4b205a2,
    },
    '7b4d05ca1caa9eb6f6a3cbae66cf88fac115ea621b2de99b184ade90e10c24c5': {
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20781,
        'sandbox_graphics_page': 0x4b20f44,
        'sandbox_blur_material': 0x4b207e3,
        'sandbox_refresh_score': 0x4b1ff4b,
        'sandbox_message': 0x4b2004f,
        'sandbox_reset': 0x4b2032b,
        'sandbox_frame': 0x4b203c8,
        'sandbox_draw': 0x4b208c7,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fe0c,
        'sandbox_binding_caption': 0x4b1fc94,
        'sandbox_binding_name': 0x4b1fc59,
        'sandbox_set_binding': 0x4b1fd1c,
        'sandbox_reset_bindings': 0x4b1fccf,
        'sandbox_notice_attach': 0x4b1ffca,
        'sandbox_notice_detach': 0x4b20009,
        'sandbox_project_markers': 0x4b20618,
        'sandbox_gun_marker': 0x4b20441,
        'sandbox_blur_slider': 0x4b21027,
        'room_header_description': 0x4b20d29,
        'room_load_description': 0x4b20c68,
        'room_get_description': 0x4b20dc8,
        'sandbox_move': 0x4b204a9,
        'sandbox_stop': 0x4b205a2,
    },
    'b55b41742ebbc5816722a51bc7092fb6d640b80af8c0097e81c20c2a0ce39022': {
        'boss_screen_root': 0x4b226d9,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20778,
        'sandbox_graphics_page': 0x4b20f3b,
        'sandbox_blur_material': 0x4b207da,
        'sandbox_refresh_score': 0x4b1fed8,
        'sandbox_message': 0x4b1ffdc,
        'sandbox_reset': 0x4b202e8,
        'sandbox_frame': 0x4b203b1,
        'sandbox_draw': 0x4b208be,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd99,
        'sandbox_binding_caption': 0x4b1fc21,
        'sandbox_binding_name': 0x4b1fbe6,
        'sandbox_set_binding': 0x4b1fca9,
        'sandbox_reset_bindings': 0x4b1fc5c,
        'sandbox_notice_attach': 0x4b1ff57,
        'sandbox_notice_detach': 0x4b1ff96,
        'sandbox_project_markers': 0x4b2060f,
        'sandbox_gun_marker': 0x4b20438,
        'sandbox_blur_slider': 0x4b2101e,
        'room_header_description': 0x4b20d20,
        'room_load_description': 0x4b20c5f,
        'room_get_description': 0x4b20dbf,
        'sandbox_move': 0x4b204a0,
        'sandbox_stop': 0x4b20599,
    },
    '60ca059183495048df198d664aeaf1c5af67d7c4ae5b9b5a77db53eb17e434bd': {
        'boss_stock_panel': 0x4b227a2,
        'boss_screen_root': 0x4b22719,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2077a,
        'sandbox_graphics_page': 0x4b20f3d,
        'sandbox_blur_material': 0x4b207dc,
        'sandbox_refresh_score': 0x4b1fece,
        'sandbox_message': 0x4b1ffd2,
        'sandbox_reset': 0x4b202de,
        'sandbox_frame': 0x4b203b3,
        'sandbox_draw': 0x4b208c0,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd8f,
        'sandbox_binding_caption': 0x4b1fc17,
        'sandbox_binding_name': 0x4b1fbdc,
        'sandbox_set_binding': 0x4b1fc9f,
        'sandbox_reset_bindings': 0x4b1fc52,
        'sandbox_notice_attach': 0x4b1ff4d,
        'sandbox_notice_detach': 0x4b1ff8c,
        'sandbox_project_markers': 0x4b20611,
        'sandbox_gun_marker': 0x4b2043a,
        'sandbox_blur_slider': 0x4b21020,
        'room_header_description': 0x4b20d22,
        'room_load_description': 0x4b20c61,
        'room_get_description': 0x4b20dc1,
        'sandbox_move': 0x4b204a2,
        'sandbox_stop': 0x4b2059b,
    },
    '7ed4eaef94a5c0886dc0d84976514695a68d13dbc8486cb4f2f9d7eb17296601': {
        'boss_stock_panel': 0x4b227b1,
        'boss_screen_root': 0x4b22728,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2077a,
        'sandbox_graphics_page': 0x4b20f3d,
        'sandbox_blur_material': 0x4b207dc,
        'sandbox_refresh_score': 0x4b1fece,
        'sandbox_message': 0x4b1ffd2,
        'sandbox_reset': 0x4b202de,
        'sandbox_frame': 0x4b203b3,
        'sandbox_draw': 0x4b208c0,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd8f,
        'sandbox_binding_caption': 0x4b1fc17,
        'sandbox_binding_name': 0x4b1fbdc,
        'sandbox_set_binding': 0x4b1fc9f,
        'sandbox_reset_bindings': 0x4b1fc52,
        'sandbox_notice_attach': 0x4b1ff4d,
        'sandbox_notice_detach': 0x4b1ff8c,
        'sandbox_project_markers': 0x4b20611,
        'sandbox_gun_marker': 0x4b2043a,
        'sandbox_blur_slider': 0x4b21020,
        'room_header_description': 0x4b20d22,
        'room_load_description': 0x4b20c61,
        'room_get_description': 0x4b20dc1,
        'sandbox_move': 0x4b204a2,
        'sandbox_stop': 0x4b2059b,
    },
    'ec2eb8266559abdd785efb14a6354b55c653849548db51a3ce9c77f3f49606bf': {
        'boss_stock_panel': 0x4b22816,
        'boss_screen_root': 0x4b2278d,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2078a,
        'sandbox_graphics_page': 0x4b20f4d,
        'sandbox_blur_material': 0x4b207ec,
        'sandbox_refresh_score': 0x4b1fed8,
        'sandbox_message': 0x4b1ffdc,
        'sandbox_reset': 0x4b202e8,
        'sandbox_frame': 0x4b203c3,
        'sandbox_draw': 0x4b208d0,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd99,
        'sandbox_binding_caption': 0x4b1fc21,
        'sandbox_binding_name': 0x4b1fbe6,
        'sandbox_set_binding': 0x4b1fca9,
        'sandbox_reset_bindings': 0x4b1fc5c,
        'sandbox_notice_attach': 0x4b1ff57,
        'sandbox_notice_detach': 0x4b1ff96,
        'sandbox_project_markers': 0x4b20621,
        'sandbox_gun_marker': 0x4b2044a,
        'sandbox_blur_slider': 0x4b21030,
        'room_header_description': 0x4b20d32,
        'room_load_description': 0x4b20c71,
        'room_get_description': 0x4b20dd1,
        'sandbox_move': 0x4b204b2,
        'sandbox_stop': 0x4b205ab,
    },
    'cf95e316b41fe904aef543d8fce2bc552138d10aeeb6d74a8a6ee62c3f67ac84': {
        'boss_stock_panel': 0x4b22816,
        'boss_screen_root': 0x4b2278d,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2077c,
        'sandbox_graphics_page': 0x4b20f59,
        'sandbox_blur_material': 0x4b207de,
        'sandbox_refresh_score': 0x4b1fed8,
        'sandbox_message': 0x4b1ffdc,
        'sandbox_reset': 0x4b202e8,
        'sandbox_frame': 0x4b203c3,
        'sandbox_draw': 0x4b208c2,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd99,
        'sandbox_binding_caption': 0x4b1fc21,
        'sandbox_binding_name': 0x4b1fbe6,
        'sandbox_set_binding': 0x4b1fca9,
        'sandbox_reset_bindings': 0x4b1fc5c,
        'sandbox_notice_attach': 0x4b1ff57,
        'sandbox_notice_detach': 0x4b1ff96,
        'sandbox_project_markers': 0x4b20613,
        'sandbox_gun_marker': 0x4b2043c,
        'sandbox_blur_slider': 0x4b2103c,
        'room_header_description': 0x4b20d3e,
        'room_load_description': 0x4b20c7d,
        'room_get_description': 0x4b20ddd,
        'sandbox_move': 0x4b204a4,
        'sandbox_stop': 0x4b2059d,
    },
    'f6cf366c1429643670347c99498abfddbe3eddb7a7bbbbe55c77e2029ff90b6a': {
        'boss_stock_panel': 0x4b22873,
        'boss_screen_root': 0x4b227ea,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2077c,
        'sandbox_graphics_page': 0x4b20f59,
        'sandbox_blur_material': 0x4b207de,
        'sandbox_refresh_score': 0x4b1fed8,
        'sandbox_message': 0x4b1ffdc,
        'sandbox_reset': 0x4b202e8,
        'sandbox_frame': 0x4b203c3,
        'sandbox_draw': 0x4b208c2,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd99,
        'sandbox_binding_caption': 0x4b1fc21,
        'sandbox_binding_name': 0x4b1fbe6,
        'sandbox_set_binding': 0x4b1fca9,
        'sandbox_reset_bindings': 0x4b1fc5c,
        'sandbox_notice_attach': 0x4b1ff57,
        'sandbox_notice_detach': 0x4b1ff96,
        'sandbox_project_markers': 0x4b20613,
        'sandbox_gun_marker': 0x4b2043c,
        'sandbox_blur_slider': 0x4b2103c,
        'room_header_description': 0x4b20d3e,
        'room_load_description': 0x4b20c7d,
        'room_get_description': 0x4b20ddd,
        'sandbox_move': 0x4b204a4,
        'sandbox_stop': 0x4b2059d,
    },
    '54bb379d5c6d4414891290ead6f587af863b20f4dcf0d4a1e70bc9b4c5885f5c': {
        'boss_stock_panel': 0x4b228db,
        'boss_screen_root': 0x4b22818,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20778,
        'sandbox_graphics_page': 0x4b20f55,
        'sandbox_blur_material': 0x4b207da,
        'sandbox_refresh_score': 0x4b1fece,
        'sandbox_message': 0x4b1ffd2,
        'sandbox_reset': 0x4b202de,
        'sandbox_frame': 0x4b203bf,
        'sandbox_draw': 0x4b208be,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd8f,
        'sandbox_binding_caption': 0x4b1fc17,
        'sandbox_binding_name': 0x4b1fbdc,
        'sandbox_set_binding': 0x4b1fc9f,
        'sandbox_reset_bindings': 0x4b1fc52,
        'sandbox_notice_attach': 0x4b1ff4d,
        'sandbox_notice_detach': 0x4b1ff8c,
        'sandbox_project_markers': 0x4b2060f,
        'sandbox_gun_marker': 0x4b20438,
        'sandbox_blur_slider': 0x4b21038,
        'room_header_description': 0x4b20d3a,
        'room_load_description': 0x4b20c79,
        'room_get_description': 0x4b20dd9,
        'sandbox_move': 0x4b204a0,
        'sandbox_stop': 0x4b20599,
    },
    'd94552ef253071421170506d65fd2ecc1104f6e6c3a5f8c37fda60ab6ec9d17d': {
        'boss_stock_panel': 0x4b228df,
        'boss_screen_root': 0x4b2281c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20772,
        'sandbox_graphics_page': 0x4b20f55,
        'sandbox_blur_material': 0x4b207d4,
        'sandbox_refresh_score': 0x4b1fec8,
        'sandbox_message': 0x4b1ffcc,
        'sandbox_reset': 0x4b202d8,
        'sandbox_frame': 0x4b203b9,
        'sandbox_draw': 0x4b208b8,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b1fd89,
        'sandbox_binding_caption': 0x4b1fc11,
        'sandbox_binding_name': 0x4b1fbd6,
        'sandbox_set_binding': 0x4b1fc99,
        'sandbox_reset_bindings': 0x4b1fc4c,
        'sandbox_notice_attach': 0x4b1ff47,
        'sandbox_notice_detach': 0x4b1ff86,
        'sandbox_project_markers': 0x4b20609,
        'sandbox_gun_marker': 0x4b20432,
        'sandbox_blur_slider': 0x4b21038,
        'room_header_description': 0x4b20d3a,
        'room_load_description': 0x4b20c79,
        'room_get_description': 0x4b20dd9,
        'sandbox_move': 0x4b2049a,
        'sandbox_stop': 0x4b20593,
    },
    '19a3e250125b95883752a51caf0de50e50b9e69c2a09099d6fed2c2a8435b6ed': {
        'boss_stock_panel': 0x4b228df,
        'boss_screen_root': 0x4b2281c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b206a8,
        'sandbox_graphics_page': 0x4b20e8b,
        'sandbox_blur_material': 0x4b2070a,
        'sandbox_refresh_score': 0x4b1fdee,
        'sandbox_message': 0x4b1fef2,
        'sandbox_reset': 0x4b2020e,
        'sandbox_frame': 0x4b202ef,
        'sandbox_draw': 0x4b207ee,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b229e6,
        'sandbox_binding_caption': 0x4b1fc76,
        'sandbox_binding_name': 0x4b1fc3b,
        'sandbox_set_binding': 0x4b1fcfe,
        'sandbox_reset_bindings': 0x4b1fcb1,
        'sandbox_notice_attach': 0x4b1fe6d,
        'sandbox_notice_detach': 0x4b1feac,
        'sandbox_project_markers': 0x4b2053f,
        'sandbox_gun_marker': 0x4b20368,
        'sandbox_blur_slider': 0x4b20f6e,
        'room_header_description': 0x4b20c70,
        'room_load_description': 0x4b20baf,
        'room_get_description': 0x4b20d0f,
        'sandbox_move': 0x4b203d0,
        'sandbox_stop': 0x4b204c9,
    },
    'bb2a0d02c74d0679c1c91c963c68b491270a46dfbf1c58eb85cbf3ef009d3dfe': {
        'boss_stock_panel': 0x4b228df,
        'boss_screen_root': 0x4b2281c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20699,
        'sandbox_graphics_page': 0x4b20e7c,
        'sandbox_blur_material': 0x4b206fb,
        'sandbox_refresh_score': 0x4b1fddf,
        'sandbox_message': 0x4b1fee3,
        'sandbox_reset': 0x4b201ff,
        'sandbox_frame': 0x4b202e0,
        'sandbox_draw': 0x4b207df,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b229e6,
        'sandbox_binding_caption': 0x4b1fc67,
        'sandbox_binding_name': 0x4b1fc2c,
        'sandbox_set_binding': 0x4b1fcef,
        'sandbox_reset_bindings': 0x4b1fca2,
        'sandbox_notice_attach': 0x4b1fe5e,
        'sandbox_notice_detach': 0x4b1fe9d,
        'sandbox_project_markers': 0x4b20530,
        'sandbox_gun_marker': 0x4b20359,
        'sandbox_blur_slider': 0x4b20f5f,
        'room_header_description': 0x4b20c61,
        'room_load_description': 0x4b20ba0,
        'room_get_description': 0x4b20d00,
        'sandbox_move': 0x4b203c1,
        'sandbox_stop': 0x4b204ba,
    },
    '594ab15bd9e62c4a640c1f6940fd8c440ec3630b7331391dced1aac416629991': {
        'boss_stock_panel': 0x4b228f1,
        'boss_screen_root': 0x4b2282e,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b206c8,
        'sandbox_graphics_page': 0x4b20eab,
        'sandbox_blur_material': 0x4b2072a,
        'sandbox_refresh_score': 0x4b1fddf,
        'sandbox_message': 0x4b1fee3,
        'sandbox_reset': 0x4b2022e,
        'sandbox_frame': 0x4b2030f,
        'sandbox_draw': 0x4b2080e,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b22af4,
        'sandbox_binding_caption': 0x4b1fc67,
        'sandbox_binding_name': 0x4b1fc2c,
        'sandbox_set_binding': 0x4b1fcef,
        'sandbox_reset_bindings': 0x4b1fca2,
        'sandbox_notice_attach': 0x4b1fe5e,
        'sandbox_notice_detach': 0x4b1fe9d,
        'sandbox_project_markers': 0x4b2055f,
        'sandbox_gun_marker': 0x4b20388,
        'sandbox_blur_slider': 0x4b20f8e,
        'room_header_description': 0x4b20c90,
        'room_load_description': 0x4b20bcf,
        'room_get_description': 0x4b20d2f,
        'sandbox_move': 0x4b203f0,
        'sandbox_stop': 0x4b204e9,
    },
    'f9b690f237a34f84a9143b0286508969455e15aa20c27682c36a112ccc925479': {
        'sandbox_load_keyboard': 0x4b22c19,
        'sandbox_save_keyboard': 0x4b22c5e,
        'boss_stock_panel': 0x4b228f1,
        'boss_screen_root': 0x4b2282e,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b206c8,
        'sandbox_graphics_page': 0x4b20eab,
        'sandbox_blur_material': 0x4b2072a,
        'sandbox_refresh_score': 0x4b1fddf,
        'sandbox_message': 0x4b1fee3,
        'sandbox_reset': 0x4b2022e,
        'sandbox_frame': 0x4b2030f,
        'sandbox_draw': 0x4b2080e,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b22d15,
        'sandbox_binding_caption': 0x4b1fc67,
        'sandbox_binding_name': 0x4b1fc2c,
        'sandbox_set_binding': 0x4b1fcef,
        'sandbox_reset_bindings': 0x4b1fca2,
        'sandbox_notice_attach': 0x4b1fe5e,
        'sandbox_notice_detach': 0x4b1fe9d,
        'sandbox_project_markers': 0x4b2055f,
        'sandbox_gun_marker': 0x4b20388,
        'sandbox_blur_slider': 0x4b20f8e,
        'room_header_description': 0x4b20c90,
        'room_load_description': 0x4b20bcf,
        'room_get_description': 0x4b20d2f,
        'sandbox_move': 0x4b203f0,
        'sandbox_stop': 0x4b204e9,
    },
    '74312cd3a6864d2425af2b362a04a7a435622ac74748a41e167ba3c2552cc702': {
        'sandbox_load_keyboard': 0x4b22d79,
        'sandbox_save_keyboard': 0x4b22dbe,
        'boss_stock_panel': 0x4b22a17,
        'boss_screen_root': 0x4b22954,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2038f,
        'sandbox_graphics_page': 0x4b20b7b,
        'sandbox_blur_material': 0x4b203f1,
        'sandbox_refresh_score': 0x4b1fde5,
        'sandbox_message': 0x4b22fb4,
        'sandbox_reset': 0x4b1fee9,
        'sandbox_frame': 0x4b1ffd6,
        'sandbox_draw': 0x4b204d5,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b22e75,
        'sandbox_binding_caption': 0x4b1fc6d,
        'sandbox_binding_name': 0x4b1fc32,
        'sandbox_set_binding': 0x4b1fcf5,
        'sandbox_reset_bindings': 0x4b1fca8,
        'sandbox_notice_attach': 0x4b1fe64,
        'sandbox_notice_detach': 0x4b1fea3,
        'sandbox_project_markers': 0x4b20226,
        'sandbox_gun_marker': 0x4b2004f,
        'sandbox_blur_slider': 0x4b20c5e,
        'room_header_description': 0x4b20960,
        'room_load_description': 0x4b2089f,
        'room_get_description': 0x4b209ff,
        'sandbox_move': 0x4b200b7,
        'sandbox_stop': 0x4b201b0,
    },
    '40daa946472993565494080760cc641e374d9092064bdef42807738203b09298': {
        'sandbox_load_keyboard': 0x4b22d79,
        'sandbox_save_keyboard': 0x4b22dbe,
        'boss_stock_panel': 0x4b22a0c,
        'boss_screen_root': 0x4b22949,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2038f,
        'sandbox_graphics_page': 0x4b20b7b,
        'sandbox_blur_material': 0x4b203f1,
        'sandbox_refresh_score': 0x4b1fde5,
        'sandbox_message': 0x4b22fb4,
        'sandbox_reset': 0x4b1fee9,
        'sandbox_frame': 0x4b1ffd6,
        'sandbox_draw': 0x4b204d5,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b22e75,
        'sandbox_binding_caption': 0x4b1fc6d,
        'sandbox_binding_name': 0x4b1fc32,
        'sandbox_set_binding': 0x4b1fcf5,
        'sandbox_reset_bindings': 0x4b1fca8,
        'sandbox_notice_attach': 0x4b1fe64,
        'sandbox_notice_detach': 0x4b1fea3,
        'sandbox_project_markers': 0x4b20226,
        'sandbox_gun_marker': 0x4b2004f,
        'sandbox_blur_slider': 0x4b20c5e,
        'room_header_description': 0x4b20960,
        'room_load_description': 0x4b2089f,
        'room_get_description': 0x4b209ff,
        'sandbox_move': 0x4b200b7,
        'sandbox_stop': 0x4b201b0,
    },
    'efcb00347f90f938158e4e92f6e127920f3b060168d28072db8efa15a4f66b2e': {
        'sandbox_load_keyboard': 0x4b22e5b,
        'sandbox_save_keyboard': 0x4b22ea0,
        'boss_stock_panel': 0x4b22a0c,
        'boss_screen_root': 0x4b22949,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203c8,
        'sandbox_graphics_page': 0x4b20bb4,
        'sandbox_blur_material': 0x4b2042a,
        'sandbox_refresh_score': 0x4b1fe1e,
        'sandbox_message': 0x4b2300e,
        'sandbox_reset': 0x4b1ff22,
        'sandbox_frame': 0x4b2000f,
        'sandbox_draw': 0x4b2050e,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b22ecf,
        'sandbox_binding_caption': 0x4b1fc6d,
        'sandbox_binding_name': 0x4b1fc32,
        'sandbox_set_binding': 0x4b1fcfe,
        'sandbox_reset_bindings': 0x4b1fca8,
        'sandbox_notice_attach': 0x4b1fe9d,
        'sandbox_notice_detach': 0x4b1fedc,
        'sandbox_project_markers': 0x4b2025f,
        'sandbox_gun_marker': 0x4b20088,
        'sandbox_blur_slider': 0x4b20c97,
        'room_header_description': 0x4b20999,
        'room_load_description': 0x4b208d8,
        'room_get_description': 0x4b20a38,
        'sandbox_move': 0x4b200f0,
        'sandbox_stop': 0x4b201e9,
    },
    '59681cc50583ad4680913adf312ab1b5ba78eeee9ce4e8d74d6e57b807ce8040': {
        'sandbox_load_keyboard': 0x4b23499,
        'sandbox_save_keyboard': 0x4b234de,
        'boss_stock_panel': 0x4b22af2,
        'boss_screen_root': 0x4b22949,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203e1,
        'sandbox_graphics_page': 0x4b20cef,
        'sandbox_blur_material': 0x4b20443,
        'sandbox_refresh_score': 0x4b1feb8,
        'sandbox_message': 0x4b2364c,
        'sandbox_reset': 0x4b23b73,
        'sandbox_frame': 0x4b1ffbc,
        'sandbox_draw': 0x4b20616,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b2350d,
        'sandbox_binding_caption': 0x4b1fd07,
        'sandbox_binding_name': 0x4b1fccc,
        'sandbox_set_binding': 0x4b1fd98,
        'sandbox_reset_bindings': 0x4b1fd42,
        'sandbox_notice_attach': 0x4b1ff37,
        'sandbox_notice_detach': 0x4b1ff76,
        'sandbox_project_markers': 0x4b20278,
        'sandbox_gun_marker': 0x4b200a1,
        'sandbox_blur_slider': 0x4b20dd2,
        'room_header_description': 0x4b20ad4,
        'room_load_description': 0x4b20a13,
        'room_get_description': 0x4b20b73,
        'sandbox_move': 0x4b20109,
        'sandbox_stop': 0x4b20202,
    },
    'b887d5adba62aa6759c372df146058a06febef0df402ffe59436c71274642c96': {
        'sandbox_load_keyboard': 0x4b23499,
        'sandbox_save_keyboard': 0x4b234de,
        'boss_stock_panel': 0x4b22af2,
        'boss_screen_root': 0x4b22949,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203e1,
        'sandbox_graphics_page': 0x4b20cef,
        'sandbox_blur_material': 0x4b20443,
        'sandbox_refresh_score': 0x4b1feb8,
        'sandbox_message': 0x4b2364c,
        'sandbox_reset': 0x4b23b73,
        'sandbox_frame': 0x4b1ffbc,
        'sandbox_draw': 0x4b20616,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b2350d,
        'sandbox_binding_caption': 0x4b1fd07,
        'sandbox_binding_name': 0x4b1fccc,
        'sandbox_set_binding': 0x4b1fd98,
        'sandbox_reset_bindings': 0x4b1fd42,
        'sandbox_notice_attach': 0x4b1ff37,
        'sandbox_notice_detach': 0x4b1ff76,
        'sandbox_project_markers': 0x4b20278,
        'sandbox_gun_marker': 0x4b200a1,
        'sandbox_blur_slider': 0x4b20dd2,
        'room_header_description': 0x4b20ad4,
        'room_load_description': 0x4b20a13,
        'room_get_description': 0x4b20b73,
        'sandbox_move': 0x4b20109,
        'sandbox_stop': 0x4b20202,
    },
    '6b9397f730e43284d9c7efa83a2b9701e68e323cb78a72130d4c3f278005dacd': {
        'sandbox_load_keyboard': 0x4b234fd,
        'sandbox_save_keyboard': 0x4b23542,
        'boss_stock_panel': 0x4b22aea,
        'boss_screen_root': 0x4b22949,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20375,
        'sandbox_graphics_page': 0x4b20caa,
        'sandbox_blur_material': 0x4b203d7,
        'sandbox_refresh_score': 0x4b1feb8,
        'sandbox_message': 0x4b236b0,
        'sandbox_reset': 0x4b23bfa,
        'sandbox_frame': 0x4b1ffbc,
        'sandbox_draw': 0x4b205aa,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23571,
        'sandbox_binding_caption': 0x4b1fd07,
        'sandbox_binding_name': 0x4b1fccc,
        'sandbox_set_binding': 0x4b1fd98,
        'sandbox_reset_bindings': 0x4b1fd42,
        'sandbox_notice_attach': 0x4b1ff37,
        'sandbox_notice_detach': 0x4b1ff76,
        'sandbox_project_markers': 0x4b2020c,
        'sandbox_gun_marker': 0x4b20035,
        'sandbox_blur_slider': 0x4b20d8d,
        'room_header_description': 0x4b20a8f,
        'room_load_description': 0x4b209ce,
        'room_get_description': 0x4b20b2e,
        'sandbox_move': 0x4b2009d,
        'sandbox_stop': 0x4b20196,
    },
    'fc4c1bf3f67057f64331d6e3e2019b284ec6b54c2842042196f33640c3cc2435': {
        'sandbox_load_keyboard': 0x4b23612,
        'sandbox_save_keyboard': 0x4b23657,
        'boss_stock_panel': 0x4b22aea,
        'boss_screen_root': 0x4b22949,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20398,
        'sandbox_graphics_page': 0x4b20cec,
        'sandbox_blur_material': 0x4b203fa,
        'sandbox_refresh_score': 0x4b1fedb,
        'sandbox_message': 0x4b237c5,
        'sandbox_reset': 0x4b23d1b,
        'sandbox_frame': 0x4b1ffdf,
        'sandbox_draw': 0x4b205cd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23686,
        'sandbox_binding_caption': 0x4b1fd2a,
        'sandbox_binding_name': 0x4b1fcef,
        'sandbox_set_binding': 0x4b1fdbb,
        'sandbox_reset_bindings': 0x4b1fd65,
        'sandbox_notice_attach': 0x4b1ff5a,
        'sandbox_notice_detach': 0x4b1ff99,
        'sandbox_project_markers': 0x4b2022f,
        'sandbox_gun_marker': 0x4b20058,
        'sandbox_blur_slider': 0x4b20dcf,
        'room_header_description': 0x4b20ad1,
        'room_load_description': 0x4b20a10,
        'room_get_description': 0x4b20b70,
        'sandbox_move': 0x4b200c0,
        'sandbox_stop': 0x4b201b9,
    },
    'd63f82760c48c028fc4d40b62c3fb179c1ae55615aec6af8d88495368e554ba4': {
        'sandbox_tracer_update': 0x4b237c5,
        'sandbox_load_keyboard': 0x4b23612,
        'sandbox_save_keyboard': 0x4b23657,
        'boss_stock_panel': 0x4b22aea,
        'boss_screen_root': 0x4b22949,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20398,
        'sandbox_graphics_page': 0x4b20cec,
        'sandbox_blur_material': 0x4b203fa,
        'sandbox_refresh_score': 0x4b1fedb,
        'sandbox_message': 0x4b2383a,
        'sandbox_reset': 0x4b23d90,
        'sandbox_frame': 0x4b1ffdf,
        'sandbox_draw': 0x4b205cd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23686,
        'sandbox_binding_caption': 0x4b1fd2a,
        'sandbox_binding_name': 0x4b1fcef,
        'sandbox_set_binding': 0x4b1fdbb,
        'sandbox_reset_bindings': 0x4b1fd65,
        'sandbox_notice_attach': 0x4b1ff5a,
        'sandbox_notice_detach': 0x4b1ff99,
        'sandbox_project_markers': 0x4b2022f,
        'sandbox_gun_marker': 0x4b20058,
        'sandbox_blur_slider': 0x4b20dcf,
        'room_header_description': 0x4b20ad1,
        'room_load_description': 0x4b20a10,
        'room_get_description': 0x4b20b70,
        'sandbox_move': 0x4b200c0,
        'sandbox_stop': 0x4b201b9,
    },
    '87b056839f0be9db9e7f94e89c1cc187bbde75687c45c3604330951f75b1fbaf': {
        'sandbox_tracer_update': 0x4b237c5,
        'sandbox_tracer_position': 0x4b2383a,
        'sandbox_load_keyboard': 0x4b23612,
        'sandbox_save_keyboard': 0x4b23657,
        'boss_stock_panel': 0x4b22aea,
        'boss_screen_root': 0x4b22949,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20398,
        'sandbox_graphics_page': 0x4b20cec,
        'sandbox_blur_material': 0x4b203fa,
        'sandbox_refresh_score': 0x4b1fedb,
        'sandbox_message': 0x4b23923,
        'sandbox_reset': 0x4b23e79,
        'sandbox_frame': 0x4b1ffdf,
        'sandbox_draw': 0x4b205cd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23686,
        'sandbox_binding_caption': 0x4b1fd2a,
        'sandbox_binding_name': 0x4b1fcef,
        'sandbox_set_binding': 0x4b1fdbb,
        'sandbox_reset_bindings': 0x4b1fd65,
        'sandbox_notice_attach': 0x4b1ff5a,
        'sandbox_notice_detach': 0x4b1ff99,
        'sandbox_project_markers': 0x4b2022f,
        'sandbox_gun_marker': 0x4b20058,
        'sandbox_blur_slider': 0x4b20dcf,
        'room_header_description': 0x4b20ad1,
        'room_load_description': 0x4b20a10,
        'room_get_description': 0x4b20b70,
        'sandbox_move': 0x4b200c0,
        'sandbox_stop': 0x4b201b9,
    },
    '0f04f3f47872911d0ced75351ef9b8ce2a9397761256fea86d91982f56b4e59a': {
        'sandbox_tracer_update': 0x4b2381c,
        'sandbox_tracer_position': 0x4b23891,
        'sandbox_scene_shader_clock': 0x4b239da,
        'sandbox_load_keyboard': 0x4b23669,
        'sandbox_save_keyboard': 0x4b236ae,
        'boss_stock_panel': 0x4b22aea,
        'boss_screen_root': 0x4b22949,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20398,
        'sandbox_graphics_page': 0x4b20cec,
        'sandbox_blur_material': 0x4b203fa,
        'sandbox_refresh_score': 0x4b1fedb,
        'sandbox_message': 0x4b23a69,
        'sandbox_reset': 0x4b23fc5,
        'sandbox_frame': 0x4b1ffdf,
        'sandbox_draw': 0x4b205cd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b236dd,
        'sandbox_binding_caption': 0x4b1fd2a,
        'sandbox_binding_name': 0x4b1fcef,
        'sandbox_set_binding': 0x4b1fdbb,
        'sandbox_reset_bindings': 0x4b1fd65,
        'sandbox_scene_animation': 0x4b2397a,
        'sandbox_scene_wind': 0x4b23992,
        'sandbox_scene_wave': 0x4b239aa,
        'sandbox_scene_trees': 0x4b239c2,
        'sandbox_notice_attach': 0x4b1ff5a,
        'sandbox_notice_detach': 0x4b1ff99,
        'sandbox_project_markers': 0x4b2022f,
        'sandbox_gun_marker': 0x4b20058,
        'sandbox_blur_slider': 0x4b20dcf,
        'room_header_description': 0x4b20ad1,
        'room_load_description': 0x4b20a10,
        'room_get_description': 0x4b20b70,
        'sandbox_move': 0x4b200c0,
        'sandbox_stop': 0x4b201b9,
    },
    'dbed056016d8a73f34de99beff35f59d45d6626eb1096747a6ed5b1303b164b5': {
        'sandbox_tracer_update': 0x4b239fb,
        'sandbox_tracer_position': 0x4b23a70,
        'sandbox_scene_shader_clock': 0x4b23bb9,
        'sandbox_load_keyboard': 0x4b23848,
        'sandbox_save_keyboard': 0x4b2388d,
        'boss_stock_panel': 0x4b22b4d,
        'boss_screen_root': 0x4b22af3,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203dc,
        'sandbox_graphics_page': 0x4b20b71,
        'sandbox_blur_material': 0x4b2438d,
        'sandbox_refresh_score': 0x4b1feeb,
        'sandbox_message': 0x4b23c48,
        'sandbox_reset': 0x4b24212,
        'sandbox_frame': 0x4b1ffef,
        'sandbox_draw': 0x4b2043e,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b238bc,
        'sandbox_binding_caption': 0x4b1fd3a,
        'sandbox_binding_name': 0x4b1fcff,
        'sandbox_set_binding': 0x4b1fdcb,
        'sandbox_reset_bindings': 0x4b1fd75,
        'sandbox_scene_animation': 0x4b23b59,
        'sandbox_scene_wind': 0x4b23b71,
        'sandbox_scene_wave': 0x4b23b89,
        'sandbox_scene_trees': 0x4b23ba1,
        'sandbox_notice_attach': 0x4b1ff6a,
        'sandbox_notice_detach': 0x4b1ffa9,
        'sandbox_project_markers': 0x4b20273,
        'sandbox_gun_marker': 0x4b2009c,
        'sandbox_blur_slider': 0x4b20c54,
        'room_header_description': 0x4b20956,
        'room_load_description': 0x4b20895,
        'room_get_description': 0x4b209f5,
        'sandbox_move': 0x4b20104,
        'sandbox_stop': 0x4b201fd,
    },
    '0eeaadc65daa4eac62d497c7506fb102bf405784e04285b56e5be31a154c8628': {
        'sandbox_tracer_update': 0x4b23b0e,
        'sandbox_tracer_position': 0x4b23b83,
        'sandbox_scene_shader_clock': 0x4b23ccc,
        'sandbox_load_keyboard': 0x4b2395b,
        'sandbox_save_keyboard': 0x4b239a0,
        'boss_stock_panel': 0x4b22b4d,
        'boss_screen_root': 0x4b22af3,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203dc,
        'sandbox_graphics_page': 0x4b20b71,
        'sandbox_blur_material': 0x4b244a0,
        'sandbox_scene_graded_blit': 0x4b246df,
        'sandbox_scene_blit': 0x4b24673,
        'sandbox_refresh_score': 0x4b1feeb,
        'sandbox_message': 0x4b23d5b,
        'sandbox_reset': 0x4b24325,
        'sandbox_frame': 0x4b1ffef,
        'sandbox_draw': 0x4b2043e,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b239cf,
        'sandbox_binding_caption': 0x4b1fd3a,
        'sandbox_binding_name': 0x4b1fcff,
        'sandbox_set_binding': 0x4b1fdcb,
        'sandbox_reset_bindings': 0x4b1fd75,
        'sandbox_scene_animation': 0x4b23c6c,
        'sandbox_scene_wind': 0x4b23c84,
        'sandbox_scene_wave': 0x4b23c9c,
        'sandbox_scene_trees': 0x4b23cb4,
        'sandbox_notice_attach': 0x4b1ff6a,
        'sandbox_notice_detach': 0x4b1ffa9,
        'sandbox_project_markers': 0x4b20273,
        'sandbox_gun_marker': 0x4b2009c,
        'sandbox_blur_slider': 0x4b20c54,
        'room_header_description': 0x4b20956,
        'room_load_description': 0x4b20895,
        'room_get_description': 0x4b209f5,
        'sandbox_move': 0x4b20104,
        'sandbox_stop': 0x4b201fd,
    },
    'aa05a1f91a5fff4a70c6c63c3df6621c7fe0f8ca3d34e1b33a4441ccc4310d2c': {
        'sandbox_tracer_update': 0x4b23ca0,
        'sandbox_tracer_position': 0x4b23d15,
        'sandbox_scene_shader_clock': 0x4b23e5e,
        'sandbox_load_keyboard': 0x4b2395b,
        'sandbox_save_keyboard': 0x4b239a0,
        'boss_stock_panel': 0x4b22b4d,
        'boss_screen_root': 0x4b22af3,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203ec,
        'sandbox_graphics_page': 0x4b20b81,
        'sandbox_blur_material': 0x4b2463c,
        'sandbox_scene_graded_blit': 0x4b2487b,
        'sandbox_scene_blit': 0x4b2480f,
        'sandbox_refresh_score': 0x4b1feeb,
        'sandbox_message': 0x4b23eed,
        'sandbox_reset': 0x4b244c1,
        'sandbox_frame': 0x4b1ffef,
        'sandbox_draw': 0x4b2044e,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b239cf,
        'sandbox_binding_caption': 0x4b1fd3a,
        'sandbox_binding_name': 0x4b1fcff,
        'sandbox_set_binding': 0x4b1fdcb,
        'sandbox_reset_bindings': 0x4b1fd75,
        'sandbox_open_replay_folder': 0x4b23b0e,
        'sandbox_scene_animation': 0x4b23dfe,
        'sandbox_scene_wind': 0x4b23e16,
        'sandbox_scene_wave': 0x4b23e2e,
        'sandbox_scene_trees': 0x4b23e46,
        'sandbox_notice_attach': 0x4b1ff6a,
        'sandbox_notice_detach': 0x4b1ffa9,
        'sandbox_project_markers': 0x4b20283,
        'sandbox_gun_marker': 0x4b2009c,
        'sandbox_blur_slider': 0x4b20c64,
        'room_header_description': 0x4b20966,
        'room_load_description': 0x4b208a5,
        'room_get_description': 0x4b20a05,
        'sandbox_move': 0x4b20104,
        'sandbox_stop': 0x4b2020d,
    },
    '562c1ebfd5cc7880b4b675edc2f80975cd527b19ef65a22695d390332a6cd98b': {
        'sandbox_tracer_update': 0x4b23e80,
        'sandbox_tracer_position': 0x4b23ef5,
        'sandbox_scene_shader_clock': 0x4b2403e,
        'sandbox_load_keyboard': 0x4b23b3b,
        'sandbox_save_keyboard': 0x4b23b80,
        'boss_stock_panel': 0x4b22ca6,
        'boss_screen_root': 0x4b22c4c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203ec,
        'sandbox_graphics_page': 0x4b20b98,
        'sandbox_blur_material': 0x4b2487e,
        'sandbox_scene_graded_blit': 0x4b24abd,
        'sandbox_scene_blit': 0x4b24a51,
        'sandbox_refresh_score': 0x4b1feeb,
        'sandbox_message': 0x4b240cd,
        'sandbox_reset': 0x4b246ea,
        'sandbox_frame': 0x4b1ffef,
        'sandbox_draw': 0x4b2044e,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23baf,
        'sandbox_binding_caption': 0x4b1fd3a,
        'sandbox_binding_name': 0x4b1fcff,
        'sandbox_set_binding': 0x4b1fdcb,
        'sandbox_reset_bindings': 0x4b1fd75,
        'sandbox_open_replay_folder': 0x4b23cee,
        'sandbox_scene_animation': 0x4b23fde,
        'sandbox_scene_wind': 0x4b23ff6,
        'sandbox_scene_wave': 0x4b2400e,
        'sandbox_scene_trees': 0x4b24026,
        'sandbox_notice_attach': 0x4b1ff6a,
        'sandbox_notice_detach': 0x4b1ffa9,
        'sandbox_project_markers': 0x4b20283,
        'sandbox_gun_marker': 0x4b2009c,
        'sandbox_blur_slider': 0x4b20c7b,
        'room_header_description': 0x4b2097d,
        'room_load_description': 0x4b208bc,
        'room_get_description': 0x4b20a1c,
        'sandbox_move': 0x4b20104,
        'sandbox_stop': 0x4b2020d,
    },
    '70a00be4cd1da6e12cfff6099aec2e12ebf4318bccc1b18233a9faf8db5c4f86': {
        'sandbox_tracer_update': 0x4b23ea0,
        'sandbox_tracer_position': 0x4b23f15,
        'sandbox_scene_shader_clock': 0x4b2405e,
        'sandbox_load_keyboard': 0x4b23b5b,
        'sandbox_save_keyboard': 0x4b23ba0,
        'boss_stock_panel': 0x4b22d5f,
        'boss_screen_root': 0x4b22d05,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203ec,
        'sandbox_graphics_page': 0x4b20b98,
        'sandbox_blur_material': 0x4b2489e,
        'sandbox_scene_graded_blit': 0x4b24add,
        'sandbox_scene_blit': 0x4b24a71,
        'sandbox_refresh_score': 0x4b1feeb,
        'sandbox_message': 0x4b240ed,
        'sandbox_reset': 0x4b2470a,
        'sandbox_frame': 0x4b1ffef,
        'sandbox_draw': 0x4b2044e,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23bcf,
        'sandbox_binding_caption': 0x4b1fd3a,
        'sandbox_binding_name': 0x4b1fcff,
        'sandbox_set_binding': 0x4b1fdcb,
        'sandbox_reset_bindings': 0x4b1fd75,
        'sandbox_open_replay_folder': 0x4b23d0e,
        'sandbox_scene_animation': 0x4b23ffe,
        'sandbox_scene_wind': 0x4b24016,
        'sandbox_scene_wave': 0x4b2402e,
        'sandbox_scene_trees': 0x4b24046,
        'sandbox_notice_attach': 0x4b1ff6a,
        'sandbox_notice_detach': 0x4b1ffa9,
        'sandbox_project_markers': 0x4b20283,
        'sandbox_gun_marker': 0x4b2009c,
        'sandbox_blur_slider': 0x4b20c7b,
        'room_header_description': 0x4b2097d,
        'room_load_description': 0x4b208bc,
        'room_get_description': 0x4b20a1c,
        'sandbox_move': 0x4b20104,
        'sandbox_stop': 0x4b2020d,
    },
    'ea59ad04772091ee8e54919d028977f567053909354884e996f8ce87aef3332f': {
        'sandbox_tracer_update': 0x4b23e88,
        'sandbox_tracer_position': 0x4b23efd,
        'sandbox_scene_shader_clock': 0x4b24046,
        'sandbox_load_keyboard': 0x4b23b43,
        'sandbox_save_keyboard': 0x4b23b88,
        'boss_stock_panel': 0x4b22d5f,
        'boss_screen_root': 0x4b22d05,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203ec,
        'sandbox_graphics_page': 0x4b20b98,
        'sandbox_blur_material': 0x4b24886,
        'sandbox_scene_graded_blit': 0x4b24ac5,
        'sandbox_scene_blit': 0x4b24a59,
        'sandbox_refresh_score': 0x4b1feeb,
        'sandbox_message': 0x4b240d5,
        'sandbox_reset': 0x4b246f2,
        'sandbox_frame': 0x4b1ffef,
        'sandbox_draw': 0x4b2044e,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23bb7,
        'sandbox_binding_caption': 0x4b1fd3a,
        'sandbox_binding_name': 0x4b1fcff,
        'sandbox_set_binding': 0x4b1fdcb,
        'sandbox_reset_bindings': 0x4b1fd75,
        'sandbox_open_replay_folder': 0x4b23cf6,
        'sandbox_scene_animation': 0x4b23fe6,
        'sandbox_scene_wind': 0x4b23ffe,
        'sandbox_scene_wave': 0x4b24016,
        'sandbox_scene_trees': 0x4b2402e,
        'sandbox_notice_attach': 0x4b1ff6a,
        'sandbox_notice_detach': 0x4b1ffa9,
        'sandbox_project_markers': 0x4b20283,
        'sandbox_gun_marker': 0x4b2009c,
        'sandbox_blur_slider': 0x4b20c7b,
        'room_header_description': 0x4b2097d,
        'room_load_description': 0x4b208bc,
        'room_get_description': 0x4b20a1c,
        'sandbox_move': 0x4b20104,
        'sandbox_stop': 0x4b2020d,
    },
    '9fad6cccad8c274b1561291f56e138f43350b7045a7e3f358270b06b64b0494a': {
        'sandbox_particle_update_queue': 0x4b242bb,
        'sandbox_tracer_update': 0x4b23f14,
        'sandbox_tracer_position': 0x4b23f89,
        'sandbox_scene_shader_clock': 0x4b24339,
        'sandbox_load_keyboard': 0x4b23bcf,
        'sandbox_save_keyboard': 0x4b23c14,
        'boss_stock_panel': 0x4b22d5f,
        'boss_screen_root': 0x4b22d05,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203f8,
        'sandbox_graphics_page': 0x4b20ba4,
        'sandbox_blur_material': 0x4b24b91,
        'sandbox_scene_graded_blit': 0x4b24dd0,
        'sandbox_scene_blit': 0x4b24d64,
        'sandbox_refresh_score': 0x4b1fef7,
        'sandbox_message': 0x4b243c8,
        'sandbox_reset': 0x4b249f1,
        'sandbox_frame': 0x4b1fffb,
        'sandbox_draw': 0x4b2045a,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23c43,
        'sandbox_binding_caption': 0x4b1fd46,
        'sandbox_binding_name': 0x4b1fd0b,
        'sandbox_set_binding': 0x4b1fdd7,
        'sandbox_reset_bindings': 0x4b1fd81,
        'sandbox_scene_particles': 0x4b240d2,
        'sandbox_open_replay_folder': 0x4b23d82,
        'sandbox_scene_animation': 0x4b24072,
        'sandbox_scene_wind': 0x4b2408a,
        'sandbox_scene_wave': 0x4b240a2,
        'sandbox_scene_trees': 0x4b240ba,
        'sandbox_notice_attach': 0x4b1ff76,
        'sandbox_notice_detach': 0x4b1ffb5,
        'sandbox_project_markers': 0x4b2028f,
        'sandbox_gun_marker': 0x4b200a8,
        'sandbox_blur_slider': 0x4b20c87,
        'room_header_description': 0x4b20989,
        'room_load_description': 0x4b208c8,
        'room_get_description': 0x4b20a28,
        'sandbox_move': 0x4b20110,
        'sandbox_stop': 0x4b20219,
        'sandbox_particle_lifetime': 0x4b22f25,
    },
    'd639bd4999ba62ef62aac68761a019eaa7197e8751a7fe6d7fd6932ba7bf507c': {
        'sandbox_particle_update_queue': 0x4b242bb,
        'sandbox_tracer_update': 0x4b23f14,
        'sandbox_tracer_position': 0x4b23f89,
        'sandbox_scene_shader_clock': 0x4b24339,
        'sandbox_load_keyboard': 0x4b23bcf,
        'sandbox_save_keyboard': 0x4b23c14,
        'boss_stock_panel': 0x4b22d5f,
        'boss_screen_root': 0x4b22d05,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20415,
        'sandbox_graphics_page': 0x4b20bc1,
        'sandbox_blur_material': 0x4b24b91,
        'sandbox_scene_graded_blit': 0x4b24dd0,
        'sandbox_scene_blit': 0x4b24d64,
        'sandbox_refresh_score': 0x4b1ff14,
        'sandbox_message': 0x4b243c8,
        'sandbox_reset': 0x4b249f1,
        'sandbox_frame': 0x4b20018,
        'sandbox_draw': 0x4b20477,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23c43,
        'sandbox_binding_caption': 0x4b1fd63,
        'sandbox_binding_name': 0x4b1fd28,
        'sandbox_set_binding': 0x4b1fdf4,
        'sandbox_reset_bindings': 0x4b1fd9e,
        'sandbox_scene_particles': 0x4b240d2,
        'sandbox_open_replay_folder': 0x4b23d82,
        'sandbox_scene_animation': 0x4b24072,
        'sandbox_scene_wind': 0x4b2408a,
        'sandbox_scene_wave': 0x4b240a2,
        'sandbox_scene_trees': 0x4b240ba,
        'sandbox_notice_attach': 0x4b1ff93,
        'sandbox_notice_detach': 0x4b1ffd2,
        'sandbox_project_markers': 0x4b202ac,
        'sandbox_gun_marker': 0x4b200c5,
        'sandbox_blur_slider': 0x4b20ca4,
        'room_header_description': 0x4b209a6,
        'room_load_description': 0x4b208e5,
        'room_get_description': 0x4b20a45,
        'sandbox_move': 0x4b2012d,
        'sandbox_stop': 0x4b20236,
        'sandbox_particle_lifetime': 0x4b22f25,
    },
    'b6d2a2f3fa047beab00fc97dcb2ab9524334f4d9716b8a2fa80f4d6fba167b39': {
        'sandbox_particle_update_queue': 0x4b242db,
        'sandbox_tracer_update': 0x4b23f34,
        'sandbox_tracer_position': 0x4b23fa9,
        'sandbox_scene_shader_clock': 0x4b24359,
        'sandbox_load_keyboard': 0x4b23bef,
        'sandbox_save_keyboard': 0x4b23c34,
        'boss_stock_panel': 0x4b22d6a,
        'boss_screen_root': 0x4b22d10,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20420,
        'sandbox_graphics_page': 0x4b20bcc,
        'sandbox_blur_material': 0x4b24be1,
        'sandbox_scene_graded_blit': 0x4b24e20,
        'sandbox_scene_blit': 0x4b24db4,
        'sandbox_refresh_score': 0x4b1ff1f,
        'sandbox_message': 0x4b243e8,
        'sandbox_reset': 0x4b24a41,
        'sandbox_frame': 0x4b20023,
        'sandbox_draw': 0x4b20482,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23c63,
        'sandbox_binding_caption': 0x4b1fd6e,
        'sandbox_binding_name': 0x4b1fd33,
        'sandbox_set_binding': 0x4b1fdff,
        'sandbox_reset_bindings': 0x4b1fda9,
        'sandbox_scene_particles': 0x4b240f2,
        'sandbox_open_replay_folder': 0x4b23da2,
        'sandbox_scene_animation': 0x4b24092,
        'sandbox_scene_wind': 0x4b240aa,
        'sandbox_scene_wave': 0x4b240c2,
        'sandbox_scene_trees': 0x4b240da,
        'sandbox_notice_attach': 0x4b1ff9e,
        'sandbox_notice_detach': 0x4b1ffdd,
        'sandbox_project_markers': 0x4b202b7,
        'sandbox_gun_marker': 0x4b200d0,
        'sandbox_blur_slider': 0x4b20caf,
        'room_header_description': 0x4b209b1,
        'room_load_description': 0x4b208f0,
        'room_get_description': 0x4b20a50,
        'sandbox_move': 0x4b20138,
        'sandbox_stop': 0x4b20241,
        'sandbox_particle_lifetime': 0x4b22f45,
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
        script.write_text(f'SECTIONS {{ . = {VA:#x}; .text : {{ *(.text*) *(.rodata*) *(.got*) '
                          f'. = {BOSS_EXIT_OFFSET:#x}; *(.boss_exit) *(.boss_ui) *(.boss_rodata) *(.late_sandbox) }} = 0x00 '
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
        return raw.read_bytes(), {name: symbols[name] for _, _, name in (*CALLS, *SLOTS, *INLINE_JUMPS)}


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
    if (len(code) <= BOSS_EXIT_OFFSET or SECTION_USED+len(code) > SECTION_LIMIT
            or any(code[MAPS_OFFSET:BOSS_EXIT_OFFSET])
            or set(symbols) != {name for _, _, name in (*CALLS, *SLOTS, *INLINE_JUMPS)}
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
    for address, original, name in INLINE_JUMPS:
        rva = address-client_recon.BASE
        offset = client_recon._offset(sections, rva, len(original))
        def jump(target):
            return (b'\xe9'+struct.pack('<i', target-address-5)).ljust(len(original), b'\x90')

        old = jump(previous_entries[name]) if name in previous_entries else original
        if image[offset:offset+len(original)] != old:
            raise ValueError('Sandbox jump site differs from the pinned client')
        image[offset:offset+len(original)] = jump(symbols[name])
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
    if len(SHARED_RULES_MAPS) > BOSS_EXIT_OFFSET-MAPS_OFFSET:
        raise ValueError('Battle map names overlap the sandbox extension')
    maps = SHARED_RULES_MAPS.ljust(BOSS_EXIT_OFFSET-MAPS_OFFSET, b'\0')
    code = code[:MAPS_OFFSET] + maps + code[BOSS_EXIT_OFFSET:]
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
    'sandbox_particle_update_queue': 0x4b242db,
    'sandbox_tracer_update': 0x4b23f34,
    'sandbox_tracer_position': 0x4b23fa9,
    'sandbox_scene_shader_clock': 0x4b24359,
    'sandbox_load_keyboard': 0x4b23bef,
    'sandbox_save_keyboard': 0x4b23c34,
    'boss_stock_panel': 0x4b22d6a,
    'boss_screen_root': 0x4b22d10,
    'sandbox_boss_menu_open': 0x4b22300,
    'sandbox_keep_replay_markers': 0x4b2042a,
    'sandbox_graphics_page': 0x4b20bd6,
    'sandbox_blur_material': 0x4b24be1,
    'sandbox_scene_graded_blit': 0x4b24e20,
    'sandbox_scene_blit': 0x4b24db4,
    'sandbox_refresh_score': 0x4b1ff29,
    'sandbox_message': 0x4b243e8,
    'sandbox_reset': 0x4b24a41,
    'sandbox_frame': 0x4b2002d,
    'sandbox_draw': 0x4b2048c,
    'sandbox_mouse_hook': 0x4b1ea0c,
    'sandbox_binding_menu': 0x4b23c63,
    'sandbox_binding_caption': 0x4b1fd78,
    'sandbox_binding_name': 0x4b1fd3d,
    'sandbox_set_binding': 0x4b1fe09,
    'sandbox_reset_bindings': 0x4b1fdb3,
    'sandbox_scene_particles': 0x4b240f2,
    'sandbox_open_replay_folder': 0x4b23da2,
    'sandbox_scene_animation': 0x4b24092,
    'sandbox_scene_wind': 0x4b240aa,
    'sandbox_scene_wave': 0x4b240c2,
    'sandbox_scene_trees': 0x4b240da,
    'sandbox_notice_attach': 0x4b1ffa8,
    'sandbox_notice_detach': 0x4b1ffe7,
    'sandbox_project_markers': 0x4b202c1,
    'sandbox_gun_marker': 0x4b200da,
    'sandbox_blur_slider': 0x4b20cb9,
    'room_header_description': 0x4b209bb,
    'room_load_description': 0x4b208fa,
    'room_get_description': 0x4b20a5a,
    'sandbox_move': 0x4b20142,
    'sandbox_stop': 0x4b2024b,
    'sandbox_particle_lifetime': 0x4b22f45,
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNrtfX9cVGXW+B2YkUEn71BUVFa0UaFZQrq7TmqCOEjh5Og4gooWCSaKwgv3+mOVAi+jPj5dY8Nf71vtuq9t9W72zb'
    b'Itf5X8MECjQmR1UioqykvMtigm+CPne865M/xw7d3t/bv5fLzce55f53me85znnPOc56gJ8Eu/z/Tdm8bqqX7Z4pfN'
    b'mub3+53a27cJgnLQMrMt553zF93sGxerNpUtusPgb/BUSBa1POGxVwRWOXuO/6v4itlzqt1iWkVJlXXZHQb2TZpLLT'
    b'KzatWuKT6rGGHX/L91+htFq11jXjEiVM3unv0YlICM7JMZLqh4obCgGPBYGLpQWGhYAG/F0/xlCaeXhCrdftkYX9Ee'
    b'z2riK5i9Q7V3LBScgGTaDH/5RsjAauElunNJqMcvHed2s83eIW6sEN+pULPPzp4baGWG9s2vBKGk5vz9guAq8b0ADT'
    b'DPAuilKa7uTgPb9Di8Mk8GPv+ai8+PPYfE0tWQDRvdz+zwPMjsdcxez+wNzN7E7F5mb2b2FmZvZdAruw9w4x4sq24t'
    b'uwDP4ubLWGcWQlYOEN8J5Zvo3VGmGj8wQs1OThho5zBfTbrnnGSNrygpKnNNSI2HHrCadlOS7aL8gV7rgUegiFp86R'
    b'JWmg4AHcxVRJ1vwprEd14IexSbThoZaNde7P8rvmiPQxtKUbEwU/T8F9SjXBy4+2H4W+vJh1TBiG+r4C0cYKK9Nr6R'
    b'bVoOn8rFQbJpt4sGS4JvQPEOPv2zjeoEv9JdtOx6PvGzrUq3uMzCNkk0gFho0ckolaqF/iGehmqeZBWtoYovirsi2c'
    b'QYK5seEylGGGMCuNOw63j6GyYpfrfoiUbk+0yfdu8dMHu1Y+4ThBkuxWfhWzv8/2H0nBQ9HEnlK4M+VwuNrGaBECcI'
    b'C5oN8CX4y2LOAIFUA7nOV9ee7QSc/Juf6VgSyjdUIOVUqWv9CJzG3Bpz+PxHuFvjDh9LGq2utZ7FBP8RdcsLl3HWOl'
    b'nSWNax0KBN+tHvnwU1DhbfsXeOzVo2gCllOEJ/QJQAaeYltP3bLkFDgPwMdgzoe6q/7Oy5JaEwsu6zngqi6Dv4jLGe'
    b'RukH1XF2KiwOqPI7f3kcELLNfjZIwd2z5+LCsk4YDRXxgskuPiuF5UdyeQhLt/I5UWKEAIvKzGalzGYFk+cq61NCBM'
    b'EwR4yYMLraHX8I1uz6IQNLT0phiQ9mmP3SmfiK0pOz50DtgVXxye24Kr6+F1YFq1Z8Q5TlkSJzdywN4VHFY4QDMIqC'
    b'HKp84o+viD9Ua0e24AeCV9dYf8CXZnXNjfjigFXg5e5m5tXWIplUiKX/D6k1aYC6+a9nMadPW0IJUki8n9t96uYqAG'
    b'uzdJjpACAtEPgjBCcFwaFB8DEEDw+CjUFwC4KvD4JNQXA7gi9fDoAHAHh9aCnghM2NnSOFJrX/Az9T4TOpeEy0dKEt'
    b'AenQ7QX8a/Vi16lJYermH7Civ16mPrfvUzffjp31atuRIDYLP1DOxwDwHAJSB8DXtXohVqc9RcVa2ktZnbo5jEbJp+'
    b'UAEAisC3iHJr5vUJ0NxRfCxbVzcK0VaWPFZ124gNzawER1YgiwGa39U6h+JOEkb+bAYRytQEKjeVELSzFyh8acFua0'
    b'qpvvxgZSzGx5JOQP1fuQKL4PE4BNhC37dfHKsAflEetTzepUAsQUrwyfI9/GizoULU6pHsgmWSD7Zfhgk6wwKDKMUr'
    b'e6+T69x2/+SDUOhlqiJZNSHdYeqlSblYNDAsSOrB/piX3sYkeBjIAckG/6aoEXI0uJsDcrF7Lka2rtPiSpGntzfHtY'
    b'/CHlQvjS22HKDgxERtuophiYt6srvrGrC4BKVdy66aZmSmPe+Maaidd8dldXl5oYAhWra4S23QJluaZZdTdzh3eav6'
    b'Evy2C1aknDKznCDFVudrEP2WlAC7Kw86pS3PqRUFIdjYkAOA3I+o9yu7df6eMzWI1L2zEElkf1wVjgmD4zc3Ron3TD'
    b'eJxmF9StgpAgmIZ+8aYRsMGJkX0udtJ/DGqgSmoThuO+Gn8Ipisljq1rwV4cWZuSsNY5UV97rqmwY3ga5bD4c21JBi'
    b'TdDnbUdPD24lBbiNzOf2eE/awVgKb0/KdDbWGyV3l6iCA1AmQvjqHWegG3DZiVA1wA6jBn/SkG2xiQxa/BF1ul/LLt'
    b'KbMcbptrFEu/orXRsScLs5zRwsYjaqyDbRgLdWlTgOriK2rXvC3oP5YcyZKtLNnCkmNZ8iiWHM2Sh7DkKJa8iiUvZ2'
    b'sqMM8aiwGfZnpa6SnQ8xKlHqQnbbRrttJzI+2mtWt2BltZM5zyx9Azmp5D6DmKnnH09NKzgZ5OeqbQcx08a9e8YAjU'
    b'9ZSZzTXOYh00+FrHzTBvVZZ7BAGGulO0zjHDw22Bh8MqxlaLsV91ijPxX3WnmH5QjP0SXr/E105xaoU4YvVOGJClgz'
    b'vFuV741wD/6toHi2knofx63KzE2KOdYkTUHHg4jfBIMeu7lAebrd10N+xSrAZkn+BiaOUTpvNp6cwYA1ufkbsy4I+Z'
    b'21s1XO6s7t6kGERPE99tqNSs8Dp7OjwW+zrFYRXw8spYZA8HAfthh7krDlAFNNPr4O+ziCfkWPs2/Z1TCe/POul95h'
    b'H4B9inz06Blzb45+joFDNOQA5XOnxBp2YmZUC6azI8kpyQ9jd42bKbSmdUAmFCgxGzH4eHK6t9Easb2hDEbxi0Pew7'
    b'qKAescugEukfIT77dTxgKK2pUDLjUwA2Q+KnCMgCwJFAnTObAHoEoQuwNWok0DHsxKJceMyTPIcg4TbMlb8bJxq+ki'
    b'B/ejv8axEnAhoRyTD+6YfhZYYZyDkVRidiCSI+H/CMeDSynwxIG7Hii+WrS5Fk7E189Tp68e6ejgtw8zO4SiwhnkPM'
    b'US/Fq0XN/rdNxcjby4tnl0C+hjR/I3fUq+X5tzYI8K6WIxNh5/2N6dzeMGsc1iKH7U6HP+1v9pNfFkUhZayM0eWXIc'
    b'zh5eu2fQ8iTKP0uOeQlMGOgbBcZr7NE2q7QX5UeSpGkFIWub1RanHckecFdeONl7oAjaLmaWn+BqXIJ0jRSpH3+r03'
    b'QEtSOOxcJVW49qapys6bPjew05BLZ3CA29EgImnqqhiz0trhX3ZJ3/wbb8TN/9s7cfMHQs1l9reZY6eW0kXMfqSycr'
    b'Qg3cftb5seN70mkNB/Oy/D1cvcryoXQuSI4t2v4OKTzMpKqyD/rv02WN5JUX5g+SxpeY8opD7Z/VibD7gVT4qGOiK4'
    b'e+fQqpILX8I2Lq4ZBbu2UrRToPkV7fadKGd4aUknRdfam/Gt1o7cE+Q5TlIGyH/BlQTi98QYc/s8njRa2Ye8AybpJW'
    b'n+wmX+hvmeihVzlH3IVvTK5amBivcIQojQv+paBfMZ/rdGYBzYCW3KcFSJrG1FRtIMqBikaJ+fwd1+B/DhgbjxbWu7'
    b'SAx3xxgFeaUUxaog1x7i24tB4oQURik4uh2GwOh+wFPygRje5wa/M5rZXwDQW5Bhfq29Hlrzm5y3Pgf924oNlInvlP'
    b'7WBxPlVO31Tu54gdu3+huKYcIs1Fl2/mhLuL0MpGmj3BTvZ0Xb4v09Ccy+8c6kVZJ7bxiqAv5B0iDxnY2/3QozpFSN'
    b'nMYdO1Qcgre1e6FTJUVeVA9myvdwezN3l9ncLfIQQCzqnxoewN0bw93boEmLvJGvjOL2bdBK13GWFHVHbVctNi7dzF'
    b'OH8PJWvePKaQNLHaJ8adA+BaWmqxaFQOkunjRE6QhhSUNeDZHCau2vkgyoF3G8ikO9gzKjDCj9CjLraUqHFYooLVal'
    b'2wCZbjqkVJq01ZTTjDmNSury6K5aK77f2VNqVDEUugv1zt5iYVoqFBP3nhpUiIUSumqjsVBYSepyJBBIGFgY01ND8V'
    b'mqQijuW0WkZr6E1D6k8lRoySkBtOjwlctZbckpBbJBt25bGM3tL+kkCsK1cWFE30/zuaQhVrF0ETSmrIwVxNJMorGd'
    b'sEuMm4MYrMVtkCcNB2JL4K7pPDUdiFW172aRMWxyz/aidXUjne3m7v2wdzBXPggiSbkcFFlXHEuSapNw8wUaXpuUAA'
    b'z25Tp9J/lDBW1v1TqTn1kvjvhwmQjwzfUEr+oUH6vEMhMhQ4MY+yFycnHEUcrzh3qq47ETBLe68nV+b91wkMq2iSOO'
    b'UL4NDfo3ZP0OebuEmXOpD/WBZckdzconsFN/GtjETug7VsQcoEv4zkCGD0pHoMPuFurwgG6sFvahiEX5tF3OhHfrmo'
    b'zAjoabfz3u75CAfbPDFhSRmot7itR+I42nFAay7oRzUE1ahRiribF1g1Yu76rNByVJtnr88nXKylEwIUgw7WG7f4t/'
    b'TLv/gvi6ooGTh3PHzqFHbJUFT/TjaIgrIB2xuBlYGuuGN3sLUKoUypNincBt2PkAvwFmw04A1+nlONoX9xC/Qdm75N'
    b'QfW1BMYytH6dw1yFv1HSb8WtxhbroNZY+PaZ+zglarrl2Geqy/HLVe1MsU1Mu2OKCP0wBKWrDdB9UvCRpgQoIGmIQZ'
    b'/vIEsq8o3X7p0X7GFzQSpfnLmzpQiUZuwO2W+EabvUMeDODiDlR0obVNVczdmqZbZ7i7FWSKQVDpAkpuvx7SQMxfGO'
    b'6XNZDDMb1CasFdo0HfN/dEIb+CjUot2qjdHYF71YO3wl6lHCxmZK2Ir+h8Tbc81NrJaqL+YSuZPwbplgjxnTFclXQ7'
    b'iJoGnG0TltKm/x3ZWjGytXR5MLeXsok/buT2dfBnK6eCSbZquUq1ly3MUjcMuUyWDCypbkaTjqZe7mecmMFls0tbbU'
    b'VN4b9AX1CTP1K6By29Vtvugz1BNs82tSy6wzB3jr9lUUWUWhqKFhzS6oNFJ1HRx/oWXRAsKuQGi8ZB0f0XeoqSrqVZ'
    b'qGgMFNV+7SPNpNq0c9idBlBrvNojALF1yHeyWlYFwr5X68QsF+DlT5f1F+xCW95YQeiVkdQis4td1naJWPGHt6DiE8'
    b'3D+NNDSMlv1j5ux43Oq25FuxlsCYo2ElBeZlK6jXJymmhNNiq+KDLuJEfyGVFBcw93tPjlZv9RVgtk4XkZOW5R8wPi'
    b'WtQPYP3CKtBOjRGENpTASsbj1MhP80nGFDQzLVWeNovL8lUQox0tqKXbm6AuTQVM/p0G02Hhjg608SrpsfJQv9z0L0'
    b'uSUQLJsRtHpypaIFsODPqWwTg2b4Csr1V8Fxj0MsNnMOikmMH4Pvx3kqAeZlW6UgYK3TjlfSdtgWb2Fnay3Wqjv7IZ'
    b'FtZqfFP2FpMwdZ2y9xl6GYgTNKct8YdeesHmo6j5OGw+Mdh8wjuf92n+sI+aD1e+N7OduEsFirtc2slrsHDnTZDP0a'
    b'0ZoDxsC9XaVh/aJbrJ8DG121bWqmPWyOhtlqrEvf+5YTbQYUBz5XPMvMiiKh8de9OY5vI3zIVBFktRquZpVsDgltMB'
    b'3VSCjzM+agXUW1IK+VrUOdkMCyRPUMu9/1gdarPCDDmBP4AQMc0CSbzYq9sw7uApo0Ekupknjibkyic+9zRmD2dnoO'
    b'anhuFW0YGTdCZAzWiP6rs4ccS+tWCnLwM70cQ2WMeyFQcNuJipbF6pAVgZLBesLruD2ryH1cDHWFwslZxQxsFkh4dW'
    b'KReyluot7zyLzaB1rsoi6IOiPU3NbInS14wdRsjKHBqXo1ABtFvIThDJ7ECIoBI2c3s0Gs3sMUDO2nItMETa4HbCYR'
    b'mrDpBOqLyI27UxSUYZpFwzd7TyhFibQZ6BFpJ9qDoLHKSTcdCt8UsTbUmxtIc99Q/c+308zchBjo7lYcw1mSWl8Elm'
    b'luTkA/gMI0uazlzpPBkAGRzadeC6Torh7ibu8DI5isstaJCALrhjeJGPyZGwSZny9ysGLrcyRzQrsqD5STYz9xDlYD'
    b'SOd5A6tKWDcCA23EhWE+g5cDnm6MDhsPs09VSA5mK+o57GZPHrqaNW+Sbu7hjjNMoRfLAtJRYk6UbmjNXe+h4wK7IC'
    b'EqrSMKfUAG3CnEP7s2brrV6p17DLLq1zIKIwiFAALdcqMsdLuA+/oF2jt19pKq5aDdL0NlbHHFu1LW262fJHXEHVII'
    b'7BKAcRu070fEtEIJZ+jt9J0POtoqeR9v3hnkaxtBZfd6MugMtgD36Fqb8f+3phqK1BGqz+Pv8E0HiD6Pkj1l6nyq9q'
    b'n0N7sNdXsg6tCuZKtNpfJWak1JFsjFJbrYKqCS1pBfUW1d7AylE6YheIaXEE8ucJMhlYV0ZMpNZI5A0y+TbuAIkSqm'
    b'0I8jigO3cr8bgO5HLsgvbnNjQrxPWRJKqMAhoiXoCRugEqgGr6JAamFxYULzJrz4XjAP/lelhT+77BRi24ptJMQqSM'
    b'50N+eZhSZBGkGE5GG54wmqdkwEKOVJXHv4fBMMgDFxpARXU2CD0KapVZuGIqtVHYSu3E61FtLvEhW2SOYnZRS/lGXy'
    b'+lpvyEvwn6bg8budZwivg7IF+s7+T98Fd203ph1XK4sns0cePhPavsV1h+khaYlhr8WjYc56IUOnZCu+5+FMfNgnS3'
    b'XmJMqDxE2deNjBpq2xdJtQ3E3Ce0DNCog/WKdBTC7cVANiIdynD+KhLKoWDuwmhspRh2aBWFDL6T6OicWLo/BLlbKR'
    b'9wNVJ6MYR0ADypWaUbwCVlH+my0u2iVScjIhuu0wyIlO5WkDwb2iOxa3NP6R0FotIS/04EiJQCWgQRC+yWjlYgEysw'
    b'u8kxkfGHtOWnkFiiGG8RgodF2GVlH559gEwshtBYaaZTpEc8mgKPGRNBqgXRe8ZoeDw6lqEInjxZte+Az+QE/UgJSM'
    b'3dwO0o3DuagPugdOwNat6w16OhjDJq9/io5uwdaM/bWasEjZXwKb/Ny9G6CFJ/rRI0L9p2odlR9EzE4YRFSrtO3dAG'
    b'1elXLjwhrkX9h2yB9bodEK1ZaFWbekgc8Wwrio9rwg1U/Wuv6s0oL2GFsZcAx9d13K2b9L8zuzHbDso28yjUc1yMvQ'
    b'yvP9LriC0aVbeX0odh3i0+vRy0m/F9pzj3OMDeC9QF9WfUwveBFvqeWynGfgJg0L1mru3AikbUkh514Kyub3VDOqcz'
    b'KMjxN0h4Awv2GOtQ31k7lrIOuyCOQCtfLYK69dbQiMZ3Usc+omrfCFT7HelSM9E2R2MNNMpoSBnfSEzpBd0it/0l/e'
    b'/z+ihF7MJhaA+JP4fkcK6V6Oy0k9XiJxsB0oTnnHSjsi+FVszgtOACewQWW3rPuvU3KO8hjcF6A9LcTaS5jp0h4nTU'
    b'8Tn1PSzvi1YizH76EKyKb2AD1iq/ppVxPXcCm5YGc7elw+BM4G4r/JkYPBiuN6Jm0WJFzcI3ijkqWKemwQiq7oO4E5'
    b'i1xlYUjg6qm58FrQn7UaF9egpBdfCqbv4jQLU6AngJsBMB7xKgngAVCHiZAA0EOIqATQRoIkArAFSQaUsABrscGu7Q'
    b'hscdFaoiFLcZ+pxW9LJLoy54aIOoA7f368A9fTsQEezADz0diOqDnrELsAnrg94NCDj3bS96d3fp6H317T+jZ/0X6K'
    b'nvjYINzqVlhuKGsQL1Cl894MrUOIBrT32BZ9j4CnIYqzEl/LcG+4da/htscg01dX4aNOXBLDzFmKauweMbPMhhBFPX'
    b'kJ3TE4tVNPBNCMPl/j2RrLlkuVWwotEEt271vclUomPnuwI7gqbU9x7H3Ze9Nxz+LLVwD6Yzz3ASn8x4GLrUhJKcoL'
    b'elY6GWu3CAXvyK9pqVfFMGAvFI/Klli+Mr1Pfmw3fC+meiTkK2dZYwpdsidwBcLcQqXsTK1w8JXTfEkGCrkv8Bm6k6'
    b'wcJfpPqnWIceVTdheUMl+8RWKw1MWD85tHOdJaT9jLrpSYCncYcZOvDoQoFGBXutqpP1vtvN/cde+8GA26dpcODUnD'
    b'l8sHdaPqczSBA2TWWjPhZ08RdtAGLpi4IuT2+ZB5jHn9PextWLp+dWglZit7chrKbnFGpuC7oFiJsqSd0vPSJleE5K'
    b'6QHRiZVtwxrL6HzcrkEh8d3Dld8hV6oKciW1zDp3N7LFQ+xT3fofoWRA1f5GEHuh2UDpDm3r1yTTXtHFa6mLv7pG76'
    b'KVRE0Nenn3Z9hL9MswRX/2LtImbqmHEZ3k0aBWhohr36PeglCuBbr2wde6VIFmCc3zBZ7JtoK0IJaWB8Q/dDYIbOwK'
    b'9kwugsJi2iHgmsWIsziismAhnsFcIjZdVzAHerR2Ih7fPDU6tFMcXrHO3inbVHvnQoP6bAZZFqBH5GGg/Qbk3Fkzga'
    b'LCdWYOWbEUehnIAR8D8R13J2Hcqq3/qke4gaHgxphqHgn/hsC/UfBvbEy1XJhdMPKB+7Nyc4VJ2VJi4Yol81KzV7ik'
    b'TCkbAWk5S7Lylk1fUJCdmeUsyJuXXVj4cJYwMVPKzHDl5T4B4EXZBdNzFme7pLz8jOwlUnbB/csyl/5kjsXZBU9mU4'
    b'5lOUsWL6Z2IX1Jdm4Aicl5mVmTc54oyCxYkSg4czNXuPLkJVmJQmFvXdmL8/MKMnMn5eZI8xYI2bk5i3OWZEo5eUvm'
    b'CIVyfkH24sx5K+YITxZkLs2R4OWJnCcfeyKvsHCOsDgz67EnMxdnwyvkyynMWfIkvC3BCqTsrDnC7MzFi/Oihazs+Z'
    b'lyrgTfi/OysqOF2bk5S7NB/potQS/gzxN5En4tyZOwpgmZkpSbneKeOAL+Tc5ckV0gJObmrpienbnYmQn9SspbImXm'
    b'LMkuGNEPLNiXZC++Wq7+cIGehT3J0ECeLMFMSNnzpLyCEVckj5ielz8Cv2AmRuiY4VTKlKMgL3eEa15eQfaEJ4Xc7P'
    b'nSiExASG+lIOfJBdKIbGxaB8DU4ZxBl4KvMzJz5ezgR1Ju3rxFQkrmkiwhJU8uENwPT8nPnAfDnZQHk7MEWhcceUuz'
    b'k3Nys+3LYfIWZOcGJ9iVAmTlzJQWJBfkLX544uScQimtz+wmwfwUZE7IhUoL8iDDkhwpJzPXARPR93syzkhfQCLMXL'
    b'8COUtkqX+WCTBt8VcCHiBAClB3dkFPI4HPPlXokN4aer8fEFzZkgSUVDhi2LARk/PmZeZOgwxT8pEgC4XeJYCZRwiu'
    b'eTALuTNyspeNKKTXnqnrmaGeEo48rAOHAqZy3qInC3AljLBn5eDE96kYMrhycwAfoT/QmV0wD2eCfrMR7fvm5xUsno'
    b'MnFYI5bNFCoc+PMB/hemJCXl6hFCcIg5GRXAmP/2d4cu6KOCxv+Wd4/NXgML9xPwG/an7kB1jgmqvA468Cd+VnLlty'
    b'FTiSY9ZV4FMotyAMugI+/de2q8JTZ8RfFe783cMzrjoOOQXZV8Mf4VfDPyk3O/Nq+CfLsHauUj9QYi527Mp5mZSXdd'
    b'VxBvhVx3l64aTszILeeq78uYQpwmRhgjBNsAuJQio8pwmP0ZcT4InCTPhKpjwTKe3/Uh6/pgpueLqE6YjjoIFhA9Bf'
    b'yxASahoQZg4fOEj4qV8/Ou0zflf79adTZH0/3e8r8ztyCguBqxHs38k/PRM2vJ9R/yPy4vy+8/Ov8k/LzoWV8O/XP9'
    b'2pwwb+m/kfzV4u/Rx8kK5/Tn6k94KfMV9I7z+nfqD3fuv1p/ID6wySDv3MvfAgidAvvBceJIV+44kbZGDKr8wfnFr6'
    b'hfXCg1N4Zf7p2bnZIOlI/fAHOE7J9Mwli66E6+0GJuCaXnhwiK/sV3Aor8QnOGT0GxCA339/RlbePHkxbCiFGb3b9X'
    b'2Lslc8ATJi4f1Sn85d5fdT5RfTJnffE7Bf/a9V/DvlAZGfXb4H/ysK33tF+bi4wuhV0XFxcdH2IKi4f47iSQOP3zkW'
    b'XsYLwuokQdh2AP6N/6h+x4GRI8sBVjx+2u7bHhKEZ5KE4v+e+FH95HEjRxYCvDFp/vxvH/qo3jLeqd44bv78u8f7pu'
    b'4eV/3DgvG7bzONf7bqh4dOiocSaj6fN/65gQPGVzxXAmWiEzKenfBQ3rXKg5A+LuPZKihfP3bkyJHjb/csGj+q+Ldj'
    b'4P0AvB8YeHz7OGjrId/U0VDvaHivH3d8ez7UUZYoCAkIey/v2tAHBaFugvDL75ffL79ffr/8fvn98vvl98uv5xc41e'
    b'ZFZpe2/zu/v6S6CY9nvqnx+/mGLDw0rPYcksZzg5+s2azSUyHFc0Etp6saDfJdfqcRQLetL/4Uksfesixyr2AoNovb'
    b'qwoHqsrOxV8Y2Gl/A+tgJ7SRRihfZFaVsoMbAdrHaYJ97HLxR8ysusbe6eeJcZ4KOSy+ou19cu9gx9Vy55cvYmPtav'
    b'nOLcUhNmeC3KK6O5XKVaoiNBeHTFtoWHi7v6G4qBMQlD8AfCoRxd0j/oCl/lpS7fx2dYgt3Sj/ZeHd/qPUh221wvRk'
    b'1321CcbJiRPuq0kwu0ouo7+BbOZr8Ti7/bquGuFx9Fjia8/ikV4on2RlieF8UiR3WxLYpEiWeA16tPxHBEu8ntvNbO'
    b'qtLPEmbrfWJN5lYIl3kANkry9Opbqazl3IyaxZvxpB3uwdIuteGkruxb74c+ojfuW8cVm0arEyh1d836o+0lB8PnyZ'
    b'uOjcwARVuqGBu73QpW/7+bcH7n2gjxP07BpWPU0tQd8C9qm/gc+5dKhav73KGjyN0lR2mJuHflpy/u0NpaGSWHK+5Y'
    b'PSUNm2Bw8k2017FqC7gsUcXwGTfjdPxKm91VYn3cTTrbajsoknRLaLthNiebX4zklu9LZ/od99pZO4Eg3Ip2bTWb+f'
    b'Lvfw1S8Z9IMDRv5dWeR9cFR6Ck9TNv/6B6CyaR/iiRy0sZBgKQh76EM8uoCvxXj5StmBJNitxQahGwn6tg69Nlh+GK'
    b'W1Y/nLhwM5L1DO/XpO7TB+HISP9H63nD6mkyFEFt0S2Hp62n3Q+/uVvTvokPYWZe/b9HKtsnc/vYQrew+ST/1AdBK6'
    b'oP2ILpFr8ZKO+sedVIFmk33SvXyMxy/dxV2X6NSiQ5Mhn61WFgE93VmtW0u5TIcu18Xj7VIRs0KNp7VUdFGstY2RjP'
    b'EVDK/2Rp6bNDlEGgwlIXe3FoHpY7hdU6rNtmr598pBS59uUeV0g+v9D3THnl26Yw+6colBx54G0YNn2MrTlhBx7XN0'
    b'xtNzjSqUp1mABholoHqzTTlLDnFjytHhQxrF6JtzHz4nWWoVI7T3EB2Kd5NTNYwe+onVaaYfcdQvkTeGRpfcdKeREH'
    b'Za3Filpf6InedKB5Y9HUiaLW6qQle2+39Ex7/gDZKh3wJt1drO+P36WZ4bx4GunmknDup9bNaP7/DQ16duKYsUBO1M'
    b'Hbprapj6vn49kk7yiiMRV7y6pTXU0fgrFiJVaSB3d2hT8JJxFR6+2X2sHH2Q1C0ZWN32uoBfX9Dlha47KHj7q/0WPb'
    b'+6ZSzmXIE5KSFw29WMFZ+7iNAoA+EJrXYo+6zkJr2pSht/icYCK9H0kTDAEAXmte2OS3QHqnc8bvoGx2PoaRwP5CXR'
    b'eG63mu6hFXnRg467W7U10N4Yak96staukWu5XduDbK7W7qNPN15S1K+X9PfB7tB9sFu12Xgxgnyw74TPeD/nOHCmio'
    b'c3op/tProAtw+Hr+1z9NnXJ1H0jCW3/VZg+13V+vwMQ4iCzcafY3ILVDgIkfzyAt0U5faW+HM2ohVpUOBGZFcXO8Zd'
    b'o+MPMUcz0OKv6UKuOinOydxN/kbubsI7Azf1ufHR7Dknh99J9ChO7Jqk+AfIDcplw9JxOvlBk4/yIi93tKpbKnCaGm'
    b'vIQ+JaxGMm+k6jc4xx2dB2k869jcWGjvZboVIYkGZG9aJ3OmHZ1kUHs8plk7jWp/vCG+hKjabfsokW/nmQnTi5Vx/m'
    b's92BYZY3tr0oBAfSIHrKaPVYAqx0NdXXqr12HknRpiB1Svl9JzdB+BmT+1SwVWk4fOpTy3YJvRMMVFpONx3LMSlI7f'
    b'1jC7CvkO+/+D3wfSJEL3M02dYj8xdL/0xc9SVyPkLXyqPq5kEoX6R/gEshQV91wNa86uYbEJ7wgb4eJ9Ii8bLy4UQ1'
    b'mDG+Ubv5PB6CN1H2O9GbQ3kcF/EgLHSIKSSu8Hx65tJzQZ8diErFYSMnD5K7CmxCVAfteVRYqzxItx9UdzOM4bXE0r'
    b'Aidc2D59B1DfPgbV5yYPXC4GIWU5zpY4EcEtBHF+hL3dJ623J00x3AlPyeuqFB6EF6N3pQ4Mayx6yHVzje+Vpfnzgv'
    b'rJjHzgfcaZrUzS1Ip8MOIp46Ces8hyguijrRgDnMlMMn9GU4Xs3TFeS/yFq8gcWpLwbgLoFpbKvu6h9vYIZLe7MFRc'
    b'FDPjyjJ/9V7eh+2lfR1bF6hqn4C/RC0XexWubV3AcgdTBKH8T8qwD0xY9Ua8CB3Kw7LGRivbX/4QvwcS4H3DLk/T1u'
    b'GcJ1NYYZ5JehV69z+bPvk0Oy3cyjoIWHPCelB5WnIkWGl/UHqFt2Ikt3aNqAClrRt3GHpuzRt5r/xCUgW3SW69WOne'
    b'vnSGH+F/Ix2pkXZC55Mhst0/pZcb/f4uwl8sSczNy8J/GctF/uCbIk5S0J2sIL8QBPr+CfygRSgwUSn8jJzZECJ9aB'
    b'8nm52ZOzl/b77sGGbPyFhc6ceYuyC36GfTgvsyDr/2rf7imP9mU+0jl1muLDy8DGmBlp2ujr0BvR7MH7u8YYNSvGOG'
    b'tmuuhZetePF/kGdJ9r+yzmx4tEEM7PkSAe/04niOHM3oDuS0dx4ht6fKNFTxWy46l+9mHJhctPo0PTm7iiyb0fPVu2'
    b'E/drMNEV0gGiB11W8BqpvvJV+zExdq2FbsrQjXxgFvY6WsjewFbDHPVdXbyoLhEv1QNZnWDVyoXwZV+o9ub4xgSguE'
    b'EPr59+zfF11rcM7R3E6EvRXd9er1SE1Ew0NUerzlB2Gt4+S4HH58DoG5jbB3xkwfXITPFe6hU39GGRTf4MF1kWOiL7'
    b'zCDS8DKKzrET7yWp+xaDIJ0GleBO54NV/StV0W8zYayM8Wp56Vm6xXQMFbI0Yj7D1PLf9wKHgG4RopanAmTZALV8KP'
    b'xdECqE6Kyq3/Xc+macg2YQ32fEn4NZMCvns2BZivbTtrfQURhQk7JYtVrsHPZECN5V6oClWoqc6BBbjxnak8V3Kyvb'
    b'rJCxpLph6RMhneKwyj6Ol3VO3bdp5toE3enVGLMQA5QchapArEkBTkkN9b+vXMPqXMremIDacM+9q/FK/B48IBsXCm'
    b'/LUhdVpO8x0s7QdZru9g+tbP8N34uix72r6RZ9JYjOt4/bjrmjdmfRTA/tFt+o3IM7Utdp1TIadB6KCnBCV6La2kGy'
    b'DjTv4rtRpo8/abtYOIZ1D/Ue0QzeRXWW0A31ALZFyLfwAtCUbP9hlG/jiWbbanJSotAl7dcUmtR0Q/sHrLa9ElSkvh'
    b'eM2DGXNu0kzvxC9Cr2RSt7E+giw3hmbxJLScs+zDeQ4+4hsRQr3T2PPFSVITB6+waSlOHDAzJbVTDERHLV3lOh+uZk'
    b'Oy4mHz9AnXLDltPVqVTEIVXTnrXO/RnKVSCIVwDkK4B0dbLjmAvkqnXo+9VkCsaWcH8N9It+drDjbkAUFxpM5LJ+BR'
    b'2zo/y5GHIzlh7BSeN8uL6dWVjt0E9K/Hg/YGnc0Cqm0Ga+L5YkCU/jU6K6GpHeo4Xqc5jGKnEetEaYg/SeNkwt/+8V'
    b'gR1h38CKufkEjtsD3+K4YTwDN0Y2WKxHNuByJLpT+/CuYQc6VJ8FCmvATD7MBCBrAuZMH4LZzwI1fqb7FadXI3Q4fH'
    b'yqf6QnjAXI8xPJja1aXLNdd4DenkKAFnHNczogGv7lWwOxFH6Act8iAKAzT+sf6Y/HdYpzW8QR2ydjSIW5kLzLSZWc'
    b'XToVsmH4hJkLAO2ZKTGQ+2/6Zf70lFEYc0EcwadjqVgoVZ5OpY4svQXyfg3/PoRsX+shBiLuhn9zje2hKLgEQubo/M'
    b'XnxdEyfoOjNZw56vh6kpCAxQJdnQ1BpZz8kNkxsfQb+KTZZfZ6vtqpC2Z38CLz0NoSPy6gpZF4ObweRMJDbAMWa3sr'
    b'BFlrE9+MN/6dfA3e/+9zcXwN+gSSoOmuA3ZIcVxA1sSbpWkV4v3a70T5Gt3EtFBoyyQH+gbV3YrTV8c3oGA3zWTt/k'
    b'ifQ7yuap3jE9MOAUr3vp9Crukzq0v8N+pO6ovRkZsy2b14u7WDPOnRax0vwC67TozFqA3HYVzLs/QbsYf3xA5EFVyM'
    b'PYK0F4gZcViMbQhc3OW5OvOCyZ27fYFeqIVvdupN1wdc5nm+7kh+GKg/GcSTPnEiWgC8PcABz+gEMXe77uWecVCMxW'
    b'T8pyzXQ0goq/SasObni3UH++2P607wn4kjvl6GxLu9VI8xgTRwkAasSR9DJNh1OnN11Jm2nf0EkcSLw+VlevXPP6P/'
    b'Ld+oN9McvFTcjKumhdZ5A8aSoeqmmR7v+gSGnm/I19fy9XRLweqom2aKey3EAGUsMZC8ZjKywtrCB8V3j1f+A6N3lA'
    b'd6XIsBO3rG4kRgqVUGrlYjmX+I3v3t5uAk8SJobQE5tOY3fYJt3kWzFoENz+RbIcs0HPAX9F5C83htwA6FcnUUo3Aw'
    b't2HoLRhg5aXgTuM/HqAyA5FvP94F7Fg7/DdUXz7/EtQX1KQjlb3P6HJnK95fW02RVlYHI61w8QDehpJNB94OeMXup3'
    b'1HKdJA2MCLBLbzzNEiKn8mucPHy4tpcSir+i8OpbRncWioiN1wERWPQ7A+OJS+v/J3IdJ8vCz+KnrP7ntGD/8xtw47'
    b'jAxO2ahPOdG5slX/WLOjJw7KsE8Q/gLBcSeeC//S12C8FClMTFszXB8bZdsVe65W1YSSQCMI6TPYsfhGF/uQIri0sD'
    b'qtcBd6BLdALz8L9Pxv6A/cK6LV6EyxDFulFd7wuwG6ZKbsLiO7mYxXY8ywxqGPJdVx9x/G/MV6V/CmRfoAnTIi7K14'
    b'VwbvsBfrfUvHQCcRDuy6XcNC2wgcgReyFxoWhk5Ty8qItcF+dRR09dri4IS1P6AeeOGsHq5rXZleyu1Vy+OiyjGGCW'
    b'4PXrzIq6MoemZeEXuN1WLgFO1PR5GV7vnC759KzWoH3whc5Jv9Fq1UewcG9wjcsZLMfEqsLcUiWZyGTnZeJ8OfjkSS'
    b'gpXXZEDlrhJfRijGm3sJbVCbtsFTK4GWbB58lWfFn3RxFV9Vz6uY4wu+CXNWnjImaSvfwItMBBcjSsfthr8zlYsj5Y'
    b'9UD74viIOK1c1jr6fQe1hK3XAJVcf9oDKlz/qnG/XvNiIp1H6OpFDjUl88iO19WuJzIn7Uql7jFqxRi3uDwsLpzuLq'
    b'Grwerb4fPXUh2noIJR46DmRPQR6grDSa2geNo0vOZvh4amlUezqIt9PhXZAfxVtmcoqy0mKQJ8zQnq9EB3+MeeH0H4'
    b'fO4ZsreKfPsx/r3VSBiAVvL6uI2AxOTfob0pOVaqtyeaRc0l9iAZVW++QIzugpEMG17tfpWiyptqaOtlsNeCH/BlYL'
    b'oilwdbxR+t3lPrpsz83aZ6iKl7CKv76u30VWi1vaow36jVq8vmxF3/YMyGda9VlAsIQ5pphPnlYMh+a2ok77Ir4ztZ'
    b'kCpEWiVrypgeInegnehLVMex17i2BOAFZrEmorDCUXcCiZpwVA8rXK+VDYVeca772t5GITyl33cL0ORySDpqgFBg1S'
    b'diZbAuPib2HdWhT2AeMZWPYErNMVr2OYAXXTWWz0PcRRfOeZ0RQqpXIks6xOULoHyd+yT+MrKs+H2OokkT/Ex65l+y'
    b'mCYUL739WJNw9nmzTqiQ+H0H6WeTrwxYPAhaELEHen/69e6hV1nzDrYdNVVt1uMLSB7L/NqCbC+A3AGzRNaAO7qD24'
    b'A68NdmAwhmrV0wzABagUONW1GReQbo6ljAuhwCH3ILfztF4MZIDBPY8RZNYO0bNxPR5Ejbp2JpqFNjWc7xc6og0J+9'
    b'nsczQJkIRBC1mVmuwP1H4IuWKjXkws3QdfftmsKo8bQ1BlIrB+G39AfEV8Y3ui6vH9CA0AD2IeTEzzlzv9oP3RB8aP'
    b'kI5zwnA+Nzrn20rdmF02TbCdKThFCqjtjAzrRpo+n+tp8+Hj5gnzle4MOaHkAvZQGj1f2U9pgnyfPjR941yEUZwL6O'
    b'KqMxRoVAp36sEq2iNhKXAa35LqBDN0QDb3vacaEC/VjzH9zydIfUUWaAXZDPRGTrGzgEdu+0uAR/729QCPBL0R7a1B'
    b'Nhle8nQ0Xd2+CUlRcb7+gIEVmbGwdfZcnS7TZ1GjPdfMo6jROGrUAu3pFiTt138JXiH/cAdZknDKfo/hFFZQC+LaUt'
    b'qIYnQuv4E+RhESS/S7Ofw/MYeWhKuM4mDqKqSuPD7iOYnSXYdJm+UJtYXKD8ZX8DQjS4rEu+VDWFIU3S2PDtwtj2Wu'
    b'4XS3PI6HUbbJzOWkHNPbb0TrK+BcZEZ7vsk6Jd5A/QxY92l06/YmNFcJFLrNbtaWwjToF37q9ubfuq8HPrcPvOKWXn'
    b'hyH3j0E2/0wO/vgethZ7WP6nEov/HiUMZSHhz5eq3tVX0sW7SnX0Nu0YFjiRJ3cPhKx6AeJ3fwSbG2ZIvoQbXO9r6X'
    b'DL54fVed5AQlg0+afi+8VFe2h7A5GvvU9ocmSCucMS7N7XYvm7Ko0rLQSOFKnahYtASileLxykicjuJ6XQeJWIAOiQ'
    b'vRdg55oiEPpro72M56MsdrbHWTHj8QMAK0d1PIvLIGPdG2BhPF1SwQy9bHHZrN4ZPyQZzlbh8XuLFsUaMFT08SzGTW'
    b'beXF+oUtby8WjVKs+shZdRRQdxN3tI4ZXBjFBwOXZRYry7Aa+VIzk6wgyjW1Nxka+JxWLjdzt9dv97Eolm9kTnP7Oj'
    b'x7mtPBlAZdVsPLhy0kGTjw2ppp57Q3hD4H4ngcD5StDfsQp2jCcWD/rldwVjTf/5CBc0hwCV1now7Lg2azM2i/SPiH'
    b'3z87vqLXigL7VOthrOXSMTTmgsAb/goalihYSv5KgWz8Xm2jXu/4nlvi0ijlH85A5UP5Q+y87VGjdCuPYF60Bn/6PV'
    b'mDYd82sRuVWqNSaWz/nD1l7H8/zaXlUNMlx3p4hIV4BKlnZ7WnXw4wiBv+R2cQZ6lOaVywd6O4E7fg+0qqMLyZ7bh8'
    b'p7JbopO3a2uLpUDQMqireLk+rPSXfAx2zv0fEhAxTgIOq3b8EGzCXX8LbsIg13KHhaIyNOMOLEfSzbcoIHvUjn0mIO'
    b'NEq61bji6xTU92xctRJSuM06ZMtssiHmsd1n7/ld/fdgazTbUwt9d2QfTYUXnRcz8AuV1piU4YOnurbsd2tGgjXtWP'
    b'wvaZ8DyghU81szI8SGz7iylYUvT4Uc2Yaiy5kGJPnCzfhodujXymWTvzCgpgGEqtVav8DhqXsdCFGVMeTpJzIYNy8V'
    b'dSuHJxiujJMNHZG1DL+6/oDT5i6ieqj8EMCUZMicPXdd0BY9Ov8Mvgd05HHwmx9Dq9ooWCy+R8bSmINW0GbBMHwjVd'
    b'9PzdSOFrbMdEz1dGytnTU/sruiwhln6CKdjbPsfLIVn8VsKlWvS8gekP02nUk6B3tP0Rvm3doqc6pHdI3kXrQKIRLb'
    b'0T4XXc9IcddtFjQ/tDssVmbxE9m0ICXcbG617WL26KpdOMvbEIULHyJGJrK8ysW2m5CFOt+A1yOs6nw6s9/AqdujCF'
    b'Im9Wie8mWhZVZBkamIK0CJ+DmRIbOASj0J2KRE/iN7SukfN7NQGYZlsEssuilhBZCtTDlMn6iaj2JWKnpARqovgQtG'
    b'3ALpSMS8ShMTeF07ajZs6nBxgG2q+Km3QdnnRAnq5rM3RfVPGHiJ4skMfbttAZ6Y9Kt2HZrECIFCkO2815GY+wEgjb'
    b'UXQsRbZdZWzAWEcTHTDwxR8KXFFnHGuQ7FhBLCGOCLf/WvGb5BtxTPCAUB8NHBuKYKT4w0SPNyQYuG7P9WjwvyPkX/'
    b'RzVd9+pgf7yVEVjODFga6i1qxt+zPFb1LW0RBiz7fAQD4TGN5tGumpBtHj1mlCy/+zHu0B3x9ph8kZizSUPC1xkpzZ'
    b'l2Z//1KQZm8J6SGbECKbgSFoBCgORCThieZAlCJGsTR5ooVRLE2x9MsAFutBCmnDrUdNNNtqRbIfBKkZNU1kDxOmpI'
    b'sexdBDuM2a+FKQcP/T0DeIBjfo4TfE0hJkg0Ve0bNcP4vmjmZbjRSJK2M+tpZspOUwy0BYYk1T8LWoxXZZLKVYbb/p'
    b'qknwIKp/wjgeScAN7kNbeE0CEspN55JWmaXrgkEKW+mQUhu9A0MCfmsuxOTovsl4q0O7SU8eWHjH/K6kVRUG6SZ2ui'
    b'dLZdsg5cswpXKgduY1yiaKCppMziUVhYilb/WEN9xzrZ5f+xCXD0Zpf1+kMJgYseU0O1OpRVRCPS2Gki/xrgdTKFZu'
    b'xxFNabGGV7IOpUXEP5VaiNISHV5Z8hVqm5JpbwVlq/x+QKVmKvkW1Y27MCW8LrwjvIElrYI1mBTdfh9FyGqlQDutDP'
    b'YCd5RpQv2bRuZASa1HSbJ1t+VdJE2QJPD+GiXIzDPUNbd+/KaRttQOLfG//f7HSCmmI+nq3vl8nCi3zzkww9iIAjsO'
    b'ZKpQnBfdJ4YOdPWDX937QPew0R1wmBJJEDpcpwNz/UBdP3TWD671w+3AyTgdljPlVWq88zX93KMj/pzWpceT8bJyio'
    b'c8HnflQ70hRlhqJEu1slQLSzWyVDNLjWWpo1hqNEsdwlKjWOoqlrqccYqKzMlXQHcj4OSNwikqMqeoyJyiInOKisx7'
    b'Y2n0Bi1hu4iV7CL9eBfFRN5F87yLTnN2EWfeRQx3FzHcXRQTeRfFRN5FljolaPi54qytpRIFke6PQHFTHWbc/aeauY'
    b'GvMDI3/m8G2tkXUe5vYjWmjkdeFaal+WUvTaPdCzO5bBu53yhFXlg3BdgYBnIrXdC7SMP0NYeBwDswwqF2EYscoPAw'
    b'0m/4s9hnYLLDx0yxyPewynufpYAt5+ctuxkDGO/UpRh7C0UzDny0Kgd07r3i3rVxtAPV9YY0UQJW1kN61Bbdgtopzu'
    b'V0RoE27DlHyHqacViMPRIwbsO/DIw8oFTo9j8ypSkBe3BlwAaneAOJvnY3f3aUvjmkDq3TFaEJvW2hHXcul3rjS/KK'
    b'npgqFFfFgdXz5iuAPtXRrD7/Owz5sQaNcK6SGrQEgmSNITWaVHuLbtJzToXN2H8M1qceJg8jlHfM7Dul11TglN5Vj7'
    b'IlWVHkKNQ+iyJJoSOvPjuId61a1AvAVs+watwCOrTr2nGmW9HboCiSF4E2FqUqcftfFtBA4tZYEYhzoJxZ+wX1+tMB'
    b'sv99+C/b2vF837Ze+e4qbTn/9BNt/X+5RbkP'
))
