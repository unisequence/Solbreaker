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
OUTPUT_SHA256 = 'd639bd4999ba62ef62aac68761a019eaa7197e8751a7fe6d7fd6932ba7bf507c'
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
}

BLOB = zlib.decompress(base64.b64decode(
    b'eNrtfX1cVNXW8BmYkUEnz1BUVFZ0o0KzhPTe66QmioMUTo2OA6hgkmCiKDxwjlZKgYdBd7tj3PDrPn1cb/WU92Zvlt3y'
    b'q+TDHCwqRNNJqaioDjG3MExQy3nXWmeGD6899/b+3j+b38/DOWt/r732WmvvvdZSE+CXeYvp21eN9dMDsiUgmzUtEAg4'
    b'tb9cIwjKPsus9vw3Tp91s69crN5Utfg6Q6DJUyNZ1Oqke18UWO2c7MAXiTVzsuvdYkZNeZ11+XUG9lWGSy01s3rVril+'
    b'qxhl1wJ/dAaaRatdYz4xKlzN65lzL5SAjOzDdBdUvEhYWAb9WBS+SFhkWAhvZTMCVUknloYrPQHZmFjTkcj2J9Ywe6dq'
    b'71wkOKGTGemB6nWQgXnhJbZrabgnIB3ldrPN3imuqxHfqFHzTs6ZG2wlXWv6nSCU7995qyC4yv1PQQPMsxBGaUpouN7A'
    b'1s+DV+bJwuc/CvD5geeAWLEKsmGje5gdnvuYvYHZG5m9idkPM7uP2VuYvZXZ2xiMyu6HvnEPllU3VZ2BZ1nLOawzFyEr'
    b'BolvhPP19O6oUo3vGKFmJ6ceaKcw3/5MzynJmlhTXlrlmpyWCCNg+ztMybaz8jt6rXvvgiJq2U8/YaWZANDBXMWu8/VY'
    b'k/jGUxF3Y9PJo4Pt2ssC/8AXbR60oZSWCbNEz39DPcrZwTvuhL9eTxGkCkZ8WwlvkQAT7d7EZrb+AfhUzg6RTTtchCwJ'
    b'vqGL1/GZn6xTJweUntLll/Ipn2xSesTlFrZeIgRiocXHY1SqFsaH/TTU82SraA1X/DHcFc2mxFnZzLhoMcoYF+w7oV3v'
    b'Z6BpqhJwi55Y7Hy/6dMuuw5mz3t2pCCkuxS/hW/qDPyX0XNc9HAklS8M+lwtMrL9C4UEQVjYYoAvIVAV9wMQSD2Q6wJ1'
    b'9bUnoU+BDY91Lg3na2uQcurU1SMQOIO5NebwBw5yt8YdfpY8Vl09jhICB9WNT53DWetiyeNZ5yKDNvXnQGA21DhUfMPe'
    b'NT53+SCmVCGGnsEuQaeZj7od2PwTNASdT2dHgL6nB6pOnloaDph1n/TUEEVfx9PHe5qlH1XHyemwOKDKbwPVCUDINvvJ'
    b'EAX3zJmLC8s6eSxUxIunufjsVFYUzeVhLNPKs2PEKAEWlZnNTp3DiqfNVR5NDRMEQ7YYNXlsvTvxAKzZR4cNrjguRUy6'
    b'PcsckH5IrKk4PicbatfX3tF0bce1uC7+ejOsC1av+IcpD0SLy8J4TNk4wWvvBDQKcgS84OSolYk/BgKJzYkHvHY/AmAF'
    b'qJVjAcbcrcwB68DH3S3Mp61CWqkRK55Fkk0epG5oPYmZNS2fEiQTrWRu19QN/4QUbYYOxnag+wIl9GCCrS8hPJRggPa0'
    b'2L4EYyhhCCZE9CWYIOHR8AroxwIAjs+WwpM7vsfPDPhMLhsXK51pn4YE6PZBnz84RwUvUZMj1A2XYVV7cF3a2zp2qxuS'
    b'cIw+7e9ICRuu+5Fy3guAPyMgbRB8XawXYg2ah4q1dlSwBnVDPMIcmlYEQO7QuoFp+MW3DaqzqexMpLg6GxdZqX+8+LgL'
    b'V47bP3iSOiUM+Iu/42Oo/nbqk7yBA9vbMBWriuDAbqYaObCbqWbA4lQLm2rlwIWmRkP+wfoY7hDfBqRjExHLbytbEXG7'
    b'PPLRNLM6nQDXla2IzJavUrQEpX4wlIe85+ADqgGMyICiHnXD3fpwd/9M1Q2FKmIlk1If0RGu1JuVfcOCJI4MH3kr+8DF'
    b'DgHpwPzjxPoB9yIykih7i3ImV74IqAXpaL+9JbEjIvGAciZy2bUwgr2Dkb02q6kG5uvuTmzu7gagUpewZqaphdKYL7F5'
    b'/5SLPrmhu1udFAYVq5VC+w6BslzUorpbuMM3I9DUn1Ewr1re9GK+kK7KLS72HjsB3YIs7LSqlLW9L5TXx2IiAE5AZwOH'
    b'uN03oPTRdLbfpf11GCyJ+rXxwCf9Zubo1D7uAXycYGfUTYKQJJiGf/aqEXoDs8Jlv4sdDxyBGqgSb9JIlKaJB2Cdpyaw'
    b'Na04ioOrU5NWO6focsg1HSjf0yxHJJ5qTzYg5XayQ6Z915aF28LkDv6QEaRYGwBNmUWPhNsiZJ/yyDBBagbILsSh9t0Z'
    b'FBYwK3u5AKRhzv1rHLYxKJdfhC+2WvkF28NmOdI21yhWfEFLo3NnLmb5QTtxB3aNdbK146Eu7R4gucQab+Xrgv5jKdEs'
    b'xcpSLCwlnqWMYSmxLGUYS4lhKStZygOssgbzVFoM+DTT00pPgZ4/Ueo+epJ4rdxEz3UkQ72V20KtVI6k/HH0jKXnMHqO'
    b'oWcCPX30bKKnk56p9FwDT2/lU4ZgXQ+b2VzjbNZJyNe+uhLmre7IjYIAqO4SrdlmeLgt8HBYxfh6Mf6LLnEW/qvvEjP3'
    b'ifGfw+vn+NolTq8RR63aBghZNrRLnOuDf03wr6FjqJhxHMo/iiJKjD/UJUbFZMPDaYRHqlmXTQ9hs957oVmgHtB4Qouh'
    b'jU+eyWdkMmMcCDwjd2XBHzOsXw3XOmu4OTkOu6eJbzbValZ4nTMTHkv8XeKIGnh5cTzyhn3Q+xHvclcCdBW6mdkAfx/H'
    b'fkKO1a/T3+xaeH/cSe+zDsI/6H3mnFR4aYd/js4uMesY5HBlwhcMalZyFqS7psEj2QlpH8HLxh1UOqsWCBMajJozDx6u'
    b'3I7FrGF4U6h/I6DtEd9CBY3Yuywqkfk+9meP3g9ApTUNSmZ9DMAWSPwYAbkAOBisc9ZhgB5E6EJsjRoJDgwHsbgAHvMl'
    b'zwFIuAZzFe3AiYavZMif2QH/WsUp0I2oFMB/5rvwkm4Gck4D7EQtxY4vgH5G3R09QPMj8av44/mqCiQZ+2G+ag29+HbM'
    b'xAW44TFcJZYwzwHmaJQS1dKWwOumMmTs1WVzyiFfU0agmTsa1eqiq5sEeFerkYmw04HmTG5vmj0Ba5EjdmTCn45XB2gt'
    b'82KQMuxxutYyjDl8fM3m70BxaZbcngPSdFbPjqhl5ms8uPiTlYfiBGnCYrcvRlUSDj4pqOsuHwaMBzo0IyPQBGJCkC4F'
    b'WaUq26741MBOlNfhwsMknatBhw6FWs9QV8aZlbbOwPKfdO23+XKU8ruvRykP1FnA7K8zxzYtu5s4/GhlxVhBuoXbXzfN'
    b'M70kkH5/La/CJcvcW5QzYXJU2Y4XccVJZmWFVZAf6rgG1nRyTAD4PEt+oFfrUe/vube9G1gUT46FOqK4e9vwuvIzn4OM'
    b'FivHgEBWSrcJNKmi3b4N9QgfrePkWK+9Bd+8dmSZoLpx0iVA1QstH5BxU+LMHfN58lhlNzIMmJnnpQWLlgeaFnhqHsxW'
    b'diMv0SuXpwcr3ikIYcLAqr0K5jP8b40AHtgxbdZI3P1Y20uNtAmgYpCidf2A8n0rMN/BKO02t58lLrt1nIIMUophdZBr'
    b'JzHrVV2ACftWRimI3U5DELvv8NQioIC3uSHgjGX2pwD0GmRY4LU3QmsBk/PqJ2B8m7CBKvGNij9agBCcqr3RyR1Pcfum'
    b'QFMZTJiFBstOH2qNtFeB4myUDycGWOnmxEBvArOvuz55peTeFYFaf2CINER8Y90fN8EMKXWjZ3DHVhVR8LqWBIMqL/Xh'
    b'TmCWfBO3t3B3lc3dKg+DjsX8S8ODuHtdpHszNGmR1/EVMdy+GVrpPsqSY67zdnuxcelKnjaMV7fpA1dOGFjaMOVzg9YG'
    b'+5duL2p50g08eZjSGcaSh20JQ4VtC6l4ehHHFkT1m5QZFTzpd5BZT1M6rVBEabUqPQbIdMUBpdakqZTTjDmNStoDsd1e'
    b'K75f31tqTBkUugG3mH3FIrR0KCbu+mZICRZK6vbGYqGI8rQHkEAgYXBJXG8NZSepCqGsfxXR2iU/IbUPq/0mvPwbATbM'
    b'kSseYN7ybxTIBsO6ZlEstz+vk2i48qFxUVT/T/Op5GFWsWIxNKasiBfEihyisW0gGiZkYw9Wo+zjySOB2JK4ayZPywRi'
    b'Ve07WHQcm9YrU7Sw00hnO7h7DwgM5ioC7SO5gMOe1ZXAkiVvMkpcoOHVyUnAVV9o0MXHMzUk0+p1zj6rURz13nIR4Bsa'
    b'CV7XJd5bi2WmQIYmMf49ZN/iqEOU55lGquPeYwS3uop0Jm9du4/KtoujDlK+tU36N2T9Fhm6hJkLaAyNwWXJHS3KhyCe'
    b'Pw5KrmO6mIrKBrqE7yzk8o7W0IBhn4EDjurBakH4RC0uIhk5C96tlVlBMYYSvxGFOiTg2Owgd6LSClCQSB2XEz6lCFBw'
    b'HaegmowaMV4T4xuGrHig21sEuyHZ6gnIlygrxsCEIMF0ROz4I/4x7fg79tcVC+w7kju2DT9oqy2+bwBHw75Cp6OWtABL'
    b'Yz3wZm8FSpXCeXK8E7gNOx3kN8Bs2DHgOn0cR2u/ifgNKtzl3/ylFXUztmKMzl1DvFUXK5EXo1j57mpUOD4g4WaFDay6'
    b'+lna4FbjBhd3XwruvjYuhTHOAChteO1+qH5p6KwlLHTWkpQeqE6ioxSlJyDdPeCcBc+DMgLVhztxv4zcgNstic02e6c8'
    b'FMBlnbinhdbW1zF3W4Z+EMPdbaBIDIFKF1Jyx6WQBrr9osiArIHyjek1UitKjSZdWO6MQX4FgkotXafdGIWySoThuZR9'
    b'ZYwOJhJrul7SDxm8djogUZ/ZRCcdQ/RDB/GNcVyV9CMPNQM423ospeX/E9laGbK1THkot1ewKT+v4/Y18GcTp4LJtnq5'
    b'TrVXLcpV1w47R4cWWFLdgKc3mnpuwDlEOpfNLm2VFbcHhbBJUFPeV3qGLLtY2wHbYSab55haF19nmJsdaF1cE6NWhONh'
    b'DW3gQ0WnUtEx/YuWhooKBaGiCVB0z5neorTB0ixU9PRVIFbu8tN2pN60bcT1BtjL+LR7AWLrlK9nXlYHGr5PM8PA2Rl4'
    b'+es5/QWH0J47XhD6FCO11Oxi57TtIla8+Src7cTyCP4I6CkgN1q0LztQ0PnUTXhEBiJB0UZDl5eblB6jnJIhWlOMij+G'
    b'znFSonl6TOhkhztaA3JL4BDzAll4XkCOW9pym7gaNwWwfmEVaC3jBKEd1a7yiTg18iN8qjEVT5SWKY+YxeVFKujOjlbc'
    b'l9sPQ13as9CT/6TBTFi4Y4NtbKHNqzw8IB/+tyX3ojxCcuxB7NTFCnRsA0jfOBRxUwEKvvbRt0GkVxk+AaTTbgzwO/ef'
    b'pEHdyer0nRgochOUt50kAs3sNRxkh9VGf2XcsK/CN2VXGSlTlyi7HqOXwThB2e3TfuyjF2w+hpo3YfPpoeaT3vi0X/Of'
    b'+an5SOU7M9uGUipY3OXSjl+EhRuugHyOHu2yb/HkhtVrL/vxJKKHjjqm99iq2vSeNTN6mw2q59ufGuYAHQa3qzzbzEst'
    b'qvL+kVeNGa5A01xAsliBqjTPsEIPbjwR3JBK8BHxT2oF9rS0E+SrcaPJ0i2QPFmt9n2/KtxmhRlyAn8AJWKGBZJ4mU8/'
    b'tbiOp44FlehKPmksda56yhOPYPZI9gPUvGYEiopOnKQfgtR8+Y8DFydi7GsLDvoQsBMtrh3WsWxFpAEXM1XNrzAAK4Pl'
    b'gtUVdVKbN7H98DEd11Mtpy4jMtm7w+uUM7nL9Ja3ncRm8CCuziLoSNEeoWYKYvQ1YwcMWZlD43IM7vrsFjociGZ2IETY'
    b'B7ZweywejdnjgJw1VQuiSLu+g/qwnNUHSSdcXszt2rhkowxarpk72nhSvM0gp+OxyG7cLwsctJMJMKyJyybZkuNJhrHv'
    b'Ufb7eYaRgx4dzyOYaxpLTuVTzSzZyQfxdCNLnslcmTwFAFkc2r0P13VyHHcf5g4fk2O43IqnEDAEdxwv9TM5GoSUqWiP'
    b'YuByG3PEslILd2jAo5h7mLIvFvEdog5t2RBExPzL6agERg5cjjk6ER12v/bsN0Gas31LI43L5ZfSQK3yFdzdOc5plKP4'
    b'UFtqPGjSzcwZr731HfSs1AqdUJWm7AoDtAlzDu3PnqO3ev6+hp1zaV2DsQutl2EXYGtrFZnjeZTDT2m/09uvNZXVrQJt'
    b'ejNrYI5N2tZ2/XDyZ1xB9aCOAZZDHbtE9HxNRCBWfIrfyTDyTaKnmeT+SE+zWOHF1x24F8BlsBO/ItQ/jX+5JNzWJA1V'
    b'/1R0DGi8SfT8BWtvUOUtWhe0B7K+lnVq78NciVb7FmJGSgPpxqi1eRXcmtCSVnDfotqbWDVqR+wMMS2OQP4kQaYB68qK'
    b'i9a+IfIGnXwzd4BGCdU2hXgc0J27jXhcJ3I5dkbb1Y5nCQn9NIk6o4CnD08Bpi6DCqCafonB6YUFxUvN2hORiOBHLoU1'
    b'1fQVNmrBNZVhEqJlvAoKyCOUUosgxXE6qeFJY3lqFizkaFWZ9x0gwyAPXmSALaqzSejdoNaZhfOmUhuDrXivuBT3yuV+'
    b'ZIvMUcbOatlf6eulwlSU9JGgS3sQ5NpX3xB/h86X6ZJ8QP+VHbReWL0cqewYS9x4ZO8q+x2Wz9KC07IfvwpG4lxUwMCO'
    b'aeZbUR03C9KNeolx4fIwZXcPMmqobXc01TYYcx/TFsCOOlSvSLce3F4GZCPS/QvnW5BQDoRyl8ZiK2UgoVVUMvg2oqNT'
    b'YsWeMORuFXzQhUjp6TDaA+ClzEr9dFtSdtNeVrpWtOpkRGTDdZoBldLdBppnU0c0Dq3kG32gQFTatH8SASKlwC6CiAWk'
    b'paMNyMQKzG5aXHTiAU39BoklhvFWIXQvhENWduM1B+jEYhjhSrvyG9pH3J0Kj/QpoNWC6p0+Fh53j2eogqdMU+1b4TMl'
    b'Sb89AlJzN3E7KveOw8B9UDv2hXbeIOvxdIwyarf5qea8rXiIt82rhE4o4VN+nVfjkSJo/V4ldKZo245njaJnCqITFilJ'
    b'nYbhTaozoJy5T1yN+x86AGzUD//wCAuP0qYfEEc93obqY2Wkgap/aYvejPI8Vhj/E/TxZb3v1vX631k9mG0rZZt1COo5'
    b'Ksafg9ef6XXURo2q20XpIzDvRr9eDtrN+q5LnHsUYG8F64L6s7zwvbeVvufWivEfAhj2XrNW45WLOMpL+6i9J/X9Vg+k'
    b'c7pughwfQcIrWLD3hA73O6vHU9YRZ8RReLTnRVCP3hqenPFtNLD3qdpXgtV+S3upWXggR7gGGmWEUsbXEVN6Sj+Ge+55'
    b'/e+TOpaitiMaOsISTyE5DPmK6OyEk3nx8+FRoE14TkmXK7tTacUMzQgtsImw2DJ7122gSXkLaQzWG5DmDiLNNewHIk5H'
    b'A89u7GV5J9uIMAfsh2BVfAUCWDvyJa2MS7kT2LQ0lLstnQZnEndb4c+U0B1woxF3Fv+w4s7CP4Y5aliX9jNgUHXvQ0lg'
    b'1r5pQ+Von7phG+yacBw1Wts3CGqAV3VDDUC1QwTwEeAgAuoI0EiAzxHwKgGaCHASAZsJcJgApm5oD3TaxwAGUg4P7k5k'
    b'wJ7IUaMqQlm7od8VRR+7NOqKhzaEBnBS7D+A8f0HcGNoAJd1hwZwfb/u3QBQ7eJ+3fsjAgz9ujc12L1/fv2v3bP+m+6p'
    b'b40BAefScsJRYNyN+wp/IyCbqQkA16o/w+tqfAU9jO03JT2rgfxQqzOwyUpq6vQMaMqDWXiqMUOtxDsbvL1hBFMr6ZzT'
    b'E49VNPH1CMPl/h2RrLn8AatgxUMTFN3qW9OoROe2NwV2EM9P35qH0pe9NRL+LLNwD6Yzz0hSn8x062lCTU7Q29J7oVZL'
    b'iKDXviBZs4Kvz0Ig3n4/vHxJYo361gL4Tnr0sZizkG2NJULpscidAFdLsIqnsfJHh4WvGWZIstXJ34MwVSdb+NNU/z3W'
    b'4YfU9VjeUMs+tHmlwUmPTgvvWmMJ6/hBXX8/wDO4wwwDuHuRQFjBUavqNH3sdvNA3Gs/GlB8HrsoeEHOHH6Qndd9ihOL'
    b'lgmmqjEfCLr6i2cAYsXTgq5Pb6yAniee0vYC4dBFuZWgX+CwX0LY/t6rp5JWtAAQ19fSdr/ioJTlOS5lBlUnVrUZa6yi'
    b'q3C7BoXEN9+t/Ra5Ul2IK6lV1rk7kC0eYB/rR/5RShZUHWgGtReaDZbu1J77knTa84Z4MQ2x26IP0UqqpgajHPcJ3a3C'
    b'wEyxn7yJtIki9V3sTspY2FaGiavfotH68ZpYH1rTl7pWgccS2n9/hrewbaAtiBXVQfUP7QqCgl3BkcmlUFjMOABcswz7'
    b'LI6qLV6EFy8/EZtuKM6GEa2egnc2D48N7xJH1qyxd8k21d61yKA+nkUnCzAiMibQ/gB67uxZQFGROjOHrFgKDQrkoDmB'
    b'+Ia7i3rcpq3/ole5AVRwY1w9j4Z/w+DfGPg3Pq5eLskrHn3brbkFBcLUPGlSyYNL56flPeiScqQ8BGTkL80tXD5zYXFe'
    b'Tq6zuHB+XknJnbnClBwpJ8tVWHAfgBfnFc/MX5LnkgqLsvKWSnnFty7PWfaLOZbkFd+fRzmW5y9dsoTahfSleQXBTkwr'
    b'zMmdln9fcU7xg5MEZ0HOg65CeWnuJKGkr668JUWFxTkFUwvypfkLhbyC/CX5S3Ok/MKl2UKJXFSctyRn/oPZwv3FOcvy'
    b'JXi5L//+e+8rLCnJFpbk5N57f86SPHiVl2IxKS83W5iTs2RJYayQm7cgRy6Q4HtJYW5erDCnIH9ZXgn8laDv8Oe+Qgm/'
    b'lhZKWH5yjiQV5KW6p4yCf9NyHswrFiYVFDw4My9niTMHRpNcuFTKyV+aVzxqAFiwL81bcqFcA+ECPUt6k6GBQlkC/Et5'
    b'86XC4lHnJY+aWVg0Cr8A/6P0nuEEypSjuLBglGt+YXHe5PuFgrwF0qgc6JDeSnH+/QulUXnYtA6ACcOZgiGFXtNzCuS8'
    b'0EdyQeH8xUJqztJcIbVQLhbcd95TlDMfkJxcCFOyFFoXHIXL8lLyC/LsD8CULcwrCE2rKxWIyZkjLUwpLlxy55Rp+SVS'
    b'Rr85TYZZKc6ZXACVFhdChqX5Un5OgQMmov/3NJyR/oBJMHMDCuQvlaWBWSbDtCWeD7iNAKlA03nFvY0EP/tVoUP6auj7'
    b'vk1w5UlS/tL7S0aNGDFqWuH8nIIZkOGeIiTDEqGP8DHzKME1H2ahID0/b/moEnrtnbreGeot4SjEOhAVMJXzF99fjPQ/'
    b'yp6bjxPfr2LI4CrIh/4IA4HOvOL5OBP4m4O9vmVBYfGSbLyeEMwRixcJ/X7U8VGu+yYXFpZICYIwFLnH+fDEf4WnFDyY'
    b'gOUt/wpPvBAcpjfhF+AXzI9MAAtcdAF44gXgrqKc5UsvAEdqzL0A/B7KLQhDzoPP/L3tgvC09MQLwp0P3Zl+QTzkF+dd'
    b'qP8Iv1D/kwvyci7U/xQZls4F6gdCLMCBnT8vUwtzL4hngA/Ac+jnEu4RpgmThRmCXZgkpMFzhnAvfTkBPkmYBV8plGcK'
    b'pZ3/+0/K49d0wQ1PlzAT+zBkcMQgtK8yhIWbBkWYIwcPEX7pN4AO++HnQr+BdIicrQ8//y6/I7+kBJgWwf6T/DNzQIr9'
    b'ivrvkpcUXQj/v5R/Rl4BUPp/Xv9Mpw4b/B/mvzvvAenX9Afp9tfkR3ou/hXzhfT8a+oHeh6wHn8pP3DGEOnQz9wHD5EI'
    b'/SL74CFSGIBPlH/BKT8/f2hq6RfRBw9N4fn5Z+YV5IH6Ig3oP8BxSmbmLF18PlxvNzgBF/XBQyg+f1whVJ7fnxDK6Dco'
    b'CL/11qzcwvnyEpAXJVl90viWxXkP3geKX8mtUr/BXeD3S+WXkAy75T4QR/9rFf9JeejIry7f2//zCt98XvmEhJLYlbEJ'
    b'CQmx9hCobGCOsqmDj14/Hl4mCsKqZEHYvBf+TXy/ceve0aOrAVY2ccaOa+4QhMeShbJnp7zfOG3C6NElAG9OXrDg6zve'
    b'b7RMdKqXT1iw4MaJ/uk7JtT/uHDijmtMEx+v+/GO4+KBpP2fzp/4xOBBE2ueKIcysUlZj0++o/Bi5XZIn5D1eB2Ubxw/'
    b'evToidd6Fk8cU/bHcfC+F973Dj763ARo6w7/9LFQ71h4b5xw9LkiqKNqkiAkIeytwovDbxeEhsnCb7/ffr/9fvv99vvt'
    b'99vvt99vv/9vv+BVNy81u7QXvw0EyuvX4Z3NV/sDAb42F28S6z0HpDHcEKAjblbrqZFu5IJaTU4bTfKVAacRDVbKdnwM'
    b'yVfJg1Vl25LPDOxEoIl1smPaaCMUKjWrStW+dQDtZz7BPnC5+F1mVr/f3hXgkxI8NXJEYk3722TowY6q1c7Pn8YWOtTq'
    b'bRvLwmzOJLlVdXcptStVRWgpC5uxyLDo2kBTWWkX9Ep+BzpRi/3aMeoZLPWP8nrn16vCbJlG+e+Lbgwcoo5v9gozU1y3'
    b'eJOM0yZNvmV/ktlVfg4tD2QzX40X2x2XdO8X5qHtEl99Ei/3wvlUK5sUyadGc7clCd2MJl2Eti3/FcUmXcrtZjb9ajbp'
    b'Cm637p90g4FNuo5MIfuscmrVVXQDQ+ZmLbpnBBmzd4qsZ1k4GRr7E0+pdwWU08blsarFyhw+8W2reldT2enI5eLiU4OT'
    b'VOmyJu72wZC+HmDpHnT7QGsnGNlFrH6GWo5WBuzjQBPP/ulAve6yypo8zdJ09i43D/+4/PTrayvCJbH8dOs7FeGybSde'
    b'TXaYdi5EwwWLObEGZvpGPgnn82pbg3QFz7TaDskmnhTdIdqOidX14hvHudHX8Znu8Ep3css1oJn9804GAuTbw1c9b9Cv'
    b'EBhZeuWSHcIh6WG8V9mQjo5lrvfwbg7aWESwfIRNfA8vMeBrLfpeKVuR7nq04SHoPwj6ug69JFR+BKVZkFwD7wZzXoU3'
    b'l8oePWf7u/ixDz4yBzg5fUB3RNhZNFBgj9LT7ofR36rs2krXtVcpu16nl4uVXXvoJVLZtY+s6wejudAZ7Wc0jlyNPjrq'
    b'X7ZRBZpN9ks383GegHQDd/1E9xedmgz5bF5ZhO7pZms9Wuo5un65JBFdSkXMCjWe0NLQWNFrGycZE2sY+vNGn5o6LUwa'
    b'CiUhd48WhenjuF1T6s22evlPyj5Lv2FR5eTAdeAd3cRnu27ig0ZdYsjEp0n04G228oglTFz9BN329HpRhfMMC9BAswRU'
    b'b7YpJ8k0blw1mn5IYxh9c+7H51SLVzFCe3fQ9XgPmVcD9tBirEEz/YxY/4nsMjTycdPNR8LYCXFdnZb2Mw6eK51Y9kQw'
    b'aY64vg6N2m79GU0AQw4k134NtOU1/hAI6Ld6bsQDeZ5pX+3Tx9iiX+Th9a9f3bgtWhC0rgbytsRU4iVQAO/0NkdjX9Fz'
    b'SzvYQPhXLESq0mDu7tTuQc/iOryGs/tZNVojqRtXYnXPNwQt/ELGL+T4oKDzV8dVen51YxbmfAhzUkLQxdWMFZ86i9AY'
    b'A/UTWu1UdlvJYHp9nTbxJ8IFVqLpmDAAioLz2n7dT+QC1YcP8SvEx8lOxAfykli8wVtFbmilPrSl4+42rRLaG0ftSfd7'
    b'7Zqg+4fuRDbntfvp040+irqjyUBr7E7dGrtNm4MuEmSNfT18JgY4R8SZau5chxa3u8n/bTeir/1TtN7XJ1H0jCcD/jZg'
    b'+2HB+RmBEAWbTTzF5FaocAh28vMz5CXK7a2Jp2xEK9KQoENkdzc7wl1jEw8wRwvQ4u/3EkudmuBk7sOBZu4+jN4DV/Tz'
    b'/WjxnJIjryd6FKd0T1UCg+Qm5Zxh2QSd/KDJu3mpjzva1I2tOE2H9pOtxMXYj1loRY1mMsblwztMOvc2lhk6O66GSgEh'
    b'LYzqRTt16mV7N13RKudM4mq/bhVvIOcaTfe3iRX+FclOnNwLo/lkTxDN8rr2p4UQIg2ip4pWjyXISldRfW3aS6eRFG0K'
    b'UqdU1H9yk4RfMbkPh1qVRsKnPrVsu9A3wUCl1eToWI1JIWofGFDgkS+Q7y/8Dvg+EaKPOQ7bHkXmL1b8D3HV58kMCY0s'
    b'D6kbRiKXnvUOLoUkfdUBW/OpG/6I8Env6OtxCi0SH6seSVSDGRObtSvJveMwZZ9C7H0eLmILFjrAFNJReBE9C+i5sJ8E'
    b'olLTsZGWfWS4AkKI6iCZR4W1un3kB6G6WwCHFxNLw4rUyjmn0IgN86AzL5my+gC5mMWUYPpAINMEVH6AvtSNbdc8gAa7'
    b'g5hS1Fs3NAgjyEQ/tmoULDvNekyFo10v9beO88GKufd00LDmsLpBQGvDm/dhP3US1nkOUVwMDaITKTmScviF/gzHp3m6'
    b'Q/wXWYsvuDj1xQDcJTiN7fXdA4MMpIP+10r6nx9v68mSVft0D8lVNHqsTzeVfYb2KLoU8zKfNm8vpA5F7YOYfx1aZP9M'
    b'tQZNyc266UIG1usd7w/ycS4HDTTK9vQaaAiX7Dekk4WGXr3O5QNvk2my3cxjoIU7PMel25WHo0XWuSyMD1I3NiFLd2ha'
    b'VA2t6Gu4Q1N26qLmz7gEZIvOcn3akVMDTCrM/e5/CguSF+YsvT8PT671q+IBvyV5S+Up+TkFhffjNemA3JNlSSpcGjor'
    b'L8ELPL2CfykTTA0VmHRffkG+FLywDpYvLMiblrdswHdvb+gOoKTEmT9/cV7xrzg/Lswpzv1/Pf/uLY/nz3y0c/oMxY9u'
    b'v8a49Axt7CVogmj2oKeuMU7NjTPOnpUpejbc8PNZvhZt5tp/jvv5LM39nE9x7pO+1ed+JLM3oc3SIZzjpl6DaNFTh5x3'
    b'eoC9V37m3CNoxfQqLl6y6UdzlueI0TWZyFl0kOhBOxXl4ThBX+Sq/YgYv9pC7jHkew98wd5Aa9YXlCrM0djdzUsbJqH7'
    b'PFDQMVavnIlc/plqb0lsTgLiGnLnozMvOrrG+pqho5N4egXa6NsblZqw/VNMLbGqM5ydgLdPUuHxKfD0Jub2A8tYeCny'
    b'TfRJPc8XH9ZTxie4nqag9bHfDNoLr6LoG9vQGUndvQl05gyoBIWaHxbw71RFd2HCWBgT1Wo0T5zhDByBr8QM4jMj1Oo9'
    b'fcBhsI0IU6uXAWT5ILV6JvxdGC6E6VxpgCPu0Racgz2gqacnnoJZMCunc2EFivYTttfQOhi6JuWyerXMOeK+MHRQ6oRV'
    b'WYFM5wB7FDN0pIhv1ta2WyFjeX3TsvvCusQRtf2sLRucukHTrNVJuqWrMW4RBiA5BFWBBpMKTJEaGuiZvJ81uJRdccEd'
    b'wk03r0Ln9514gTYhHN6Wpy2uydxpJCHQfYK8+IfXdvyB70It4+ZV5C9fC1rytROew9wxO3Jppof3iK/U7kTh031CtYyF'
    b'7Q35/x/T90vtHaBEB5t38R2ovicet50tGcd6hvsOagbf4gZL+NpGANui5Kt4MWyKbP9llK/hk8y2VWSZRKFJOi4qMamZ'
    b'ho53mLejFnZD/b2K2BGXln0cZ/4uNCX2xyq7ksh7YSKzHxYraBf9Ll9L1roHxAqsdMd8MktVhgH2dg8mhcKPF2i2ulAw'
    b'iZS6Xd+E63LIdlRMObqXBuUG6dLdpdQkIFWTeFrj/gRVKNC5awDyBUC6u9hRzAUq1Bo0+DpsCkWRcH8J9IvGdSBc12IX'
    b'FxlMZKd+Hh2zQ/yJOLItlu7CSeN8pC65LMw7/MPyADoFLEsYXscUktu740lp8DQ/LKqrsNM7tXB9DjNYLc6D1gxzkNnb'
    b'hqn1/7wosIPsK1gxNxxDvF38NeINIxe4MYbBEj2GAZej0Ybajw6GnWhFfRIorAkz+TETgKxJmDNzGGY/CdT4iW5MnFmP'
    b'0JHw8bH+kZk0HiBPTiHbtXqx8jnd6vm5VAK0ipVP6IBY+FdkDUZN+BHKfY0AgM46oX9kzkvoEue2iqOew0gqy+ZC8nYn'
    b'VXJy2XTIhoESZi2Ebs9KjYPcH+lu+5mpYzC6gjiKz8RS8VCqOpNKHVx2FeT9Ev69B9m+1IMJRN0I/+YaO8JRRwmGxNH5'
    b'S7cPsdWGdsHAWR0N/FFShoDFAl2dDMP9NxkfsyNixVfwSbPL7I18lVPXwa7jpebh3vIALqBl0egR3gja3wG2Fou1vxaG'
    b'rPUw34C+/U5eiZ7+/bzFK9EQkHRKdwOwQ230OVIr0Z00o0a8VXtIlC/Sj5AWCe05ZDXfpLrbcPoa+FrU4WaYrD3v63OI'
    b'PqrWbL+YcQC6dPPbqWSPPqu+PHC5bpm+BK23KZPdhy6tnWQ+j6bq6PW6/BIxHuMzHAW8VufqbrDv7owfjLttMf4g0l4w'
    b'OsS7YnxT0FuXF+jMCyZ37nML9UKtfINTb7oxaCfPi3Tr8XeB+lNAE+kXEaIVwM8FOeAPOkHMfU43bc/aJ8ZjMv5THtCD'
    b'RSgr9Zqw5ifLdKv65+bplu+fiKO+XI7E+1yFHk0CaWAfIeywjkMk2DU6c3U0mDaf/BA7id7C1VV69U8+pv+tXqc30xLy'
    b'JG7BVdNK67wJo8ZQdTNM87o/BNTztUX6Wr6UXBOsjoYZpoSXwgxQxhIHyZXTkBV6S24X3zxa+z3G6agOjtiLoTl6cXEs'
    b'uNRqg/7USObvoUl/hzk0SbwUWltIVqxFhz/ENm+gWYvChmfxTZBlBiL8KX2U0Dz6CtihUIHexRhE5mYMrQUIVp4PSZrA'
    b'0SCVGYh8B/AuYMfaRx/hTuXtz2GngpvmaGXXY7qK2YZOa6sopsqqUEwVLu5FFyjZtPf1oCnsHpI7SqkGygZ6D9hOM0er'
    b'qPwP6R1+Xl1Gi0NZOXBxKBW9i0PDPddlZ3GPcQDWB4fSt9Y+FCYtQA/xLWgyu/sxPdDH3AYcMDI4ZZ0+5UTnyib9o3Jr'
    b'b8STER8i/CmCoySeC/8yKzEyihQhZlSO1HGjbD5P5mrvH0ZN4DXQx9PZkcRmF3uPYrW0sgatejuaAbfCKD8JjvwjNALu'
    b'U9H260yxClulFd700CBdM1N2VNERmYz+MGZY4zDG8vqEW9/F/GX6UNC9InOQThlR9jZ0kEHH9TJ9bJkY0iTKgUO3a1ho'
    b'M4Gj0At7kWFR+Ay1qopYG8irQ7At95aFJqzjNnWv96QejGtNlV7K7VOrE2KqMVoJigcfeu/qXRQ9s86Lrca8GCJF23oI'
    b'WemfPwsEplOzWtsrQe+9Fa/RSrV3YkSPoGOVZOb3xNtSLZLFaehip3Uy/OXwIy6sfP94qNxV7s8Kx3hyz+Nx0/rN8NT+'
    b'Ci3ZPPgqz0487uIqvqqeLZjjM74ec9Z+Y0zWnnwFvZcILkZVTNgBf2cpZ0fL76sefF+YABWrG7IupdB6WEpdOwz3kV7Y'
    b'HWXO/hc3+rpmJIUXP0VS2O9Sn96H7X1c7ndi/6hVvcaNWKPmfIXCvukW4mol+kSrb8dOX4THOtQlHj4BdE9BHqSsMJo6'
    b'hkwgz2YzfDy8LKYjE9TbmfAuyHeja5mcqqywGOTJ6doLtWjVv/UnVHuPwuDwzRVy5PPswXrX12DHQi7LKnYsnVOTgabM'
    b'FKXeqpwbLZcP1Fhg96odO4gz6gUVXIv5P+QLS7tYU2f71Qb0wr+MeUE1Ba6ObqTfnuu3be11p91IVVRgFR+9rDsgq2Wt'
    b'HbEG3Y0WfZataNB+P+QzZX0SVCxhjim6k6cNo565rbh9fRrfmdpCcdCicQO8voniI/oIfhhrKX4ZR4tgTgDmNQneGkP5'
    b'GUQl87QCSL5YOR0OUnWu8eZrys8eRr3rJq7X4Yhm0BS1wKBBys5kSxAvgVbWo43FMWAQA8vO4EF068sYW0BdfxIbfQv7'
    b'KL7x2FiKj1I7mllWJSk9Q+Sv2ceJNbWnw2wNksjv4ONXsz0UoTCp45/qlCtHsvUajcSPKLSfZJ5OfPEgcFH4Quy7M/AP'
    b'H42Khk8962XTdVb9iGBME1LjFS24TQT8DUK3mcN43HVWm70VfQU7MQJDveppAeBC3BQ41dVZZ5BujqROCKNoITcht/O0'
    b'nQ1mAOSexrAxq4fp2bgeBGK/uvohPAFa33R6QLyIdiTsxytP0SRAEgYlZHVqSiBY+wHkis16MbFiN3wFZLOqzDOG4ZaJ'
    b'wLoL/qDEmsTmjkmqx/8zNAA8iHkwMSNQ7QzA7o8+MGiEdJRTDxdwo3OBrcKN2WXTZNsPxd/QBtT2gwzrRpq5gOtpC+Dj'
    b'yskLlJ4sOan8DI5QGrtA2UNpgnyLjpr+wS0iKLgFDHHlDxRIVIp06hEqOqJhKXDCb3l9khkGIJv7O6cG1ctNH2C65xht'
    b'X5EFWkE3g30jpyhZwCNr/h7kkRkvB3kk7BvxaDXEJiPLH4klf+0rkBQV58u3GVipGQtb58zV6TJzNjXa61t+PTVqpUYt'
    b'0J5+WKS5/x7yG+/YSodGOGV/whgKD1IL4uoKEkRxOpdfSx9jqBNLdYcc/mfMoc3HVUZxLvUtpL55vMtzHLW7TpM22xNu'
    b'C5dvT6zhGUaWHI0O5cNYcgw5lMcGHcrjmWskOZQn8AjKNo25nJRjZsfleNAKfS4149G9yXpPooHGGTzIJ+w27EpqqRMo'
    b'SJvdrC2DadC9fBp2FV29uxc+tx+85qo+eEo/eOx9r/TCb+2F62FlNV8jovIdH6IynvIg5hu1yL/puGzV/vIScotOxCVq'
    b'3CH0VYzDfZzcyafG21Isoge3dba3fXS2iz676lQnbDL41Jk3w0t9bUcYy9bYx7ZnDkNaSfqEDLfbvfyexbWWRUYKR+rE'
    b'jUVrMBop3qSMxukoa9T3IFEL0WBxER6TQ55YyIOp7k62rZFO3jW26rAeKRB6BN3eQcHxqpr0RFslJoqrWDBWrZ87NJvD'
    b'LxWBOsvdfi5wY9XiZgtelCSZ6QS3jZfpXlq+vl40S/HqXSfVMUDdh7mjbdzQkhg+FLgss1hZltXIl5mZZAVV7nDHYUMT'
    b'z27jcgt3+wJ2P4thRUbmNHeswWum7E6mNOm6GnoctpJm4EBfNdO2Ga8I/e6+8bodKFv7/Xs4Rb87Cuy/5EWcFc3ydzrL'
    b'HBZaQpfYaMDykDnsBzy/SPo+EJiTWNN3igJy6vt3sZaWI3huCwrv8BfxYIkipBStEOg436e9/jeqd2Kva7g0RvneGax8'
    b'OL+DnbbdbZSu5lHMRzEfv6ODX5DbJna54jUqtcaOT9nDxoFOaS5NoqZzjvTyCAvxCNqendT+8kKQQfzhbzqDOEl1ShNC'
    b'oxvDnSiCbymvw5hmtqPy9coOiS7ZLvaWScFIZVBX2QM6WukvmRNsm/s3UhAxOAKiVfviAAjhox+FhDDotTzbwh1WmnQz'
    b'L40md7cYIHvPcdDqTRib1WprkGPLx81McSXKMeUrjDPumWaXRf0G64UvAoH2OsxWYmGyz/aB6LHh5kXPPRJyuzImOWW8'
    b'H9MdBGds0a+81powXIKZVeFtYXuZKVRG9JymQArG8jOp9knT5GvwZq2ZzzFrMVtQ9cLIaW2a91toNg0LnUm/585kuQAy'
    b'KGd/J0UqZ+8RPbeZ6IIN6KTtRb21WNMAJT0KMyQZMQU9FPianuAxUw8IYgy3NhMNIUDaGamiRYLL5HxpGSg07UfRuRZR'
    b'4JopeuoxWW61dYueHXpOfYxLX9RVCLFiC4L73x6H9cbgFD0cE9PosmkN7DXaQfMTbA2i59mwPmRsIL9+I8aRGA6vE2be'
    b'6bCLnmvwzGGqxWZvFT1SWGiw3S/od7BiRaKx1zFxkOi5HttJhnXdqrSehc2LAVa1PAObXYkd1aNp1olvJlsW1+QamvSw'
    b'm/A5lFXqh1ShcJzS+WE3U+JZpzYS2GP7OQrh4wuTZawn8TjUxGhXjM1cis08g0cG0HqYHqZTlwkgc6bignBozE3Bse24'
    b'D187M8ge8LSq7LC+Y6cd39pMfe9Cpyqi505QvdsVOvVuVTSQ3obls5W39UWTgC0/+QIa6CT1hg7llXSSWzk+eDRHkxs8'
    b'zks8oLytHwStxRqkW7GC2YhTilnRcQ103iR6PgrTkQMrdoOOFkRSh/y/Dmhl/wFlhgbEcYcXtbYsOCbcDGvt/4OSt1WP'
    b'YppYgwgTN8IMPBbEpVfDDajPIHqm6ROvvQklQNGuxff/6oC5GI2EkjJj0lR5Ti9JHn8+RJKXhvXSRpjoiQjDfX1ZMLII'
    b'UEkw2hCjQJg82cIoEKZY8Wmw/fdBsWg/iOIt2QxEvOrPhj5iVQ20OFyT78kUPWWGIF0yR4uW9XyINDcY+pMmDlJ52Az1'
    b'P0z3gC02h0/0FOuXyTavFI2EP5+EqZGoPYNi3ZmxJqKu7FbbZ2IFhp3k13bXJ3mwq89gPI4UWOZ4rX6qPkm64lTKSrN0'
    b'Cd+gRxpso/tF7eGtGNfvS3MJJsf2T0YvDu1ePXlwyXULulNW1hikK9jHvVlq24con0cotYO1sXo2UVTwCORUSmmYWPEa'
    b'ZuqtDS9CtShEG0ZV3y1SFF0rCJCP2bFaLaoW6mk1lH+Ovh3BKLe+g5rSao2sZT6lVcQ/tVqY0hobWVv+Be4eJdOuGspW'
    b'2zGoVjOVf4nbhxswJbIh0hfZxFJWwppNie24haL2tLHSaF7axoDJyzGmyY2vGlk2al69mx5bQ3v1WdrZkUY9cIcIOnC6'
    b'Wnn1B68aSUR2an96NhC4lza5dJtc3xfZZB5RbL8rXIYBDgV2lB1jCgVr0c1Z6C5Wv7PVDQd04xjddoYp0QShe3G669bv'
    b'wvX7Yv3OWb+XDl5q0z03U7ZQ410v6fcYnYmntJu+JrHqY9UUydiDUvZAX5wQlhbN0qwszcLSjCzNzNLiWdoYlhbL0oax'
    b'tBiWtpKlPcA4xTPmdM2vWwBwMiThFM+YUzxjTvGMOcUz5n0BMfoij7DtxCe20353O0Uz3k7zvJ1uZ7YT/91ObHU7sdXt'
    b'FM14O0Uz3k4nb0roIOe8u7Or6lCxMMNuYLrqMKM0n27mBv6gkbnxfx/QpjyD3OQw22/qvGsLxoGVfTSNdh/MpHczWc4A'
    b'N4FFg2uOorFVLOxboRGiZ7Z+jsU6MUyhlvZXKLKXYrxIf+CP45iBjY4cd49FvonV3vw4RV05PX/5lRh6eJuuldhbKQ5x'
    b'8KNN2avz5wdvXp1AcqahLy6JEjw1PaCHXtFPRLvEuZzuHPBMOvsgnYZmvSvGHwweVsO/LAwfoNTo53l0NKYEz3drg2dq'
    b'ii+Y6O9w88fH6Ow/bXiDvrGZ3NcWnsvO5VJfkEhe0xsYhYKjOLB63nIe0K86WtQnnzyF4STwUM1Vvh9P9kBTxrgYh1V7'
    b'q35E55zOHb7AEVifeqw7jC3eOav/lKbW4JT+vhF1RToVkWNwN4mamUZhw0CmgLrWpi18ivh+PXL+Ti25A2e6DQ0FcLnD'
    b'7gojBO95QcADD7fGSi3MDZst64DIXC17sa0D7/3btn56sn9bR7+9QFvOv/5CW/8X84eLbA=='
))
