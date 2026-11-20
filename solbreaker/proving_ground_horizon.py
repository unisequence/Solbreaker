"""Original distant terrain outside the proving ground's physical map border."""
import math

import numpy as np

from .proving_ground_layout import elevation


def horizon_meshes():
    samples = 256
    angles = np.arange(samples) * (math.tau/samples)
    dx, dy = np.cos(angles), np.sin(angles)
    bearing = np.maximum(np.abs(dx),np.abs(dy))
    # The near edge overlaps the last few metres of the heightfield below its
    # surface. At the back, a second massif is visible through the lower saddles.
    border = 509./bearing
    ground = elevation(dx*border,dy*border)-3.
    def wave(frequency, phase):
        return np.sin(frequency*angles+phase)

    peak = 122. + 23.*wave(3,.8) + 19.*wave(5,1.4) + 11.*wave(9,-.7)
    crest = 10.*wave(13,0) + 7.*wave(21,1) + 3.5*wave(37,.3)
    north = np.maximum(0.,dy)
    far = 155. + 30.*north + 38.*wave(3,0) + 26.*wave(7,.24) + 15.*wave(11,-.6)
    rows = []
    bands = 24
    for ring in range(bands + 1):
        t = ring / bands
        shoulder = np.cos(.5 * math.pi * min(1., abs(t - .32) / .32)) ** 1.5
        mountain = np.cos(.5 * math.pi * min(1., abs(t - .76) / .34)) ** 1.5
        variation = 1. + .12*wave(4,-.6) + .08*wave(9,.5)
        r = border + 342.*t*variation + 9.*wave(18,.5+1.8*t)*min(1.,8*t)
        relief = (peak-ground + crest + 7.*wave(32,4*t))*shoulder
        relief += (far-ground + 1.65*crest + 9.*wave(27,7*t))*mountain
        z = ground*(1.-max(0.,(t-.76)*3.3)) + relief
        rows.append(np.column_stack((r*dx,r*dy,z)))
    vertices = np.round(np.vstack(rows),3)
    faces = []
    for ring in range(bands):
        for j in range(samples):
            a = ring * samples + j
            b = ring * samples + (j + 1) % samples
            faces.extend(((a, a + samples, b + samples), (a, b + samples, b)))
    # One draw with a continuous gradient from wooded foothills to exposed rock.
    altitude = vertices[:, 2] + np.tile(9. * wave(6, .3), bands + 1)
    blend = np.clip((altitude - 63.) / 135., 0., 1.)[:, None]
    colors = np.array((.27, .34, .31, 1.)) + blend * np.array((.22, .17, .18, 0.))
    yield dict(name='sb_horizon', color=(.27, .34, .31, 1.), solid=False,
               vertex_colors=np.round(colors, 3).tolist(),
               vertices=vertices.tolist(), triangles=faces)
