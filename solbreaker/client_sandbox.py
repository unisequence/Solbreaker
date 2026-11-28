"""Version-pinned sandbox controls and rendering on the prepared PE32 client."""
import base64
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

from . import client_patch, client_recon

INPUT_SHA256 = '2dc4ab995f04313d93b998b366067538b035d45a696b4ced046de356ca7b5a3d'
PREVIOUS_MINIMAP_SHA256 = '583e6cc45da9df9972443a1043058ceb7a6d83f9d7d5104e82bea6cb183a1e38'
PREVIOUS_SANDBOX_SHA256 = '88f2855c5f1d6a57c4d6d8f056f1cb5e2ad034cc3290690a2a1147431204f713'
PREVIOUS_ROOM_DIFFICULTY_SHA256 = '13c85fd8d0dc97086c7c608365e22d71e9e381eee6e5e11e8f43fa56da842884'
PREVIOUS_CONTROLS_SHA256 = 'fdd5cfa3435b194df7dd3c5c93a9c8038ddbaa23e86fbbb090725f9d2c93a1d3'
PREVIOUS_SETTINGS_BACK_SHA256 = '2bceb98941ea41603e8daf8becb8ff0d6b4e37faaa3a911a2b4400c9a14fb4e0'
PREVIOUS_DIRECT_HANGAR_SHA256 = '898900f7de0473121ac6b84dd66924b5d0bc15e106be75a70fa174c3044f5199'
OUTPUT_SHA256 = 'd8964f5205bccfc42c10c488a8af9c5118be22ef4a26be6211e9135cebd2d3c6'
VA = 0x4B1EA00
RAW = 0x466E600
SECTION_USED = 0x2A00
# The sandbox occupies the final PE section, whose size is updated on patch.
SECTION_LIMIT = 0x10000
MAPS_OFFSET = 0x3000
BOSS_EXIT_OFFSET = 0x3900
CALLS = (
    (0x1C08309, 0xCE3490, 'sandbox_controls_page'),
    (0x1BC21D6, 0x15EAF10, 'sandbox_battle_fov'),
    (0x82C50B, 0x7890D0, 'sandbox_marker_visible'),
    (0xB7E690, 0xB84750, 'sandbox_zoom_changed'),
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
    (0x1E2303E, 0x1E111C0, 'sandbox_compute_marker'),
    (0x7BC3A3, 0xD54070, 'sandbox_draw'),
    (0xAB4D6A, 0xABA090, 'sandbox_mouse_hook'),
    (0x1C08FB0, 0x1C0C700, 'sandbox_binding_menu'),
    (0x1C33B6B, 0x1216C00, 'sandbox_binding_caption'),
    (0x1C1A825, 0x1216BE0, 'sandbox_binding_name'),
    (0x1C1A89E, 0x1216BE0, 'sandbox_binding_name'),
    (0x1C1A851, 0x123C830, 'sandbox_set_binding'),
    (0x1C1A939, 0x123C830, 'sandbox_set_binding'),
    (0x1C27475, 0x1232AB0, 'sandbox_reset_bindings'),
    (0x1232A15, 0x123DD70, 'sandbox_controls_version'),
    (0x14B8C28, 0x1232A20, 'sandbox_migrate_controls'),
    (0x14B8C46, 0x123DD70, 'sandbox_controls_version'),
    (0x1DB5AF7, 0x1DA4350, 'sandbox_minimap_extent'),
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
    (0x3847AE0, 0x1D8E8F0, 'sandbox_training_roster_added'),
    (0x385E1B0, 0x1E98010, 'room_header_description'),
    (0x385E0A0, 0x1E98000, 'room_load_description'),
    (0x385E0D0, 0x1E8A240, 'room_get_description'),
    (0x3801FC4 + 0xEC, 0x183BC70, 'sandbox_move'),
    (0x3801FC4 + 0xF0, 0x1845DD0, 'sandbox_stop'),
)
INLINE_JUMPS = (
    (0x1237308, bytes.fromhex('807da300c745e800000000'), 'sandbox_zoom_migration'),
    (0xAF5450, bytes.fromhex('8b338b8e94000000'), 'sandbox_particle_lifetime'),
    (0x1BCA078, bytes.fromhex('0f54c80f54d0f30f58d1'), 'sandbox_spread_ring'),
    (0x4B1DE64, bytes.fromhex('837d0c7f7418'), 'sandbox_setup_reply'),
)
PREVIOUS_ENTRIES = {
    PREVIOUS_MINIMAP_SHA256: {
        'sandbox_controls_page': 0x4b28faf,
        'sandbox_battle_fov': 0x4b28c74,
        'sandbox_marker_visible': 0x4b28cac,
        'sandbox_zoom_changed': 0x4b29194,
        'sandbox_particle_update_queue': 0x4b27d3a,
        'sandbox_tracer_update': 0x4b27993,
        'sandbox_tracer_position': 0x4b27a08,
        'sandbox_scene_shader_clock': 0x4b27db8,
        'sandbox_load_keyboard': 0x4b27594,
        'sandbox_save_keyboard': 0x4b275d9,
        'boss_stock_panel': 0x4b240f7,
        'boss_screen_root': 0x4b2409d,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2057b,
        'sandbox_graphics_page': 0x4b20e75,
        'sandbox_blur_material': 0x4b28d04,
        'sandbox_scene_graded_blit': 0x4b28f43,
        'sandbox_scene_blit': 0x4b28ed7,
        'sandbox_refresh_score': 0x4b200ed,
        'sandbox_message': 0x4b27e47,
        'sandbox_reset': 0x4b287c3,
        'sandbox_frame': 0x4b201f1,
        'sandbox_compute_marker': 0x4b28adc,
        'sandbox_draw': 0x4b205dd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b276bc,
        'sandbox_binding_caption': 0x4b1ff3c,
        'sandbox_binding_name': 0x4b1ff01,
        'sandbox_set_binding': 0x4b1ffcd,
        'sandbox_reset_bindings': 0x4b1ff77,
        'sandbox_controls_version': 0x4b27657,
        'sandbox_migrate_controls': 0x4b27608,
        'sandbox_scene_particles': 0x4b27b51,
        'sandbox_open_replay_folder': 0x4b27801,
        'sandbox_scene_animation': 0x4b27af1,
        'sandbox_scene_wind': 0x4b27b09,
        'sandbox_scene_wave': 0x4b27b21,
        'sandbox_scene_trees': 0x4b27b39,
        'sandbox_notice_attach': 0x4b2016c,
        'sandbox_notice_detach': 0x4b201ab,
        'sandbox_project_markers': 0x4b2041d,
        'sandbox_gun_marker': 0x4b28896,
        'sandbox_blur_slider': 0x4b20f58,
        'room_header_description': 0x4b20b81,
        'room_load_description': 0x4b20a56,
        'room_get_description': 0x4b20c97,
        'sandbox_move': 0x4b2029e,
        'sandbox_stop': 0x4b203a7,
        'sandbox_zoom_migration': 0x4b25114,
        'sandbox_particle_lifetime': 0x4b25080,
        'sandbox_spread_ring': 0x4b250b6,
        'sandbox_setup_reply': 0x4b250d2,
    },
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
    'd27f01710bd265d9e3596e07052c2e5836ab0dfe9a5856012881c79afdc2a3e6': {
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
    },
    '1d2821be53d48e38cd6d7043a4b8427d8015354011856a9305662f46f47d85b5': {
        'sandbox_particle_update_queue': 0x4b242db,
        'sandbox_tracer_update': 0x4b23f34,
        'sandbox_tracer_position': 0x4b23fa9,
        'sandbox_scene_shader_clock': 0x4b24359,
        'sandbox_load_keyboard': 0x4b23bef,
        'sandbox_save_keyboard': 0x4b23c34,
        'boss_stock_panel': 0x4b22d6a,
        'boss_screen_root': 0x4b22d10,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203c2,
        'sandbox_graphics_page': 0x4b20b6e,
        'sandbox_blur_material': 0x4b24c86,
        'sandbox_scene_graded_blit': 0x4b24ec5,
        'sandbox_scene_blit': 0x4b24e59,
        'sandbox_refresh_score': 0x4b1ff29,
        'sandbox_message': 0x4b243e8,
        'sandbox_reset': 0x4b24a4f,
        'sandbox_frame': 0x4b2002d,
        'sandbox_draw': 0x4b20424,
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
        'sandbox_project_markers': 0x4b20259,
        'sandbox_gun_marker': 0x4b24bef,
        'sandbox_blur_slider': 0x4b20c51,
        'room_header_description': 0x4b20953,
        'room_load_description': 0x4b20892,
        'room_get_description': 0x4b209f2,
        'sandbox_move': 0x4b200da,
        'sandbox_stop': 0x4b201e3,
        'sandbox_particle_lifetime': 0x4b22f45,
    },
    '2b2dee319d20bbad7a33ab3b1d77c26837b6615524d1c9db1cabb620bb022143': {
        'sandbox_particle_update_queue': 0x4b242db,
        'sandbox_tracer_update': 0x4b23f34,
        'sandbox_tracer_position': 0x4b23fa9,
        'sandbox_scene_shader_clock': 0x4b24359,
        'sandbox_load_keyboard': 0x4b23bef,
        'sandbox_save_keyboard': 0x4b23c34,
        'boss_stock_panel': 0x4b22d6a,
        'boss_screen_root': 0x4b22d10,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203c8,
        'sandbox_graphics_page': 0x4b20b74,
        'sandbox_blur_material': 0x4b24da8,
        'sandbox_scene_graded_blit': 0x4b24fe7,
        'sandbox_scene_blit': 0x4b24f7b,
        'sandbox_refresh_score': 0x4b1ff2f,
        'sandbox_message': 0x4b243e8,
        'sandbox_reset': 0x4b24a4f,
        'sandbox_frame': 0x4b20033,
        'sandbox_compute_marker': 0x4b24d17,
        'sandbox_draw': 0x4b2042a,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23c63,
        'sandbox_binding_caption': 0x4b1fd7e,
        'sandbox_binding_name': 0x4b1fd43,
        'sandbox_set_binding': 0x4b1fe0f,
        'sandbox_reset_bindings': 0x4b1fdb9,
        'sandbox_scene_particles': 0x4b240f2,
        'sandbox_open_replay_folder': 0x4b23da2,
        'sandbox_scene_animation': 0x4b24092,
        'sandbox_scene_wind': 0x4b240aa,
        'sandbox_scene_wave': 0x4b240c2,
        'sandbox_scene_trees': 0x4b240da,
        'sandbox_notice_attach': 0x4b1ffae,
        'sandbox_notice_detach': 0x4b1ffed,
        'sandbox_project_markers': 0x4b2025f,
        'sandbox_gun_marker': 0x4b24bf5,
        'sandbox_blur_slider': 0x4b20c57,
        'room_header_description': 0x4b20959,
        'room_load_description': 0x4b20898,
        'room_get_description': 0x4b209f8,
        'sandbox_move': 0x4b200e0,
        'sandbox_stop': 0x4b201e9,
        'sandbox_particle_lifetime': 0x4b22f45,
    },
    '947b7818e62bd00c36062c4c0ca2164bce8fb5698525e78960d25ca48dbac910': {
        'sandbox_particle_update_queue': 0x4b2440e,
        'sandbox_tracer_update': 0x4b24067,
        'sandbox_tracer_position': 0x4b240dc,
        'sandbox_scene_shader_clock': 0x4b2448c,
        'sandbox_load_keyboard': 0x4b23d22,
        'sandbox_save_keyboard': 0x4b23d67,
        'boss_stock_panel': 0x4b22d6a,
        'boss_screen_root': 0x4b22d10,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203c8,
        'sandbox_graphics_page': 0x4b20b74,
        'sandbox_blur_material': 0x4b25106,
        'sandbox_scene_graded_blit': 0x4b25345,
        'sandbox_scene_blit': 0x4b252d9,
        'sandbox_refresh_score': 0x4b1ff2f,
        'sandbox_message': 0x4b2451b,
        'sandbox_reset': 0x4b24b82,
        'sandbox_frame': 0x4b20033,
        'sandbox_compute_marker': 0x4b24f6e,
        'sandbox_draw': 0x4b2042a,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23d96,
        'sandbox_binding_caption': 0x4b1fd7e,
        'sandbox_binding_name': 0x4b1fd43,
        'sandbox_set_binding': 0x4b1fe0f,
        'sandbox_reset_bindings': 0x4b1fdb9,
        'sandbox_scene_particles': 0x4b24225,
        'sandbox_open_replay_folder': 0x4b23ed5,
        'sandbox_scene_animation': 0x4b241c5,
        'sandbox_scene_wind': 0x4b241dd,
        'sandbox_scene_wave': 0x4b241f5,
        'sandbox_scene_trees': 0x4b2420d,
        'sandbox_notice_attach': 0x4b1ffae,
        'sandbox_notice_detach': 0x4b1ffed,
        'sandbox_project_markers': 0x4b2025f,
        'sandbox_gun_marker': 0x4b24d28,
        'sandbox_blur_slider': 0x4b20c57,
        'room_header_description': 0x4b20959,
        'room_load_description': 0x4b20898,
        'room_get_description': 0x4b209f8,
        'sandbox_move': 0x4b200e0,
        'sandbox_stop': 0x4b201e9,
        'sandbox_particle_lifetime': 0x4b22f45,
        'sandbox_spread_ring': 0x4b22f7b,
    },
    '1bf4431d6bfb2843195f28638035a9b44f670e9ab1df8562bdf8a5e6d43f0063': {
        'sandbox_particle_update_queue': 0x4b2440e,
        'sandbox_tracer_update': 0x4b24067,
        'sandbox_tracer_position': 0x4b240dc,
        'sandbox_scene_shader_clock': 0x4b2448c,
        'sandbox_load_keyboard': 0x4b23d22,
        'sandbox_save_keyboard': 0x4b23d67,
        'boss_stock_panel': 0x4b22d6a,
        'boss_screen_root': 0x4b22d10,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203c8,
        'sandbox_graphics_page': 0x4b20b74,
        'sandbox_blur_material': 0x4b25106,
        'sandbox_scene_graded_blit': 0x4b25345,
        'sandbox_scene_blit': 0x4b252d9,
        'sandbox_refresh_score': 0x4b1ff2f,
        'sandbox_message': 0x4b2451b,
        'sandbox_reset': 0x4b24b82,
        'sandbox_frame': 0x4b20033,
        'sandbox_compute_marker': 0x4b24f6e,
        'sandbox_draw': 0x4b2042a,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23d96,
        'sandbox_binding_caption': 0x4b1fd7e,
        'sandbox_binding_name': 0x4b1fd43,
        'sandbox_set_binding': 0x4b1fe0f,
        'sandbox_reset_bindings': 0x4b1fdb9,
        'sandbox_scene_particles': 0x4b24225,
        'sandbox_open_replay_folder': 0x4b23ed5,
        'sandbox_scene_animation': 0x4b241c5,
        'sandbox_scene_wind': 0x4b241dd,
        'sandbox_scene_wave': 0x4b241f5,
        'sandbox_scene_trees': 0x4b2420d,
        'sandbox_notice_attach': 0x4b1ffae,
        'sandbox_notice_detach': 0x4b1ffed,
        'sandbox_project_markers': 0x4b2025f,
        'sandbox_gun_marker': 0x4b24d28,
        'sandbox_blur_slider': 0x4b20c57,
        'room_header_description': 0x4b20959,
        'room_load_description': 0x4b20898,
        'room_get_description': 0x4b209f8,
        'sandbox_move': 0x4b200e0,
        'sandbox_stop': 0x4b201e9,
        'sandbox_particle_lifetime': 0x4b22f45,
        'sandbox_spread_ring': 0x4b22f7b,
    },
    'd60c490960c7d3249b5795714374dc90d111abac91d8f60fdb43b566fc422335': {
        'sandbox_particle_update_queue': 0x4b244c2,
        'sandbox_tracer_update': 0x4b2411b,
        'sandbox_tracer_position': 0x4b24190,
        'sandbox_scene_shader_clock': 0x4b24540,
        'sandbox_load_keyboard': 0x4b23d22,
        'sandbox_save_keyboard': 0x4b23d67,
        'boss_stock_panel': 0x4b22d6a,
        'boss_screen_root': 0x4b22d10,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b203c8,
        'sandbox_graphics_page': 0x4b20b74,
        'sandbox_blur_material': 0x4b251ba,
        'sandbox_scene_graded_blit': 0x4b253f9,
        'sandbox_scene_blit': 0x4b2538d,
        'sandbox_refresh_score': 0x4b1ff2f,
        'sandbox_message': 0x4b245cf,
        'sandbox_reset': 0x4b24c36,
        'sandbox_frame': 0x4b20033,
        'sandbox_compute_marker': 0x4b25022,
        'sandbox_draw': 0x4b2042a,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b23e4a,
        'sandbox_binding_caption': 0x4b1fd7e,
        'sandbox_binding_name': 0x4b1fd43,
        'sandbox_set_binding': 0x4b1fe0f,
        'sandbox_reset_bindings': 0x4b1fdb9,
        'sandbox_controls_version': 0x4b23de5,
        'sandbox_migrate_controls': 0x4b23d96,
        'sandbox_scene_particles': 0x4b242d9,
        'sandbox_open_replay_folder': 0x4b23f89,
        'sandbox_scene_animation': 0x4b24279,
        'sandbox_scene_wind': 0x4b24291,
        'sandbox_scene_wave': 0x4b242a9,
        'sandbox_scene_trees': 0x4b242c1,
        'sandbox_notice_attach': 0x4b1ffae,
        'sandbox_notice_detach': 0x4b1ffed,
        'sandbox_project_markers': 0x4b2025f,
        'sandbox_gun_marker': 0x4b24ddc,
        'sandbox_blur_slider': 0x4b20c57,
        'room_header_description': 0x4b20959,
        'room_load_description': 0x4b20898,
        'room_get_description': 0x4b209f8,
        'sandbox_move': 0x4b200e0,
        'sandbox_stop': 0x4b201e9,
        'sandbox_particle_lifetime': 0x4b22f45,
        'sandbox_spread_ring': 0x4b22f7b,
    },
    'bc2ee600217ba0d9117ca6c2d56c736bb68c45b3d0f3d800aa992a89a39cded8': {
        'sandbox_particle_update_queue': 0x4b24882,
        'sandbox_tracer_update': 0x4b244db,
        'sandbox_tracer_position': 0x4b24550,
        'sandbox_scene_shader_clock': 0x4b24900,
        'sandbox_load_keyboard': 0x4b240e2,
        'sandbox_save_keyboard': 0x4b24127,
        'boss_stock_panel': 0x4b22d6a,
        'boss_screen_root': 0x4b22d10,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20407,
        'sandbox_graphics_page': 0x4b20bb3,
        'sandbox_blur_material': 0x4b25587,
        'sandbox_scene_graded_blit': 0x4b257c6,
        'sandbox_scene_blit': 0x4b2575a,
        'sandbox_refresh_score': 0x4b1ff6e,
        'sandbox_message': 0x4b2498f,
        'sandbox_reset': 0x4b25003,
        'sandbox_frame': 0x4b20072,
        'sandbox_compute_marker': 0x4b253ef,
        'sandbox_draw': 0x4b20469,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b2420a,
        'sandbox_binding_caption': 0x4b1fdbd,
        'sandbox_binding_name': 0x4b1fd82,
        'sandbox_set_binding': 0x4b1fe4e,
        'sandbox_reset_bindings': 0x4b1fdf8,
        'sandbox_controls_version': 0x4b241a5,
        'sandbox_migrate_controls': 0x4b24156,
        'sandbox_scene_particles': 0x4b24699,
        'sandbox_open_replay_folder': 0x4b24349,
        'sandbox_scene_animation': 0x4b24639,
        'sandbox_scene_wind': 0x4b24651,
        'sandbox_scene_wave': 0x4b24669,
        'sandbox_scene_trees': 0x4b24681,
        'sandbox_notice_attach': 0x4b1ffed,
        'sandbox_notice_detach': 0x4b2002c,
        'sandbox_project_markers': 0x4b2029e,
        'sandbox_gun_marker': 0x4b251a9,
        'sandbox_blur_slider': 0x4b20c96,
        'room_header_description': 0x4b20998,
        'room_load_description': 0x4b208d7,
        'room_get_description': 0x4b20a37,
        'sandbox_move': 0x4b2011f,
        'sandbox_stop': 0x4b20228,
        'sandbox_particle_lifetime': 0x4b23305,
        'sandbox_spread_ring': 0x4b2333b,
    },
    '522e741af622f4dd23670dc7300fd2169d79a61cf202147c9072b73feccf2980': {
        'sandbox_particle_update_queue': 0x4b2590c,
        'sandbox_tracer_update': 0x4b25565,
        'sandbox_tracer_position': 0x4b255da,
        'sandbox_scene_shader_clock': 0x4b2598a,
        'sandbox_load_keyboard': 0x4b2516c,
        'sandbox_save_keyboard': 0x4b251b1,
        'boss_stock_panel': 0x4b23930,
        'boss_screen_root': 0x4b238d6,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2039e,
        'sandbox_graphics_page': 0x4b20b61,
        'sandbox_blur_material': 0x4b2664a,
        'sandbox_scene_graded_blit': 0x4b26889,
        'sandbox_scene_blit': 0x4b2681d,
        'sandbox_refresh_score': 0x4b1ff05,
        'sandbox_message': 0x4b25a19,
        'sandbox_reset': 0x4b260b4,
        'sandbox_frame': 0x4b20009,
        'sandbox_compute_marker': 0x4b264b2,
        'sandbox_draw': 0x4b20400,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b25294,
        'sandbox_binding_caption': 0x4b1fd54,
        'sandbox_binding_name': 0x4b1fd19,
        'sandbox_set_binding': 0x4b1fde5,
        'sandbox_reset_bindings': 0x4b1fd8f,
        'sandbox_controls_version': 0x4b2522f,
        'sandbox_migrate_controls': 0x4b251e0,
        'sandbox_scene_particles': 0x4b25723,
        'sandbox_open_replay_folder': 0x4b253d3,
        'sandbox_scene_animation': 0x4b256c3,
        'sandbox_scene_wind': 0x4b256db,
        'sandbox_scene_wave': 0x4b256f3,
        'sandbox_scene_trees': 0x4b2570b,
        'sandbox_notice_attach': 0x4b1ff84,
        'sandbox_notice_detach': 0x4b1ffc3,
        'sandbox_project_markers': 0x4b20235,
        'sandbox_gun_marker': 0x4b2626c,
        'sandbox_blur_slider': 0x4b20c44,
        'room_header_description': 0x4b20946,
        'room_load_description': 0x4b20885,
        'room_get_description': 0x4b209e5,
        'sandbox_move': 0x4b200b6,
        'sandbox_stop': 0x4b201bf,
        'sandbox_particle_lifetime': 0x4b24165,
        'sandbox_spread_ring': 0x4b2419b,
    },
    '79671c0237466c225ef4d550c7c5059bcc7fc715c7ce52d8eca3911c9fcbaaab': {
        'sandbox_particle_update_queue': 0x4b25a34,
        'sandbox_tracer_update': 0x4b2568d,
        'sandbox_tracer_position': 0x4b25702,
        'sandbox_scene_shader_clock': 0x4b25ab2,
        'sandbox_load_keyboard': 0x4b25294,
        'sandbox_save_keyboard': 0x4b252d9,
        'boss_stock_panel': 0x4b23a13,
        'boss_screen_root': 0x4b239b9,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2039e,
        'sandbox_graphics_page': 0x4b20b61,
        'sandbox_blur_material': 0x4b26778,
        'sandbox_scene_graded_blit': 0x4b269b7,
        'sandbox_scene_blit': 0x4b2694b,
        'sandbox_refresh_score': 0x4b1ff05,
        'sandbox_message': 0x4b25b41,
        'sandbox_reset': 0x4b261e2,
        'sandbox_frame': 0x4b20009,
        'sandbox_compute_marker': 0x4b265e0,
        'sandbox_draw': 0x4b20400,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b253bc,
        'sandbox_binding_caption': 0x4b1fd54,
        'sandbox_binding_name': 0x4b1fd19,
        'sandbox_set_binding': 0x4b1fde5,
        'sandbox_reset_bindings': 0x4b1fd8f,
        'sandbox_controls_version': 0x4b25357,
        'sandbox_migrate_controls': 0x4b25308,
        'sandbox_scene_particles': 0x4b2584b,
        'sandbox_open_replay_folder': 0x4b254fb,
        'sandbox_scene_animation': 0x4b257eb,
        'sandbox_scene_wind': 0x4b25803,
        'sandbox_scene_wave': 0x4b2581b,
        'sandbox_scene_trees': 0x4b25833,
        'sandbox_notice_attach': 0x4b1ff84,
        'sandbox_notice_detach': 0x4b1ffc3,
        'sandbox_project_markers': 0x4b20235,
        'sandbox_gun_marker': 0x4b2639a,
        'sandbox_blur_slider': 0x4b20c44,
        'room_header_description': 0x4b20946,
        'room_load_description': 0x4b20885,
        'room_get_description': 0x4b209e5,
        'sandbox_move': 0x4b200b6,
        'sandbox_stop': 0x4b201bf,
        'sandbox_particle_lifetime': 0x4b24245,
        'sandbox_spread_ring': 0x4b2427b,
    },
    '5553bc8b38ec63670848b1cde9c9e8c933c114b1f18f9d5ba882f124712b4604': {
        'sandbox_particle_update_queue': 0x4b25b42,
        'sandbox_tracer_update': 0x4b2579b,
        'sandbox_tracer_position': 0x4b25810,
        'sandbox_scene_shader_clock': 0x4b25bc0,
        'sandbox_load_keyboard': 0x4b253a2,
        'sandbox_save_keyboard': 0x4b253e7,
        'boss_stock_panel': 0x4b23d8c,
        'boss_screen_root': 0x4b23d32,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20392,
        'sandbox_graphics_page': 0x4b20b47,
        'sandbox_blur_material': 0x4b268e0,
        'sandbox_scene_graded_blit': 0x4b26b1f,
        'sandbox_scene_blit': 0x4b26ab3,
        'sandbox_refresh_score': 0x4b1fef9,
        'sandbox_message': 0x4b25c4f,
        'sandbox_reset': 0x4b26356,
        'sandbox_frame': 0x4b1fffd,
        'sandbox_compute_marker': 0x4b26748,
        'sandbox_draw': 0x4b203f4,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b254ca,
        'sandbox_binding_caption': 0x4b1fd48,
        'sandbox_binding_name': 0x4b1fd0d,
        'sandbox_set_binding': 0x4b1fdd9,
        'sandbox_reset_bindings': 0x4b1fd83,
        'sandbox_controls_version': 0x4b25465,
        'sandbox_migrate_controls': 0x4b25416,
        'sandbox_scene_particles': 0x4b25959,
        'sandbox_open_replay_folder': 0x4b25609,
        'sandbox_scene_animation': 0x4b258f9,
        'sandbox_scene_wind': 0x4b25911,
        'sandbox_scene_wave': 0x4b25929,
        'sandbox_scene_trees': 0x4b25941,
        'sandbox_notice_attach': 0x4b1ff78,
        'sandbox_notice_detach': 0x4b1ffb7,
        'sandbox_project_markers': 0x4b20229,
        'sandbox_gun_marker': 0x4b26502,
        'sandbox_blur_slider': 0x4b20c2a,
        'room_header_description': 0x4b2092c,
        'room_load_description': 0x4b2086b,
        'room_get_description': 0x4b209cb,
        'sandbox_move': 0x4b200aa,
        'sandbox_stop': 0x4b201b3,
        'sandbox_particle_lifetime': 0x4b245c5,
        'sandbox_spread_ring': 0x4b245fb,
    },
    '5c3a758bf4e191c63205ab3ffda6e28808db6de75bc49883048091b6b76e7a01': {
        'sandbox_particle_update_queue': 0x4b25bc2,
        'sandbox_tracer_update': 0x4b2581b,
        'sandbox_tracer_position': 0x4b25890,
        'sandbox_scene_shader_clock': 0x4b25c40,
        'sandbox_load_keyboard': 0x4b25422,
        'sandbox_save_keyboard': 0x4b25467,
        'boss_stock_panel': 0x4b23e22,
        'boss_screen_root': 0x4b23dc8,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20392,
        'sandbox_graphics_page': 0x4b20b47,
        'sandbox_blur_material': 0x4b26960,
        'sandbox_scene_graded_blit': 0x4b26b9f,
        'sandbox_scene_blit': 0x4b26b33,
        'sandbox_refresh_score': 0x4b1fef9,
        'sandbox_message': 0x4b25ccf,
        'sandbox_reset': 0x4b263d6,
        'sandbox_frame': 0x4b1fffd,
        'sandbox_compute_marker': 0x4b267c8,
        'sandbox_draw': 0x4b203f4,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b2554a,
        'sandbox_binding_caption': 0x4b1fd48,
        'sandbox_binding_name': 0x4b1fd0d,
        'sandbox_set_binding': 0x4b1fdd9,
        'sandbox_reset_bindings': 0x4b1fd83,
        'sandbox_controls_version': 0x4b254e5,
        'sandbox_migrate_controls': 0x4b25496,
        'sandbox_scene_particles': 0x4b259d9,
        'sandbox_open_replay_folder': 0x4b25689,
        'sandbox_scene_animation': 0x4b25979,
        'sandbox_scene_wind': 0x4b25991,
        'sandbox_scene_wave': 0x4b259a9,
        'sandbox_scene_trees': 0x4b259c1,
        'sandbox_notice_attach': 0x4b1ff78,
        'sandbox_notice_detach': 0x4b1ffb7,
        'sandbox_project_markers': 0x4b20229,
        'sandbox_gun_marker': 0x4b26582,
        'sandbox_blur_slider': 0x4b20c2a,
        'room_header_description': 0x4b2092c,
        'room_load_description': 0x4b2086b,
        'room_get_description': 0x4b209cb,
        'sandbox_move': 0x4b200aa,
        'sandbox_stop': 0x4b201b3,
        'sandbox_particle_lifetime': 0x4b24645,
        'sandbox_spread_ring': 0x4b2467b,
    },
    'ceb3eb7fbfdde837df4e054c3cb8d905f15dd33c7967ff8d18bee23f855a9732': {
        'sandbox_particle_update_queue': 0x4b25d42,
        'sandbox_tracer_update': 0x4b2599b,
        'sandbox_tracer_position': 0x4b25a10,
        'sandbox_scene_shader_clock': 0x4b25dc0,
        'sandbox_load_keyboard': 0x4b255a2,
        'sandbox_save_keyboard': 0x4b255e7,
        'boss_stock_panel': 0x4b23f61,
        'boss_screen_root': 0x4b23f07,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20392,
        'sandbox_graphics_page': 0x4b20b47,
        'sandbox_blur_material': 0x4b26b06,
        'sandbox_scene_graded_blit': 0x4b26d45,
        'sandbox_scene_blit': 0x4b26cd9,
        'sandbox_refresh_score': 0x4b1fef9,
        'sandbox_message': 0x4b25e4f,
        'sandbox_reset': 0x4b2657c,
        'sandbox_frame': 0x4b1fffd,
        'sandbox_compute_marker': 0x4b2696e,
        'sandbox_draw': 0x4b203f4,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b256ca,
        'sandbox_binding_caption': 0x4b1fd48,
        'sandbox_binding_name': 0x4b1fd0d,
        'sandbox_set_binding': 0x4b1fdd9,
        'sandbox_reset_bindings': 0x4b1fd83,
        'sandbox_controls_version': 0x4b25665,
        'sandbox_migrate_controls': 0x4b25616,
        'sandbox_scene_particles': 0x4b25b59,
        'sandbox_open_replay_folder': 0x4b25809,
        'sandbox_scene_animation': 0x4b25af9,
        'sandbox_scene_wind': 0x4b25b11,
        'sandbox_scene_wave': 0x4b25b29,
        'sandbox_scene_trees': 0x4b25b41,
        'sandbox_notice_attach': 0x4b1ff78,
        'sandbox_notice_detach': 0x4b1ffb7,
        'sandbox_project_markers': 0x4b20229,
        'sandbox_gun_marker': 0x4b26728,
        'sandbox_blur_slider': 0x4b20c2a,
        'room_header_description': 0x4b2092c,
        'room_load_description': 0x4b2086b,
        'room_get_description': 0x4b209cb,
        'sandbox_move': 0x4b200aa,
        'sandbox_stop': 0x4b201b3,
        'sandbox_particle_lifetime': 0x4b247c5,
        'sandbox_spread_ring': 0x4b247fb,
    },
    'c1a1db9502f56394adcc6e96fea88e5df035862862f338203808c3c9b39c4995': {
        'sandbox_particle_update_queue': 0x4b25dc2,
        'sandbox_tracer_update': 0x4b25a1b,
        'sandbox_tracer_position': 0x4b25a90,
        'sandbox_scene_shader_clock': 0x4b25e40,
        'sandbox_load_keyboard': 0x4b25622,
        'sandbox_save_keyboard': 0x4b25667,
        'boss_stock_panel': 0x4b23f9b,
        'boss_screen_root': 0x4b23f41,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20392,
        'sandbox_graphics_page': 0x4b20b47,
        'sandbox_blur_material': 0x4b26bb7,
        'sandbox_scene_graded_blit': 0x4b26df6,
        'sandbox_scene_blit': 0x4b26d8a,
        'sandbox_refresh_score': 0x4b1fef9,
        'sandbox_message': 0x4b25ecf,
        'sandbox_reset': 0x4b2662d,
        'sandbox_frame': 0x4b1fffd,
        'sandbox_compute_marker': 0x4b26a1f,
        'sandbox_draw': 0x4b203f4,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b2574a,
        'sandbox_binding_caption': 0x4b1fd48,
        'sandbox_binding_name': 0x4b1fd0d,
        'sandbox_set_binding': 0x4b1fdd9,
        'sandbox_reset_bindings': 0x4b1fd83,
        'sandbox_controls_version': 0x4b256e5,
        'sandbox_migrate_controls': 0x4b25696,
        'sandbox_scene_particles': 0x4b25bd9,
        'sandbox_open_replay_folder': 0x4b25889,
        'sandbox_scene_animation': 0x4b25b79,
        'sandbox_scene_wind': 0x4b25b91,
        'sandbox_scene_wave': 0x4b25ba9,
        'sandbox_scene_trees': 0x4b25bc1,
        'sandbox_notice_attach': 0x4b1ff78,
        'sandbox_notice_detach': 0x4b1ffb7,
        'sandbox_project_markers': 0x4b20229,
        'sandbox_gun_marker': 0x4b267d9,
        'sandbox_blur_slider': 0x4b20c2a,
        'room_header_description': 0x4b2092c,
        'room_load_description': 0x4b2086b,
        'room_get_description': 0x4b209cb,
        'sandbox_move': 0x4b200aa,
        'sandbox_stop': 0x4b201b3,
        'sandbox_particle_lifetime': 0x4b24845,
        'sandbox_spread_ring': 0x4b2487b,
    },
    '305a19dd34bd8240800240d158de6db19e5da933f74d4195bf04a87ab03bee5a': {
        'sandbox_particle_update_queue': 0x4b25de2,
        'sandbox_tracer_update': 0x4b25a3b,
        'sandbox_tracer_position': 0x4b25ab0,
        'sandbox_scene_shader_clock': 0x4b25e60,
        'sandbox_load_keyboard': 0x4b25642,
        'sandbox_save_keyboard': 0x4b25687,
        'boss_stock_panel': 0x4b23fba,
        'boss_screen_root': 0x4b23f60,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20392,
        'sandbox_graphics_page': 0x4b20b47,
        'sandbox_blur_material': 0x4b26d33,
        'sandbox_scene_graded_blit': 0x4b26f72,
        'sandbox_scene_blit': 0x4b26f06,
        'sandbox_refresh_score': 0x4b1fef9,
        'sandbox_message': 0x4b25eef,
        'sandbox_reset': 0x4b267a3,
        'sandbox_frame': 0x4b1fffd,
        'sandbox_compute_marker': 0x4b26b9b,
        'sandbox_draw': 0x4b203f4,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b2576a,
        'sandbox_binding_caption': 0x4b1fd48,
        'sandbox_binding_name': 0x4b1fd0d,
        'sandbox_set_binding': 0x4b1fdd9,
        'sandbox_reset_bindings': 0x4b1fd83,
        'sandbox_controls_version': 0x4b25705,
        'sandbox_migrate_controls': 0x4b256b6,
        'sandbox_scene_particles': 0x4b25bf9,
        'sandbox_open_replay_folder': 0x4b258a9,
        'sandbox_scene_animation': 0x4b25b99,
        'sandbox_scene_wind': 0x4b25bb1,
        'sandbox_scene_wave': 0x4b25bc9,
        'sandbox_scene_trees': 0x4b25be1,
        'sandbox_notice_attach': 0x4b1ff78,
        'sandbox_notice_detach': 0x4b1ffb7,
        'sandbox_project_markers': 0x4b20229,
        'sandbox_gun_marker': 0x4b26955,
        'sandbox_blur_slider': 0x4b20c2a,
        'room_header_description': 0x4b2092c,
        'room_load_description': 0x4b2086b,
        'room_get_description': 0x4b209cb,
        'sandbox_move': 0x4b200aa,
        'sandbox_stop': 0x4b201b3,
        'sandbox_particle_lifetime': 0x4b24865,
        'sandbox_spread_ring': 0x4b2489b,
    },
    'd492d70d207bf5324b62929d5a9168f9ad6345157203959b4df8733b494e9cca': {
        'sandbox_particle_update_queue': 0x4b25f10,
        'sandbox_tracer_update': 0x4b25b69,
        'sandbox_tracer_position': 0x4b25bde,
        'sandbox_scene_shader_clock': 0x4b25f8e,
        'sandbox_load_keyboard': 0x4b25770,
        'sandbox_save_keyboard': 0x4b257b5,
        'boss_stock_panel': 0x4b23fe8,
        'boss_screen_root': 0x4b23f8e,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20589,
        'sandbox_graphics_page': 0x4b20d3e,
        'sandbox_blur_material': 0x4b26e50,
        'sandbox_scene_graded_blit': 0x4b2708f,
        'sandbox_scene_blit': 0x4b27023,
        'sandbox_refresh_score': 0x4b200f0,
        'sandbox_message': 0x4b2601d,
        'sandbox_reset': 0x4b268b5,
        'sandbox_frame': 0x4b201f4,
        'sandbox_compute_marker': 0x4b26cb8,
        'sandbox_draw': 0x4b205eb,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b25898,
        'sandbox_binding_caption': 0x4b1ff3f,
        'sandbox_binding_name': 0x4b1ff04,
        'sandbox_set_binding': 0x4b1ffd0,
        'sandbox_reset_bindings': 0x4b1ff7a,
        'sandbox_controls_version': 0x4b25833,
        'sandbox_migrate_controls': 0x4b257e4,
        'sandbox_scene_particles': 0x4b25d27,
        'sandbox_open_replay_folder': 0x4b259d7,
        'sandbox_scene_animation': 0x4b25cc7,
        'sandbox_scene_wind': 0x4b25cdf,
        'sandbox_scene_wave': 0x4b25cf7,
        'sandbox_scene_trees': 0x4b25d0f,
        'sandbox_notice_attach': 0x4b2016f,
        'sandbox_notice_detach': 0x4b201ae,
        'sandbox_project_markers': 0x4b20420,
        'sandbox_gun_marker': 0x4b26a72,
        'sandbox_blur_slider': 0x4b20e21,
        'room_header_description': 0x4b20b23,
        'room_load_description': 0x4b20a62,
        'room_get_description': 0x4b20bc2,
        'sandbox_move': 0x4b202a1,
        'sandbox_stop': 0x4b203aa,
        'sandbox_particle_lifetime': 0x4b24885,
        'sandbox_spread_ring': 0x4b248bb,
    },
    'e5a1dec215f16e5192a5d4644513d4b17feaf0052e04362e0065cea1388d5eb0': {
        'sandbox_particle_update_queue': 0x4b25f34,
        'sandbox_tracer_update': 0x4b25b8d,
        'sandbox_tracer_position': 0x4b25c02,
        'sandbox_scene_shader_clock': 0x4b25fb2,
        'sandbox_load_keyboard': 0x4b25794,
        'sandbox_save_keyboard': 0x4b257d9,
        'boss_stock_panel': 0x4b23fe8,
        'boss_screen_root': 0x4b23f8e,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20588,
        'sandbox_graphics_page': 0x4b20d3d,
        'sandbox_blur_material': 0x4b26e74,
        'sandbox_scene_graded_blit': 0x4b270b3,
        'sandbox_scene_blit': 0x4b27047,
        'sandbox_refresh_score': 0x4b200ef,
        'sandbox_message': 0x4b26041,
        'sandbox_reset': 0x4b268d9,
        'sandbox_frame': 0x4b201f3,
        'sandbox_compute_marker': 0x4b26cdc,
        'sandbox_draw': 0x4b205ea,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b258bc,
        'sandbox_binding_caption': 0x4b1ff3e,
        'sandbox_binding_name': 0x4b1ff03,
        'sandbox_set_binding': 0x4b1ffcf,
        'sandbox_reset_bindings': 0x4b1ff79,
        'sandbox_controls_version': 0x4b25857,
        'sandbox_migrate_controls': 0x4b25808,
        'sandbox_scene_particles': 0x4b25d4b,
        'sandbox_open_replay_folder': 0x4b259fb,
        'sandbox_scene_animation': 0x4b25ceb,
        'sandbox_scene_wind': 0x4b25d03,
        'sandbox_scene_wave': 0x4b25d1b,
        'sandbox_scene_trees': 0x4b25d33,
        'sandbox_notice_attach': 0x4b2016e,
        'sandbox_notice_detach': 0x4b201ad,
        'sandbox_project_markers': 0x4b2041f,
        'sandbox_gun_marker': 0x4b26a96,
        'sandbox_blur_slider': 0x4b20e20,
        'room_header_description': 0x4b20b22,
        'room_load_description': 0x4b20a61,
        'room_get_description': 0x4b20bc1,
        'sandbox_move': 0x4b202a0,
        'sandbox_stop': 0x4b203a9,
        'sandbox_particle_lifetime': 0x4b24885,
        'sandbox_spread_ring': 0x4b248bb,
    },
    '6bd237342afab27d8e8858c11050bcfe6a1b28efa53f49727c927c0c199cf40d': {
        'sandbox_particle_update_queue': 0x4b26194,
        'sandbox_tracer_update': 0x4b25ded,
        'sandbox_tracer_position': 0x4b25e62,
        'sandbox_scene_shader_clock': 0x4b26212,
        'sandbox_load_keyboard': 0x4b259f4,
        'sandbox_save_keyboard': 0x4b25a39,
        'boss_stock_panel': 0x4b23fe8,
        'boss_screen_root': 0x4b23f8e,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2059c,
        'sandbox_graphics_page': 0x4b20d51,
        'sandbox_blur_material': 0x4b26fea,
        'sandbox_scene_graded_blit': 0x4b27229,
        'sandbox_scene_blit': 0x4b271bd,
        'sandbox_refresh_score': 0x4b20103,
        'sandbox_message': 0x4b262a1,
        'sandbox_reset': 0x4b26b39,
        'sandbox_frame': 0x4b20207,
        'sandbox_compute_marker': 0x4b26e52,
        'sandbox_draw': 0x4b205fe,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b25b1c,
        'sandbox_binding_caption': 0x4b1ff52,
        'sandbox_binding_name': 0x4b1ff17,
        'sandbox_set_binding': 0x4b1ffe3,
        'sandbox_reset_bindings': 0x4b1ff8d,
        'sandbox_controls_version': 0x4b25ab7,
        'sandbox_migrate_controls': 0x4b25a68,
        'sandbox_scene_particles': 0x4b25fab,
        'sandbox_open_replay_folder': 0x4b25c5b,
        'sandbox_scene_animation': 0x4b25f4b,
        'sandbox_scene_wind': 0x4b25f63,
        'sandbox_scene_wave': 0x4b25f7b,
        'sandbox_scene_trees': 0x4b25f93,
        'sandbox_notice_attach': 0x4b20182,
        'sandbox_notice_detach': 0x4b201c1,
        'sandbox_project_markers': 0x4b20433,
        'sandbox_gun_marker': 0x4b26c0c,
        'sandbox_blur_slider': 0x4b20e34,
        'room_header_description': 0x4b20b36,
        'room_load_description': 0x4b20a75,
        'room_get_description': 0x4b20bd5,
        'sandbox_move': 0x4b202b4,
        'sandbox_stop': 0x4b203bd,
        'sandbox_particle_lifetime': 0x4b24b05,
        'sandbox_spread_ring': 0x4b24b3b,
    },
    'f868af37ff7d0a0ad675734075b3b73407ef4225ee86cc8e5934f394a6259b4d': {
        'sandbox_particle_update_queue': 0x4b26294,
        'sandbox_tracer_update': 0x4b25eed,
        'sandbox_tracer_position': 0x4b25f62,
        'sandbox_scene_shader_clock': 0x4b26312,
        'sandbox_load_keyboard': 0x4b25af4,
        'sandbox_save_keyboard': 0x4b25b39,
        'boss_stock_panel': 0x4b23fe8,
        'boss_screen_root': 0x4b23f8e,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b205a2,
        'sandbox_graphics_page': 0x4b20d57,
        'sandbox_blur_material': 0x4b270ea,
        'sandbox_scene_graded_blit': 0x4b27329,
        'sandbox_scene_blit': 0x4b272bd,
        'sandbox_refresh_score': 0x4b20109,
        'sandbox_message': 0x4b263a1,
        'sandbox_reset': 0x4b26c39,
        'sandbox_frame': 0x4b2020d,
        'sandbox_compute_marker': 0x4b26f52,
        'sandbox_draw': 0x4b20604,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b25c1c,
        'sandbox_binding_caption': 0x4b1ff58,
        'sandbox_binding_name': 0x4b1ff1d,
        'sandbox_set_binding': 0x4b1ffe9,
        'sandbox_reset_bindings': 0x4b1ff93,
        'sandbox_controls_version': 0x4b25bb7,
        'sandbox_migrate_controls': 0x4b25b68,
        'sandbox_scene_particles': 0x4b260ab,
        'sandbox_open_replay_folder': 0x4b25d5b,
        'sandbox_scene_animation': 0x4b2604b,
        'sandbox_scene_wind': 0x4b26063,
        'sandbox_scene_wave': 0x4b2607b,
        'sandbox_scene_trees': 0x4b26093,
        'sandbox_notice_attach': 0x4b20188,
        'sandbox_notice_detach': 0x4b201c7,
        'sandbox_project_markers': 0x4b20439,
        'sandbox_gun_marker': 0x4b26d0c,
        'sandbox_blur_slider': 0x4b20e3a,
        'room_header_description': 0x4b20b3c,
        'room_load_description': 0x4b20a7b,
        'room_get_description': 0x4b20bdb,
        'sandbox_move': 0x4b202ba,
        'sandbox_stop': 0x4b203c3,
        'sandbox_particle_lifetime': 0x4b24c05,
        'sandbox_spread_ring': 0x4b24c3b,
    },
    'dd52c40dfbca478a590e0096c91ad15dc06ac99b1bb8dd4d2b5daa89dd6462d8': {
        'sandbox_particle_update_queue': 0x4b2643a,
        'sandbox_tracer_update': 0x4b26093,
        'sandbox_tracer_position': 0x4b26108,
        'sandbox_scene_shader_clock': 0x4b264b8,
        'sandbox_load_keyboard': 0x4b25c94,
        'sandbox_save_keyboard': 0x4b25cd9,
        'boss_stock_panel': 0x4b240fb,
        'boss_screen_root': 0x4b240a1,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20544,
        'sandbox_graphics_page': 0x4b20cf9,
        'sandbox_blur_material': 0x4b272aa,
        'sandbox_scene_graded_blit': 0x4b274e9,
        'sandbox_scene_blit': 0x4b2747d,
        'sandbox_refresh_score': 0x4b200ab,
        'sandbox_message': 0x4b26547,
        'sandbox_reset': 0x4b26df9,
        'sandbox_frame': 0x4b201af,
        'sandbox_compute_marker': 0x4b27112,
        'sandbox_draw': 0x4b205a6,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b25dbc,
        'sandbox_binding_caption': 0x4b1fefa,
        'sandbox_binding_name': 0x4b1febf,
        'sandbox_set_binding': 0x4b1ff8b,
        'sandbox_reset_bindings': 0x4b1ff35,
        'sandbox_controls_version': 0x4b25d57,
        'sandbox_migrate_controls': 0x4b25d08,
        'sandbox_scene_particles': 0x4b26251,
        'sandbox_open_replay_folder': 0x4b25f01,
        'sandbox_scene_animation': 0x4b261f1,
        'sandbox_scene_wind': 0x4b26209,
        'sandbox_scene_wave': 0x4b26221,
        'sandbox_scene_trees': 0x4b26239,
        'sandbox_notice_attach': 0x4b2012a,
        'sandbox_notice_detach': 0x4b20169,
        'sandbox_project_markers': 0x4b203db,
        'sandbox_gun_marker': 0x4b26ecc,
        'sandbox_blur_slider': 0x4b20ddc,
        'room_header_description': 0x4b20ade,
        'room_load_description': 0x4b20a1d,
        'room_get_description': 0x4b20b7d,
        'sandbox_move': 0x4b2025c,
        'sandbox_stop': 0x4b20365,
        'sandbox_particle_lifetime': 0x4b24da5,
        'sandbox_spread_ring': 0x4b24ddb,
    },
    'b2a64a575e8269018c302880b4acc6d060116451edbada1e858dee7eb47e5db5': {
        'sandbox_particle_update_queue': 0x4b27324,
        'sandbox_tracer_update': 0x4b26f7d,
        'sandbox_tracer_position': 0x4b26ff2,
        'sandbox_scene_shader_clock': 0x4b273a2,
        'sandbox_load_keyboard': 0x4b26b7e,
        'sandbox_save_keyboard': 0x4b26bc3,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20544,
        'sandbox_graphics_page': 0x4b20d12,
        'sandbox_blur_material': 0x4b281f0,
        'sandbox_scene_graded_blit': 0x4b2842f,
        'sandbox_scene_blit': 0x4b283c3,
        'sandbox_refresh_score': 0x4b200ab,
        'sandbox_message': 0x4b27431,
        'sandbox_reset': 0x4b27d3f,
        'sandbox_frame': 0x4b201af,
        'sandbox_compute_marker': 0x4b28058,
        'sandbox_draw': 0x4b205a6,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b26ca6,
        'sandbox_binding_caption': 0x4b1fefa,
        'sandbox_binding_name': 0x4b1febf,
        'sandbox_set_binding': 0x4b1ff8b,
        'sandbox_reset_bindings': 0x4b1ff35,
        'sandbox_controls_version': 0x4b26c41,
        'sandbox_migrate_controls': 0x4b26bf2,
        'sandbox_scene_particles': 0x4b2713b,
        'sandbox_open_replay_folder': 0x4b26deb,
        'sandbox_scene_animation': 0x4b270db,
        'sandbox_scene_wind': 0x4b270f3,
        'sandbox_scene_wave': 0x4b2710b,
        'sandbox_scene_trees': 0x4b27123,
        'sandbox_notice_attach': 0x4b2012a,
        'sandbox_notice_detach': 0x4b20169,
        'sandbox_project_markers': 0x4b203db,
        'sandbox_gun_marker': 0x4b27e12,
        'sandbox_blur_slider': 0x4b20df5,
        'room_header_description': 0x4b20ade,
        'room_load_description': 0x4b20a1d,
        'room_get_description': 0x4b20b7d,
        'sandbox_move': 0x4b2025c,
        'sandbox_stop': 0x4b20365,
        'sandbox_particle_lifetime': 0x4b24e85,
        'sandbox_spread_ring': 0x4b24ebb,
    },
    'e224fb878864e50b7f0e4dd9b6cc76c5a4b2383d4ba7bd3896ab72af798a88fc': {
        'sandbox_particle_update_queue': 0x4b2722f,
        'sandbox_tracer_update': 0x4b26e88,
        'sandbox_tracer_position': 0x4b26efd,
        'sandbox_scene_shader_clock': 0x4b272ad,
        'sandbox_load_keyboard': 0x4b26a89,
        'sandbox_save_keyboard': 0x4b26ace,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20544,
        'sandbox_graphics_page': 0x4b20d12,
        'sandbox_blur_material': 0x4b280fb,
        'sandbox_scene_graded_blit': 0x4b2833a,
        'sandbox_scene_blit': 0x4b282ce,
        'sandbox_refresh_score': 0x4b200ab,
        'sandbox_message': 0x4b2733c,
        'sandbox_reset': 0x4b27c4a,
        'sandbox_frame': 0x4b201af,
        'sandbox_compute_marker': 0x4b27f63,
        'sandbox_draw': 0x4b205a6,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b26bb1,
        'sandbox_binding_caption': 0x4b1fefa,
        'sandbox_binding_name': 0x4b1febf,
        'sandbox_set_binding': 0x4b1ff8b,
        'sandbox_reset_bindings': 0x4b1ff35,
        'sandbox_controls_version': 0x4b26b4c,
        'sandbox_migrate_controls': 0x4b26afd,
        'sandbox_scene_particles': 0x4b27046,
        'sandbox_open_replay_folder': 0x4b26cf6,
        'sandbox_scene_animation': 0x4b26fe6,
        'sandbox_scene_wind': 0x4b26ffe,
        'sandbox_scene_wave': 0x4b27016,
        'sandbox_scene_trees': 0x4b2702e,
        'sandbox_notice_attach': 0x4b2012a,
        'sandbox_notice_detach': 0x4b20169,
        'sandbox_project_markers': 0x4b203db,
        'sandbox_gun_marker': 0x4b27d1d,
        'sandbox_blur_slider': 0x4b20df5,
        'room_header_description': 0x4b20ade,
        'room_load_description': 0x4b20a1d,
        'room_get_description': 0x4b20b7d,
        'sandbox_move': 0x4b2025c,
        'sandbox_stop': 0x4b20365,
        'sandbox_particle_lifetime': 0x4b24e85,
        'sandbox_spread_ring': 0x4b24ebb,
        'sandbox_setup_reply': 0x4b24ed7,
    },
    'bbbc1a4ca47976c2d39761908ae0134e42c4a6e40975137c60855de26b4dd3e2': {
        'sandbox_particle_update_queue': 0x4b2722f,
        'sandbox_tracer_update': 0x4b26e88,
        'sandbox_tracer_position': 0x4b26efd,
        'sandbox_scene_shader_clock': 0x4b272ad,
        'sandbox_load_keyboard': 0x4b26a89,
        'sandbox_save_keyboard': 0x4b26ace,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20544,
        'sandbox_graphics_page': 0x4b20d12,
        'sandbox_blur_material': 0x4b280fb,
        'sandbox_scene_graded_blit': 0x4b2833a,
        'sandbox_scene_blit': 0x4b282ce,
        'sandbox_refresh_score': 0x4b200ab,
        'sandbox_message': 0x4b2733c,
        'sandbox_reset': 0x4b27c4a,
        'sandbox_frame': 0x4b201af,
        'sandbox_compute_marker': 0x4b27f63,
        'sandbox_draw': 0x4b205a6,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b26bb1,
        'sandbox_binding_caption': 0x4b1fefa,
        'sandbox_binding_name': 0x4b1febf,
        'sandbox_set_binding': 0x4b1ff8b,
        'sandbox_reset_bindings': 0x4b1ff35,
        'sandbox_controls_version': 0x4b26b4c,
        'sandbox_migrate_controls': 0x4b26afd,
        'sandbox_scene_particles': 0x4b27046,
        'sandbox_open_replay_folder': 0x4b26cf6,
        'sandbox_scene_animation': 0x4b26fe6,
        'sandbox_scene_wind': 0x4b26ffe,
        'sandbox_scene_wave': 0x4b27016,
        'sandbox_scene_trees': 0x4b2702e,
        'sandbox_notice_attach': 0x4b2012a,
        'sandbox_notice_detach': 0x4b20169,
        'sandbox_project_markers': 0x4b203db,
        'sandbox_gun_marker': 0x4b27d1d,
        'sandbox_blur_slider': 0x4b20df5,
        'room_header_description': 0x4b20ade,
        'room_load_description': 0x4b20a1d,
        'room_get_description': 0x4b20b7d,
        'sandbox_move': 0x4b2025c,
        'sandbox_stop': 0x4b20365,
        'sandbox_particle_lifetime': 0x4b24e85,
        'sandbox_spread_ring': 0x4b24ebb,
        'sandbox_setup_reply': 0x4b24ed7,
    },
    '04d8fd455820b9f8cc7da89710570b436f74daa34fd254962d94c7830f3e22ce': {
        'sandbox_particle_update_queue': 0x4b27441,
        'sandbox_tracer_update': 0x4b2709a,
        'sandbox_tracer_position': 0x4b2710f,
        'sandbox_scene_shader_clock': 0x4b274bf,
        'sandbox_load_keyboard': 0x4b26c9b,
        'sandbox_save_keyboard': 0x4b26ce0,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20544,
        'sandbox_graphics_page': 0x4b20d1b,
        'sandbox_blur_material': 0x4b2830d,
        'sandbox_scene_graded_blit': 0x4b2854c,
        'sandbox_scene_blit': 0x4b284e0,
        'sandbox_refresh_score': 0x4b200ab,
        'sandbox_message': 0x4b2754e,
        'sandbox_reset': 0x4b27e5c,
        'sandbox_frame': 0x4b201af,
        'sandbox_compute_marker': 0x4b28175,
        'sandbox_draw': 0x4b205a6,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b26dc3,
        'sandbox_binding_caption': 0x4b1fefa,
        'sandbox_binding_name': 0x4b1febf,
        'sandbox_set_binding': 0x4b1ff8b,
        'sandbox_reset_bindings': 0x4b1ff35,
        'sandbox_controls_version': 0x4b26d5e,
        'sandbox_migrate_controls': 0x4b26d0f,
        'sandbox_scene_particles': 0x4b27258,
        'sandbox_open_replay_folder': 0x4b26f08,
        'sandbox_scene_animation': 0x4b271f8,
        'sandbox_scene_wind': 0x4b27210,
        'sandbox_scene_wave': 0x4b27228,
        'sandbox_scene_trees': 0x4b27240,
        'sandbox_notice_attach': 0x4b2012a,
        'sandbox_notice_detach': 0x4b20169,
        'sandbox_project_markers': 0x4b203db,
        'sandbox_gun_marker': 0x4b27f2f,
        'sandbox_blur_slider': 0x4b20dfe,
        'room_header_description': 0x4b20ade,
        'room_load_description': 0x4b20a1d,
        'room_get_description': 0x4b20b7d,
        'sandbox_move': 0x4b2025c,
        'sandbox_stop': 0x4b20365,
        'sandbox_particle_lifetime': 0x4b24ea5,
        'sandbox_spread_ring': 0x4b24edb,
        'sandbox_setup_reply': 0x4b24ef7,
    },
    'e317b1c0030a9cd5b6df2036a6847bd8ab26214f4ae37339069132eef684abd9': {
        'sandbox_zoom_changed': 0x4b28760,
        'sandbox_particle_update_queue': 0x4b274df,
        'sandbox_tracer_update': 0x4b27138,
        'sandbox_tracer_position': 0x4b271ad,
        'sandbox_scene_shader_clock': 0x4b2755d,
        'sandbox_load_keyboard': 0x4b26d39,
        'sandbox_save_keyboard': 0x4b26d7e,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20544,
        'sandbox_graphics_page': 0x4b20d1b,
        'sandbox_blur_material': 0x4b283ab,
        'sandbox_scene_graded_blit': 0x4b285ea,
        'sandbox_scene_blit': 0x4b2857e,
        'sandbox_refresh_score': 0x4b200ab,
        'sandbox_message': 0x4b275ec,
        'sandbox_reset': 0x4b27efa,
        'sandbox_frame': 0x4b201af,
        'sandbox_compute_marker': 0x4b28213,
        'sandbox_draw': 0x4b205a6,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b26e61,
        'sandbox_binding_caption': 0x4b1fefa,
        'sandbox_binding_name': 0x4b1febf,
        'sandbox_set_binding': 0x4b1ff8b,
        'sandbox_reset_bindings': 0x4b1ff35,
        'sandbox_controls_version': 0x4b26dfc,
        'sandbox_migrate_controls': 0x4b26dad,
        'sandbox_scene_particles': 0x4b272f6,
        'sandbox_open_replay_folder': 0x4b26fa6,
        'sandbox_scene_animation': 0x4b27296,
        'sandbox_scene_wind': 0x4b272ae,
        'sandbox_scene_wave': 0x4b272c6,
        'sandbox_scene_trees': 0x4b272de,
        'sandbox_notice_attach': 0x4b2012a,
        'sandbox_notice_detach': 0x4b20169,
        'sandbox_project_markers': 0x4b203db,
        'sandbox_gun_marker': 0x4b27fcd,
        'sandbox_blur_slider': 0x4b20dfe,
        'room_header_description': 0x4b20ade,
        'room_load_description': 0x4b20a1d,
        'room_get_description': 0x4b20b7d,
        'sandbox_move': 0x4b2025c,
        'sandbox_stop': 0x4b20365,
        'sandbox_zoom_migration': 0x4b24f99,
        'sandbox_zoom_cache': 0x4b24fbc,
        'sandbox_particle_lifetime': 0x4b24f05,
        'sandbox_spread_ring': 0x4b24f3b,
        'sandbox_setup_reply': 0x4b24f57,
    },
    'd2ee85023ae0aef18f10c14789cfecad94a9f93f5b27f7acf351b5cb777cb7e7': {
        'sandbox_zoom_changed': 0x4b28760,
        'sandbox_particle_update_queue': 0x4b274df,
        'sandbox_tracer_update': 0x4b27138,
        'sandbox_tracer_position': 0x4b271ad,
        'sandbox_scene_shader_clock': 0x4b2755d,
        'sandbox_load_keyboard': 0x4b26d39,
        'sandbox_save_keyboard': 0x4b26d7e,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20544,
        'sandbox_graphics_page': 0x4b20d1b,
        'sandbox_blur_material': 0x4b283ab,
        'sandbox_scene_graded_blit': 0x4b285ea,
        'sandbox_scene_blit': 0x4b2857e,
        'sandbox_refresh_score': 0x4b200ab,
        'sandbox_message': 0x4b275ec,
        'sandbox_reset': 0x4b27efa,
        'sandbox_frame': 0x4b201af,
        'sandbox_compute_marker': 0x4b28213,
        'sandbox_draw': 0x4b205a6,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b26e61,
        'sandbox_binding_caption': 0x4b1fefa,
        'sandbox_binding_name': 0x4b1febf,
        'sandbox_set_binding': 0x4b1ff8b,
        'sandbox_reset_bindings': 0x4b1ff35,
        'sandbox_controls_version': 0x4b26dfc,
        'sandbox_migrate_controls': 0x4b26dad,
        'sandbox_scene_particles': 0x4b272f6,
        'sandbox_open_replay_folder': 0x4b26fa6,
        'sandbox_scene_animation': 0x4b27296,
        'sandbox_scene_wind': 0x4b272ae,
        'sandbox_scene_wave': 0x4b272c6,
        'sandbox_scene_trees': 0x4b272de,
        'sandbox_notice_attach': 0x4b2012a,
        'sandbox_notice_detach': 0x4b20169,
        'sandbox_project_markers': 0x4b203db,
        'sandbox_gun_marker': 0x4b27fcd,
        'sandbox_blur_slider': 0x4b20dfe,
        'room_header_description': 0x4b20ade,
        'room_load_description': 0x4b20a1d,
        'room_get_description': 0x4b20b7d,
        'sandbox_move': 0x4b2025c,
        'sandbox_stop': 0x4b20365,
        'sandbox_zoom_migration': 0x4b24f99,
        'sandbox_zoom_cache': 0x4b24fbc,
        'sandbox_particle_lifetime': 0x4b24f05,
        'sandbox_spread_ring': 0x4b24f3b,
        'sandbox_setup_reply': 0x4b24f57,
    },
    '07d42dee84a66a933850831a531689976f2b05206ff04c858da369697db6b9ff': {
        'sandbox_zoom_changed': 0x4b28760,
        'sandbox_particle_update_queue': 0x4b274df,
        'sandbox_tracer_update': 0x4b27138,
        'sandbox_tracer_position': 0x4b271ad,
        'sandbox_scene_shader_clock': 0x4b2755d,
        'sandbox_load_keyboard': 0x4b26d39,
        'sandbox_save_keyboard': 0x4b26d7e,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20544,
        'sandbox_graphics_page': 0x4b20d1b,
        'sandbox_blur_material': 0x4b283ab,
        'sandbox_scene_graded_blit': 0x4b285ea,
        'sandbox_scene_blit': 0x4b2857e,
        'sandbox_refresh_score': 0x4b200ab,
        'sandbox_message': 0x4b275ec,
        'sandbox_reset': 0x4b27efa,
        'sandbox_frame': 0x4b201af,
        'sandbox_compute_marker': 0x4b28213,
        'sandbox_draw': 0x4b205a6,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b26e61,
        'sandbox_binding_caption': 0x4b1fefa,
        'sandbox_binding_name': 0x4b1febf,
        'sandbox_set_binding': 0x4b1ff8b,
        'sandbox_reset_bindings': 0x4b1ff35,
        'sandbox_controls_version': 0x4b26dfc,
        'sandbox_migrate_controls': 0x4b26dad,
        'sandbox_scene_particles': 0x4b272f6,
        'sandbox_open_replay_folder': 0x4b26fa6,
        'sandbox_scene_animation': 0x4b27296,
        'sandbox_scene_wind': 0x4b272ae,
        'sandbox_scene_wave': 0x4b272c6,
        'sandbox_scene_trees': 0x4b272de,
        'sandbox_notice_attach': 0x4b2012a,
        'sandbox_notice_detach': 0x4b20169,
        'sandbox_project_markers': 0x4b203db,
        'sandbox_gun_marker': 0x4b27fcd,
        'sandbox_blur_slider': 0x4b20dfe,
        'room_header_description': 0x4b20ade,
        'room_load_description': 0x4b20a1d,
        'room_get_description': 0x4b20b7d,
        'sandbox_move': 0x4b2025c,
        'sandbox_stop': 0x4b20365,
        'sandbox_zoom_migration': 0x4b24f99,
        'sandbox_particle_lifetime': 0x4b24f05,
        'sandbox_spread_ring': 0x4b24f3b,
        'sandbox_setup_reply': 0x4b24f57,
    },
    '8b2a7f1ba77e87fbc1285963ba1de255854e700faf4d8b29ab6727858683c9b1': {
        'sandbox_zoom_changed': 0x4b28745,
        'sandbox_particle_update_queue': 0x4b274c4,
        'sandbox_tracer_update': 0x4b2711d,
        'sandbox_tracer_position': 0x4b27192,
        'sandbox_scene_shader_clock': 0x4b27542,
        'sandbox_load_keyboard': 0x4b26d1e,
        'sandbox_save_keyboard': 0x4b26d63,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20544,
        'sandbox_graphics_page': 0x4b20d1b,
        'sandbox_blur_material': 0x4b28390,
        'sandbox_scene_graded_blit': 0x4b285cf,
        'sandbox_scene_blit': 0x4b28563,
        'sandbox_refresh_score': 0x4b200ab,
        'sandbox_message': 0x4b275d1,
        'sandbox_reset': 0x4b27edf,
        'sandbox_frame': 0x4b201af,
        'sandbox_compute_marker': 0x4b281f8,
        'sandbox_draw': 0x4b205a6,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b26e46,
        'sandbox_binding_caption': 0x4b1fefa,
        'sandbox_binding_name': 0x4b1febf,
        'sandbox_set_binding': 0x4b1ff8b,
        'sandbox_reset_bindings': 0x4b1ff35,
        'sandbox_controls_version': 0x4b26de1,
        'sandbox_migrate_controls': 0x4b26d92,
        'sandbox_scene_particles': 0x4b272db,
        'sandbox_open_replay_folder': 0x4b26f8b,
        'sandbox_scene_animation': 0x4b2727b,
        'sandbox_scene_wind': 0x4b27293,
        'sandbox_scene_wave': 0x4b272ab,
        'sandbox_scene_trees': 0x4b272c3,
        'sandbox_notice_attach': 0x4b2012a,
        'sandbox_notice_detach': 0x4b20169,
        'sandbox_project_markers': 0x4b203db,
        'sandbox_gun_marker': 0x4b27fb2,
        'sandbox_blur_slider': 0x4b20dfe,
        'room_header_description': 0x4b20ade,
        'room_load_description': 0x4b20a1d,
        'room_get_description': 0x4b20b7d,
        'sandbox_move': 0x4b2025c,
        'sandbox_stop': 0x4b20365,
        'sandbox_zoom_migration': 0x4b24f99,
        'sandbox_particle_lifetime': 0x4b24f05,
        'sandbox_spread_ring': 0x4b24f3b,
        'sandbox_setup_reply': 0x4b24f57,
    },
    'd160e901da3cfc189dff43bc26af2cf7f4251a6f332f602c262ed0e7d6f782f9': {
        'sandbox_zoom_changed': 0x4b28ae7,
        'sandbox_particle_update_queue': 0x4b27866,
        'sandbox_tracer_update': 0x4b274bf,
        'sandbox_tracer_position': 0x4b27534,
        'sandbox_scene_shader_clock': 0x4b278e4,
        'sandbox_load_keyboard': 0x4b270c0,
        'sandbox_save_keyboard': 0x4b27105,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2054b,
        'sandbox_graphics_page': 0x4b20dcc,
        'sandbox_blur_material': 0x4b28732,
        'sandbox_scene_graded_blit': 0x4b28971,
        'sandbox_scene_blit': 0x4b28905,
        'sandbox_refresh_score': 0x4b200b2,
        'sandbox_message': 0x4b27973,
        'sandbox_reset': 0x4b28281,
        'sandbox_frame': 0x4b201b6,
        'sandbox_compute_marker': 0x4b2859a,
        'sandbox_draw': 0x4b205ad,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b271e8,
        'sandbox_binding_caption': 0x4b1ff01,
        'sandbox_binding_name': 0x4b1fec6,
        'sandbox_set_binding': 0x4b1ff92,
        'sandbox_reset_bindings': 0x4b1ff3c,
        'sandbox_controls_version': 0x4b27183,
        'sandbox_migrate_controls': 0x4b27134,
        'sandbox_scene_particles': 0x4b2767d,
        'sandbox_open_replay_folder': 0x4b2732d,
        'sandbox_scene_animation': 0x4b2761d,
        'sandbox_scene_wind': 0x4b27635,
        'sandbox_scene_wave': 0x4b2764d,
        'sandbox_scene_trees': 0x4b27665,
        'sandbox_notice_attach': 0x4b20131,
        'sandbox_notice_detach': 0x4b20170,
        'sandbox_project_markers': 0x4b203e2,
        'sandbox_gun_marker': 0x4b28354,
        'sandbox_blur_slider': 0x4b20eaf,
        'room_header_description': 0x4b20b4f,
        'room_load_description': 0x4b20a24,
        'room_get_description': 0x4b20bee,
        'sandbox_move': 0x4b20263,
        'sandbox_stop': 0x4b2036c,
        'sandbox_zoom_migration': 0x4b24f99,
        'sandbox_particle_lifetime': 0x4b24f05,
        'sandbox_spread_ring': 0x4b24f3b,
        'sandbox_setup_reply': 0x4b24f57,
    },
    'dd4b446d7ded1c453f68e9e2953a4c4f1156ff0869304ec747524d2acfcbf28c': {
        'sandbox_zoom_changed': 0x4b28ae7,
        'sandbox_particle_update_queue': 0x4b27866,
        'sandbox_tracer_update': 0x4b274bf,
        'sandbox_tracer_position': 0x4b27534,
        'sandbox_scene_shader_clock': 0x4b278e4,
        'sandbox_load_keyboard': 0x4b270c0,
        'sandbox_save_keyboard': 0x4b27105,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2054b,
        'sandbox_graphics_page': 0x4b20e43,
        'sandbox_blur_material': 0x4b28732,
        'sandbox_scene_graded_blit': 0x4b28971,
        'sandbox_scene_blit': 0x4b28905,
        'sandbox_refresh_score': 0x4b200b2,
        'sandbox_message': 0x4b27973,
        'sandbox_reset': 0x4b28281,
        'sandbox_frame': 0x4b201b6,
        'sandbox_compute_marker': 0x4b2859a,
        'sandbox_draw': 0x4b205ad,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b271e8,
        'sandbox_binding_caption': 0x4b1ff01,
        'sandbox_binding_name': 0x4b1fec6,
        'sandbox_set_binding': 0x4b1ff92,
        'sandbox_reset_bindings': 0x4b1ff3c,
        'sandbox_controls_version': 0x4b27183,
        'sandbox_migrate_controls': 0x4b27134,
        'sandbox_scene_particles': 0x4b2767d,
        'sandbox_open_replay_folder': 0x4b2732d,
        'sandbox_scene_animation': 0x4b2761d,
        'sandbox_scene_wind': 0x4b27635,
        'sandbox_scene_wave': 0x4b2764d,
        'sandbox_scene_trees': 0x4b27665,
        'sandbox_notice_attach': 0x4b20131,
        'sandbox_notice_detach': 0x4b20170,
        'sandbox_project_markers': 0x4b203e2,
        'sandbox_gun_marker': 0x4b28354,
        'sandbox_blur_slider': 0x4b20f26,
        'room_header_description': 0x4b20b4f,
        'room_load_description': 0x4b20a24,
        'room_get_description': 0x4b20c65,
        'sandbox_move': 0x4b20263,
        'sandbox_stop': 0x4b2036c,
        'sandbox_zoom_migration': 0x4b24f99,
        'sandbox_particle_lifetime': 0x4b24f05,
        'sandbox_spread_ring': 0x4b24f3b,
        'sandbox_setup_reply': 0x4b24f57,
    },
    '20de03c44cc3cfc62c7a499f68e61e4601f98571e23ba6cc204f49a96b63ca5e': {
        'sandbox_zoom_changed': 0x4b28b55,
        'sandbox_particle_update_queue': 0x4b27866,
        'sandbox_tracer_update': 0x4b274bf,
        'sandbox_tracer_position': 0x4b27534,
        'sandbox_scene_shader_clock': 0x4b278e4,
        'sandbox_load_keyboard': 0x4b270c0,
        'sandbox_save_keyboard': 0x4b27105,
        'boss_stock_panel': 0x4b240e6,
        'boss_screen_root': 0x4b2408c,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2054b,
        'sandbox_graphics_page': 0x4b20e43,
        'sandbox_blur_material': 0x4b287a0,
        'sandbox_scene_graded_blit': 0x4b289df,
        'sandbox_scene_blit': 0x4b28973,
        'sandbox_refresh_score': 0x4b200b2,
        'sandbox_message': 0x4b27973,
        'sandbox_reset': 0x4b282ef,
        'sandbox_frame': 0x4b201b6,
        'sandbox_compute_marker': 0x4b28608,
        'sandbox_draw': 0x4b205ad,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b271e8,
        'sandbox_binding_caption': 0x4b1ff01,
        'sandbox_binding_name': 0x4b1fec6,
        'sandbox_set_binding': 0x4b1ff92,
        'sandbox_reset_bindings': 0x4b1ff3c,
        'sandbox_controls_version': 0x4b27183,
        'sandbox_migrate_controls': 0x4b27134,
        'sandbox_scene_particles': 0x4b2767d,
        'sandbox_open_replay_folder': 0x4b2732d,
        'sandbox_scene_animation': 0x4b2761d,
        'sandbox_scene_wind': 0x4b27635,
        'sandbox_scene_wave': 0x4b2764d,
        'sandbox_scene_trees': 0x4b27665,
        'sandbox_notice_attach': 0x4b20131,
        'sandbox_notice_detach': 0x4b20170,
        'sandbox_project_markers': 0x4b203e2,
        'sandbox_gun_marker': 0x4b283c2,
        'sandbox_blur_slider': 0x4b20f26,
        'room_header_description': 0x4b20b4f,
        'room_load_description': 0x4b20a24,
        'room_get_description': 0x4b20c65,
        'sandbox_move': 0x4b20263,
        'sandbox_stop': 0x4b2036c,
        'sandbox_zoom_migration': 0x4b24f99,
        'sandbox_particle_lifetime': 0x4b24f05,
        'sandbox_spread_ring': 0x4b24f3b,
        'sandbox_setup_reply': 0x4b24f57,
    },
    'a17050ea1704278055b2f380a2e21e1ee41be8a2ea1169abd0f50c1d683b0f4a': {
        'sandbox_zoom_changed': 0x4b28b75,
        'sandbox_particle_update_queue': 0x4b27886,
        'sandbox_tracer_update': 0x4b274df,
        'sandbox_tracer_position': 0x4b27554,
        'sandbox_scene_shader_clock': 0x4b27904,
        'sandbox_load_keyboard': 0x4b270e0,
        'sandbox_save_keyboard': 0x4b27125,
        'boss_stock_panel': 0x4b240f7,
        'boss_screen_root': 0x4b2409d,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2054b,
        'sandbox_graphics_page': 0x4b20e43,
        'sandbox_blur_material': 0x4b287c0,
        'sandbox_scene_graded_blit': 0x4b289ff,
        'sandbox_scene_blit': 0x4b28993,
        'sandbox_refresh_score': 0x4b200b2,
        'sandbox_message': 0x4b27993,
        'sandbox_reset': 0x4b2830f,
        'sandbox_frame': 0x4b201b6,
        'sandbox_compute_marker': 0x4b28628,
        'sandbox_draw': 0x4b205ad,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b27208,
        'sandbox_binding_caption': 0x4b1ff01,
        'sandbox_binding_name': 0x4b1fec6,
        'sandbox_set_binding': 0x4b1ff92,
        'sandbox_reset_bindings': 0x4b1ff3c,
        'sandbox_controls_version': 0x4b271a3,
        'sandbox_migrate_controls': 0x4b27154,
        'sandbox_scene_particles': 0x4b2769d,
        'sandbox_open_replay_folder': 0x4b2734d,
        'sandbox_scene_animation': 0x4b2763d,
        'sandbox_scene_wind': 0x4b27655,
        'sandbox_scene_wave': 0x4b2766d,
        'sandbox_scene_trees': 0x4b27685,
        'sandbox_notice_attach': 0x4b20131,
        'sandbox_notice_detach': 0x4b20170,
        'sandbox_project_markers': 0x4b203e2,
        'sandbox_gun_marker': 0x4b283e2,
        'sandbox_blur_slider': 0x4b20f26,
        'room_header_description': 0x4b20b4f,
        'room_load_description': 0x4b20a24,
        'room_get_description': 0x4b20c65,
        'sandbox_move': 0x4b20263,
        'sandbox_stop': 0x4b2036c,
        'sandbox_zoom_migration': 0x4b24fb9,
        'sandbox_particle_lifetime': 0x4b24f25,
        'sandbox_spread_ring': 0x4b24f5b,
        'sandbox_setup_reply': 0x4b24f77,
    },
    'b5d6c0b48322f06a000c667c1a9ba2e8b6ba2c49312fbbf446a7426f3ae2a35a': {
        'sandbox_marker_visible': 0x4b287c0,
        'sandbox_zoom_changed': 0x4b28bcd,
        'sandbox_particle_update_queue': 0x4b27886,
        'sandbox_tracer_update': 0x4b274df,
        'sandbox_tracer_position': 0x4b27554,
        'sandbox_scene_shader_clock': 0x4b27904,
        'sandbox_load_keyboard': 0x4b270e0,
        'sandbox_save_keyboard': 0x4b27125,
        'boss_stock_panel': 0x4b240f7,
        'boss_screen_root': 0x4b2409d,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2054b,
        'sandbox_graphics_page': 0x4b20e43,
        'sandbox_blur_material': 0x4b28818,
        'sandbox_scene_graded_blit': 0x4b28a57,
        'sandbox_scene_blit': 0x4b289eb,
        'sandbox_refresh_score': 0x4b200b2,
        'sandbox_message': 0x4b27993,
        'sandbox_reset': 0x4b2830f,
        'sandbox_frame': 0x4b201b6,
        'sandbox_compute_marker': 0x4b28628,
        'sandbox_draw': 0x4b205ad,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b27208,
        'sandbox_binding_caption': 0x4b1ff01,
        'sandbox_binding_name': 0x4b1fec6,
        'sandbox_set_binding': 0x4b1ff92,
        'sandbox_reset_bindings': 0x4b1ff3c,
        'sandbox_controls_version': 0x4b271a3,
        'sandbox_migrate_controls': 0x4b27154,
        'sandbox_scene_particles': 0x4b2769d,
        'sandbox_open_replay_folder': 0x4b2734d,
        'sandbox_scene_animation': 0x4b2763d,
        'sandbox_scene_wind': 0x4b27655,
        'sandbox_scene_wave': 0x4b2766d,
        'sandbox_scene_trees': 0x4b27685,
        'sandbox_notice_attach': 0x4b20131,
        'sandbox_notice_detach': 0x4b20170,
        'sandbox_project_markers': 0x4b203e2,
        'sandbox_gun_marker': 0x4b283e2,
        'sandbox_blur_slider': 0x4b20f26,
        'room_header_description': 0x4b20b4f,
        'room_load_description': 0x4b20a24,
        'room_get_description': 0x4b20c65,
        'sandbox_move': 0x4b20263,
        'sandbox_stop': 0x4b2036c,
        'sandbox_zoom_migration': 0x4b24fb9,
        'sandbox_particle_lifetime': 0x4b24f25,
        'sandbox_spread_ring': 0x4b24f5b,
        'sandbox_setup_reply': 0x4b24f77,
    },
    '185db12cadf1f6dd605cd55503e10ca795067b91d9d36bcb6679daad6b98c53d': {
        'sandbox_controls_page': 0x4b28faf,
        'sandbox_battle_fov': 0x4b28c74,
        'sandbox_marker_visible': 0x4b28cac,
        'sandbox_zoom_changed': 0x4b29194,
        'sandbox_particle_update_queue': 0x4b27d3a,
        'sandbox_tracer_update': 0x4b27993,
        'sandbox_tracer_position': 0x4b27a08,
        'sandbox_scene_shader_clock': 0x4b27db8,
        'sandbox_load_keyboard': 0x4b27594,
        'sandbox_save_keyboard': 0x4b275d9,
        'boss_stock_panel': 0x4b240f7,
        'boss_screen_root': 0x4b2409d,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2057b,
        'sandbox_graphics_page': 0x4b20e75,
        'sandbox_blur_material': 0x4b28d04,
        'sandbox_scene_graded_blit': 0x4b28f43,
        'sandbox_scene_blit': 0x4b28ed7,
        'sandbox_refresh_score': 0x4b200ed,
        'sandbox_message': 0x4b27e47,
        'sandbox_reset': 0x4b287c3,
        'sandbox_frame': 0x4b201f1,
        'sandbox_compute_marker': 0x4b28adc,
        'sandbox_draw': 0x4b205dd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b276bc,
        'sandbox_binding_caption': 0x4b1ff3c,
        'sandbox_binding_name': 0x4b1ff01,
        'sandbox_set_binding': 0x4b1ffcd,
        'sandbox_reset_bindings': 0x4b1ff77,
        'sandbox_controls_version': 0x4b27657,
        'sandbox_migrate_controls': 0x4b27608,
        'sandbox_scene_particles': 0x4b27b51,
        'sandbox_open_replay_folder': 0x4b27801,
        'sandbox_scene_animation': 0x4b27af1,
        'sandbox_scene_wind': 0x4b27b09,
        'sandbox_scene_wave': 0x4b27b21,
        'sandbox_scene_trees': 0x4b27b39,
        'sandbox_notice_attach': 0x4b2016c,
        'sandbox_notice_detach': 0x4b201ab,
        'sandbox_project_markers': 0x4b2041d,
        'sandbox_gun_marker': 0x4b28896,
        'sandbox_blur_slider': 0x4b20f58,
        'room_header_description': 0x4b20b81,
        'room_load_description': 0x4b20a56,
        'room_get_description': 0x4b20c97,
        'sandbox_move': 0x4b2029e,
        'sandbox_stop': 0x4b203a7,
        'sandbox_zoom_migration': 0x4b25114,
        'sandbox_particle_lifetime': 0x4b25080,
        'sandbox_spread_ring': 0x4b250b6,
        'sandbox_setup_reply': 0x4b250d2,
    },
    '7f887c9681e6aa9343f984bc12e795cd0c8dc669f8fda88b7891855b9a612683': {
        'sandbox_controls_page': 0x4b2901c,
        'sandbox_battle_fov': 0x4b28ce1,
        'sandbox_marker_visible': 0x4b28d19,
        'sandbox_zoom_changed': 0x4b29201,
        'sandbox_particle_update_queue': 0x4b27da7,
        'sandbox_tracer_update': 0x4b27a00,
        'sandbox_tracer_position': 0x4b27a75,
        'sandbox_scene_shader_clock': 0x4b27e25,
        'sandbox_load_keyboard': 0x4b27601,
        'sandbox_save_keyboard': 0x4b27646,
        'boss_stock_panel': 0x4b240f7,
        'boss_screen_root': 0x4b2409d,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2057b,
        'sandbox_graphics_page': 0x4b20e75,
        'sandbox_blur_material': 0x4b28d71,
        'sandbox_scene_graded_blit': 0x4b28fb0,
        'sandbox_scene_blit': 0x4b28f44,
        'sandbox_refresh_score': 0x4b200ed,
        'sandbox_message': 0x4b27eb4,
        'sandbox_reset': 0x4b28830,
        'sandbox_frame': 0x4b201f1,
        'sandbox_compute_marker': 0x4b28b49,
        'sandbox_draw': 0x4b205dd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b27729,
        'sandbox_binding_caption': 0x4b1ff3c,
        'sandbox_binding_name': 0x4b1ff01,
        'sandbox_set_binding': 0x4b1ffcd,
        'sandbox_reset_bindings': 0x4b1ff77,
        'sandbox_controls_version': 0x4b276c4,
        'sandbox_migrate_controls': 0x4b27675,
        'sandbox_minimap_extent': 0x4b25137,
        'sandbox_scene_particles': 0x4b27bbe,
        'sandbox_open_replay_folder': 0x4b2786e,
        'sandbox_scene_animation': 0x4b27b5e,
        'sandbox_scene_wind': 0x4b27b76,
        'sandbox_scene_wave': 0x4b27b8e,
        'sandbox_scene_trees': 0x4b27ba6,
        'sandbox_notice_attach': 0x4b2016c,
        'sandbox_notice_detach': 0x4b201ab,
        'sandbox_project_markers': 0x4b2041d,
        'sandbox_gun_marker': 0x4b28903,
        'sandbox_blur_slider': 0x4b20f58,
        'room_header_description': 0x4b20b81,
        'room_load_description': 0x4b20a56,
        'room_get_description': 0x4b20c97,
        'sandbox_move': 0x4b2029e,
        'sandbox_stop': 0x4b203a7,
        'sandbox_zoom_migration': 0x4b25114,
        'sandbox_particle_lifetime': 0x4b25080,
        'sandbox_spread_ring': 0x4b250b6,
        'sandbox_setup_reply': 0x4b250d2,
    },
    '62d14be53df8aede02c7c40e78892a4c52d9ab921a4f9b6ca91e238e8e1835e5': {
        'sandbox_controls_page': 0x4b296d5,
        'sandbox_battle_fov': 0x4b2939a,
        'sandbox_marker_visible': 0x4b293d2,
        'sandbox_zoom_changed': 0x4b298ba,
        'sandbox_particle_update_queue': 0x4b2842b,
        'sandbox_tracer_update': 0x4b28084,
        'sandbox_tracer_position': 0x4b280f9,
        'sandbox_scene_shader_clock': 0x4b284a9,
        'sandbox_load_keyboard': 0x4b27c5b,
        'sandbox_save_keyboard': 0x4b27ca0,
        'boss_stock_panel': 0x4b241ae,
        'boss_screen_root': 0x4b24154,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2049b,
        'sandbox_graphics_page': 0x4b20d95,
        'sandbox_blur_material': 0x4b2942a,
        'sandbox_scene_graded_blit': 0x4b29669,
        'sandbox_scene_blit': 0x4b295fd,
        'sandbox_refresh_score': 0x4b2000d,
        'sandbox_message': 0x4b28538,
        'sandbox_reset': 0x4b28ed8,
        'sandbox_frame': 0x4b20111,
        'sandbox_compute_marker': 0x4b29202,
        'sandbox_draw': 0x4b204fd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b27d83,
        'sandbox_binding_caption': 0x4b1fe5c,
        'sandbox_binding_name': 0x4b1fe21,
        'sandbox_set_binding': 0x4b1feed,
        'sandbox_reset_bindings': 0x4b1fe97,
        'sandbox_controls_version': 0x4b27d1e,
        'sandbox_migrate_controls': 0x4b27ccf,
        'sandbox_minimap_extent': 0x4b25297,
        'sandbox_scene_particles': 0x4b28242,
        'sandbox_open_replay_folder': 0x4b27ef2,
        'sandbox_scene_animation': 0x4b281e2,
        'sandbox_scene_wind': 0x4b281fa,
        'sandbox_scene_wave': 0x4b28212,
        'sandbox_scene_trees': 0x4b2822a,
        'sandbox_notice_attach': 0x4b2008c,
        'sandbox_notice_detach': 0x4b200cb,
        'sandbox_project_markers': 0x4b2033d,
        'sandbox_gun_marker': 0x4b28fbc,
        'sandbox_blur_slider': 0x4b20e78,
        'room_header_description': 0x4b20aa1,
        'room_load_description': 0x4b20976,
        'room_get_description': 0x4b20bb7,
        'sandbox_move': 0x4b201be,
        'sandbox_stop': 0x4b202c7,
        'sandbox_zoom_migration': 0x4b25274,
        'sandbox_particle_lifetime': 0x4b251e0,
        'sandbox_spread_ring': 0x4b25216,
        'sandbox_setup_reply': 0x4b25232,
    },
    '7222581ad0ae5db6fe240667c8089a065140c64860b7cb3f9ee617f39b3c4770': {
        'sandbox_controls_page': 0x4b296c5,
        'sandbox_battle_fov': 0x4b2938a,
        'sandbox_marker_visible': 0x4b293c2,
        'sandbox_zoom_changed': 0x4b298aa,
        'sandbox_particle_update_queue': 0x4b2841b,
        'sandbox_tracer_update': 0x4b28074,
        'sandbox_tracer_position': 0x4b280e9,
        'sandbox_scene_shader_clock': 0x4b28499,
        'sandbox_load_keyboard': 0x4b27c4b,
        'sandbox_save_keyboard': 0x4b27c90,
        'boss_stock_panel': 0x4b241ae,
        'boss_screen_root': 0x4b24154,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2049b,
        'sandbox_graphics_page': 0x4b20d95,
        'sandbox_blur_material': 0x4b2941a,
        'sandbox_scene_graded_blit': 0x4b29659,
        'sandbox_scene_blit': 0x4b295ed,
        'sandbox_refresh_score': 0x4b2000d,
        'sandbox_message': 0x4b28528,
        'sandbox_reset': 0x4b28ec8,
        'sandbox_frame': 0x4b20111,
        'sandbox_compute_marker': 0x4b291f2,
        'sandbox_draw': 0x4b204fd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b27d73,
        'sandbox_binding_caption': 0x4b1fe5c,
        'sandbox_binding_name': 0x4b1fe21,
        'sandbox_set_binding': 0x4b1feed,
        'sandbox_reset_bindings': 0x4b1fe97,
        'sandbox_controls_version': 0x4b27d0e,
        'sandbox_migrate_controls': 0x4b27cbf,
        'sandbox_minimap_extent': 0x4b25297,
        'sandbox_scene_particles': 0x4b28232,
        'sandbox_open_replay_folder': 0x4b27ee2,
        'sandbox_scene_animation': 0x4b281d2,
        'sandbox_scene_wind': 0x4b281ea,
        'sandbox_scene_wave': 0x4b28202,
        'sandbox_scene_trees': 0x4b2821a,
        'sandbox_notice_attach': 0x4b2008c,
        'sandbox_notice_detach': 0x4b200cb,
        'sandbox_project_markers': 0x4b2033d,
        'sandbox_gun_marker': 0x4b28fac,
        'sandbox_blur_slider': 0x4b20e78,
        'room_header_description': 0x4b20aa1,
        'room_load_description': 0x4b20976,
        'room_get_description': 0x4b20bb7,
        'sandbox_move': 0x4b201be,
        'sandbox_stop': 0x4b202c7,
        'sandbox_zoom_migration': 0x4b25274,
        'sandbox_particle_lifetime': 0x4b251e0,
        'sandbox_spread_ring': 0x4b25216,
        'sandbox_setup_reply': 0x4b25232,
    },
    'a891ccb726092cf5572cb348ffa1cd9ab092ccd633dbcb961fc55f653145e708': {
        'sandbox_controls_page': 0x4b298a8,
        'sandbox_battle_fov': 0x4b2956d,
        'sandbox_marker_visible': 0x4b295a5,
        'sandbox_zoom_changed': 0x4b29a8d,
        'sandbox_particle_update_queue': 0x4b2854a,
        'sandbox_tracer_update': 0x4b281a3,
        'sandbox_tracer_position': 0x4b28218,
        'sandbox_scene_shader_clock': 0x4b285c8,
        'sandbox_load_keyboard': 0x4b27d7a,
        'sandbox_save_keyboard': 0x4b27dbf,
        'boss_stock_panel': 0x4b24223,
        'boss_screen_root': 0x4b241c9,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2049b,
        'sandbox_graphics_page': 0x4b20d95,
        'sandbox_blur_material': 0x4b295fd,
        'sandbox_scene_graded_blit': 0x4b2983c,
        'sandbox_scene_blit': 0x4b297d0,
        'sandbox_refresh_score': 0x4b2000d,
        'sandbox_message': 0x4b28657,
        'sandbox_reset': 0x4b29087,
        'sandbox_frame': 0x4b20111,
        'sandbox_compute_marker': 0x4b293d5,
        'sandbox_draw': 0x4b204fd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b27ea2,
        'sandbox_binding_caption': 0x4b1fe5c,
        'sandbox_binding_name': 0x4b1fe21,
        'sandbox_set_binding': 0x4b1feed,
        'sandbox_reset_bindings': 0x4b1fe97,
        'sandbox_controls_version': 0x4b27e3d,
        'sandbox_migrate_controls': 0x4b27dee,
        'sandbox_minimap_extent': 0x4b252f7,
        'sandbox_scene_particles': 0x4b28361,
        'sandbox_open_replay_folder': 0x4b28011,
        'sandbox_scene_animation': 0x4b28301,
        'sandbox_scene_wind': 0x4b28319,
        'sandbox_scene_wave': 0x4b28331,
        'sandbox_scene_trees': 0x4b28349,
        'sandbox_notice_attach': 0x4b2008c,
        'sandbox_notice_detach': 0x4b200cb,
        'sandbox_project_markers': 0x4b2033d,
        'sandbox_gun_marker': 0x4b2918f,
        'sandbox_blur_slider': 0x4b20e78,
        'room_header_description': 0x4b20aa1,
        'room_load_description': 0x4b20976,
        'room_get_description': 0x4b20bb7,
        'sandbox_move': 0x4b201be,
        'sandbox_stop': 0x4b202c7,
        'sandbox_zoom_migration': 0x4b252d4,
        'sandbox_particle_lifetime': 0x4b25240,
        'sandbox_spread_ring': 0x4b25276,
        'sandbox_setup_reply': 0x4b25292,
    },
    '0d26ba11058a49490254fbcb36f19b8f7d70a527465767519cccb12bf6d71242': {
        'sandbox_controls_page': 0x4b297ea,
        'sandbox_battle_fov': 0x4b294af,
        'sandbox_marker_visible': 0x4b294e7,
        'sandbox_zoom_changed': 0x4b299cf,
        'sandbox_particle_update_queue': 0x4b284b5,
        'sandbox_tracer_update': 0x4b2810e,
        'sandbox_tracer_position': 0x4b28183,
        'sandbox_scene_shader_clock': 0x4b28533,
        'sandbox_load_keyboard': 0x4b27ce5,
        'sandbox_save_keyboard': 0x4b27d2a,
        'boss_stock_panel': 0x4b241e7,
        'boss_screen_root': 0x4b2418d,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2049b,
        'sandbox_graphics_page': 0x4b20d95,
        'sandbox_blur_material': 0x4b2953f,
        'sandbox_scene_graded_blit': 0x4b2977e,
        'sandbox_scene_blit': 0x4b29712,
        'sandbox_refresh_score': 0x4b2000d,
        'sandbox_message': 0x4b285c2,
        'sandbox_reset': 0x4b28fdb,
        'sandbox_frame': 0x4b20111,
        'sandbox_compute_marker': 0x4b29317,
        'sandbox_draw': 0x4b204fd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b27e0d,
        'sandbox_binding_caption': 0x4b1fe5c,
        'sandbox_binding_name': 0x4b1fe21,
        'sandbox_set_binding': 0x4b1feed,
        'sandbox_reset_bindings': 0x4b1fe97,
        'sandbox_controls_version': 0x4b27da8,
        'sandbox_migrate_controls': 0x4b27d59,
        'sandbox_minimap_extent': 0x4b252b7,
        'sandbox_scene_particles': 0x4b282cc,
        'sandbox_open_replay_folder': 0x4b27f7c,
        'sandbox_scene_animation': 0x4b2826c,
        'sandbox_scene_wind': 0x4b28284,
        'sandbox_scene_wave': 0x4b2829c,
        'sandbox_scene_trees': 0x4b282b4,
        'sandbox_notice_attach': 0x4b2008c,
        'sandbox_notice_detach': 0x4b200cb,
        'sandbox_project_markers': 0x4b2033d,
        'sandbox_gun_marker': 0x4b290d1,
        'sandbox_blur_slider': 0x4b20e78,
        'room_header_description': 0x4b20aa1,
        'room_load_description': 0x4b20976,
        'room_get_description': 0x4b20bb7,
        'sandbox_move': 0x4b201be,
        'sandbox_stop': 0x4b202c7,
        'sandbox_zoom_migration': 0x4b25294,
        'sandbox_particle_lifetime': 0x4b25200,
        'sandbox_spread_ring': 0x4b25236,
        'sandbox_setup_reply': 0x4b25252,
    },
    '19c17eaf7873671eac6b19e906598657ffedc616ba82eefcfbf22e172cc9b1ab': {
        'sandbox_controls_page': 0x4b29861,
        'sandbox_battle_fov': 0x4b29526,
        'sandbox_marker_visible': 0x4b2955e,
        'sandbox_zoom_changed': 0x4b29a46,
        'sandbox_particle_update_queue': 0x4b2851a,
        'sandbox_tracer_update': 0x4b28173,
        'sandbox_tracer_position': 0x4b281e8,
        'sandbox_scene_shader_clock': 0x4b28598,
        'sandbox_load_keyboard': 0x4b27d4a,
        'sandbox_save_keyboard': 0x4b27d8f,
        'boss_stock_panel': 0x4b2421e,
        'boss_screen_root': 0x4b241c4,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b2049b,
        'sandbox_graphics_page': 0x4b20d95,
        'sandbox_blur_material': 0x4b295b6,
        'sandbox_scene_graded_blit': 0x4b297f5,
        'sandbox_scene_blit': 0x4b29789,
        'sandbox_refresh_score': 0x4b2000d,
        'sandbox_message': 0x4b28627,
        'sandbox_reset': 0x4b29040,
        'sandbox_frame': 0x4b20111,
        'sandbox_compute_marker': 0x4b2938e,
        'sandbox_draw': 0x4b204fd,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b27e72,
        'sandbox_binding_caption': 0x4b1fe5c,
        'sandbox_binding_name': 0x4b1fe21,
        'sandbox_set_binding': 0x4b1feed,
        'sandbox_reset_bindings': 0x4b1fe97,
        'sandbox_controls_version': 0x4b27e0d,
        'sandbox_migrate_controls': 0x4b27dbe,
        'sandbox_minimap_extent': 0x4b252f7,
        'sandbox_scene_particles': 0x4b28331,
        'sandbox_open_replay_folder': 0x4b27fe1,
        'sandbox_scene_animation': 0x4b282d1,
        'sandbox_scene_wind': 0x4b282e9,
        'sandbox_scene_wave': 0x4b28301,
        'sandbox_scene_trees': 0x4b28319,
        'sandbox_notice_attach': 0x4b2008c,
        'sandbox_notice_detach': 0x4b200cb,
        'sandbox_project_markers': 0x4b2033d,
        'sandbox_gun_marker': 0x4b29148,
        'sandbox_blur_slider': 0x4b20e78,
        'room_header_description': 0x4b20aa1,
        'room_load_description': 0x4b20976,
        'room_get_description': 0x4b20bb7,
        'sandbox_move': 0x4b201be,
        'sandbox_stop': 0x4b202c7,
        'sandbox_zoom_migration': 0x4b252d4,
        'sandbox_particle_lifetime': 0x4b25240,
        'sandbox_spread_ring': 0x4b25276,
        'sandbox_setup_reply': 0x4b25292,
    },
    PREVIOUS_SANDBOX_SHA256: {
        'sandbox_controls_page': 0x4b29a4f,
        'sandbox_battle_fov': 0x4b29714,
        'sandbox_marker_visible': 0x4b2974c,
        'sandbox_zoom_changed': 0x4b29c34,
        'sandbox_particle_update_queue': 0x4b286ba,
        'sandbox_tracer_update': 0x4b28313,
        'sandbox_tracer_position': 0x4b28388,
        'sandbox_scene_shader_clock': 0x4b28738,
        'sandbox_load_keyboard': 0x4b27eea,
        'sandbox_save_keyboard': 0x4b27f2f,
        'boss_stock_panel': 0x4b243b2,
        'boss_screen_root': 0x4b24358,
        'sandbox_boss_menu_open': 0x4b22300,
        'sandbox_keep_replay_markers': 0x4b20546,
        'sandbox_graphics_page': 0x4b20e6f,
        'sandbox_blur_material': 0x4b297a4,
        'sandbox_scene_graded_blit': 0x4b299e3,
        'sandbox_scene_blit': 0x4b29977,
        'sandbox_refresh_score': 0x4b200b8,
        'sandbox_message': 0x4b287c7,
        'sandbox_reset': 0x4b2922e,
        'sandbox_frame': 0x4b201bc,
        'sandbox_compute_marker': 0x4b2957c,
        'sandbox_draw': 0x4b205a8,
        'sandbox_mouse_hook': 0x4b1ea0c,
        'sandbox_binding_menu': 0x4b28012,
        'sandbox_binding_caption': 0x4b1ff07,
        'sandbox_binding_name': 0x4b1fecc,
        'sandbox_set_binding': 0x4b1ff98,
        'sandbox_reset_bindings': 0x4b1ff42,
        'sandbox_controls_version': 0x4b27fad,
        'sandbox_migrate_controls': 0x4b27f5e,
        'sandbox_minimap_extent': 0x4b25497,
        'sandbox_scene_particles': 0x4b284d1,
        'sandbox_open_replay_folder': 0x4b28181,
        'sandbox_scene_animation': 0x4b28471,
        'sandbox_scene_wind': 0x4b28489,
        'sandbox_scene_wave': 0x4b284a1,
        'sandbox_scene_trees': 0x4b284b9,
        'sandbox_notice_attach': 0x4b20137,
        'sandbox_notice_detach': 0x4b20176,
        'sandbox_project_markers': 0x4b203e8,
        'sandbox_gun_marker': 0x4b29336,
        'sandbox_blur_slider': 0x4b20f52,
        'sandbox_training_roster_added': 0x4b242fe,
        'room_header_description': 0x4b20b6c,
        'room_load_description': 0x4b20a21,
        'room_get_description': 0x4b20c91,
        'sandbox_move': 0x4b20269,
        'sandbox_stop': 0x4b20372,
        'sandbox_zoom_migration': 0x4b25474,
        'sandbox_particle_lifetime': 0x4b253e0,
        'sandbox_spread_ring': 0x4b25416,
        'sandbox_setup_reply': 0x4b25432,
    },
}
CFLAGS = ('-m32', '-Os', '-std=c11', '-ffreestanding', '-fno-builtin', '-fpie',
          '-fno-stack-protector', '-fno-stack-clash-protection',
          '-fno-asynchronous-unwind-tables', '-fno-unwind-tables',
          '-fno-jump-tables', '-fno-ident', '-msse2', '-mfpmath=sse',
          '-mpreferred-stack-boundary=2', '-Wall', '-Wextra', '-Werror')


def build_blob():
    """Rebuild the position-independent code and its embedded constants."""
    source = Path(__file__).with_name('native')/'sandbox_client.c'
    with tempfile.TemporaryDirectory(prefix='solbreaker-sandbox-build-') as temporary:
        work = Path(temporary)
        obj, script, elf, raw = (work/name for name in ('client.o', 'layout.ld', 'client.elf', 'code.bin'))
        compiler = os.environ.get('SOLBREAKER_SANDBOX_CC', 'gcc')
        subprocess.run([compiler, *CFLAGS, '-c', str(source), '-o', str(obj)], check=True)
        listing = subprocess.check_output(['objdump', '-r', str(obj)], text=True)
        absolute = [line.split() for line in listing.splitlines() if 'R_386_32 ' in line]
        if absolute != [['00000007', 'R_386_32', '.text']]:
            raise ValueError('Unexpected absolute sandbox relocation')
        script.write_text(f'SECTIONS {{ . = {VA:#x}; .text : {{ *(.text*) *(.got*) '
                          f'. = {BOSS_EXIT_OFFSET:#x}; *(.boss_exit) *(.boss_ui) *(.boss_rodata) *(.late_sandbox) *(.rodata*) }} = 0x00 '
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
    # The pinned options migration overwrites SmartZoom=0 as if it were
    # missing. Boolean 0 and 1 are both saved values; keep defaulting a missing
    # or invalid value through the original setter.
    zoom = client_recon._offset(sections, 0x12372DC-client_recon.BASE, 9)
    before = bytes.fromhex('803801c645a3007404')
    after = bytes.fromhex('803801c645a3007604')
    if image[zoom:zoom+len(before)] not in (before, after):
        raise ValueError('Unexpected SmartZoom migration')
    image[zoom:zoom+len(after)] = after
    # Keep the graphics tab in battle. The existing graphics controller
    # continues to manage how each option is applied and persisted.
    graphics = client_recon._offset(sections, 0x1C130AC-client_recon.BASE, 6)
    if image[graphics:graphics+6] not in (bytes.fromhex('0f858d000000'), bytes.fromhex('e98e00000090')):
        raise ValueError('Unexpected battle graphics tab branch')
    image[graphics:graphics+6] = bytes.fromhex('e98e00000090')
    for address, target, name in CALLS:
        rva = address-client_recon.BASE
        offset = client_recon._offset(sections, rva, 5)
        target = previous_entries.get(name, target)
        if image[offset:offset+5] != b'\xe8'+struct.pack('<i', target-address-5):
            raise ValueError('Sandbox call site differs from the pinned client')
        image[offset:offset+5] = b'\xe8'+struct.pack('<i', symbols[name]-address-5)
    from .client_battle_settings import (EXTENSION_VA, NAVIGATION_AFTER, NAVIGATION_OFFSET,
        SHARED_RULES_MAPS, STANDALONE_NAVIGATION, STANDALONE_SETTINGS_EXTENSION)
    from .client_tls import THUNK_RAW
    extension = RAW - (VA - EXTENSION_VA)
    if extension + len(STANDALONE_SETTINGS_EXTENSION) > THUNK_RAW:
        raise ValueError('Battle settings overlap the pinned TLS code')
    settings = STANDALONE_SETTINGS_EXTENSION.replace(b'SolbreakerBotV2!', b'SolbreakerBotV3!')
    image[extension:THUNK_RAW] = settings.ljust(THUNK_RAW-extension, b'\0')
    navigation = slice(NAVIGATION_OFFSET, NAVIGATION_OFFSET + len(STANDALONE_NAVIGATION))
    if image[navigation] != NAVIGATION_AFTER.ljust(len(STANDALONE_NAVIGATION), b'\x90'):
        raise ValueError('Unexpected battle settings entry point')
    image[navigation] = STANDALONE_NAVIGATION
    # Restore the stock runtime assignment in experimental builds with the
    # earlier SmartZoom cache hook.
    cache_address = 0x12466CF
    cache_original = bytes.fromhex('8886a4000000')
    if 'sandbox_zoom_cache' in previous_entries:
        cache = client_recon._offset(sections, cache_address-client_recon.BASE, len(cache_original))
        cache_jump = b'\xe9'+struct.pack('<i', previous_entries['sandbox_zoom_cache']-cache_address-5)+b'\x90'
        if image[cache:cache+len(cache_jump)] != cache_jump:
            raise ValueError('Unexpected SmartZoom cache hook')
        image[cache:cache+len(cache_original)] = cache_original
    for address, original, name in INLINE_JUMPS:
        rva = address-client_recon.BASE
        offset = client_recon._offset(sections, rva, len(original))
        def jump(target):
            return (b'\xe9'+struct.pack('<i', target-address-5)).ljust(len(original), b'\x90')

        old = jump(previous_entries[name]) if name in previous_entries else original
        allowed = (old, original) if name == 'sandbox_setup_reply' else (old,)
        if image[offset:offset+len(original)] not in allowed:
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
    'sandbox_controls_page': 0x4b29a4f,
    'sandbox_battle_fov': 0x4b29714,
    'sandbox_marker_visible': 0x4b2974c,
    'sandbox_zoom_changed': 0x4b29c34,
    'sandbox_particle_update_queue': 0x4b286ba,
    'sandbox_tracer_update': 0x4b28313,
    'sandbox_tracer_position': 0x4b28388,
    'sandbox_scene_shader_clock': 0x4b28738,
    'sandbox_load_keyboard': 0x4b27eea,
    'sandbox_save_keyboard': 0x4b27f2f,
    'boss_stock_panel': 0x4b243b2,
    'boss_screen_root': 0x4b24358,
    'sandbox_boss_menu_open': 0x4b22300,
    'sandbox_keep_replay_markers': 0x4b20546,
    'sandbox_graphics_page': 0x4b20e6f,
    'sandbox_blur_material': 0x4b297a4,
    'sandbox_scene_graded_blit': 0x4b299e3,
    'sandbox_scene_blit': 0x4b29977,
    'sandbox_refresh_score': 0x4b200b8,
    'sandbox_message': 0x4b287c7,
    'sandbox_reset': 0x4b2922e,
    'sandbox_frame': 0x4b201bc,
    'sandbox_compute_marker': 0x4b2957c,
    'sandbox_draw': 0x4b205a8,
    'sandbox_mouse_hook': 0x4b1ea0c,
    'sandbox_binding_menu': 0x4b28012,
    'sandbox_binding_caption': 0x4b1ff07,
    'sandbox_binding_name': 0x4b1fecc,
    'sandbox_set_binding': 0x4b1ff98,
    'sandbox_reset_bindings': 0x4b1ff42,
    'sandbox_controls_version': 0x4b27fad,
    'sandbox_migrate_controls': 0x4b27f5e,
    'sandbox_minimap_extent': 0x4b25497,
    'sandbox_scene_particles': 0x4b284d1,
    'sandbox_open_replay_folder': 0x4b28181,
    'sandbox_scene_animation': 0x4b28471,
    'sandbox_scene_wind': 0x4b28489,
    'sandbox_scene_wave': 0x4b284a1,
    'sandbox_scene_trees': 0x4b284b9,
    'sandbox_notice_attach': 0x4b20137,
    'sandbox_notice_detach': 0x4b20176,
    'sandbox_project_markers': 0x4b203e8,
    'sandbox_gun_marker': 0x4b29336,
    'sandbox_blur_slider': 0x4b20f52,
    'sandbox_training_roster_added': 0x4b242fe,
    'room_header_description': 0x4b20b6c,
    'room_load_description': 0x4b20a21,
    'room_get_description': 0x4b20c91,
    'sandbox_move': 0x4b20269,
    'sandbox_stop': 0x4b20372,
    'sandbox_zoom_migration': 0x4b25474,
    'sandbox_particle_lifetime': 0x4b253e0,
    'sandbox_spread_ring': 0x4b25416,
    'sandbox_setup_reply': 0x4b25432,
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNrsvX9cVGX2OD4Dgw44egclo9aKNtrVcpMpKydtQ2GQ0nHRcRB/piWm+DO8V6ykBi8jPDxeNfHXbrXbbr7b'
    b'PuVubpqiWA1gDJYVKquoVFRUF4dtR3ERsZzvOefe4YdZ237/jteLO/ee5zzneZ7znOc85zw/VQP8Zf0m6sw/'
    b'TFUTQ5IlJJlVNRQKZainbzIY5IOWqc0L9ly85GZfulhV1IaFNxtDtV6faFFKkx/+q4FVTJ8Z+tzmmz6zyi1M'
    b'8RVUWvNuNrIvp7iUfDOrUhyq3GIVYh1q6N6M0FHB6lBZvRAbqWS3T38YYgAi+yjTBYRzDPM9kI+cyBxDjnE+'
    b'vHkmhTYkn10SKbeHJJPNF7CxapuPOYKKI5hjyIBMTskMlW4CBOaHl4TWJZHekHiCO8x2R1DY5BP2+JTs89Nn'
    b'6alkqvW3GgwF1Z/B01XQUg8JMG8SlDIqqeYWI9s8FF6ZMhyf3sHw9B4SV5RFAZbtEFv77iqEj3/Xkyy3C9K/'
    b'lDXDtxkMyh/TtyKRRAhTnjILeyL5ZnpXVuLT9K4JYmdwSkQdQISR/DRg3bU2X8FbiOUaM84GedVCAlEp9kvS'
    b'XuXtDKBrO+33pgLQCESUsb3ZCyOIQgKSXlOXDAm/MAohLyQTfBA8hT03cQXLwTdjmkrKXXrmvFnwDL2J72rW'
    b'5VBIfgsBhqnibxeejle8SBnyiSSMVTzFKlgj5ZZ47opjqYlWNjkxTog1JeqUiTcaqVAtp+yMtfulQuWPWZBp'
    b'JaW3nqSWRQoX9tylZ8g7H0kQIeXpu6aoh7/DvCDQME3wvg0l9XvT8Qs4V/YgfMqXY8S+tqPyZZOS9pCwtXJI'
    b'LfOOBwSQA28G/Pq9k+EZTZh9JIufAl3wGegnX7asiFbSrtEiyG8hgw1iIt/0H6xLZUxIbs/Pu4an/scDVZpn'
    b'YZvHEx+R6sI2YEq6xhQsgLGapwFTeiFTMn+QKVNCtSn2F7Dwgjcak+gmfOrsW0D2/NnwzHTJLRa+LRh63OQ9'
    b'LXirUdA/NwqFb6Hsm1j1fEOSwTB/w0D4MoQ2JJ4D8a7y+oTCF1AOis6sBZEKbVkbXBLJ1/lQ9iuVov8gcBJz'
    b'q8zZEjrC3Sp3trCUEUpRLwUDQkcUZ6uy9TwKa8ooFswxAp66+lvIuLMFpbGfsMfROmpuXi8mb8BcGAF5DCAH'
    b'1ZmAxCqzdt7jPSr2LXG0Khn1no5RKyJ23lMs/x+gFsvbsR39EUsD5WX1VOLQi99C/qDcmew4NOyJoQ3n25ZE'
    b'2o4y93mvj5ryzTxzFJD8j+I8PxG0AuThTKg0CVqw3XE+3HTbp88C5kXNjnrNwL4M1doQ6xq5bCckGCFZbEc9'
    b'ZX9FvSVsrmS1qHqsY0ZAijx3vItPS2fL4rg0iGVZ+cx4IdYAasfMpqVPZ7njZ8kl6REGg3GmEDtmRJUbWveX'
    b'rpJBMYWnxd6j75thDonnbL7C09NnQjZIn6HqYB+62DG5ZRBz1KMGavGDgsGkYx0Ncsdcqa/f0YLNtNrRYAv0'
    b'th2SO6JX3MQdLe/EoPo4qqQbWf2FC7ajFy4AUK5MKp4c1UBhrN52tDq178e3XrigjI4AwsoaQ3OZgVD6Niju'
    b'Bu6snxSq7S5JzK8U1P51gSFTkRpc7H12FrIFKOyiInuaPjAUVCVgIADOQmZDx7ijvqcSPJaASrABniCHg6AO'
    b'bmd+m0/ePz4CC1TYH8qBRWzkjkb72tUJfQwG0RpyNMr5jX2kgM3XfBbQWgVrwQxAFKYcEoZVLI8pw8I3n9BC'
    b'1ll+DyHDanKbT+Z/1SpkrbHC9zzugPdYR/1JR73nsEXON0fOY46mk4ubPs2vP7n4Ky7VSwsgSWFPYUQy5MQe'
    b'FKfBp9+hYhUDe/GHO9WKRjPwnj5mtkQHM6ApMUviPvhOYDMSTdwRhNZpZu3MXa/uvRP7Lit312NfxWc2smBx'
    b'4fVIHSjzwtXjoWzYsH6L6gEyJPUBHGHPbkJhF9k59TtQT803XdGUM9UNNyELf38TsXCwvDJOgKa3IoJDVZcj'
    b'J9T7Hoc2PcLA/FKk/D7oKr+jEdUlc9TpAtSgrPnuWUjEUausMW3El8PcXcva1cWQIubp76T0eylbthBakzqZ'
    b'AsQI22nuaFK2vARgNVmDRZVj1RH47wgeEgZHhsF7Edw/DDaFwRUIvvStDo4Kgw8juCkM7gXgkt6FkKdpUIRR'
    b'M8XIsYF/4+c41PyeBxLEjmbsgbj7MOT/b1q0AdBhKVuOI6HnUIM4GgLlyhYLlrRdZYSjrPlMK5pQqGDK7iYI'
    b'WqJFH0hFp+gzKHp98xNYC1vUZ4nC2E40SCWIaHcTWl3zJET7HUQ+j5g1zNmkXk96rulCPQi18LZRyaj1dEQL'
    b'RYOp1htHCetvRI3vbowZraRG1AJS4CSrhEQ+v0RV4X2ke8of6EAnltgBerZB2TIAi5Vu4s5GFs/SzaBQ0i0s'
    b'3QpNl6XHcWcdS4+HmKVazML1mkoY2wvw3C3MHRTetirTaj2XIE8bsdan1bKZTcAOZWYLA5mN48LbWUbFhble'
    b'EZkcGLjQ3RQj+yKUydfXcgqXO4Rnpgtvx++/D6KD1RK1fya82I9JacpEY/I+lEi5I0Layt0t0IKYn020cHeQ'
    b'TbRC5UlQm+3Kluu1mvl3B/G1n+eZ3glilOzvHYiU/Wb54GDW3qV/TmSyapdaPAjaQNVmeMotZuYMqmX/gRo4'
    b'yzoUMIySDVFDPv2HCfQZNEgutbjY6dBxoEBE/MlDDWRVQX+QnsSKm1APHilKTy7KSK1y75uLn2BnHlMrHiVV'
    b'JKcaUVGqB7HbPwpcK03F9gnGicvKXBbmMjGXmbkGM9dw5kpgrkHMFc9cq5hrJStdC70oKy2mZyo9Z9NzFT2p'
    b'nZeOped4eh7ExEvjMMFSKz3j6Wmip4GeNYTzLb0/T+/b6LkJFZVcZtD+/PJu/Y3x4YjLh9JzMD0T6TmKniPo'
    b'2U7PID2X0XM+PV8xIq0DRo3WdF0FuSZ6j0I/ejqwlFWw44rn4E2eSHuE9A1/0gQm8NdKQdayZyLtvaWP5WcG'
    b'GcQT+zE6toaguvQCVBPYE2IlB30ahO7YPPfPiRgcOZdfgy/2Wull+1NmKcq+3CQK7BSrUeeBycieMrPlpml6'
    b'D696rof6r9wMT8gQaP2ZZni4LfBwWoXBVcLgz1uFqfhfBV3AQWHwZ/D6Gb62ChN9wrDVg6BTWNGvVZhVD/+1'
    b'8F8T6CdMOQ3xS+Kx/xh8DLqL+JnwyDDBI92smVHXYbL+G+EJUgiuRbhbbuJjJvNJWcyUCJaZibtmwI8ZtJnq'
    b'Qr1bc3tKImZPFfbWVqhWeJ0+GR6LW1qF23zw8lfT78nNgc/3uCsJsgrZzKqB3/WYT8AoSqDfmRXwvj6R3qce'
    b'gX/Ifdb0dHhphn9nsFWYcQowXFnwBYWamjIDwl3j4ZGSAWH/hJetgyn2jAqoQUgwdvpseLjmBhaymiG14fzd'
    b'BmnfdgYIHMbcxVOMrA8wP0O1fAArreMg5oyTAGyAwJMImAuAIzrNqXUAPYLQ+ZgaJaIXDAuxcBE8HhW9hyDg'
    b'RsRaRp04fKUAflYA/huFVMhGbBrwP+s9eMk0syAbB9yJXYIZnwf5jJ0Q16NrJHMP+kS+uhBFylHHVxfTS33Z'
    b'ZCDPt6xFVW+J8B5izsOiTclvCO2O8oB0KaWe6QXYH04JHeXOw0rpshtqDfCulKI5wy6GjmZxR+20+5GK1Lss'
    b'C34C/wgnPUVZlWiWm4KhvG91d1L917XYRbfBkyRkPHPsVsfNxQ5uNzulPvIf1HNSoq3NnxIfuskwiNWz5SvZ'
    b'LlQXOlHlsfaHmyeCzpafGmEQj0M89XGKJb4nP2U1SJk8JQE+rKxjSGVBx2fQpwprPoUO1xsiPvodDWSbOvzQ'
    b'92vqzu9oInWQkpAD2Wjg7kY0XnSBdTSh3RJ4lKeMkMsTqbk6XhHn5eSFaud5fU/MlMtRcRiItjRRpw5mT4RB'
    b'p69T98uIh1g/mIjGgj8MIeOo+fZITcliNC2kdRq8uVYVvAdEBt1cy0GbpsRfOHpzJXPv9HweIf6SpwzipWiX'
    b'ycEIljJIboyQ240Q97pDckWkemu6wdDm2GnqgWclPGsXXpT63Vhgz/7snZblJnncymSIkiD2Lhi3EjOP8Jjl'
    b'iZ0EPOc9QOBWgwdodZKIU/frJKKXU0Qr5DtlEKso+EwGRObc8UqEmJCTwB2vaEw0yc4dppzYHt/mtpRBVqFw'
    b'ISQqPzUY7N85qHXbQV/cPxMzUjQR+ZIyFKQlmbsm83FZwE/FUcbGJ5qgK4pL1FWNGuhAg6CMuw+AHmEpi5hr'
    b'GXf4oLmwFNGfgrof2FyUkgxN7eXnNZ3yx02k6Kq05j71sDDs/TwB4FteJHhlq/BwBcZJBYRaYfD72KaFYccI'
    b'548vEo2HTxHc6lqmtXzrum0Ut1kYdoTw1m3XvgH1DLZyEZEXURlqdckBu0UYfFLXZac0xRU7E0QH1MoMbPfO'
    b'JignFhhtNCxr6CK4uIM/APZZSWlOBc1kXaNpqdiFyzR1Y3XWoqqHUCycA4jHjluE6kUMXEsMFXtDp/5v6I7A'
    b'xxIGq8LgGi7t6LN85YXqZTeZDJLF2yYJ8lPDDVJ04Mmye9Gljyp7FfPrSgCvxcwuDjlir8h9WG9wmM8GaG7w'
    b'u7gRPY0maGgsCEInRvKUwRnQGNhZvTlAW4BuDZz3zgah8l9Rc0Djp+CrPzVeAkuDLQd7YuUUahTcuVM9Nsdg'
    b'yOqmHbQOqXgAdkjKAOyQPiTlZwVnQClajJ53qBT9dLT6ZLTstr4ONsIkgJLf7miB1JaEB70iwoNeyZmh0mQa'
    b'05LbQ+KEHgNeODA3JVRaF0S3H2LHcwf4wXZHUOoHYE8QfWxIDRxid9MUbUQM6gw6mj5AdD4FB66BMPBCc6JD'
    b'kgpuIob7xEb03Ws1Pb7vRrLApmQqb+FAifqX/qhKX+tPo2Zk6njn0ghM62ts8wwagwkPZHUbA5pN4z4juTKj'
    b'c9xHmXLXJL4Z46p1gVAoq0Ab+AHVKV3LvYuwoy78bhUNpyzDj9TvPJzopNirpF0KpZKzSlknbqVRtblkl+M4'
    b'nJp7uYeLlskls0v9KhYt1FZ4KmkfyO39VvRX4yBZJpmnRzUuvNk4a2aocaEvXimMxJEqGmsIR32NopZ1j3ry'
    b'jB7VsCgcNQminr+/MyqNEqjLKGo+PNWXz5BFXBW187ZbjOCQ16vlALEHpVvABq8E7VGvzsUMdcDLmsvaCxah'
    b'edEqJBruU5V8s4tdVq8lwomxaHAn8N78mUEMHA5Hg/rAGfKQlG04jAliLqt3QZbzouR2k5Q2RbCmmeSWeBqr'
    b'SovjmfHh0Sto9iGpIXSM+dGfeRnVdH7DnULR8+SMgftfrzY9BV499tgFD1AtPcPHmtJx1GyF/IxZyFumgNnl'
    b'bAT/Gjp6oKVebg6FfkqCWd6QNEJPQyGvVBoSkur+a8x30M9FSSVvpDLBQCNMwPRLAvLGDB2AamvWmb7B+DEw'
    b'XbN869WVLdR3P8gqNWsXjOX75bczMBRUyRtYyIDVTr+SGdrcanyT92PTNIgD5P1r6SUGK2hmcyworbC8YPKr'
    b'KHkGT/UNVU8+ec8n3ZK/GKDko+VvzGwn9mh6dJdLfYAijxewx2pXc1V0cliV6g+g19pOvvTEdvsGVcvZUUZv'
    b'0xQ56e1PjNNBDnV/gM8083yLIn9w/B+mKa5Q7SxgslCIVhifYoUcvNsKGasGciJ8zGihVBxBtga9GV5Enkim'
    b'BYLHKKX1/14dabdCDWWA6hB78UkWCOKe+stUhpt5+ghwMq/no0dQ5kpTNz6D6NHsHFD+4G7sUIJYSed0aZ7V'
    b'dsX4iUsd1w/LnNVPk2UH5NzKnCqX4tGQd1jIb4yjMSgw7Ru4IwHkmzsSQczUiV/rWVf/7wzlJ49V6VUaKS0E'
    b'l3xkikkCM8oMLj9PHmw3gqXmaJHLqQfmBrH3/aA2Hlgx2p4ymDqgJWex327hU0wcTLPBvDdzjWcp6XysmaVk'
    b'8F4808RSJjNXFk8DwAxwZ9WbsM2mJHJ3HXr4UjyXGnHYCYrgTuT5LUyKg64zatkB2cilJuZMYPkWdLslM3MP'
    b'kg8mIB/CtaaO6YuM+F1f8qKh5KB9wElDdjha1Hlf6bJQ3kwlTQw7aFbpOnDgR2aYpFjez54+WIoBtIzB6l+C'
    b'kLN8K2RCkWtnFhohTagLSH/adC3VKwxlyECFBTPwoQUzAL6KVWD5LzLnduxNt6lffkke92nmj/JUrgaL9HnW'
    b'rt7RrAvSKO7YDhyey/tSpvpJQwB4qz3NIv2Sb0sAdntPQwcVqawf9bflkfYa0aKsX3ZqNb5FAMmTIB/t6kfZ'
    b'iCbdxB0von+2DdK1cud27ni+W0cbauRpaC+cYu1K/ivqnzAD58DvbVebofIEq+MV0hprDtMAFppg/jVoDFPb'
    b'W4OWMhg7bEsjDdiCbtmCIL4Ov0G7WMEUGJ8Ypx4AsiFpGyQNGQjVAtnasDICQXQ3kTJqR3UENvJ6QJYPJnXL'
    b'ZKXJEO4DeL5ZXdAH+boCnuqfmpCwBbXClChDnIRzZyHpNjnfgrMRa1AH8eQRPH0GtKo4RZ79DfDIKMXkGAuq'
    b'EjJqDaFjeiqVZsMV9af2wVT81j44qVDQgjqKOT3skjq0SWskhVHLkv8J/TV1qo5CNfdrUrYDuRN61lVd45oa'
    b'l+UyaiSsSoqWy5JJNQ7tbFq/xPiJqN1OAeur8WvvJOR3IRTslOqajAa02SAO64xxq1z+LWpNqb9cPoj8lmi5'
    b'nEaZxRiMdkrNsRsMYXTB+xhl0mNPsQje6fjOa4w0nqVjv3oHJufhO0m22sRfcGch76U8S+JVC+L1LIlXLYhX'
    b'WzhbS7UhJK+I83ltQmFphG7Ni6JcTg6TeJNglUlyZBKLUhILRyNYSWBE1gbisKRDv9bKDXKkSv8mmUPhAD+A'
    b'5AN6MmcTidI5FCXbIXXp1ygf8Yw30ZSWqE/jyeUbyCss7EOD0IXqMWjiIPgT0uGRmQq2KdjOmSPgMWEUQxs6'
    b'bbzi2AGfacmMzB9oH+5acISxfdaBBqIh9bB7B/0wDnoQojr9G6KcvQPHZnZ2jUvBp7Sb02AV2O5do1T2XWja'
    b'CV4cbPMeFQqpR6gZUqtkhOSOR4QiFA0a1zmsjengyASOkEw8JAxb34BOxppoI5F/bYeWjPwKEhz8LeTxb1re'
    b'rZu136ntiLaT0KYeAzonhMGX4fU7eh22tZHI7afw2xB3a5M24gLpzvimVZh1AmBv6bSA/gyw+q3v1BPOrAph'
    b'8EcABu9papFKMxF+8oTeadE8pnYI5zTNBRj/hIDXMWLnwAs6LEUmQr2tQxiGIzZ+BAW1HOCACN9NBfuAyL6u'
    b'kz0DAAjHcRbiNUgqI5Yyjrxm8vOak/LSK9rvcxqXYnftNGiiUNNEMnY2g/nxM+YR6OVByq+Vy+dTU+w3JSzU'
    b'IQjK6mzCoVr5LZGmNn8FYllGYlnMzpFgOmv4zMMgklY2AzTcqSYSyh4uDGiJL6EDVrO+oC7mGp4xFPr8ftxt'
    b'CRozkrnbCj+pnYZoubq5FzoDf+yFUx8HPahtWtW3PwVF4n4+rGbM6vGv0HZ5Xtly/Wb0FkStYatvNyP4RXhV'
    b'ttwGIerfCLCTACMR8DwBdhNgHAI4AbYTYA4CnkTAzFcIICL1k+pjCHLsULZ44VuRykE9URPAJMc1o6Oyz468'
    b'fhWyRnM1/hVRaHOMoB5o/RSQfb+I8zShNzEaIbuUmYVyyyj2det27tYZrSZkQU3RKzspH0wNvYkFi8K5OJwI'
    b'p8I7Pa4og6fZGAawk1EJFB7W4VovoU6KQsUN3QEpbrLSsOdH3b3sE42bVSyoKl9qjNy6hRi5jEr1SpjRO7rK'
    b'+ajayaet/4d8mgQA+ek4YUUEv0dxlGuVwCqB5h8zocDV4hhPfrlBHGU7yqkYiuMEo9bJNq+icXb5aWMIHElj'
    b'5+cN+ClXm4yEBl7Maa5o+YA09xLrC9XTX+P0/sow2I9Z8QMM7BEcxjsbenOZtm7A6VFkKzCq23RlV0enM+k3'
    b'JmTSnfDMVN4sA30GrMoA/4cpO+EDWOX4GFOjD+9ueEYl/0U1gouM3MdvZesJ5NvZSZ2VhLg83aQFZ0bhhCnN'
    b'lFZpEKos5n3lG8qjw1yw0oqjSkLR/Tg+BLKjSQ6g7EBC9FS2nsdS7v6MGtB13GnmmykZoqgaoWNsvowTM6eF'
    b'wjasaC1rb9Zitk9mRgV37jVgG26Cb4G9uR1+Vli4l4K924lQIzzB73paKMIJb4288kKgMxFoBPFQq+BT+ozH'
    b'StaO2L3FYCi2RCbL7RYpqEXnbyEpZZoFDC/igiJaLUNOKJvPwLv9rBgzuiRVaC02xQTOgSTqjNpMpZxgNdYr'
    b'LyDekEpjBfvI3i7GJJeMjwhBEoC9GXMApc5QvJijjILK4IQcQ+goJzYqSm2YmVfU7u8isHYnR+gLIpizBWp0'
    b'+WlakwDmfdSG4R8aqC5poERb/oCexVYTlM7WpgYb9YkMK0EfAajagDCwTYLkd6nyJ9Trb66gMZHCI+IMMEmz'
    b'dCuabXgRKW7YQP29CpGEve9VnME+oDLcBygbrLPKsCc4xE5q4+axMs57Q+lw7i+oxw6qdZ+SF9GziGBeP2JE'
    b'M3ApSk+LlZq4qq4/hWVUoVhRCR/vRdlHy+SXaAamjaDuNkIoGoBR2nFK2g1Za1FPLYbgCSP0ryPzaOba+y/q'
    b'O1psR9Vt0BRCK0Yg3UrsQFrUfsCLqegDoof3ilHw+gyd1lbRi9TX/12bUwdsiMo61M2LsM8BupspAJwwVSkl'
    b'xsqf4hgDjhTlA1CYcgh6Pg+tABhWkZsDHzw86T8T+CTjjL/89IjIVmGor9jRKtkVR2uOUeG7aeQG+FSKa1HU'
    b'cnBDp00FgzSaFyEjARVj4WIUqRcjkLDH3Uo5adIUYyeDuSmxisfBvwX+B8H/cPgflVhl+Pnv57+f/37++/nv'
    b'57+f/37++/nv57+f/37++/nv57+f/37++/nv578r//SZW55vdqmv/ysUKqg6AE/1y+pQiK+bizNhVd5DYgY3'
    b'hmjQkFXgQgluUEpp3XWtdE8owwSgYSWekxA86ra8W0cNzIsZZRQHjDJL1wX6yuq1+2MNZoPwUuXyGEXeufhT'
    b'IzsbqmVBdkp9egAQzDcr8oaDmwDabXkA+9Dl4g+ZWVW1ozXERyd5fVJvm68Zd8lxAzuhlGZ89gKmHlBKd271'
    b'RNgzkqVGxd0qV6xSZEODJ2JSjjHnplCtJ78Vciy9CxmswDyXDfsjxnqzoCrjq9UR9iyT9GrOr0LHqFAv+g2T'
    b'01y/8Sebxo8e85vqZLOr4DJOpEtmXoRzuIEBF6oNs3HNDC9qh3wEIvlYKxsdzcfGcbclmY2NY6P74tqNx2PZ'
    b'6Gu4w8wm3sBGX8cd1urRtxrZ6JtprV7XapAKZTWOf2uzSw3aYu4poVo5Pyiw9hWRtIK1xdamPBSSL5ryEhSL'
    b'lTnrccPEQ7Wei9F5wsK2mGRFHFjL3fVQpK+02QR2kaYSWDC85MQFJevLqiYpBTihzk6GavnMbw/pi1HklhHe'
    b'o+LDrIodV9wHJyoemqk4wtwHlHJt7obzF7953AQ8bS4zcPcB5vCBMNzc5jAbxV8octKR5wzKzIZJLpxI4A4f'
    b'/p4MnQiYJ+H6T6hlxXkwdKLHXsDJARAx/1R44lYK/tAMdsx7SLo5x6Rt/bMYLF1b/1JmYMXQnrmxBXbXmMl3'
    b'iTcShrL1NYXGnvcfxIFekA2eMsN2lFnhp2AEIkrvF6w04cKtvMqClWZDgsEgbPn+ptiqM5CZ6sPwBFYMp/WQ'
    b'Y8y6qNXjoPT4RDM7pmz5D6SmPnUI177gMsnPEM5TzMqWa9YZDLaQOhuCEKZsuXcdrnIeDoHquE7gFAKOQOA9'
    b'ncCVBByFwJs7gdsImIxASydwDwFTEXihJgz8JwHTEfh5J/DfBByPwA86gf3WIzALgWWdwF8RMAOBL9XgitY1'
    b'hdsMBn9q4tClK4dNYimT4TVpaV6SQYhNTRykjE2iaJMTBxWnJt6JsRbX4CrF8rixcsgsfcrlA2Yclpf07XDP'
    b'GTTmKVt+Cwm9AzVrUEd2Jj4RE09NNKmDAQQJxY3JnbOEyT4z5SD+0flJ+NsvZU7u3Hnw0j/JwNr19ONsbZCD'
    b'6znGPuOHLJgSlbRw5vpBUH9uSlRrMaRw19BogyFNvhwlNYyVq+JY+6jh0p5mXF4hHxzeTQreUlEKquFJjZCm'
    b'YyqYI4iD9VumQl5Vjx+nLYLKlrz1+oRJi7pIg4HUyTiRo26grVZapCKMNK4LwYxrYZZ2Q9iGCLeFqb6MVGVc'
    b'yoKEB4TBbxA4Tgd/W91JbhCSS0By9AoSeUuJ62kI9J2JBN24xYfUDwO+/LURF0F1qK2XaG4J12qdAnn98B5c'
    b'80EJIkW1DoO1lOj74CWcOqZMnKJM1OiZyNMyoS3tkzOw3Nsu0RxpOVIzVMsm/NG2FIl9bb55F1KeTn9E2Oxj'
    b'fiq7CpmZh7vBQkLhRwZcoIIY5mRK68v1uKMVUSlvarSWGkS5C6KUpDxNWIYNBoNP7RXGgjKqje+GEWMI8RlC'
    b'HAiItWp3vLJOvM87uirDBnjqCxDE5KwIYucIuTyDFusMA44pVBj1PdrDJt7ACYm7g6q9A6OUxRBrmIxY8kFL'
    b'98Wrz4NamcscG7hjA3Nv4g9ZacaTlPxOAVR8/vPcuaPsAUjJXiGMrbH57DXLc0riens6aqUJyjKj/cPlk0tm'
    b'9DYWz3xOrlrmuSTk9S1xP1edGrW7Vq5MCAwsQfhshAs6/Ih8JoEFAzd68p8rkOIJ8sazAOir5L7DWj2XrsuL'
    b'hJjFk6N2Jwc2Vqf23Q01YGvj+ZtKAGQ7PWJTb9CWUAlV7NPoywAu1gDJ9orlp6AUivP5rkl9vZPx3GeQelGz'
    b'r/KMMEiRNh/1O7ZD2sbiWH1jcTIGngFVHkW4uJsLtx9DLzSJZ81gxcsgGVYsmrXZ4r8BSsFI1OSC98+oTMaZ'
    b'Ci6iPtc2CrJquSrTFrJf1FaXz5Of2mMQl3meChmkHE/5m2jXSHP4mtmklESzsMe1D/rI3/E1c7sgZQC5n6+Z'
    b'3wXZD5DbQTNjTupxw9jNtK4hCMFxfA1mTckwsi34Mo+tSps9tqAKF9o1S4CW1WOT4Icu2gwrt8QxR5PfoaYt'
    b'mvPYPL+jBRSb45vJTyzLho9zSQYlJYk7mxS3WuxoEfakRdrrhU2+FOimcBFWuxpdRbs3ubtRvbMdWvOlGOkD'
    b'ohuO902x4xzEM10R70hlZ7yOCxgvSvpAPhjXTTKdjar/C9R6tV/oWi+DOWpKViPD/I7g0iVJBtmXBL3Cpm04'
    b'NXmuZDWyiU8aLsQ6GgG8HcCIEOv4wO9oWL48CZA+4Y6aHKP6F0iRlyKfbb6pyroxG2hN1SafGKPEbVTWDhyO'
    b'usc1IsMWAnxVRGzXqByj4vwAv+fCd9Y0nlqebDvKnTXygXLMEu5zH6vegWUJRUqf8ZQsCvWUr6TATZVqf6Qj'
    b'owxVNEYyRxnu6wZjRVmzbRNtJ1LWKBvwZTsYJjxVhmo5iBPre1EFIamR5VirwsZKtaYtFNK/ZFoWtjf1qdnc'
    b'NaOi0ayMj4DOYbdiiVBSIwZy9yueJwbewpw7hU3QXRzgjoPCnkPMuQPIqRMqcOEjvB5glViutW04Sb4TGBKJ'
    b'202dB4Q9yf1GxeT1VUwepbAXMgVJOGu4G7J4UO0H8XMMndGdGN19kNYH1ahbvguFpoUcvrFyvs8seIeglEJf'
    b'7qxBPbxM55ca+o+2K4fAPvNIGcvEUyZjIb/AsJQZyrrsDST9cZrVpKxzwbfYS4PLxAX6zN+AlTZeq2EfRM7q'
    b'uaoZ935YuGcVdLi4sdepCnszzMLe5F7KqAjcJZQftD9pzs0oCKEl9kw6q6j4rJeyNsYCLRrYLwXtweV3yx1R'
    b'YpKwx86lFvsF6Ub5coRkEfY8YRxZXghkpb5yvgp54ZLKLFvGJMsVyYFqKNhBS3jvBlqXbY1oXV6CZ6atDc1b'
    b'5mhg7sNKkSYGPqVo5CaN047D0AjYORWPEcGFYo6ahdVxxnrbaZAMqABeuA2tB+aoRzGpRQ3kqOHOBnsVsA8U'
    b'1CZfNFRMfbG7TthzVN10PhTy5NcZhMKXNUx71fJbeOoLjcDaX3hWPmCQrvGsjLhFSY/A406ctdRS6wJWpWjj'
    b'Bmxwddqmt1pIgLt9uDWg3zsoP3XcWYsC0KCOhxSyINOEccD+/E5PNAq+T6OEMRrexkYPoXVqFCDLl43SIPx2'
    b'1Ctbt2MPV96F0NQaCqWAWhC8sZAMmEgmwXvm8pVL1VEtxNFGiyb1V29h5EZWHd76d25IpdxxT15vW6i5zoBl'
    b'oMMKoIMPn1fgbtEPKwhJ5p4bj4LaxqMmtf85NMWsuAtD3Otto34B9JaQGuRkU+E+pAS+i7pfWys2cDOZKE1g'
    b'otzc2mWy4Hdsa9gc0r6N9I29OHxrS2K+OUdnp5zVumrNqGEybXKWaduzbKWNgE1Rvgc3GbUuTtv7DU5SIo8Z'
    b'UlFwcfe6wkjwNlbjusQqm6/gYuO7AIjmq9M1QBW5WxU8GX1ik72mszvkWVb7MSmKJ8cFrrGfEkqrhD2nuale'
    b'x/pMO0EI936/r67DpWinps8M7NLs0xs+QU39K3i6JqJZuvqgUTu8wUP8CqKHsgNr+OQBiIjMzCH75x2EvXtA'
    b'tyabUP/Jh2mLn/p6GNoHt/bLdRr09+H4t1HYI3i4gDeM+ThhNmiYSxFTboKPzn4PlQBPjlcvaVslfs1dCXxc'
    b'ov295TfwO+VKk71aaoWPGB4BH2xMe+Cc3Gxi4xI7NxC5cPcYlG6MCexvLN27WLridloHrWlqbmTHQhmDcQW+'
    b'3tI8VGPuFtV7WV+ko52oIJcfpoWjv5DLqW2J/eXyBnqJlsubaEF2DBnD6u8v0zr/EHOrfNMljKXZyepSrRxx'
    b'2paPvjw2IzQpCcQcI4PTY5JqSSH9Em0/ECkLiU4wxxCVsfifxpDUFKrFraUIvqJVvY/bHfItXMLiMjyXoIUU'
    b'iv196MjMYKV428Rx3G22d0h9eUoc/PwSHKt+gUE8xWqPEFuAf4Ev2pLHR4gxmrU+MBgKTcADDhzBTN5nCh5g'
    b'9Sxqtelm0I6BF3scoIKnTcn7NX1zDNIHfuzvwQ8Gfcvb1BA3tR7WyqR+RwfhaMzAY0ZyEnAvnjqUPCnLBKn3'
    b'23jaRuCDHimRFYQNHyUWV7OzEno6WoCdd8j7wxW0P1xB+8MV1D1DHWo7JK4U4eEAyp9qiYBql1rE2/lIb0i8'
    b'lbu+1evrHcCz+yWh0z1pV7/7hrYCDLCFgKUCogLFs+pmLI7fPlI0QQ3hsVlxbWOBm/0076pdrcZlhSNBlclV'
    b'ZnuV9Gx3o55p5wnQMRAJe7VTLXZhalUc+1Gh8xwBwYuLnuVnLBGaxdr9sAE+xWLzgU6J5GPNdhnHkiTzyFLc'
    b'MCAOZ/TNOapOPtbil82Q3m/J3fqW/CXgHm43qlH3/wvbIBqgXG4hO0bbdBDBzqJFZKDCc/k8xj2rB00HwwD3'
    b'JX3xL9zFFe41fSex13wXntpyRDfygc6vUEft0crYrKuaKvSYtqbfB5phYRk2ORVD3yfV30KLEUfcF9ak6oNl'
    b'xH9SrsCcGHSaTJjrSs310s6WULbGIblBZfremujwlokYbECongO/0F21rU12wDy3FwseT0RpcMiMhF9rQWiC'
    b'UfdhWVAuRxQyhc636N6eGmaSEVn0cUvYK0Yz4hTt2Gr+Nwl1t+GBP9aj+v1rPQ0SJYFFwJx19hLUwUJhADsS'
    b'emc1uCMKNPG3UNtK+fCBWH7IaLXjGMQ5rKwZVIqyW6Os6UcvB0scx+T8wwZwBBrRK2hwLVoqFjsapV5Q6t6B'
    b'OKCGNoq7QY3E4vbWi9oH4e4a9RuoFzbWxAEuarCD6imCmQkGwlyvbOkPSalVBLbI/kF+PDsmFLrsOObJPwaO'
    b'1RPk3kA6deAsl9JI1hZELh9F3SYQuBnzKo+gHRt16hMYKKdrtamlMBRTmANw8OXk8SSLGA78p5M/AEfNC3Sl'
    b'Ykf0O4lMRncyyBz1eoLjKQe2kDLWhpyxgV/RyAkXWDRpwZLH9PIiY868iaM85Xgewlj0C94HWwc02MhfUm6J'
    b'7oOUfw8K44E3NWEspMNLNtBzLT2L9e6UGfRSZ2Ju1r5J2Va2TCcaxVQ2jKzmvkn7tiELYOv0p6aJhJQ1s0ux'
    b'5SIOHo5Fu/fqwQhClKikqA8NOYZJdHbgAJ4PRlnTjStx7yCeO9ZJGxIEhe44g4dCoILcZ6Y2z060vtZ9SxAW'
    b'c2tHF1u3YQM6vRvZ16LpGmo7qEMC8VSIYsTYRxhBQ/eGU6/mNof1CHpC9bqm0HQKtBJdqTfvbL7iVLeSf6LW'
    b'WP9PTWvE6adoYZ+W3yQUrjZhD6Pad+kmRV/oYmkHVPN8E5ZWH0SxtannVNIjdhpJEaPAtf/qAUUz0UClkJWG'
    b'tXMa1ellq5RkawvbgdjMd2G4OgD3QZDNGFb/qgFAzZ9FYqRYabiw18p5LcaSD8Oz4rO4ikZrdA3ndfQVj6/1'
    b'9Doouia6trkUI37TX748QCh6Ht7fwROiWBCPMWPVimc25u4EGBz2TbuQnlCYF6ktRz4EPoV6DrsiQX40kqQC'
    b'cCewA1QiCU9IojS1/lpNAzO1eSTlcrh0TVvKM8mCNwLy7ymngTRpeiAKLfnfhU1byAH1ztpAm1G9Hjc/dBvJ'
    b'i1G/x4gLuC9BS+1JCG124/JqcK7mduIBzYiuwTu1+uueo4F76Du+8/vlr7tMbfze8nW3POirzwHU3EI19msp'
    b'vnsGF3wdtssDY+XLgzEwI5wLdXwnpcBt8uUhIpT9LmlY9+i3deYFwoXUcC61VPtCYOAJJe243N47z5yxDw9j'
    b'CmTKl++UonPwwBBDRCAd+TxML2nYywAC6pGvejKy4qtwIQN/UtLel9uj8m7C6hGvQ7qGZM1aOnaOjrQUCku7'
    b'6kw7UUPneCtumK1Q0i6xSrk9Ii9JWTMAS5vfxI5gH9vIC0kuki/sXp94Dx3ORmdWsU0EDsRA5YtRbRVG0JPQ'
    b'XzqbVD/2WflN8uVIKZbLB2K0xoH9aHrgJnDkpFs1aLIShwa0YWSpLwaHMj4nPYUhdFadNyR4cfxVSQvJ7b2E'
    b'NVX03g6ME4p2EGsQVUmtbh8pawS20FjI7pQdmP/yV+BZ8ZVZLseWaBAfUVLvtYBO1AeoabiYmCvEUntTc7/s'
    b'ct/02noYd9j6PKvuHXgLL7X0IXdWGX/vwAzk24wvO4X2FjBvpwauF/ZCEhnEdqgAs0GdclY7TvQXOKpMyiNg'
    b'1aNcC0ZG82+uPIbyZC0qrE9qUWGhb5sA7Zfl16t30Oi3Zk+IedzdpD7cFAqNJFtCfKzLs92Hs4Hfc29zDD/g'
    b'3Q7/grxbHJ2HT1uIczSKdN+SldMRW+VoGjV/gi1SU7uCd5Tmg4Jwlf1Ns71uo+4BE7O1MamRekDI5P4vUG/i'
    b'WYW2Njvpb7GPfugiKKnj3DUCdI2zAezMu9/RzlFNymDuutBR7q7DI2CuI3+bXTzWGO1o8LZJ0beQrSmkXoDe'
    b'tBe4N5eNK+7neociToA+C8cct67EnmTp38k56o/5uBPyQVs3THlD9MFdxeQxBgM3ANFEPHuE6EI+tV6m+QKp'
    b'UvlylFCExPU4Op/34QjR95icgdb41dn8wWc6m6VNzS8Ywow0Ct4NJMZxeue+mug1qYWfo5lpl7HFiMu6V26y'
    b'4X+o3CnhVMWh8KlVLdtl6qpgaAbdzlILW7I95PHBD1EeJ3yodaCDGHTmxTjbCjU2G/r5Gew4q1I2mG/0RtoH'
    b'ShPkpxMNYvpCd3284qF5103X7oaqUPIbJtGEQotBvAksA9shOb8+Tinded0nRmFrhVJoppPbzmbggVLOBm2W'
    b'uHO7me6Tujj4jG98gJP/78CTtuFb1M9f1vfg3/sq1fa1fIOFdsiKfUdmxktmbGmRtO3egpa/HFTrcdv9tB67'
    b'/GcQ1QUf4HmytMFf5S+TMY7bwqsyozyf4jY0zWPzs3r1/f8Hof3QYCBHpxJA0nlqyPrJF2ZtC5UF6fpjP9B9'
    b'Fi7pu6h+83LnLirDgGpjJm2j0shrHk3O/0ONjFvZ4iGF33pPi/fhZkEWXBHBeylb56P74lTVRq3MN3KnKu/T'
    b'3KrfoyslWTQdV6/+qbHHvifzD6yvcC2ekytOW7p08VVDH1yyIjtXzJ57dYRwqHOptDz7e6Fj5jy6MG9O7g/E'
    b'pTiu7CXLF4gLViwQn/jx0AeXuJYsWLYsO9e5dG7PlO64Y8bcpY9Ki7OXiMtnLF+66JHc7DkLs3N/sxzw4edJ'
    b'SPyORxYs+cH1JT8U/9GlS8TcpYuW3yEuXvZj61P+a3w9caP2iDAajT25tHTpcpHeJmUvWjoHj1g0jF4QZtjo'
    b'Rx+Vcuc8CtyZLOXmZhNiurRokRaaOmfxnMeIHenZcxaJ84leLiSv04PaoXoZm7vgB8vgWrrIlS1KyyZBNrJz'
    b'e8LQ57sSLyM3e3m2uDwMmzwn97FsMWPBowv1yJ0wqClpUTYhTl+0YEm2tCwB35Nsd941/O577h1hn/PIo3Oz'
    b'5yFs2KPScnHpYtsweMceAvvsqB6CRo8H6QHPBzNxjANhmQ/S40F6PGh4MAs+s3pK0oK5cxdlD8sEQV3w6JxF'
    b'Y5auHNbF8p+AN3rxUmmJ+KN4XRVn+Al4VxC8Cl5X9f84PcC7MndXx+sUop+G14PoVfC6RPHH6Wl4/728XQL9'
    b'4/QQ73sFvgpeV7P4KXg/IX+djeu/5I/w/ju9rib6X+QP8X6S/IUbuuGn4PWkeBW8q6qLH8D7Xn1A+0+ZP2fJ'
    b'Y9mT5yxZmDlnkZT94+v7QHFKqQvmLFr62LDbbhvWI/YYSRSXLtFoTspevmxO3hKN4Pfi6KHhCKMfWbAIOo2M'
    b'OUuyF+l5mrR0UfZ4KH/3787cwfeYpcuXd9NiXX/jl0Jph7keIWWQZDD0Qz/jSrjt+/C0RU/gsiKD5ftw29Xg'
    b'KXMWJ/0A/Kr440GVYIS+V4HbrgJ3IYOuAncuXZE99yrw3+VpfWafK+CT77ZfFT4u03ZVeMaTpK2/z4cFudlX'
    b'yz/Cr5b/lEXZc66W/zRdeVxJHxsjFuzKehm7dO5V+Qzwq/J58vKx2XNyv0+HhOUq9CcsRdH9Pnz0AhfpzL7f'
    b'gzsXLF9+NbnKnZudm0Sw78NtPwC/83vwyYuvTn/yYuTQ1eDjFiy6Knzs0iXZV4NrNRCGG36s/XQr/9X+eraT'
    b'BY/NF3+Y7pX4WMoFi7I7y//f8DUj5afTf0jS7EDLT8QPGwU/lf7kDA0W8xPxJ2SvFP+X/GC7+l/wsb3l/g/1'
    b'pUnTT6cP7a2Hvvjv/FwMmuqn83P0I4vu/F/yA/h3/RT8q9jPPexJs44H8LAI0190Fzwsqj3qu9Nu/j5+WPTo'
    b'r3cXvIfd2Q1/cvai7GVLc8Ue/AU4ioymnHrCtXR1AenbBQ+LwJXlClf1lfkJVyn99eqeT63qrsyn3k3feWV+'
    b'dPhd/1U+fsz/Wpj9xCNLwfv8/+u/dcb/MefxR+LPW7qCkv6xcCTN78qYOEluwYPUTYmZU9RVdDOG2Ytnn5sS'
    b'lbmJpmlTswTvacN3l/g6PK6qeQy8yi1miBCXCI/xiaZM9bvRtLizebAz5ruCfAuOJuXeBi+40Dnv5tncYfnN'
    b'am2tS0Zoz0qcJfowjWLMaX7sxD3fyfmWZ4TCASWhUHMbC4VmT1HnpWMwrRKpdrxkmOPJfyk8BNa86sbHv31+'
    b'tvrI5lBIKX7zjGi0O03Sg9xt4JN2eo+KD/CNCdo4zN1Kce2aZyLtMdLtfBIeXpWoFJseKkTA9fPk/Tgft1Cy'
    b'EtFpBZXXGOjQv+FzXmie8s2vv1NWwLcnFf/1sZqd5ThW8xY89YnOmQfZMTViM11igYNoW4GgXcaLAsR4XJdU'
    b'T4PV6h8240hIA6MQgEYFja8ZcKZLKMzGkb/0ZbiCYxq+GkMZCcxRh0EP0aBg/Ty/43Mc2YzKuGEjTpDiur3D'
    b'uALLWSfsKex1cIvBkKE4Ps/gjppQrWeEQbRo45ZncdzyMK5dko7YQrY2ll8rX44SEyCq7SgNeGJ0PJNXbjex'
    b'Crw/6cixrwO9bb538Fwj1nGLa5X40P7eNEkRJUYqGXdNwistHE04KTZsE7AivwlXnE7FQUyVuw/b3S3SdXhQ'
    b'GOYHsteVpcjo/FrISS99SjXFapfx3gVpqa1tYfUg+UAk3r9gEF2UmrcNEst8oFtilaWdiU2T7sDE8g/b81uk'
    b'WyCxm76XWF9lwvXEAmQAJhsp/SEQhWzD0/F6jDB+vQ8HyVr2aYNkQwEDj7c7huyr7TxQU/BW4pDtxBB7v6Dj'
    b'8jO4xPYfWI90Visez/MS1WptFI1EQhlLkWVPJxq0GVPFcVwYXDR3Gy0cRUViOwS1SBOg9fpwNHMevnCB59eM'
    b'xrt94vBahSq5IzrvU8XRYDua7D0k9nmwZHLfE8XWN4yBIBWtEM9edRyWfRHVqVENCUpGJDsLbx+nw+MTYEUt'
    b'c7co7ob51+CAKw51XnFREAjzF2UozMEyFGYzcwf5Bro3aieeP62UP6oYDFOACI6Gt3h90i8VWTu1Gi9aekAp'
    b'fVKh06qPw5dtCk3a3qaUFnUBB8n5wQildDRA8noppTfB7/xIQ4Q2xdujDh7FfPjnl4WXJJrli3OZs0VwnLW/'
    b'gQdNQtbEuaxK8WTc9kgEnkkd5M6WQpzBPcRKECGQJuytqGi2AmJBVe2KRyJahdsqup3UV5OhHc80tcisnUJu'
    b'SszBPTTHgBQooHSpF6OEel5WUM1qXPJ+7dDLY+Kvb1+NV3Hsw875/kh4yxu30Je1z0STwhfO0sUkQyoC9/D9'
    b'eEbi7avphMQKb0i86f6XEDu+bC7V9JB24fWKfdsBdOGsYhkRiNKuNDmlLRVvDlwOhfTkXbwM1/TYTtsvLR/J'
    b'2ofUH1GN9QtrLJHr8DYpe6z0C55rtvnsj5ukG/los301nYhECx4DfZdHKVnGwLvMH6iw+XqcFs2Ou9TmPVjz'
    b'bXuw5hPk/em0mukB0DpCIe1ge4+vG6+vKkOiZY8i5cGyCCJcHkMzES3YKdorwzddpVXu/zpSm9S3nxDSTrxD'
    b'hXLXA2taZV8SSjXN9Re7P8a5F1xuC5DPAXKhlZ1ALGUsAJq4oy4qfMWV+wuQ30l4OFgdX4dZzDFG0ZGntVcu'
    b'wuQbh2ra/iGsNE7rKqDNWJh/yEcFIT9kZUXSkEom03qL8iSabfAefVpQVmOm96mRWh1OYRVYD+rRy1es3lUr'
    b'3kQJrX4Tt2Qdtx2lFWqDcCV5jXr/RrooCJr6xwZtHuWf8NtNdeANbtCRbjhAN2L5hDtqn+ylaQy5bCet55Lw'
    b'jE+z4sbjsguqku54D/E9u+m8TDwyMqsX/L5Hp9bjoZ94pL6njEKz8PaNWGcLhqkYyUfgWFxll2PMiZykbNhJ'
    b'C8KAj8eg//F7wlfFBO5U3tmoUJBSvFOL5a5XSpPiS/FiDTxutx5PC9eyKHindpsqlPfTydhC4bPhzZE0WRjP'
    b'SyZrjBdZNV89V5st/DUugg8vBaFoICO62BTjTVdY3VDLttBY21F7OW2nPMsLy5EUrkCaXqMvacd1SXNJUGmx'
    b'CzsrbO5ceXFPTVfu9EVotqPYhCaqL2ygWYqcbudAz+K/M0Mup/ApFmFPZVv1t9J4nHkey5+y2i+KJm9IGrXQ'
    b'N4hxuhjIHTRWCXuqmUwXBAXVB57FadegXx4P37hWqOKMdcFRFCFjJStNH9htvSet2ExljoOaSO/HFU8G5tzJ'
    b'HLuFwudQFsdY2TG8964MF8ZpPWJ4xXJZQRXiY78t55dBB8qXW3lKHHfuHFKD847Xen1PD4jqIb/QqWi9XA30'
    b'hBWN5uigPSVeKKTbhFLiuRN6fW8bj8TlXlG4gyrFjBP7B6F7REafw61EKSbcv+gTH7P5oNL0aqJpIeqv0uX2'
    b'R/IekPNroldcB6B9WuelV+yFs9SooZm36AHMcSBZi3zhLHcc0K+2gz6NO33QRe3WzrbD8y3lpywG8Wlc1r+H'
    b'h0JkBJDyjFSmPABVFT+Juw8qjsPqfk5XiXFcCF43K9DLdsjmsx21tYGm6syusKmioAMtBSa1QMEw34VHxGuU'
    b'jK+wS+0r+wbqGUbxg7yMs2h5GStXDQJJE7wl3dd5VzRfL3/WW69MS3QJXgIjFNJJiw8l4M5M0crd5iHvyR2/'
    b'FNZsoM5dWxxKt0sd+8p7Tly+8CJYOCNRgIyi5cLxm49rd8kEpoUnV93hNeH6pOpE6OBwWhUM630GQ4IBbxJx'
    b'B/H8XnZKvVwVnlhNvHCcbIGbiZycbzYIDoeZpSQEYkbLF6Ekud+7nK51Jy7V+3anvqfFjIuRamj18ke4g3Pf'
    b'+s4tdLiCRP34IKRVHq8t2qDlxrQApHNjSKgt5ZkEsZ/tkH6EOu7z+IjINSC5nPXh3WSbDuKln+I9NENP6yBx'
    b'IYKWSKqOVbgL7xOk8wmVjMF64K/X00ZFuo0vJI4Ei/Uu7QI+PI2XO4JR2hmMsUV08mIt11F9olXYs4lQce3K'
    b'pHW4iN/RkoH3gK7OmqZxJHx9YzWrxenlbpqp68bGBE/HXKkPfNP6H8fHtjdioH3ymS1t56pTb/ikEh6f7pzH'
    b'HKeEPY5TbeeEPQexFaTe0DCPI6xOSTGB3OPdjQWoBdjZ4sk3fKx1bD0XA4PCUt2vY+3Mep0OM6WF5N54XLat'
    b'JMBTPcrpJtfL2poHWjtU1qBVzadYNV7EKlhp7UBQEZ1aPdbEFaSgbFmNS9VPKcDpF5CCXcaY2s0OWkQ6/LZy'
    b'hVn74lb9N8sKJkaV2LfEElG8NioRQMmBf9sOKZvoveR5WzzUafEgFLhoKWgLKXtMuEqHEpErI0rWjmyChHzN'
    b'JmHPoRJLv+KHeuugsfJnAgHMcmie9CHfjPmMws6TU3EzFC+m0HliaE85vv3vyKnhf6clp+OZewcoVWVL+91Q'
    b'yPS1GoPo3FzHbshrhrkkwha6r/BezJpoHiuHrNLZwGMlY4y20yMK79KgKfIlhE6s+NqkjIkwXmKOnYqjvHhG'
    b'3/LRzPGKfNEuRnH3zsCbnvxyY97t3LHTM2KmdIucvwO3u+fv4NIrrduVMaA/d2PYSmOC2EsZE0mfu+WD47vd'
    b'0Jnp4mNMavbf0AbLpRUsoqSUYgWxi7jMYPha/ezSh1m7thConulrBJ1yfqqBbWsE/q0cajtUTr7ZxV7QhKVU'
    b'YayUjpYU1Nc5odR3oZ6lKtFQLf2kj6HNBQv8D1nwGF4zHWFc7DC3bu8ycBp3oIHz5Q40cHBNOrgDLdo9CjMZ'
    b'HQqPayGforXNcnkjrQxLNokByNHXtKSZpGsVR7nCUPA3fSWbRmB1FMf1SpbbzSAaPmU9crpkUyRmxHcGJOJo'
    b'SVxs8eQ0sw6TPxc0QDREsUv/nOdPe3JmQtkqdM6ndu6iUIdhZqtH7EAdVsVq2XvspGrgxEcrO6ltqMaLOFpK'
    b'QqHpNt+sroX9avA1ZPp3r2kuD20uUHZtuhutbH3B9bkUdXcJrQt3Bu2XoP+Qq+PU4+9oewRPhzVH+D6XF4ne'
    b'34geXkOh8Hs3Aifc2nWetOwNMl+ypkwzoHdrPiDuBlRKc9bRalZtk2BjseMcLiAeAWnjVr9GGjxQ8yBhvgZP'
    b'FJ/nB92TVO341DBNbo/O66dkwMcnBjCo8a4kaHfSx4E+2iqmXb/cSJRx4WvpQ5CK+jlU4hR2VqN5yzvdzduF'
    b'h2JQAD59FQWg6VU6dMDmo87BqqwV4oA3JXFG8DZ7Q9/dlBw4p+Q32Y4a27nTXGKJLbZYxxQeks7h1Y6sXn2S'
    b'UdfU3cl8GglX8VeRQ8ng5gJbFF6DTEqiDSk0BpJGy6cPK7umYc7pDl9hT3I0LgYfawQDyu6okXopu1KpXHXo'
    b'w5casH+JYcgtdlbZxXDPYCVyMPXtUGgqmDV2J8V5vFucekz2eHE4zq7OOFaIs9Bv4c7D0zyrjA/ihr5df+oW'
    b'sRkjvqBH1KJ8/BaeCSAf3EYWbdMsW5uSlgSVdBLb1hklzVbsqK92nDbYL0h9qh3104sdJ6sdp2bqFN1NaaoT'
    b'6RkUZ71GcC0QnIpjNXv9jnqXOEe0teHG0bG5S/2ORmkZSExaEsRW3PXkOB227zyo+XtpZC+CzVerdhRhT1qH'
    b'y99veQt3uEVKh7Bc+YdtbcZ2MMcUGSsUrEtF/oS2+e2GMOhTb/x2OJV2ZJE2QHVA2OSoWXg8piQ16bfFDmj2'
    b'ZcZKTOD3WgLMD71dDVQMXYVadgB3q4u/9OTXGMRBEFIV7p3rruyd8XqCupC0m51VV3+Hphwer+1X5LaNtIKt'
    b'Tl30nV6Cg+qcA/qOzjpIY/IB3PWKO1EolX7CHneVFpK3hvbn9RG8tm7WjhrxCshdxYBXNHfaRsM72OKVgk13'
    b'kUkr5wcN4hg5FCXej9eQZiV5T4u/tvkWBm/EQ0UOGY+XxPUDsTffF9dHih4dOOsZ1ccgXgOKqZd0GiKM5ckj'
    b'gFygTK4JdSqnI9Q3rf8r6qdtf+2ysVpy4pQtdcBi9faCUOhh3EyNW7dTbOJIm28frjNRtkyGYPZp62v+SAxA'
    b'G8jZmmNUOzzaGs7+dAm3MS/Gn2KiIYkUc897n/F2RdWKSVfdAM+J5AaqyR59hdpmme5YcOBW7Vj9Lg/ouH43'
    b'2J5uES0Zxlass1hTYo+Fby71nZeR4GF4gnudJO9LwH1M4q+FYXLc7w2G3Bv5tKHe09K1wp6N28nPvQ381Bls'
    b'eCR5pxWToqyvzzJqdDtPVXGBzTxQ2UbXcVyHl1xaC4ZG4nb3gII7/KIa//5XPGHlS1Aeoyn1CS+j8kBEN14j'
    b'uli7RpRLceizou+7OIhe8HmwEmoRqQWRAGRNRsysQYh+Hpzmj7WLH7KqEDoUPk5qH1nJowDynJXOqK4S1ryk'
    b'eewvJRGgUVizUQMkwP8yq35x6X8g3lcIAOjUs9pH1uykVmFWozDspeF4f+ksCN6VSETOr4D6mIp3lU6dD9me'
    b'mp4I2P/Ubs7MSh+OF5wKw/gIjDUYYpWOolhHVvwCcL+A//cB7QvtPs/YX8H/LFMgEr1q/RZ0fWhHveb/UPJu'
    b'/D9d8uLk/Tu0dX1NeIHSarotdnV4CIAL73yFy/Wi3tmtj1wcoDEs3LQnFKIOtl9kzkaBbnJXwGYvfdGIrVR+'
    b'ng4s77w3UsbBLFr1icePN6n3XcbNH4fwbm2IfUfFkxHiPLxrMBmP/S7foV3HN6sGhRErT9YGOkAy6XIM7WNN'
    b'aueFqrd9hPADBMdRvVk4IrIGL14VewtT1tRto7EL2fe9bYRq5HaUHut2lJ6hzFnDS2hnC3Q5h4TC83gWxX66'
    b'PIMdFwq/hE8aXcKh9dXLtLGLm3m+eYi/IIQDeCvi8MrMw6Fa2yG2DqM1vxGBbKnjW1YSW9aIPdmyZlUnW0g3'
    b'3/Id9Y14lyGOAqlPClJfvg5zlGNonkMdIPYLKM41fB1uyIHW0/6BJtOOOryopQXYClm6/e352thQVUHoWo1f'
    b'i/HmEULCqxSnEi/xzSrjvYt5A4TBeGXsCeBe6Xjtgtn39g2OwS2AwuAjOHagX1j7njC4Vr8rkk/WBk9B2Ge9'
    b'lKFFauRblmlJH9bveOFZ2s0n74F6SOMOc7dLahsB/JI+AntOayCzXpqhETooDMZg/Jdna/fXynN/30n5ufna'
    b'HZgvpWu3tnwsDPsiDxvzS4s0ucA2cZAYVqfxEBvwMm1w11kT9eL5j5AUXldZulIj/5yo/Zau0pJp6LzKErVI'
    b'I40zArkGjdykqNkXPsIOa90GbUjrGrpWx+qsmRSV9FoEqjQLqrQ1i3Ao1r/8PmHviYp/49XBpXqJ/agLO3lx'
    b'Slc9Ffptntjs30dZDpjDlcTzITXcQ1XgX1b3EaZ5K9VaLCY8lXsAZRIyvFArJSSP99w4INJaLYvxyMxiCMW7'
    b'hOW14ZHu0Aldyowkvt0vQlS/fBH1xTfwdN1eYkC3Guxp+eJNK24WrKsHoUx6huJzYxKGpViZK06IjWw+ounD'
    b'jeaIcKO07k+g96lrNmg1B+4iVXQHvL+RSO/WPwzWfl+N137/YtJwzmp3/kwFoc1C/I1WgmedRTpxGg4wMPYp'
    b'HOxcbu66i9S6cagWSrcaW7QapVuNS5K0gA/wYmOrdjMP3pBsfXWQFoCaZxxq4ne1W6djI+nyvi5PofpPqDyO'
    b'/UlTHg5V4b/ahEdctXbuoj6NO2N88js+UqZOVSm9exMZZHjHRJO65GntIodz6rZd9AZApTQVUPhejKI6wwi2'
    b'NlUCFJ2S1A+JvmIwhGnpMSdj8td1Ek3ZpR1ABMnOhBA9cmfqwfww4vXdUp/flfpHYQRl1xrczNf6BuaAkkXr'
    b'd3mpfvkDxBIx5T/lh83mv5VqZrOqnuyKk4WR/tAtkoyRcvK7TGZVfbULfZYYVTJ2SSCyZKxZ2XUcohX6kEBl'
    b'NwJ4CZU6RCcADiyReOwNnEdVla14bYX6AXz5Hf/CNcKg1RV5wCZyr8SkJMNU3FEqa2dahMcpWfDCWVysrs9H'
    b'nIVkit3nFPe/1MpV2iWZaR241WeLz3YUbOqRRcgoaSC54PLlvpLFdjRcMZvRk0L2uRvUff9AO7VBSQNPpRFn'
    b'D7E7lfProRPFC2n1KFIvnlrUK9AXKvdyX8HhaITPpVw6jNe4LlB2fVtKPc7Dcn4tOHBqKc6FskpwiB+SxvG9'
    b'iTTLJOz1QXrAiAkmBo7ABAt3NNgddWKisNdor5X6e554EBffuxsqPjcr4sCBgdiQo6GgAtd9B+o0oshc7I7q'
    b'1Y+e6l41ApQhS85v7AcmodTY/P4VN6aCaSnvo2KwKu428wLcACXGVI9eYmSTC5YGzNWjzfgWQ7c8ln1LV/Bp'
    b'U9/oiOcH9UkttFEefx51zpPP08hNPFnkNTRZhHeeMKmR34sb0Hrvw/G2gI0XpGI39T42OBzRUjKDcsc1AlUN'
    b'7nW8CSTjH4iyz6QNpOJxVPqsQK8gukCxkhO3Nhak4ylUbHwMnn9jrfgsDvczZsR0bm2cHKNvbbQdiq5lnFI9'
    b'yXr3pBjEQa1v+tNWR9tRsASCFy6wE6wamdiiDtivbWp8EDv+A7swwQl0KWcppqNtvApTWrYfB1brSZgK47t2'
    b'w3W83qlfIjqhytOX1MO7cR4kIm+Ivj/O2YjjMw28UEbuJV/YvU/bHdfANhEkYKJdUJCtvbhN1xHeDzciWrP1'
    b'0KZND/wCHc4bNWjXfrhR8LVcZAQNLJK/McuXe+fNfcUozdYwFVP1SJmQfifsLUyhy8FLE/X5rVg5nXisnQVE'
    b'jIS3gby0OFrrhILqn/+OOcIRYHX+PhA88oCaH7gc3tAWZlLuniv2p9n/gKMUv/2DNlufwPBu2RZoC7hbpV7Z'
    b'+iI29/wmtXWlNgI4IkobTq63ncZRZ9IrHu+zNOtFVu7mSvlSf2Gzr7BSvMXbJv0at7+3kWOn4v4a3PkODXz7'
    b'StwQlRI4jLMVf6Y1GS3auFzyCm2kTigs0SoL/FHH33BCsC6SVUqTleTncFvk+H23Yj2/VLF8jF9OGKiZ36AI'
    b'OnJQX8uFFlAPg+27cORMTGD0a/NVyzgkZvAXyuMs2kwT+J79pH9zwqflJtLt9l04jCvewuhX2YUDq/twQwO7'
    b'0PoqbqRdi7gc01TeGI+nUVyO0bcvOup7XtQjrK4mMM4xFuv39Qir3wDYyXw03zzoH83jjnNokjScdDR4Dluw'
    b'nPOgCZ9cHPw0v+Hk4nM8v0Hqv1DGVC3sorG+eG2v6y1EFRqSUozjPiiSd7fox2Vk4j6+s4oHByMy6MzAf2nu'
    b'7v24Q6lD/QMwOGDDZKTbFY5llDuyxSjgnHSrftvRQ+2hEGka/dsKtLUjFmnTcMdjUn89JAdo76NzOOrkjnmS'
    b'yXYo8L7cMVeKl/MbDGK0Zxcy3BhY7iE2GgM5yphtcrsxbzKEG6/z2dpkX2+5Y754HauGt0B/itbfX7gLKwjV'
    b'VZq9OvdMoC9AvuqjQQItGm2tMo2Ba6tljXY/ZYyCtAcy+XlLZ96fvhAKNT+A1dSRJ6yhWxgp6SivdmLG6r1G'
    b'AhmU3GJJYLuKMaqWWuBa6lnESGUMY7IWQJJDvYjcESHGQBvYtQEggZtZZYVqtrVVnDHJ4N9hcIww9jgrxVC5'
    b'3bQiAnfjberKlyUAnMPyNG9FDdixWPBu04QFqknZgGc+Yi/ylKTtLJN3aHWubCjSQ+ZIqO4QbGDy7GtQ6WXp'
    b'8xgZ2Nfs2m7Rzgg8pxa+pjdePHbmEME0AVY/eZW2kwHOO6glg+rHO8OAdLAEqAdobgM3SxmzEXmbIncswd3T'
    b'WKq3+5DKTr7QppUyMJBTAd+O6oJ3K/AvzkBF7CFSm+TGS2KMXAPFfly8MfAbuWOReK1GlFOUikYTdLjRtXJN'
    b'RIYevQZkspnMgI5coXAW0AmsUlbjTXq47/rxgo71uCjOzKihBHILOnAmXbqZl+K3sKf8S9IASq7RfgEr1Fgs'
    b'IyTwYEEHqm0pVm9hnt2Rt5JYJRV04DizNFgPsB1VUo2/ZVaWYWIZ5kCssjqH0o5AeXuxq5hZWMyPoLuehGeT'
    b'uFU1EzKe1fzWt2hbhjXvldcCq6/jyq/qsk00SzUjEmeptmOXv/lFeKr/WQy9oBdfpWm20y6u4KvifQUxPuWb'
    b'EbPia1OK+u/FeFMqwYXYwvvRaJgqX7pL+kDx4vv8JDwaYMumMXSbO8ZS1k2GD3W7B0cCe1xTSTY7q6ARsH6Y'
    b'uapfbKIhI+wd2pXScjQlP8nV5Go7zYuNCPeBKMClHyLCW7n6aS1iPqjAIk0tasNci7rfYfZw5x1m+ebwHWbC'
    b'nlrpHv0as7oxndeYYX7PP6NdYyZa8Lay4i04mhKIEfbQS7F+f5lavAXzpS74QldegQhbj5N41d/iCjh/Sikt'
    b'QaHNsNDV19Kd9XJ75NOP4KCJluWiVzDLEwGwfofmBIKqh3w/wKoUvIy6AdrLxIA2GljPTnYulGuCIGUrjlJC'
    b'lNQXGpT1m+A9Jy5TtT3TjeVadl7fiNnZvVE7pFh54SBW78mClgwUB6pkrQK3YgWqdy8KHwKAJtwavIpeeTth'
    b'Yg5OBpME8Mj7UV9KveSnTFGBPvfThfJm+Hh6RXwgC3q6yfBukCbgJcJSuvyUxSiNyVSHefAKwR0QPyN0AmQJ'
    b'31zha5m9B5DuZh9mLHxTvIIZy+SUZKg2K02usuL5CQU9B45wDs7/LIpRHTzV77DtSFbajRsVbL7BmAmO/0Dm'
    b'lw9avEfplnD9YDj9KLTwve+FROJZJLE/R7v3XfE0BhJwb/s57ar4P3+rGcbqbMT1Zz+rzb8VtCDv2CUVm203'
    b'PioKsblK/TKH+CnFFFQl3HaLsdvtmNo5Y6qZ0r7m2fCe4vPqvTl42yF3nmdVwOUn4o2iLVORl318C1443SYm'
    b'QjFG5ujnC11Lm4rjLusdKxn19TlYRhxu7Lq/8C8bMJXX8dmC622Z0kg2nJU74/B07811+Al8U/B2eOZtwFns'
    b'vy/AaqEQDeyPMvh9Rn3JhrcJQFJ/+WKk1JfPMt1+Y8ElvCR5xa85xWbOOAbkKR3mMHNCZ5JFL30Iz/YUF+jG'
    b'hWWffsrVIMi733seUEGgDRWXImxHy1Dv+73tALub3r7FSXF6Q1vrfnozwRseLGU/Jd7J+/FRzyrpRnaACMmh'
    b'3tJ1/kIF35NRqNMj/YVEZDTugGU1gfeUzRiomFazzSoVvwU/HeeZF90IxYvAnMj5WOyM0JsNxBDiHxWq61JY'
    b'q7ad+/71KCJj1qPtW9CS2AvZXXeRbjqdPh9i7QwGl0SCiHkbADgf5wgylKI4vFIxdDz9fnQmhKJf4yCpt+mS'
    b'jsC89Rdxor2obguh8VJc+smqlaImtKQ312JoqBRXhYZPuFLWB3EMgYImZoSOskolLaRTP4TDyEe1aEJhubbY'
    b'W5FnmyJw1SaBmT/LG8IDnGxHA6MVbwv0jLjtnnkxcEqoNCO0JJLThw2XmZ7glMN53JQxz17oRnQpaoz9XO7X'
    b'pLXs5yTQDeLkeVwLmwcf14+ZJ7fPkJILOuj8ghHz5AMUZpB+o7EmxzAfTwXMicgx5PTGLZXJUMRV55ZEoice'
    b'De8JrUsiA3HQ3Dnxt6Aq2QwFkMzdr1rXG8CfFQz/u0LTyahPrTwfWlsLx2kNnH6pmKdPvwzM0adf8Cw7n9g/'
    b'PAMTXfBMAlT2AyuuQymWM/52p5Hl48J6h3X6LE2ktTnnSks40aGUqJ0StUB62sZ+NVNLChJ9dQGZYVhlaFcU'
    b'PEEpCEW00EpLV/Cuo49RlIkl2g2n/PeIoc7MwTasAgVtFau2fvUh72kc4A1GqdO8kfZI6T6bj08xsZQ43pu5'
    b'BuH6srH/X2fXH99UleVfS9KmWH0p6oAI2BV3p65KqaBDhdJfpD+ksaFtqEgLpu0rRNKkm7zXUseObdNQXp8R'
    b'RlQYVh3GcXf8tZ+pDOsCorQUoWh1ClQpP0ZRUV8tn7GuKEVcs+ec+5ImpTjO/JGX9879fe+5555777nfi5ZH'
    b'SoyyFKhJcvGtSg4QZiux5K1ALraQj5KhyQgSAXluRPC95XpjYUoUlVPDFKPa7dmdcWofJ3+KVWZQr/80EGBi'
    b'p2d37fTXQvQJYfTO60fpQ2dG6YkVfwzRj4bo2iLfRwpW5V8VrMok8oM136tOFFhdnlZXrEZBM4x1iSaowepr'
    b'nY/zPWlYyU1KzYnnfUhNfWOEEM9QEvpzLWhsnltyC7x0dw1Fy+WqfDz1mXPg5lmaVmq1WusL13TFa6D+Ftxb'
    b'OK1h+qMEnoPN0XSWbUMkrOaacJfAQn4SwQ9NnofljrM0GVXllnMMhxByBNneifmQNw4zx9R16Mi3yAwnC22v'
    b'1FTzWbEWjehgus0puo1rjoCqdEbJMBAi1xmliV17OzCaiyNikv/uc/65wN39ivnM/Ks8U0AirtDJ8Ua5zKhT'
    b'6gyyaDQo1v6h/qg+pfyMIp1SrAMB0L+myLWkhK5HFbN8WPYOs+V6cy/ineCmoxl3MvUdRX/kwnAx8LYN4Gx1'
    b'YTs20WJ4qmIltor6djWZekwLdqGrU6nA0hXL5f/FVYPXjzCzj3A7jREZY9G343jYaFD/tZLZQ3TrO2p/yREU'
    b'yYDqYfGma/HK3eJc75cWLfKblYXyhdR7dOJ0JYEhXy89oiFfS3p5svegztulG/pA/pUu8pbfYrWRkm6XQzIi'
    b'nmQE7dCcU7dVaALiG4EJiHMMTTstWLq5igXVjNua9yHkX+oxaaZ35+ME/jfpYBO+sP20hKYtrFrpn24M6Vjx'
    b'Ail/8fctX47Vqn6+PhDQc/LoWhzoSknKL2RTrzyiPKxT7jXI0oBSG6/kGWWpH/Hnn8bFnvp4WepL/UBa1Zxe'
    b'klOcIq1sflhXUlRUIi1NNfdLS3B55Yh6p8C0668gBOWYb/0sDu2gp5PR7RTedwxdcg3+vEDzBRjfbuDb3kRK'
    b'+YB82NLczXVAOxwd3I4Jlk5JPSrZtdRskFpxSWYJSKw4WrWAxA5VaRZecRGW1hKlbcS0H4DX+aUG3meLw+ld'
    b'e9nk+KaLVTCvPbn+KSMaBWZ5v7tCUgezwDm1vI/3odklS5H3ocIBqWbmmyHVY1ww1eIqph7xrfFxobuSY3jf'
    b'DwgavdKgvIEggM0XyQ6+bdBACFKtH8F/6jOIFsi3voeo6fQOslRQTL2j9rM6LnQ4hE9qOrMF4dXPf0VHRKz9'
    b'uD/Xbuon69oPOxLJNv4v3ouVdVexswNoZR+yvUXcP/lCEZkc454WOCqPzgvubvpO8k/sKwr0DeYjVHupMfWE'
    b'lKhVdCIWOSu/QJqM6Hnfqf8N/D14I2b/KN+CK2nB6mlj1bPIal7G+ybrteqRzX1qSiXTvPjWL2JDNRStmKF+'
    b'j8eSuTO6vQuv83Ohbd6El2+zGg2gK8TibArRoV+BN2Ar3vs8BpD65Z6bh70j0/gNW2LRLBZy9pVqfREnqW2x'
    b'pJglDD6FF8H/UJJ5z2K+dW6s1p7ZUaMZTkZBWK9r/qG4NNMizYRI2OpdSwVjozRMqtQgb8QeNpgci9HlmTIL'
    b'pBms+lYa1NIKnN4w+6n9b0PFxJGvpYX52dKD4MF78Ub+8U7vxUIGUBvvz/7BOxLFt30cE+Sea7XE+mIieLYr'
    b'hkzp0OXVmHA03+dj2FmwErzghW/9DYvoAa5Yb3mpDnT2wbYYVlO+GTGYlaLCYuDWScwbK9+ztmBzLEdyOMBq'
    b'dLBjdvO+hehYaMCiCS9A0WZhxCd4n3nCaBVmTmANBGM539qJfP5dSb7ZxPt2RFMz+ZfGpx7mfVMR4q/e4P3B'
    b'IIlKbry8j/+fga4vjPL7cR+AvP4umvcd1rPbCw7B/wvfB1ATeUMflue/3h/M8x/0l+b5ypvYRRC+dlzZ3VrF'
    b'zsepa/4M2a4PcqL64v3BVdlKfYgNJ/A+q55QtU5fhKEZIQOLMKwLK8mLR3swt7nxazqrovpkLwpf+LxK9s7W'
    b'wKnmEv714/SkAZYGMlR1BtTcVZCBybQ21xctPYjxpAQgJtnr0LJowGReq6Vwq6MY/KZFp3A04PbL1tNouHKK'
    b'RmCYh/fi+IgLsU0dbNfaBO4Jm0R2TkNZy/69DzGcQt4Hc2NucDMB1P0frnzdp5lji7Mx7UegPhQlj9Im7DQv'
    b'naryLtKOyRBfaUdr0JKbGUUoGINowggWY4V6MfNDd+BlMZOxZnBfg9UJ1hBayEFeYnnfl8gDm9CufRcepxw8'
    b'EP3TS7sgvLRPsarDAm/TCoyWJurrK2l7yfs8NQyWfzM0z8taRScewuX3vijeVz2BUfzo/wRtEahvYd8lxv4u'
    b'pygzV3KEGE9aEWS8WRNGZRfvQ4Ongy1NGnwSDGDe3Y/QRlhLK8MfllvWk0Z4PpolNwm078EvoqkPtcrRTB61'
    b'1sKcy19qQFmaFz3ar3AwwAEuq/BekFPRIVnaq35aHhxtnokOH202ot3KrwyQ4Dotbl8j7Qf1gjAQpzApuobV'
    b'ubdTh52lPJoyjnEVRTOh0ZqG1dXYr9xwvjvD1/E035oChPk5RpaLb7szxOu+zXnIIF6tPKmyEZCAVlVhO8zt'
    b'dn9i8KBzYrgznqpWc7bjKl/Xl1d4P48FbxM911Wfz3moM0pMCHn0F05Ub2Cx8BhLY3R4LJMImBScz3dzoBmL'
    b'M8DbNbsmA9Wj34U7JyGv6olXmK+mKPI1edd1zNeUCF87wJfvZMPPvAFdfXgmpqtbtfDcNL71Luzuuz+Zumsq'
    b'Hls2wuv1u6ax6K6PiE6AQIOI8fYanYzJMYIyeEI+3qUmdH1skD/0no46PNj8MR2fXUcHbwa8Hxm71GjvaTQP'
    b'bv4cV7lE/d5Ocop7SzEP/DPP5VXE9cknur7Qx/VAWPBBn0OxXV/ENH+Cx96BqkNq3EDciJzzEIijnMTBdZgl'
    b'aUDOM/qfzOp9RYejJahwa3UyqHCNA/K84KLIx6knBvEmHhoDiwoLQGz3juDKOQ7z+c8FAkMLmeD8t+Xaysms'
    b'IP9LNykt09gsZCqM2dK1SiGKC9XxOwhlAO4bfOdbWvtiN9aEraGVG2AGXez3Tn/3FZ2mYE+C6FesRDbEfU95'
    b'32gn66G5iX87nngiqSFTj5PP+7cTHH/LI0QjoO6WYXqepaeBXL+n9xF6TiPKfpINX7+Uckg+L7fPIwzqAkJY'
    b'JzFIB9XkdjpH1n4vPcvoeT896RBaOwEgtxMAcvt6epL5WHtTCKmdusTXL+HS3wBtWJbZRjcsw5bN5RG2cF7X'
    b'iAvnDzfSKmWZcq8OJaB5j5Iul3fKX8umneqvl+J0c79+Y2VrVEDaGZA6SxXzHjScxctT1BmkRND5sfdI3u1P'
    b'Oan+131kb6dYd6CM2wlylLbz1j2GPF2y7hwZyBkf3fEbtHLnZ/XwLWspLB7vT4bfKpyVrNShLSO+zQNBK+3A'
    b'L51i2qNs+T6KjdhoGJy6Fb94H5qzyz23tCCvekeq+LY/kOL6Hp+0/2u+7DHNxqYFbYL4pLfhtZ2ZAC07Aj9I'
    b'd9khkPKaPZTx0Z1sYb1O8Jt6oDpkaydmYRs8Gp9DC63n4SG8jIZxHarpJO4amHrwEhDzHrR86+ATWnSkhQwo'
    b'jaeAgl6NGMiIwY3SNnk7GhHJu9HsaOgOHHqgeAkvMuujhGeZFVPCVmaxlLCbWR8lPMYsjxK2T9HoSSRUsX4b'
    b'Kfw0LXyiFv4mzd+tWjhmgmQEXuEYpWUuxtBC1g5UkQjlLr99c7cXpiYGbN8llSH+wZ2N4JoPzmDv/CVOI7N/'
    b'SScNgtNIMvpTy0rYasWA2lFKiNdyt7+5M+GjKLyb5aSYFDr4OCP1T5is9DO5R62/F0Eeb/Fi3tJwhQBtTY1o'
    b'nT4As0f5K5yXjy6r+htPgZgbUQ8cDwQCf8JyyF+VgtfRVcrQmpj/QczoVngu8ZsN3rNTINpM5K58g5LKmh5Z'
    b'wLhpBxvsgQWWIEvQlgqf1MOXKIxlcdti1mGehld1bbG2YYEhe5iNmbIH/v0daKmnN577M6fsNRIk9ilm75WA'
    b'xpQrkMuUTmZGqOzXLARR/1B6MfRvm9j02ayyrCSYzvpNp2E6b1EKvw+87zepFrxmEt77Uj+UzviF4WWjxtHP'
    b'NtD6NzwtsnlE7S/S5vI3L2WZNSjmEVpKfwXmUvctpzWB9szpGHQVBRXh2doppitLDB1RMMu/UylaAH/YK719'
    b'WB2zsuY9OE2aCt93L0jpJE0Fe/DWTn5WF2/aF3ZTGKt8I0U7vSFY+dMgZiVKadDBNBIZJKUI1/b65QP64buf'
    b'54pKA9IACWdo9GHVbSVgf2a65EYpdxCFNaqDGguBencfM/OQh/HQipqyFE26NpLV2J3KBrzQATTKW+cXxks/'
    b'B+7aQFvYFyrrsQRtO1hVo3ZnbNupfZzx7mWqasMtbfNI6e5hJpPEE5oxJYqLnqChJDYkmWajqWr5YTKSLHuL'
    b'Tzqs2bDCr2wfBn5c2/3Ck9FezeyzSztS7d2jOZ4dsiobFjBNePHNPWyxM2s0LTTXXKFoZqsorhQWLXIYcRly'
    b'jlHpH0M86zef8v+7/gna7pqyiStuPoAHu2VzfwCPrgKPsRPaliXQXwPvw4je9pSmmoSxGDZpWj026d312PGN'
    b'CMoqTcEV5sZraNGWLg8xGfECIquF9OJuHAWG1UVd2NK4Py83XqM0DsvSFL939p7/5HD/xKrKjfGy1aCd1QlB'
    b'0R6uw7Q+qPubaQ0Whqc10DlOWpbf/UhaqgtTOijWBbSDGYRMu6kwhEy7ce67HAHT4m0owIRPsNs1/JvXPolH'
    b'SFWjVTsjdyNRlwNV/b6EXcSzGbdz5QO4MVeCviCLTMf/dRHugfFP7POdlA8ys58MX0BcoE0w27bRuaO25wg8'
    b'Zcz50nXsBAv/6ubntIs+BtTX3ozAt9X2xRTJUKy2SViTj8NTff0eSrX1DAH4Kjk02cdlpeCZPzW7JDhhRbO2'
    b'4N0snywJzkjIjLAxnpN+7u8IWfgAy6CNT6e8Fc1i5JYtaOvRgoYSQ1enbkGaJEGbpZKD9AC8phzqOh3tHRHq'
    b'r1XKjUeHvF2G9U9FY3wZ3guJ0mcK+cxgMXlHpkp38q+2oN3BAl19XNNuslupu9q/NTwHH3794pCulR0PjMoD'
    b'PS0+XAtUfSIqPI+KpPDchAwFrADjE8wBv1b/w0yoOahO3tPLEIcHvBcmyOa+9i0FBLDRdDGqPgd3pa2n1d/2'
    b'YuP2je5Wy/vOH5lpVVtPitEpnXF9/kdxt5qdskFzXehg16DoVsX76b48PHQ+gDjMjf16S+5OWiINjm7EkX/x'
    b'IEd+7GEcOQ/YUQcZ9Ft7Uk7KB9TEXrxjB48OQW/fjKbxMipQCdY9DGvn+NHTM029opsdbvc/SdcEm/av6Z4R'
    b'1aOY91sCR7wXJ9Rfj7gsx/1Pdrw9M+qoCpMFS0Dar5gNgSNDt0G8i57ezyeb9rj/Cffuz2nmBtcU+dc1HZup'
    b'naJh3kGE7PevY9dMZnsvwrSwDlF1TL1UJIgXWkDyCO45t8+qcji4XEHM9DQ4KxcLDXiwTEBCqd1Z5aovWe0W'
    b'bFUWt6tS8Hjyq7hFNtFWVhyCiCqx1wjFoqu2THCKgntWva3usj5qBPcqgXzU2501NZQuuDsFh5YJRIUssFe4'
    b'be6GTM7isDUUuyRnVSY3CkhVItTUutw2R67DLlauvixslUhYYQzVmKDDnGuwVJ7x8EEv7yDY3JWrk5dIgrth'
    b'PPd7bKLd5UzOttXi/7jQsnbB/WPu2Q6bx/NjHlgSmZWivU64XAKXd6XoL++MBZd+rOR5dqc4nrPFLdSNm1sE'
    b'+7uUzi0VVtsrHeMAojJINw0PdSxM8yUQzYjtbHNWVbjW5kkVo86Z+WFeIYVxa2K1y+UJz8C4lcmYZlzM4fzx'
    b'kZcRzNIzmny2q6YGchiBvolNFEEw1dSKDREUq9NWZ7M7bBVQR+H0PFc9V4RIeOFE6EmVa7hcyVlgB5csmyg6'
    b'hDzromT4FdgaIK1Mh6OhRLDVEK5stssp2uxOYMMIMmdyCjXj+Yqkc/T0hJwhAZckgigQhUrR5U4e45xc4qpN'
    b'xi/ojsksZ4zHshnKeHJxpcstZK3iHEK1mGyDDLFU3AhAmCxg0ozAuIOAvPOrIDIEXhcc9hq7k/pDOeeRat1C'
    b'ja2yoZxb5bYh9Ho5V2FftbLC5fGUczW2qpWrbDUCvII/u8fuXAVvToxAFKrKueW2mhpXIrdchHJ6ErmUO8q5'
    b'KqHaJjlEcKtxVQmJiAVeJ3jQDwgv+KtwifjlhJJDrCG0Wswh9UFnQyQceYQPBkZe6ByXWl0dSV4k2LDHogiO'
    b'oGeOS82CfI2NmGgQ7Vgs9GAMIYGDHkMfBbYKqHnEQURgYwZuaIdet8hew8GgQIiH9hqU5cTv8A8cF3xlGMXo'
    b'RRJdRUKlqw5lpuaY7UCOzcOOkeeS3Jw1v7DWVgltBt2l1uWE9h1ToQKCCUcW3uHAvFzi7fZIEnLvJf6yCbV9'
    b'THyQzUgKlLpqPF81wHGVY1KhDjwGkZ7lsFhwQM8QqjgtJ6HvZVBwArXMsTscWmnwNZhjRgo1hkYeHRqAKUMf'
    b'wZSxPcmtToCoBNPaTG500M1x1bFGoWems0oLncyontWCIzjqFufBWG+xiatz3K6a/EUFdo9YGjbkZkNPctuy'
    b'HNBybhd4cAJf2Bx0w0HYdwH2l3BCJvSxCA8oIbHNInyZ7U5JjAyIXJkylnA7EfJAEdEuVwj7DIuCUUZjGP2+'
    b'nYM6E0ESeHDsofYsAg+FVCmesIqjTsEVV4K8ciy1C/XJHnoNCbmQLAuFMLswDqygZLxMYpUblZZkU5UdRWRY'
    b'xOCh2GGvYkNbGNEiuCuxE/w9aebYBUdVYTV6ppMCy7G0t1W73DXldH+BIXbNAxH4r1whV8BlcUWcicvkFsOz'
    b'iFtJXxagZ3LL4CuH/Cwit0vwY39CePxawlnhWcyV4DTpyvgrJsbG6LkJ+lhDXDwXFR0z8Yorr/q78U1BC/2H'
    b'77eooba5rQKq+Uej+Cnh/5H7OUL5HxP4ljHhZ8/2JD6UOHv27ETTuDj24RoAKoXh36jmRUo8h43J/NkhmYEg'
    b'pZHpzZod/g1Tg4cjc9SUO/HYzAUc15MFH+kclwj/TemuSd67Nuz7Jo3jtqWXbdi38J3eXvDTkg3fe9/pfXnv'
    b'nDmb0G9G2YashUU7ZyzkuEeyuaZnF73TW5A2Z44H3I5kV1d/BuHi0y3+yWnV1f+SfnbJzrTub1an75yhT4e4'
    b'F57kD2Uc+KAy/bGJMemdjzWnI5rinDlz0m/wrUmf2/SL+fC+F973pu7IS3szsTN94rHfp0H2If9NWZAOpNkE'
    b'v84Mq9UK/xkZ8P3m2SXzIJ15aZDftGO/r4U4N2aCG9Jed02acBf4T/9/pHUDOw=='
))
# GCC changes only this immediate when the native room parser's per-team bot
# limit moves from 15 to 29. Keep the otherwise identical generated payload
# stable so the source diff remains reviewable.
_ROOM_BOT_LIMIT_OFFSET = 0x9c0
if BLOB[_ROOM_BOT_LIMIT_OFFSET] != 15:
    raise RuntimeError('Generated sandbox room limit differs')
BLOB = (BLOB[:_ROOM_BOT_LIMIT_OFFSET] + bytes((29,))
        + BLOB[_ROOM_BOT_LIMIT_OFFSET + 1:])

# Keep the previous 0.4.0 sandbox executable upgradeable, then select the
# freshly generated room-difficulty parser and its relocated entry points.
PREVIOUS_ENTRIES[PREVIOUS_ROOM_DIFFICULTY_SHA256] = ENTRIES
ENTRIES = {
    "sandbox_controls_page": 78813775,
    "sandbox_battle_fov": 78812948,
    "sandbox_marker_visible": 78813004,
    "sandbox_zoom_changed": 78814260,
    "sandbox_particle_update_queue": 78808762,
    "sandbox_tracer_update": 78807827,
    "sandbox_tracer_position": 78807944,
    "sandbox_scene_shader_clock": 78808888,
    "sandbox_load_keyboard": 78806762,
    "sandbox_save_keyboard": 78806831,
    "boss_stock_panel": 78791602,
    "boss_screen_root": 78791512,
    "sandbox_boss_menu_open": 78783232,
    "sandbox_keep_replay_markers": 78775726,
    "sandbox_graphics_page": 78778162,
    "sandbox_blur_material": 78813092,
    "sandbox_scene_graded_blit": 78813667,
    "sandbox_scene_blit": 78813559,
    "sandbox_refresh_score": 78774560,
    "sandbox_message": 78809031,
    "sandbox_reset": 78811694,
    "sandbox_frame": 78774820,
    "sandbox_compute_marker": 78812540,
    "sandbox_draw": 78775824,
    "sandbox_mouse_hook": 78768652,
    "sandbox_binding_menu": 78807058,
    "sandbox_binding_caption": 78774127,
    "sandbox_binding_name": 78774068,
    "sandbox_set_binding": 78774272,
    "sandbox_reset_bindings": 78774186,
    "sandbox_controls_version": 78806957,
    "sandbox_migrate_controls": 78806878,
    "sandbox_minimap_extent": 78795927,
    "sandbox_scene_particles": 78808273,
    "sandbox_open_replay_folder": 78807425,
    "sandbox_scene_animation": 78808177,
    "sandbox_scene_wind": 78808201,
    "sandbox_scene_wave": 78808225,
    "sandbox_scene_trees": 78808249,
    "sandbox_notice_attach": 78774687,
    "sandbox_notice_detach": 78774750,
    "sandbox_project_markers": 78775376,
    "sandbox_gun_marker": 78811958,
    "sandbox_blur_slider": 78778389,
    "sandbox_training_roster_added": 78791422,
    "room_header_description": 78777353,
    "room_load_description": 78776969,
    "room_get_description": 78777684,
    "sandbox_move": 78774993,
    "sandbox_stop": 78775258,
    "sandbox_zoom_migration": 78795892,
    "sandbox_particle_lifetime": 78795744,
    "sandbox_spread_ring": 78795798,
    "sandbox_setup_reply": 78795826
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNrsvX9cVFX6OD4Dgw44egcjo9aKkjY1S6asnNU2FEaonBYdB/FnWmKKPxfuFSupwcsgh+M1E0V3q912673re3N33TRFsRrAAMsK'
    b'kVVUKiqqi8O2o7iIaM7neZ57hx9mbe/v6/tnvF7cufc5z3nOOc95znOe85xfqgH+Mu6MOP0PU9WUoGQJSmZVDQaDaWpSnMEgH7TM'
    b'aF2858JFN/vSxaoiNi65xRis8/pEi1KS+NifDaxi1pzg5zbfrDlVbmG6L7/SmnuLkX053aXkmVmV4lDlNqsQ7VCD96cF6wWrQ2WN'
    b'QnS4ktk56zGIAYjso3QXEM4yLPJAPrLCswxZxkXw5pka3Jh4Znm43BmUTDaf38aqbT7mCCiOQJYhDTI5PT1YshkQWA28xLUvD/cG'
    b'xePcYbY7AsJmn7DHp2SemzVXTyVdffHnBkN+9f/A05Xf1ggJMG8ClDIioXaYkW0ZBa9MGYNP73B4eg+Jq8oiAMt2iK1/dw3CJ7/r'
    b'SZQ7BelfSuHOrQaD8ruDpUgkHsKUZ8zCnnC+hd6V1fg0vWuC2GmcElGvIcJIfiaw7jqbL/8txHJNfMQGedVC/BFJ9ovSXuXtw0DX'
    b'dqrGmwxAIxBRUvqzl8cShTgkXWhNhIRfHo+QlxMJPhSewp6buYLl4FswTSXpHj1z3gx4Bt/EdzXjcjAov4UAwwzxl0tOxSpepAz5'
    b'RBLGKp5kFazhclssd8Ww5HgrmxYfI0Sb4nXKxBuNVLCOU3ZS7DVSgfK7Bsi0ktRfT1LLIoULe+7RM+RdhCSIkPLsPdPVw99iXhBo'
    b'mCl434aS1nhT8Qs4V/YQfMqXo8SBtnr5skmZ9LCwtXJEHfNOBgSQA28a/NZ4p8EzkjAHSJYaCnTBp3+QfNmyKlKZdK0WQX4LGWwQ'
    b'4/nm/2BdKhODcmde7rU8+T8eqNJcC9symfiIVJd0AFNSNaZgAYzVfBIwpR8yJf17mTI9WJdkfxkLL3gjMYlewqcOiwfZqxkBz3SX'
    b'3Gbh2wLBX5u8pwRvNQr650ah4C2UfROrXmRIMBgWbRwCX4bgxvizIN5VXp9Q8DLKwbqfrweRCpauDywP5xt8KPuVyjobAqcyt8qc'
    b'bcEj3K1yZxtLGqusm0gBwSOKs13ZuhJEliWNZ4EsI+Cpay9Bxp1tKI2DhD2O9vELcvsxeSPmwqhsTZgIyAF1DiCxyoyd93nrxYHF'
    b'jnYlrdHTNX5V2M77iuT/AdQi+TVsR7/D0kB5WSOVOPjKJcgflDudHYOGPSW48VzH8nBbPXOf8/qoKd/C08cDyf8oznNTQCtAHk4H'
    b'SxKgBdsd50JNt3PWXGBexLyI1w3sy2CdDbGulct2QoJhksVW7yn7M+otYUslq0PVY504FlLk2ZNdfGYqWxnDpaEsw8rnxArRBlA7'
    b'ZjYzdRbLnjxXLk4NMxiMc4ToiWOr3NC6v3QVD40qOCX2n/CL2eageNbmKzg1aw5kg/QZqg72oYsdlduGMkcjaqC2GlAwmHS0o0nu'
    b'WiANrHG0YTOtdjTZ/P1th+SuyFU3c0fbO1GoPuqVVCNrPH/eVn/+PADlyoSiaRFNFMYabfXVyQM/vu38eWVCGBBWCg2tZQZCGdik'
    b'uJu4s3FqsK63JLEaJb/uz4sN6YrU5GLvszOQLUBhFxTZ0/KBIb8qDgMBcAYyGzzKHY19lWDpLagEX4EnyOFQqIM7WI3NJ++fHIYF'
    b'KhgM5cAiNnNHs3392rgBBoNoDTqa5bzmAZLf5ms9A2jtgjV/NiAK0w8Joytyosqw8K3HtZANRdsgZHRtduuJvK/ahYzC9fC9kDvg'
    b'PdrReMLR6DlskfPM4QuZo+XEspZP8xpPLPuKS43SYkhS2FMQlgg5sQfEmfBZ41CxioG9+MOdakWzGXhPH3PaIgNp0JSYJX4ffMex'
    b'2fEm7ghA6zSzTuZuVPfejX2Xlbsbsa/ic5pZoKjgBqQOlHnB2slQNmxYv0T1ABmSBgCOsGc3obAL7Kz6Lain1puvaMrpqisOWTgn'
    b'jlg4Sl4dI0DTWxXGoarLkRPqL34NbXqsgdVI4fL7oKtqHE0IZ4465mhA7jYqhVNfQMBhpXDGC1jAZtSnzFHL3YdZp7oQEsas/Y10'
    b'fz+ltH4jYreoTgoQw2ynuKNFKf0UwKpdg0WUYw0S+DSCbw2Bw0PgdgQPCIFNIfC3CD53SQdHhMBmyJf6cQjcD8DF/QsgTzMho+Pn'
    b'iOEp/n/j5yPYAXgejBO7WrEj4u5ayP+ftGjXQL+llA5GQptRkTga/eVK6XwseSfpH69PKbyRGNEiFBzFlN0tEPSkFn0IFZ2iuyl6'
    b'Q+tTWBml8RqFxG40SGUUot1FaHWtUwktQUO7XkO7V0mxKKUPaKmpJgCiQmoCNnuHYcaxzElIpPViMJjIHE2tFqTyK8jCQxipljlb'
    b'1A8uotJsOd8ILUR426ik1Xm6IoV1n2Ofmtc8Xni+Ed6WuJujJijJYVDbzf4TrBLysOUiVSh1sd359+jA/r1zu0QHnr2MeQJN3qiU'
    b'LsKgVBN3NrNYPqeJpZpBa6VaWKqVOxtYxlCWGsOddSw1FuLfocUveF5TPSn9ANXdxtwB4W2rMrPOcxGyuwnFamYdm9MC/FbmtDFo'
    b'GzFceDvDqLiwQKvCE/1DlrhbomRfmDLthjpO4XLXjc/NEt6O3f8LiA7WUcT+OfBiPypNUqYYE/ehhMtdYdJW7m6Dlspq2BQLdwfY'
    b'FCtIhwTi0qmUrtDKuK6LamSQ57n+cWKEXNPfHy7XmOWDo1hnj547ns6qXeoXQ6GtVZ2Bp9xmZs6AmvYfqOIzrEsBrZJoiBjx6T9M'
    b'oDeh4XOpzcVOBY8BBSJSkzjKQNYbVHNqAitqwRZ4ZF1q4rq05Cr3vgX4CfbsUXXGE6Ty5GQjKmT1IJoX9cC1kmTUA2AEuazMZWEu'
    b'E3OZmWs4c41hrjjmGspcscy1hrlWs5L10FuzkiJ6JtNzHj3X0JP0SUkKPSfT8yAmXhKDCZZY6RlLTxM9DfSsJZxL9P4SvW+j52ZU'
    b'iHKZQfurkXfrb4yPQVw+ip7D6RlPz/H0HEvPTnoG6LmSnovoud2ItA4YNVqzdFXnmuKth/76lH8Fq2DHFM/Bmz3h9jDpG/60CUzt'
    b'r5X8jJXPhdv7Sx/Lzw01iMf3k5arxWrqfx6qCewWsZKD3g5At29e8Id4DA5fwK/FF3ud9Cf7M2Ypwp5jEgV2ktWqC0Hm2TNmlmOa'
    b'qVsS6qkboP4rz8ATMgS9yxwzPNwWeDitwvAqYfjn7cIM/K+CruagMPwzeP0MX9uFKT5h9NptwLRVg9qFuY3wXwf/tf5BwvRTEL94'
    b'M/ZTw49CtxQ7Bx5pJnikmjVzTcRka56GJ0ghDGFC3X8LnziNT81gpniwAE3cNRt+zKA6VBeq79o7kuIxe6qwt65CtcLrrGnwWNbW'
    b'Loz0wcufPdtoOAWf73FXAmQVsplRC7/PYz4BY91L9DunAt6ff4XeZxyBf8h9xqxUeGmFf2egXZh9EjBcGfAFhZqRNBvCXZPhkZQG'
    b'Yf+El62vUezZFVCDkGD0rHnwcC3wL2G1I+pC+RsJaY88DQQOY+42U4yMDzA/27V8ACutj0DM2ScA2ASBJxCwAABHdJozGgB6BKGL'
    b'MDVKRC8YFmLJUng8IXoPQcBNiLWSjAX4SgL8DD/8NwvJkI3oScD/jPfgJd3MAuwR4E70csz4Qshn9KMxfbpgMivltuF8bQH1qw18'
    b'bRG9NJZNA/K8dD32JZYw7yHmPCzalLym4O4ID0iXUuKZlY8d8fRgPXceVkpW3lhngHelBM0mdiFYn8EddTMfQCpS/7IM+PH/I5T0'
    b'dGVNvFluCQRzL+nDVjU1Fk2BqfAkCZnMHLvVL57AHnQ3O6l2nUM9J8XbOmqSYoM3G4ayRpazmu1CdaETVZ7sfKx1Cuhs+ZmxBvEY'
    b'xFMj/0Pa8T35GatBSudJcfBhZV0jKvO7PoNOWyj8FHp0b5D4CJYF2cCOGjAhNHVX42ghdZAUlwXZaOLuZjSSdIF1tKB95H+CJ42V'
    b'y+M1o2S7uDArN1i30Ot7ao5cjorDQLSlKTp1MK/CDDp9nXqNjHiI9b2JaCy4ewQZYa13hGtKFqNpIXwmvLnW5L8HRIbeUsdBmybF'
    b'nq+/pZK5d3o+DxNv5UlDeQnaf3IgjCUNlZvD5E4jxL3+kFwRrr6VYjB0OHaa+uBZCc/agxehbgE8YX/mTkuOSX5kdSJEiRP75z+y'
    b'GjOP8Kic+G4CnnMeIHCbwQO0uknEqFN1EpE5FNEK+U4ayiryP5MBkTl3bA8T47LiuGO7xkST7Nxhyoru823uSBpqFQqWQKLyM8PB'
    b'zp6PWrcT9MUDczAj66YgX5JGgbQkctc0/kgG8FNxlLHJ8SboimLidVWj+rvQLCjj7gOgR1jSUuZayR0+aC4sSaxJQt0PbF6XlAhN'
    b'7U+xvyGd8jvrb1DRVWnNfcZhYfT7uQLAS4cSvLJdeKwC4yQDQp0w/H1s08Loo4Tzu6FE47GTBLe6Vmot37ohhuK2CqOPEN6GOO0b'
    b'UE9jKxcReSmVoU6XHLBehOEndF12UlNc0XNAdECtzMZ272yBcmKB0QjEsgYvwFB6+AfAPispzRmgmayFmpaKXrJSUzdWZx2qegjF'
    b'wjmAePQjS1G9iP7riKFif+jU/w3dEYzlhOGqMLyWSzsG5Kw+X73yZpNBsng7JEF+ZoxBivQ/XXY/ug4iyv6C+XXFwejIzC6MOGKv'
    b'yH5Mb3CYzyZobvC7rBlHNC3Q0FgAhE4M50nD06AxsDN6c4C2AN2aKb6nQajDfk7NAY2f/K9+33wRbdAcsCdWT6dGwZ071SXzDYaM'
    b'XtpB65C+uAY7pNZrsEP6kJSfFQYdyrr/JX9ACfoD0OqT0bLbagIbYSpAyT/gaIPUloeca2Eh51pierAkkXxncmdQfLSPYw0dgNOD'
    b'JQ0BdC9A7FjugPG23RGQBgHYE8CxPKQGA293y3TN8wZ1Bh3NACC6iIL910IYjHazIoOSCsNRDPeJzegjqNP0+L6byAKbnq68hQ4Z'
    b'9dvBqEr7X0PeOTJ1vAvI09P+Otsym3w9IYdZL1/TPPIvjePK7G7/kjL9nql8C8ZVl/qDwYx8zcEEqlO6jnuXYkdd8O0actusxI/k'
    b'bz2c6CTZq6RdCqWStUbZECgl790Css7R36dmX+4zFEznktmlTsB8V/0KnsqkD+TOQasGqztOA1nJPCuiecktxrlzgs1LfLFKQTh6'
    b'xMinEYraj6LG9I7661BUw9JQ1ASImvBAd1TyRqjvR2PUE/BUf3maLOKqiJ0jhxlh4N+ougBiD0jDwAavBO3RqF5ClC54KbysvWAR'
    b'Wk1rkGioT1XyzC52Wc0mwp5oNLjjeH/+3FAGww5Hk3q8lUZwyjZ0l4KYy+o9kOXcCLnTJE2aLlgnmeS2WPKJTYrh6bEhLxk0+6DU'
    b'FDzKanBU8ydU03lNdwvrXqLRXjN3NKprnjEYWrHHzn+Qauk5nmJKRe/cKvk5s5C7UgGzy9kM43jo6IGWWgo5+TEJZniD0lg9DYWG'
    b'vdKIoNTwX2O+gwNplFQajVTGGciTBUxPtyJvFsBTPaTqTN9o/BiYrlm+jaq1jfruh1ilZu2CsfyA/HYahoIqeQML6bfa6VcyQ5tb'
    b'i2/yfmyaBvEaef96eonCCprTGg1KKyQvmPxxAZP/Ep7qw6HkE/d80iv5F/yUfKT8jZntxB5Nj+5yqb+lyNsF7LE61SiVhr9V6mN+'
    b'HNB20mB9Sqd9o6rlrJ7R20xFTnj7E+MskEN9PMDnmHmeRZE/OPYP03RXsG4uMFkoQCuMT7dCDt5th4xVAzkRPs75KRVHgBXiaIav'
    b'o5FIugWCJyoljf9eG263Qg2lgeoQ+/GpFgjiHhxBA8ItPHUsDDJv4BPGUuZKkjc9h+iR7CxQzrwXO5QAVtJZXZrndlzhp3GpfxqE'
    b'Zd49SJNlB+Tcypwql2LRkHdYaNwYQ74uMO2buCMO5Js74kHM1Nav9KyrD5ym/OSyKr1Kw6UlMDAfl2SSwIwyc2cLTxxuN4Kl5miT'
    b'y6kH5gax/wOgNh5cNcGeNJw6oOVnsN9u49NNHEyz4bw/c01mSak8xcyS0ng/nm5iSdOYK4NPAsBsGM6qe7HNJsVzdwN3NjIplkvN'
    b'6N6CIrjjeV4bk2Kg64xYeUA2cqmFOeNYngWH3ZKZuYfKB+OQD6FaU18eiIx4fSCNoqHkoH1gkIbscLSpl7/UZcHVSiWNDw3QrNL1'
    b'MIAfl2aSovkge+pwKQrQ0oarfwxAzvKskAlFrptTYIQ0oS4g/ZmztFSvMJQhA0MpAyMpAzBWsQos7xXmfA17021qHmXAdorVRHgq'
    b'14JF+hLrVKtVXZDGc8drwOEFfCBlapA0AoC32SdZpFv5tjhgt/cUdFDhyvPj/5oTbq8VLcrzK0+uxbcwIHkC5KNTfTIT0aSbueMV'
    b'HJ9tg3St3Pkad7zUq6MNNvNJaC+cZJ1K3nb1ftR3Z2Hc26m2QuUJVsd20hqFh8lDhiZYTSEaw9T2CtFSBmOHlTaTYxh0SymC+Ab8'
    b'Bu1iBVNgcnyMOg3IBqVtkDRkIFgHZOtCyggE0d1CyqgT1RHYyLcDsnwwoVcmK02GUB/A88zqwQHI13p4qve3IGELaoXpEYYYCefo'
    b'gtJIOc+Csx6FqIN44lieOhtaVYwiz/sGeGSUorKM+VVxaXWG4FE9lUqz4Yr6UxdiKjVLB+DkRX4b6ijm9LCL6sEvtEZSELEy8Z/Q'
    b'X1On6ihQo74mZTuEO6FnXdPjP9W4LJdRI2FVUqRclkiqcVR307oV4x+A+MCuClaNX7+aivwugIKdVP0uNKDNBnF0d4zb5PJLqDWl'
    b'wXL5UBq3RMrl5M0WozDaSTXMbjCE0AXvk5RJjz3JInhn4TuvNZI/S8eeeBcm5+E7SbY6xJ9xZwHvp7xA4lUH4vUCiVcdiFdHKFv9'
    b'NReSV8R5ww6hoCRMt+ZFUS6nAZN4s2CVSXJkEosSEgtHM1hJYETW+WOwpAe/0soNcqRK/yaZQ+GAcQDJB/RkzhYSpbMoSrZDav+v'
    b'UT5iGW+hqTNRny6UyzfSqLBgADm7C9QlQBgE/9FUeKQng20KtnP6WHg8Op6hDT1psuLYAZ+TEhmZP9A+3HUwEMb22QAaiFz3oeEd'
    b'9MPo9CBEddY3RDlzB/pmdvb4peBT2s3JWQW2e4+Xyr4LTTvBi842b71QQD1C7Yg6JS0odz0urEPRIL/OYc2ng54J9JBMOSSMfj4D'
    b'BxmFkUYi//oOLRl5OxIcfgny+Fct79Yt2u+MTkTbSWgzjgKd48Lwy/D6Lb2O3jqbyO2n8JGIu3UejXYw3dnftAtzjwPsLZ0W0J8N'
    b'Vr/1nWmEM7dCGP4RgGH0NGPdAiQ0uoZGQu8s0kZMnRDOaToNMP4JAX/HiN2OFxywrPPQUGZklzAaPTY1CFqq5QAdInw3FewDIvt3'
    b'nexpAEA4+lmI1yCpjFjKOPKayS9pg5RXt2u/L2pcit6106CJwrwWkrEzaawGP/8Io42ZIOXXyeWLqCkOmh4S6q2Pw0CkuwkH6+S3'
    b'RJpC/TmIZRmJZRE7S4LprOVzDoNIWtls0HDZLSSUfYYwoCW+hA5YDXxOXcy1PG0U9PmDuNsSMKYlcrcVfpK7DdFyNdAPBwNd/Wgw'
    b'QD4ehWbg29W0T0GduLdpcookF32FFsw2pXTFZhwzrCTDnkYUb7ZiyEu6Cip9DsLVPxBsRwi2EWEKwXaGYK8g7CmC7Q7B9iAsk2Cv'
    b'hGCHETYFYXNeC8E+xTycUMch1LFdKQ3AtyKVgyqjxqlnbGgrjmv22bFq/gJloCmkmlURaKKMpQ6rMx2aSo2I00fBNzEmIbuUOR65'
    b'bTz7uv01vkUbwVAjLJwOdUuv7IR8MDn4JjIhAmcJcYqeGEXsc0UYPK3GEIydiIgjlJDi17oWdXUEavtnIzRtj341NBc0/l9U6z7W'
    b'+F/FAuq/WjTWbzVuQdYvpRp5LVQ12/uUebjam3lbrRBDjVF7M3nrrQgzAEx+NkZYFcbvUxzlWoWySkitxQ08qRYnevLKDeJ4Wz2n'
    b'YiqO42wLpsOo1dvq5WeNQRiaGrs/b8RPudpkJDQYF53itEAjlOz9W7COPGrh18gnsVdICmZoBYDBzkH34Jngm0u1dQ/EDUW2Ajt7'
    b'zbj29KE6K+80ISvvgWe68mYZqEpgaFo0SvPOb4iba5swTfrw7oZnROIfVSOMvrGO8FvZOh1Ze2Zqd20iLk81acHpETjnS5O9VRqE'
    b'qpR5t39D2XSY81db0WElrHsAXU8gZ5qUAcoOJERPZauIBT3TTG3zeu408y2UDFFUfwl9butlnPM5JRR0oDhoWXuzDrN9Ij0isHOv'
    b'AdVDC3wL7M3X4GeVhXsp2PsaEWqGJwzpnhXW4Zy9Rl552d+dCLQZN1QvDFd9xqPF68cmwNi7yBKeKHdapIAWnb+FpJSZFrDpiAuK'
    b'aLWMOK5sOQ3v9jNi1ITiZKG9yBTlPwvyqjNqC5XyUauxUXkZ8UZUGivYR/ZOMSqxeHJYEJIA7C2YAyh1muLFHKXlVwYezTIE6zmx'
    b'UVHqQsy8onZ/FYa16w7T13QwZxvU6M6TtKwCRg4RG8d8aKC6JB+MtoIDBy1bC4Ddtg71zmZ9jsRK0M+xEq5HGJg9ARrSqb6PyaDY'
    b'UkHuloIj4mywdjN0A51tfAUpbtxIpoQKkYS971Wcxu6lMtS9KButc8uwkznETmgu+WgZp+6hdDi5GNBjB9ToT2mA0reIYLk/bkQL'
    b'E6eK5DYrjRtU9fAJLKMKxYqI+3gvyj4aPbeihTlpLPXkYcK6azBKJ86quyFrbep1yyD40bH616CFNPnu/Rd1S222evWf0BSCq8Yi'
    b'3Ursm9rURyBLM3B4iYPH7UbB6zN0G3LrXiEz4m/asgDAhqisS61fgt0Z0N1CATC+U5USYqzvE3RfoBMqD4DC9EPQqXpoEcPoiuws'
    b'+OChdQtzgE8yLlqQnx0b3i6M8hU52iW74mjPMio8YSs6hYBPJbicRp0H4/2ZM8DWjeTrkJGAirFwPY3UjxFI2ONup5y0qH/9uNsU'
    b'BgZzU3wVj4F/C/wPhf8x8D8+vsrw099Pfz/9/fT3099Pfz/9/fT3099Pfz/9/fT3099Pfz/9/fT309//z3/6jCrPM7vU7G+Cwfwq'
    b'Dzo+v6wOBvmGBThDVeU9JKZxY5A8bqwCFzBwg1JC66HrpPuCaSYAjS72nIDg8SNzbxs/JDdqvFG8ZrxZut4/UFav2x9tMBuEVytz'
    b'ohR557JPjexMsI4F2En12WuAYJ5ZkTce3AzQXtP27EOXiz9sZlXVjvYgn5Dg9Un9bb5W3CXHDey4UpL22cuYul8p2bnVE2ZPS5Sa'
    b'FXe7XLFGkQ1NnrCpWcasm4N1nrx2yLH0LmSwAvNcNvp3GOvN/Kq0r9aG2TNM0l+yfh48SoV6pcYwbZLrzppE0+QJE++sTjS78i/j'
    b'BLdk5utwbtV/zflqwzxcy8LXdUI+/OE8xcomRPKUGO62JLKUGDZhIK6p+HU0m3Atd5jZlBvZhOu5w1o94TYjm3ALraHrWaVRoaxF'
    b'57E269OkLbKeHqyT8wIC61wVTitL22wdysNB+YIpN06xWJmzETcyPFznuRCZKyzpiEpUxCF13N0IRfpKc8WzC+SHZ4HQUhAXlGwg'
    b'q5qq5ONENzsRrONzLh3SF4nIbWO99eJjrIodU9wHpyge8vQfYe4DSrk2PcL5K9/82gQ8bS0zcPcB5vCBMNzS4TAbxZ8pcsKRFw3K'
    b'nKapLvTCc4cPf08Ej/vNU3FdJtSy4jwYPN5nL+B1bSBiNTfCE7c48Idns6PeQ9ItWSZt65/FYOnZ+pc0GyuG9syl5NtdE6fdI95E'
    b'GMrWr9eT43b/QfSSgmzwpNm2emaFn/yxiCi9n7/ahAuqcivzV5sNcQaDUPrdTbGFfshM9UZ4AivG0DrFiWZd1BrRozs53syOKqU2'
    b'xWBQnzmEa1Jw+eJnCOdJZqV0CgTYguo8CEKYUpqj4OrjMRCoPtINLCHgWATe1w3cTcDxCLylG9hAwEQEWrqBZwmYjMDztSFg9AYE'
    b'piLw827gHQScjMAPuoEPEzADgWXdwMcJmIbAV2txpWmhYZvBUJMcP2rF6tFTWdI0eE1YkZtgEKKT44cqKQkUbVr80KLk+Lsx1rJa'
    b'XD1YHpMiB83Sp1w+YEaftqRvh3vRoDFPKX0aEnoHatagjutOXMHEk+NN6nAAQUIxE7PnL2eyz0w5iH1iUQL+Dkqan71gIbwMTjCw'
    b'Tj39GFsH5OAGjrFP10AWTPHKpFDmBkHQYG6KV+swpGDXqEiDYZJ8OUJqSpGrYljn+DHSnlZc9iAfHNNLCjytKAXF8KRGSHMZFcwR'
    b'QE936RbIq+qpQZ9/QCndtUGfbWhTl2owkDp5WhjgbKQtpVqkKoz0SA+CGdeorOiF0IAII0NUP0eqsnUAzQao14TA3xA4Rgdfqu4m'
    b'NxTJxSE5egWJHFbsehYCfafDQTeWXkLqhwFf/tqIi5O61HbauoU7hdhJkNcP78O1GJQgUlQbMFhLib4PXsQ5WsrEkOcxE7V6JnK1'
    b'TGhL7uQ0LPe2izTTWI7UDNWyCX+0rT7iQJtv4fmkZ1MfF7b4WA2VXYXMLMRdWkGh4CMDLhxBDHMipXXL87ijFVEpb2qklhpEuQei'
    b'FCc9S1jjAcun9gthQRnV5ndDiFGE+BwhTgXEOrU3Xlk33uddPZWxDPDUlyGIyRlhxM6xcnkaLaIZDRxTqDDqe7S3TLyRExJ3B1R7'
    b'F0YpiyLWMBmx5IOW3otKXwK1soA5NnLHRubezB+20nQhKfmdAqj4vJe4c0fZg5CSvUJIqbX57LU5WcUx/T1dddKjykqj/cOcacWz'
    b'+xuL5rwoV630XBRyBxa7X6xOjthdJ1fG+YcUI3wewgUdfkQ+HccC/ps8eS/mS7EEeeMFAAxUst9h7Z6L1+eGQ8yiaRG7E/2bqpMH'
    b'7oYasHXwvM3FALKdGru5P2hLqIQq9mnkZQAXaYBEe0XOSSiF4nypZ95c72Q8vzBI/ajZV3nGGqRwm4/6HdshbWNxtL6xOBEDT4Mq'
    b'jyBc3GWF24+hF5rKM2azopWQDCsSzdpU618BJX8canLB+wdUJo+Y8i+gPtc28LFquSrdFrRf0FZ9L5Sf2WMQV3qeCRqkLE/5m2jX'
    b'SPN54TxSSqJZ2OPaB33kr3jhgh5IGUAe4IWLeiD7AXKHUooFZo24keuWIEkJBMfwQsyakmZkpfiykK2ZNC8lvwoXwLVKgJbRZ/Pe'
    b'hy7aDCu3xTBHS41DnbR0/pMLaxxtoNgc30x7amUmfJxNMChJCdzZorjVIkebsGdSuL1R2OxLgm4KF0d1qpFVtOGSu5vVuzuhNV+M'
    b'kj4guqF43xQ5zkI80xXxjlR2x+s6j/EipA/kgzG9JNPZrBa3oNbb3KJrvTTmqC1eiwyrcQRWLE8wyL4E6BWs23Be72zxWmQTnzpG'
    b'iHY0AzgOZwgBIdrxQY2jKScnAZA+4Y7aLKP6R0iRlyCfbb4ZyoZnn6e1Tpt9YpQSs0lZP2Qlrl51jU2zBQFfFRHbNT7LqDg/wO8F'
    b'8J0xkyeXJ9rqubNWPlCOWcJ97inqXViWYLj0GU/KoFBP+WoK3FypDkY6MspQRXM4c5Thvm4wVpTCf5XQNh+l8BAqMsdrYJjwZBmq'
    b'5SDOSu9FFYSkxpVjrQqbKtXajmBQ/5Jpudbe5GfmcdfsimazMjkMOofdiiVMSQ4bwt3bPU8NGcacO4XN0F0c4I6Dwp5DzLkDyKmP'
    b'VuCCRHg9wCqxXOs7cIZ5JzAkA7eBOg8IexIHjY/KHaiYPEpBP2QKknDWcjdk8aA6COJnGbqjOzG6+yCuF4Gv0m+DwZlBhy9FzvOZ'
    b'Be8IlFLoy521qIdX6vxSg//RdssQ2GceJ2OZeNI0LOQXGJY0W9nwh+dJ+mM0q0nZ8Dx8i/00uExcoM99z2OlTdZq2AeRM/quNsY9'
    b'GRbuWQMdLm64darC3jSzsDexnzI+DHfv5AXsT5uz0/KDaIk9l8oqKj7rp6yPskCLBvZLAXsg5165K0JMEPbYudRmPy/dJF8OkyzC'
    b'nqeM48oLgKw0UM5TIS9cUpmldGKiXJHor4aCHbSE9lSgdbnvM7Qu34Fnuq0DzVvmaGLuw8o6TQx8yrqNJRqnHYehEbCzKh4jggu4'
    b'HLVLqmOMjbZTIBlQAbxgG1oPzNGIYlKHGshRy51N9ipgHyiozb5IqJjGIneDsKde3XwuGPTkNRiEgj9pmPaqnGE8+eVmYO3PPKsf'
    b'NEjXelaHDVNSw/C4E2cdtdQGv1VZ9+Hz2OAa9B3ykAB3+3DJ/qB3UH4acGczpNOkToYUMiDThHHA/tJOTyQKvk+jhDGa3sZGD6EN'
    b'agQgy5eN0lD8djQqW5uxhyvvQWhpDwaTQC0I3mhIBkwkk+A9ffnKJeSoFmJoA0SL+vO3MHIzqw5tyTs7olLuui+3vy3Y2mDAMtBh'
    b'BdDBh84rcLfphxUEJXPfDUEBbUNQizr4LJpiVtwdIe71dlC/AHpLSA5wsqlwf1Ac30Xdr60dG7iZTJQWMFFuae8xWfA7uj1kDmnf'
    b'RvrGXhy+tfUk35yls1POaF21ZtQwmTYfy7QdWbbSBr2WCN9Dm41aF6ftyYZBUjyPGlGRf2H3hoJwGG2sxfWCVTZf/oXmdwEQydem'
    b'aoAqGm5V8EQcE5vstd3dIc+w2o9KETwxxn+t/aRQUiXsOcVNjTrWZ9oJQrgn+311Ay7oOjlrjn+XZp82f4Ka+jQ8XVPQLF170Kgd'
    b'3uAhfgVwhKJiDZ84ABGRmVlk/1xE2LsHdGsyDk9RkA/T1jv17yFoCkEbNOhvQvFHUtjv8GwEbwjzr4TZpGGuQEy5BT66+z1UAjwx'
    b'Vr2obWG4nbvi+CPx9vdybuR3y5Ume7XUDh9RPAw+2MRO/1m51cQeie/e2OPCXV1QuokmsL+xdO9i6Yo6aX2ypqm5kR0Npg3HlfF6'
    b'S/NQjbnbVO9lfYWLUKBgx1t+mBZ0/kwup7YlDpbL6RgKMVIub6GF0lFkDKu/uUzr74PMrfLNFzGWZierK7RyxGhbMQby6LTg1AQQ'
    b'c4wMgx6TVEcK6Va0/UCkLCQ6gSxDRNqyfxqDUkuwDrd8IviKVvU+bkPIs3AJi8vwvIA2Uij296EjM4OV4u0QH+Fus71LGsiTYuDn'
    b'VhhYDfIP5UlWe5jYBvzzf9GRODlMjNKs9SGBYPBRPHjAEUjnA6bjAVYvoFabZQbt6H+lzwEqeNqUvF/TN0chfeDH/j78YNC3vE0N'
    b'cXP7Ya1M6rd0EI7GDDxmJCsO98ipo2gkZXlU6v82HrPh/6BPSmQFYcNHicVV5qyYno42YOdd8v5QBe0PVdD+UAX1zlCX2gmJK+tw'
    b'077y+zoioNqlNvEOPs4bFG/jrkt6fb0DePYaSegennSq335DS/SvsQWBpQKiAsUz6hYsTo19nGiCGsJjs2I6UoCbg7TRVadajWvy'
    b'xoEqk6vM9irphd5GPdP2+dPxDHF7tdMmdmFqVRz7UaF7f7/gxcXI8nOWMM1i7X0IAJ9usflAp4TzFLNdRl+SZB5Xggv5xTGMvjlH'
    b'1clTLDWyGdL7JQ23LtF4CbiH24Bq1f3/wjaIBiiX28iO0TYDhLEzaBEZqPBcPodxz+hBs8AwwP1CX/wLd1eFek35JPaaDJ7aWj43'
    b'8oHOlVDH79HK2KqrmiocMW0tsINmWFKGTU7F0PdJ9bfRSj7RHtKk6kNlxH9SrsCcKBw0mTDXldrQSzvzQdmahuSGlul7XiJDWxmi'
    b'sAGhevb/TB+qbY1DzLN7seCxRJScQ2Yk/HobQuOM+hiWBeRyRCFT6FybPtpTQ0wyIos+bguNitGMOEk7qVr/TULdyz0w/wSq3yUn'
    b'yEmUABYBczbYi1EHCwV+7EjondXiTiXQxFOgtpXyMUOw/JDRasfRBDrq5tebUHZrlcIn6OVgseOonHfYAAOBZhwVNLmWrhCLHM1S'
    b'Pyh1f38MUEMbxd2khmNx++tFHYBwd636DdQLSzFxgIsa7KB6kmBmgg2is1OehKTUKgJb5JqhNSmXcAXvZcdRT95RGFg9RcMbSKdB'
    b'KV2yiTxZpYhcPp66TSCwCvMqj6WdFA3qUxgop2q1qaWQjynMBziM5eTJJIsYDvynEzkAR83196SyAdHvJjJpvckgc9QbCI6nD9iC'
    b'SooNOWODcUUzJ1xg0dTFy5/Uy4uMOf0mennK8ZyCFBwXvA+2DmiwcbdSbonu7yj/HhTGA29qwlhAh4pspOd6ehbp3Skz6KV+HXOz'
    b'/k3KtlK6k2gUUdkwspr9Ju2nhiyArTOYmiYSUgrf3IQtF3HwcCzaVdcIRhCiRCREfGjIMkylswOv4XlglLXctBr39OG5Y920IUFQ'
    b'6I7TeFgDKsh9Zmrz7Hj767236mAxt3b1sLUBm8Wp3ci+Nk3XUNtBHeKPpUIcRIx9hBEw9G44jWp2a0iP4EioUdcUmk6BVqIr9dad'
    b'rVec6uY8hlrDdUzTGjH6KVrYp+W1CAVrTdjDqPZdukkxELpY2pnUusiEpdWdKLYO9axKesROnhQxAob2Xz2oaCYaqBSy0rB2TqE6'
    b'vWyVEmwdITsQm/kuDFevwT0HZDOG1L9qAFDrZ+EYKVoaI+y1cl6HseTD8Kz4LKai2RpZy3kDfcXiayO9Do2sjaxrLcGI3wyWL18j'
    b'rHsJ3t/Bo6FYAI8xY9WKZx7m7jgYHPbNu5CeUJAbrq3lPQRjCvUsdkWC/EQ4SgXiPsoOUIkkPLyI0tT6a3USmKmt4yiXY6RrO5Ke'
    b'SxS8YZB/Tzk50qRZ/gi05H8VMm0hB9Q7a442o3oD7iLo5cmLUr/DiPO4tF9L7WkIbXXj2mQYXC3oxgOaYT3OO7X6677ewD30Hdv9'
    b'/aeve0xt/C79ulcetDRlALW2UY3dLsX2zuDir0N2uT9FvjwcA9NCuVAnd1Pyj5QvjxCh7PdIo3tHH9mdFwgXkkO51FIdCIH+p5RJ'
    b'x+TO/rnmtH14SJI/Xb58txSZhQd5GML8qcjn0XpJQ6MMIKAe+aovIyu+ChXS/3tl0vtyZ0TuzVg94vVI15CoWUtHz9KRlkJBSU+d'
    b'aSdd6BxvB+XHKpRJF1ml3BmWm6AUXoOlzWthR7CPbeYFJBeJ53c/P+9eOpyNzpJimwnsj4LKFyM6KoygJ6G/dLaoNdhn5bXIl8Ol'
    b'aC4fiNIaB/ajqf6bYSAn3aZBE5UYNKAN40p8UejK+Jz0FIbQWXXeoOBF/6syKSh39hMKq+i9ExgnrNtBrEFUJbm6c5ysESglX8ju'
    b'pB2Y//Lt8Kz4yiyXY0s0iI8ryfdbQCfqDmpyFxNzhWhqb2r2lz3DN722HsOdrz7PmvuHDOMllgE0nFUm3z8kDfk2+8tuoR0G5u0M'
    b'/w3CXkgijdgOFWA2qNPPaMeJ/gy9yqQ8/FY9ynVgZLTeeeUxlC8dQYX1xyOosHBsGwftl+U1qneR91uzJ8Rc7m5RH2sJBseRLSE+'
    b'2TOy3Yezgd8Z3mYZvmd0O+YLGt2idx4+bUHO0SjSx5asnI6+KkfTqPUTbJGa2hW847UxKAhX2V8122skdQ+YmK2DSc3UA0Im93+B'
    b'ehPPKrR12El/iwP0QxdBSR3jrrGga5xNYGfe+452jmpCGnM3BOu5uwGPZrmextvswtHmSEeTt0OKHEa2ppB8HnrTfjC8uWxc9QDX'
    b'OxTxUeiz0Oe4dTf2JCv+RoOjwZiPuyEftO/BlDtCd+4qJo8x4L8RiMbjmSBEF/Kp9TKt50mVypcjhHVIXI+j83kfeoi+w+Q0tMav'
    b'zuYPPtPZLG1ufdkQYqRR8G4kMY7RO/e1RK9FLfgczUy7jC1GXNm7chMN/4fKnR5KVRwFn1rVsl2mngqGZtDrjLOQJdtHHi0foTwO'
    b'/kjrQIcy6MyLcLYVamwe9POz2TFWpWw03+QNtw+RHpWfjTeIqUvcjbGKh+ZdN18XgKpQ8pqm0oRCm0G8GSwD2yE5rzFGKdl5/SdG'
    b'YWuFUmCmE9XOpOFBT84mbZa4e6+WPiZ1cRgzSh/i5L8MT9oeb1E//5O+N/7+v1BtX8c3WmjnqjhwXHqsZMaWFk7b4S1o+csBtRG3'
    b'w8/ss/v+ZqJ654d4nixtvFf5n8gYx+3aVekRnk9xD5c2Yqthjer7/wuhg9BgoIFOJYCkc9SQ9RMpzNr+o48+QOb98wN9zMIlfQvS'
    b'nX/q3oJkuKbamE57kDTy2ogm639RI+M+sFhI4ZfeU+IvcMsdC6wK4/2Ura/h8MWpqs1amW/iTlXepw2rfoNDKcmi6bhG9ffNfTYN'
    b'mb9nfYVr2fxsceaKFcuuGvrQ8lWZ2WLmgqsjhEKdK6SczO+ETpz/xJLc+dnfE5fiuDKX5ywWF69aLD71w6EPLXctX7xyZWa2c8WC'
    b'vindddfsBSuekJZlLhdzZuesWPp4dub8JZnZd+YAPvw8DYnf9fji5d+7vuT74j+xYrmYvWJpzl3ispU/tD7lv8bXEzdqjzCj0diX'
    b'SytW5Ij0NjVz6Yr5ePShYcLiEMMmPPGElD3/CeDONCk7O5MQU6WlS7XQ5PnL5j9J7EjNnL9UXET0siF5nR7UDtVLSvbi7y2Da8VS'
    b'V6YorZwK2cjM7gvDMd+VeGnZmTmZYk4INm1+9pOZYtriJ5bokbthUFPS0kxCnLV08fJMaWUcvifY7r5nzL333T/WPv/xJxZkLkTY'
    b'6CekHHHFMttoeMceAvvsiD6CRo+H6AHPh9LRx4Gw9Ifo8RA9HjI8lAGfGX0lafGCBUszR6eDoC5+Yv7SiStWj+5h+Y/Am7BshbRc'
    b'/EG8nooz/Ai8KwheBa+n+n+YHuBdmbur43UL0Y/D60P0Kng9ovjD9DS8/17eHoH+YXqI950CXwWvp1n8GLwfkb/uxvVf8kd4/51e'
    b'TxP9L/KHeD9K/kIN3fBj8PpSvAreVdXF9+B9pz6g/Sctmr/8ycxp85cvSZ+/VMr84fV9oDil5MXzl654cvTIkaP7xJ4oieKK5RrN'
    b'qZk5K+fnLtcIfieOHhqKMOHxxUuh00ibvzxzqZ6nqSuWZk6G8vf+7s4dfE9ckZPTS4v1/E1eAaUd7XqclEGCwTAIxxlXwm3fhU9a'
    b'+hQuKzJYvgu3XQ2eNH9ZwvfAr4o/GVQJRhh4FbjtKnAXMugqcOeKVZkLrgL/Va7WZw64Aj7tXvtV4Y+k264KT3uatPV3+bA4O/Nq'
    b'+Uf41fKftDRz/tXyP0lXHlfSx8aIBbuyXlJWLLgqnwF+VT5Py0nJnJ/9XTokLFeh/+gKFN3vwicsdpHOHPgduHNxTs7V5Cp7QWZ2'
    b'AsG+C7d9D/zu78CnLbs6/WnLkENXgz+yeOlV4SkrlmdeDa7VQAhu+KH206v8V/vr204WP7lI/H66V+JjKRcvzewu/3/D14yUH0//'
    b'YUmzAy0/Ej9kFPxY+tPSNFjUj8R/NHO1+H/JD7ar/ws+trfs/0N9adL04+lDe+ujL/47P5eBpvrx/Jzw+NK7/y/5Afx7fgz+Vezn'
    b'PvakWccDeEiE6S+yBx4S1T713W03fxc/JHr0178H3sfu7IU/LXNp5soV2WIf/gIcRUZTTn3hWrq6gAzsgYdE4Mpyhar6yvyEqpT+'
    b'+vXOp1Z1V+ZT76bvvjI/Ovye/yofPzT+WpL51OMrYPT5/3X81h3/hwaPPxB/4YpVlPQPhSNpfk/alKlyGx5wbopPn66uoZsxzF48'
    b'k9wUryyIN82ckSF4Txm+vcg34DFSrRPhVW4zQ4SYeHhMjjelq99OoMWdrcOdUd/m51nQm5Q9El5woXPuLfO4w3LnWm2tS1pwz2qc'
    b'JfpwEsWY3/rk8fu+lfMszwkF1xQHg60dLBicN11dmIrBtEqk2vGqYb4n79WQC6x1zU2/vvTSPPXxLcGgUvTmadFod5qkh7jbwKfu'
    b'9NaLD/JNcZof5l6lqK7wuXB7lHQHn4qHSsUrRaaHCxBww0J5P87HLZGsRHRmfuW1BjqMb8z8l1unf3P7t8oq+PYk47/uq8k5gL6a'
    b'fHjqE51zDrKjatgW9DPiXREFW4GgXcYD/MVYXJfUSM5q9bdb0BPSxCgEoBEB4+sGnOkSCjLR85e6EldwzMRXYzAtjjkaMOhhcgo2'
    b'LqxxfI6ezYi0GzfhBCmu2zuMK7CcDcKegn6JeBCt4vg8jTtqg3WesQbRovktz6Df8jCuXZKO2IK2DpZXJ1+OEOMgqq2eHJ4YHc/K'
    b'lTtNrALvTzpy9Gt/f5vvHTwUiHUNc60RH97fnyYpIsRwJe2eqdzZhOetuhvV0ZuBFXktuOJ0BjoxVe4+bHe3SdfjAV6YH8heT5bC'
    b'I/PqICf99CnVJKtdxvsQpBW2jiXVQ+UD4XgvgkF0UWreDkgs/cFeiVWWdCc2U7oLE8s7bM9rk4ZBYjd/J7GByqM3EAuQAZhsuPRb'
    b'fwSyDU+t6+Nh/Mt+dJLt3K85yUYBBh47h3eNsLrugy4FbyW6bKcE2fv5XZefwyW2/8B6pDNU8WybV6lW6yLIEwllLEGWPRtv0GZM'
    b'FccxYfi65q20cBQVie0Q1CJNgDbq7mjmPHz+PM+rnYB3+8TgdQdVcldk7qeKo8lWn+g9JA54qHjawONF1jeM/gAVrQDPRHUcln1h'
    b'1ckRTXFKWjg7A28fp8LjE2BFHXO3Ke6mRdeiwxVdnVdcFITnn+5DYd69D4XZzNwBvpHujdqJ50Ir5b9fbzBMByLoDW/z+qRbFVk7'
    b'TRovWnpQKdmznk6RPgZftuk0aTtSKanqAQ6V8wJhSkkeQHL7KSWz4HdRuCFMm+LtUwc/x3zUjNoXWpJoli8sYM42wXHG/gYeAAlZ'
    b'ExewKsWTNvLxMDwrOsCdbQU4g3uIFSOCf5Kwt6Ki1QqI+VV1qx4PaxdGVvQ6Qa82TTvbaMa6Au10cFN8Fu6hOQqkQAGlSv0YJdT3'
    b'EoFqVuuS92uHUR4Vb79jLV6RsQ875wfC4S33kSW+jH0mmhQ+f4YuDBlR4b+P78ezC+9YSycXVniD4s0PvIrYsWULqKZHdAp/r9j3'
    b'GoDOn1EsY/0R2lUjJ7Wl4q3+y8GgnryLl+GaHtsp+8WccaxzROMR1di4pNYSvgFvk7JHSz/j2Wabz/5rk3QTn2C2r6XjhGjBo39g'
    b'ToSSYfS/y2r8FTZfn1Oc2TGXumMv1vz+vVjzcfL+VFrN9CBoHaGAdrC9xzdM1leVIdGyJ5DycDkAIlweRTMRbdgp2itDN11Nqtz/'
    b'dbg2qW8/Lkw6/g4Vyt0IrGmXfQko1TTXX+T+GOdecLktQD4HyPl2dhyxlBQAtHBHQ0Toiiv3FyC/U/FkrQa+AbOYZYygo0jrrlyE'
    b'yTeN0rT9w1hpnNZVQJuxsJoRH+UHayArqxJGVDKZ1luUJ9Bsg7f+WUFZi5nep4ZrdTidVWA9qPWXr1i9qxbsQQkt3oNbso7Z6mmF'
    b'2lBcSV6rPrCJ7vaBpv6xQZtH+Sf89lIdeIMbdKQbD9CNWD7hrrqn+2kaQy7bSeu5JDx706y48Rjr/KqEu95DfM9uOscSj3LM6Ae/'
    b'79Fp8ngYJx517ymj0Ay8FSPa2YZhKkbyETgaV9llGbPCpyobd9KCMODjUeh/ajyhK1z8dyvvfLiegpSinVosd6NSkhBbghde4DG4'
    b'jXiKt5ZFwTuj11ShvJ9OrBYKXghtjqTJwlhePE1jvMiq+doF2mzh7bgIPrQUhKKBjOhiAzKh3WgGtWwLptjq7eW0nfIMLyhHUrgC'
    b'aVatvqQd1yUtIEGlxS7sjLCle+XFfbU9udMXodnqsQlNUV/eSLMUWb3OZ57Lf2WGXE7n0y3CnsqO6kvSZJx5TuHPWO0XRJM3KI1f'
    b'4hvKOF3Y4w4Yq4Q91Uymi3sC6oMv4LRroEaeDN+4VqjitHVxPYqQsZKVpA7ptd6TVmwmM8dBTaT344onA3PuZI7dQsGLKIsTrewo'
    b'XjNVhgvjtB4xtGK5LL8K8bHflvPKoAPlOVaeFMOdO0fU4rzjdV7fs9dE9JFf6FS0Xq4WesKKZnNkwJ4UKxTQLT9JsdwJvb63g4fj'
    b'cq8I3EGVZMaJ/YPQPSKjz+JWoiQT7l/0iU/afFBpejXRtBD1V6ly5+O5D8p5tZGrrgfQPq3z0iv2/Blq1NDM2/QA5jiQqEU+f4Y7'
    b'DuhX20Gfxp0+6KJ2awfD4fmd8jMWg/gsLuvfw4NBMgJIeYYr0x+Eqoqdyt0HFcdhdT9H7oPxUwcd/lx/P9shm89Wb+sATdWdXWFz'
    b'RX4XWgpMaoOCYb4LjojXKmlfYZc6UPYN0TOM4gd5ecSi5SVFrhoKkiZ4i3uv865ovUH+rL9emZbIYrycRSigYwofjsOdmaKVu80j'
    b'3pO7bhUKN1Lnri0OpVufjn7lPSvmLLkAFs44FCCjaDl/7JZj2h0v/pmhyVV3aE24Pqk6BTo4nFYFw3qfwRBnwBs+3AE8V5edVC9X'
    b'hSZW488fI1vgFiIn55kNgsNhZklx/qgJ8gUoSfZ3Lqfb8w9cquf7h76nxYyLkWpp9fIA3MG57/nuLXS4gkT9+CCkVR6rLdqg5ca0'
    b'AKR7Y0iwI+m5OHGQ7ZB+tDnu8/iIyMUiuaznQ7vJNh/ESz933ksz9LQOEhciaIkk61gFu/A+QTrcT0kbrgfe/jxtVKTb+ILiOLBY'
    b'79Eu4MNTcrkjEKEdYBi9jo4trOM6qk+0Cns2EyquXZm6ARfxO9rS8B7QtRkzNY6Erm+sZnU4vdxLM/Xc2Bjn6VogDYBvWv/j+Nj2'
    b'RhS0Tz6nreNsdfKNn1TC49OdC5njpLDHcbLjrLDnILaC5BubFnKENShJJpB7vLsxH7UAO1M07caPtY6t72JgUFhq7E6snVt30kmg'
    b'tJDcG4vLtpU4eKr1nG5yvayteaC1Q2VNWtV8ilXjRaz81dYuBK2j06RTTFxBCkrp27hU/aQCnH4ZKdhljKnduKBFpFNmK1eZtS9u'
    b'1X8zrGBiVIkDiy1hResj4gGU6P+37ZCymd6LX7JNgzotGooCFykFbEFlTyKu0qFE5Mqw4vXjWiAhX6tJ2HOo2DKo6OH+OihF/kwg'
    b'gFkOLpQ+5FswnxHYeXIqbprixRS6j9vsK8ff/A051fk3WnI6mbl3gFJVSsdA0mrqeo1BdOuAYzfkNc1cHGYL/qLgfsyaaE6Rg1bp'
    b'jP/J4olG26mxIE8ETZIvInRKxdcmZWKY8SJz7FQc5UWzB5ZPYI7t8gW7GMHdO/1vevLKjbl3cMdOz9g50jA5bwdud8/bwaXt7a8p'
    b'E0F/7saw1cY4sZ8yMZw+d8sHJ/e6oTPdxSea1OFYgqr7tGUiklKCFcQu4DKDMev1gz8fY53aQqBGpq8RdMp5yQa2rRn4t3qU7VA5'
    b'jc0u9IMmLCULKVIqWlJQX2eFEt/5RpasREK1DJI+hjYXyK952IIn2ZrprOAih7n9tR4D59W/ooHzv39FAwfXpMNwoE2732AOo8Pa'
    b'cS3kM7S2WS5vppVhiSbRDzn6mpY0k3St4ShXGArjTV/x5rFYHUUx/RLlTjOIhk95HjldvDkcM+I7DRJRXxwTXTRtklmHyZ8LGiAS'
    b'otilfy6smfT0nLiyNTg4n9G9i0I9uwPr/tIO1GFVrI69x06oBk58tLIT2oZqvCCjrTgYnGXzze1Z2K/uwqhVFTu0IQ9tLlB21Y1B'
    b'K1tfcH02Sd1dTOvCnQH7Reg/5OoY9dg72h7BUyHNEbpn5Qmi92uih9dDKHw93UdJt3XSGB6HgsWFZZoBnbCNVDzuBlRK/qTQalZt'
    b'k2BzkeMsLiAeC2njVr9mch6ouZAwL8STvhfWgO5JqHZ8apgpd0bmDlLS4OMTAxjUeIcRtDvpY/8AbRXTrtwXiDIufC0pRD38OVTi'
    b'dHZGoznsnd7m7ZJDUSgAf3wdBWD763TogM1HnYNVWS+kAW+KY4ww2uwPfXdLov+sktdiqzd2cqe52BJdZLFOLDgkncUrF1mj+jSj'
    b'rqn3IHMCEq761evIoUQY5gJbFH4jLh9OoA0p5AOZRMunDyu7/o45pzt8hT2JkbgYPMUIBpTdUSv1U3b9hsrVgGP4kvFgvKpRDLnF'
    b'zii7PsE9g5XIweS3g8EZYNbYnRSnulecOzHZY0WhOOZNoThWiLOkxsKdh2d61hgfwg19u871ivhLjPiyHlGL8vFbeCaAnLiNLNqW'
    b'ubYOZVICVNIJbFunlUm2IkdjteOUwX5eGlDtaJxV5DhR7Tg5R6fobpmkOpGeQXE2agTXA8EZ6KvZW+NodInzRVsHbhxNyV5R42iW'
    b'VoLETEqA2Iq7kQZOh+07D2rjvUlkL4LNV6d2rcOetAGXvw97C3e4hUuHsFx5h20dxk4wxxQZKxSsS0W+B88iB+2Udxj61JvGjqHS'
    b'jlunOagOCJsdtUuORRUnJ/yyyAHNvsxYiQn8RkuA1UBvVwsVQ7eXlh3A3erirZ68WoM4FEKqQr1zw5W9M14b0BCUdrMz6tpv0ZTD'
    b's6lrFPmRTbSCrUFd+q1egoPq/AP6js4GSGPaAdz1ijtRKJVBwh53lRaSW0j78wYIXlsva0d9dzvIXcXx7dpw2kbuHWzxSn7d3WTS'
    b'ynkBgzhRDkaID+D1oBkJ3lPi7TbfksBNeKjIIeOx4phBIPbmX8QMkCIn+M94xg8wiNeCYuonnYIIKTxxLJDzl8m1wW7ldIT6pqmY'
    b'dPXs7T02VltWjFJqBRard+QHg4/hZmrcup1kE8fZfPtwnYlSuvEeyNan7a/XhGMA2kDO9iyj2uXR1nAOpku4jblRNUkmckkkmfve'
    b'+4y3HqpH/4yt7TN4TqFhoJro0VeobZHp7gMHbtWO1u/YgI7rV8PtqRbRkmZsxzqLNsX3WfjmUtcSwY3whOF1grwvDvcxibcLo+WN'
    b'IPnZN/GZo7ynpOuEPZteo3HuSBinzmZjwml0WjE1wvr3uUaNbvepKi6wmYco2+iajOvx8klr/qhw3O7uV3CHX0Tz3/6MJ6x8CcrD'
    b'RKkP/jMqD0R04/Wey7TrPbkUg2NWHPsuC+Ao+BxYCXWI1IZIALImImbGUEQ/B4Pmj7ULGTKqEDoKPk5oHxmJ4wHy4no64LlKKHxV'
    b'G7G/uoMAzULhJg0QB/8rrfqFov+BeF8hAKAzzmgfGfMS2oW5zcLoV3fivaJzIXjXK0Tk3Cqojxl4h+iMRZDtGanxgP1P7UbLjNQx'
    b'ePGoMJrvxljDIVZJGcU6supngPsF/L8PaF9o92xG/xz+55r84Tiq1m9B1107auP/oOR9/j+65MXI+3do6/pa8GKjtXSL69qQC4AL'
    b'73yFy/Ui3tmtey4OkA8LN+0JBaiD7ReYs1mgm9wVsNlLXjFiK5VfotO+u+9zlNGZRas+8ezuFvUXl3HzxyG8Wxti31XxdJi4EO8A'
    b'PIA74st3aNfkza1FYcTKkzVHB0gmXVqhfRT6ui86HfkRwg8QHL16c9EjUogXoor9hemFaZpHT/Z9ZxuhWv0aSk/Dayg9o5izlhfT'
    b'zhbocg4JBefwLIr9dKkFOyYUfAmf5F1C1/ralZrv4haeZx5Rkx9EB96qGLzK8nCwznaIbcBorW+EIVsaeOlqYkuh2JcthWu62UK6'
    b'edi31DfiHYPoBVKfFqSBfAPmKMvQOp86QOwXUJxr+QbckAOtp/MDTaYdDXiBShuwFbJ0x9uLNN9QVX7wOo1fy/BGEELCKw5nEC/x'
    b'zSrjfYi51wjD8SrX48C9klrt4tf39g2Pwi2AwvAj6DvQL5J9Txhep9/hyOs05ykI+9xXD2uRmnnpSi3pw/rdK7xBu4X2PVAPk7jD'
    b'3Ovy2GYAv6p7YM9qDWTuq40aoYPCcAzGf7lJu1dWbt7WTfnFFu2+21cP0u/cj4XRX+RiY35V1eQC28RBYliDxkNswG2aKDhrI145'
    b'9xGSwmskS85p5F8MaL8lnVoyTd1XTKIWaSY/I5Br0shNjZh3/iPssDZs1Fxa19J1N1Zn7dSIhNfDUKVZUKUVLkVXbE3OL4S9xyv+'
    b'jVf6luglrkFd2M2Lk7rqqdBv2cRm/z7Kst8cqiSeB6nhHqr8mpUNH2Gat1GtRWPCM/glQJmKDDfQTS+YPN4/44BI67UsxiIzTRCK'
    b'd/zK5t/onu7gcV3KjCS+vS8oVLf/AfXFG/B03VFswGE12NPyhZtX3SJY1w5FmfSMwuemBAxLsjJXjBAd3npE04ebzGGhRmndH0fv'
    b'Mwot2q2iMFykiu6C9zfi6d362+Ha719itd8/mjScM9pdPDNAaDMQf5OV4BlnkE6MhgMMjH4GnZ055p47Qq2bRmmhdNuwRatRum24'
    b'OEEL+AAvHLZqN+bgzcXWvwzVAlDzPIKa+F3tNujocLpUr2ekwF5B5bH1FU15OFSFP1OCR1y1d++iPoU7Y3zyOz5Spk5VKSkuIYMM'
    b'L2hoUZc/q92CcFbdtove8Ar5kt8ACt+LUVRnCMHWoUqAolOSBiHR7QZDiJYe838x+eu7iSbt0g4ggmTfgBA9cnfqgbwQ4g29Un+r'
    b'J/WPQgjKrlNo3ba/gTmgZNH6rd2k35wAsQ5hyr/PC5nNYSWa2ayqJ3riZGCkf/eK1IiRsvJ6TGZV/UsP+lwxojhluT+8OMWs7LoD'
    b'UAt8SOC6kh4CKhIYoROAASyRePINuvZe2Yp3PqgfwFeN41+4Rhi0uiIvKqHhlZiQYJiBO0pl7UyLkJ+SBc6fwcXq+nzEGUimyH1W'
    b'cf9LrVyjXV45qQu3+pT6bPVgU49bh4yShtAQXL48ULLY6kMVswVHUsg+d5O67x9opzYpk2Ck0oyzh9idynmN0IniRbF6FKkfT17X'
    b'zz8QKvfyQMHhaIbPFVw6jNerLlZ2TSmhHucxOa8OBnAPlOBcKKuEAfHD0iN8bzzNMgl7fZAeMOJRE4OBwKMW7miyOxrEeGGv0V4n'
    b'DfY89RAuvnc3VXxuVsQhQ/zRQUdTfgWu+/Y3aESRudgdNaofPdO7agQoQ4ac1zwITEKpufX9K24yBdNS3kfFYFXcbeb5uAFKjKqe'
    b'sNzIpuWv8JurJ5jxLYpuXyy7RFfjaVPfOBDPC+iTWmijjHkZdc4vXybPTSxZ5HSXvRsvDGFSM78fN6D134f+Nr+N5ydjN/U+Njj0'
    b'aCnpAbnrWoGqBvc63gyS8Q9E2WfSHKl4HJU+K9AvgEOgaMmJWxvzU/EUKjY5Cs+/sVZ8FoP7GdOiurc2TovStzbaDkXWMU6pnmD9'
    b'+1IMoFPrm8G01dFWD5ZA4Px5dpxVIxPb1Gv2a5saH8KO/8AuTPBRuiyzBNPRNl6FKK3cj47VRhKmgtie3XBdf+/WL2HdUOXZi+rh'
    b'3TgPEpY7Qt8f52xG/0wTL5CRe4nnd+/Tdsc1sc0E8ZtoFxRkay9u03WE9sONjdRsPbRpU/0/wwHnTRq0Zz/cePjKERlB/Uvlb8zy'
    b'5f65C7YbpXkapmKqHicT0q+EvQVJdGl3Sbw+vxUtpxKPtbOAiJHwNoSXFEVqnVBA/cPfMEfoAVYX7QPBoxFQ64OXQxvaQkzK3nPF'
    b'/rRLv0UvRdiL2mx9HMM7X9ugLeBulUZl63+wuee1qO2rNQ/g2AjNndxoO4VeZ9IrnoqNNOtFVu6WSvniYGGLr6BSHObtkG7H7e8d'
    b'NLBTcX8N7nyHBv7aatwQleQ/jLMVf6A1GW2aXy5xleapEwqKtcqC8ajjrzgh2BDOKqVpSuKLuC1y8r7bsJ5frciZWCPHDdHMb1AE'
    b'XVmor+UCC6iH4fZd6DkT4xj92nzVMrrEDDUF8iMWbaYJxp6DpH9zwqflJtId9l3oxhWHMfpVdqFjdR9uaGDn2/+CG2nXIy7HNJU3'
    b'JuNpFJej9O2Ljsa+t9wIa6sJjHOMRfplN8LaNwB2Ig/NNw+OjxZyx1k0SZpOOJo8hy1YzoXQhE8sC3ya13Ri2Vme1yQNXiJjqhZ2'
    b'wdhYtL7fDRaiCg1JKUK/D4rkvW36cRnpuI/vjOJBZ0QanRn4L224+wDuUOpSfwsM9tswGekOhWMZ5a5MMQI4J92mXxX0cGcwSJpG'
    b'/8Y7drUjFmnTcNeT0mA9JAto76NzOBrkroWSyXbI/77ctUCKlfOaDGKkZxcy3OjP8RAbjf4sZeI2udOYOw3Cjdf7bB2yr7/ctUi8'
    b'nlXDm38wRRtcU7ALKwjV1SR7dfZp/0CAfDVAg/jbNNpaZRr911XLGu1BykQFaQ9h8kuW7rw/ez4YbH0Qq6krVyik2xEp6QivdmLG'
    b'2r1GAhmU7CJJYLuKMKqWmv866lnEcGUiY7IWQJJDvYjcFSZGQRvYtREg/ltYZYVqtnVUnDbJML7D4Cgh5RgrwVC507QqDHfjbe7J'
    b'l8UPnMPytG5FDdi1TPBu04QFqknZiGc+Yi/yjKTtLJN3aHWubKzSQ+ZLqO4QbGDyvGtR6WXo8xhp2Nfses2inRF4Vi14XW+8eOzM'
    b'IYJpAqx+8hfaTgY476CWDKgf7wwBUsESoB6gtQOGWcrETcjbJLlrOe6exlK9PYBUduL5Dq2U/iGcCvh2RA+8V4F/dhoqYg+R2iw3'
    b'XxSj5Foo9q/Fm/x3yl1Lxes0opyiVDSboMONrJNrw9L06LUgk61kBnRlCwVzgY5/jbIW747Dfde/zu96HhfFmRk1FH92fhfOpEu3'
    b'8BL8FvaUf0kaQMk22s9jhRqLZIT4H8rvQrUtRestzLM7/DYSq4T8LvQzS8P1AFu9kmz8JbOyNBNLM/ujlbVZlHYYytsrPcXMwGJ+'
    b'BN31VDybxK2q6ZDxjNa3LqFtGdK8V17Xq2ZvwR58zRaapZodjrNUr2GXv+UVeKr/WQa9oBdfpZm2Uy6u4Kvi3Y4Yn/ItiFnxtSlJ'
    b'/fcyvMGU4EJ0wQNoNMyQL94jfaB48X1RAh4NUFo3gW5Zx1jKho3woVry0RPY5/pIstlZBXnAjuCytKpPN5PLCHuHTqXEilcxfpKt'
    b'ydVrNC82NtQHogCXDEOEt7L101rEPFCB6zS1qLm5lva+AOyx7gvA8syhC8CEPXXSffodYNaJoTvAEuBNne3R7gATLXjVV1EpelP8'
    b'UcIeeinSL/9Si0oxX+riL3Tl5Q+z9TmJVzViuWr6b6YlKLQZFrr6OjXGj/Ua/uzj6DTRsrwuHoZg2VMA8PxwbRAIqh7y/SCrUvCS'
    b'6CZoL1P8mjewkZ3oXijXAkHKVvRSQpTkl5uU5+vugR47Jl39zXO9WK5lJxsX5NWsLtEOKVZePojVeyK/LQ3FgSpZq8CtWIHqvUtD'
    b'hwCgCVeIV8Qrb8dNycLJYJIAHv4A6kupn/yMKcI/4AG66N0MH8+uivVnQE83Dd4N0qN4ua+UKj9jMUoT09WtHrx/bwfETwseB1nC'
    b'N1foumTvAaS7xYcZC93grmDG0jklGazLmCRXWfH8hPy+jiOcgyvehGK0DZ7qt9h2JCvtxo0ItN5oTIeB/xBWIx+0eOvp9m79YDj9'
    b'KLTQfewpRMKNJPZnafexK55mfxzubT+rXeH+h0uaYawOQ9yaEZu0+bf8NuQd3v+Y1YePikJsrlK/zCJ+SlH5VXEjhxl7XS2pnTOm'
    b'vv8Cpn3ihdCe4nPq/Vl4VSB3nmNVwOWnYo2iLV2RV348DC+C7hDjoRjjsvTzha6jTcUxl/WOlYz6xiwsI7obey7/y6RUcvDZhutt'
    b'mdJMNpyVO2PwdO8tDfgJfFPw1nbmbcJZ7L8txmqhEA1cE2Go8Rn1JRveFgBJg+UL4dJAPtd0x035F/Hy4lW3c4rNnDEMyFM6zGHm'
    b'hM4ki176IJ7tKS7WjQvLPv2Uq6GQ9xrvOUAF4TZUXAyz1Zeh3q/xdgLsXnq7hJPi9Ia21gP0ZoI3PFjKflK8mw/i419QUo3sABGS'
    b'g/2l62sKFHxPRKFODa8pICITcAcsq/W/p2zBQMW0lm1Rqfht+Ok4x7w4jFC8CMwKX4TFTgu+2UQMIf5RoXruXbVq27kNG1FE+m1E'
    b'2ze/Lb4fsrvhAl0TOmsRxNoZCCwPBxHzNgFwEc4RpCnrNuLa4OCx1AdwMCGsux2dpN6WizoC8zZewIn2dWkaGi/BpZ+sWllnx8mh'
    b'LXUYGizBVaGhE66U5x2b8YZODJqSFqxnlcqkoE79ELqR67VoQkG5tthbkeeZwnDVJoFZTYY3iAc42er9ExRvG/SMuO2eeTFwerAk'
    b'Lbg8nNOHDZeZHueUw4XclLbQXuBGdCliov1s9tektexnJdAN4rSFXAtbCB83TFwod86WEvO76PyCsQvlAxRmkO7UWJNlWISnAmaF'
    b'ZRmy+uOWykQo4pqzy8NxJB4J73Hty8P9MdDcOfE3vyrRDAWQzL2vQNcbwIINGJ69gaaTUZ9aeR60tjaO0xo4/VKxUJ9+GZKlT7/g'
    b'WXY+cXBoBiYy/7k4qOwHV12PUiyn/fVuI8vDhfUO66y5mkhrc86VllCi//5/nV1/fFNVln8taZti9aX+GFARu+Lu4Ki0FXQaofRn'
    b'+kMbG9qG8qtgmr5CJE26yXstday2TUN9fUQYfyKrDuO4M/7aj5VxXUCUliIUrU6BKhVYRUXn1fJZ6/qDIo7Zc859SZNSXGf+yMt7'
    b'5/6+99xzz7333O8NYKJ/C9AIB+mxg/3qYpYUJPq8g9QwbDLUK1qbKAW+gwytWLq8/wH6WECZcLHrQZXH0YdadRf2YRViYFaszH71'
    b'Nv8xXOAdjVOX+acYp0i3pncrlTo571IlQS6fgfZlhWh5pMQri4E6Wy6/QSkAQpqSQN5K5HIL+agYmYYgEZDnZgTfWx5nKE2PoXJq'
    b'mGJUu307s4/v4eTPsMr06pWfBYNM7PTtrL/qtTB9SgS9+8px+sjJcXpK9Uth+uEwXVvke2YDVuW2DViVs8kP1ny/OlVgdXlCXbkG'
    b'Bc0o1iWaoIaqr30+zvekUaVwtrEgifcj1fjGGCGeoSQMFFrQ2Lyw4np46e0ZiZWrVPkD41PfgJt3cWal1WptLF3bk6SB+ltwb+GE'
    b'humPEnguNkfLKbYNkbyGa8FdAgv5SQE/NHkelbtO0WRUldu+YTiEkCPI9nbMh7xplDka16Mj3yYznCy0vVKN5lNiPRrRwXSbU3Sb'
    b'1h4CVemkkq0nRK6TSgu7M3ZoPBeHxNmB274JzAPuHlTMJ+df5J0OEnGlTk4yyCsMOqVBL4sGvWIdHBmMGVCqTirSccU6FAT9a7pc'
    b'T0ro/ahiVo3KvlG2XG/uR7wT3HQ0405mXFfZS1wELgbetgGcrcYo2EQ8PFXRjq2ivl1Lph4zQl3oEiMVWLpgufy/uGrw+iFm9hFp'
    b'p/FaJ8ZyoBPHw2a9+is7s4fojeuq/w1HUCRDqpfFm6XFK/eK83xfWrTIr1MWymeMd+jEq5Rkhny9+JCGfC3FydN8+3W+Ht3Ih/K9'
    b'uugrcsvVbEr6js6wjEgiGUE7NN+oW6s1AfGtwATENwxNOzNUunmKBdWMG1v3IOSf8Yg0y7f9YQL/u3h/C76w/bTkls2sWumfbgzp'
    b'WvkcKX9Jy5Yvx2pVn5eDwbi98vhaHOhKs5Vfy6Z+eUy5T6cs0cvSkFKfpBQZZGkQ8eefxMWexiRZGjB+KK1uzaooKE+XVrXep6so'
    b'K6uQFhvNg9IiXF45pN4iMO36KwhBOebbP09EO+iryOh2Ou8/gi6F+kBRsPUMjG9X8x1vIqVqSD5oae3luqAdDg9vwwQrpxsPSw4t'
    b'NRukVl6RUwESK5FWLSCxAzWahVdilKW1RGkbMO274HV+pZ732xJxete5YlpSy9kamNceu/8JAxoF5vq+v0BSh3PB2Vg1wPvR7JKl'
    b'yPtR4YBUc4rNkOoRLpRqeQ1Tj/j2pMTwRcPxvP9HBI1epVfeQBDA1rNkB98xrCcEqfaP4d/4FKIF8u3vIWo6vYMsFRRT/7j9rI4L'
    b'Hw7hZ7fcuRnh1U9/RUdErIO4P9dpGiTr2o+6Usg2/r99Z+0NF7GzA2hlH7a9Rdw/+UwZmRzjnhY4Kg9khHY3/cf4R/aUBQeGixGq'
    b'vdJgPCqlaBWdgkXOLS6RpiF63vfqfwJ/D1+D2T/Mt+FKWqh6Olj15FvNS3n/tDitemTzgJpuZ5oX3/5FQriGYhUz1O8HCWTujG7v'
    b'wuv8QmibN+Hlu9xmPegKCTibQnTol+EN2Ir3PYsBpEG577pR39gMfuPmBDSLhZx9pVqfx0lqRwIpZsnDT+At6j9W5NxxO98+L0Fr'
    b'z7yY8QynoiBs1LX+WF6ZY5FmQSRs9a6tmrFRJiZVqZc3YQ8bTk3A6IpMOSXSTFZ9q/RqZTVOb5j91N63oWISydfi0uI86W7w4Dt7'
    b'Df9wt+9sKQOoTQrk/egbi+E7PokPcc9lWmID8VE82xNPpnTo8mp8JJrvs/HsLFgFXvDCtz/OIrqLK4+zvNAAOvtwRzyrKf/MeMxK'
    b'WWk5cOvFzBsr39O2UHMsR3IkwGpsqGP28v6F6Fiqx6IJz0HR5mDER3m/ecp4FeZMYQ0EYznf3o18/n1FsdnE+1+JpWYKLE4yHuT9'
    b'VyDEX6Pe96NeEpXCJHkP/19DPV8Y5PcTPwR5/X0s7z8Yx24vOAD/z/0QRE3kjbiIPP/PnaE8/ynu3DxfeC27CMLfiSu7W2rY+Th1'
    b'7V8g240hTlSfvzO0KmuPC7PhFN5vjSNUrRNnYWhGyMAyDOvGSvLh0R7MbWHS2u6amAHZh8IXPi+SfWkaONU8wr9+mJ40wNJAhqrO'
    b'kPrSasjANFqbG4iV7sZ40oMQk+xzalnUYzKv1VO4NTEMftOiUzgacAdl6wk0XDlOIzDMw/txfMSF2JYbaAa/xATuyQ+J7JyGso79'
    b'++5hOIW8H+bG3PBjBFD3N1z5WqaZY4tpmPYGqA9FKaK0CTvNR6eqfPnaMRniK+1oDVpyM6MIBWMQTRjB7VihPsz8yM14Wcw0rBnc'
    b'12B1gjU0/CBtyCTw/i+RBx5Cu/YdeJxyeF/szy/t9s0RpX2CVR0WeKtWYLQ0UV9fRdtLvmepYbD8j0HzvKhVdMoBXH4fiOH9tVMY'
    b'JYD+j9IWgfoW9l1i7O8LynIKJWeY8aSVIcabM2VcdvF+NHja39aiwSfBAObbuYE2wtraGf6w3HY/aYSnY1lyd4P2PfxFLPWhdjmW'
    b'yaP2ephzBSr1KEuLYsf7FQ4GOMDlli4BORUblqX96mdVodHmqdjI0WYT2q3cq4cE12tx+5tpP6gfhIE4nUnRtazOfd067CxVsZRx'
    b'jKsslgmN9kysruZB5erTvdn+rif59nQgzC8wsFx815stXv5dwT168RLlUZWNgAS0qgrbYG6381O9F51TIp3xVLVasA1X+Xq+vMD3'
    b'1wTwNtV7ee3pgnu6Y8TksMdA6VT1ahYLj7E0x0bGcjEBk4Lz6V4ONGNxJni7dMc0oHrjduDOSdirevRl5qslhnxN23E58zU9ytcr'
    b'4Mt/rOkXvqCuMTITV6lbtPDcDL79VuzuOz+9YscVeGzZAK9X7pjBorsyKjoBAg0jxttrdDKmwADK4FH5gx41uecTvfyR70TMweHW'
    b'T+j47Ho6eDPk+9jQo8b6TqB5cOtfcZVLjNvdTU6JbynmoX/muaLqxAH5aM8XcYl9EBZ80OdIQs8X8a2f4rF3oOqQmjiUOCYX3APi'
    b'qCBleD1mSRqSiwyBR3P7X9bhaAkq3DqdDCpc85CcEVoU+cR4dBhv4qExsKy0BMR2/xiunOMwX/xMMDiykAnOf12urZzMCfG/dK3S'
    b'NoPNQq6AMVu6TClFcaE6fw+h9MB9w+98R2tf7MaaiDW0Kj3MoMsDvqvefVmnKdgXQ/QrVyEb4r6nvGe8k/XR3CSwDU88kdSQqcfJ'
    b'pwPbCI6/bQPRCKi7bZSep+ipJ9cf6H2MnjOIspdkw9cvpB+QT8udGYRBXUII6yQG6aCa3EnnyDqX0HMFPe+kJx1C6yQA5E4CQO68'
    b'n55kPtbZEkZqpy7x9Qu49DdEG5Z7beMblhHL5vIYWzg33osL53n30irlCmWJDiWgeZeSJVd1y1/Lpu3qbxfjdHNv3CZ7e0xQ2h6U'
    b'uisV8y40nMXLU9T1pETQ+bH3SN7tTT+m/scysrdTrK+gjNsOcpS289Y/iDxdsb6emVI9kPY4Wrnzc/r4tnUUFo/3p8JvNc5KVunQ'
    b'lhHfMkDQSq/gl04x7VI2/xDDRmw0DDZuwS/ej+bsct/1bcirvrEavuNPpLi+x8/e+zW/4kHNxqYNbYL42W/DayczAVp6CH6Q7tID'
    b'IOU1eyjDA/Mep4X1BiFg6oPqkK3dmIWt8Gh+Bi20noWH8CIaxnWppmO4a2Dqw0tAzLvQ8q2LT27TkRYypDQfBwp6NWAgAwY3SFvl'
    b'bWhEJO9Es6ORm3HogeIlP8+sj5KfZlZMyVuYxVLyTmZ9lPwgszxK3jZdo88moYr120zhZ2jhU7Tw12r+btDCMRMkA/AKxyht8zCG'
    b'NrJ2oIpEKHf57et6fTA10WP77rCH+Qd3NkJrPjiDPfMbnEYm3EMnDULTSDL6U1dUsNWKIbWrkhCv5d5Aa3fyxzF4N8sxcXb44ONM'
    b'458xWekXcp/auARBHq/3Yd4ycYUAbU0NaJ0+BLNH+Sucl48vqwaaj4OYG1P3fRAMBv+M5ZC/qgSv46uU4TUxC2V0JTwXBcx636np'
    b'EG0OclexXjGypkcWMDyUxlQbYIFFyBK0pcLP7uMrFMayuG0x5yBPw6u6rlzbsMCQ+czGTMmA/0AXWurFGb75C6fsNhAk9nFm75WM'
    b'xpQrkcuUBRRgpZLN9kPMqH8oRRj6dy1s+mxWWVaSTacCphMwnbcopT8E3w+YVAteMwnvA8aPpJMBYXTpuHG0cDetf8PTIpvH1MEy'
    b'bS5/3WKWWb1iHqOl9JdhLrVsOa0JdOZchUF/RUEz4NneLWYpi/RdMTDLv0UpWwB/2Ct9Jdgr5uRm3D1DugK+b1uQ3k2aCvbgLd38'
    b'nB7etCfipjBW+YebyP67KVT5MyBmJUZp0sE0EhkkvQzX9gblfXGjtz3LlVUGpSESztDoo6rHSsD+zHTJg1JuPwprVAc1FgL1bhkz'
    b'85BH8dCKmr4YTbo2kdXYLcpGvNABNMob5pcmSb8E7tpIW9hn7I1Ygo5XWFWjdmfo2K59nPTtZqpq0/UdGaR09zGTSeIJzZgSxUVf'
    b'yFASG5JMs9FUteogGUmueIuffVCzYYXfij0Y2KDtfuHJaJ9m9tmjHan2ZWiOp0asysYFTBO+/bo+ttiZO54WmmuuVDSzVRRXCosW'
    b'OYy4DDnHoFgmEE8FzMcD/7bsYdrumv4QV966Dw92y+bBIB5dBR5jJ7Qti6C/Bt+HEb3jCU01iWAxbFJuHTbpReuw4xsQlFWajivM'
    b'zZfSoi1dHmIy4AVEVgvpxb04Coyq+T3Y0rg/LzdfqjSPytL0gC9t1x853D+xqnJzkmzVa2d1wlC0DzdiWk83/r9pDZdGpjXUPUla'
    b'lt//RFrqTZjS/ozGoHYwg5BpHyoNI9NumvcuR8C0eBsKMOEj7HaNwGNjj+ARUtVg1c7IXUPUY0BVf6hgF/E8htu58j7cmKtAX5BF'
    b'puP/tgz3wPhH9viPyfuZ2U+2Pygu0CaYHVvp3FHHMwSeMuF86Xp2goV/9bFntIs+htTX3ozCt9X2xRRJX67e1oA1uQSe6ut3UKrt'
    b'JwnAVymgyT4uK4XO/Kl5FaEJK5q1he5m+XRRaEZCZoTNSZz0y0BX2MIHWAZtfLrlLWgWI7dtRluPNjSUGLnEuBlpkgRtZiQH6S54'
    b'TT/QcyLWNyY0XqZUGQ6P+Hr09z8Ri/Fl+86kSJ8r5DObxeQbu0K6hX+1De0OFugaE1t2kt1KwyWBLZE5+Ojr50d07ex4YEwR6GlJ'
    b'kVqgWiShwlMmkcJzLTIUsAKMTzAH/Fr9dzOh5qA6eUc/Qxwe8p2ZIpsHOjeXEMBGy9mYxgLclbaeUH/Xj407ML5bLe85fWiWVW0/'
    b'JsamdycOBB7A3Wp2ygbNdaGDXYqiWz1+J92Xh4fOhxCHuXkwzlK4nZZIQ6MbceRWETnyjyLjyAxgRx1kMGDtSz8m71NT+vGOHTw6'
    b'BL39MTSNl1GBSrbuYlg7Hxw+McvUL3rY4fbAo3RNsGnv2t6ZMX2Kea8leMh3dkrjlYjL8kHg0a63Z8UcVmGyYAlKexWzPnho5EaI'
    b'N//JvXyqaZfnn3Dvvp5pRZ5LywLrW47M0k7RMO8gQvYG1rNrJvN8Z2Fa2ICoOqZ+KhLECy0geQXP3Jvm1DidXKEg5nibXPbbhSY8'
    b'WCYgodLhqnE3VqzxCLYai8dtF7ze4hou3ybaVpSHIaIqHHVCueiuXyG4RMEzp9HWcF4fdYJntUA+Gh2uujpKF9xdglPLBKJCljiq'
    b'PTZPUw5ncdqayt2SqyaHGwekqhDq6t0em7PQ6RDta84LWyUSVhhDNSboMNdaLJV3MnzQ8zsINo99TeoiSfA0TeZ+h010uF2pebZ6'
    b'/J8UWtYheH7KPc9p83p/ygNLIscuOhqE8yVwfleK/vzOWHDpp0pe5HCJkzlbPELDpLlFsL9z6dxiYY3D7pwEEJVBuml4qBNhms+B'
    b'aEZsZ5urptq9rkiqHnfOKY7wCilMWhNr3G5vZAYmrUzGNJNiDhdPjryMYJbe8eTz3HV1kMMo9E1soiiCqa5ebIqiWF22BpvDaauG'
    b'OoqkF7kbuTJEwoskQk+yr+UKJVeJA1xybaLoFIqs+anwK7E1QVo5TmdThWCrI1zZPLdLtDlcwIZRZM7kEuom8xVN5+jpDTtDAm5J'
    b'BFEgCnbR7Umd4Jxa4a5PxS/ojqksZ4zH8hjKeGq53e0RcldzTqFWTLVBhlgqHgQgTBUwaUZg3EFA3sU1EBkCrwtOR53DRf2hivNK'
    b'9R6hzmZvquJWe2wIvV7FVTtWr6p2e71VXJ2tZtVqW50Ar+DP4XW4VsObCyMQhZoqbrmtrs6dwi0XoZzeFC79ZiDVOGprHXbJKTal'
    b'cILNC/GtsXnAb41QawMq+Khz1wgpCBLeIHgxMEg1+Kt2i/jlgiqB5MIwtph16pyupmic8igfDKW81DUptbY2mpwv2LAro2yOoudM'
    b'Ss2FfE2MmGgQ7USQ9FAMYUmEHsMfJbZqaBIESETEY4Z66IDumO+o42C0IChERx0KeeoI8A+sGHpl4MXoRRLdZYLd3YDCVHPMcyIr'
    b'F2GPKXJLHs5aXFpvs0NjQj+qd7ug4SdUqIAow9GFdzoxL+d4uymahGx9jr88gnOfEB9kM5oCpa6ZzFcdsKJ9QirUsydA1bMclgtO'
    b'6DJCDaflJPy9FApOaJcFDqdTKw2+hnLMSOHG0MjjYwYwZfgjlDK2J7k1CBCVYFqXw42PxgXuBtYo9Mxx1WihUxnVu0Zwhobj8iJQ'
    b'Aiw2cU2Bx11XnF/i8IqVEWNxHnQxjy3XCS3ncYMHF/CFzUlXH0R8l2B/iSTkQOeL8oCiE9ssyld+uD9GRe5wSWJ0dMir6RMJNxGh'
    b'CPQW7S6GiM+IKBhlQlKMOB7t+PdNHFSvCNLEi+MXNX0ZeCil+vNG1DH1H67cDjLPudghNKZ66TUsKMPyMBzC7MY4sC5T8UKK1R5U'
    b'fFJNNQ4UsxERg4dyp6OGDY8RRIvgsWN/+XvSLHAIzprSWvTMLceS3ljr9tRV0f0H+oS1d0Xhx3KlXAmXy5VxJi6Hux2eZdwq+rIA'
    b'PYdbCl8F5Cef3M7Bn/0Z4fFrEWeFZzlXgdOsC5MumJoQH8dNiUvQJyZxMbHxUy+48KK/Gx8VtNh/+H6MOmqXG6uhin8yip8T/h+5'
    b'3yOc/wmBr58QPi3Nm3JPSlpaWoppUhz8SA0ClcrIb1QTowWj08aGhrSwaEGQ0+j05qRFfsPU4r7oHLUUTj0yawHH9eXCRxbHpcB/'
    b'S5b7Yt+tG/d8m8lxW7NWbNyz8J3+fvDTlgffu9/pf3H33LkPod/sFRtzF5Ztn7mQ4zbkcS1P57/TX5I5d64X3A7l1dZ+DuGSsiyB'
    b'aZm1tf+SdWrR9szeb9dkbZ8ZlwVxLzzGH8je96E968Gp8VndD7ZmIRrj3Llzs672r82a1/Lr+fC+G953G18pynwzpTtr6pE/ZEL2'
    b'If8tuZAOpNkCv+5sq9UK/9nZ8P3mqUUZkE5GJuQ388gf6iFONDTPRtrr7oun3Ar+s/4Pzhid7w=='
))

# Zero is a login/disconnect sentinel, not a keyboard schema. Preserve the
# current executable as an upgrade source for the corrected controls hook.
PREVIOUS_ENTRIES[PREVIOUS_CONTROLS_SHA256] = ENTRIES
ENTRIES = {
    "sandbox_controls_page": 78813765,
    "sandbox_battle_fov": 78812938,
    "sandbox_marker_visible": 78812994,
    "sandbox_zoom_changed": 78814250,
    "sandbox_particle_update_queue": 78808752,
    "sandbox_tracer_update": 78807817,
    "sandbox_tracer_position": 78807934,
    "sandbox_scene_shader_clock": 78808878,
    "sandbox_load_keyboard": 78806762,
    "sandbox_save_keyboard": 78806831,
    "boss_stock_panel": 78791602,
    "boss_screen_root": 78791512,
    "sandbox_boss_menu_open": 78783232,
    "sandbox_keep_replay_markers": 78775726,
    "sandbox_graphics_page": 78778162,
    "sandbox_blur_material": 78813082,
    "sandbox_scene_graded_blit": 78813657,
    "sandbox_scene_blit": 78813549,
    "sandbox_refresh_score": 78774560,
    "sandbox_message": 78809021,
    "sandbox_reset": 78811684,
    "sandbox_frame": 78774820,
    "sandbox_compute_marker": 78812530,
    "sandbox_draw": 78775824,
    "sandbox_mouse_hook": 78768652,
    "sandbox_binding_menu": 78807048,
    "sandbox_binding_caption": 78774127,
    "sandbox_binding_name": 78774068,
    "sandbox_set_binding": 78774272,
    "sandbox_reset_bindings": 78774186,
    "sandbox_controls_version": 78806957,
    "sandbox_migrate_controls": 78806878,
    "sandbox_minimap_extent": 78795927,
    "sandbox_scene_particles": 78808263,
    "sandbox_open_replay_folder": 78807415,
    "sandbox_scene_animation": 78808167,
    "sandbox_scene_wind": 78808191,
    "sandbox_scene_wave": 78808215,
    "sandbox_scene_trees": 78808239,
    "sandbox_notice_attach": 78774687,
    "sandbox_notice_detach": 78774750,
    "sandbox_project_markers": 78775376,
    "sandbox_gun_marker": 78811948,
    "sandbox_blur_slider": 78778389,
    "sandbox_training_roster_added": 78791422,
    "room_header_description": 78777353,
    "room_load_description": 78776969,
    "room_get_description": 78777684,
    "sandbox_move": 78774993,
    "sandbox_stop": 78775258,
    "sandbox_zoom_migration": 78795892,
    "sandbox_particle_lifetime": 78795744,
    "sandbox_spread_ring": 78795798,
    "sandbox_setup_reply": 78795826
}
BLOB = zlib.decompress(base64.b64decode(
    b'eNrsvX9clFX2OD4Dgw44+gxKRq0VJm1qlkxZOattKAxQOYWOg/gzLTHFnwvziJXU4MMgl+ujJoruVrvt1rv1vbm7bpqiWA0/'
    b'AiwrRFZRsaioHhy2HcVFxHI+55znGX6Ytb2/r++f8XrxzPOce+6595577rnn3J+KDv7S7ww7+w9D5dSAaAqIRkUJBAKpSkKM'
    b'TidVmWa2Ltl36bKTfelglWGblw7XB+o8XpdJLo5//M86Vj57buBzi3f23EqnMMObV2HOGa5nX85wyLlGVinbFKnNLETalMD9'
    b'qYF6wWxTWKMQGSpndM5+HGIAIvs4zQGEM3WL3ZCPzNBMXaZ+Mby5pwU2x59bESp1BkSDxeuzsGqLl9n8ss2fqUuFTM5ICxRv'
    b'BQRWAy8x7StCPQHXCW4zWm1+YatX2OeVMy7Mnqelkqa8+EudLq/6f+DpyGtrhASYJw5KGRZXO0LPto2BVyaPw6dnJDw9h12r'
    b'S8MAy3KYbXhvLcKnvOeOlzoF8V9ywe7tOp38+6oSJBILYfKzRmFfKN9G7/IafBreM0DsVE6JKEOIMJKfBay73uLNexuxHJMf'
    b'sUBe1RBfWIL1srhffucI0LWcrvEkAlAPROTk/uzl8UQhBkkXmOMh4ZcnIuTleIIPg6ew7xYuYzn4NkxTTrhHy5wnHZ6Bt/Bd'
    b'Sb8SCEhvI0A30/XrpaejZQ9ShnwiCX0lTzAL5lCpLZo7olhirJlNj40SIg2xGmXijUoqUMcpO8nWGjFf/n0DZFpO6K8lqWaR'
    b'woV992gZ8ixGEkRIfu6eGcqR7zAvCNTNEjzvQElrPCn4BZwrfQg+pSsRroGWeumKQU56WNheMaqOeaYAAsiBJxV+azzT4RlO'
    b'mANEUw0FOuDTN0i6YlodLiddp0aQ3kYG61yxfOt/sC7lyQGpMzfnOp74HzdUaY6JbZtCfESqSzuAKSkqU7AA+mqeBEzph0xJ'
    b'+0GmzAjUJVhfxsILnnBMopfwKSNiQfZqRsEzzSG1mfgOf+A3Bs9pwVONgv65Xsh/G2XfwKoX6+J0usWbh8KXLrA59jyId6XH'
    b'K+S/jHKwftgGEKlAyQb/ilC+0YuyXyGvvx2B05hTYfa2wFHuVLi9jSWMl9ePp4DAUdneLm9fBSLLEiYyf6Ye8JR130LG7W0o'
    b'jYOEfbb2iQtz+jFpM+ZCL2+PmwzIfmUuILGK9N33eepdA4ts7XJqo7tr4uqQ3fcVSv8DqIXSa9iOfo+lgfKyRipx4JVvIX9Q'
    b'7jR2HBr21MDmCx0rQi31zHnB46WmPJynTQSS/5HtF6aCVoA8nA0Ux0ELttouBJtu5+x5wLyw+WFv6NiXgToLYl0nle6GBENE'
    b'k6XeXfpn1FvCtgpWh6rHPHk8pMizpjj4rBS2KoqLw1i6mc+NFiJ1oHaMbFbKbJY1ZZ5UlBKi0+nnCpGTx1c6oXV/6SgaFpF/'
    b'2tV/0q/mGAOu8xZv/unZcyEbpM9QdbCPHOyY1DaM2RpRA7XVgILBpCNtTVLXQnFgja0Nm2m1rcni6285LHWFr76F29rejUD1'
    b'US+n6FnjxYuW+osXAShVxBVOD2uiMNZoqa9OHHjmtosX5UkhQFgu0LWW6ghlYJPsbOL2xmmBut6SxGrkvLo/L9GlyWKTg33A'
    b'zkG2AIVdkiV3y4e6vMoYDATAOchs4Bi3NfZVgiXDUQm+Ak+Qw2FQB3ewGotXOjglBAuUPxjKgUVs5rZm64Z1MQN0Opc5YGuW'
    b'cpsHiD6Lt/UcoLUL5rw5gCjMOCyMLc+OKMXCt55QQzYW7oCQsbVZrSdzv2oX0gs2wPciboP3SFvjSVuj+4hJyjWGLmK2lpPL'
    b'Wz7NbTy5/CsuNopLIElhX35IPOTE6nfNgs8am4JVDOzFH25XypuNwHv6mNsW7k+FpsRMsQfgO4bNiTVwmx9ap5F1Mmejsv9u'
    b'7LvM3NmIfRWf28z8hfk3InWgzPPXTYGyYcP6NaoHyJA4AHCEfXsJhV1i55XvQD213nJVU05THDHIwrkxxMIx0pooAZre6hAO'
    b'VV2GnFB+9Rto0+N1rEYMlT4AXVVja0I4s9UxWwNyt1EuSHkBAUfkgsdewAI2oz5ltlruPMI6lUWQMGbtb6T7+8kltZsRu0Wx'
    b'U4ArxHKa21rkkn8CWLGqsLAyrEECNyP41iA4NAhuRfCAINgQBLcj+MK3GjgsCL6C4DNBcD8AF/XPhzzNgoxOnOsKTfb9Gz8f'
    b'wQ7A/WCMq6sVOyLurIX8v65GGwL9llxihAIqW1GR2Bp9ZXLJDCx5J+kfj1cuGEKMaBHyj2HKzhYIekqNPpSKTtGdFL2h9Wms'
    b'jJIbVQrx3WiQyq2Idheh1bVOI7Rfqmg3qGj3yskmueQeNTXFAEBUSE3AZs8IzDiW2YpEWi8HAvHM1tRqQiqPQRYexEi1zN6i'
    b'fHgZlWbLxUZoIcI7ejm1zt0VLqz/HPvU3OaJwqZGeFvqbI6YJCeGQG03+06yCsjDtstUodTFduffrQH7987tUg14/grmCTR5'
    b'o1wyD4NSDNzezKL53CaWYgStlWJiKWZub2Dpw1hKFLfXsZRoiH+HGj9/k6p6kvsBqrONOf3CO2Z5Vp37MmR3C4rVrDo2twX4'
    b'Lc9tY9A2orjwTrpedmCBVofG+4YudbZESN4QefqNdZzCpa6bnp8tvBN98FcQHayjsINz4cV6TEySp+rjD6CES10h4nbubIOW'
    b'ymrYVBN3+tlUM0iHCOLSKZdkqGVc30U1Msj9fP8YV5hU098XKtUYpaoxrLNHz51IY9UO5Yth0NYqz8FTajMyu19J/Q9U8TnW'
    b'JYNWideFjfr0HwbQm9DwudjmYKcDx4ECEamJH6Mj6w2qOSWOFbZgCzy6PiV+fWpipfPAQvwEe/aYMvNJUnlSoh4VslKF5kU9'
    b'cK04EfUAGEEOM3OYmMPAHEbmGMkc45gjhjmGMUc0c6xljjWseAP01qy4kJ6J9JxPz7X0JH1SnEzPKfSswsSLozDBYjM9o+lp'
    b'oKeOnrWE8y29v0TvO+i5FRWiVKpT/2qkvdob4+MQl4+h50h6xtJzIj3H07OTnn56rqLnYnru1COtQ3qV1mxN1Tmmeuqhvz7t'
    b'W8nK2XHZXXWLO9QaIn7DnzGAqf21nJe+6vlQa3/xjPT8MJ3rxEHScrVYTf0vQjWB3eKq4KC3/dDtGxf+MRaDQxfy6/DFWie+'
    b'bn3WKIZZsw0ugZ1itcoikHn2rJFlG2ZploRy+kao/4pz8IQMQe8y1wgPpwkedrMwslIY+Xm7MBP/K6GrqRJGfgavn+FruzDV'
    b'K4xdtwOYtnpQuzCvEf7r4L/WN0iYcRriF23FfmrkMeiWoufCI9UAjxSjaq65MNmaZ+AJUgguTLD7b+GTp/Np6cwQCxaggTvm'
    b'wI8RVIfiQPVde0dCLGZPEfbXlStmeJ09HR7L29qF0V54+bN7B7lT8Pk+d8RBViGb6bXwuwnzCRjrX6LfueXwvukVep95FP4h'
    b'9+mzU+ClFf7t/nZhzinAcKTDFxRqZsIcCHdMgUdCKoT9E162v0ax55RDDUKCkbPnw8Ox0LeU1Y6qC+ZvNKQ9+iwQOIK520ox'
    b'0j/E/OxU8wGsND8CMeecBGATBJ5EwEIAHNVozmwA6FGELsbUKBGtYFiIpcvg8aTLcxgCbkasVWQswFcC4Kf74L9ZSIRsRCYB'
    b'/9Pfh5c0I/OzR4A7kSsw44sgn5GPRvXpgsmslNpG8nX51K828HWF9NJYOh3I85IN2JeYQjyHmf2IyyLnNgX2hrlBuuRi9+w8'
    b'7IhnBOq5/YhcvOqmOh28y8VoNrFLgfp0bqub9QBSEfuXpsOP7x/BpGfIa2ONUos/kPOt5rYqKdFoCkyDJ0nIFGbbq3zxJPag'
    b'e9kppesC6jkx1tJRkxAduEU3jDWy7DVsD6oLjaj8VOfjrVNBZ0vPjte5jkM8Jfw/pB3fl54168Q0nhADH2bWNaoir+sz6LSF'
    b'gk+hR/cEiI9gWZANbKsBE0JVdzW2FlIHCTGZkI0m7mxGI0kTWFsL2ke+J3nCeKksVjVKdroWZeYE6hZ5vE/PlcpQceiItjhV'
    b'ow7mVYhOo69Rr5EQD7F+MBGVBXePIiOs9Y5QVcliNDWEz4I3x9q894HIsOF1HLRpQvTF+uEVzLnb/XmI61aeMIwXo/0n+UNY'
    b'wjCpOUTq1EPcGw5L5aHK28k6XYdtt6EPnpnwzD14Yco2wBMOZuw2ZRukR9bEQ5QYV/+8R9Zg5hEekR3bTcB9wQ0EbtO5gVY3'
    b'iShlmkYiPJsimiHfCcNYed5nEiAy+66dIa6YzBhu26ky0SDZdxkyI/t8GzsShpmF/KWQqPTsSLCzF6DW7QR98cBczMj6qciX'
    b'hDEgLfHcMZ0/kg78lG2lbEqsAbqiqFhN1Si+LjQLSrnzEOgRlrCMOVZxmxeaC0tw1SSg7gc2r0+Ih6b2evRvSaf83vxbVHSV'
    b'anOfeUQY+0GOAPCSYQSvaBceL8c4iYBQJ4z8ANu0MPYY4fx+GNF4/BTBzY5Vass3b4yiuK3C2KOEtzFG/QbUs9jKXYi8jMpQ'
    b'p0kOWC/CyJOaLjulKq7IuSA6oFbmYLu3t0A5scBoBGJZA5fAlR75IbDPTEpzJmgmc4GqpSKXrlLVjdleh6oeQrFwNiAe+cgy'
    b'VC8u3/XEUFd/6NT/Dd0R+HLCSEUYWcvFXQOy11ysXnWLQSeaPB2iID07TieG+54pvR+HDsJK/4L5dcSAd2Rkl0YdtZZnPa41'
    b'OMxnEzQ3+F3ejB5NCzQ05gehc4XyhJGp0BjYOa05QFuAbs0Q29MglBG/pOaAxk/eV39ovow2aDbYE2tmUKPg9t3K0gU6XXov'
    b'7aB2SF8MwQ6pdQh2SB+R8jOD0yGv/wONBxTjeABafRJadtu7inW6aQCl8QFbG6S2Iji4FhIcXItPCxTH09iZ1BlwPdpnYA0H'
    b'AGcEihv8OLwAsaO5Dfxtq80vDgKw24++PKQGjrezZYY68gZ1Bh3NACC6mIJ910EYeLuZ4QFRAXcUw72uZhwjqFP1+IGbyQKb'
    b'kSa/jQMyyneDUZX2H0Kjc2TqeBbSSE/7G2zbHBrrCQ6Y9Rprmk/jSxO4PKd7fEmecc80vg3jKst8gUB6njrABKpTvJ57lmFH'
    b'nf/dWhq2WYUfid+5OdFJsFaKe2RKJXOtvNFfQqN3C8k6x/E+JetKH1cwjYtGhzIJ8135GDzlpA+lzkGrByu7zgJZ0Tg7rHnp'
    b'cP28uYHmpd5oOT8UR8RoTCMYtR9Fjeod9TfBqLplwahxEDXuge6oNBqhfBCJUU/CU/n1WbKIK8N2jx6hB8e/UXEAxOoXR4AN'
    b'XgHao1H5FlG64KXgivqCRWg1rEWiwT5VzjU62BUliwi7I9HgjuH9+fPDGLgdtiblRCt5cPIOHC4FMZeUeyDLOWFSp0FMmiGY'
    b'kwxSWzSNiSVF8bTo4CgZNPuA2BQ4xmrQq3kd1XRu093C+pfI22vmtkZl7bM6XSv22HkPUi09z5MNKTg6t1p63ijkrJLB7LI3'
    b'gx8PHT3QUkogJz8lwXRPQByvpSGT2yuOCogN/zXmu+hIo6SSN1IRo6ORLGB6mhl5sxCeymFFY/pm/Rlgumr5NirmNuq7H2IV'
    b'qrULxvID0jupGAqq5E0spM9spV/RCG1uHb5JB7Fp6lxDpIMb6CUCK2huayQoraC8YPInBEz+S3gqDweTj9/3Sa/kX/BR8uHS'
    b'N0a2G3s0LbrDofyOIu8UsMfqVCIUcn8rlcd96NB2krM+tdO6WVFzVs/obZYsxb3ziX42yKHmD/C5Rp5rkqUPj//DMMMRqJsH'
    b'TBby0QrjM8yQg/faIWPVQM4FHxd8lIrNzwrQm+HryRNJM0HwZLm48d/rQq1mqKFUUB2ufnyaCYK4Gz1oQBjOU8aDk3kjnzSe'
    b'MlecuOV5RA9n54Fyxr3Yofixks5r0jyv46pxGofy+iAs895BqizbIOdmZle4GI2GvM1EfmMUjXWBad/EbTEg39wWC2KmtH6l'
    b'ZV154CzlJ4dValUaKi4Fx3xCgkEEM8rI7S08fqRVD5aarU0qox6Y61z9HwC18eDqSdaEkdQBrTiH/XYbn2HgYJqN5P2ZYwpL'
    b'SOHJRpaQyvvxNANLmM4c6TwJAHPAnVX2Y5tNiOXOBm5vZGI0F5txeAuK4IzluW1MjIKuM2zVIUnPxRZmj2G5JnS7RSNzDpOq'
    b'YpAPwVpTXh6IjHhjIHnRUHLQPuCkITtsbcqVLzVZcLRSSWODDppZvAEc+AmpBjGSD7KmjBQjAC11pPInP+Qs1wyZkKW6ufl6'
    b'SBPqAtKfNVtN9SpDGTIwjDIwmjIAvopZYLmvMPtr2JvuUHIpA5bTrCbMXbEOLNKXWKdSrWiCNJHbXgMOL+QDKVODxFEAvM2a'
    b'ZBJv5TtigN2e09BBhcqbJv41O9Ra6zLJm1adWodvIUDyJMhHp/JUBqKJt3DbK+if7YB0zdz+Gre91KujDTTzJLQXTrFOOXen'
    b'cj/qu/Pg93YqrVB5gtm2k7RGwREaIUMTrKYAjWFqewVoKYOxw0qaaWAYdEsJgvhG/AbtYgZTYEpslDIdyAbEHZA0ZCBQB2Tr'
    b'gsoIBNHZQsqoE9UR2Mi3A7JUFdcrkxUGXbAP4LlGpWoA8rUensr9LUjYhFphRpguSsQ5uoA4Wso14axHAeogHj+ep8yBVhUl'
    b'S/O/AR7pxYhMfV5lTGqdLnBMS6XCqLuq/pRFmErNsgE4eZHXhjqK2d3sslL1hdpI8sNWxf8T+mvqVG35SsTXpGyHcjv0rGt7'
    b'xk9VLkul1EhYpRgulcaTahzT3bRuxfiHID6wq5xV49dj05Df+VCwU4rPgQa0Ueca2x3jNqnsW9Sa4mCpbBj5LeFSGY1muyIw'
    b'2iklxKrTBdEFz1OUSbc1wSR4ZuM7r9XTeJaGPfkuTM7Nd5Nsdbh+we35vJ/8AolXHYjXCyRedSBeHcFs9VeHkDwunDfsEPKL'
    b'QzRr3uWSyshhct0imCWSHInEopjEwtYMVhIYkXW+KCxp1VdquUGOFPHfJHMoHOAHkHxAT2ZvIVE6j6JkOaz0/xrlI5rxFpo6'
    b'c2nThVLZZvIK8wfQYHe+shQIg+A/mgKPtESwTcF2ThsPj0cnMrShk6bItl3wmRTPyPyB9uGsA0cY22cDaCAaug+6d9AP46AH'
    b'ISqzvyHKGbtwbGZ3z7gUfIp7OQ1Wge3eM0pl3YOmneDBwTZPvZBPPULtqDo5NSB1PSGsR9GgcZ0j6pgOjkzgCMnUw8LYTeno'
    b'ZBSE64n8G7vUZKSdSHDkt5DHv6p5N29Tf2d2ItpuQpt5DOicEEZegdfv6HXs9jlE7iCFj0bc7fPJ28F053zTLsw7AbC3NVpA'
    b'fw5Y/eZ3pxPOvHJh5McABu9p5vqFSGhsDXlC7y5WPaZOCOc0nQYY/4SAv2PE7oEXdFjWu8mVGd0ljMURmxoELVNzgAMifC8V'
    b'7EMi+3eN7FkAQDiOsxCvQVIZsZRx5DWTXlKdlFd3qr8vqlyK3LNbp4rC/BaSsXOprAY//wTexiyQ8uulssXUFAfNCAr19ifA'
    b'EeluwoE66W0XTaH+EsSylMSykJ0nwbTX8rlHQCTNbA5ouKwWEso+LgxoiS+hA1b8n1MXcx1PHQN9/iDuNPn1qfHcaYafxG5D'
    b'tEzx90NnoKsfOQM0xiPTDHy7kvopqBPnDlVOkeTir9CC2SGXZGxFn2EVGfbkUbzViiEvaSqoRIRw5Y8E2xWEFSBMJtjuIKwE'
    b'YU8TbG8QtgthGQR7JQirQNhUhM19LQj7J+bhpDIBobadcslX8C2LZaDKqHFqGRvWin7NAStWzV+gDDSFVLM6DE2U8dRhdaZB'
    b'U6lx4fRR4C2MScgOea5bapvIvm5/jW9TPRhqhAUzoG7plZ2UqhIDbyETwnCWEKfoiVHEPkeYzt2qD8LYybAYQgkqfrVrUdaE'
    b'obZ/LkzV9jiuhuaCyv/LSt0Zlf+VzK/8q0Vl/faLxPplVCOvBatmZ58yj1R6M297v23AvCilN5O3RyNMBzDpuShhdQi/T7aV'
    b'qRXKKiC1FifwpNo12Z1bpnNNtNRzKqZsO8G2YTqMWr2lXnpOHwDXVN/9eRN+StUGPaGBX3Sa0wKNYLJ3bsM6cisFXyOfXL1C'
    b'HsAMrQQw2Dk4PHgu8NYydd0DcUOWzMDOXjOuPX2oxso7DcjKe+CZJr9VCqoSGJoaidK8+xvi5romTJM+PHvhGRb/J0UP3jfW'
    b'EX7L2+2QB3ZuWndtIi5PMajBaWE450uTvZUqhKqUeXZ+Q9m0GfPWmHHASlj/AA49gZypUgYou5AQPeXtmVjQc83UNm/gdiPf'
    b'RskQReXX0Oe2XsE5n9NCfgeKg5q1t+ow2yfTwvy79+tQPbTAt8Deeg1+Vpu4h4I9rxGhZniCS/ecsB7n7FXy8su+7kSgzTih'
    b'esFd9eqPFW0YHwe+d6EpNF7qNIl+NTp/G0nJs0xg0xEXZJfZNOqEvO0svFvPuSImFSUK7YWGCN95kFeNUduolI+a9Y3yy4g3'
    b'qkJfzj62droi4oumhAQgCcDehjmAUqfKHsxRal6F/9FMXaCeExtluS7IzKtq97EQrF1niLamg9nboEZ3n6JlFeA5hG0e95GO'
    b'6pLGYNQVHOi0bH8G2G3pUO5s1uZIzARtxEq4AWFg9vjJpVO8Z8ig2FZOwy35R11zwNpN1wx0tvkVpLh5M5kSCkQS9r9ffha7'
    b'l4pg9yJvNs8rxU7mMDupDslHSjh1D6XDyUW/FtuvRH5KDkrfIoLl/oQeLUycKpLazOQ3KMqRk1hGBYoVFnNmP8o+Gj23ooWZ'
    b'NJ568hBh/RCM0omz6k7IWpty/XIIfnS89jVoEU2+e/5F3VKbpV75JzSFwOrxSLcC+6Y25RHI0kx0L9F53KkXPF5dtyG3/hUy'
    b'I/6mLgsAbIjKupT6pdidAd1tFAD+nQKOJjLW+wkOX+AgVC4AhRmHoVN10yKGseVZmfDBg+sW5gKfJFy0ID03PrRdGOMttLWL'
    b'VtnWnqmXedx2HBQCPhXjchplPvj7s2aCrRvO1yMjARVj4XoasR8jkLDP2U45aVH+eqbbFAYGc0NsJY+CfxP8D4P/cfA/MbZS'
    b'9/Pfz38///389/Pfz38///389/Pfz38///389/Pfz38///389/Pf/89/2owqzzU6lKxvAoG8SjcOfH5ZHQjwjQtxhqrSc9iV'
    b'yvUBGnFj5biAgevkYloPXSfeF0g1AGhskfskBE8cnXPbxKE5ERP1riETjeINvoGScv3BSJ1RJ7xakR0hS7uXf6pn5wJ1zM9O'
    b'Kc8NAYK5RlnaXLUVoL2m7dlHDgd/2Mgqq23tAT4pzuMV+1u8rbhLjuvYCbk49bOXMXWfXLx7uzvEmhovNsvOdql8rSzpmtwh'
    b'0zL1mbcE6ty57ZBj8T3IYDnmuXTs7zHWW3mVqV+tC7GmG8S/ZP4ycIwK9UqNbnqS486aeMOUSZPvrI43OvKu4AS3aOTrcW7V'
    b'N+RitW4+rmXh6zshH75Qnmxmk8J5chR3muJZchSbNBDXVPwmkk26jtuMbOpNbNIN3GaunnSbnk0aTmvoelZplMvrcPBYnfVp'
    b'UhdZzwjUSbl+gXWuDqWVpW2WDvnhgHTJkBMjm8zM3ogbGR6uc18KzxGWdkTEy66hddzZCEX6Sh2KZ5doHJ75g0tBHFCygaxy'
    b'mpyHE93sZKCOz/32sLZIRGob76l3Pc4q2XHZWTVVdtNI/1HmPCSXqdMjnL/yzW8MwNPWUh13HmI2LwjD8A6bUe/6hSzFHX1R'
    b'J89tmubAUXhu8+LvycAJn3EarsuEWpbtVYETffYCXt8GIlZzEzxxiwN/eA475jksDs80qFv/TDpTz9a/hDlYMbRnLjnP6pg8'
    b'/R7XzYQhbz+zgQZuD1bhKCnIBk+YY6lnZvjJG4+I4gd5awy4oCqnIm+NURej0wkl398UW+CDzFRvhiewYhytU5xs1EStEUd0'
    b'p8Qa2TG55HZZp1OePYxrUnD54mcI5wlGuSQZAiwBZT4EIUwuWSLj6uNxEKg80g1kBByPwPu6gX8h4EQEDu8Gvk/AeASauoEK'
    b'ARMReLE2COy/EYEpCPy8GzicgFMQ+GE3MJ6A6Qgs7QamEzAVga/W4krTAt0Ona4mMXbMyjVjp7GE6fAatzInTidEJsYOk5Pj'
    b'KNr02GGFibF3Y6zltbh6sCwqWQoYxU+5dMiIY9qith3uRZ3KPLlkFST0LtSsTpnQnbiEiSfGGpSRAIKEoiZnLVjBJK+RchD9'
    b'5OI4/B2UsCBr4SJ4GRynY51a+lGWDsjBjRxjn62BLBhi5aRg5gZB0GBuiFXqMCR/z5hwnS5JuhImNiVLlVGsc+I4cV8rLnuQ'
    b'qsb1kgJ3K0pBETypEdJcRjmz+XGku4RDXhV3DY75++WS/92ozTa0KctUGEidND0EcDbTllI10gGM9EgPghHXqKzshfA+IowO'
    b'Um1EqpJ5AM0GKEOC4BYCR2ngb6u7yQ1DcjFIjl5BIkcUOZ6DQO/ZUNCNJeeR+hHAl77W4+KkLqWdtm7hTiF2CuT1o/twLQYl'
    b'iBSVBgxWU6Lvqss4R0uZMG3CTNRqmchRM6EuuZNSsdw7LtNMYxlS01VLBvxRt/q4Blq8iy4mPJfyhLDNy2qo7ApkZhHu0goI'
    b'+R/rcOEIYhjjKa3rN+GOVkSlvCnhamoQ5R6IUpTwHGHdDVhepV8QC8qoNL8XRIwgxOcJMQUQ65TeeKXdeJ939VTGk4CnvAxB'
    b'TEoPIXaOl8pSaRHNWOCYTIVR3qe9Za6bOCFxp1+xdmGU0ghiDZMQS6oy9V5U+hKolYXMtpnbNjPnVv6wmaYLScnvFkDF577E'
    b'7btKH4SUrOVCcq3Fa63NziyK6u/uqhMflVfprR9lTy+a019fOPdFqXKV+7KQM7DI+WJ1YtjeOqkixje0COHzES5o8KPS2Rjm'
    b'993szn0xT4wmyJsvAGCgnPUua3dfviEnFGIWTg/bG+/bUp04cC/UgKWD524tApDl9Pit/UFbQiVUsk/DrwC4UAXEW8uzT0Ep'
    b'ZPtLPfPmWifj/pVO7EfNvtI9XieGWrzU71gOqxuLI7WNxfEYeBZUeRjh4i4r3H4MvdA0nj6HFa6CZFihy6hOtf4VUPImoCYX'
    b'PH9EZfKIIe8S6nN1Ax+rlirTLAHrJXXV9yLp2X061yr3swGdmOkuewvtGnEBL5hPSsllFPY5DkAf+RgvWNgDKQXIA7xgcQ/k'
    b'IEDukEuwwKwRN3IND5CUQHAUL8Csyal6VoIvi9japPnJeZW4AK5VBLT0Ppv3PnLQZlipLYrZWmpsStKyBU8tqrG1gWKzfTP9'
    b'6VUZ8HE+TicnxHF7i+xUCm1twr6kUGujsNWbAN0ULo7qVMIracMldzYrd3dCa74cIX5IdIPxvim0nYd4hqviHa3ojtd1EeOF'
    b'iR9KVVG9JNPerBS1oNbb2qJpvVRmqy1ahwyrsflXrojTSd446BXMO3Be73zROmQTnzZOiLQ1AzgGZwgBIdL2YY2tKTs7DpA+'
    b'4bbaTL3yJ0iRFyOfLd6Z8kbXJlrrtNXripCjtsgbhq7C1auO8amWAOArLsR2TMzUy/YP8XshfKfP4oll8ZZ6bq+VDpVhlnCf'
    b'e7JyF5YlECp+xhPSKdRdtoYCt1Yog5GOhDJU3hzKbKW4rxuMFbngi2La5iMXvIuKzPYaGCY8UYJqqcJZ6f2ogpDUhDKsVWFL'
    b'hVLbEQhoXxIt19qf+Ox87phT3myUp4RA57BXNoXIiSFDuXOn++mhI5h9t7AVuotD3FYl7DvM7LuAnPJoOS5IhNdDrALLtaED'
    b'Z5h3A0MexW2g9kPCvvhBEyNyBsoGt5zfD5mCJOy13AlZrFIGQfxMXXd0O0Z3VuF6Efgq+S4QmBWweZOlXK9R8IxCKYW+3F6L'
    b'eniVxi8l8B91twyBvcYJEpaJJ0zHQn6BYQlz5I3bN5H0R6lWk7zRA9+ufipcIi7Q5983YaVNUWvYC5HT+642xj0ZJu5eCx0u'
    b'bri1K8L+VKOwP76fPDEEd+/k+q3PGLNS8wJoiT2fwsrLP+snb4gwQYsG9ot+qz/7XqkrzBUn7LNysc16UbxZuhIimoR9T+sn'
    b'lOUDWXGglKtAXrioMFPJ5HipPN5XDQWrMgX3VKB1eeAztC7fhWeapQPNW2ZrYs4j8npVDLzy+oJildO2I9AI2HkFjxHBBVy2'
    b'2qXVUfpGy2mQDKgAnr8DrQdma0QxqUMNZKvl9iZrJbAPFNRWbzhUTGOhs0HYV69svRAIuHMbdEL+6yqmtTJ7BE98uRlY+wv3'
    b'mgd14nXuNSEj5JQQPO7EXkcttcFnltdXbcIG16DtkIcEuNOLS/YHvYvy04A7myGdJmUKpJAOmSaMQ9aXdrvDUfC9KiWM0fQO'
    b'NnoIbVDCAFm6oheH4betUd5+HHu4sh6ElvZAIAHUguCJhGTARDIInrNXrl5CjmohijZAtCi/fBsjN7Pq4Ja886MqpK77cvpb'
    b'Aq0NOiwDHVYAHXzwvAJnm3ZYQUA09t0Q5Fc3BLUog8+jKWbG3RGu/Z4O6hdAbwmJfk42Fe4PiuF7qPu1tGMDN5KJ0gImyvD2'
    b'HpMFvyPbg+aQ+q2nb+zF4VtdT/LNeTo75ZzaVatGDZNo87FE25ElM23QawnzPrRVr3Zx6p5scJJiecSo8rxLezfmh4K3sQ7X'
    b'C1ZavHmXmt8DQDhfl6ICKsndKufx6BMbrLXd3SFPN1uPiWE8Psp3nfWUUFwp7DvNDY0a1mfqCUK4J/sDZSMu6Do1e65vj2qf'
    b'Nn+CmvosPB1T0SxdV6VXD29wE7/86KF8gjV88hBERGZmkv1zDmHvHdKsyaF4ioJ0hLbeKX8PQh8gaIMK/W0w/mgK24qHIHiC'
    b'mK8SZpOKuRIxpRb46O73UAnw+GjlsrqF4XbuiOGPxFrfz76J3y1VGKzVYjt8RPAQ+GCTO33npVYDeyS2e2OPA3d1QekmG8D+'
    b'xtK9h6Ur7KT1yaqm5np2LJA6ElfGay3NTTXmbFM8V7QVLkK+jB1v2RFa0PkLqYzalmuwVEbHULjCpbIWWigdQcaw8tsrtP4+'
    b'wJwK33oZY6l2srJSLUeUuhVjII9MDUyLAzHHyOD0GMQ6Uki3ou0HImUi0fFn6sJSl/9THxBbAnW45RPBV7WqD3AbQq6Ji1hc'
    b'hucFtJFCsX4AHZkRrBRPh+sR7jRau8SBPCEKfm4Fx2qQbxhPMFtDXG3AP98XHfFTQlwRqrU+1B8IPIoHD9j8aXzADDzA6gXU'
    b'arONoB19r/Q5QAVPm5IOqvrmGKQP/DjYhx8M+pZ3qCFubT+ilkn5jg7CUZmBx4xkxuAeOWUMeVKmR8X+7+AxG74P+6REVhA2'
    b'fJRYXGXOiuhpawN23iUdDFbQwWAFHQxWUO8MdSmdkLi8Hjfty3+oIwKKVWxz3cEneAKu27jjW62+3gU8a40odLsnncp339AS'
    b'/SGWALBUQFSgeE7ZhsWpsU5wGaCG8NisqI5k4OYg1bvqVKpxTd4EUGVSpdFaKb7Q26hn6j5/Op4hZr962sQeTK2SYz8qdO/v'
    b'Fzy4GFl63hSiWqy9DwHgM0wWL+iUUJ5stEo4liQaJxTjQn7XOEbfnKPq5MmmGskI6f2a3K1vyV8C7uE2oFrl4L+wDaIByqU2'
    b'smPUzQAh7BxaRDoqPJcuYNxzWtBsMAxwv9AX/8LdVcFeUzqFvSaDp7qWz4l8oHMllIn71DK2aqqmEj2m7flW0AxLS7HJKRj6'
    b'Aan+NlrJ57IGNanyUCnxn5QrMCcCnSYD5rpCdb3UMx/k7alIbliptuclPLiVIQIbEKpn3y80V217DGKe348FjyaiNDhkRMJv'
    b'tCE0Rq/5sMwvlSEKmUIX2jRvTwkySY8sOtMW9IrRjDhFO6la/01C3Wt4YMFJVL9LT9IgURxYBMzeYC1CHSzk+7AjoXdWizuV'
    b'QBMnQ23LZeOGYvkho9W2Y3F01M1TW1B2a+WCmfRSVWQ7JuUe0YEj0IxeQZNj2UpXoa1Z7Ael7u+LAmpooziblFAsbn+tqAMQ'
    b'7qxVvoF6YckGDnCXCqtSThHMSLBBdHbKXEhKqSSwSaoZVpP8La7gvWI75s49Bo7V0+TeQDoNcsmCLTSSVYLIZROp2wQCyzCv'
    b'0njaSdGgPI2BUopam2oKOZjCAoCDLydNIVnEcOA/ncgBOEqOryeVfES/m8ik9iaDzFFuJDiePmAJyMkW5IwF/IpmTrjAomlL'
    b'VjyllRcZc/YtHOUpw3MKktEv+ABsHdBgE26l3BLdrZR/NwrjobdUYcynQ0U203MDPQu17pTptFL/EXOz4S3KtlzyOtEopLJh'
    b'ZCXrLdpPDVkAW2cwNU0kJBe8sQVbLuLg4Vi0q64RjCBECYsL+0iXqZtGZwcO4blglLXcvAb39OG5Y920IUFQ6LazeFgDKsgD'
    b'Rmrz7ET7G7236mAxt3f1sLUBm8Xpvci+NlXXUNtBHeKLpkJUIcYBwvDrejecRiWrNahH0BNq1DSFqlOglWhKvXV361WnutmP'
    b'o9ZwHFe1RpR2ihb2abktQv46A/YwinWPZlIMhC6Wdia1LjZgabVBFEuHcl4hPWKlkRRXGLj2Xz0oqyYaqBSy0rB2TqM6vWIW'
    b'4ywdQTsQm/keDFeG4J4DshmD6l/RAaj1s1CMFCmOE/abOa/DWNIReJZ/FlXebA6v5byBvqLxtZFeh4XXhte1FmPEbwZLV4YI'
    b'61+C93fxaCjmx2PMWLXsno+5OwEGh3XrHqQn5OeEqmt5D4NPoZzHrkiQngxFqUDcR9khKpGIhxdRmmp/rSSBmdo6gXI5Tryu'
    b'I+H5eMETAvl3l9FAmjjbF4aW/GNB0xZyQL2zOtCmV27EXQS9RvIilO8x4iIu7VdTewZCW524Nhmcq4XdeEAzpGfwTqn+uu9o'
    b'4D76ju7+fv3rHlMbv0u+7pUHNU0JQK1tVGO3i9G9M7jk66Bd7kuWrozEwNRgLpQp3ZR8o6Uro1xQ9nvEsb2jj+7OC4QLicFc'
    b'qqkOhEDf03LScamzf44x9QAekuRLk67cLYZn4kEeuhBfCvJ5rFbSoJcBBJSjX/VlZPlXwUL6/iAnfSB1huXcgtXjugHp6uJV'
    b'a+nYeTrSUsgv7qkz9aQLjePtoPxYuZx0mVVInSE5cXLBECxtbgs7in1sM88nuYi/uHfT/HvpcDY6S4ptJbAvAirfFdZRrgc9'
    b'Cf2lvUWpwT4rt0W6EipGculQhNo4sB9N8d0Cjpx4mwqNl6PQgNZNKPZG4FDG56SnMITOqvMEBA+Ov8pJAamzn1BQSe+dwDhh'
    b'/S5iDaLKidWdEySVQAmNhexN2IX5L9sJz/KvjFIZtkSd6wk58X4T6ERtgJqGi4m5QiS1NyXryx73Tautx3Hnq9e99v6hI3ix'
    b'aQC5s/KU+4emIt/mfNkttCPAvJ3pu1HYD0mkEtuhAow6ZcY59TjRX+CoMikPn1mLcj0YGa13Xn0M5UtHUWH96SgqLPRtY6D9'
    b'stxG5S4a/VbtCVcOd7Yoj7cEAhPIlnA91ePZHsDZwO+5t5m6H/Bux31B3i2OzsOnJcA5GkWab8nK6OirMjSNWj/BFqmqXcEz'
    b'UfVBQbhK/6raXqOpe8DELB1MbKYeEDJ58AvUm3hWoaXDSvrbNUA7dBGU1HHuGA+6xt4Edua976rnqMalMmdDoJ47G/BolhvI'
    b'32aXjjWH25o8HWL4CLI1hcSL0Jv2A/fmin71A1zrUFyPQp+FY47b92JPsvJv5BwNxnzcDfmgfQ+GnFHa4K5scOv9vpuAaCye'
    b'CUJ0IZ9qL9N6kVSpdCVMWI/EtTganw/gCNH3mJyK1vi12fzhZxqbxa2tL+uCjNQLns0kxlFa576O6LUo+Z+jmWmVsMW4VvWu'
    b'3Hjd/6FyZwRTdY2BT7Vq2R5DTwVDM+h1xlnQku0jj6aPUR4Hf6x2oMMYdOaFONsKNTYf+vk57DirlDcbb/aEWoeKj0rPxepc'
    b'KUudjdGym+Zdt17vh6qQc5um0YRCm851C1gGlsNSbmOUXLz7hk/0wvZyOd9IJ6qdS8WDnuxN6ixx914tzSd1cPAZxY9w8l+C'
    b'J22PNymfv67tjb//L1Tb1/PNJtq56ho4IS1aNGJLC6Xt8Ca0/CW/0ojb4Wf12X1/C1G98yM8T5Y23iv8dTLGcbt2ZVqY+1Pc'
    b'w6V6bDWsUfngfyF0EBoM5OhUAEi8QA1ZO5HCqO4/+vhDZN4/P9R8Fi5qW5DufL17C5JuSLU+jfYgqeRVjybzf1Ej4z6waEjh'
    b'157Trl/hljvmXx3C+8nbX0P3xa4ozWqZb+Z2RTqgulW/RVdKNKk6rlH5Q3OfTUPGH1hf4Vi+IMs1a+XK5dcMfWjF6owsV8bC'
    b'ayMEQ+0rxeyM74VOXvDk0pwFWT8Ql+I4MlZkL3EtWb3E9fSPhz60wrFiyapVGVn2lQv7pnTXXXMWrnxSXJ6xwpU9J3vlsiey'
    b'MhYszci6Mxvw4ecZSPyuJ5as+MH1JT8U/8mVK1xZK5dl3+VavurH1qf81/ha4nr1EaLX6/tyaeXKbBe9TctYtnIBHn2om7Qk'
    b'yLBJTz4pZi14ErgzXczKyiDEFHHZMjU0ccHyBU8RO1IyFixzLSZ6WZC8Rg9qh+olOWvJD5bBsXKZI8MlrpoG2cjI6gtDn+9q'
    b'vNSsjOwMV3YQNn1B1lMZrtQlTy7VInfDoKbEZRmEOHvZkhUZ4qoYfI+z3H3PuHvvu3+8dcETTy7MWISwsU+K2a6Vyy1j4R17'
    b'COyzw/oIGj0eogc8H0rDMQ6EpT1Ej4fo8ZDuoXT4TO8rSUsWLlyWMTYNBHXJkwuWTV65ZmwPy38C3qTlK8UVrh/F66k43U/A'
    b'u4rgNfB6qv/H6QHe1bm7Nl63EP00vD5Er4HXI4o/Tk/F++/l7RHoH6eHeN8r8DXweprFT8H7Cfnrblz/JX+E99/p9TTR/yJ/'
    b'iPeT5C/Y0HU/Ba8vxWvgXVNd/ADe9+oD2n/C4gUrnsqYvmDF0rQFy8SMH1/fB4pTTFyyYNnKp8aOHj22T+zJosu1coVKc1pG'
    b'9qoFOStUgt+Lo4UGI0x6Ysky6DRSF6zIWKbladrKZRlToPy9v7tzB9+TV2Zn99JiPX9TVkJpxzqeIGUQp9MNQj/jarjl+/Ck'
    b'ZU/jsiKd6ftwy7XgCQuWx/0A/Jr4U0CVYISB14BbrgF3IIOuAbevXJ2x8Brwx3LUPnPAVfDp91qvCX8kzXJNeOozpK2/z4cl'
    b'WRnXyj/Cr5X/hGUZC66V/yRNeVxNHxsjFuzqekleufCafAb4Nfk8PTs5Y0HW9+mQsFyD/qMrUXS/D5+0xEE6c+D34PYl2dnX'
    b'kqushRlZcQT7PtzyA/C7vwefvvza9KcvRw5dC/7IkmXXhCevXJFxLbhaA0G47sfaT6/yX+uvbztZ8tRi1w/TvRofS7lkWUZ3'
    b'+f8bvmqk/HT6D4uqHWj6ifhBo+Cn0p+eqsIifiL+oxlrXP+X/GC7+r/gY3vL+j/UlypNP50+tLc++uK/83M5aKqfzs9JTyy7'
    b'+/+SH8C/56fgX8N+7mNPGjU8gAdFmP7Ce+BBUe1T39128/fxg6JHf/174H3szl740zOWZaxameXqw1+Ao8ioyqkvXE1XE5CB'
    b'PfCgCFxdrmBVX52fYJXSX7/e+VSr7up8at303VfnR4Pf81/l48f8r6UZTz+xErzP/6/+W3f8H3MefyT+opWrKekfC0fS/J7U'
    b'qdOkNjzg3BCbNkPJopsxjB48k9wQKy+MNcyamS54Tuu+u8w34jFSrZPhVWozQoSoWHhMiTWkKe2TaHFn60h7xHd5uSYcTcoa'
    b'DS+40Dln+HxuM925Tl3rkhrYtwZniaqSKMaC1qdO3PedlGt6XsgfUhQItHawQGD+DGVOCgbTKpFq26u6Be7cV4NDYK1rb/7N'
    b'ty/NV57YFgjIhW+ddemtdoP4EHfq+LTdnnrXg3xLjDoOc69cWFfwfKg1QryDT8NDpWLlQsPD+Qi4cZF0EOfjlopmIjorr+I6'
    b'HR3GN27By60zvrn9O3k1fLsT8V8bq8k+hGM1efDUJjrnVrFjSsg2HGfEuyLytwNBq4QH+LuicV1SIw1WK7/bhiMhTYxCABrm'
    b'17+hw5kuIT8DR/5SVuEKjln4qg+kxjBbAwY9TIOCjYtqbJ/jyGZY6k1bcIIU1+0dwRVY9gZhX36/eDyIVrZ9nspttYE693id'
    b'y6SOW57DccsjuHZJPGoJWDpYbp10JcwVA1Et9TTgidHxrFyp08DK8f6ko8e+9vW3eN/FQ4FY1wjHWtfDB/vTJEWYK1ROvWca'
    b'tzfheavORmXsVmBFbguuOJ2Jg5gKdx6xOtvEG/AAL8wPZK8nS6HhuXWQk37alGqC2SrhfQjiSkvH0uph0qFQvBdB53JQap4O'
    b'SCztwV6JVRR3JzZLvAsTyz1izW0TR0Bit3wvsYHyozcSC5ABmGyo+DtfGLINT63rM8L4l4M4SLb7oDpINgYw8Ng5vGuE1XUf'
    b'dCl4KnDIdmqAfZDXdeV5XGL7D6xHOkMVz7Z5lWq1LoxGIqGMxciy52J16oypbDsujFzfvJ0WjqIisRyGWqQJ0EZtOJrZj1y8'
    b'yHNrJ+HdPlF43UGl1BWe86lsa7LUx3sOuwY8VDR94IlC85t6n5+Klo9notqOSN6Q6sSwphg5NZSdg7czKfD4BFhRx5xtsrNp'
    b'8XU44IpDnVddFITnnx5AYd57AIXZyJx+vpnujdqN50LLZds26HQzgAiOhrd5vOKtsqSeJo0XLT0oF+/aQKdIH4cvywyatB0t'
    b'Fx/oAQ6Tcv0hcnE2QHL6ycVT4XdxqC5EneLtUwe/xHzUjDkQXJJolC4tZPY2wXbO+iYeAAlZcy1klbI7dfQTIXhWtJ/b2/Jx'
    b'BvcwK0IEX5Kwv7y81QyIeZV1q58IaRdGl/c6Qa82VT3baOb6fPV0cENsJu6hOQakQAGliP0YJdT3EoFqVuuQDqqHUR5z3X7H'
    b'Orwi4wB2zg+EwlvOI0u96QcMNCl88RxdGDKq3HcfP4hnF96xjk4uLPcEXLc88CpiR5cupJoe1Sn8vfzAawC6eE42jfeFqVeN'
    b'nFKXirf6rgQCWvIOXopreiynrZezJ7DOUY1HFX3j0lpT6Ea8TcoaKf6CZxktXutvDOLNfJLRuo6OE6IFj76B2WFyut73Hqvx'
    b'lVu8fU5xZscdyq79WPMH92PNx0gHU2g104OgdYR82sH2Pt84RVtVhkRLn0TKIyU/iHBZBM1EtGGnaK0I3nSVVHHw61B1Ut96'
    b'Qkg68S4VytkIrGmXvHEo1TTXX+g8g3MvuNwWIJ8D5GI7O4FYcjIAWritISx4xZXzC5DfaXiyVgPfiFnM1IfRUaR1Vy/C5FvG'
    b'qNr+Yaw0TusqoM2YWM2oj/MCNZCV1XGjKphE6y3K4mi2wVP/nCCvw0wfUELVOpzByrEelPorV63eVfL3oYQW7cMtWcct9bRC'
    b'bRiuJK9VHthCd/tAUz+jU+dR/gm/vVQH3uAGHenmQ3Qjlle4q+6ZfqrGkEp303ouEc/eNMpOPMY6rzLurvcR372XzrHEoxzT'
    b'+8Hv+3SaPB7GiUfdu0spNB1vxYi0t2GYgpG8BI7EVXaZ+szQafLm3bQgDPh4DPqfGnfwChff3fK7VRsoSC7crcZyNsrFcdHF'
    b'eOEFHoPbiKd4q1kUPDN7TRVKB+nEaiH/heDmSJosjOZF01XGu1g1X7dQnS28HRfBB5eCUDSQEU1sQCbUG82gli2BZEu9tYy2'
    b'U57j+WVIClcgza7VlrTjuqSFJKi02IWdE7Z1r7y4r7Ynd9oiNEs9NqGpysubaZYis9f5zPP4Y0bI5Qw+wyTsq+io/lacgjPP'
    b'yfxZs/WSy+AJiBOXeocxThf2OP36SmFfNZPo4h6/8uALOO3qr5GmwDeuFSo/a15SjyKkr2DFKUN7rfekFZuJzFalivRBXPGk'
    b'Y/bdzLZXyH8RZXGymR3Da6ZKcWGc2iMGVyyX5lUiPvbbUm4pdKA828wTorh996hanHe83uN9bkhYH/mFTkXt5WqhJyxvNob7'
    b'rQnRQj7d8pMQze3Q63s6eCgu9wrDHVQJRpzYr4LuERl9HrcSJRhw/6LX9ZTFC5WmVRNNC1F/lSJ1PpHzoJRbG776BgAdUDsv'
    b'rWIvnqNGDc28TQtgtkPxauSL57jtkHa1HfRp3O6FLmqvejAcnt8pPWvSuZ7DZf37eCBARgApz1B5xoNQVdHTuLNKth1RDnLk'
    b'Phg/ddDhz/P1sxy2eC31lg7QVN3ZFbaW53WhpcDENigY5jv/qOs6OfUr7FIHSt6hWoZR/CAvj5jUvCRLlcNA0gRPUe913uWt'
    b'N0qf9dcq0xRehJezCPl0TOHDMbgz02XmTuOo96WuW4WCzdS5q4tD6danY195zruyl14CC2cCCpDeZbp4fPhx9Y4X36zg5Koz'
    b'uCZcm1SdCh0cTquCYX1Ap4vR4Q0fTj+eq8tOKVcqgxOrsRePky0wnMhJuUadYLMZWUKML2KSdAlKkvW9y+n2/QOX6nn/oe1p'
    b'MeJipFpavazHHZwHNnVvocMVJMqZKkirLFpdtEHLjWkBSPfGkEBHwvMxrkGWw9rR5rjP42MiNwjJZW4K7ibbWoWXfu6+l2bo'
    b'aR0kLkRQE0nUsPL34H2CdLifnDpSC7x9E21UpNv4Aq4JYLHeo17Ah6fkcps/TD3AMHI9HVtYxzVUr8ss7NtKqLh2ZdpGXMRv'
    b'a0vFe0DXpc9SORK8vrGa1eH0ci/N1HNjY4y7a6E4AL5p/Y/tjOXNCGiffG5bx/nqxJs+qYDHp7sXMdspYZ/tVMd5YV8VtoLE'
    b'm5oWcYQ1yAkGkHu8uzEPtQA7Vzj9pjNqx9Z3MTAoLCV6N9bOrbvpJFBaSO6JxmXbcgw8lXpON7leUdc80Nqh0ia1aj7FqvEg'
    b'Vt4acxeC1tNp0skGLiMFuWQvLlU/JQOnX0YKVgljqjcuqBHplNmK1Ub1i5u133QzmBiVroFFppDCDWGxAIr3/dtyWN5K70Uv'
    b'WaZDnRYOQ4ELF/2WgLwvHlfpUCJSRUjRhgktkJC31SDsO1xkGlT4cH8NlCx9JhDAKAUWiR/xbZjPMOw8ORU3VfZgCt3HbfaV'
    b'42/+hpzq/BstOZ3CnLtAqcol4yBpJWWDyiC6dcC2F/KaaiwKsQR+lX8/Zs1lTJYCZvGc76miyXrL6fEgTwRNkC4jdGr51wZ5'
    b'coj+MrPtlm1lhXMGlk1itp3SJasrjDt3+95y55bpc+7gtt3u8XPFEVLuLtzunruLizvbX5Mng/7ci2Fr9DGufvLkUPrcK1VN'
    b'6XVDZ5qDTzYoI7EElfepy0REuRgriF3CZQbjNmgHfz7OOtWFQI1MWyNol3ITdWxHM/BvzRjL4TLyzS71gyYsJgrJYgpaUlBf'
    b'54Vi78VGliiHQ7UMEs9Am/Pn1TxswpNsjXRWcKHN2P5aj4Hz6l/RwPnfv6KBg2vSwR1oU+83mMvosHZcC/ksrW2WypppZVi8'
    b'weWDHH1NS5pJutZylCsMBX/TW7R1PFZHYVS/eKnTCKLhlTchp4u2hmJGvGdBIuqLoiILpycZNZj0uaACwiGKVfznopqkZ+bG'
    b'lK5F53xm9y4K5fwurPtvd6EOq2R17H12UtFx4qOZnVQ3VOMFGW1FgcBsi3dez8J+ZQ9GrSzfpbo8tLlA3lM3Dq1sbcH1+QRl'
    b'bxGtC7f7rZeh/5Cqo5Tj76p7BE8HNUfwnpUnid5viB5eDyHzdXQfJd3WST48uoJFBaWqAR23g1Q87gaUi1+SaTWrukmwudB2'
    b'HhcQj4e0catfMw0eKDmQMC/Ak74X1YDuiau2faqbJXWG5wySU+HjEx0Y1HiHEbQ78YxvgLqKac/yF4gyLnwtXot6+HOoxBns'
    b'nEpzxLu9zdulhyNQAP70BgrAzjfo0AGLlzoHs7xBSAXeFEXpwdvsD313S7zvvJzbYqnXd3K7scgUWWgyT84/LJ7HKxdZo/IM'
    b'o66pt5M5CQlXPvYGcige3Fxgi8yH4PLhONqQQmMgSbR8+oi8538w53SHr7AvPhwXgyfrwYCy2mrFfvKeTVSuBvThi+8G41WJ'
    b'YMgtdk7e04B7BiuQg4nvBAIzwayx2inOoV5xRmCyxwuDca50xzFDnKU1Jm4/Msu9Vv8Qbujbc7ZXxHEY8WUtohrlzNt4JoAU'
    b'v4Ms2pZ5lg45KQ4q6SS2rbNykqXQ1lhtO62zXhQHVNsaZxfaTlbbTs3VKDpbkhQ70tPJ9kaV4AYgOBPHavbX2BodrgUuSwdu'
    b'HE3OWlljaxZXgcQkxUFs2dlIjtMR6+4q1d9LInsRbL46pWs99qQNuPx9xNu4wy1UPIzlyj1i6dB3gjkmS1ihYF3K0ig8lBu0'
    b'U+4R6FNvHj+OSjthvTpAdUjYaqtdejyiKDHu14U2aPal+gpM4LdqAqwGertaqBi6vbT0EO5Wd93qzq3VuYZBSGWwd264unfG'
    b'awMaAuJedk5Z9x2acng2dY0sTdpCK9galGXfaSWoUhYc0nZ0NkAa0w/hrlfciUKpDBL2OSvVkJwC2p83QPBYelk7yns7Qe7K'
    b'T+xU3WkLDe9gi5fz6u4mk1bK9etck6VAmOsBvB40Pc5z2nW7xbvUfzMeKnJYf7woahCIvfFXUQPE8Em+c+6JA3Su60Ax9RNP'
    b'Q4RkHj8eyPlKpdpAt3I6Sn3TNEy6es7OHhurLTNKLjEDi5U78gKBx3EzNW7dTrC4Jli8B3CdiVyy+R7I1qftb9SEYgDaQPb2'
    b'TL3S5VbXcA6mS7j1ORE1CQYakkgw9r33GW89VI79GVvbZ/CcSm6gEu/WVqhtk+juAxtu1Y7U7tiAjuuxkdYUk8uUqm/HOos0'
    b'xPZZ+OZQ1hHBzfAE9zpOOhCD+5hctwtjpc0g+Vk381ljPKfF64V9W14jP3c0+Klz2LhQ8k7Lp4WZ/z5Pr9LtPlXFATbzUHkH'
    b'XZNxA14+ac4bE4rb3X0y7vALa/7bn/GElS9BeRgo9cF/RuWBiE683nO5er0nF6PQZ0Xfd7kfveALYCXUIVIbIgHIHI+Y6cMQ'
    b'/QI4zWfUCxnSKxE6Bj5Oqh/p8RMB8uIGOuC5Uih4VfXYX91FgGahYIsKiIH/VWbtQtH/QLyvEADQmefUj/T5ce3CvGZh7Ku7'
    b'8V7ReRC85xUicmE11MdMvEN05mLI9syUWMD+p3qjZXrKOLx4VBjL92KskRCruJRiHV39C8D9Av4/ALQv1Hs2I38J//MMvlD0'
    b'qrVb0LWhHaXxf1DyPv8fTfKipIO71HV9LXix0Tq6xXVdcAiAC+9+hcv1wt7dq41cHKIxLNy0J+SjDrZeYvZmgW5yl8FmL35F'
    b'j61UeolO++6+z1HCwSxa9Ylnd7cov7qCmz8O493aEPuu8mdCXIvwDsBDuCO+bJd6Td68WhRGrDxJHegAyaRLK9SPAm/3Raej'
    b'P0b4IYLjqN48HBEpwAtRXf2FGQWp6oie5P3eNkKl+jWUnobXUHrGMHstL6KdLdDlHBbyL+BZFAfpUgt2XMj/Ej5pdAmH1tet'
    b'UscuhvNc46iavAAO4K2OwqssjwTqLIfZRozW+mYIsqWBl6whthS4+rKlYG03W0g3j/iO+ka8YxBHgZRnBHEg34g5ytS1LqAO'
    b'EPsFFOdavhE35EDr6fxQlWlbA16g0gZshSzd8c5idWyoMi9wvcqv5XgjCCHhFYcziZf4ZpbwPsScIcJIvMr1BHCvuFa9+PX9'
    b'AyMjcAugMPIojh1oF8m+L4ys0+5w5HXq4CkI+7xXj6iRmnnJKjXpI9rdK7xBvYX2fVAPSdxm7HV5bDOAX9VGYM+rDWTeq40q'
    b'oSphJAbjv9Sk3isrNe/opvxii3rf7atV9DvvjDD2ixxszK8qqlxgm6gihjWoPMQG3KaKgr027JULHyMpvEay+IJK/kW/+lvc'
    b'qSbT1H3FJGqRZhpnBHJNKrlpYfMvfowd1sbN6pDWdXTdjdleOy0s7o0QVGkmVGkFy3Aotib7V8L+E+X/xit9i7US16Au7ObF'
    b'KU31lGu3bGKz/wBl2WcMVhLPhdRwD1VezaqGjzHN26jWIjHhmfxbQJmGDNfRTS+YPN4/Y4NIG9QsRiMzDRCKd/xKxt9qI92B'
    b'E5qU6Ul8e19QqOz8I+qLN+HpuKNIh2412NPSpVtWDxfM64ahTLrH4HNLHIYlmJkjSogMbT2q6sMtxpBgozQfjKH3mQUm9VZR'
    b'cBeporvg/c1Yejf/bqT6+5do9fdPBhXnnHoXz0wQ2nTE32ImePo5pBOl4gADI5/Fwc5sY88doeYtY9RQum3YpNYo3TZcFKcG'
    b'fIgXDpvVG3Pw5mLzX4apAah5HkFN/J56G3RkKF2q1+MpsFdQeWx/RVUeNkXmvynGI67au3dRn8adMV7pXS8pU7siF7uLySDD'
    b'CxpalBXPqbcgnFd27KE3vEK+eBOg8P0YRbEHESwdiggoGiVxEBLdqdMFaWkx/4DJ39BNNGGPegARJLsTQrTI3an7c4OIN/ZK'
    b'fU9P6h8HEeQ9R9G6bX8Tc0DJovX7zhbt5gSI9S6m/IfcoNncuUU1mxXlZE+cdIz0Za9IH2GkzNwek1lR/tKDPs8VVpS8whda'
    b'lGyU9wwH1HwvEhhY3EPgEyQwSiMADiyReOpNuvZe3o53PigfwleN7V+4Rhi0uizNKyb3yhUXp5uJO0ol9UyL4Dgl8188h4vV'
    b'tfmIc5BMofO87PyXUrFWvbwyqQu3+pR4LfVgU09Yj4wSh5ILLl0ZKJos9cGK2YaeFLLP2aQc+AfaqU1yEngqzTh7iN2plNsI'
    b'nSheFKtFEfvxxPX9fAOhcq8MFGy2ZvhcycUjeL3qEnlPcjH1OI9LuXXgwN1TjHOhrAIc4ofFR/j+WJplEvZ7IT1gxKMGBo7A'
    b'oyZua7LaGlyxwn69tU4c7H76IVx872wq/9wou4YO9UUGbE155bju29egEkXmYnfUqHz8bO+qEaAM6VJu8yAwCcXm1g+uuskU'
    b'TEvpABWDVXKnkefhBihXRPWkFXo2PW+lz1g9yYhvEXT7Yum3dDWeOvWNjniuX5vUQhtl3Muoc379Mo3cRJNFTnfZO/HCECY2'
    b'8/txA1r/Azje5rPwvETspj7ABocjWnKaX+q6TqCqwb2Ot4Bk/ANRDhjUgVQ8jkqbFejnRxcoUrTj1sa8FDyFik2JwPNvzOWf'
    b'ReF+xtSI7q2N0yO0rY2Ww+F1jFOqJ1n/vhT9OKj1zWDa6mipB0vAf/EiO8GqkYltypCD6qbGh7DjP7QHE3yULsssxnTUjVdB'
    b'SqsO4sBqIwlTfnTPbriuv3frl5BuqPzcZeXIXpwHCckZpe2Pszfj+EwTz5eQe/EX9x5Qd8c1sa0E8RloFxRkaz9u07UF98ON'
    b'D1dtPbRpU3y/QIfzZhXasx9uInxluxhBfcukb4zSlf45C3fqxfkqpmyoniAR0mPC/vwEurS7OFab34qUUojH6llAxEh4G8qL'
    b'C8PVTsiv/PFvmCMcAVYWHwDBIw+o9cErwQ1tQSZl7btqf9q3v8NRipAX1dn6GIZ3vrZBW8DdKo3ydh8299wWpX2NOgI4Pkwd'
    b'Tm60nMZRZ9Ir7v2badaLrNxtFdLlwcI2b36Fa4SnQ7wdt793kGOn4P4a3PkODfy1NbghKsF3BGcr/khrMtrUcbn41epInZBf'
    b'pFYW+KO2v+KEYEMoqxCny/Ev4rbIKQduw3p+tTx7co0UM1Q1v0ERdGWivpbyTaAeRlr34MiZK4bRr8VbLeGQmK4mX3rEpM40'
    b'ge85SPw3J3xabiLeYd2Dw7iuEYx+5T04sHoANzSwi+1/wY20GxCXY5rym1PwNIorEdr2RVtj31tuhHXVBMY5xkLtshth3ZsA'
    b'O5mL5psb/aNF3HYeTZKmk7Ym9xETlnMRNOGTy/2f5jadXH6e5zaJg5dKmKqJXdI3Fm7od6OJqEJDkgtx3AdF8t427biMNNzH'
    b'd05242BEKp0Z+C/V3X0Adyh1Kb8DBvssmIx4h8yxjFJXhisMOCfepl0V9HBnIECaRvvGO3bVIxZp03DXU+JgLSQTaB+gczga'
    b'pK5FosFy2PeB1LVQjJZym3SucPceZLjel+0mNup9mfLkHVKnPmc6hOtv8Fo6JG9/qWux6wZWDW++wRRtcE3+HqwgVFdJ1uqs'
    b's76BAPlqgArxtam01crU+66vllTag+TJMtIeyqSXTN15f+5iIND6IFZTV45QQLcjUtJhHvXEjHX79QTSyVmFosD2FGJUNTXf'
    b'9dSzuELlyYxJagBJDvUiUleIKwLawJ7NAPENZxXlitHSUX7WIIF/h8ERQvJxVoyhUqdhdQjuxtvaky+TDziH5Wndjhqwa7ng'
    b'2aEKC1STvBnPfMRe5FlR3Vkm7VLrXN58QAtZIKK6Q7COSfOvQ6WXrs1jpGJfs+c1k3pG4Hkl/w2t8eKxM4cJpgqw8slfaDsZ'
    b'4LyLWtKvnNkdBKSAJUA9QGsHuFny5C3I2wSpawXunsZSvTOAVHb8xQ61lL6hnAr4TlgPvFeBf3EWKmIfkdoqNV92RUi1UOzf'
    b'uG723Sl1LXNdrxLlFKW82QAdbnidVBuSqkWvBZlsJTOgK0vInwd0fGvldXh3HO67/k1e1yZcFGdk1FB8WXldOJMuDufF+C3s'
    b'K/uSNICcpbdexArVF0oI8T2U14VqW4zUWph7b+htJFZxeXinOygRLcBSLyfqf83MLNXAUo2+SHldJqUdgvL2Sk8x07GYH0N3'
    b'PQ3PJnEqShpkPL317W/Rtgxq3quv61WytmEPvnYbzVLNCcVZqtewy9/2CjyV/yyHXtCDr+Isy2kHl/FV9uxEjE/5NsQs/9qQ'
    b'oPx7Od5gSnAhMv8BNBpmSpfvET+UPfi+OA6PBiipm0S3rGMseeNm+FBMeTgS2Of6SLLZWTmNgB3FZWmVn26lISPsHTrl4n54'
    b'FeMnWapcvUbzYuODfSAKcPENiPB2lnZaiysXVOB6VS2qw1zLel8A9nj3BWC5xuAFYMK+OvE+7Q4w8+TgHWBx8KbMcat3gLlM'
    b'eNVXYQmOpvgihH30Uqhd/qUUlmC+lCVfaMrLF2LpcxKvosdy1fTfSktQaDMsdPV1SpQP6zX0uSdw0ETN8vpYcMGypgJg00jV'
    b'CQRVD/l+kFXKeEl0E7SXqT51NLCRnexeKNcCQfJ2HKWEKIkvN8mb6u6BHjsqTfnt871YrmYnCxfk1awpVg8pll+uwuo9mdeW'
    b'iuJAlaxW4HasQOXeZcFDANCEK8Ar4uV3YqZm4mQwSQAPfQD1pdhPetYQ5hvwAF30boSP51ZH+9Khp5sO7zrxUbzcV0yRnjXp'
    b'xclpynY33r+3C+KnBk6ALOGbI3hdsucQ0t3mxYwFb3CXMWNpnJIM1KUnSZVmPD8hr+/AEc7BFW1BMdoBT+U7bDuimXbjhvlb'
    b'b9KngeM/lNVIVSZPPd3erR0Mpx2FFryPPZlIOJHEwUz1PnbZ3eyLwb3t59Ur3P/4rWoYKyMQt2bUFnX+La8NeYf3P2b24aMs'
    b'E5srlS8ziZ9iRF5lzOgR+l5XS6rnjCkfvIBpn3whuKf4gnJ/Jl4V6LqbVabJ7lVnRui5/QJkpFGxZWqnCl1PW4lvvqJ1p2TK'
    b'f56JJcNBxp4r/1YS7Vx8tuEqWyY3k+Vm5vYoPNN7WwN+ArdkvKudeZpw7rp0CVYGhajgmjBdjVevLdTwtABIHCxdChUH8nmG'
    b'O27Ou4xXFq++nVNsZo9iQJ7SYTYjJ3QmmrQyB/BEz+eWaCaF6YB2ttUvIe81nguACiKtK78cYqkvRW1f4+kE2L309i1OhdMb'
    b'WlgP0JsB3vA4Kesp1918EJ/4gpyiZ4eIkBToL95Qky/jezyKckpoTT4RmYT7Xlmt7315GwbKhnVsm0LFb8NP2wXmQedB9iAw'
    b'M3QxFjs18FYTMYT4R4XquW3VrG7ijtiMgmHejBZvXltsP2R3wyW6HDRjMcTa7fevCAXB8jQBcDHODKTK6zfjiuDA8ZQH0IUQ'
    b'1t+OQ6OelssaAvM0XsLp9fWpKhovxgWfrFpePxanhLbVYWigGNeCBs+1kjdN2Ir3cmLQ1NRAPauQkwIa9cM4eFyvRhPyy9Ql'
    b'3rI03xCCazUJzGrSPQE8tslS75ske9qgP8TN9syDgTMCxamBFaGcPiy4uPQEpxwu4obURdZ8J6KLYZOt57O+Jl1lPS+CRnBN'
    b'X8TVsEXwcePkRVLnHDE+r4tOLRi/SDpEYTrxTpU1mbrFeBZgZkimLrM/bqSMhyKuPb8iFP3vcHiPaV8R6ouCRs6Jv3mV8UYo'
    b'gGjsffG51gBWbMTwtRtpEhm1qJnnQhtr4ziZgZMuHyzSJl1iMrVJFzzBzusaHJx3Cc97PgYq+8HVN6AUS6l/vVvPcnE5vc08'
    b'e54q0upMc4UpmOglGRPtT4maID11O///6+zaw5uqsv1pSdsUqidVZqiI2CvOnToqbQUdKpQ+0xZtbGgbyqtAmp7SSJr0S3Ja'
    b'qlTbpqGeHiJ8+ADxcZ0Zvzu+vmtl+BxArrSURxkrU6BKeaiojHNqudd6QSniNXettU/SpBSvM3/k5Jy133uvvfbae6/928oq'
    b'lhQkusNKyhc2GWoTzQ2UAt9G5lUsXd77JH3Mo0zY2aWg8nPoQ1n9MPZhBWJgtqvMavUB72lc1h2OUpZ6J6RNEO9P7ZTLNFLO'
    b'ZDlGKpmGVmX5aG8kR8uLgJokldwl5wEhRY4hb4VSiZF8lA5NQWgIyHMjQu4ti9IVpUZQOVUkMardnt2ZZ/Zx0t+wyrTKr/7m'
    b'9zOx07O79pZ3g/TrQuidN4/Svz03Sk+seCtI/zhIV5f2/mMDVuWeDViVSeQHa75XmSywujyrVFejoBnGukTD00D1tc7FWZ44'
    b'LOcnpeXF8V6kpr03QjhnKAl9+UY0Mc8vvRNeuruGIqVyRTqZ9tJFcHMtSi8zmUz1RWu64lQofyPuKJxVkfxRAs/C5mg6zzYf'
    b'4qu5JtwbMJKfRPBDU+ZhqeM8TUEVqeUiQx+EHEG2d2I+pE3DzDFtPTryLRJDx0KLKyXNcN5di6ZzMMnmZM2mNcdAQTonZ2oJ'
    b'h+uc3MRuih0YzcUxd5LvgYu+2cDd/bLh3NzrXQkgEVdopDidtFynkeu0klunlU39Q/0RfXL5OVk8I5sG/KB1JUi1pHo+gYpl'
    b'+bDkGWaL9IZeRDnBrUYD7l9GdRS/xYWgYeAdG8DZyiQZm+gmeCqPWbBVlA+ryMBjWqAL3ZhGBRYnLZP+B9cKDh1jxh6h1hkH'
    b'2jGW4+04CuLKk4VZQXRHddQ+yhEAyYDSyOLNUOOVut2zPV8b1cjvkOdLl9Me0rhvkeMZ3vWqYyretRglTfEc0ni6NEOfSI9p'
    b'wi/GLVEeoKQXtwdlRBzJCNqXuai8XqEKCL/ABMRFhqGdHijdbNmIysXdzfsQ6C/thDjDs/Npgvy74VATvrBdtPimraxa6Z/u'
    b'CelY8RqpfHFLly3DalV2SH5/1BFpdAUONKQk+beSvlcakR/XyIu1kjgg18bJBTpJ7EfU+Rdxiac+ThL70j4RVzdnlOaVpIor'
    b'mx/XlBYXl4qL0gz94kJcVDmmZApMp/4GQlCO+dYvY9H6+RYytU3gvSfQJV/rK/A3X4bx7Va+7QBSygeko8bmbq4D2uH44HZM'
    b'sCwh7bhoVVMzQ2olpVmlILFiaa0CEjteqdp1xYbZV4uUtg7Tfhhe55Zpea85Fid17cunxDVdqYTZ7OknXtChKWC25/tJojKY'
    b'Dc5p5X28F40tWYq8FxUOSDVrgQFSPcEFUi2vZOoR3xoXG7xeOJr3/ohQ0Su18nsI/dd8hazf2wa1hBvV+hn8p72EGIF864eI'
    b'lU7vIEsFWd87ajWr4YJHQvikplVbEVT90jd0MMTUj7ty7fp+sqn9tCORLOI/9lyx1F3PTgygbX3Q4hbR/qTLxWRojDtZ4Cg/'
    b'OSewp+k9zT+zr9jfN7gAAdrLdGmnxES1ohOxyNkLCsUpiJn3vbIX+HvwNsz+cb4F188C1dPGqifXZFjCe6dEqdUjGfqUuRam'
    b'efGtX8UEayhSNkD9nowhI2d0OwKvc/OhbQ7Ay3fZjVrQFWJwDoWY0G/DG7AV73kVA4j9Us8dw56RafzGrTFoDAs5+0ZZ+TpO'
    b'TdtiSDGLH3wB707/sTTroQf51tkxanvmRIxmOBkFYb2m+ceSsiyjOAMiYWt2GyoYG6VjUmVaaRP2sMHkGIyuQJ9VKE5n1bdS'
    b'q5grcFLDrKaOvA8VE0u+FhUtyBEfAQ+eK7fxT3d6rhQxWNo4X86PnpEIvu3z6AD33Kom1hcdxrNd0WRAhy7vRIdi+L4azU6A'
    b'leK1Lnzrcyyih7mSKOMbdTBxGGyLZjXlnR6NWSkuKgFuvYF5Y+V70xxojmVIDoVVjQx0zG7eOx8di7RYNMdrULSZGPEp3muY'
    b'MFqFWRNYA8FYzrd2Ip9/X7rAoOe9OyKpmXyL4tKO8t6pCOxXr/X8qBXdcn6ctI//80DXVzrpo9hPQF5/H8l7j0axOwsOw/9r'
    b'P/hRE3kvKiTPI6sCef5j1NV5vu52dv2Dtx3Xc7dVslNxivhXyHZ9gBOVHasCa7GWqCAbTuC9pijC0jp7BYZmBAosxrANWEke'
    b'PNCDuc2PW9NZGdEneVD4wuf1kidFhaSaTajXT9OTBlgayFDVGVB2rYYMTKEVub5I8RGMJ9UPMUkem5rFGzCZd2spXHUEA900'
    b'amSOBtx+yXQWzVXO0AgMs+9eHB9x+bXpLpq3L9aDe/xTbnY6Q17L/j3rGDoh74UZMTe4hWDp/hfXu5aqRtjuFEz7WagPWS6g'
    b'tAkxzUNnqTy56uEY4iv1QA3abzNTCBljcOsxAhNWqAczP3QvXhEzBWsGdzNYnWANDW6mbZgY3vs18sBTaM2+Cw9RDh6M/Pml'
    b'3bk1pLQvsKrDAr+sFhjtS5RDK2lTyfMqNQyWfws0z5tqRf/mMC6690Xw3qoJjLIF/Z+ijQGlH/suMfb3ecVZ+aItyHiPrwgw'
    b'3swJo7KL96KZ06GWJhU0CQYwz+4NtP3V0spQh6WWJ0gjvBTJkmsF7Xvwq0jqQ61SJJNHrbUw5/KVaVGWFkSO9iscDHCAyy5a'
    b'DHIqMihLe5WvywOjzUuRoaPNJrRWeUwLCa5X4/Y20i5QLwgDdwKTomtYnXs6NdhZyiMp4xhXcSQTGq3pWF2N/fKtl7ozvR0v'
    b'8q2pQJibp2O5+K47033Td3nrtO4b5WcVNgISvKri2A5zu91faF3onBjqjGepFeN2XNvr+nqS5+8x4G2i66aqS3nrOiPc8UGP'
    b'vqKJyh0sFh5jaYwMjQXxX5WJ4HypmwPN2D0dvE3eNQWorqhduF8S9Kqce5v5aoogX1N23cR8JYT5eg98eU83/NLj19SHZuIW'
    b'5RU1PDeNb70fu/vuL6bumoqHlXXwevOuaSy6m8Oic0CgQUR2e5fOw+TpQBk8JZ3sUuK7PtdKn3rORhwdbP6cDs2up+M2A57P'
    b'dF1KpOcsGgU3/x3XttxRezvJKfYvsmHgVzxXUBHbJ53q+ioqtgfCgg/6HIrp+iq6+Qs87A5UDVJjB2JHpLx1II7yEgfXY5bE'
    b'AalA53s2u/dtDY6WoMKt1UigwjUOSHMCiyKfp50axPt3aAwsLioEsd07guvlOMyXvOL3D81ngvPRZerKycwA/4u3yy3T2Cxk'
    b'KozZ4i/kIhQXSt3vIJQWuG/wg+9oxYvdUxOyclauhRl0ic9zy5G3NaqCPQ2iX7ES2RB3O6V9o52sh+Ymvu14zomkhkQ9Trrk'
    b'204g/C0biEbw3C3D9DxPTy25/kDvI/ScRpT9JBsuvJF6WLoktc8h5OlCwlUnMUjH06R2Oj3Wvpiey+m5ip509KydYI/bCfa4'
    b'/Ql6ktFYe1MQn526xIU3cMFvgLYpj5hHtylDFsulEbZcnvMYLpejtQdo3svlxRqUgIY9coZU3ildkPQ7lecX4XRzf9QmS2uE'
    b'X9zpFzvLZMMeNJfFK1OUTaRE0KmxD0ne7U89rfx5KVnZyaYdKON2ghylTbz1m5GnS9fXMgOqJ1OeQ9t2fmYP37KWwuKh/mT4'
    b'rcZZyUoNWjDi2xwQtOIO/NLI+j3y1h8i2IiN5sBp2/CL96IRu9RzZwvyqmekkm/7IymuH/JJ+y/wyzerljUtaAnEJ70Pr+3M'
    b'8GfJMfhBuksOg5RXraB0T85+jpbT6wSfvgeqQzJ1YhZehkfjK2iX9So8hDfRHK5DKTqNewX6Hrz6w7AH7d06+PgWDWkhA3Lj'
    b'GaCgVx0G0mFwnfiytB1Nh6TdaGw0dC8OPVC8+NeZzVH875ntUvw2ZqcUv5vZHMVvZvZG8dsTVHoSCVWs30YKP00Nn6iGv131'
    b'd5cajhke6YBXOEZpmY0xtJCNA1UkArhL79/R7YGpiRbbt9sS5B/czwis+eAMdsI6nEbGr6PzBYFpJJn6KVWlbLViQNldRjjX'
    b'UrevuTP+swi8keW0Oyl43HF62p8wWfGXUo/SvBihHe/0YN7ScYUALUx1aJM+ALNH6Rucl48uq/oaz4CYG1H6Tvr9/j9hOaRv'
    b'ysDr6CplcE1s6aOY0Wp4LvQZtJ7zCRBtFnLXAq2cxpoeWUD3VApTbYAFFiJL0EYKn9TDl8qMZXGzYuZRnoZXpaVE3abAkLnM'
    b'skyeA/++DrTPi9Jd/Csn79UREPYZZuUVjyaUK5DL5HkUYIWcyXZBDKh/yAUY+t+a2PTZoLCsxOvP+/RnYTpvlIt+8H/k0ytG'
    b'vFwS3vvSPhXP+YThJaMm0Y5HaP0bnkbJMKJ8UqzO5WctYpnVyoYRWkp/F+ZSS5fRmkB71i0YdDYFzYZna6c7Q16o7YiAWf59'
    b'cvE8+MNe6SnEXjEze84j08Sp8P3AvNRO0lSwB2/r5Gd28fp9IfeDscr/uAGjHWoIVP40iFmOkBs0MI1EBplbjGt7/dLBqOEH'
    b'XuWKy/ziAAlnaPRhZZ2J4PyZwZITpdwhFNaoDqosBOrdUmbcIQ3jURVl7iI05NpEtmL3yRvxGgfQKO+aWxQn/hq4ayNtXF+2'
    b'1GMJ2nawqkbtTte2U/0459nLVNWGO9vmkNLdwwwliSdUE0oUFz0B80hsSDLIRgPV8qNkGrn8L3zSUdVyFX7L92Fgnbrnheeh'
    b'PaqxZ5d6kNozR3U8P2SSN85jmvCDd/Swxc7s0bTQSHOFrBqroriSWbTIYcRlyDk62TiGeN5nOON73vg0bXIlPMWVNB/E49yS'
    b'od+PB1aBx9i5bONC6K/+j2BEb3tBVU1CWAybdOJabNKEtdjxdQjFKibgCnPjZFq0pStD9Dq8dmilkfTibhwFhulSLRkPkp+X'
    b'GifLjcOSmODzpOz5dw73T0yK1BgnmbTqCZ0gAO1L9ZjWm/X/b1oXikLT+rxznLSMv/uJtJR5mNKh7Hq/ehyD8GhfLAri0W6a'
    b'fYQjOFq8AwWY8Bl2p4Zvy38/gwdHlakm9WTcbUQ9ClQl2sSu39mCm7jSQeh3F0vRF2SR6fjPF+POF//MPu9p6RAz9sn0+t3z'
    b'1Alm28t02qjtFYJMGXOqdD07t8K/s+UV9XqPAeXAgTBUW3VfTBa1JUppHdZkBTyVQw9Rqq3nCLZXzqPJPi4rBU76KYbSwIQV'
    b'jdkCN7L818LAjISMBxvjOPHXvo6gXQ+wDFr2dErb0BhGatmKFh4taB4xdGPaVqSJIrRZGjmID8Nr6uGus5GeEaH+F3K57viQ'
    b'p0v7xAuRGF+m53Ki+KVMPjNZTJ6RqeJ9/DstaG0wT1Mf27SbrFXqbvRtC83BpxdeH9K0skOBEQWgp8WFaoFKsUjnX0RSeG5H'
    b'hgJWgPEJ5oAXlLcMhJWD6uTiXoYzPOC5PEEy9LVvLSRYjaYrEfV5uBdtOqu81ouN2ze6Ry3tu3RshklpPe2OTO2M7fM9iXvU'
    b'7GwNGulCB5uMolv5chXdkodHzQcQfbmxP8qYv5OWSAOjG8N/cRP+i5tx5Bxgx+shgz5TT+pp6aDym168WQcPDEFv34IG8RIq'
    b'UPGmPQxh5+TxszP0vW4nO9Lue5YuB9bvX9M9PaJHNuw3+o95rkyovxnRWE76nu14f0bEcQUmC0a/uF82aP3Hhu6GeHNf3M8n'
    b'6/c4/wV37GuZVuScXOxb33Rihnp2hnkHEbLft55dLpnjuQLTwjrE0tH3UpEgXmgB0SU4Z90zs9Jm4/IFd5arwW55UGjA42QC'
    b'Esqs9kpHfWm1UzBXGp0Oi+ByLajkcs1u8/KSIDBUqbVGKHE7apcLdrfgnFlvrrumjxrBuVogH/VWe00NpQvudsGmZgKxIAut'
    b'FU6zsyGLM9rMDSUO0V6ZxY3CUJUKNbUOp9mWb7O6LdXXBKtyE0IYwzImwDD7GiyVazxU0Gs7CGanpTp5oSg4G8Zzf8jstjrs'
    b'yTnmWvwfF1DWKjh/yj3HZna5fsoDSyLL4rbWCddK4NquFP21nbHg4k+VvMBqd4/nbHQKdePmFiH+rqZzi4Rqq8U2DgwqA3JT'
    b'UVDHgjNfBcyMiM5me2WFY22BWDHqnLUgxCukMG5NVDscrtAMjFuZjGnGRRpeMD7eMkJYukaTz3HU1EAOwzA3sYnCCPqaWndD'
    b'GMVkN9eZrTZzBdRRKL3AUc8VI/5dKBF6kmUNly/aC63gkm12u21CgSk3GX6F5gZIK8tmaygVzDWEJpvjsLvNVjuwYRiZ09uF'
    b'mvF8hdM5erqCzpCAQ3SDKHALFrfDmTzGObnUUZuMX9Adk1nOGI/lMGzx5BKLwylkr+ZsQpU72QwZYqk4EXYwWcCkGYFxB8F3'
    b'L6iEyBBuXbBZa6x26g/lnEusdQo1ZktDObfaaUbA9XKuwrp6ZYXD5SrnasyVK1ebawR4BX9Wl9W+Gt7sGIFbqCznlplrahyJ'
    b'3DI3lNOVyKXeC6RKa1WV1SLa3A2JnGB2QXzVZif4rRSqzEAFHzWOSiERocHrBBcGBqkGfxUON37ZoUoguSB4LWadOqe9IRyd'
    b'PMwHwyYvso9LraoKJ+cKZuzKKJvD6FnjUrMhX2MjJhpEOxYaPRBDUBKhx+BHobkCmgRhERHnmGEdWqE75lprOBgtCADRWoNC'
    b'njoC/AMrBl4ZZDF6Ed2OYsHiqENhqjrm2JCVC7DHFDhEJ2daUFRrtkBjQj+qddih4cdUqIDYwuGFt9kwL1d5uyechGx9lb8c'
    b'AnEfEx9kM5wCpa4cz1cNsKJlTCrUs8cA1LMclgg26DJCJafmJPi9BApOGJd5VptNLQ2+BnLMSMHGUMmjYwYwZfAjkDK2J7nV'
    b'CRCVoF+bxY2OxnmOOtYo9MyyV6qhkxnVVS3YAsNxSQEoAUazuzrP6ahZkFtodbnLQsbiHOhiTnO2DVrO6QAPduALs40uPAj5'
    b'LsT+EkrIgs4X5gFFJ7ZZmK/cYH8Mi9xqF93h0SGvpo4l3EOEAtBb1BsYQj5DomCUMUkx4mi0o9/3cFC9bpAmLhy/qOmLwUMR'
    b'1Z8rpI6p/3AlFpB5tkVWoT7ZRa9BQRmUh8EQBgfGgXWZjNdQrHai4pOsr7SimA2JGDyU2KyVbHgMIRoFpwX7yz+SZp5VsFUW'
    b'VaHnEHzQZVjmu6sczppyuv9AG7Pm4TD8WK6IK+SyuWJOz2VxD8KzmFtJX0agZ3FL4CuP/OSS21X4sz8jPH4t5EzwLOFKccJ1'
    b'XdykiTHRUdyEqBhtbBwXERk9cdJ11//D+Kigz/7T92PUUAvdXQGV/ZNR/Jzw/8z9HsH8jwl855jwKSmuxHWJKSkpifpxcfBD'
    b'dQlUL0O/UWEMF5E2MxskUoJCBkFOw9ObmRL6DZOMx8Nz1JQ/8cSMeRzXkw0fGRyXCP9NGY4bPPdv3PdtOse9nLF84775H/T2'
    b'gp+WHPje+0Hvm3tnzXoK/WYu35g9v3jn9PkctyGHa/p97ge9hemzZrnA7VhOVdWXEC4uw+ibkl5V9a8Z5xfuTO/+tjpj5/So'
    b'DIh7/mn+cObBTywZmydGZ3Rubs5ANMZZs2Zl3OpdkzG76bdz4X0vvO9N21GQfiCxM2PiiT+kQ/Yh/03ZkA6k2QS/zkyTyQT/'
    b'mZnwfeD8wjmQzpx0yG/6iT/UQpxoaJ6JtP903DDhfvCf8X8/WJs6'
))

# The direct-return build changes only the settings adapter, not the sandbox blob.
PREVIOUS_ENTRIES[PREVIOUS_SETTINGS_BACK_SHA256] = ENTRIES
PREVIOUS_ENTRIES[PREVIOUS_DIRECT_HANGAR_SHA256] = ENTRIES
