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
OUTPUT_SHA256 = '0eeaadc65daa4eac62d497c7506fb102bf405784e04285b56e5be31a154c8628'
VA = 0x4B1EA00
RAW = 0x466E600
SECTION_USED = 0x2A00
SECTION_LIMIT = 0x9000
MAPS_OFFSET = 0x3000
BOSS_EXIT_OFFSET = 0x3900
CALLS = (
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
    if (len(code) <= BOSS_EXIT_OFFSET or SECTION_USED+len(code) > SECTION_LIMIT
            or any(code[MAPS_OFFSET:BOSS_EXIT_OFFSET])
            or set(symbols) != {name for _, _, name in (*CALLS, *SLOTS)}
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
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNrtfH9clFXW+DMwI4NOPkPRRkVFG7uLZgnpvsmmCY6DlE6OjgOoYKJggggszKOWUoMPA96uj7Hhj/bN9nXXdrf3'
    b'rS3L1kwr+eEyWLaNaEpJRS3Zg1A7hglqOd9zzjPDD9f90ffv5vOZmec5995zzz333HPO/XGuKsAn6w7D6Zf1TXMC'
    b'kikgGVU1EAjY1dk3C4J80DS/q2DP+YtO9rmDNRlqV9yqC/g89S6TUpfy4B8F1rAwJ/BZUv3CnCanmFlf2WhefauO'
    b'fZ7pUCqMrEmxqnKPWYyyqoG77YFW0WxVWZsYFa7k9y98EEpARvZehgMQFwrL3UBHYXihUKhbDk/uuYHalDPF4XJ/'
    b'QNIn1Xcnseakemb1K1Z/oWAHIjMzAnWbIQPzwkNcb3G4J+A6wa3GZKtf3Fwv7qlX8s8uXBSsJUN1/1gQKpvPjxME'
    b'R2XPdqiAeZZDKw2JLbfp2JbF8Mg82fj75yL8/avnkFi1HrJhpW8wK/weZNYWZj3MrD5mPcasbczazqwdzNrJoFXW'
    b'HqCNe7Cs8tTmC/Drbr+EOPMQsnaEuCecb6FnW62i/4seMNs5UaCew3zNWZ5zLnNSfWVFrWPazCRoAWvuNliSL0p/'
    b'0bAeuB+KKO5vv0WkWQDQwFxB0vkWxCTu2R4xF6u2TAjWa3UH/owP6mKoQ65wC/NFz38DHvniyL33wb/XUwqpgh6f'
    b'1sFTJMBEqzeplW1ZA6/yxVGSYa+DmOWCdyDxVj7vo83KtIDcX7H6Wj79o6fkfnG1iW1xEQOx0IqTMQqhhfYhnbom'
    b'bjGL5nC5J4Y7otn0eDObFx8tRunjg7QT2zU6A74ZcsApeuKQ+CHdpzbHQe9577ldEDIcco+JP+UP/FLvOSl6OIrK'
    b'Zzqtrwr1rHm5kCgIy5/TwZsQqI3/GgSkCcR1mVLzxDdAU2DrRn9xON9Uj5LTqNT8NwLnMqfKbD2BI9ypclsPs0xS'
    b'ap6nhMARZdv2S9hrvcwymfkLdeqM7wKBBYBxtLjH2js5b/UIJtcih36DJAHRrI3IDuz4FioC4jPYcZDvOYHas+eK'
    b'w4GzzrOeepLoW3nGZE+r6xvFdnYODA5AeTpQlwiCnGw9G5Lg/oWLcGCZp00CRLxsloMvSGel0VyKZVlmnhMjRgkw'
    b'qIxsQfpCVjZrkfx4epgg6HLEqGmTmpxJh2DMPh47suqkKyL1F9nGgOvrpPqqkwtzALs29k5kqMtvwXHx6RgYF6xJ'
    b'7omV10SLq8J4jPsewWv1AxsFKQIesHOU6q3nAoGk1qRDXmsPAmAEKNXPAIw5O5gNxkEbd7azNnU9ykq9WOVBkbWM'
    b'ULbaMY9VVQsowWWgkcytqrI1B1LUuRoY6wHyBUpYjgnJgwnhoYRfYkLcYAIOpcfDq+pd8uQcV7ilu7cKakZxtLjv'
    b'iXNd6MI6ubMNyOq+RIWuUSwRyta1iOYDHHrWzu4DytbfI4ltqhc7e2v1Ocr5IAD2IGDmCHi7WivEWtTfUbGO7g2s'
    b'RdmqIMymqgoAuU3tA73QI76lU+w+94VIsSYXx1FFz2TxiUwcHM6ekanK9DBQIT3dHwD6TKJJ2spBs219AVFFcNAo'
    b'M/QcNMoMIzBqhonNMHNQNDOiIf/PtDbcK74FfMUqIlbf5V4b8Qtp3OMzjcocAtzqXhuZI90oq4ly00goD3kvwQug'
    b'AY5IwKJ+Zeturblt3xG60YAizmWQmyK6w+Umo3wwNijFqNNRfbK/OthRkA7oYuy7HuC7iLoiytouX8iTrgKBQFFp'
    b'trYndUckHZIvRK66BVpwYCRq0FYlXcfa+vqSWvv6ACg3Jm6YZ2inNNaW1No8/aqPftLXp6SGAWKlWujaK1CWq9oV'
    b'Zzu3tc0N+IbqAuZVKn1/LBAyFKndwd5hZ4AsyMLOK7K7812hsikOEwFwBogNHOXWtmGlT2SwZodaHAtS39T9U1CF'
    b'PUZm86vn+oEfZ9gF5SlBSBEMYz55WQ/UQK9wqcfBTgaOAwZC4k0ZhwYz6RAM5fREtqEDW3GkJj2lxj5dMzWOOSDc'
    b'nlYpIulc1906lFo/O2o4eIs7PDlM6uaP6MFQdQLQkFX6WHhyhNQmPxYruFoBsg95qI66iPYAeuUAF0A0jHm/jcc6'
    b'RuTxq/AhuUH6Q/KjRikyeZFerDpOw8L/eh5m+Vptm4ykMT/bNBlwqbNB5JLqvdWvCtqHpUWzNDNLM7G0BJY2kaXF'
    b'sbRYlhbD0taxtDWsuh7zVJt0+GukXzP9CvT7LaUepF+yoNVP0e9mMpPe6l2hWqrHUf54+o2j31j6nUi/ifRrp990'
    b'+t0Av97q7bpg+UeNbJF+AfMTw9W3boC+apz6E0EA9vaK5hwj/DhN8GMziwlNYsJnveJ8/Db1ilkHxYRP4fFTfOwV'
    b'59SL49fvAiasGt0rLmqDrw++Ld2jxcyTUP5xtDxiwtFeMSomB37sevhJN2omx4HVeluA5SAx4MiEBkAnnzaPz81i'
    b'+niwY3ruyIY/I4xZ1YEaseV2SzySp4qv+RpUMzwunAc/K3t6xbH18PDHyagPDgL1Y9/mjkQgFcjMaoH/J5BOyFHz'
    b'Kv3nNMDzE3Z6nn8EvkB91sJ0eOiCr83fK2Z/CDkcWfAGjZpvyYZ0xyz4sdgh7X142LaXSmc3gDBChVELF8OPI697'
    b'BWsZ4wvRNxbqHnsaEBxG6rKpRNa7SM8bGh3ASvNMKJn9AQDbIfEDBOQB4EgQ5/xjAD2C0OVYG1USbBg2YkUR/Cx1'
    b'eQ5Bws2Yq3QvdjS8WSB/Vjd8O8TpQEZUGvA/6214yDCCCM8E7kQVI+HLgM6oB6KHOXRkVeWeBL6+CkXGeoyv30AP'
    b'bXvn4aDbuhFHhinMc4jZDruSlIr2wKsGNyrzOvfCSsjnywy0ctthpa70Jp8Az0odKg52PtCaxa2+BVMQixSxNwv+'
    b'ul8e5owkx6BkvPhjzRmJZbY2vmHHV+CPtLqcnkOuOayJHVfcxps9OOAt8iPxgmvKCmdbjCInHnlaUDZftx2UDRA0'
    b'NzPgA9MguK4F+6TIu67/WMfOVDbiYMMkTZMBQUdDtWcq6+KNcqc/sPpbzal9/jo03vE/RuMN0lnErK8y2y61uo+0'
    b'+gR57STBdQe3vmpYbHheILf9Fl6Lw5Q5n5MvhElR7r1/xBHnMsprzYL0SPfNMI4tMQHQ7cyyZsCZUR7qf7ArHNQS'
    b't8QBjiju3DWmsfLCp2B6xeqJBlCiFbsE6lTRat2F7kEbjWNLnNfajk9eK6pJ8Mg4uQjgwYWGD9i16fHG7qXcMkne'
    b'j0oCeuZZ17LC1QHfMk/9wznyftQfGnJpThDx64IQJgxH7ZUxn+5fVQJ8YB+q68fhpMbcVaEn356KQYoa24s2/QVQ'
    b'uCPRwu3oukia9YV7ZFSKrhjWCLleJwX9PGSFFEYpyF2/Lsjdv/D0UpCAt7guYI9j1u0AegUyLPNaD0NtAYP9pieh'
    b'fU9hBbXinqq7N4Ig2BXrYTu3befWpwI+N3SYiRrLzh/tiLTWgj+sl44lBVjFjqTAQAKzbr7Nss7l3BeBznxglGuU'
    b'uGfz3dvB95cbJ8zlthcUZMGrasHXgUBlRRs6+POln3FrO3fWJjs7pFggLOYfKh7BnZsjnTugSpO0ma+N4dYdUEvf'
    b'CWaJudXb58XKXTfwmbG8rlNruHxGx2bGyp/q1E6YlvR50Xlz/YRbYmV/GLPEPheGDtpz5LlpRWzPIatfo8zot7l+'
    b'DJm1NNlvhiJyh1nu10Gm6w/JDQZVoZxGzKmXZ66J6/Oa8fm2gVIT3VDoJzhzHCwWoWZAMXHfF6PKsVBKnzcOC0VU'
    b'zlyDAgIJI8vjBzC4zxIKwT0URbR6zbco7bENX4RXfiHAPDhy7RrmrfxChmzQrJsL47j1WU1Ew+X39IVRQ1+N5yyx'
    b'ZrFqBVQmr00QxKpckrFdYBqm5CAFNWj7uGUcCFsKd8zjM7NAWBXrXhYdz2YN2BQ17DzK2V7ufAMMBnOUgsdhKeIw'
    b'FXUkMovLa0ErCzJcY0kBrfqHFs18/KaebFqTptnnHxbHv7NaBPjWwwRv7BUfbMAy0yGDT0x4B9W3OP4o5fnNYcLx'
    b'4IcENztKNSVv3nSQynaJ449Qvk0+7R2ynkaF7sLMRdSGw8FhyW3t8ntgnj8IWq4PNTMVlQNyCe/ZqOVtHaEGw/QB'
    b'GxzVj2jB+EStKCUbOR+ezdXZQTOGFv8wGnVIwLZZwe5EzSxCQ+Lqvo746YoAp3Y++Lbgt4oJqpjQMmrtmj5vKUxy'
    b'JLMnIF0jr50IHYIC0x2x9278M+z9P6TXEQfqO5Lbdo05ktxQtmSYRkNageiole2g0lg/PFk7QFJd4dySYAdtw84H'
    b'9Q0oG/YhaJ1BjaNenUD6Bp3syi/+pwP9MbZ2oqZdQ7pVMyufRaFZyYxFh+OvZNzMMC9Vaq4+h/PWOpy34qRKxknV'
    b'tu8AOBegNI+19gD64tASSlhoCSUlI1CXQiskcn/A9cCw5RNc5skM1B3z4zQYtQG3mpJak61+aTSA3X6cqkJtWxqZ'
    b'szNTW1/hzk5wJEYB0uWU3H0tpIE/XxgZkFRwuDG93tWBVsOnGcvXY1BfgaFSKjarATPaqjU3gq2SD7oZrTck1fc+'
    b'r60deK207qH8ZjstYIzS1hLEPfdwxaWtZCiZoNm2YCn1yS9RrblRrWVJo7m1ik3/bjO3boC/pzgVtCQ3SY2KtbYw'
    b'T9kUe4nWIrCkshUXZWiqNsSiZ3DJ6FAXIn1NbeD1KWnvyv2jVl2tfgqzXCYZFxo6VtyqW5QT6FhRH6NUheMaDM3L'
    b'Q0XjqOgzQ4v+IVRUKAoVTYSib1wYKEqTKvVzEYsugaJqeQ9NQZoMu8bepoP5S5u6ASDJfuk25mWN4NW3qUnQcHYB'
    b'Hn57SXvAJnRd/wtBGHSMlAqjg11SNxLiyBtwhhPHI/hj4KeA3WhXTVgNTIqewpUvMAmyOgFIXm2Q+/VSWqZoTtPL'
    b'PTG0PJMWzTNiQgs23NYRkNoDR5kXxMLzB9S4Fe13iTU4EYDxC6NA/VWyIHSh21U5FbtGeozP0KfjQtEq+TGjuLpU'
    b'Ad/Z1oFzcesxwKW+0x0I/CcVZsHAnRSs4zmasEpjAtKxf1vyANojFMd+5E5jnECrMcD00tHImy9BOtVLp4NMr9V9'
    b'BEynGRjwt+ZL8qDuY43a7AscuSnyW3YygUb2Cjay25xM/xJO0tfjk7zPTc7UNfK+jfQwEjsop2vWN4PygtWfuQqr'
    b'L8bqHwtVn7Ln4yHVG7XqI+WvjGwXWqlgcYdDfZUKJ0FhZutXp57GBRnWpB7vwdWHflremNOfXNupUdbK6GkBuJ5v'
    b'faxbCHIYnKLyHCOvMCnyu8df1mc6Ar5FwGSxCl1pnmkGCu46E5yEuuAl8UuqBeaxNPvjNTi5ZBkmSJ6m1LX9fX14'
    b'shl6yA76AZyIuSZI4u42baXiVp4+CVyiG3jqJCKubvqTj2H2SPY1qup4NBV+7KSvg9J83TfDBydy7IAJGz0ZvF3V'
    b'1gXjWDIj00CLGWqXVulAlcFwQXTr/FTnz1gzvDyCgt7AiWRkJnt7TKN8IW+VVvOus1gNrq81mgSNKWoWVfP+j7Qx'
    b'YwUOmZlN5VIMzvqsJloQiGZWEESYB7ZzaxyueFnjQZzVPWqQReqsbqLhIdYUFJ1waSG3qvdY9JKDW4zc1slTEpJ1'
    b'UhqoTi4kWxLIZv3+72jre3imnoPfnMAjmGMWs6TzGUZmsfMRPEPPLPOYI4unASAbZvzq01APs8Rz5zFua2NSDJc6'
    b'cKUBSHbG84oeJkWDUTKUviHruNTJbHGswsRtKugk5oyVD8Yhf0PSoD44Chvefi0th0BLQasxmx+bb+1RT34RlLGy'
    b'09Sy+Dx+LTXMLF3Pnf577Hopio9OTk8Az7mV2RPUD74CyirMQIQi+3KqdFAn9DHUv2ChVuvl8xh2yaG2j0QS5hMJ'
    b'MJU1i8z2LNrd7epCrf4Gg7txPXjPO1gLsz2lft6lrTF+hyOmCdwv4GqIsGtEzynqdLHqY3y3QMufEj2tZOfHeVrF'
    b'Ki8+7kXfH8X+dXyLUH41+U/l4ck+12jlV6Ufgkz7RM//IPYWRXpOHYcj9UPWwPzqR9BXotn6HCkfuYV8YfTSvDJO'
    b'RWgIyzhPUaw+VofeELtASoojkD9NkFmgqrLjo9W40yjO4IPv4DbwIAGtL6TTQM6cnaTT/KjV2AW1twvXDhKHeA6N'
    b'egFXG7YDp34ECADNkMRg98IA4hVGdW0kMrj/GhhDkaewUhOOoUyDEC3hjk5AGitXmARXPKeVGZ4yiadnw8CNVuTF'
    b'XwEzdNLIQh1MSe0+YWBC2mgULutK9SasxfvkNTg3ruxBNchsbnZR3fq5Nj6qDKUp7wuadQfDrd6skj4H4t2a5R5G'
    b'v7w3ntjZJEXKeyeR9h03MKp+jOW3qMFuaca3krHYF1XQsA/VP9+O7rdRcP1UK3FPuBQr7+9HxQzY9kcTtpGY+0N1'
    b'8W2CEMIr0uYFt7pBbETaRuG7UFDAACvraBNErHojDNVWFR9xJZl5Joyce9xEWactU7vk/TRJdd0imjV5IfngmnCA'
    b'r+jsBJfS1x2NbWj6QmsRSI9a/iVJGooETA9IKsAM2jpBHsygxWbFRycdUj/6AqUihvEOIbSPg22T9+O2BDi7Yhgx'
    b'RS38giYID6TDT8Z0cFfBp86YBD8PTGboW6fNUqwvwGtairbbAzLl9HEreu22Y6Bm0O1tC02pwYjjshdlVOf3EOb8'
    b'F3B1bpdXDi03wqv0Kq/D9UFw571yaIEweTcuHIqe6Tj+YDSSOWkZ41PsAfnCErEGJza0sndYW9XDtSlcI5tzSBz/'
    b'RCf6hdWROkL//HNaNfKziDDhW6DxTxrt5i3a//x+zPYCZZt/FPCcEBMuweN39Dh+m0ro9lH6WMy7rUcrB/Vmf9Ur'
    b'LjoBsDeDuAB/thfeD3TQ+6IGMeE9AMOkan4NbpGI4700QTpwVptI9UM6p+0hyPE+JLyEBQeW3nAiUzOZso69II7H'
    b'NTsvgvq12nBJjO+ihr1LaF8Koj1Nk6T5uNJGvAZhZcRSxjeT9tmura/tfFb7f1rjUtRuZEN3WNI5FIeFn5OcnbEz'
    b'L74uuAPcBM8513Xy/nQaGqMzQyMpHEZV1sAADfjkN1HGYGCBaO4l0dzAvibhtLXwnMMDum365ySYwyY6MCo+B8uq'
    b'/rSTRsa13A762DWaO01+nT2FO83wNz20Z3tYj1MGq4hThp6JzFbPelXbp6AjnAdR5RvVuz9Hr+egsvXmPmgMtKNe'
    b'vU5FUAs8KlvvBKg6ggBtBJiKgG++QMBhAsxGQCcBfARYgoCjBDhGgHIAKOCsHgAYmDNckTuTCZMdW70iC+4u3ZD9'
    b'hkG9qNc8CnUUNaBq9NAGbOwY0oBVncEGVA00IGMIeXVIjXUIeb9HwMQh5P05SF7cFcgz/xvylDcngiVzqLnhaBk+'
    b'Rsey5zBMZZiSCHD1b5/g9jI+goPFmg0pv1PBUCh1f8Eqq6mq83OhKg9m4en6TKUaN2BwK4YRTKmmBUxPAqLw8S0I'
    b'w+H+FYmssXKNWTDjagjaaOXNWVTCv+s1gR3BhdE3F6OZZW+Og79VJu7BdOYZR36SkXYpDeiiCVpdGhVK3WfIoLC/'
    b'kVFZy7dkIxB3qx9dvTKpXnlzGbynPL4xZi0MsQ2mCLnfJPkBrpQjimcQ+eOx4RtidSnJjdLfwWoq00z8GcI/2zzm'
    b'qLIFy+sa2HvJXtfIlMdnhfduMIV1f61seQjgmdxmhAY8UCgQV7DVijJLa7vVOJz36jc6tJOFpuCGNrP1gJH85cfY'
    b'sXiSwFA78a+C5tfi5F6sekbQHOVtX0MDk86pX4Pg0Ma2maB2aI/6McKaB/aRmjrIWG1poHl81RFXtuekKyvoI7Ha'
    b'HYixlraurSoUEl97u+E0aqXGkFZSas2L9qJaPMQ+0Nbyo+RsQB1oBX8Wqg2W9qvH/0bO62VNvJqaWDNKa6KZfEoV'
    b'Wsk/oo1SaJgh7qPXUDbRpL6N5KRNgvlimFjzJrW2B/d7taaFd2ruA643qKc/wS3VTnALxKq6oJ+H5wCCFlzGlkkV'
    b'UFjMPARa0400i+MbygpxR+VbUtMtZTnQoprpuBnz6KTwXnFc/QZrr5SsWHsLdcoT2bRkAC2izX81BxzaBfNBoiI1'
    b'ZQ5ZsRQeAJCC2//iHmcvUdyptnw24MUAK7g+volHwzcWvhPhOzm+SSrPL5tw1515RUXCjHxXavnDxUtn5j/scOW6'
    b'8hGQWVCcV7J63vKy/Nw8e1nJ0vzy8vvyhOm5rtxsR0nREgCvyC+bV7Ay3+EqKc3OL3bll925OnfVP82xMr/soXzK'
    b'sbqgeOVKqhfSi/OLgkTMKsnNm1WwpCy37OFUwV6U+7CjRCrOSxXKB3HlrywtKcstmlFU4Fq6XMgvKlhZUJzrKigp'
    b'zhHKpdKy/JW5Sx/OER4qy11V4IKHJQUPPbikpLw8R5CKMa8rPy9HWJi7cmVJnJCXvyxXKnLB+8qSvPw4YWFRwar8'
    b'cvh3AcHwt6TEhW/FJa58KA/twYakO6eHHjNyi6T80IulqGTpCiE9tzhPSC+RygTnfbNLc5cCDZYSoLgYmCPYSlbl'
    b'pxUU5VvXDG2RJRfYkjutCMqUlZSsvK+4wFWQW2QDioa+z0LShgJSoQnDChQUS67hWaYB/UmXA+4iQDr0aH7ZQCXB'
    b'1yEoNMgghsH3uwRHvstVUPxQ+fixY8fPKlmaWzQXMswuxU4oFwa7HTOPFxxLy0qKijIK8lePL6dHS0mxK7egGBoO'
    b'DwAYP1jCVoI4kBXjp+UuXfFQGfb+eGtegaukbEg2zOAoKgB6hOFAe37ZUmT0lT8LsR13LCspW5mD6/CCMWJF4dB0'
    b'asp4x5JpJSXlrkRBGI2j6XJ40j/C04oeTsTypn+EJ10JDh2e+E/gV8yPgwILXHUFeNIV4I7S3NXFV4Cj+OVdAT6b'
    b'cgvCqMvg836efEX4zIykK8Ltj9yXcUU+FJTlX4l+hF+JfktRfu6V6E+TQENcAT+IZhE27PJ+mVGSd0U+A3wYn02j'
    b'RkaMMOCEOSzcMCLCGDly1BXlZ5gcDKHvSp/hclDw0HLXIH3/Lr+toLwctATB/pP883JBq34P/PdLK0uHtv/f5Z+b'
    b'XwSS9p/jn2fXYCP/w/wP5K9xfR96UG6+T36Up7Lv0V8oT98HP8jTsPHwz/KDrgqJDn2Mg/CQiNAnchAeEoVh/ESD'
    b'E+zyy/OHupY+EYPwUBdenn9eflE+mFPXMPoBjl0yL7d4xeVwrd5gB1w1CA+x+PJ2hVh5OT0hltFnRBB+553ZeSVL'
    b'pZWgwcuzB+3jHSvyH14Cjkj5na4hjbvC55+VX0lW5Y4lYCD+JYr/pDwQ8r3LD9B/WeHbLyufmFgety4uMTExzhoC'
    b'uYfncM8YeeK2yfAwVRDWWwRhxwH4Tn338AsHJkyoA5h76ty9N98rCBstgvt30989PGvKhAnlAG+1LFt26t53D5um'
    b'2pXrpixb9tOpPXP2Tmn6ZvnUvTcbpj7R+M29J8VDKc0fL5365MgRU+ufrIQycSnZT0y7t+Rq+ReQPiX7iUYof3jy'
    b'hAkTpt7iWTF1ovvue+D5ADwfGHli5xSo696eOZMA7yR4PjzlxM5SwFGbKggpCHuz5OrwXwhCyzThh88Pnx8+P3x+'
    b'+Pzw+eHzw+eHzw+fHz70Ce6R8wqjQ808HQhUNh07GwionzcHAnxTHu5MNnkOuSZyXYCWzFmDp971Uy4odRTR4ZNu'
    b'CNj1eLLFvfcDSL5RGqnIu1Z+omNnAj7mZx+qZ8OhUIVRkWsPbgbokHMX7K8OB7/fyJqarb0BnproqZcikuq73sLF'
    b'b4GdUOrsnz6DNXQrdbu2ucOS7SlSh+LslRvWKbLQ7g6bW6grvCXgc1f0AlXSX4CIBqRr7/jfYKk/VzbZT60PS87S'
    b'S/9X+NPAUSJ8h1eYl+a4w5uin5U67Y7mFKOj8hIeWZCMvAZ3xLuv6WsWFuMhJ15zFjcLw/kMM0uN5DOiudOUgjFI'
    b'qVfhIZhfRrHUa7nVyObcxFKv51Zzc+pPdCz1VjozOXh8p0FZTzs6dC6tXQuhoFPvfpH1rwqnE8k9SeeU+wPyef3q'
    b'OMVkZrY28S2zcr/PfT5ytbji3MgUxfUjH3e2QZNODTsSH4wPwWNR0LKrWNNcpRKPJ7APAj6e8+2h4CZijQp92iz0'
    b'BgIOLYAp+fFncUOg6jHsW3pm7zFgj7J1PwZF9atb34E/OUVHx4ZGc1ubsrUZo8UefYc2IWTctsYzN3W4T80pY1Kr'
    b'ShGQ+xcH0/qVrUexzP1Y5hCTSZB4Kf0W0e9ynbZzwWivA+rowPw3vEO7lcrWz5EWeTnVgIVV4R061ao4273Wnqtp'
    b'mxcRKdWnISfnmAfDsehgUpvX6scshkTDXwXaj0IJRby/6rx5DR6/GsHk0gHckAAN2H0pWHV9NCDvV594GylQaYOF'
    b'DsXLPSgRMZRnF+RRV1KOHi0HhjQaoZ/UEXg2Uf6WIr8gr3bmIowTJnFzY1A4uqYPP4SZyU7QthWFZxmbtcM9u7XD'
    b'PdgPYuhwj0/04Pa2/JgpTKx5Uqs7FCMVzjNNSfWeVheIrTFZPkuH4O6pQwJcExm9c4708hkmr6yH+u4lRvbTQWoY'
    b'5ng2rEWd/l2oBRrVSa3BRrAz0AD18e9QDrjsx7JngkkLxS2N2KyV3+Fhv1CoyOhTIHxe2R8IaNt8Tj/uYmFcmRr/'
    b'F62N7drOHu4H9yjbSpGv1x6icElMJWUABXCTbzH2i4xxWWpviyaKJk1IR3KnX92EocGNuC9n7WF1eA5J2TYZ0TW3'
    b'BM/yhY69aL2JoV3dN2r5lW2xmHM75qSEwQ71qz9HxHIMCQrS75f3m+lo9JZG9dFviReIRNU4oQMWyQdN1MeLv6Vg'
    b'p0F+XOpEfuBuawYpgzjc0ltPQWYVbUDhau7sVP9yMRC4h+pzPeS1qoIW+fk66ikQfXp1YgSiFlIy/Ny1Xzt33an+'
    b'NwZD0Lnr2+A1CUYIMs5Qf99mPFu7n6Lb9iP7uj7Gc/paJ4qeyXRUvxP0dupBrX/G0jDBapPOMakDEI5CIuMuUgwo'
    b't3YknUsmWXGNCoY79vWx49wxCYa9rR1k8ecHSCfOSLQz57FAK3cewziB64dEebR7zkmRt5E8itP7ZsiBEZJPvqRb'
    b'NYUHh5/rAV7Rxm2dyrYd2E3f0PBwXY10PIXnpfHcjH71mG6Dpn71bp2/+yZACgxpZ4QXT6QTlV19tGcrXzKINT3a'
    b'+XcdhdGoWmRNnPCPTLZj516ZzRPOB9ksbe56RggxUid6amn0mIJKbj3h61RPn0dRTJZROl2lQzs3Rfgenftmf6hz'
    b'x8Gr1rVstzDYwSCldRTGWIdJIWkfFkDmUAs+Q2t/qgf3eunAo+p/C1WgH8/GNWUY3J/gaQbtTK6XtallDZA6Gm0N'
    b'aYpGAP36PCmx4Aljo7bxfT3i9W7qCQ56LgW39598a2B7X7imWZdB+/saek0lRDXQiVWrkcdADfd6Trp+IT8aLTL/'
    b'qjA+QtlWi+Pfpqq3NVL338xtqvy6ppd+jfySTNr4bFPV/mEb8sZ/5f/gOvLy3OKH8nHlWdtbHfZZmV8sTS/ILSp5'
    b'CDceh+WeJrlcJcWhte5y3ADTEPxDmWBqqEDqkoKiAtfD9tzi/KIgDXNLivJn5a8a9j5ADa3hl5fbC5auyC/7Huu/'
    b'Jbllef+/69cD5XH9mPo1tQP7dc9prV/HMasPT7Mcxf7zDZyJFT2NOATnBNg7lRcuPYbnW15GoaZj3HjQYSdJvM9A'
    b'8YEjRA+eYJAfjRc0T0CxHhcTakwUEUEh1qBFrC1k2NuC6oXZDvf18YqWVIySBun4kDXJFyJXf6JY25NaU0BwRt33'
    b'+LyrTmwwv6Lr9tPgrsJj2dbDcn1Y83RDe5xiD2dn4OmjdPj5GAa3jzl7wK9Yfi0OIAxDvCzkGsbKlE9wrOzDA6g9'
    b'RjBjvJbuUdiF8SfKfiO4IJmABLVbD+iCHyuyFrWCtxpMVepu0aJVjsNbUiY5I2OVunGDwFhwCMOUulPfBAKrR4Bn'
    b'Df/Lw4UwzXUZFnv55sfYB3cBHRlJ56AXjPL5PBhdovVM8it4QBRIc+WxJsVtH7skDGNS/DDiqtB/OcQexwzdaeJr'
    b'DQ1dZshY2eRbtSSsVxzbMOQcXotdO+oyvyZFOwOpjy/EqySOAiowZengOVFFw4NRm1mLQ96nHVY96vrZ7esxxvl1'
    b'3MqaEg5Pq2euqM96XU+OYt8ZCtYe09D9X3wfmpvb11NYdIMn4Lplyk7MHbM3j3p6TL/4UsPr6KH2nVFMk7oNWpj3'
    b'h5rn24X3GYRCPthxh/rrj7CPfqRiH8XJ+9AvFfhUZj0mVtHM5W2+iU5cHhKrUDvvXUpHC+VYaOf+kWQDenDTKbkx'
    b'FN2f1rjvi3DNrUw+IaadOEDVO8FZ7OuV6xNR/sjb3OD8CK0euEn1APkMIH297ATmAqu3AQ/tHDOEwvqdfwNJwwNS'
    b'1mN8E5JYqDPQoeLLJI4d5U/G0/lQ1/3IXs7HEe0uE/OOea8ygCe4VyWOaWQyeeH7E0jPe1ofFZX1SPTrarjG7UzW'
    b'gBxTW4FbWQN1GDpe/KPAjrDPQbZz25FvH59CvmFYuRMDzFdqAeZcisZzsD0Y/eXHk7BnQRZ8mKkHMwHInII5s2Ix'
    b'+1mQm4+0A6FZTQgdBy8faC9ZKZMB8vR0On/UJFbv1E6u7kwnQIdY/aQGiINvqTkY0v4NlDuFAIDOP6O9ZC1O7BUX'
    b'dYjjd87CyPZFkLzbTkjOrpoD2TCKff5yIHt+ejzkfl+Lqc5Kn4ih7+J4Pg9LJUCpuiwqdWTVjZD3b/B9B7L9TYv0'
    b'jvopfBfpu8NxyhG8hkTTBBNOIrd24dlO0IG2Fv44zW1AGYJcmYDpfD0F9R9Hu/8tnj2uAC/SN8bLq2fhibBGmqM5'
    b'Dxuwg5L7y67TDpxTqEiFcYy3MoBjYFU0xvEeBkt+iG1CfF0vh6F2PMa3YkS2nVdjfPaQGN9qPOVF/oGzBTSaesMl'
    b'chEwCDCzXrxTfUSUrtLm84VC12I6Eu1TnJ3Yry18E07b5hrM/e9qnYuRheacHjHzEJB0+1vpdNh4flNl4Drt2PFK'
    b'PJpLmaxtGIjop7PReA4ZYxVXXyMmYFT9CWB4XZ4WvPj26wkjcVYlJhxBoQzG9L8tJviCMZa8SNM/0OuLdi7XCnXw'
    b'rXat6sPBQ9C8VDsa/DYMizRwFIbE8XcAeGdQiX2tScqindq55eyDYgIm41deo4X4y+s0TIj5abd2ZHrnYu1Y80fi'
    b'+L+tRqneWaXdAYDCcZAYdkzjIUryBk0/2loMO86+h0RijGddrYb+6Y3af91mrZr2UPxnOw6nDlIAPrzfg9DNNSzu'
    b'ew9YzzeVaoP8Wjp3bra1zDUkPh+mgzKmeEgmCUK52oZ6Ck9r12GL+SacJ4tp/eJrJxr+jszw8gofXrEwwJ0PK72l'
    b'x4jKRk2/U3Qsjot38Bw3VPkT6rQorHc+fwqwzkV+b9caCbXjOXArUFikURiDvNyB1xwBf+VnQ7YicCIoZDqS3mE6'
    b'DdS0ev4ErkaUgYdI859oed9GzQHsxEij9XQRxvrQRRhcPIBxLJLhwKvBY45vkOWQK1RwF/BkePJ5ZusQ5d+T59DD'
    b'69w0NuR1w8eGXDUwNlR0ny9dwKWEQzA8OJS+s+GRMNcyDOt9Do9D7t+o3c6wqAUbjIpP3qz1OIm5/JT2Uv3CwDUV'
    b'Y99D+HaCoy1dBN+sarzOwhUhZlaP03gj77jMaqpnjqMtX/Yp2HJ2PKnVwd6hBZoOmIFH7MEjnh3Qyo+CLX8fD3gO'
    b'OlnNmrKsxVppgPseGaH5VvLeWjq/LmGsgxGGOLSxsinxzrcxv1trCh6dzxqhCUCUtRODHzDa2K21LQvvoYiyYdOt'
    b'KhbaQeAoDJ0t1BWGz1Vqa0nlgR07CjMsrzvUYd13KQfuPqddjLShVivlbFPqEmPq8IoJNBttGHKpkSh65l92zxXz'
    b'4r0Wqu99VLE2cDfnULVq1ivBEKy/v0oD1erHaxiC0TEuI5+dkJxucpnsul52XhPDf35nxGpE3uwHf85R2ZONi5Oe'
    b'Z3GCv2UH/KrXQk3JHnyUFiSddHAFHxXPc5jjE74FczZ8obeo4isYmUJwMapqyl74ny9fnCC9q3jweXkiIFa2xl5L'
    b'15xhKWVTO05dr24CK7zgH2KfTx1DUVj4CYpCs0N55iDW90Fljx3po1o1jNsQo3rwZbqCSzv9q1RjIKvyVtycQpyh'
    b'E0k8fAp4j4I0Ql6rN3SPmkLhqEZ4eXRVTHcWOKjz4FmQHsD4ICldXmvSSdMy1DNNeGL7hW/RcT0BjcMnRygay/MG'
    b'4t1Sj4SF4kwVJCyDU5UBX1aa3GSWL02QKod7MjC3VC8dxR5dC86runEXBTDSHNPg77pJh6HTP2JecC5BqWMs4OlL'
    b'QyaVAzGQ+wnFJERx3y4talRxd3TH6bTYRww0NeNhZQ75DOaPg64h9DFdw+PpxNU9pxknl8/gM1Paab0vGqenW3x0'
    b'V10bwY8hllMvYWsRzAnAvAbBW6+rvICsZJ4OAElXy+fDwagu0t9+c+XFY+iP/YxrOGzRDKqiGhhUSNmZZAryJdDB'
    b'+tUXXsLVKrxIkC7VAMi8XRgQrmw5i5W+iTSKezZOokstGiYw0/oUuX+UdIp9kFTfcD4sucUl8nv55Br2Bt0Wl9L9'
    b'pTL9hnFsi0ot6UEWWs8yjx8fPAgsDF+OtNsDf26jVlHzibIBNd1oDkWCPtCKzH6nnaY7OODM4AjAPIPTRTowIptf'
    b'DI7InJeDIxLmGbgmExqUkZWPIV1TV12PDZftf7pLxyqMWNi8cJHGhawFVOlA+OkHR7DSDKrUhF4TLRyoC18MhZae'
    b'2UULCCj5v8Iw64epBrGmitRevKZTNtHLRCKiWDvaz3+NOdSCl1EE8IY7bcqhTTbu95xEV8JvUBfAhDRc+kVSPc/U'
    b'M0s0xqDGMksMxaDGBWNQE5hjHMWgJvIIyjaLOeyUY173dbhCAzRXGHHNz2CenaSjdgZXAGl5pGVfSnujQPc4WY2q'
    b'8WIgoMULtOwrvWn/ANx/YRBef+MgvG0IPG7JSwPw+gH455kO9Vc+5OPokwOdZ6LOIyftrGr6U7Dndr6k9dxZbkN1'
    b'OiXUcxO5HUfiHZWNeB9N8gnpNnmvi5ZNr/a6XcFbZgCXe41mCemfdnh2LfpfshMY6IoNVwPvwVis+jA0FsG8cZuJ'
    b'wmrbcSBK0RTREAM2D53nHANos1Rzcr8UV5k8L82RJMVUPqyfO3uWVRJxTfJt9RjM7bqWYrY5JpiHJV8QPV+gIGi5'
    b'74LcjsxUuzQGcmvrSrYO9Y0XtXXMcQZcv+3gc4ysFleBu240hEqKnt+itzFHX3kh3Zo6S7oZV0xb+Xyj+tiLqIfx'
    b'7ptOtf8rqPwURjRdyJh9n0X0HIVnyCRf/LErUr44W/Qc0tPiaVKrOiNY6Wv6YVb7OcyQoqeAFnzc0B+cjz6Bb7qA'
    b'fR7uUolVbg1RoeAw2J9fBRquq1iPDp/oeTJskOhq9OJT9VPm3Wezih5zmLa+YsR5Qpop2dohevLCggQhI3b/SQuZ'
    b'Eatu1Q+Ge6IH5DFjfQ8bWb/ccRE6Qw6APUCO29rUiS/SrgiT6dayRvG1VNOK+jydj8koLfA6mskJwTVGuvZMRhHp'
    b'OkcXJHSESb8MlmDyLG1pWf0M6ZDTg2Uo2JbGFgzVNBRXm8qcdJuoFX1lPk+TMhRfs/uY5lWTW8azNAeDYnLkQJjo'
    b'uQ9MZPdjyozv5H7d6gXyfk2eE7HSlX/CTaIUInAirWjTaok8OTipJm4HJ+JJh4IxgIwjBrxBqlMdR1Qjtd03yQGD'
    b'6GkP05qPS61aw5EN3eX/sjHrhjYmK9QYji5YFHcH24PeqvrGC3TDhbyB+ITN2wbc2hjk4bIvyT/UiR6n1sXqxhe0'
    b'sFl83vd3kNTJkDAlbW7qDCl36HD40/Oa3RGrbgwbkIIwkoKRYeh8u4Mx3DzVGLzHgdEVYzzVxOiKMbHq0yAV5buh'
    b'nmPo1qYak70i+e0h4UQPD8fjtNlZokfWDchhu3r78yE5/LVuaNgx12kBy2JVJeqdijbRs0Zbuee29uRmVzRK9zKs'
    b'LU1P0r1AR1Qiptn4WNGRfEmsotts/quvOcWDpOLAvscCQ+8OXEVqTkGBuP6cZZ3RdU3oGqfO13F9WL11N16adMpY'
    b'jslxQ5Px5LIapiWPLL91WZ9lXb3OdT07M5CloWuU/GmE3DBSPfkKZRNFGacq5ywVYWLVK4MXQAE23JdUX4VsXXgT'
    b'7VsiXRSGMe5n2NcNalQD4OnQVX6K55mZTNcG+o+ococ5soH55Q4R/xrUMLkjLrKh8jP08lyGffWUreGrEQ2qofIU'
    b'mvmfYEpkS6Q/0scs62CgWeK67+A5HTiq8WqCTgbKNyfGMO3wy3pmQ5s14Jwk93ftuEgeGC2eDjhh6MiB15ahVN/0'
    b'15f1tMfgVxf9rxYaJlbZ6KjAYHdOIcENrdjhxVHQHgqI1zYOaUtJ21/Vtmi0bUhtl5LJ0QSh3WnacNY2pLVdW23n'
    b'V9sdDm4ty4uJBUnn1Pe1K0HaWB1dATkLzd2hwZhsNjOazTSzmSY2U89mGtnMBDZzIpsZx2bGspkxbOY6NnMN43QR'
    b'JKcdFG1zhdMeHaeLIDldBMnpIkhOF0HyweDjwShvtpvUwm7yP3fTNZC7qT9303rnblKou+kayN20YrSbZr9yaDI1'
    b'fNnpjRY06sng3M5RbEa0pHOMXMcf1jMnXsas/vz36NwcY80G//3P4f15Uht1kbUNemnvH2kfUq5ogyFRhnXhLTZV'
    b'ywfHX4Q2nPC6Uz9e76ROhsmwfIBC6F3/xZ/AZoKeHHfPbJP0M9Zw+xMU1H5+6eob8MrGXZpHYO2g+xuDL53yAU0B'
    b'P3x7TSLZipbBsG85uFJzSIts1xaOYOrMaTkQV4VyjtCCRPbbYsKR4HIRfLMxOlOu16bUNDuVgyssDcFprdwWTOzp'
    b'dvInJmr6feaYFs3bmzZYF66ALOKuwcu1eP1A3DnFntsQPW+/DNij2NqVpyMpCBjntY7KZpxcM9sxDDs+plg7tFmy'
    b'fQ6YzcBxGHraHUF4D6t//tA9ha5m7NJlR9BPo4mJFIMudkU0ea0mshrgKnWq83eCxvyaNaF296v1GI6L0/oeVhHN'
    b'K8DlxJsV3/iDgHMOp8oqwDUCD9Q87IaTCqrrgu/f1vXl74bWNe9Kddl/+0/q+n9xXp1D'
))
