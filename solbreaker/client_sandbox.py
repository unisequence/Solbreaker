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
OUTPUT_SHA256 = 'bb2a0d02c74d0679c1c91c963c68b491270a46dfbf1c58eb85cbf3ef009d3dfe'
VA = 0x4B1EA00
RAW = 0x466E600
SECTION_USED = 0x2A00
MAPS_OFFSET = 0x3000
BOSS_EXIT_OFFSET = 0x3900
CALLS = (
    (0x1DCB58F, 0x1C61300, 'boss_stock_panel'),
    (0x17449D6, 0xCDDF80, 'boss_screen_root'),
    (0x1DF6D08, 0x194C490, 'sandbox_boss_menu_open'),
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
    if (len(code) <= BOSS_EXIT_OFFSET or len(code) > 0x4200
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
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNrte39cVNW2+BmYkUFHz2DUpaLCG93QLCV9Nyet0PEgpZOj4wgKWCijggg8OMcfV6nBwyjb3Sl6oPnerW7vPj+3'
    b'vi/v/dq7ZvZDBcbLYGmN6E1UKuxiHoRqDBP8keettc+A4KXvvf3z/av5fDjss/baa6+199prrb32PioHv4z7Tefe'
    b'MvrnaJJFk8yqqmmaU104iuPkA5YF7Xm7L11xkzMu4jdVrRhl0IK+WtGiVKc89TpH6jKztS+TazOz/W4+vba83rp6'
    b'lIGcSXcpZWbiVwRV7rTyMYKqPeTUmniroJJmPiZS8fRkPgUtAJF8Mt8FhPO55V7gIz8yn8s3LIeSd65WlXK+MFLu'
    b'0SRjcm1HMmlIriVCSBFC+ZwTmEyfr1XXAAIJQCGhqzDSp4nHqWC2CSG+ppbfXat4LmQuCvcyX6X3cFx5w4sPcJyr'
    b'vPNl6ID4loOUpvGNdxvIlqehSHxZ+Hy7AJ8f+w7yFRsADTv9gAjwPECERiIcIkKQCMeI0EyEFiK0EqGNgFRCJ/BG'
    b'fdhW2TblCjy9FYxmLkLWDeF3R9ItrOyoUox/MQJlJ2UcqBevAV5Dhu+iaE2uLS+rck2bmQwSkIYOk912RfqLTnX/'
    b'E9BE8W7/AYlmAEAHUwVZp1uQEr/75ajLl6EL+4Rwv4JXexsL6tPQh1zm5Rbwvv8AOvKVoXseh/8BXzHUckYsrYdS'
    b'NMB4IZDcRLasgVf5yjDJtMfFBkuEd2BxFJ33WY0yTZN7ylbfTKd/tk3u4VdbyBaRCYuNVpyKUxhZkA/5NPip3cpb'
    b'I+XOOOqKJdMTrWReYiwfY0wM886GXedTC86QNTfvS0Dm+02feuRumL3A0bEcN98ld1rotpD2r0bfKd5HUVW+NOhz'
    b'lW8kDcu58Ry33GqAN06rSvwOFMQP6rpU2RTRDTxpW58LFUbS52tRc+qVTcMROJe4VeLo1I5Qt0odncQ+Sdl0J6vQ'
    b'jigvTWez1kXsU0go36DOgBlYCBRH8LuFrim5q4cQuQpH6FVkCZgmzYxt7bWr0BEwP598Cvo9R6u6cLEwEkbWfcFX'
    b'yzR6FJ0/xdckfq84LsyBxQEkz2nV40GRbcKFXg3uyVyEC8s6bRIQoiWzXHRhGimOpVI8ybDS7Dg+hoNFZSYL0zJJ'
    b'yaxF8ua0CI4zZPMx0yb53ckHYc1ujh9acUqMmvpwllkTv0uurTiVmQ3U9bV3fL5a+ktcF2vvg3VB/HJnvLwmll8V'
    b'QeO8k7mAEIJh5KQoKODkKBvLYFCSm5IPBoROBMAKUDZWAIy4W4kD1kEzdbeQZnUD6kotX+FDlbUPUbY+hDiCquax'
    b'CtHEVjIVVGXrdKhR5+pg7AfY51jFbKywXa+I7K3IwIqE6xW4lDZHVtSK8pRsMdLe0VUBPaM62r2TE8TL7dgndTcD'
    b'Wx3XWKObFHuUsnUxkjmBS09o69ivbH0OWWxWAwBRtq7sZphPAWA3AmYOgbeReiPSqP6eNWvtqCSNylYRYQ5VVQBI'
    b'HWo32IVOfp9BcQa9l6P5TTm4jso6p/AvpOPicHcOnapMjwAT0tlxAsinM56krRQs29ZqJBVFwaLMMFKwKDPMMFAz'
    b'LGSGlYKhmREL+PfqMjzK74NxxS6iVj/oXRf1sDR280yzMocBRnnXRWdLt8vqeNk/FNoD7jV4ATIwIhIMUY+y9T90'
    b'cZt/YORGAIkE0ST7ozoiZb9ZPhAf1mK06Wg+yccuchS0A6YY564Txp1HWxEjtMiXc6XhoBCoKg1CS3JHVPJB+XL0'
    b'qrtAgv1D0YI2KWkG0tzdndzU3Q1AuX585TxTC6sjzclNDdOHf3ZPd7cyNQIIKxu59j0cQxneorhbqKN5rhbsbwtI'
    b'QCkPvp7HzVekFhf5iJwHtgCFXFJkb9thrtyfgJUAOA/Makep0Dyg9fH5pMGlrr0LtN4vJ4Ep7DQTR0i92APjcZ5c'
    b'VrZxXApnGv3FW0bgBmaFSp0uckr7FCgwIoGUsegwkw/CUk4bTypbUYojm9JSNjmn+31NfMVJAOCyhwFTqg7c5Y20'
    b'3SRGJde270MFLjWCDf1OqcoofjbSdoukys/Ec+KX7+HYqTddYXNxjFp9TVJ0Lr05EUlbc+lwLNiOSO/Zis2SyZZh'
    b'FCVV0nAlkmIzccYSp5U4LcSZRJwTiTOBOOOJM4441xPnGlJVi0SqLAZ8mtmTY8+rDH6APZk7rNrGnjXoFry7OP0X'
    b'8O4Ml0iGEW1Rcq1fnXQnDF596r1Mzi7emm2Gh9sCD4eVT/LzSV928Qvwz9/FZxzgk05D8TQWu/g5tfy4DTuB91Uj'
    b'uvhFzfAXhL/GjhF8+ilov3kHurGko118TFw2PJxGeKSZdR9w+A70AZ/8CnwAaYDIolcj2+i0eXRuBjEmgmMxUlcW'
    b'/DPDIlJdaKIa77MnInsq/06wTrVCMXMePFZ2dvFjaqHw+i5coAeA+zEfUtd4YBXYzGiE/y8gn4CxaQ/7n10H5Rc+'
    b'YOUFR+APuM/ITINCO/w5Ql181knAcGXAGwi1wJ4F9a5Z8LA7oe6vUHiplrXOqoMJhg5jMp+Ghyu3YwVpHB3s5W8M'
    b'9D3mHBA4hNztYC0yDiM/B3Q+YCitM6Fl1gkAtkDlCQTkAuBImOaCYwA9gtDl2BvrJCwYCrGiAB5LRN9BqLgTsYr3'
    b'oALCmx3wMzrgr5WfDmzEpML4Z3wIhflmEiIzYXRiCpHxpcBnzJOxAyIs5ubkziS6oQLVRThGN1SyQvOeeUCebn0O'
    b'F4AlwneQOA6JyUpZi7bL5EXrWu3NLAe8YLrWRB2HlOriO4IclJVqXMnkktaUQYXgwkeQihS1JwP+dbw1IDr48+2o'
    b'GW8n6tFBPHE008rXvoEAoUl0+w6Kc4iffKp4zXf6Im0Rkl3+TSInPrLC3RynyOOP/JZTan4x/RKwUdYyN10Lgq3m'
    b'xJvBYSjyzls/N5Dz5fW4YLBKNy3A0NHe3tOV9YlmuS2krb6qR5n3ICcN9yWiNwXtLCDCLuLYqX6v+5Nfy+smceJ4'
    b'KuwyPW16k2Nx9C9pFS4x4n5DvhwhxXr3vI6rTRwmr7Ny4t3tlSwEDdjjNLC3xL6mL8BQlvU81V4EJoPaE4BMDHXv'
    b'HF1ffvk0uEN+42F4ymU7OTavvCDsRJfdzJaxPSEgtLClLaDpgiiJMrcNURUuH3IS1hF6m+m4hnaprvtxD2BtXwcU'
    b'qX0SEV5G6Ldd6AB3gOMaiu5ge/sV5p13TJbR3IhxpB6w3mVW7ZkLwKKwg7AalDxkCEv+F5pWDLOzjxo0MFrCawD6'
    b'H0BYGhAOgcyayXnHv4FWbMMOqvjdFQ9NhElyKsIhJ3W8RoVtWtALg2lhIpJLR1ujhSoIHo3SsWSNlG1P1voqiFBz'
    b't3296H4vCiNfbZg4jN9d89B0GDq5fsJc6tih4ADsUqeAUOVlzRgNL5DupUILdVfZ3K1SPDAW93cdD6Hummj3dujS'
    b'ItXQdXFU2A69dB8n9rhRge4Adi7eRmfG0+o2XXD5vIHMjJdPG9Q0cLrdAYx0xHuoPV4ORRB7/BsRGM28wcIcvYnj'
    b'DRzq2xkyBjmgKvYwOTlkhSZyq1XuMQDSrQflOpN6/ipimhHTKM9ck9AdsGL57r5WE73Q6B7cZl1vFqW+B834984O'
    b'K8VGKd2BBGwUVT5zDao9VAwtTeyj4L3ASHDe/iRi1dVXUQ3j685Glp/lwDlEr1tDAuVnZUADse7MT6DCy1pwqa92'
    b'baT8iTE/pv+r+aI93spXrIDO5HVJHF+Rg8ZC2Alm+5Fs5GCTEwH2saBsKdQ1j87MAFVVhD0kNpHM6rP36tJLqGd7'
    b'qPsDMObEVQzO0V5AYd/mGk/sYsA+1sBW0iZ7Cli8PwR10/5qI/M3ft3qLjjEj/toNQ/wrccYvL6Lf6oO20wHhCCf'
    b'9BGaVn7cUYbz6jFG46mTDG51FesG2Pr8Ida2nR93hOE936y/A+o5NLYiIhcwGQ6FlyJ1tMifgOs8EfYqJ3UXEpMN'
    b'egnvWWiBHa29AkOsjQJLPUgWHEPMimLmvxZA2bpxR9jFoDc+hA4XKlA2AXxCzMwCNPJixy/YeIpREAGOBdMEQR6f'
    b'pPJJjcPWrekOFENsJFl9mnSTvG4iTAgqTEfUnofwn2nPfyO/rgQwrdHUsXP0EVtdyeIBpgZ5BaZjVraArSE9UBJa'
    b'QVPFSGpPcoKtIZd+3NqcSWLWBiPS8rO/a4UQp5asm6ibvV6jp5v82pvR5NfficHAx8zxWGETp2z6PdvkVeMmD3cg'
    b'Mu5AXvq/AJwLULbpEzqBfGFvviGiN9+QMl+rTmHpBLlHE58ckGvAnEi6Vn0shHtGtAZUsCQ32YSQNALA3hDu66C3'
    b'LfXE3ZauJyOouw2c/DAgupxVd9wMdRD85kdrkgrRKdbXiq1ozoO6I3s3Du0VOBGlrEb9LBb9yN8g4nHJB7yEbc6T'
    b'a7ve1DfaAYElCZRX0Ygp9mH6xpvfPZkqor7tV9LBsm3BVqrnGzRrXjRrGdIIKlSQ6T/UUKES/m2jrKHd5pfqFaEq'
    b'P1d5/sA1tnHHlspWzGCwfU0/bzufSmaXakP+/HbgT0k9LPcMWzVS3fU1+ATJnGlqXTHKsChba11RG6dURGLCgm1i'
    b'e5teuwmbmvs3XdvblCvobToemuZe6WvKdiCqnzU9HA9uJfVrFq/7TTvH3G2AYL9ZzQSILSTdRgKkHna+zaoRBCeX'
    b'dW/Z/so1pNUbqyhlZhe5ppYyemXxuAtIoFH0WQgdwF20qGon+rdmZRtmh8ATyOoE4HS1Se4xSreQZ+IBRIZQR2uf'
    b'N9Zayx/DEZaepTOMaZgcWSU/a+ZXFyuYHjqmSbCRaYXFrB4Hyum8NdUod8axHEhqLJ0f15sV0fG0oySQAetvEix1'
    b'WDDqabZJk0Zr0rF/2HI/uhVkqwfZqk9gWxEcu00jUdYXIT5Rh3aGx67K8BmMHdt9wHhVf8OClMdJfe5/sh1HhPSI'
    b'vM/JPJmZ/A+Gbx1WG/sv4cZ0A5bk97wsXrlJfu85VhiKA5Ld3v799WnH7s2s+5HYPe0Id5+y+/N+3cfq3UfL35jJ'
    b'TnQ24eYul/pRDDb+622A5+hRZ3awrY9f/fJr3HH3sC39nB5bVZvOWRNhpYUQ3e373JAJc8PmHCJD2EababaFllmp'
    b'I5ZlIOLQHJziKz6IwEyF1dYjJZRPnpfqSpbiytcZ586eJUg8bALJSfV393Bcez2ilVqIpNqu8b5y6CWMvRaw589+'
    b'3M77tgEKzTTLV34JolyZzfueR4AQSm5SH/xaT4usB0h4e2ez8r6ViJBixJrFWKzsgRHB9OM8fIPgaB6pw0pBJ5TP'
    b'uUzON1fByLVPAAiwXBBmIheYSJ07dYaUCWj6nnI/TnU9to7CTsPzyvsuQW1gA04dolG7OTx9hAXv1G4hLHjnK4LM'
    b'G4fUWyGMa/cbcJTMtgC/gRp6Zed9z2J5ndE1bXYG7ysxhMUljjY1BbtvwO4rDX3dD+F9v0Fv/4wZ6BdDyVam8r5l'
    b'rF0ndbTZ6sRYFN+NvU012oRO3vc4iwHMSOlRLJZ12i7xFRgq01u7/Sk+ZPVVeJucCgMaDxgX/SnirRdT15vFm+hW'
    b'PQILvYuxkfphF8Y7fzOXYnVC/2rMfqpv6tVDS0ct7U5dX2sQbyUn+lDq2ofJp6PkuqFqhY7G8/KfoOpialkEX7Ed'
    b'kfqojURq2YDWjiP7PqZLSKqVnCDN5GTd6agjKjjF8tMc8BRdR5rlVh7/1akRcmtCdF35lxzMl2h6rxY36811HUPq'
    b'VFP539DE3IM10Y3RzdFBkroe1mFqQsf9VMJcBQHFLgsR0G0pzjTt0FtGkm0hZeaw9dZabT3tx39g1px561576KLZ'
    b'ZnByLkW+4+O3jFoQ5q5Z9Z2DBbwQB3w3N0BdX2cpHVLlZemC59gzxJ6d7Kmyp5ElGXpY+QKOxAFY3rAL6csskLRY'
    b'kmYlaRaSZiRpZpKWRNImkrQEkhZP0uJI2nqStoZUsuxFJcteVLLsRSXLXlSy7EUly15UsuxFJcteVNYMyFnQ7KsH'
    b'dYPvAtdDyyzKxsOfvmVMB/+7INOniW6abiUhNRUdBSaJBXiZfo5ZF5nRrqObpmB/LgvU/kLZ2vzthkibVRpOU9B7'
    b'D6EpFmK3oLU9SULMzXScH+gs0fQNH47W6xe3wCBUfQUeVbKi9YMBN1UtqTBAaAHu6zsY8f3MPIj3kgZ42dsOPNXR'
    b'jcgGWkXy4eh6+XLuqmiGWsG6weRwvYXTBVT/YMFudt6sOzMwdGVW4lCpFIcZEsHCsllg9MCjxIOLo0ICpmuFRPBO'
    b'6qUzTGZY5i+ojIdlxB9erJFoTNTJdqPkgvUHq5OmJNkMUiouVc5mT2IxJLpLBKQbKWwwk2gUcc0i9jQ6w0zsTjqE'
    b'zjcS+zziyqCpAMhCc/I59EPsidR9jDqaQVup1KrrL3EnwuImUiwEiabiD2QDldqII4GUWahDhRiBuOPlAwm6OocF'
    b'/+MwFPydWJbLA0lhqokjhOILneqdZ8LO4u2zTLLEPk2WbqXu0GSnUYqhI2xpSbCTbSLOJDUeTRauo05FDmZXGKBP'
    b'6gDOLAsz9V5v3POTay51DGNhImMhCyJvnji2Yxz8svpqG+u/zuSt3wC72ddII3FsU5PO6p7gB1QyP2wdYVR7GbuJ'
    b'933FJp2v+Bzf7SD5Nt7XxOLusZhhDGBxTwJ6CY2veBffopQXp/yxNNIWFEcoLxafBC0N8r7fIfVGRXpDLYL+QEfr'
    b'QL/fAvl4q/AGiyLkRmascNcUkFvD3oDImClQhCCpxt0JucyiDYpA+lsGmQUxR1ZirLrkLKrzy1R4jTq2a0EgG+wN'
    b'TkDP3G0sOAlheEIuq78+i3m28f0i+Xojh5m5l2GkbgECQKZfZXh6YQHRMrN6MBoH+CjYVHXW37BTC66hdBMXK+Fx'
    b'pCaNkcssnJhIN7LdYsokmpblOyjGKvLT38BgGKSh+YZyf4IzyPUlb+rNnJoPZE3rR3LhNTRHcZjZIQidY6bJmMJa'
    b'ayQQtJ34Uj9C8JtCT7zBzUX7gcsFzxQWnWHZQ7msGTwQS3yBuxOf6XV1kgh7K3kfHhBhsrxVMillrR330o0v6+v6'
    b'/ORUi9QJdA4Dnfs21rC9+ZLVJ5V9O9m+8JjiboE4Jq6aU8j7sIuZQ44Q9zHcPhwD8AMfAkoz7mDyI9PnAlO1hBFm'
    b'Oe/QgsFTVBvMuLWojMGtRed64vCSLrXntG4AKkzFKX/l9O0E7BRUbxuLPGF2vPpWYcAEyWvMoL3iHbqwkyNhw/h+'
    b'jx4E4jbjpHrgjj6nHym9TwUvKLP0Z111IURXcJeBKvwmRjbuCtDizUyLG0GLNzMtbuR9z0Ww7T+eSa7XT31E+X2W'
    b'uhLv4q26BjONpbq6wm7S3QZjHeyIRRHENl31QZ/Vt9uZ7qOSKsIepqcQYTvaQEOtsFOYBSpdheu1Vj4Qpx+I6usB'
    b'e5Pf1yex4hoLVCrU29tYCmF2GjzSp8OGFnbd6ZPgMXsKDCQAZ8yClxkpirADFNwNU4ZbegcYPNyJg/KEt/Ng/2bB'
    b'dv4PajgNbfXsgLGwvb+NpWSt2TsDcl82Xu71bFAh7aIU9YX3TQ7HixPQVQVHNyppmnxpMb/pbkyfYD7+kJ6Lx4wy'
    b'ZrbnHOTHPd+KO8aNuOCB1ptv6DRlDGH4pKu4fUcR3PBY0IMIOxjCgqNA4TifdA2KP7DiuK1tjNA7rH4M4m5V9Rw5'
    b'9Jj1TRe/6DjAypAQkM0KwMu+FoawqI5P+gTA9fC3Ec8T+XEBljHZF9IzKz1QT9nhKmD8FSr+hA35dxr1PDlmNjbu'
    b'YqhjLvHjMMEeQNAFvXvMX9OdTJ7DjOyfwmTPsazJAkyLs5EFvSRstEl1DZvul/Vk+H9t1///Vh+cmD/jGHREJGt9'
    b'q0ELyntRSzjxV6BWe5haVYbtpaORZh/qs5S2L3F24wakMUCjz4CfVm8/zbT6ZuoE6y6OoG5LyOBMoW4r/Jvee33h'
    b'kBFXbdNwtmonEkctrNppn8OCdB9AB2JWx6KNchxQtgbxZC9AhVrVcgZBjVBUtrbiceeVNgQ0M8B5BJxjgEMMYOjB'
    b'A1EGCDJALAIaGeAYA9wDAEVqUXcBDJwj5sLPp4MtctQqMudtN/Q7ertuZY26bVWHMQFiBghQ/lk/AVacDgvw655e'
    b'AWb2Y09Abh7ux146Akb3Y29ZmL2bB2HP+g/YU/ZO/FbTXGpOJPqZFRhKdR6ygjIo4wGuNrfgTQssQrhGGkwpv1fB'
    b'7SjVq7HLjayrS2B/qQ9RaJoxXdmIZ5F4KkkYTNnIjg58SUgiSLcgDF35N0z/zOVrrJwVc53o8ZW9s1iL0M53OHIE'
    b'jyT2Pg0AnuwdC/9WWagP64kP36AtO7A3YcDH6X3pXCjVNThA3V8wC76ObslCIF7ceGb1yuRaZe9SeE/Z/Fzc3Zc0'
    b'rdISJfdYpBDAlVIk8QoS3xwfWRlvSLHVS9+CD1amWegrjP5s6+ijyhZsb6gjn9gC4tCUzbMiuyotER3fKVuWATyd'
    b'OswgwJP5HBsVlFpRZumyC+aBY69+j9vHQPfQ8N0O4ugkV9Slp3Bi8VKNqWrix5weJWPqjq94hXnGkPLSdhAw+aJ6'
    b'9nQ4fLcyaATIox5FWEPfNnjPZ3h5hd9Sx7J0FUfELN8pMaP3iLXqNaRYxW5xCCo04t/5sO4cmhg0TZvQxChV1kV7'
    b'0MAdJCf0U7QYOQtIa00QHUO34dYh9aNWFgrfIOJIJmJsWEQri1BVkNJ7kt0ZAMFMCZ+9g7oJXFZ8iOykTkqTeyL4'
    b'TXuZtJ149UEXradV99WYTVS/aMHbBW3ghvmK6nDUiFdiwl5XRsmkMmjMpx8EE+hFnvlxdSX5eJbZw2xuY0k2SLTp'
    b'Kh6DPjMpsosfW1spdEk2RejKNygvGNlNHpCI3YNRn4Xt0cIFoFHRumUGVGyFd2Gk8E0Yfre7i3Hcpu79oi9kgKGg'
    b'xkQ/jYW/ePibCH9TEv1SqadkwoMP5BYUcDM84tTStYVLZnrWusQc0YOA9LzC3KLV85aXeHJynSVFSzylpY/ncp6C'
    b'vJV5hTliXlFhNlcqFZd4VuYsWZvNLSvJWZUnQmFx3rKnFheVlmZzUiHiip7cbC4zZ+XKogQu17M0RyoQ4X1lUa4n'
    b'gcssyFvlKYX/Yt5KfF1cJOJbYZHogfYrPCWFnoIwh46iVZ7UvAKPsGYqV1pUsBi4gnp7zkpPSc60AqmEKykqWvl4'
    b'YZ6Yl1PgAOL932dhL/0BU4GbAQ3yCiVxIMo0YCX5RsCDDJAGA+Ip6esk/NqPhA65TuH6+4OcyyOKeYXLSseNGTNu'
    b'VtGSnIK5gDC7GMezlHP1CYbI4zjXkpKigoL5eZ7V40pZ0V5UKObkFYLgUADAuOstHEVIA4di3LScJSuWlRRJhbnj'
    b'hNw8saikHxoiuArygB9uINDpKVniKRS5/r9M5P7+pUUlK7PxaIozR63I71/PBBjnWjytqKhUHM9xI1AFb4Qn/z08'
    b'tWDteGxv+Xt48mBwmObxPwIfFH9WUU4uNhg+CDx5ELirOGd14SBwVLrcQeCzGTbHDbsBPu9fbIPCZ85PHhTu/M3j'
    b'8wcdh7wSz2D8I3ww/u0FnpzB+E+VYOUMQh8UsgAFu3FeZhTlDjrOAB8wzpZhQ6OGmHDPGhFpGhJljh46jBvsN0AP'
    b'+vE32G+gHuQtWy5e5+8f4TvySkvBNjDYP4M/L6dkmecn0H9CWlncX/5/hD/XUwCa9s/Tn+fUYUP/SfwnPWvEn8IP'
    b'6s1PwUd9KvkJ84X69FPogz4NWA8/hg8Wqld12M98Hd6rIuwXfR3eqwoDxhPgvVN+I37v1LJf1HV47xTeiD/PU+Ap'
    b'LioRB/APcJySeTmFK26E6/2GJ2D4dXjvEN8oV+9Q3shP75Cx35Aw/IEHsnKLlkgrwW6XZl33ivev8KxdDN679AGx'
    b'n3CD/H6s/UrmS+5fDG7h/0nin2kPjPzk9n389zX2PlY0Un4Y/s94of77RzjutceyXqh/9PChQ1MmTJjw2F2+FY9N'
    b'9D40Gcr7oby/c86kR6DukeP/VfwYx1VN5biUxwC2t2hkJNBonAYEH+N+/v38+/n38+/n38+/n38//37+/X//hY8Z'
    b'aZnZpS7+WtPK/QUXNU0906Bp9PlcPFLx+w6KE6lBY3lCvIsj/opySjX7tiMo3aY5jXgFybvnBFTfLg1V5J0rvzCQ'
    b'81qQ4MWhpAhoVGZW5KoDNQDtd3RNPna56BNm4m8QujQ6dbyvVsIPRfBCCeXIcaXaefoV7KFDqd75kjfC5kyRWhV3'
    b'l1y3XpG5Fm/E3HxD/l1a0FvWBVxJfwEm6pCvPeNexVZvl/udX22IsGUYpf/O/5V2lDH+WoCbl+q6P5BinDV12v0N'
    b'KWZXdwP3NF7wopvwmkZHJJ1hJVOj6YxY6rak4DdHU4fjvYF/jSFTb6aCmcy5g0y9lQrWhqn3GMjUUewiyfUbD3XK'
    b'Bpa2ZnfsWvQvNNil+hBPelZFskvVnckXlSc0+ZJxdYJisRJHM7/PqjwR9F6KXs2vuDg0RRFvCVJ3M4jw1YAb9/oV'
    b'C3ZhBSQZTvxzlXI80SUntGDfTQ9ynGV82Uc+9QH9lP0/9VN2PG/le0/Zg7xvM57qPWuJ4Dd5WeZUMutZUwJjgPc9'
    b'IvH+goyjQilebQFoQMZ7LY+yU6Iedp8YVAOvZDSqHH5sIl9ll2OozC7BnNePQyP4mno1lX2vQ+VQ/4pMfks93hoZ'
    b'rfW7nKieOQdKGDjbpWmYLDYTN16mYt8iqaEGdlS0HG8k+FlS1mLlOPXMh/o1K3E2u0EVUl66il8LyHhjRm38UL/Q'
    b'gpdoAGUodYdUF37AVk+q8fgP8KE6pLNkyARmF7GrLDWAk7HwOltPMracjC2c1wT5PSYEfgBb1sz7HsLOLOEhH4OE'
    b'ZRy15ItEaoV+h1F3m3rqB/bhHBVaky/a2GCJw8LfiHV3k0+pa1LyQeJo8TWJ/7KfKdaM8U48726i7mN4X/zWfrf9'
    b'W3wXpei72YTw07tnyNoQKShfM6x6RB996PJJWtZMHW3KS1NwkGoPsrEbiXy48BaUU5MvG1eP7jDpOmz0GkIddwDR'
    b'RLzdyejizWTGZXsHy+7L10z8plb9HrSBfUKBPXHvcglY7mRXjtz4bRzHPu/Fj5/7X3MO6dec29Rr+O0BXnOWatpf'
    b'ZGfLbOx5H7t81ztTv0FO/89VnFqbjDMlLg93CJTf5VIG6TOf+5Een+vtUQRoG6XsLtWfkaip9vEaA7s6wPrVlWbA'
    b'1zVuFSc+/XxYH/XrNeSK6q/Dw4sQXmTyfnGYHawJneofAapf6QR9TTmARzN0iO+UmI8f4TLdzMLZeLARl4s+Uewj'
    b'EqYsHXEMYxZiWBlGp46B38uaUXP//Uq/ZeYIa22EPuW4zj6/MuD8xayfv/zbWRShJqSLYKFS+JSJq+s7ZeJuajDM'
    b'Z8dMYe7ZgnvWz65hCWYaByv9UZDjYfmZWJ6EVkXQIcpLY/GI0KGqLx5gunUndajyu/qq+HecEMmir65mNfVGvvT8'
    b'TikmfefnFEgebqWnUJqel1NQtAzT8tdrp0miWFTIcVMX5xXkiWudOYUelpxBjKICzyzPqgHvOrHevFVpqTNvyQpP'
    b'Cac++5WmmV78VtP0qy7lnYk42742/GrVzS7GvoJlorSw71hjcYy2BBHia2bwY/BUP9mPH5sjmDIACZi4QK2h/DLe'
    b'TCS+VgBJI+VLkdJwush4353lV44BJ6vupToNRyyBrlgPBDpk6ESy9N1PJD3qlv36hVXRwj4fAsgjtXgjQ9lyATvd'
    b'izzyu5+bxD7fqZtALBtS5J5h0lfkRHJt3aUIW6PI00fplE3kA/YReUrH18r028aSLSqTpBPvsgsXiC+EBR8C8yOX'
    b'I+9O7e1mJhUTn3HW99lpvZX7X48Z9iA='
))
