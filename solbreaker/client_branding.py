"""Neutral, generated placeholders for standalone vendor logo textures.

No source artwork is bundled. Texture dimensions remain unchanged so native
sprite sizing and neighbouring controls retain their original geometry.
"""
import base64
from pathlib import Path
import struct
from . import dvpl

TEXTURES = {'Gfx/UI/SettingsScreen/PrimaryTab/Wg_logo.packed.webp.dvpl': '512x256', 'Gfx/UI/SettingsScreen/PrimaryTab/Wg_logo@2x.packed.webp.dvpl': '1024x512', 'Gfx/UI/LobbyCommon/BlitzLogo.packed.webp.dvpl': '512x512', 'Gfx/UI/LobbyCommon/BlitzLogo@2x.packed.webp.dvpl': '1024x512', 'Gfx/UI/LobbyCommon/BlitzLogo_url.packed.webp.dvpl': '256x64', 'Gfx/UI/LobbyCommon/BlitzLogo_url@2x.packed.webp.dvpl': '512x128', 'Gfx/Lobby/backgrounds/splash-screens/logo_wotb.packed.webp.dvpl': '1024x1024', 'Gfx/Lobby/backgrounds/splash-screens/logo_wotb@2x.packed.webp.dvpl': '2048x2048', 'Gfx/Lobby/events/2026-PROGRESS/BlitzLogo.packed.webp.dvpl': '256x256', 'Gfx/Lobby/events/2026-PROGRESS/BlitzLogo@2x.packed.webp.dvpl': '512x512'}

# Lossless transparent WebP images generated from empty RGBA canvases.
_BLANKS = {'512x256': 'UklGRiQAAABXRUJQVlA4TBgAAAAv/8E/EAcQEREAUKT//ymi/6n//e9//wE=', '1024x512': 'UklGRjYAAABXRUJQVlA4TCoAAAAv/8N/EAcQEREQCCT7i49QRP8z/vOf//znP//5z3/+85///Oc///nP/wU=', '512x512': 'UklGRiwAAABXRUJQVlA4TB8AAAAv/8F/EAcQEREQCCT7e89QRP8z/vOf//znP//5z/8BAA==', '256x64': 'UklGRiAAAABXRUJQVlA4TBQAAAAv/8APEAcQEREGICH8/69E9D/1Dw==', '512x128': 'UklGRiIAAABXRUJQVlA4TBUAAAAv/8EfEAcQEREAUKT//ymi/6n//QcA', '1024x1024': 'UklGRkwAAABXRUJQVlA4TEAAAAAv/8P/EAcQEREQCCT7m09QRP8z/vOf//znP//5z3/+85///Oc///nPf/7zn//85z//+c9//vOf//znP//5z/8N', '2048x2048': 'UklGRtAAAABXRUJQVlA4TMQAAAAv/8f/EQcQEREQEiT+/24mov8Z//nPf/7zn//85z//+c9//vOf//znP//5z3/+85///Oc///nPf/7zn//85z//+c9//vOf//znP//5z3/+85///Oc///nPf/7zn//85z//+c9//vOf//znP//5z3/+85///Oc///nPf/7zn//85z//+c9//vOf//znP//5z3/+85///Oc///nPf/7zn//85z//+c9//vOf//znP//5z3/+85///Oc///nPf/7zn//85/8e', '256x256': 'UklGRiIAAABXRUJQVlA4TBUAAAAv/8A/EAcQEREAUKT//ymi/6n//QcA'}

def patches():
    return {relative: dvpl.encode(base64.b64decode(_BLANKS[size]))
            for relative, size in TEXTURES.items()}

# Reuse the hangar's existing neutral display texture for branded boards.
# Change name-table entries only; geometry, components and IDs remain intact.
SCENES = {
    '3d/Maps/hangar_main_2022/hv_main.sc2.dvpl': {
        'Environment/images/env_hg_board_albedo.tex': 'Environment/images/image_albedo.tex',
        'Environment/images_pbr/env_hg_board_BC.tex': 'Environment/images_pbr/image.tex',
    },
    '3d/Maps/hangar_main_2022/slotobjects/banner_slot.sc2.dvpl': {
        'images/board_1.tex': '../Environment/images/image_albedo.tex',
        'images/board2_albedo.tex': '../Environment/images/image_albedo.tex',
        'pbr_images/board2_BC.tex': '../Environment/images_pbr/image.tex',
    },
}


def replace_texture_names(data, replacements, *, version):
    if data[:8] != b'SFV2' + struct.pack('<I', version):
        raise ValueError('Unsupported SC2 scene version')
    for old, new in replacements.items():
        old, new = old.encode(), new.encode()
        before = struct.pack('<H', len(old)) + old
        after = struct.pack('<H', len(new)) + new
        if data.count(before) != 1:
            raise ValueError('Unsupported SC2 texture name table')
        data = data.replace(before, after)
    return data


def scene_patch(source, replacements):
    return dvpl.encode(replace_texture_names(dvpl.read(source), replacements, version=48))


def scene_patches(game):
    return {relative: scene_patch(Path(game)/'Data'/relative, replacements)
            for relative, replacements in SCENES.items()}

# The loading emblem shares an atlas with unrelated UI icons. Give only its
# sprite descriptor a separate transparent atlas, preserving both frame rects.
DESCRIPTORS = {'Gfx/UI/CommonUIElements/LoadingProcessLogo.txt.dvpl': ('texture0.packed.webp', 'tfs_no_brand.packed.webp'), 'Gfx/UI/CommonUIElements/LoadingProcessLogo@2x.txt.dvpl': ('texture0@2x.packed.webp', 'tfs_no_brand@2x.packed.webp')}
_DESCRIPTOR_BLANKS = {'Gfx/UI/CommonUIElements/tfs_no_brand.packed.webp.dvpl': 'UklGRiIAAABXRUJQVlA4TBUAAAAv/8EfEAcQEREAUKT//ymi/6n//QcA', 'Gfx/UI/CommonUIElements/tfs_no_brand@2x.packed.webp.dvpl': 'UklGRiwAAABXRUJQVlA4TB8AAAAv/8M/EAcQEREQCCT7e89QRP8z/vOf//znP//5z/8BAA=='}


def descriptor_patches(game):
    result = {relative: dvpl.encode(base64.b64decode(data))
              for relative, data in _DESCRIPTOR_BLANKS.items()}
    for relative, (original, neutral) in DESCRIPTORS.items():
        text = dvpl.read(Path(game)/'Data'/relative).decode('utf-8')
        lines = text.splitlines()
        if len(lines) < 5 or lines[0] != '1' or lines[1] != original:
            raise ValueError('Unsupported loading emblem descriptor')
        lines[1] = neutral
        result[relative] = dvpl.encode(('\n'.join(lines) + '\n').encode())
    return result
