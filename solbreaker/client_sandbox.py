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
OUTPUT_SHA256 = 'e5d88c07d956c128d9713c53bb5cf69090459e30182ffabdb136e8787d3b504b'
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
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNqdOg1YFNe1s7ArC6KzJJiSlCTYkIbYJLDVvkg1EV1HSXTr6rKAwpoQwciPQGFGTRUDDotcbybhFSW+ryb1veZ7'
    b'9b3YFl+tMWk0/FgXU0xWpHGjJEUlZgybdJW8gD9x3jl3lj/re00efDt758y55+fec8/frMrBX/bDps/2GduXalK0'
    b'JplVVdM0h3pjKsfJR6KXXyw8cOWai3ziJO2mhuKpBs3naRGjlca0p37NkdYct3bO2pLjbnfxWS01bZb1Uw3kkyyn'
    b'UmUm7YqgygELHyOo2qMOrYu3CCrx8zHhSsFQzlMwAxDJ+5lOIFzErakGOYrCi7giwxoYVS/TGtIulYbLQ5pktLb0'
    b'W8lRawsRgooQLOIcIGRWpta4HRCIFwYJA6XhHk08RQVzqhDkt7fwB1qUgi9zVoa4ZKrJ3+e4mqMnHuE4Z01gOzAg'
    b'nnLQ0pTScZ+B7FgDQ+LJx+sfRLy+5zkmbqIeHFvPEPcR4u4g7k7i9hF3N3H7ibuHuHuJu4+4VeIOEHdQ2VlyTdOU'
    b'6lpGqQSuykYTfyCc7mBj+07F+CcjsHVQxlfl4LKcHIWVtFhbaqp2OuctsoLc5Gi/yZZ6TfqTzvudJ2GKUv3a10j0'
    b'aQDoYLoDaVAFxeYP7IqYhqxt00N8hXrtDzhQi25omlxVz63gPVuBjnwt6uAT8O31bICnHEjDC16vUBvJVmMTqgor'
    b'XO35SryHZny0S5mnyUNV62+j8z/aLQ/x6yPJDsQBjOIzcQqjAcqgUIZ2arPwlnA5EEedsWR+ooVkJMbyMcZEXUTK'
    b'VlYXSvMtlLUM3jMdJR2zQ+rPE2GDvCkPc1ymUw5E051B7adGzxneQ9Eazhn42i1oIEZydA2XwnFrVLzjtIbEy2AD'
    b'7bCOq5WtqwdBJq3phWBpOH2xBY2jTdn6UwQuIy6V2APaCepSqT1AbDOVrVvYA+2E8vJ8tkUDxDabBIsMaj4s9wqg'
    b'OJk/IAzMzl8/gcgNuEavokggNPEzsbXd14ERMy/yntNJnwSLPyoMaHRuiqdFirC2XPwVzKIcOaU0Os6+Ep7qk/qV'
    b'xuaXq8NSHWlSr+IakFs3KTLXUx22rMhQdK/mq64agBMl/Ql4t1JOaTyY/CrO+kNNu+PClrDUbKP0n0Xf106SVkDY'
    b'7eUyFjgf9qYZF8+d9/DRNLOTLrSQuZF0YWwaWRhL5k4iP42RL1jEcLo+Cc4FdUWTn045Ovd+A5l7JxUsZOndZO7U'
    b'gwY0sOF94C3zZpJPMmnFYiddkU7KY6kUT7It1B3Hx3Bwes1kRXoOqVi8Ut6WHsZxBjcfM29mu8t6DJzDtvio2jNi'
    b'xNwf55o18bK1pfZMjhsMWj/kpzLV334PD6DjB3AASbsciJc3xPLrwmhc9SzOKwRRDCkCBmgiSt0p2Bprl/WYVwho'
    b'zOZ6lLqPAUZcvcTeRwQ/dfUQv7oFnsFSFCi2CUrTL/C5oKprdaCJuQsqqErTHniiZupg5BGG24IPfo8PHht9EA4P'
    b'toXXwuLOdovhtv4BGO6wVc9KEK/2K9TlB5a8jn27YotQmt7G+V/fQL59/UeVpgsogl/tB4jS5B1kmE8B4DQCFk2A'
    b'u9v0SaRDPcqm9fa/RDqUpvcQZlfVfQCkdnXQT4QAf9igOHzVVyP5rXiI5KrAbP6llTAqdgWi5irzw3yA1P8hkN94'
    b'AzlJTRTcY9PnSCqCCn1koZEKAbLQDAuxMJostFChFw3Dry5i+OLj/GFYN2QRsf6H1Rsjfiw9tG2RWVnKAFOrN0a6'
    b'pe/KaorcHgXzAfcG3AAZWA4J1mZIafpSV/f614zcZCCRIJrk9oj+cLndLB+JD50TDAyhQ0JOws7D9uHeBGDBeVhw'
    b'CA898tV8aRJsNprBUaHH2h9hPSZfjVx3L2jwThQArV1KuoH4BwetXYODAJTbUuozTD3sGfFbu47On/TR/YODytww'
    b'IKzUcRcPcgxlUo/i6qF2/zLNN9bbEK9S4/t1IZepSD1O8mdyCcQCFHJFkav7jnM17Qn4EACXQFjtJBX842afyiRH'
    b'nar/XrDo9s1J4FkDZmIPqvuGYD0ukavKTo5L40wP/nWfEaSBXaFSwEnOaB8ABUbEm/YQRl3rMWsXSU8h9b2oxYmt'
    b'6WlbHfPbPV18bRsAgFMmLJjScOTe6vDU20V0KP+OlltpBBd9WWnILn8+PPUOSZU3x3Piubdw7dR9V9ledFOLp0uK'
    b'zKdTEpG0JZ9OwkHqCemt1HKzZAJXIrpVCV1+Fyk3E0cscViII5o4kohjBnEkEEc8ccQRxybi2EAaWpBIwxF23cWu'
    b'O9kV46i3ej+n/3mrm0Mjkm3MWelut7a0q7+7BxapbfkDTJ8B3uI2w8UVDRe7hU9q55PODfDL8dM+wGcf4ZPOwvAs'
    b'Dgf4pS188pZmkHHd5AF+pR8+Pvh09E/ms87A/G17MfolnRzgY+LccHEY4ZJu1qPJfGTrPQdBH7YK0pBhy+uj8zLo'
    b'smxiTIQQZaTOXPgyw2FRnehmOn5gS0TxVP4NX6tqgWFOBlzWBgb4aS0w+PV+PIhHQPpp71JnCogKYmZ3wPdLKCdg'
    b'bD3Ivt2tMH7pbTZefgI+IH12TjoMLsLHHhzgc08DhjMb7kCp5bZceO5cDBebA579BQYvt7DZua2wkcAwJudpuDjz'
    b'+4tJx4O+YfmmAe9pnwGBTpRuL5uRfRzlOaLLAUtpWQQzcz8EYA88/BAB+QA4EaK5vBugJxC6BrkxJiHFUIniEris'
    b'Ej3H4ME9iFXOQgbc2QA/ux8+vfx8ECNmAax/9rswyDSTIFkEqxNTioKvBjljfhI7Lh0jH7AgkES31KK5CN10Sz0b'
    b'+A9mAHna9AIaenSY5xixd4pWpapH22+qRi/aWJ1TA3i+LK2L2juVxvK7fRyMlUY8seSK1pVNBd+Kx5CKFHEwG776'
    b'943LM6bFo2V0JOp5Rjyx+2n97i8g1egSXZD0LSXt5AOl2nyPJzw1TLLJP0vkxMeKXf44RU458QtO2f6d166AGFU9'
    b'y7I0H/hkTpwCgUGRm+/82EAu1bRxwBEf6S4EBDo5zD1L2ZRolvuC2vrrekq667sYER9PxIgI1llChP3E3qw1fPlV'
    b'aTgcS5cfDnImdc4GyZYodv9SyKABMp8K+7WGFEh4Zwl+6Z/kjTM5MQVgpqdNr3MsNf8ebcCDSFx75KthUmz1wV/j'
    b'mRQnyhstnHjfxTr0PC1eW5wWAUi2DSMJjfLs0FMX8yCVpLYEIBNDXc0PttVcPYuJYt3P4SpXNXNs93lBaMbg7EfC'
    b'kKwRWwK4SK/Qg/cQmr1CL3MDcMTIaThrGHnm4znbrx7/ip2gi/OAHrXNJMIuhL46gMFwLwSxKAwNuy9eYyF67ywZ'
    b'XY8YR9oA603m4R7+EgQU9hL2BPUOGkJ6/4mml8MOHqYGDRyY8BqA/gsQVnuFTtBYMznu/jlYzk5k0MAfqH10O2yk'
    b'QxE6HdT+GhV2QvoFSxnNFCRXTvZGCg2QqhqlbqtGqnZbtZEHRNh+n22T6HorArXXosSJ/IHtj4qwcHLb9GXUvlfB'
    b'/GS/2n9Z02qq/JhoL5ceoEIPdTWkunqleBAs7u8YT6Cu7ZGu3cByorSdboyjwm7gMniK2OKmege9yFy8iy6Kp419'
    b'uuLyJQNZFC+fNaiJEIAHvZjViPdTW7wcDCO2+D1hmNLswZQmNMW+B5d68DoiYykChmILkZODFpgi91rkIQMg3XlM'
    b'bjWpxxmmGTGN8qINCYNeC47vG5k1oxom3Y912+i0CLUJpvFvfRpViZPSBr0JOCmiZtEGPBowt/XT8JpPOfDPkRs3'
    b'EG/NpzI8B2HvKQIb2qX5VntanguX3zcWxYy9NX9li7fwtcVAQt6YxPG1eUhMaAaH/ZgbhvxWB6P+EJhQGnVm0EXZ'
    b'YJeKcJDEJpLFI55eLbmC1nOQut4GIyTOcjhnthIqtBBnCrGJXttDBnY6ttrSwNf9u0936q92sEjTrvvb5Z188p/X'
    b'8wBv6mbwtgH+qVacMx8QfHzSn9Gp8sknGc6r3YzGU6cZ3OIs112v5cVONvcin3yC4b3o1+8B9TN0syIilzAdOocP'
    b'lr1Hfh+C5oeheHJaDx4xbrA2uM9F32vvHVYYMmVUeOMQkoWQEFNcziLXchhb6vaGggvG4U4MtfAAdRMgGsQsKkH3'
    b'LvZ/h62nGAE53pOQ6kEaxyepfFLHxI0bBr3lkP1I0R5N4uWNMzhR6o84+CgI2W86+J8oqzMB3FYktTc/eCK1tSJr'
    b'nOsArYDBWh91QcYFvgPl7qQCyBEjdI9xGt3DTmPnZeY0MMms+fSXvaxQ3ThD913Dnkv37ovvQO/+2T0Y999jMcYC'
    b'lZ+y9TqrDBuxMvS08LVoc8rLkbA2ywDKKkUhAORLh/sQYcN9iLRMrTGNtRnkIU38ybgeBPZKsrTG7iAWmnioqRBt'
    b'7UoVgtJkAFcH0Y0Dtx1txNWXpTcpqKsP4vlEILqGPe6fAs8gny2K1CQoUXz4vEXsRZ/s02PWm3eg24F4oVTtUgun'
    b'YMiYCOpB7NpAhFpry8Drel3uFVgPQXlVZM2AKKjT+QOzqLJJbwkoWeCaXLVq5hfolOrRKWVLk6nwApn/NbjgBvja'
    b'TYVqW2q71KEIO4tylReP3MAKX6hVmrCpoe66Ma5iz6SS2al2x2IO/LO7YTkXHJeHotbdpr7+OfhyyZxj6i2ealjp'
    b'1nqLW+KU2nDsYWBSODL1X9jUOWOnlg9P5UqGp6bA1NprI1NZFaGuZFONMFV97HOWc7ebmqfdZ4CE3a8uA0hqULqL'
    b'eEkbVKZ+9RqiXNVj3MW9N5DWcB6iVJmd5IY6mdHbH4/BLIFG0OchLQA336P+NYBxya/sxDYReHBZnQ6SrjfJQ0bp'
    b'DrI5HkBkArX3jsRQrbdmDq6t9DxdaEzH/sk6+Xkzv75cgYJM6NYkKEZ64biq7wHlLN6ywCgH4linZEEszYwb7p3o'
    b'eFDpe7PhhM2EwwzxVQ2yQkt6UJO6/+HMd8L0wp4MoVhtCZw69XaOM73/XZaIZ2U6awKJBuz79GHJ6bJgh+AVHBOl'
    b'hxWhUPtHkx0+hHj8DN4NV/V4P7afEEwZgHhNnLfFUHMVtSaeXgBJt8lXwqVJdKXxB/fUXMMcYd0DVKdhjyXAinEg'
    b'wJChEyk6ZFZaLxlSG4EBO03RLN4DZFYAD7yy40tkeghl5A+8MJPF29bpJHpLGmyJdIF8aG1pvRKW2iHy9HE6eyt5'
    b'2xMPZpvW/7ky/64kskNlmgTQiIUviSeIAw8Ci8LXoOwO7Q9+phVTn0k2UjO2WVg1hqZ3PgZNhdwFptf2Wcj0Ggwf'
    b'gemxAgzM7ZHPWQn2BGnL/1dWdIVJj8mHHSyAm8l/YWbbb0ll3xLW5ltwJL9VzZK02+W3XmCDKLQn98X3vhw9Ncg+'
    b'n7GPQva2YfZpBz4ew/53AcY+Uv7CTJoxSoemO53qA2zy8TsBzz6khn/Gqr92tSqA+z3EuhpLh1Ib+nTJuggbrYDE'
    b'9/DHhhzYHd1wwKWisUjR1G6hVbGsyRKH7vMr8KtheJqjiaCmdkhVNY9nLHBaJalmszFzyRM2qYSuNMvXvgeyXVvC'
    b'e6rCMIIHrV3q9X4UGaI7QEIla6qF9+QiQpoRnyzFYf0QqOg5xtfa8A6SvAxshPG1M3RCRZzT5Hh9HSzFxe8xMSyp'
    b'rVJJSIZ8kGHBsrkLpRzA1EvlupCh8bUDcD+8V7znE7jzbsHtQDS6wBzaEsJqFQrasVqFr32bpSBBdRokrxd/Z0CW'
    b'5tRWfstmGOpseU8FjjcbnfOWZPOeAkNIY2LvUyOQfTuyF0fZh/Oe1SzFMQP9XES396XaVd7DshshkOoVYz0aXzsX'
    b'uS00pgoB3vMjA5MSKU1jMwKp7/O1mPPTRwePpnlQ1FfhbpYN1tQMGABEC7vzK9sms3j7cD4ZfBMzPdV/GbO3C+ZK'
    b'fJww9jG2idU39cdRlVNXD9o2tRjEO8mlEZTWixPlsxFya5T6cx2N5+WX4dFXtqowvlYZzUSB2m1IrRjQ+jccxi4Q'
    b'sVnIZRJsPTfhhAp5aM05PJORbSQo9/L41aqGyb0JOLqfg90VTQdbQJXWL0w1F/A+8oNIfD9QuQnSif7vUzd2XUhV'
    b'LK0KEruFuONM8zr3GcHTkCrziLOBAUtJhsOBk7rNENSdinz3e/uMmg/2ya8+fREOIDaD141YplSMZ6OadUBeYCUJ'
    b'xFQoh0YaISQ9lqRbSHo0STeSdDNJTyLpM0h6AkmPJ+lxJH0TSd9A6llTpZ41VepZU6WeNVXqt49rpVD39WN6rHJC'
    b'1KRV0Urd8Q/2GbMgV1ie49FEF82ykKDq/Vy3JlGAm1UX2cmWGe1WunU2Ononpk/fUZr8f9sSDjpMommYaUygaVC9'
    b'oQOGnCvIImTGpfFxHt3O59HoORoh+1A7PwHHKFnQ88BimRpW1RogDYLIexlWK033fQ+Qo3DTo4JMrbQOxUCPRN59'
    b'sE2+mr8ukqF+HkQ2EJ3cbdGhkKTWMTYPTdHjMDiZKguxq1SKw8aNEM2aaeBwIBjGQ3SmQgJ2goVECKzqjz5hOsNx'
    b'/O2nTIZnSfvwocJDr86yGSUntZnhRNG0pFSDtADOE+VSbUkswS1EJwiALCMFW0yiEcS5mNjS6UIzsTnoBJppJLYM'
    b'4syGk0ZsuXjsvwY+xJZIXd3U7idSHJV6mdFBwplIqwJEioVK2FT+tmygUh+xJ5Aq8JgqpDfEFS8fSdBtMKS4MhEV'
    b'nx7LWomgKWw1sQdRfSGgrugLOeruC0yzxFFLvJO6grMcRimGTk5NT4LiuYs4ktRX0LVUgXMOKLLPXWsAntQOkkWv'
    b'yNG53tyKIDecahQTofl2FCEXygKe2F/DxHyXevo8499qqm7bAgX0btJB7DvVhRd0p/01Glk7FV6DVR0W7Hbec4Ft'
    b'Ol/7Md7bQPOdvKeLFQYPYYPTi8ODCejQwZ29iXcRyj/P/k1leKpPnKz8c/lpsFIf7/klUu9QpD3qi8APbLQV7Psu'
    b'2CveIuxhCZDcwTwKlnReuTfktYmMaYci+Egjlk7kKkuUKALpLxhkMaRLuYmxqvlTNGfIgHdDMa75gKxvOK8CO3P1'
    b'sbwqiJkVuaqeuYB1SMqYqqPNyGHDcBes1B2smt895mFoe+EA0Sqz+h+RuMBLwPGp9eeQaTSeoSwTFyvhK1VNmiZX'
    b'RXNiIq1jzj5tJk3P9RwTYxX56S9gMQxSVJGhpj3B4eNGekptZk59EsiahmKG07qlit3M3q/QpWZqxc7ac0YC+eaU'
    b'c5jm+SFdCD65h1uG/gOPC77SqOpj1ZVc5YdIwfpx+Fpyc+j0TJBEKPzkw/gGDHv1vZJJqertf4DW7dLP9aVZC6Kl'
    b'ANAJAJ0f1G1n7YBV608rh5tZ0dqtuHogh4hr5BQyBSqupeQEcXVjqdMN4EfeBRQ/VltF4VnLQKgWwgizlntw+a07'
    b'Z8+YsQwyxrAyaBOxV5MBdcZZ3QHUmsrT/gKZKL6fhOJF/eV5dmhix7gDOGHVYJMi7Fg1ZYjjNg2eQt3ACqdRE30d'
    b'kwxXLVjpNmalHWCl25iVdvCeF8JY7wHYs1IM2InyH1kLTLyXt+gWyiyS6uYo9ELVB2vp649FEX9/XjdtsFf18YvM'
    b'ttEIFeEgs0NI/u19YIEWKGIWg8m60B+0yEfivOylrW7vyE3+o75JtTdYtlCrrjjP+hdL0uGSNZ+V3blZM+GyZDYs'
    b'FAAXLoabhWmKsBcM2IUFOR55cGjYBgDjCPUSwL8tTjSryWqo+20p2AtrkfrHnawTbHE3e+WRlwDycOSCB9J+StEe'
    b'eM+sUOo2HUOR78EOJV2TrzzDb70Pezf4GqBTfwWAjWxsqC89xie/2IstkTo80EDr9T06Tfk1JJh0HTsIqIILLsuH'
    b'EGEvQ1h+Eiic4pNuwPBrNkxu6mOE3mDPpyFuk6q35oFj7hcD/MpTAKtCQkA21ws3h3sYwspWPul9ALfBpw5fRfLJ'
    b'XtauORzU2zpD8Jyyt8OA8Rd48FucyL/Robfnsa1St5+hTrvCJ2Nf34ugL3X22DanzUyf44zsb0NkP2Mtm+XYjWcr'
    b'CyZJ2GqTRvbbBXmX3oP/1Wv69y/0xYn5Pa5Bf5hVG7FszScfQivhxO+DWR1kZlUf8of2DuruHPGEN87i7saNa6mA'
    b'RX8CcVhd0cusegp1gPcWJ1NXdNDgSIOyEb7mD//EotOIp7J0EjuVM4i9BU7lqx/B4XIdwQBhVi+dxULjiNI0DV+U'
    b'eanQov6mD0EdMFSafjyEXQYG8DPAIgRQBuhkgBwE/IwBfAxQioBCBuhmgOcBoEg97IxA8MMW/KUs8DX2FkXmqi8a'
    b'xrzZG/WiRt13qhOZAr+JHqvARz1jFEgZVqBhRIHA+VHxfonSnDk/Kh6+EFTfPT8qXmtIvAPn/148yz8QTzk042+a'
    b'5lTzwjGORGCqFOi0gDEoKQBXZ/Rg8YpDSMfIUVPav6kQVpTGE8iyjrG6Av6VehCFphuzlDp81YkvPQmDKXXsjYUn'
    b'CUn46A6EYaj+gtmfuWaDhbNgoxUjunJoMZsRbH6DIyfwTcihpwHAk0MPwde6aOrB58SDdzCXves3YULH6bx0KZTG'
    b'L3CB3v4ra2tspDtyEYg/O9m8fi1U/IdWw33athfi5CuaVh8dIQ9FS0GAK5VI4hUkvi0+vD7ekJbaJv0NYqwyL5q+'
    b'wugvsTx4UtmB8w2t5H0om6LSti0OH6iPDuu/rOx4FuBZ1G4GBX5SxLFVQa0VZbGuu2Aev/bqf2MZ522MCv04hdgD'
    b'5Jradho3NgA5p6lhxnucngVjG5GvfYVFvqDy8g1Q0PqVWn82lJ5bGDQX9FFFhB0dKUcHYf+ghN7RyjqGtSfEXM8Z'
    b'MXv4DW7DbqTYwH6GIqgwiX/j3dbP0MWga9qKLkZpsKw8iA7uGPlQf3kXI2MJqXVB9gtsQ7ODakkvS3VvUvE2pmJz'
    b'pK6ihWWgKmjZ8yH7SQIoZkr46A20TSz032Wl8Mx0eSiM33qIaRvAn1Toqv1Lrx6LsbOp3tODP17ogySRr20MZYX4'
    b'm55QOJZRM6kKJvNZx8AFVqPMfHJrRRG+Qh1iPrejwg0abb2Ob183zwwf4B9qqRcGpFRFGCgyKC8Z2U+RQCP2Qx51'
    b'AELoiuVgUZG6ZwZUnIU/5pFCP+XhD7gGmMR96sq/joR/WApqTGynsfCJh88M+MxObJcqCyqm//CR/JISbmGBOLfy'
    b'udJViwqec4p5YgECsgpL88vWZ6ypKMjLd1SUrSqorHwinysoKVxbWJonFpaVurlKqbyiYG3equfc3LMVeesKRRhI'
    b'pYghFuS7uZy8tWvLErj8gtV5UokI92vL8gsSuJySwnUFlfAtFq7F22fKRLwrLRMLKt1ccUFFaUFJSC572bqCBYUl'
    b'BcKGuVxlWckzIAs8t+WtLajIm1ciVXAVZWVrnygtFAvzSuxAfOz9YuQyFjAXpBk3obBUEsejzANRrDcDfsgA6bAM'
    b'BRUjTEK3Y0jokFEKo/c/5JwFolhY+mxl8rRpyYvLVuWVLAOEJeW4ipWcc0QxRE7mnKsqykpKMgsL1idXsqGtrFTM'
    b'KywFxWEAgOTRGfYypIFLkTwvb1XxsxVlUml+spBfKJZVjEFDBGdJIcjDjQc6CipWFZSK3OqGm/9zUIeHV5dVrHXj'
    b'my7OHFFcxI35Y2okO5+ZV1ZWKaZw3GQ0v5vh1r+HLyh5LgXnR/893HorOGx2yv8CvyX+4rK8fJww6RZw6y3gzvK8'
    b'9aW3gKPp5d8CvoRhc9zEm+AZP0q9JXxRpvWWcMfPnsi85ToUVhTcSn6E30p+W0lB3q3kXyDB+bkFfTDLElRseF8m'
    b'RkVMMCGiISzcNCHCHBl1i/0dQ/9Wf+P3sfDZNeIo/X+Eby+srIQTzmDfBD8jr+LZgm9B/0lpbfnYdfhH+MsKSsBS'
    b'vjn9DIcOi/qG+D8p2CB+G3lw378NPtpDxbfYL7SHb0L/5j/wI8Omwf7MITyAD5sA+4schQ9v9bj1Avjwlt6MP7x1'
    b'7C9iFD68RTfjZxSUFJSXVYjjzhvAcckz8kqLb4brfEMLPGkUPryEN+s1vFTj5Pnf/h55JDe/bJW0FrxrZe5o7Hq4'
    b'uOC5ZyCyVj4ijlHuW8xfyzz+w8+A8/4/SXyT+SDI/19+nLwac6/qOWW3yT+G74Uvtf33Yxy3e07uS22PH+/snD19'
    b'+vQ593qK58yofnQWjN+B8TuBpTMfg2ePnfpV+RyOa5jLcWlzAHao7LZwoNExD1jPGSvH/wDOL4mv'
))
