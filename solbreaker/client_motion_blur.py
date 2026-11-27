"""Install the opt-in camera blur shader in an idle prepared client."""
import hashlib
import json
from pathlib import Path

import yaml

from . import dvpl
from .client_battle_settings import _walk, patch_boss_screen

SHADER = Path('Materials/Shaders/2d/scene-blit-fp.sl.dvpl')
SHADER_SHA256 = '42f1c765ab13bf23d1ba8ab598dc70cd6916e5b08acb99943cc89a25d0a8d5e8'
GRAPHICS = Path('UI/Screens/Options/GraphicsOptions/GraphicsOptionsPage.yaml.dvpl')
BATTLE = Path('UI/Screens3/Battle/BattleScreen.yaml.dvpl')
RECEIPT = 'motion_blur_shader_sha256'
LABEL_KEY = '#solbreaker:MotionBlur'
RECOVERY_KEY = '#solbreaker:Battle/AutoRecovery'


def _range_ability_hud():
    icon_root = '~res:/Gfx/Shared/tank-supply/consumables'
    icons = (
        '2021_Lunar/normal/consumable_jump_s',
        '2020_Mad-games/normal/consumable_dash_s',
        '2020_Mad-games/normal/consumable_forsage_s',
    )

    def label(name, position, size, font, *, text=''):
        return {'class': 'UIStaticText', 'name': name, 'input': False,
                'position': position, 'size': size, 'components': {'UITextComponent': {
                    'text': text, 'fontPath': '~res:/Fonts/WarHeliosCondCBold.ttf', 'fontSize': font,
                    'color': [1., 1., 1., 1.], 'colorInheritType': 'COLOR_IGNORE_PARENT',
                    'align': ['HCENTER', 'VCENTER'], 'shadowColor': [0., 0., 0., .9],
                    'shadowOffset': [1., 1.]}}}

    rows = []
    for i, icon in enumerate(icons):
        rows.append({'class': 'UIControl', 'name': f'Slot{i}', 'input': False, 'visible': False,
                     'position': [i * 53., 0.], 'size': [48., 48.],
                     'components': {'Background': {'drawType': 'DRAW_FILL', 'color': [.07, .11, .14, .85]}},
                     'children': [
                         {'class': 'UIControl', 'name': 'Icon', 'input': False,
                          'position': [4., 4.], 'size': [40., 40.], 'components': {'Background': {
                              'drawType': 'DRAW_SCALE_PROPORTIONAL_ONE', 'sprite': f'{icon_root}/{icon}'}}},
                         {'class': 'UIControl', 'name': 'Dim', 'input': False, 'visible': False,
                          'size': [48., 48.], 'components': {'Background': {
                              'drawType': 'DRAW_FILL', 'color': [.02, .03, .04, .64]}}},
                         label('Time', [0., 0.], [48., 48.], 23.),
                         label('Key', [0., 48.], [48., 15.], 13., text=str(7 + i)),
                     ]})
    return {'class': 'UIControl', 'name': 'SolRangeAbilities', 'input': False, 'size': [154., 63.],
            'components': {'Anchor': {'rightAnchorEnabled': True, 'rightAnchor': 15.,
                                      'bottomAnchorEnabled': True, 'bottomAnchor': 100.}},
            'children': rows}


RECOVERY_ATLAS = 'auto-recovery.packed.webp'
RECOVERY_ART = 'SolbreakerRecovery'


def _recovery_warning_hud():
    def label(name, text, y, height, font, color):
        return {
            'class': 'UIStaticText', 'name': name, 'input': False,
            'position': [0., y], 'size': [152., height],
            'components': {'UITextComponent': {
                'text': text, 'fontPath': '~res:/Fonts/WarHeliosCondCBold.ttf',
                'fontSize': font, 'align': ['HCENTER', 'VCENTER'], 'color': color,
                'colorInheritType': 'COLOR_IGNORE_PARENT',
                'shadowColor': [0., 0., 0., .9], 'shadowOffset': [0., 1.],
            }},
        }

    rings = [
        {'class': 'UIControl', 'name': f'Ring{seconds}', 'input': False, 'visible': False,
         'size': [112., 112.], 'position': [20., 0.], 'components': {'Background': {
             'drawType': 'DRAW_SCALE_TO_RECT', 'sprite': f'~res:/{RECOVERY_ART}/ring{seconds}',
         }}}
        for seconds in range(1, 4)
    ]
    return {
        'class': 'UIControl', 'name': 'SolAutoRecovery', 'input': False,
        'visible': False, 'size': [152., 131.],
        'components': {'Anchor': {'hCenterAnchorEnabled': True,
                                  'vCenterAnchorEnabled': True, 'vCenterAnchor': 150.}},
        'children': [
            *rings,
            label('Time', '3', 60., 34., 33., [1., .97, 1., 1.]),
            label('Recovery', RECOVERY_KEY, 111., 19., 16., [.91, .84, 1., 1.]),
        ],
    }


def _install_recovery_art(data):
    from .connection import atomic_write

    directory = data / RECOVERY_ART
    directory.mkdir(exist_ok=True)
    if directory.resolve(strict=True) != directory:
        raise ValueError('Auto-recovery art requires a private resource directory')
    atlas = Path(__file__).with_name('assets') / RECOVERY_ATLAS
    files = {RECOVERY_ATLAS: atlas.read_bytes()}
    for seconds in range(1, 4):
        x = (seconds - 1) * 256 + 16
        files[f'ring{seconds}.txt'] = (
            f'1\n{RECOVERY_ATLAS}\n224 224\n1\n{x} 16 224 224 0 0 0 frame0\n'.encode())
    for name, payload in files.items():
        target = directory / f'{name}.dvpl'
        encoded = dvpl.encode(payload)
        if target.is_symlink() or not target.is_file() or target.read_bytes() != encoded:
            atomic_write(target, encoded, replace_symlink=True)


def battle_framebuffer(source):
    from .client_time_stop import patch_hud

    document = yaml.safe_load(dvpl.decode(source))
    root, = document['Prototypes']
    names = {node.get('name') for node in root['children']}
    if 'SolAimTime' in names:
        if 'SolAutoRecovery' in names:
            return source
        root['children'].insert(-1, _recovery_warning_hud())
        return dvpl.encode(yaml.safe_dump(document, sort_keys=False).encode())
    patch_boss_screen(document)
    patch_hud(root)
    root['children'].insert(-1, _range_ability_hud())
    root['children'].insert(-1, _recovery_warning_hud())
    root['children'].insert(-1, {
        'class': 'UIStaticText', 'name': 'SolAimTime', 'input': False, 'visible': False,
        'size': [64., 26.],
        'components': {
            'Anchor': {'hCenterAnchorEnabled': True, 'vCenterAnchorEnabled': True, 'vCenterAnchor': -104.},
            'UITextComponent': {
                'text': '0.0', 'fontPath': '~res:/Fonts/WarHeliosCondCBold.ttf', 'fontSize': 16.,
                'align': ['HCENTER', 'VCENTER'], 'color': [.12, .12, .12, .94],
                'shadowColor': [1., 1., 1., .6], 'shadowOffset': [0., -1.],
            },
        },
    })
    scene, = (node for node in _walk(root) if node.get('class') == 'UI3DView' and node.get('name') == 'Scene')
    scene['components'].setdefault('SceneComponent', {})['drawToFramebuffer'] = True
    return dvpl.encode(yaml.safe_dump(document, sort_keys=False).encode())


def motion_blur_shader(source):
    shader = dvpl.decode(source)
    if hashlib.sha256(shader).hexdigest() != SHADER_SHA256:
        raise ValueError('Motion blur requires the pinned scene compositing shader')
    # The shader source ships as a small insertion. The stock shader and all
    # includes are read from the player's installation.
    shader = shader.replace(b'\r\n', b'\n')
    shader = shader.replace(b'uniform sampler2D tex;', b'''uniform sampler2D tex;
[material][a] property float4 solbreakerCameraBlur = float4(0.0, 0.0, 0.0, 0.0);
[material][a] property float4 solbreakerTemporalGlitch = float4(0.0, 0.0, 0.0, 0.0);''')
    shader = shader.replace(b'#if COLOR_GRADING\n    half3 graded', b'''    float2 travel = solbreakerCameraBlur.xy;
    if (dot(travel, travel) > 0.00000000125)
    {
        float2 uv = input.uv;
        float4 trail =
            tex2D(tex, clamp(uv + travel * 0.25, 0.0, 1.0)) +
            tex2D(tex, clamp(uv + travel * 0.50, 0.0, 1.0)) +
            tex2D(tex, clamp(uv + travel * 0.75, 0.0, 1.0)) +
            tex2D(tex, clamp(uv + travel, 0.0, 1.0));
        output.color.rgb = output.color.rgb * 0.28 + trail.rgb * 0.18;
    }
    float temporal = solbreakerCameraBlur.z;
    if (temporal > 0.0001)
    {
        float brightness = dot(output.color.rgb, float3(0.2126, 0.7152, 0.0722));
        float3 stillFrame = brightness * float3(0.77, 0.98, 1.12);
        float2 centered = input.uv * 2.0 - float2(1.0, 1.0);
        float rim = smoothstep(0.32, 1.42, dot(centered, centered));
        output.color.rgb = lerp(output.color.rgb, stillFrame, temporal)
                         + float3(0.10, 0.35, 0.44) * rim * solbreakerCameraBlur.w;
    }

    float radius = solbreakerTemporalGlitch.z;
    if (radius > 0.0001)
    {
        float phase = abs(solbreakerTemporalGlitch.w);
        float attack = smoothstep(0.0, 0.18, phase);
        float decay = 1.0 - smoothstep(0.18, 1.0, phase);
        float envelope = solbreakerTemporalGlitch.w < 0.0 ? decay * decay : attack * decay;
        float2 local = (input.uv - solbreakerTemporalGlitch.xy) / radius;
        local.y *= 1.65;
        float mask = 1.0 - smoothstep(0.15, 1.0, dot(local, local));
        if (mask > 0.0)
        {
            float row = local.y * 5.0 + floor(phase * 6.0) * 0.67;
            float stripe = step(0.54, row - floor(row));
            float split = min(0.009, radius * 0.11) * envelope * (0.4 + 0.6 * stripe);
            float2 shift = float2(split * (2.0 * stripe - 1.0), 0.0);
            float3 left = tex2D(tex, clamp(input.uv - shift, 0.0, 1.0)).rgb;
            float3 right = tex2D(tex, clamp(input.uv + shift, 0.0, 1.0)).rgb;
            float3 channels = float3(left.r, right.g, right.b);
            float3 color = lerp(float3(0.31, 0.04, 0.18), float3(0.02, 0.21, 0.31), stripe);
            output.color.rgb = lerp(output.color.rgb, channels + color, mask * envelope * 0.64);
        }
    }

#if COLOR_GRADING
    half3 graded''')
    return dvpl.encode(shader)


def graphics_slider(source):
    document = yaml.safe_load(dvpl.decode(source))
    root, = document['Prototypes']
    rows, = (node for node in _walk(root) if node.get('name') == 'scrollContainerControl')
    if any(row.get('name') == 'SolbreakerMotionBlur' for row in rows['children']):
        return source
    document['ImportedPackages'].append('~res:/UI/Screens/Common/Slider.yaml')
    rows['children'].insert(1, {
        'prototype': 'OptionEditor/EditorRow', 'name': 'SolbreakerMotionBlur',
        'children': [
            {'path': 'Background/NameAndDescription/Name', 'components': {'UITextComponent': {
                'text': LABEL_KEY}}},
            {'path': 'Background/Editor', 'children': [
                {'prototype': 'Slider/Slider', 'name': 'SolbreakerBlurSlider', 'size': [400., 64.],
                 'input': True, 'components': {'Anchor': {'vCenterAnchorEnabled': True}},
                 'children': [
                     {'class': 'UIStaticText', 'name': 'SolbreakerBlurPercent', 'input': False,
                      'size': [70., 64.], 'classes': 'header4 text-color-white',
                      'components': {
                          'Anchor': {'rightAnchorEnabled': True, 'rightAnchor': -90., 'vCenterAnchorEnabled': True},
                          'UITextComponent': {'text': '0%', 'align': ['RIGHT', 'VCENTER']},
                          'StyleSheet': {'styles': '~res:/UI/Screens/Fonts.yaml'}}},
                 ]},
            ], 'components': {'SizePolicy': {'horizontalPolicy': 'FixedSize', 'horizontalValue': 490.}}},
        ],
    })
    return dvpl.encode(yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode())


def install_motion_blur(lab, manifest, *, copied=False):
    from .client_ui import STRINGS_EN, STRINGS_RU, _StringsLoader, local_directory
    from .connection import atomic_write

    previous = manifest.get(RECEIPT)
    source = lab/'client' if copied else Path(manifest.get('source_game', manifest['game'])).resolve(strict=True)
    data = lab/'client/Data'
    local_directory(data, source)
    changes = {}
    overrides = dict(manifest.get('ui_overrides', {}))
    for relative, patch in ((SHADER, motion_blur_shader), (BATTLE, battle_framebuffer),
                            (GRAPHICS, graphics_slider)):
        original = (source/'Data'/relative).read_bytes()
        recorded = previous if isinstance(previous, str) and relative == SHADER else (
            previous.get(relative.as_posix()) if isinstance(previous, dict) else None)
        modified = (original if copied and relative == SHADER
                    and hashlib.sha256(original).hexdigest() == recorded else patch(original))
        parent = data
        for name in relative.parts[:-1]:
            parent /= name
            local_directory(parent, source)
            if parent.resolve(strict=True) != parent:
                raise ValueError('Motion blur requires private client directories')
        target = lab/'client/Data'/relative
        current = target.read_bytes()
        if current not in (original, modified) and hashlib.sha256(current).hexdigest() not in (recorded, overrides.get(relative.as_posix())):
            raise ValueError('A motion blur resource already has a different local modification')
        changes[relative] = (target, current, modified)
    _install_recovery_art(data)
    manifest_path = lab/'manifest.json'
    manifest = dict(manifest)
    receipts = {key.as_posix(): hashlib.sha256(row[2]).hexdigest()
                for key, row in changes.items()}
    if BATTLE.as_posix() in overrides:
        overrides[BATTLE.as_posix()] = receipts[BATTLE.as_posix()]
    for relative, labels in (
        (STRINGS_EN, {LABEL_KEY: 'MOTION BLUR', RECOVERY_KEY: 'Auto-righting'}),
        (STRINGS_RU, {LABEL_KEY: 'РАЗМЫТИЕ В ДВИЖЕНИИ', RECOVERY_KEY: 'Автопереворот'}),
    ):
        target = data/relative
        document = yaml.load(dvpl.read(target), Loader=_StringsLoader)
        if any(document.get(key) != value for key, value in labels.items()):
            local_directory(target.parent, source)
            if target.parent.resolve(strict=True) != target.parent:
                raise ValueError('Motion blur requires private client directories')
            document.update(labels)
            content = dvpl.encode(yaml.safe_dump(
                document, sort_keys=False, allow_unicode=True, width=1 << 30).encode())
            atomic_write(target, content, replace_symlink=True)
        overrides[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    for target, current, content in changes.values():
        if current != content:
            atomic_write(target, content, replace_symlink=True)
    if receipts != previous or overrides != manifest.get('ui_overrides'):
        manifest.update({RECEIPT: receipts, 'ui_overrides': overrides})
        atomic_write(manifest_path, (json.dumps(manifest, indent=2)+'\n').encode())
