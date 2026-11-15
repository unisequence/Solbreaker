"""HUD, clock art and local audio for a time branch."""
from functools import lru_cache
from io import BytesIO
import math
from pathlib import Path
import struct
import wave

from . import dvpl
from .connection import atomic_write

# The original clock and hand share one transparent atlas. Coordinates are
# (x, y, width, height) within the atlas, as required by DAVA sprite descriptors.
_CLOCK_FRAMES = {'clock': (0, 0, 256, 320), 'hand': (266, 0, 28, 66)}
_CLOCK_ATLAS = 'time-stop-clock.packed.webp'


@lru_cache(maxsize=1)
def _clock_resources():
    texture = (Path(__file__).with_name('assets')/_CLOCK_ATLAS).read_bytes()
    resources = {_CLOCK_ATLAS+'.dvpl': dvpl.encode(texture)}
    for name, (x, y, width, height) in _CLOCK_FRAMES.items():
        descriptor = f'1\n{_CLOCK_ATLAS}\n{width} {height}\n1\n{x} {y} {width} {height} 0 0 0 frame0\n'
        resources[name+'.txt.dvpl'] = dvpl.encode(descriptor.encode())
    return resources


@lru_cache(maxsize=2)
def _effect(merge):
    """Render a short two-tone cue with a soft attack and no clipping."""
    rate = 22050
    duration = .43 if merge else .30
    phase = 0.
    count = round(duration*rate)
    samples = bytearray()
    for i in range(count):
        t = i/rate
        position = t/duration
        # Descent on entry, ascent on return, with a small separated clock tick.
        frequency = 150.+220.*position if merge else 365.-230.*position
        phase += math.tau*frequency/rate
        envelope = min(1., t/.012)*(1.-position)**1.45
        tick = max(0., t-(.10 if merge else .072))
        accent = math.sin(math.tau*1100.*tick)*min(1., tick/.006)*math.exp(-37.*tick)
        sample = .23*envelope*(math.sin(phase)+.21*math.sin(phase*2.01)) + .08*accent
        samples += struct.pack('<h', round(32767*sample))
    buffer = BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setparams((1, 2, rate, count, 'NONE', 'not compressed'))
        writer.writeframes(samples)
    return buffer.getvalue()


def install(lab):
    directory = lab/'client/Data/SolbreakerTimeStop'
    directory.mkdir(exist_ok=True)
    if directory.resolve(strict=True) != directory:
        raise ValueError('Time Stop requires a private resource directory')
    resources = {
        'enter.wav': _effect(False),
        'merge.wav': _effect(True),
        **_clock_resources(),
    }
    for name, payload in resources.items():
        target = directory/name
        if not target.is_file() or target.read_bytes() != payload:
            atomic_write(target, payload)


def patch_hud(root):
    def hand(name, width, height, angle):
        return {
            'class': 'UIControl', 'name': name, 'input': False,
            'size': [width, height], 'position': [128., 130.], 'pivot': [.5, .91],
            'angle': angle,
            'components': {'Background': {'drawType': 'DRAW_SCALE_TO_RECT',
                                          'sprite': '~res:/SolbreakerTimeStop/hand'}},
        }

    root['children'].insert(-1, {
        'class': 'UIControl', 'name': 'SolTimeClock', 'input': False, 'visible': False,
        'size': [256., 320.], 'pivot': [.5, 255./320.],
        'components': {
            'Background': {'drawType': 'DRAW_SCALE_TO_RECT',
                           'sprite': '~res:/SolbreakerTimeStop/clock'},
            'UIOpacityComponent': {'opacity': 1.},
        },
        'children': [hand('Hour', 31., 42., -58.), hand('Hand', 31., 66., 51.)],
    })
    root['children'].insert(-1, {
        'class': 'UIControl', 'name': 'SolTimeHUD', 'input': False, 'visible': False,
        'size': [188., 51.],
        'components': {'Anchor': {'hCenterAnchorEnabled': True, 'topAnchorEnabled': True,
                                  'topAnchor': 65.}},
        'children': [
            {'class': 'UIControl', 'name': 'Panel', 'input': False, 'size': [188., 51.],
             'components': {'Background': {'drawType': 'DRAW_FILL', 'color': [.03, .06, .08, .88]}}},
            {'class': 'UIControl', 'name': 'Accent', 'input': False, 'size': [188., 2.],
             'components': {'Background': {'drawType': 'DRAW_FILL', 'color': [.35, .81, .88, 1.]}}},
            {'class': 'UIStaticText', 'name': 'Title', 'input': False,
             'position': [0., 5.], 'size': [188., 16.], 'classes': 'text2-small text-color-white',
             'components': {'UITextComponent': {'text': 'TIME STOP', 'align': ['HCENTER', 'VCENTER']},
                            'StyleSheet': {'styles': '~res:/UI/Screens/Fonts.yaml'}}},
            {'class': 'UIStaticText', 'name': 'SolTimeValue', 'input': False,
             'position': [0., 22.], 'size': [188., 26.], 'classes': 'header4 text-color-white',
             'components': {'UITextComponent': {'text': '15s | 050 E', 'align': ['HCENTER', 'VCENTER']},
                            'StyleSheet': {'styles': '~res:/UI/Screens/Fonts.yaml'}}},
        ],
    })
