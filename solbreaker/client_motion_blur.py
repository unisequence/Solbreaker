"""Install the opt-in camera blur shader in an idle prepared client."""
import hashlib
import json
from pathlib import Path

import yaml

from . import dvpl
from .client_battle_settings import _walk

SHADER = Path('Materials/Shaders/2d/scene-blit-fp.sl.dvpl')
SHADER_SHA256 = '42f1c765ab13bf23d1ba8ab598dc70cd6916e5b08acb99943cc89a25d0a8d5e8'
GRAPHICS = Path('UI/Screens/Options/GraphicsOptions/GraphicsOptionsPage.yaml.dvpl')
BATTLE = Path('UI/Screens3/Battle/BattleScreen.yaml.dvpl')
RECEIPT = 'motion_blur_shader_sha256'
LABEL_KEY = '#solbreaker:MotionBlur'


def battle_framebuffer(source):
    document = yaml.safe_load(dvpl.decode(source))
    root, = document['Prototypes']
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
[material][a] property float4 solbreakerCameraBlur = float4(0.0, 0.0, 0.0, 0.0);''')
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

#if COLOR_GRADING
    half3 graded''')
    return dvpl.encode(shader)


def graphics_slider(source):
    document = yaml.safe_load(dvpl.decode(source))
    root, = document['Prototypes']
    rows, = (node for node in _walk(root) if node.get('name') == 'scrollContainerControl')
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


def install_motion_blur(lab, manifest):
    from .client_ui import STRINGS_EN, STRINGS_RU, _StringsLoader, local_directory
    from .connection import atomic_write

    previous = manifest.get(RECEIPT)
    source = Path(manifest.get('source_game', manifest['game'])).resolve(strict=True)
    data = lab/'client/Data'
    local_directory(data, source)
    changes = {}
    for relative, patch in ((SHADER, motion_blur_shader), (BATTLE, battle_framebuffer),
                            (GRAPHICS, graphics_slider)):
        original = (source/'Data'/relative).read_bytes()
        modified = patch(original)
        parent = data
        for name in relative.parts[:-1]:
            parent /= name
            local_directory(parent, source)
            if parent.resolve(strict=True) != parent:
                raise ValueError('Motion blur requires private client directories')
        target = lab/'client/Data'/relative
        current = target.read_bytes()
        recorded = previous if isinstance(previous, str) and relative == SHADER else (
            previous.get(relative.as_posix()) if isinstance(previous, dict) else None)
        if current not in (original, modified) and hashlib.sha256(current).hexdigest() != recorded:
            raise ValueError('A motion blur resource already has a different local modification')
        changes[relative] = (target, current, modified)
    manifest_path = lab/'manifest.json'
    manifest = dict(manifest)
    receipts = {key.as_posix(): hashlib.sha256(row[2]).hexdigest()
                for key, row in changes.items()}
    overrides = dict(manifest.get('ui_overrides', {}))
    for relative, label in ((STRINGS_EN, 'Motion Blur'), (STRINGS_RU, 'Размытие в движении')):
        target = data/relative
        document = yaml.load(dvpl.read(target), Loader=_StringsLoader)
        if document.get(LABEL_KEY) != label:
            local_directory(target.parent, source)
            if target.parent.resolve(strict=True) != target.parent:
                raise ValueError('Motion blur requires private client directories')
            document[LABEL_KEY] = label
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
