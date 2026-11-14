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
OUTPUT_SHA256 = '594ab15bd9e62c4a640c1f6940fd8c440ec3630b7331391dced1aac416629991'
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
    if (len(code) <= BOSS_EXIT_OFFSET or len(code) > 0x4400
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
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNrte39cVFX68B2YkQFG71C0S7tUtNFGpuFsft+ctALHi5ROjQ4DGFKhkIIILNzrj1Vq8DLC8XSLXdT8vlnbu9u7'
    b'9e763a/tuma/jB8uAy21I7o5KhUW1SWmGsMEf+R9n+fcAcG1727/N58Pl3Ofc85znuc5z3me5zznXJWDX/5002cv'
    b'GdsXaZJFk8yqqmmaSyXXc5x8wLJkoHTvmXMe8rGbtJuaVl1v0AK+FtGiNGc89AJHWgsKtQ9tLQWF7R4+r6Wuzbr2'
    b'egP5OM+t1JpJuyKocsjKJwiqdrtL6+GtgkqCfEK0UjJS8BD0gIbk77luQFzGrfQCHWXRZVyZYSWUvIu1poyTFdHy'
    b'iCYZbS2DNtJhayFCWBHCZZwLiMzL1Zq3QgPih0LKUEW0TxOPUMFsF8L81hZ+b4tScqrgwcgoueqrN3JcXcfArRzn'
    b'rgvthAGIbyVwaZrReYOBbHsYisS3FJ9/KcfnO74uvn4TNMNBXycCPA8QoZMI3UQIEOEwEYJE6CVCHxH6CXAlhIA2'
    b'6sO+yo7Uc/D0Mjy+YoRsmMTvjabbWNnZpBj/agTMLsooUE9fgHYd+b7TotXWUlfb5J67wAYckI5Bk8N+TvqrjvXN'
    b'e6GL4n38G0SaDwAdTBUknW5DTPzenTEfnoUhHLdFxhW82l+woD4MY8i1Xm4J7/vfgEc+F7fvHvjv91VBLWfE0kYo'
    b'xQKMF/y2HrJtHbzK5+Il0z43E5YI70Di9TTnva3KXE0eqV17FZ333g55hF9rIdtExix2WnU8SWFogT+k09BOHVbe'
    b'Gi2Hkqg7kcxLtZKc1EQ+wZgaoZ2JXadTC8yXNQ/vS0Hix02fyqXC7Plvmc5xuW45ZKE7wtrPjb7jvI+iqnxo0Oeq'
    b'zEg6VnIzOG6l1QBvnNaU+hUoSDuo6yNKw9HTQJO2/fFwRTR9ogU1p01p6EfgYuJRiTOkHaQelTpDxDFLafiaVWgH'
    b'laemsVkbIo45JFxmUOfDDDwAGKfwe4WhOcVrJxG5CSX0LJIERJMgI1t77jwMBMTnkndBvxdpTadOV0SDZD2nfC1M'
    b'o6+nuXN8PeLXivPUIlgcgPIzrXkGKLJdODWqwSMFD+LCss6dBYho9UI3fSCbVCVSKZnkW2lhEp/AwaIykweyC0j1'
    b'wgflLdlRHGco5BPmzmr32LpgzW5Jjqs/LsZk3rHUrIlf2VrqjxcUAnZ97R3JVX/zE1wXB2+BdUHa5VCyvC6RXxNF'
    b'k7yzOb8QBjFyUgwUcHKUzTnDmmbrsXX5hRACYAUomwsARjx9xAnrIEg9vSSobkJdaeHrfaiyjknK9inYRlDVUlYh'
    b'mthKpoKqbP8x1KiLdTCOA+RzrOKnWGG/WBE9WpGOFSkXK3ApbYmubxHlOYVitGNwqB5GRnV0eGeniGcHcEzqCQJZ'
    b'gxdYpysVR4yy3Y5ojuLSE/oH31S2L0cSg6ofIMr2ecOs5UMA2IuABZPg7Qq9E+lUf8O69Q02kk5l+wKEOVVVASB1'
    b'qsNgF0L8foPiCnjPxvINRbiOakNz+CfzcHF4QnGZyrwoMCGhwaOAPo/RJG2nYNm2lyGqGAoWZb6RgkWZbwZBzbeQ'
    b'+VYKhmZ+IrS/SefhLn4/yBWHiFn7M++GmDukaVsWmJVFDHC9d0NsofRjWZ0ht8dBf2h7AV4ADUhEAhGNKNt/rrMb'
    b'/IahmwIoUkST3B4zGC23m+UDyREtRpuO5pO84yaHQDtginHuQiB3Hm1FgtArny2WJoNCoKp0CL22wRhbl3w2ds11'
    b'wMGbcWhBe5RsAwkOD9t6hocBKLfNaMwx9bI6ErT1dMyb/N6Nw8NKZhQgVjZzA/s41mRyr+Lppc7gYi0w3hYQv1IX'
    b'eKGUy1WkXjf5GzkJZEETckaRvf1vc3XtKVgJgJNArHaICsEJvY/kkg63+uJ1oPXtx9PAFIbMxBlWT4+APE6Ss8oO'
    b'jsvgTDd/8JIRqIFZoVLITY5r7wIGhsSfMQ0dpq0LlnL2DNLYh1wcbMjOaHDNa/f18PXHAIDLHgSmNB24zhttv1KM'
    b'sbUM7EcFrjGCDf1Kacqveiza/gNJlR9N5sQPX0XZqVeeY3NxmFp9PVJsMb0qFVFbi+lkLNgPSq/aq8ySyZ5vFCVV'
    b'0nAlkiozcSUSl5W4LMSVRlwziSuFuJKJK4m4NhLXOtLUgkiaLAZ8mtmTY8/zDH6APZk7bNrBnlvRLXj3cPrP790d'
    b'KZF8I9oiW0u7uuJaEF7bszcxPod4a6EZHh4LPJxWPq2dT/twiF+Cf+1DfP4BPu0EFE9gcYhf1MKnb9oNtK+ZMsQ/'
    b'GIS/APx1Dk7h845D/y270I2lHRriE5IK4eEywiPbrPuA89egD7gJhoUphMhiVCP76dwcujifGFPBsRipeyn8M8Mi'
    b'Ut1oojpvcaQieSr/cqBVtUKxIAceq0ND/NQWKLywBxfoAaB+6lvUPQNIBTLzO+H/k0gntGjYx/4XtkL5yddZeclB'
    b'+APq8wuyoTAAf87wEL/0GLRw58MbMLXEsRTq3Qvh4XBB3T+g8FQL6720FSYYBkwoeBge7uLBVaTz5sAofVNh7Kmf'
    b'AYJupG4X65H/NtJzQKcDRGldAD2XHgVgL1QeRUAxAA5GcC45DNCDCF2Jo7FBIowhE6vK4bFc9HVBxbXYqmofKiC8'
    b'OaB9/iD89fHzgIyELJB//ltQyDWTMFkA0kmoQMIfAToT7kucEGExNyeH0uimelQX4TDd1MgKwX05gJ5ufxwXgCXK'
    b'10Wc3aJNqe3V9pi8aF2bvQV10C6Qp/VQZ7fSXHVNgIOy0owrmZzRevKpEHjgTsQixezLh3+DL02IDj78MWrG5Bv1'
    b'6CCZOIO08bkvIEDoET2+LnERaSfvKl7ztb5oe5TkkH+Ryol3rvIEkxR5xsGnOWXrD6edATJqexfnaQGw1Zx4FTgM'
    b'Rd599fsGcrKuDRcMVummBQg6NDp6nrIx1Sz3h7W15/UoMwcp6ZBT0ZuCdpYTYQ9x7la/1v3J/5I3zOLEGVTYY3rY'
    b'9AeOxdE/oU24xIjnRflslJTo3fcCrjYxXt5g5cQbBhpZCOp3JGlgb4lj3ViAoawYeWigEkwGdaQAmgTq2X1zW93Z'
    b'E+AO+c1vw1Ou3c2xeeUFYTe67CBbxo4Uv9DLlraApguiJMrcNkRVuHzIMVhH6G3m4Rrao7qn4x7AOrABMFLHLCLs'
    b'ROiXQ+gAd4HjikN38PzAOeadd82W0dyISaQNWr3CrNqjp4BEYRdhNch52BDh/K80uwpmZz81aGC0hOcA9Cdo8Ihf'
    b'6AaeNZPrml+BVuzAAZr4vfW3J8MkuRSh20Wdz1FhhxbwgjAtjEVy5lBfrNAEwaNROmzTSO3zNm2sgghbb3BsFD2v'
    b'xmDkq8WL8fzerbdPA9HJbbctps5dCgpgjzoHmKqrDWI0vES6iQq91NNk9/RJyUBY0j8NPIl6tsZ6nochLdJWuiGJ'
    b'Cs/DKMNHiCPpev+wHwcXf0QXJNPmfp1x+aSBLEiWTxjUbHC6w36MdMQbqSNZDkcRR/KLURjNvMjCHL2L80UU9Y9Z'
    b'YwxyQFUcEXRy2Apd5D6rPGKARld3ya0m9eR5bGnGlkZ5wbqUYb8VyzeM9ZrphU434jbrYrcY9VXoxr/6aXwNdsoY'
    b'9qdgp5i6BetQ7aEiriZ1DIP3FEPBecejSFTXnkc1TG79NLruUw6cQ+yGdcRf96kMzYCta8tSqLBTCzzia1kfLf/d'
    b'WJYw/tV82pFs5etXwWDyhjSOry9CYyHsBrN9ZyFS0OBCgGMaKFsGdefQBfmgqoqwjySmkoVj9l595Azq2T7qeR2M'
    b'OXFXgXN0lFPYt7lnEIfod0wzsJXU4MgAi/e7gG7an+1k/qZdt7pLuvn0v63lAb79MIO3DfEPtWKfedAgwKf9DU0r'
    b'n36ItXn2MMPx0DEGt7qrdANsfaKb9R3g0w+ydk8E9Xdo+hkaWxEblzMeuiNLkTp75b+D6zwa8SrHdBeSUAh6Ce9L'
    b'0QI7+0YZhlgbGZZGEC04hoRVVcx/LYGydfOuiItBb9yNDhcqkDcBfELCgnI08uLgD5k8xRiIAKeBaYIgj09T+bTO'
    b'+A3rhv1VEBtJVp8mXSlvmAkTggozGLPvdvxn2vd7pNedAqY1ljp333zQ3lq9bIKpQVqB6ITVvWBryAiUhD7QVDGa'
    b'OtJcYGvImW+3Nh+nMWuDEWndp7/ugxCnhWyYqZu9UaOnm/wvr0KT/6PrMBh4hzkeK2zilIb1w7jJa8ZNHu5AZNyB'
    b'PLUZgIsByjZ9QgjQV4zmG6JG8w0ZuVpzBksnyCOaeN+EXAPmRPK05sNh3DOiNaCCxdZjF8LSFAB7w7ivg9G2tRFP'
    b'f56ejKCefnDy8YB0JasevArqIPgti9UkFaJTrG8R+9CcB3RH9koS2itwIkrtVtWC7HXMhrXklg94Cduc21qG/qBv'
    b'tP0CSxIoz6IRUxzx+sab3zubKqK+7VfywLJtw15qyRdo1rxo1vKlKVSoJ/O+2UqFRvi3g7KODnu71KYITWXFyhO7'
    b'LrCNO/ZUtmOCge1rxnnbXCqZ3erKRIyfn4aITMl6Wx6JX3OFuudz8AmSucDUt+p6w4OFWt+qliSlPhoTFmwTO9p1'
    b'Ouv68Piu60e7cuWjXWdA14XnxrqyHYj61ZXY9UboqmZ9zuL1dtPuqTcYINgPqgUAsYelHxE/aYOdb1A1AuPkrO4t'
    b'B565gLhGYxWl1uwmF9TfMHz/SMZdQAqNoY9B6ADuoldVQ+jfgsoOzA6BJ5DV24DStSZ5xCj9gDyaDCAyiTr7xryx'
    b'1ld3N0pYeozON2ZjcmSN/JiZX1ulYHrosCbBRqYPFrN6BDDn8dYsoxxKYjmQrESamzSaFdHbaYeIPx/W3yxY6rBg'
    b'1BNskybdrEmH/2XPN9GtIFkjSFZbCtuKoOz2XoG8DkB8osaFIrJrMrwHsmO7D5BX8xcsSLmHtBX/H7bjiJLulPe7'
    b'mCczkz9h+DZotbP/Em5MN2FJftXL4pUr5VcfZ4U4FEjhwMDXF6cdh7+dDV+Gw9PByPAZe98fN3yiPnys/IWZ7EZn'
    b'E+nudqtnE7DzdOhMnCPqgkG29WlXP/wcd9wjbEu/aMTe1K9T1kNY6QGI7va/byiAuWFzDpEhbKPNtNBCa63Umcgy'
    b'EEloDo7z9YNRmKmw2keklLrZOVlum5RUt8G4+P6FgsTDJpAcU38N0eXASWxWYyGSar/A+zBui7S+CVpnC5kLpWuh'
    b'ta2HFpjVeaihRzESCqu/mgqd90SNNV8PzXPvv8fB+3YAEFrL534CnJ+7n/c9gQDEoUZ/rmdRNgIkshu0W3nfamyQ'
    b'YcSaZVhsHAEBYrYyB98glsohrVgp6IjKOLfJ9Yc1IOiB2wACHJZHiCgGIrIWZ86XCqCZvgVtQs1ow94xOGhEDXjf'
    b'Gaj1b8KZxmbUYY7MNmGxPnVYCIv1+foAc95h9TOIpQbaDShUs93Pb6KGUd5532NY3mB0z70/n/dVGyLsEme/moDD'
    b'd+DwjYax4Sfxvl9gcPCoGfBXQcleq/K+FaxfiDr77a1iIrLvwdEyjXYhxPvuYSGDGTHdhcXakP0MX4+RNb16uD3D'
    b'h6Q+C2+zs0CgydDidHuGePXprI1m8Uq6XQ/Ywq9gKKU+PYTh0UfmGqxOGV+NyVJ1o14dV3P9I8NZG1sM4tXk6FiT'
    b'1oF4+USM3BqnevRmPC//EapOZ9VG8fXPY6MxbFcgtltRbCjZ1zC7QrKsoEBBcqz1RMxBFXxo3QkOaIptJUG5j8d/'
    b'rWqU3JcS21r3IQfzJZpebcG9fbB1cFKraqr7CC3SjVgT2xkbjA2QrI2wbLNSBqdTCVMbBNZBbZjAUpCSTHO7XzKS'
    b'QgupNUeMvdZnHxk48g0z/sy5j5pPNy00g090K/I177xk1AIwd0E19zNY7w+gwPdyE9T1BZYBIk1ell14nD3D7Bli'
    b'T5U9jSwnMcLKp1AS28AawKZlLBFBshNJtpVkW0i2kWSbSXYayZ5JslNIdjLJTiLZG0n2OtLIkh2NLNnRyJIdjSzZ'
    b'0ciSHY0s2dHIkh2NLNnRuHVCioMWnu/S/YMbPBWttSib3373JWMeuOslBT5N9NA8KwmrP0S/gjllAV6u+owZI5nh'
    b'bqUNc3A8twVqf6hsD365KdpulSbTDHT2k2iGhTgsaJyPkTDzSq+fnOhb0VJOnozGLuMHIISiT8ABS1Y0liBwU9Py'
    b'egNEIuDtvgKJNzHzIN5EOuDliQGgqZVuRjLQiJK3bm6TzxaviWVNPWwYzCW3WTidQfV3Fhzm6FW67wO7WGslTpVK'
    b'SZhQESws+QU2EhxQMnhEKqRgdldIBWemvvUx4xmW+cMqo2EFaY8s1mg0Jupsh1Fyw/qD1Ukz0uwGKQuXKmd3pLGQ'
    b'85UQhuohmmeksB9NozHEvZA4sul8M3G46CSaaySOHOLOp1kAWIrm5L9hHOJIpZ7D1BkEbaVSn66/xJMKi5tIiRBT'
    b'mqpelw1U6ifOFFJroU4VQgriSZYPpOjqHGH8v+KR8b5ElvoDTmGqiTOM7Ash9cv+iG/Z/CnjLHVMk6WrqSc822WU'
    b'EugUe3YabHx7iCtN/RydGa6jkCIHCusNMCZ1AmWWBwr0US9NEZALbnUqI6GQkbAUAnWeOJ/HsHmnWqmP32rytm2C'
    b'ze9zpJM4d6hnPtE9wTeoZO2w0wSpjhJ2Je/7hE06X/8+vjuA8x28r4eF6dMwIenH4r4U9BIaX/8KvsUov5zzXzXR'
    b'9oA4Rfll1THQ0gDv+zVi71SkF9UMYB50tBX0exPMFW8VXmRBh9zJjBVusvxyX8QbEBkTC4oQIM24mSFnWXBCEUif'
    b'ZpCFEKIsTU1UZ36K6ryTCs9R5/NaANAGRmMZ0DNPP4tlwhjNkLNqzKeYlpsxLvBvM3KYyNsJkvoBIAA04yoj0wsL'
    b'iNaa1a5YFPB5sKnqNR/hoBZcQ3kmLlHC00tNmirXWjgxlW5mm8uMWTR7qa9LTFTkh78AYRikuDJDXXuKK8CN5Xra'
    b'zJxaBmhN//cKLrKGFilOMzszoYvM1IYZr/VGAjHe7z/UTxzaTeF7X+QWo/3A5YJHEOkfs2SjXBsED8TyZODuxEdH'
    b'XZ0kwlZM3o/nSZhb75NMSm3f4E108059XZ+cnWWRQoDnWcBzy+atbCu/fO0xZf9uto08rHh6IexJauYU8ivY9Cwi'
    b'B4nnMO42DgP41regSRA3PGXReYuBqBbCELMUeXjJ5TNam8y4E3kpAXcioY3E6SVDatcJ3QDUm6oy/sHpuw/YWKiL'
    b'+lmgCrPj1XcWEyZIXmcG7RWv0ZmdHQ37y9dG9JgRdyXH1BeuGXP60dJrVPCCMkt/1lUXInoFNyWown/AyMZTD1q8'
    b'hWlxJ2jxFqbFnbzv8SiWLcAjzI36IZEov8YyXeJ1vFXXYKaxVFdX2Hx6+kHWgcFEZEHo11Uf9FndPMB0H5VUEfYx'
    b'PYWA3NkPGmqFjcVCUOkiXK8t8oEk/fxUXw84mvyaPon1F1igUq+GPmIZh/uz4ZE3D/a/sEnPmwWP++eAIAE4fyG8'
    b'zM9QhF2g4B6YMswAOMHg4cYdlCey+wf7txB2/2vVSNbaWrILZGF/bQfL4FoLd/vlseS9POrZoELaQynqC++bHYkX'
    b'b0NXFbi5U8nW5DPL+IYbMNuC6ftuPXWPCWhMhC/q4tOf6MMN5mZc8IDrDy/qOGUMYfi087jbRxY88Fgygg12sQZL'
    b'DgGGI3zaBSh+w4rp2/sZopdZ/VRsu13VU+ow4tIvhvgHjwCsFhEB2qV+eNnfyxo82Mqn/R3AbfC3GY8f+XQ/S7Ds'
    b'D+uJmBGop+wsFlr8Ayr+iB35lzv1tDomQjbvYU2nnuHTMR/vR9ApfXhMd9PdjJ+3Gdo/RtB+xpIsSzCLziQLekmY'
    b'tEnzVjbdO/Xc+W+f1/8/rQsn4c8og8Eomza2GrSA/AZqCSf+FNRqH1Orxoi9dHbSwu4xSxn3Ic5u0oSsB2j0x+Cn'
    b'1VAf0+qrqAusuziFeixhgyuDeqzwb97obYduI67ac5PZqp1JnC2waq98Hxak5wA6ELP6zQncOx1Qtr+AB4F+KrSo'
    b'7/UjqBOKyvaX8XS0mwGCDNCJgNcYoJsBggj4PQMEGGAQAf/JAIcZ4DwAFKlX9QEMnCOmzk/mgS1ytigy5x0wjDup'
    b'u2hljbptVeMZA3dMYGDxe+MYmDPKwOSRUQaSx5GXDFA1fhx5tyLg7EcXybtrRCfvk4/+mTzrvyBPeWPml5rmVoui'
    b'0c9sxVAq1G0FZVBmAFz9f714MQOLEK6RDlPGb1RwO0rzfTjkZjbUGbC/1IdNaLYxT9mMR5d4iEkYTNnMThp8aYgi'
    b'QLchDF35F0z/zHXrrJwVU6Po8ZU3FrIe4d0vc+QgnmC88TAAePLGNPi3xkJ9WE98+AZ92fm+CQM+Th9Lp0JpXoUC'
    b'6vyAWfANdNtSBOI9j0fXrra1KG88Au8ZWx5Pij+jaY2WGHnEIoUBrtQgimcQ+Zbk6MZkQ4a9TfoSfLAy10KfYfjv'
    b't958SNmG/Q2t5O92vxiXsWVh9FCjJWrwK2XbCoDnUacZGLivjGNSQa4VZaHOu2CeKHv1a9w++lPiI1dBiDNEzqm3'
    b'H8eJxTs4pqaZ73B6lIyZPr7+GeYZw8pTG4BB22l134lI+G5l0KPI9vMI6xjbBpP38K4Lv62VJfXqD4pLfcfF/NET'
    b'2abnEGMTu/QhqNCJf/mt1s/QxKBpakATozRZH9yHBq6LHNUP3RLkpYBa64HoGIaN9A6rO/tYKHwJi1cwFu+M01m0'
    b'sghVBS4XHWNXDIAxU8p7L6NuApX1byE5WbOy5ZEovuENxm0Ib0rorHX16b4ak4/q7l68jNAPbpivb45EjXiDJuJ1'
    b'ZeRMqoXOfF4XmEAv0synt1aX4dHnCLO5ndWFwFHDeTw1fXRW9BA/raVRGJLsijBUZlCeDLH8IXDErs2oLtgePbAE'
    b'NCpWt8zQFHvh1RkpcnGG3+sZYhT3q098MBYygCioMbWdJsJfMvzNhL85qe1STUn1bT+7tbi8nJtfImbWrK9YvqBk'
    b'vVssEksQkFdaUVy5NmdldUlRsau6cnlJTc09xVxJeenq0ooisbSyopCrkaqqS1YXLV9fyK2oLlpTKkJhWemKh5ZV'
    b'1tQUclIFthVLigu5gqLVqytTuOKSR4qkchHeV1cWl6RwBeWla0pq4L9Yuhpfl1WK+FZRKZZA/1Ul1RUl5REKnZVr'
    b'SrJKy0uEdZlcTWX5MqAK6h1Fq0uqi+aWS9VcdWXl6nsqSsXSonInIB//vhBHGQ/IBGomdCitkMSJTeYCKbZLAT9j'
    b'gGwQSEn12CCR13EodMhFDBfff8a5S0SxtGJFTfrUqekLK5cXlS+GBvdXoTxrOPcYY9g4nXMvr64sL88tLVmbXsOK'
    b'jsoKsai0AhiHAgDSL/ZwViIOFEX63KLlq1ZUV0oVxelCcalYWT2uGTZwl5cCPdxEoKukenlJhcj9j78CZGf6I5XV'
    b'qwvxaIszx6wqG1/POEp3L5tbWVkjzuC4KaiTl8Jt/wzPKl8/A/tb/hluuxwc5n3Gt8Av235hZVExdph8GbjtMnB3'
    b'VdHaisvAUQuLLwO/n7XmuPhL4Dn/Yb8sfEGu7bJw1y/uyb2sHEqrSy5HP8IvR7+jvKTocvRnSbCULoMfNLQcGbt0'
    b'XuZXFl9WzgCfIGdLfFzMJBNuYqOiTZNizLFx8ZfVnwl6MI6+y/0m6kHpipXiRfr+VXtnaU0NGAsG+3fa5xRVryj5'
    b'DvjvlVZXjef/X7VfXFIOmvbv489x6bC4f7P9fSXrxO9CD+rNd2mP+lT9HeYL9em74Ad9mrAevq09mKxR1WE/80X4'
    b'qIqwX+xF+KgqTJAnwEen/NL2o1PLfjEX4aNTeGn7nJLykqrKanEC/QDHKckpqlh1KVwfNzIBky/CR0V8KV+joryU'
    b'nlGRsd+kCPzWW5cWVy6XVoMhr1l60U1OX1Wyfhm485pbxXHMXeb3bf1XM+cyfRn4if8Rxb/THwj5zv3H6L+k8y3/'
    b'hMF7d+UV8h3wf/6TbV/fyXHP3b30yba73u7unnPbbbfdfZ1v1d0zvbfPhvKbUH4ztGjWnVB355HfVt3NcU2ZHJdx'
    b'N8DeqLwiGnB0zgWEd3Pf/77/ff/7/vf97/vf97/vf9//vvUXOTektWa3+svPNa2u/cBpTVM/7tA0+kQxnpG0+7rE'
    b'mdSgscQfXq4Rf0o5pZl92xGQfqS5jHgFybvvKFT/WIpT5N2rPzCQk1qA4MWhO6KgU61ZkZsObAXouLNo8o7bTe81'
    b'k/YOYUijmTN8LRJ+KII3RChHjijNrhPP4AiDSvPup7xRdleG1Kd4huTWjYrM9XqjFpcZyq7TAt7aIaBK+isQ0Yp0'
    b'7Ut/Fnv9pa7d9cmmKHu+Ufp92U+1Q4zw5/xcTpZ7uj/DuDBz7vSODLN7uIN7GC940Qa8dzEYTedbSWYsnZ9IPZYM'
    b'/OYoczJeBPh5Asm8igpmsugaknk1FawdmTcaSOb17GbIxSsMrcomlodmd+x69S802KX6ME9G1kSzS9Uh22nlXk0+'
    b'Y1ybolisxBnk91uVewPeM7Fr+VWn4zIU8QcB6gkCC59MuHGv35lgN1CAk8mkfbFSh0e05KgWGLu6QY6wFC77yKfN'
    b'rx+b/1k/NscDVH702DzA+/DcRn7MEsU3/IqlQiWzngYl0TTPYmvx9YggDLNdPsWulc1uxrNKcSZh75Ti7RU63+KX'
    b'8erKXewgaITdMAZlwVsXneoX+JWYfJ4dOKrsFox+3hlFTvJb29SZ7AseKrPLMCcjVQX8tja8GPJDbdx1RTVqENTS'
    b'P/2UpmE+2Ew8eF+KfZ2kvt/BToNW4qWDdpZ37YRNlXroLf0mlXg/uyQVVp7ah98PyHgpRt37ln5nBe/JQJM46gmr'
    b'mUhsG2nGEz5oD9VhnSRDARD7ILutsgna5D9wkaxVnyFZ24eQLJzpFPlVnT+hn9QGed/tONjZDn0SpiJiGaVmO02k'
    b'Phg3nnr61a5v2Kd0VOiznbYzYYnxka/GhofJu9Q9y9ZFnL0wGf/xJlO1+TNceKTdQz2H8Qb51ePu//f6TkuxN7AJ'
    b'4ecNz5e1SVJAvmBYc6cufxjyPlobpM5+5akRFNIfu5jsrkA6MvGik0uTzxrX3jxo0rXa6DWEB68BpKl435PhxbvK'
    b'jMqBQZbAly+Y+IY+/Wa0gX1UgSNxr3ApWA6xW0Ue/FqOYx/84ufQ4y8+h/WLz/1qCL9GwIvP0taBX7LjYyZ73sfu'
    b'143O1C+Q0qfO49TaZZwpcWVkQMD8CpdxmTHLuG8Z8RejI4oA7aeUXZf6MyI1tdyz1cBuB7BxdaWZ8L1NtYoT/8zJ'
    b'iD7qN2jIOfWlVjyfCONdJe8Hb7OzMyGkPg1Q/ZIn6OutB/D0hU7yHRfL8LNcpptJVpiNlE5cLvpEsc9KmLIMJrEW'
    b'ZmxxwY8tQnoL/ILWjJrbcG50mcFqcka0Nkqfclxn3ecmHLGY9SOW//4UWTgW1lmwUClykPRFy9hBEndlhyGXnSRF'
    b'qGcLbnU7u2klmGkSrPS7gI875EcTeRJeE0UnKU+puMycqvrYAaZb11KnKr+ir4r/xAmRLPrqCqozL6VrNGdTg4nc'
    b'3KJyqYRbXVIhzSstKq9cgbn3i7VzJVGsrOC4zGWl5aXieldRRUn5aP/K8pKFJWsmvOvIRnNRNTWu0uWrSqp1Qfzu'
    b'ExTEwS91QUwjQgAPGg8hw4Fx1/jacEEs0sjf6s5eeAyPHl9CNWP3dPEM6rdMBwMm9o3VJN6Hh0vyo6kcX0/Y5aB3'
    b'+bSGnezmOvtuFNa00Ikfj5JgZLETZ/fwMK3tzMRPP0Gcx0i7fDZ27QeK0GvryQBJx9+zJWfykUbrnwyDYbbU6nvE'
    b'SVTolluiOuaZelMUVzQ5CaX3suHxPiy1APGEFE/vyqtQpfFTrnHfkarPfaxppuNfaJp+k6culIqa7uvHb3g97Jrw'
    b'M1gmSi/7qjcR9WNbACG+IIMfhqfauh8/vUcwZQDiN3H+FkPdWbx4SXx9AJKukM9ES5Ppg8Zbrq07dxjoXnMT1XE4'
    b'EwkMxUYgMCBrTiTL2PVLMqI27Nfv44oW9jEVQNLfxAsnyrZTOOgbSCO/9/FZ7GOm1tuIZVOGPBIvfUKO2lpaz0TZ'
    b'O0We3kXnNJDX2Sf1GYOfK/N+NI1sUxkneFanCKeIL4wFHwLLolci7S7tL0HGFWOfUTYmvDYr9/8BebZ/Lg=='
))
