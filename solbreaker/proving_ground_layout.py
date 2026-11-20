"""Original Solbreaker range. Coordinates are metres east, north and up.

The terrain, materials and overhead map share the same building and road plan.
No source artwork or map data is required.
"""

import math
import random
import struct
from functools import lru_cache
from itertools import pairwise

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .proving_ground_landscape_detail import (
    QUARRY_YARD, SETTLEMENT_HOUSES, SETTLEMENT_WINDMILL, SHRUBS, TREES, vegetation,
)
from .proving_ground_rules import LOOP_CENTER, LOOP_RADIUS, loop_east

SIZE = 1024
HALF = 512.0
PLAZA_LEVEL = 32.0
# The northwestern bend of the suspension circuit runs roughly 29 degrees
# north of east. The swale crosses the road at right angles.
FORD_CENTER = (-404.7, -214.1)
FORD_WATER_LEVEL = 31.55
_FORD_COS = math.cos(math.radians(29.0))
_FORD_SIN = math.sin(math.radians(29.0))
RUNWAY = (-393.0, 401.0, 393.0, 439.0)
AIRFIELD_HANGARS = ((90.0, 371.0), (148.0, 370.0))
SETTLEMENT_STREET = ((-216., 123.), (-245., 132.), (-290., 134.), (-337., 134.), (-385., 133.))
QUARRY_SERVICE_ROAD = ((-385., 133.), (-407., 151.), (-406., 174.), (-404., 193.))
QUARRY_OFFICE = (-185., 304.)
AIRFIELD_SERVICE = (250., 371.)
BRIDGE_CENTER = (-16., -349.)
BRIDGE_DECK_LEVEL = 36.
BRIDGE_HALF_SPAN = 35.
BRIDGE_HALF_WIDTH = 6.2
BRIDGE_ROAD = ((-144., -223.), (-118., -271.), (-89., -341.), (-58., -349.),
               (24., -349.), (83., -337.), (114., -296.), (150., -277.))
LOW_RAMP_X = 222.0
LOW_RAMP_START = 240.0
LOW_RAMP_CREST = 276.0
LOW_RAMP_END = 310.0
LOW_RAMP_WIDTH = 17.0
STUNT_RAMPS = ((292.0, 45.0, 15.0, 12.0), (325.0, 54.0, 17.0, 20.0))
TRACK = (
    (-439, -275),
    (-417, -224),
    (-367, -201),
    (-318, -223),
    (-272, -200),
    (-220, -200),
    (-171, -253),
    (-209, -302),
    (-165, -344),
    (-207, -390),
    (-282, -377),
    (-326, -406),
    (-405, -394),
    (-440, -349),
    (-406, -316),
)
MAIN_TRACK_WIDTH = 18.0
MAIN_TRACK_SHOULDER = 25.0
MAIN_TRACK = (
    (-218, 0), (-217, 121), (-178, 228), (-100, 302), (22, 308),
    (139, 273), (178, 181), (180, 0), (192, -127), (159, -196),
    (66, -231), (-47, -239), (-135, -216), (-204, -149), (-227, -74),
)
# Access lanes open directly onto the loop. The arena lane enters at its
# west gate and the quarry lane follows the cut across the terraces.
ACCESS_LANES = (
    ((-218, 0), (-159, 0), (-97, 0)),
    ((-218, 0), (-254, 0), (-292, 0)),
    ((-178, 228), (-216, 208), (-251, 221), (-282, 243), (-309, 264)),
    ((22, 308), (28, 347), (28, 397), (28, 409)),
    ((178, 181), (199, 199), (227, 207)),
    ((180, 0), (224, 0), (255, 0)),
    ((159, -196), (150, -237), (153, -278), (175, -295), (204, -295)),
    ((-135, -216), (-169, -210), (-200, -219)),
)


def _smooth(v):
    v = np.clip(v, 0.0, 1.0)
    return v * v * (3.0 - 2.0 * v)


def _footprint(x, y, cx, cy, sx, sy, fade):
    outside = np.maximum(np.abs(x - cx) - sx, np.abs(y - cy) - sy)
    return 1.0 - _smooth(outside / fade)


def _ravine_channel(x, y):
    along = y - BRIDGE_CENTER[1]
    bend = 6. * np.sin(along / 24.) * _smooth(np.abs(along) / 22.)
    return np.abs(x - BRIDGE_CENTER[0] - bend), np.abs(along)


def _ford_radius(x, y):
    dx, dy = x - FORD_CENTER[0], y - FORD_CENTER[1]
    across = -dx * _FORD_SIN + dy * _FORD_COS
    along = dx * _FORD_COS + dy * _FORD_SIN - 2.5 * np.sin(across / 12.0)
    return np.hypot(along / 14.0, across / 33.0), across


def _ford_point(along, across):
    along += 2.5 * math.sin(across / 12.0)
    return (
        FORD_CENTER[0] + along * _FORD_COS - across * _FORD_SIN,
        FORD_CENTER[1] + along * _FORD_SIN + across * _FORD_COS,
    )


def _natural_elevation(x, north):
    x, y = np.asarray(x), np.asarray(north)
    wave = np.sin(0.012 * x + 0.009 * y) * np.cos(0.016 * y - 0.009 * x)
    natural = (
        34.0
        + 2.6 * wave
        + 1.7 * np.cos(0.015 * x - 0.013 * y)
        + 0.5 * np.sin(0.026 * y + 0.016 * x)
    )
    edge = _smooth((np.maximum(np.abs(x), np.abs(y)) - 434.0) / 78.0)
    ridge = (
        29.0
        + 11.0 * np.sin(0.032 * x - 0.022 * y)
        + 7.0 * np.cos(0.055 * y + 0.014 * x)
        + 10.0 * _noise(x, y, 31.0, 35)
        + 7.0 * (1.0 - np.abs(_noise(x, y, 14.0, 26)))
        + 3.0 * _noise(x, y, 6.0, 44)
    )
    # Meadow rises divide the southern fields and the range approach. The
    # later grading still owns the roads, lanes and activity footprints.
    meadows = (
        5.2 * _footprint(x, y, -76, -314, 8, 7, 62)
        + 4.3 * _footprint(x, y, 46, -349, 9, 5, 66)
        + 3.4 * _footprint(x, y, 279, 128, 10, 8, 49)
        + 4.7 * _footprint(x, y, -54, 194, 11, 6, 57)
    )
    height = natural + edge * ridge + meadows

    def grade(base, level, mask):
        return base + (level - base) * mask

    height = grade(height, PLAZA_LEVEL, _footprint(x, y, 0, 0, 130, 78, 65))
    height = grade(height, 34.0, _footprint(x, y, 315, 0, 119, 77, 35))
    height = grade(height, 33.0, _footprint(x, y, -310, 0, 92, 87, 40))
    height = grade(height, 37.0, _footprint(x, y, 294, 267, 113, 83, 36))
    height = grade(height, 40.0, _footprint(x, y, 0, 415, 403, 34, 34))
    arena = np.hypot(x - 300.0, y + 295.0)
    height = grade(height, 32.0, 1.0 - _smooth((arena - 126.0) / 33.0))
    test = _footprint(x, y, -300, -299, 139, 103, 25)
    roller = 1.6 * np.sin((x + 431.0) * 0.10) * _footprint(x, y, -342, -220, 36, 14, 17)
    bank = 3.7 * np.cos((y + 330.0) * 0.105) * _footprint(x, y, -426, -330, 13, 24, 16)
    hill = 7.0 * np.exp(-(((x + 220.0) / 30.0) ** 2) - ((y + 386.0) / 26.0) ** 2)
    height = grade(height, 32.0 + roller + bank + hill, test)
    radius, across = _ford_radius(x, y)
    # The deepest gravel is ~0.8 m below the visible surface. Both approaches
    # follow the continuous heightfield; the water carries no collision mesh.
    approach = (x - FORD_CENTER[0]) * _FORD_COS + (y - FORD_CENTER[1]) * _FORD_SIN
    shoulder = (1.0 - _smooth((np.abs(approach) - 31.0) / 10.0)) * (
        1.0 - _smooth((np.abs(across) - 10.0) / 9.0)
    )
    height = grade(height, 32.0, shoulder)
    bed = 30.73 + 0.12 * np.sin(across * 0.09) + 1.28 * _smooth((radius - 0.38) / 0.79)
    height = grade(height, bed, 1.0 - _smooth((radius - 1.17) / 0.3))
    qx, qy = (x + 309.0) / 125.0, (y - 265.0) / 91.0
    radius = np.hypot(qx, qy) + 0.024 * np.sin(y * 0.081 + x * 0.034)
    terraces = (
        10.0
        + 7.0 * _smooth((radius - 0.37) / 0.10)
        + 8.0 * _smooth((radius - 0.60) / 0.10)
        + 8.0 * _smooth((radius - 0.84) / 0.10)
    )
    # The SE cut is the continuous haul road from the lip to the excavation.
    ramp = _smooth((qx - qy * 0.86 + 0.06) * 2.3)
    quarry_height = grade(terraces, 10.0 + 24.0 * _smooth(radius / 1.02), ramp)
    height = grade(height, quarry_height, 1.0 - _smooth((radius - 0.97) / 0.16))
    height = grade(height, 35.5, _footprint(x, y, *QUARRY_OFFICE, 14., 7., 10.))
    height = grade(height, 34.0, _footprint(x, y, -315., 133., 77., 34., 13.))
    height = grade(height, 34., _footprint(x, y, *QUARRY_YARD, 25., 18., 15.))
    height = grade(height, 35.0, _footprint(x, y, -385, 361, 36, 23, 17))
    height = grade(height, 40.0, _footprint(x, y, 134, 375, 76, 28, 19))
    height = grade(height, 40., _footprint(x, y, *AIRFIELD_SERVICE, 41., 22., 17.))
    return grade(height, BRIDGE_DECK_LEVEL, _footprint(x, y, *BRIDGE_CENTER, 52., 66., 32.))


def _height_at(grid, east, north):
    x, y = np.broadcast_arrays(np.clip(east + HALF, 0, SIZE - 1),
                               np.clip(north + HALF, 0, SIZE - 1))
    ix, iy = x.astype(int), y.astype(int)
    dx, dy = x - ix, y - iy
    nx, ny = np.minimum(ix + 1, SIZE - 1), np.minimum(iy + 1, SIZE - 1)
    low = grid[iy, ix] * (1 - dx) + grid[iy, nx] * dx
    high = grid[ny, ix] * (1 - dx) + grid[ny, nx] * dx
    return low * (1 - dy) + high * dy


def _grade_route(terrain, route, half_width, feather, *, closed=False):
    """Smooth the road along its arc and tie its shoulders into the terrain."""
    points = np.asarray(route)
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    distance = np.r_[0., lengths.cumsum()]
    heights = _height_at(terrain, *points.T)
    offsets = np.arange(-42., 43., 3.)
    weights = np.exp(-0.5 * (offsets / 15.)**2)
    sampled = np.interp(distance[:, None] + offsets, distance, heights,
                        period=distance[-1] if closed else None)
    profile = sampled @ (weights / weights.sum())
    if not closed:
        # Keep the profile tied to the main ring and the adjoining aprons.
        start = 1. - _smooth(distance / 22.)
        end = 1. - _smooth((distance[-1] - distance) / 22.)
        profile += start * (heights[0] - profile[0]) + end * (heights[-1] - profile[-1])
    influence = half_width + feather
    closest = np.full(terrain.shape, influence**2)
    target = np.zeros_like(terrain)
    for i, (a, b) in enumerate(pairwise(points)):
        x0, y0 = np.maximum(0, np.floor(np.minimum(a, b) + HALF - influence).astype(int))
        x1, y1 = np.minimum(SIZE, np.ceil(np.maximum(a, b) + HALF + influence).astype(int) + 1)
        dx = np.arange(x0, x1)[None, :] - a[0] - HALF
        dy = np.arange(y0, y1)[:, None] - a[1] - HALF
        vx, vy = b - a
        t = np.clip((dx * vx + dy * vy) / lengths[i]**2, 0, 1)
        separation = (dx - vx * t)**2 + (dy - vy * t)**2
        window = np.s_[y0:y1, x0:x1]
        nearer = separation < closest[window]
        closest[window][nearer] = separation[nearer]
        target[window][nearer] = (profile[i] + t * (profile[i + 1] - profile[i]))[nearer]
    blend = 1. - _smooth((np.sqrt(closest) - half_width) / feather)
    terrain += (target - terrain) * blend


@lru_cache(maxsize=1)
def _heightfield():
    axis = np.arange(SIZE, dtype=np.float64) - HALF
    terrain = _natural_elevation(axis[None, :], axis[:, None])
    # Give the south-west circuit a smooth dry driving line and blend back
    # into the original ford where the road crosses the water.
    before = terrain.copy()
    _grade_route(terrain, _circuit(), 10., 18., closed=True)
    ford, _ = _ford_radius(axis[None, :], axis[:, None])
    terrain += (before - terrain) * (1. - _smooth((ford - 1.15) / .85))
    # The quarry haul cut and the suspension course have their own relief.
    for lane in (ACCESS_LANES[i] for i in (0, 1, 3, 4, 5, 6)):
        _grade_route(terrain, _curve(lane), 5., 14.)
    _grade_route(terrain, _curve(SETTLEMENT_STREET), 7., 12.)
    _grade_route(terrain, _curve(QUARRY_SERVICE_ROAD), 6., 10.)
    _grade_route(terrain, _curve(BRIDGE_ROAD), 6., 13.)
    _grade_route(terrain, _main_track(), MAIN_TRACK_SHOULDER / 2, 21., closed=True)
    east, north = axis[None, :], axis[:, None]
    across, along = _ravine_channel(east, north)
    terrain -= 5.6 * (1. - _smooth((across - 8.) / 24.)) * (1. - _smooth((along - 45.) / 47.))
    approach = _smooth((north - LOW_RAMP_START) / (LOW_RAMP_CREST - LOW_RAMP_START))
    departure = _smooth((north - LOW_RAMP_CREST) / (LOW_RAMP_END - LOW_RAMP_CREST))
    shoulder = 1.0 - _smooth((np.abs(east - LOW_RAMP_X) - LOW_RAMP_WIDTH / 2) / 52.0)
    # Keep the training ramp's deck and graded shoulders on one continuous
    # heightfield. The tracks and camera can cross its edge at any angle.
    terrain += 5.0 * (approach - departure) * shoulder
    return terrain


def elevation(x, north):
    """Heights shared by the client, the collision map and placed objects."""
    return _height_at(_heightfield(), np.asarray(x), np.asarray(north))


def heightmap():
    """Native uint16 height samples in row-major 16×16 tiles, south row first."""
    metres = _heightfield()
    samples = np.rint(np.clip(metres, 0.0, 100.0) * 65535.0 / 100.0).astype("<u2")
    return (
        struct.pack("<II", SIZE, 16)
        + samples.reshape(64, 16, 64, 16).transpose(0, 2, 1, 3).tobytes()
    )


def _curve(points, closed=False):
    """Sample a centripetal-looking road axis through the surveyed waypoints."""
    p = np.asarray(points, dtype=float)
    chain = np.vstack((p[-1], p, p[0], p[1])) if closed else np.vstack((p[0], p, p[-1]))
    result = []
    for a, b, c, d in zip(chain, chain[1:], chain[2:], chain[3:]):
        count = max(3, math.ceil(np.linalg.norm(c - b) / 4.0))
        for t in np.arange(count) / count:
            xy = 0.5 * (
                2 * b
                + (-a + c) * t
                + (2 * a - 5 * b + 4 * c - d) * t * t
                + (-a + 3 * b - 3 * c + d) * t**3
            )
            result.append(tuple(xy))
    result.append(tuple(p[0] if closed else p[-1]))
    return result


@lru_cache(maxsize=1)
def _main_track():
    return _curve(MAIN_TRACK, closed=True)


@lru_cache(maxsize=1)
def _circuit():
    return _curve(TRACK, True)


class _Canvas:
    def __init__(self, image):
        self.draw = ImageDraw.Draw(image)
        self.scale = image.width / 1024.0

    def point(self, p):
        return ((p[0] + HALF) * self.scale, (HALF - p[1]) * self.scale)

    def width(self, metres):
        return max(1, round(metres * self.scale))

    def line(self, points, width, ink):
        self.draw.line(
            [self.point(p) for p in points],
            fill=ink,
            width=self.width(width),
            joint="curve",
        )

    def ribbon(self, points, width, ink, *, closed=False):
        # Rasterize adjacent quads with shared edges. Wide PIL lines can leave
        # single-pixel holes where two segments meet at a shallow angle.
        points = np.asarray(points)
        delta = np.diff(points, axis=0)
        normals = np.column_stack((-delta[:, 1], delta[:, 0]))
        normals /= np.linalg.norm(normals, axis=1, keepdims=True)
        if closed:
            incoming = np.vstack((normals[-1], normals))
            outgoing = np.vstack((normals, normals[0]))
        else:
            incoming = np.vstack((normals[0], normals))
            outgoing = np.vstack((normals, normals[-1]))
        bisector = incoming + outgoing
        offset = width * 0.5 * bisector / np.sum(bisector * outgoing, axis=1, keepdims=True)
        left = [self.point(p) for p in points + offset]
        right = [self.point(p) for p in points - offset]
        for i in range(len(points) - 1):
            self.draw.polygon((left[i], left[i + 1], right[i + 1], right[i]), fill=ink)

    def rect(self, bounds, ink):
        x0, y0, x1, y1 = bounds
        self.draw.rectangle([self.point((x0, y1)), self.point((x1, y0))], fill=ink)

    def ring(self, x, y, rx, ry, ink, width=0):
        box = [self.point((x - rx, y + ry)), self.point((x + rx, y - ry))]
        if width:
            self.draw.ellipse(box, outline=ink, width=self.width(width))
        else:
            self.draw.ellipse(box, fill=ink)


def _materials(size):
    # 0 asphalt/concrete; 1 grass; 2 rock; 3 sand/gravel.
    image = Image.new("L", (size, size), 1)
    axis = (np.arange(size, dtype=np.float32) + 0.5) * 1024.0 / size - HALF
    grid = np.asarray(image).copy()
    for j in range(0, size, 128):
        xs = axis[None, :]
        ys = -axis[j : j + 128, None]
        ridge = np.maximum(np.abs(xs), np.abs(ys)) > 447.0
        h = elevation(xs, ys)
        grid[j : j + 128][ridge & (h + 5.0 * _noise(xs, ys, 9.0, 12) > 62.0)] = 2
    image = Image.fromarray(grid)
    c = _Canvas(image)
    c.ring(-310, 264, 135, 99, 2)
    c.ring(-310, 264, 108, 78, 3)
    c.ring(-310, 264, 91, 65, 2)
    c.ring(-310, 264, 70, 47, 3)
    c.ring(-310, 264, 41, 30, 2)
    for perimeter in (
        (
            (-413, -48),
            (-403, -81),
            (-353, -88),
            (-309, -77),
            (-264, -81),
            (-232, -66),
            (-221, -31),
            (-229, 11),
            (-243, 63),
            (-278, 73),
            (-323, 69),
            (-364, 84),
            (-397, 61),
            (-408, 26),
        ),
        (
            (199, -28),
            (213, -61),
            (243, -78),
            (302, -72),
            (376, -77),
            (423, -69),
            (436, -41),
            (439, 31),
            (423, 70),
            (370, 81),
            (295, 75),
            (234, 66),
            (205, 39),
        ),
        (
            (193, 241),
            (199, 212),
            (222, 192),
            (268, 189),
            (309, 200),
            (372, 201),
            (402, 224),
            (405, 282),
            (388, 335),
            (345, 345),
            (312, 334),
            (247, 343),
            (210, 317),
        ),
    ):
        c.draw.polygon([c.point(p) for p in perimeter], fill=3)
    c.ring(300, -295, 126, 126, 3)
    c.ring(300, -295, 122, 122, 0, 8)
    for lane in ACCESS_LANES:
        c.ribbon(_curve(lane), 10, 3)
    c.ribbon(_curve(SETTLEMENT_STREET), 14., 3)
    c.ring(-385., 133., 13., 11., 3)
    c.line(((-307., 134.), (-306., 153.), (-287., 153.)), 8., 3)
    for _, x, y, w, d, _ in SETTLEMENT_HOUSES:
        c.rect((x - w - 3., y - d - 3., x + w + 3., y + d + 3.), 3)
        c.line(((x, 133.), (x, y)), 5., 3)
    c.ribbon(_curve(QUARRY_SERVICE_ROAD), 12., 3)
    qx, qy = QUARRY_YARD
    c.rect((qx - 24., qy - 17., qx + 23., qy + 17.), 3)
    ox, oy = QUARRY_OFFICE
    c.rect((ox - 14., oy - 7., ox + 14., oy + 7.), 3)
    c.ribbon(_curve(BRIDGE_ROAD), 12., 3)
    across, along = _ravine_channel(axis[None, :], -axis[:, None])
    ground = np.asarray(image).copy()
    ground[(across < 11.) & (along < 88.)] = 3
    image = Image.fromarray(ground)
    c = _Canvas(image)
    c.ribbon(_main_track(), MAIN_TRACK_SHOULDER, 3, closed=True)
    c.ribbon(_main_track(), MAIN_TRACK_WIDTH, 0, closed=True)
    c.ribbon(_circuit(), 20, 3, closed=True)
    c.ribbon(_circuit(), 12, 0, closed=True)
    ford_bank = [
        _ford_point(
            14.0 * 1.20 * math.cos(i * math.tau / 52),
            33.0 * 1.20 * math.sin(i * math.tau / 52),
        )
        for i in range(52)
    ]
    c.draw.polygon([c.point(p) for p in ford_bank], fill=3)
    c.rect((-420, 340, -350, 382), 3)
    c.ring(0, 0, 112, 65, 3)
    c.ring(0, 0, 108, 61, 0)
    # Maintenance aprons behind the hangars and small, open side service roads.
    c.line(
        (
            (-92, 37),
            (-106, 53),
            (-116, 67),
            (-115, 97),
            (-98, 108),
            (43, 109),
            (64, 96),
            (81, 92),
        ),
        9,
        3,
    )
    c.line(
        (
            (-92, -40),
            (-98, -53),
            (-89, -65),
            (-56, -67),
            (48, -67),
            (74, -59),
            (88, -38),
        ),
        7,
        3,
    )
    c.line(((54, 34), (61, 53), (64, 65), (61, 87), (58, 102)), 6, 3)
    c.line(((94, 22), (107, 34), (117, 40), (125, 55), (124, 66), (109, 74)), 6, 3)
    c.rect((-401, 396, 401, 445), 3)
    c.rect((63, 349, 205, 399), 0)
    ax, ay = AIRFIELD_SERVICE
    c.rect((ax - 46., ay - 17., ax + 36., ay + 21.), 0)
    c.rect((148, 393, 173, 410), 0)
    c.rect(RUNWAY, 0)
    c.rect((LOW_RAMP_X - LOW_RAMP_WIDTH / 2, LOW_RAMP_START - 3,
            LOW_RAMP_X + LOW_RAMP_WIDTH / 2, LOW_RAMP_END + 3), 0)
    return image


def terrain_mask(size=512, *, blended=False):
    """North-up material weights: asphalt R, grass G, rock B, gravel A."""
    indices = _materials(size)
    layers = tuple(indices.point([255 if i == index else 0 for i in range(256)])
                   for index in range(4))
    if blended:
        # Filter each weight independently. The alpha channel is gravel.
        blur = ImageFilter.GaussianBlur(0.42 * size / SIZE)
        layers = tuple(layer.filter(blur) for layer in layers)
    return Image.merge("RGBA", layers)


PALETTE = {
    "mortar": (0.48, 0.48, 0.42),
    "plinth": (0.38, 0.39, 0.36),
    "chalk": (0.82, 0.81, 0.68),
    "steel": (0.20, 0.25, 0.25),
    "galvanized": (0.43, 0.49, 0.46),
    "dark": (0.13, 0.17, 0.18),
    "sage": (0.36, 0.43, 0.35),
    "sage_light": (0.46, 0.51, 0.40),
    "rust": (0.52, 0.26, 0.18),
    "signal": (0.76, 0.22, 0.13),
    "ochre": (0.81, 0.56, 0.21),
    "glass": (0.23, 0.39, 0.43),
    "teal": (0.20, 0.47, 0.53),
    "lamp": (0.92, 0.77, 0.44),
    "bark": (0.32, 0.25, 0.19),
    "pine": (0.18, 0.31, 0.24),
    "needles": (0.27, 0.39, 0.28),
    "aspen": (0.43, 0.52, 0.32),
    "shale": (0.39, 0.41, 0.36),
}


class _Mesh:
    def __init__(self):
        self.groups = {}
        self.zone = "hq"
        self.pivots = {}

    def _add(self, material, vertices, triangles):
        key = "sb_" + self.zone + "_" + material
        if key not in self.groups:
            self.groups[key] = {
                "name": key,
                "color": [*PALETTE[material], 1.0],
                "vertices": [],
                "triangles": [],
            }
            if material in ("pine", "needles"):
                self.groups[key]["solid"] = False
            if self.zone in self.pivots:
                self.groups[key].update(assembly=self.zone, origin=self.pivots[self.zone])
        group = self.groups[key]
        start = len(group["vertices"])
        group["vertices"].extend([round(float(v), 3) for v in p] for p in vertices)
        group["triangles"].extend(
            [start + a, start + b, start + c] for a, b, c in triangles
        )

    def solid(self, bottom, top, mat):
        """Two identically ordered, convex rings, CCW looking from top to base."""
        n = len(bottom)
        faces = []
        for i in range(1, n - 1):
            faces.extend(((0, i + 1, i), (n, n + i, n + i + 1)))
        for i in range(n):
            j = (i + 1) % n
            faces.extend(((i, j, n + j), (i, n + j, n + i)))
        self._add(mat, list(bottom) + list(top), faces)

    def box(self, x, y, z, w, d, h, mat, angle=0.0):
        c, s = math.cos(angle), math.sin(angle)
        xy = [
            (x + u * c - v * s, y + u * s + v * c)
            for u, v in (
                (-w / 2, -d / 2),
                (w / 2, -d / 2),
                (w / 2, d / 2),
                (-w / 2, d / 2),
            )
        ]
        self.solid([(a, b, z) for a, b in xy], [(a, b, z + h) for a, b in xy], mat)

    def column(self, x, y, z, r0, r1, h, n, mat, spin=0.0):
        vec = [
            (math.cos(spin + i * math.tau / n), math.sin(spin + i * math.tau / n))
            for i in range(n)
        ]
        self.solid(
            [(x + a * r0, y + b * r0, z) for a, b in vec],
            [(x + a * r1, y + b * r1, z + h) for a, b in vec],
            mat,
        )

    def beam(self, start, end, width, depth, mat):
        a, b = np.array(start), np.array(end)
        d = b - a
        d = d / np.linalg.norm(d)
        u = np.array((-d[1], d[0], 0.0)) if abs(d[2]) < 0.95 else np.cross((0, 1, 0), d)
        u = u / np.linalg.norm(u)
        v = np.cross(d, u)
        offsets = [
            i * width / 2 * u + j * depth / 2 * v
            for i, j in ((-1, -1), (1, -1), (1, 1), (-1, 1))
        ]
        self.solid([a + o for o in offsets], [b + o for o in offsets], mat)


def _hangar(m, name, x, y, width, depth, *, yaw=0.):
    z = float(elevation(x, y)) - 0.4
    previous_zone, m.zone = m.zone, name
    m.pivots[name] = (x, y, z)
    # Open front on the south apron. Arched roof panels and ribs share the vault.
    rise, eaves = width * 0.23, 8.0

    def arch(t):
        return x - width / 2 * math.cos(t), z + eaves + rise * math.sin(t)

    faces = 10
    for i in range(faces):
        ax, az = arch(i * math.pi / faces)
        bx, bz = arch((i + 1) * math.pi / faces)
        base = [
            (ax, y - depth / 2, az),
            (bx, y - depth / 2, bz),
            (bx, y + depth / 2, bz),
            (ax, y + depth / 2, az),
        ]
        m.solid(
            base,
            [(a, b, c + 0.35) for a, b, c in base],
            "sage" if i % 4 else "sage_light",
        )
        if i % 2 == 0:
            m.beam(
                (ax, y - depth / 2, az + 0.40),
                (ax, y + depth / 2, az + 0.40),
                0.16,
                0.18,
                "galvanized",
            )
    for side in (-1, 1):
        wx = x + side * width / 2
        m.box(wx, y, z, 0.55, depth, eaves, "sage")
        for i in range(7):
            m.box(
                wx + side * 0.31,
                y + depth * (i / 6 - 0.5),
                z,
                0.21,
                0.35,
                eaves + 0.1,
                "galvanized",
            )
        m.box(wx, y, z, 1.2, depth, 1.1, "plinth")
    m.box(x, y + depth / 2, z, width, 0.58, eaves + 1.0, "sage_light")
    for side in (-1, 1):
        m.box(
            x + side * (width / 2 - 2.1), y - depth / 2, z, 4.2, 0.90, eaves, "mortar"
        )
        m.box(
            x + side * (width / 2 - 2.1),
            y - depth / 2 - 0.53,
            z + 3.2,
            4.2,
            0.19,
            0.75,
            "ochre",
        )
    for fy in (y - depth / 2 - 0.25, y, y + depth / 2):
        m.beam(
            (x - width * 0.49, fy, z + eaves),
            (x + width * 0.49, fy, z + eaves),
            0.30,
            0.40,
            "steel",
        )
        for i in range(faces):
            ax, az = arch(i * math.pi / faces)
            bx, bz = arch((i + 1) * math.pi / faces)
            m.beam((ax, fy, az), (bx, fy, bz), 0.28, 0.32, "steel")
            if 0 < i < faces - 1 and i % 2:
                m.beam((ax, fy, az), (bx, fy, z + eaves), 0.17, 0.21, "steel")
    m.box(x, y - depth / 2 - 0.4, z + eaves + 1.0, width * 0.31, 0.35, 1.9, "rust")
    m.box(x, y - depth / 2 - 0.7, z + eaves + 1.72, width * 0.20, 0.28, 0.35, "chalk")
    if yaw:
        c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        for part in m.groups.values():
            if part.get("assembly") == name:
                part["vertices"] = [(x + (px - x) * c - (py - y) * s,
                                     y + (px - x) * s + (py - y) * c, pz)
                                    for px, py, pz in part["vertices"]]
    m.zone = previous_zone


def _tower(m, x, y, h=18.0):
    z = float(elevation(x, y)) - 0.45
    for dx in (-2.35, 2.35):
        for dy in (-2.35, 2.35):
            m.box(x + dx, y + dy, z, 1.65, 1.65, 0.9, "mortar")
            m.beam(
                (x + dx, y + dy, z),
                (x + dx * 0.76, y + dy * 0.76, z + h),
                0.41,
                0.48,
                "steel",
            )
    for s in (-1.0, 1.0):
        for k in range(2):
            lo = z + 1 + k * (h - 1) / 2
            hi = lo + (h - 1) / 2
            m.beam(
                (x - 2, y + s * 2, lo), (x + 2, y + s * 2, hi), 0.25, 0.26, "galvanized"
            )
            m.beam(
                (x + 2, y + s * 2, lo), (x - 2, y + s * 2, hi), 0.25, 0.26, "galvanized"
            )
            m.beam(
                (x + 2 * s, y - 2, lo), (x + 2 * s, y + 2, hi), 0.25, 0.26, "galvanized"
            )
    m.box(x, y, z + h, 6.2, 6.2, 1.4, "plinth")
    m.box(x, y, z + h + 1.4, 5.1, 5.1, 1.4, "sage_light")
    m.box(x, y, z + h + 2.8, 5.1, 5.1, 1.55, "glass")
    for dx in (-2.52, 2.52):
        for dy in (-2.52, 2.52):
            m.box(x + dx, y + dy, z + h + 2.7, 0.22, 0.22, 1.8, "steel")
    m.box(x, y, z + h + 4.35, 6.4, 6.4, 0.52, "rust")
    m.beam((x + 1, y, z + h + 4.8), (x + 1, y, z + h + 8.1), 0.15, 0.16, "steel")
    m.box(x + 1, y, z + h + 8.05, 0.45, 0.45, 0.5, "lamp")


def _barracks(m, x, y):
    z = float(elevation(x, y)) - 0.35
    m.box(x, y, z, 24, 10, 1.1, "plinth")
    m.box(x, y, z + 1.0, 23.5, 9.5, 5.3, "mortar")
    m.box(x, y, z + 6.3, 25, 11, 0.65, "rust")
    for side in (-1, 1):
        for wx in (-8, -4, 0, 4, 8):
            m.box(x + wx, y + side * 4.78, z + 2.8, 2.3, 0.2, 1.95, "glass")
            m.box(x + wx, y + side * 4.87, z + 2.65, 2.65, 0.27, 0.16, "chalk")
    m.box(x + 6, y, z + 7.0, 2.8, 2.4, 1.1, "steel")


def _container(m, x, y, mat):
    z = float(elevation(x, y)) - 0.26
    m.box(x, y, z, 7.8, 3.5, 3.6, mat)
    for offset in (-3.5, -2.3, -1.1, 0.1, 1.3, 2.5, 3.5):
        for side in (-1, 1):
            m.box(x + offset, y + side * 1.76, z + 0.1, 0.12, 0.11, 3.4, "galvanized")
    m.box(x, y, z + 3.6, 8.0, 3.7, 0.12, "dark")


def _service_pump(m, x, y):
    z = float(elevation(x, y)) - 0.16
    for side in (-1, 1):
        m.box(x + side * 6.0, y, z, 0.60, 0.60, 6.0, "steel")
    m.box(x, y, z + 6.0, 15.0, 7.0, 0.65, "chalk")
    m.box(x, y - 3.5, z + 6.05, 15.0, 0.24, 0.5, "rust")
    for side in (-1, 1):
        m.box(x + side * 3.7, y, z, 1.15, 0.9, 2.3, "sage_light")
        m.box(x + side * 3.7, y - 0.49, z + 1.2, 0.8, 0.14, 0.69, "dark")


def _command_post(m, x, y):
    z = float(elevation(x, y)) - 0.45
    m.box(x, y, z, 17.0, 14.0, 1.5, "plinth")
    m.box(x, y, z + 1.5, 16.0, 13.0, 15.0, "mortar")
    # Ground-floor entrance, cantilevered awning, side stair and rooftop rail.
    m.box(x - 4.0, y - 6.52, z + 0.85, 3.0, 0.19, 3.15, "dark")
    m.box(x - 4.0, y - 7.8, z + 3.94, 4.7, 2.7, 0.27, "rust")
    for i in range(3):
        m.box(
            x - 4.0, y - 8.45 + i * 0.56, z + 0.34, 4.1, 0.70, 0.23 * (i + 1), "plinth"
        )
    for side in (-1, 1):
        m.beam(
            (x - 7.6, y + side * 6.5, z + 18.0),
            (x + 7.6, y + side * 6.5, z + 18.0),
            0.14,
            0.18,
            "steel",
        )
    for f in range(3):
        at = z + 4.1 + f * 4.2
        m.box(x, y - 6.55, at, 12.8, 0.18, 1.65, "glass")
        m.box(x, y - 6.75, at - 0.23, 13.2, 0.55, 0.21, "chalk")
        for side in (-1, 1):
            m.box(x + side * 8.01, y, at, 0.20, 9.7, 1.65, "glass")
        for dx in (-4.5, -1.5, 1.5, 4.5):
            m.box(x + dx, y - 6.66, at, 0.21, 0.25, 1.65, "steel")
    m.box(x, y, z + 16.5, 18.2, 15.0, 1.15, "rust")
    m.box(x, y, z + 17.65, 13.8, 10.8, 0.7, "plinth")
    m.box(x, y, z + 18.35, 11.4, 8.6, 2.6, "glass")
    m.box(x, y, z + 20.95, 13.1, 10.0, 0.83, "sage_light")
    m.beam(
        (x + 3.0, y + 1.0, z + 21.0), (x + 3.0, y + 1.0, z + 35.0), 0.42, 0.45, "steel"
    )
    for dx in (-2.8, 2.8):
        m.beam(
            (x + 3.0 + dx, y + 1.0, z + 21.4),
            (x + 3.0, y + 1.0, z + 29.3),
            0.22,
            0.24,
            "galvanized",
        )
    for h, w in ((26.0, 4.7), (31.0, 3.5), (34.0, 2.0)):
        m.beam(
            (x + 3.0 - w, y + 1.0, z + h),
            (x + 3.0 + w, y + 1.0, z + h),
            0.16,
            0.16,
            "galvanized",
        )
    m.box(x + 3.0, y + 1.0, z + 35.0, 0.65, 0.65, 0.65, "lamp")


def _ramp(m, x, y, length, width, rise):
    z = float(elevation(x, y))

    # A smooth increasing slope; the first edge sits below terrain.
    def profile(i):
        u = i / 8
        return y + (u - 0.5) * length, z - 0.20 + rise * (0.34 * u + 0.66 * u * u)

    stations = [profile(i) for i in range(9)]
    for i, ((a, h0), (b, h1)) in enumerate(pairwise(stations)):
        xy = [
            (x - width / 2, a),
            (x + width / 2, a),
            (x + width / 2, b),
            (x - width / 2, b),
        ]
        m.solid(
            [(u, v, z - 0.8) for u, v in xy],
            [(u, v, h) for (u, v), h in zip(xy, (h0, h0, h1, h1))],
            "rust" if i % 4 == 2 else "mortar",
        )
        for sx in (x - width / 2 - 0.24, x + width / 2 + 0.24):
            m.beam((sx, a, h0 - 0.15), (sx, b, h1 - 0.15), 0.45, 0.38, "signal")
    for sx in (x - width / 2 - 0.3, x + width / 2 + 0.3):
        m.box(sx, y + length / 2, z, 1.25, 1.55, rise + 0.35, "chalk")


def _yard_light(m, x, y, heading, height=7.5):
    z = float(elevation(x, y)) - 0.16
    dx, dy = math.cos(heading), math.sin(heading)
    hx, hy = x + 1.5 * dx, y + 1.5 * dy
    m.box(x, y, z, 0.95, 0.95, 0.48, "plinth")
    m.column(x, y, z + 0.38, 0.12, 0.075, height - 0.38, 8, "galvanized")
    m.beam((x, y, z + height - 0.65), (hx, hy, z + height + 0.03),
           0.15, 0.16, "galvanized")
    m.box(hx, hy, z + height - 0.02, 0.80, 0.40, 0.23, "steel", heading)
    m.box(hx, hy, z + height - 0.08, 0.62, 0.32, 0.065, "lamp", heading)


def _ravine_bridge(m):
    m.zone = "ravine_bridge"
    x, y = BRIDGE_CENTER
    z = BRIDGE_DECK_LEVEL
    span, width = BRIDGE_HALF_SPAN, BRIDGE_HALF_WIDTH
    m.box(x, y, z - .55, 2. * span, 2. * width, .72, "plinth")
    for side in (-1, 1):
        end = x + side * (span - 7.)
        m.box(end, y, z - 4.9, 4., 2. * width + 1., 4.35, "mortar")
        verge = y + side * (width - .2)
        m.box(x, verge, z + .17, 2. * span - 2., .35, .26, "mortar")
        m.box(x, verge, z + .94, 2. * span - 2., .16, .17, "rust")
        for offset in range(-32, 33, 8):
            m.box(x + offset, verge, z + .39, .20, .22, .58, "steel")


def _quarry_loading_yard(m):
    m.zone = "quarry_yard"
    x, y = QUARRY_YARD
    z = float(elevation(x, y))
    for dx in (-1., 17.):
        for dy in (0., 11.):
            m.box(x + dx, y + dy, z - .2, .40, .40, 6.1 - dy * .09, "galvanized")
    roof = [(x - 3., y - 1.7, z + 5.95), (x + 19., y - 1.7, z + 5.95),
            (x + 19., y + 12.7, z + 4.66), (x - 3., y + 12.7, z + 4.66)]
    m.solid([(px, py, pz - .28) for px, py, pz in roof], roof, "sage_light")
    for dx, dy, radius, height in ((5.5, 4.8, 3.9, 2.4), (13., 6., 3.6, 1.8)):
        m.column(x + dx, y + dy, z - .10, radius, .45, height, 9, "shale", dx)
    for rail in (-.64, .64):
        m.box(x + 8., y - 10. + rail, z - .03, 25., .13, .15, "steel")
    for offset in range(-3, 20, 2):
        m.box(x + offset, y - 10., z - .10, .22, 2.3, .16, "bark")
    _yard_light(m, x + 4., y + 14., -math.pi / 2)


def _circuit_finish():
    route = np.asarray(_circuit())
    index = np.linalg.norm(route - (-350., -405.), axis=1).argmin()
    tangent = route[index + 1] - route[index - 1]
    return route[index], tangent / np.linalg.norm(tangent)


def _timing_gantry(m):
    center, tangent = _circuit_finish()
    across = np.array((-tangent[1], tangent[0]))
    angle = math.atan2(across[1], across[0])
    beam_height = float(elevation(*center)) + 9.4
    posts = (center - 12. * across, center + 12. * across)
    for x, y in posts:
        bottom = float(elevation(x, y)) - .5
        m.box(x, y, bottom, .55, .55, beam_height - bottom, "steel", angle)
    m.beam((*posts[0], beam_height), (*posts[1], beam_height), .90, 1.30, "rust")
    # Keep the painted plaque below the beam and center it on the lane.
    for across_offset in (-2.3, 2.3):
        x, y = center + across_offset * across
        m.box(x, y, beam_height - 1.10, .20, .28, .57, "galvanized", angle)
    m.box(*center, beam_height - 2.70, 6.4, .5, 1.7, "steel", angle)
    for side in (-1, 1):
        for row in range(2):
            for column in range(row, 8, 2):
                x, y = center + ((column - 3.5) * .78) * across + side * .252 * tangent
                m.box(x, y, beam_height - 2.38 + row * .53, .77, .05, .51, "chalk", angle)


def _airfield_service_yard(m):
    m.zone = "airfield_service"
    x, y = AIRFIELD_SERVICE
    z = float(elevation(x, y))
    m.box(x - 12., y - 3., z + .38, 3.2, 5.0, .82, "ochre")
    m.box(x - 12., y - 3.8, z + 1.20, 2.75, 1.85, 1.37, "galvanized")
    m.box(x - 12., y - 4.75, z + 1.65, 2.25, .16, .69, "glass")
    for side in (-1, 1):
        for axle in (-4.7, -1.7):
            m.box(x - 12. + side * 1.47, y + axle, z + .09, .63, .95, .89, "dark")
    m.beam((x - 12., y - .35, z + .63), (x - 12., y + .97, z + .63), .33, .23, "steel")
    cy = y + 6.8
    for cx in (x + 4.8, x + 13.2):
        m.box(cx, cy, z - .08, .75, 3.7, 1.06, "plinth")
    rings = [[(x + dx, cy + radius * math.cos(i * math.tau / 14),
               z + 2.37 + radius * math.sin(i * math.tau / 14)) for i in range(14)]
             for dx, radius in ((2., .66), (3.1, 1.56), (14.9, 1.56), (16., .66))]
    for rear, front in pairwise(rings):
        m.solid(rear, front, "sage_light")
    m.box(x + 13.2, cy, z + 3.88, 1.0, .95, .38, "galvanized")
    for dx in (-23., 24.):
        m.column(x + dx, y + 15., z - .1, .22, .14, 1.40, 8, "galvanized")
        m.column(x + dx, y + 15., z + 1.29, .29, .27, .24, 8, "signal")
    _yard_light(m, x + 24., y - 12., math.pi * .75, 9.)


def _arena_banner(m, x, y, material):
    z = float(elevation(x, y)) - .35
    for side in (-1, 1):
        m.column(x + side * 2.8, y, z, .16, .11, 10.2, 8, "steel")
    m.beam((x - 2.8, y, z + 9.9), (x + 2.8, y, z + 9.9), .19, .19, "steel")
    m.box(x, y, z + 5.3, 5.2, .14, 4.6, material)
    for side in (-1, 1):
        m.box(x, y + side * .10, z + 7.0, 3.2, .055, .28, "chalk")
        m.box(x, y + side * .10, z + 7.62, 4.25, .055, .28, "chalk")


def _parked_trainer(m, x, y):
    """A small, unarmed prop aircraft parked with its nose toward the runway."""
    z = float(elevation(x, y))
    sections = ((-5.4, 0.14, 1.35, 0.28), (-2.0, 0.66, 1.42, 0.54),
                (1.8, 0.78, 1.55, 0.63), (4.25, 0.55, 1.65, 0.50),
                (4.8, 0.30, 1.67, 0.31))
    hoops = [[(x + w * math.sin(i * math.tau / 8), y + dy,
               z + center + h * math.cos(i * math.tau / 8)) for i in range(8)]
             for dy, w, center, h in sections]
    for index, (rear, front) in enumerate(pairwise(hoops)):
        m.solid(rear, front, "ochre" if index == 3 else "sage_light")
    for side in (-1, 1):
        for span, root_y, tip_y, chord, height in ((5.6, 1.0, .2, 2.65, 1.48),
                                                 (2.05, -4.12, -4.5, 1.14, 1.82)):
            root_x, tip_x = x + side * .39, x + side * span
            outline = [(root_x, y + root_y - chord * .54, z + height),
                       (tip_x, y + tip_y - chord * .28, z + height + .35),
                       (tip_x, y + tip_y + chord * .21, z + height + .35),
                       (root_x, y + root_y + chord * .46, z + height)]
            if side < 0:
                outline.reverse()
            m.solid([(px, py, pz - .14) for px, py, pz in outline], outline, "sage_light")
        m.beam((x + side * .49, y + .72, z + 1.38),
               (x + side * 1.2, y + .71, z + .37), .16, .16, "galvanized")
        m.beam((x + side * 1.32, y + .71, z + .38),
               (x + side * 1.08, y + .71, z + .38), .74, .74, "dark")
    m.box(x, y - 4.6, z + .08, .22, .28, 1.13, "galvanized")
    m.beam((x, y - 4.5, z + 1.55), (x, y - 5.0, z + 3.2), .18, .95, "ochre")
    m.beam((x, y - 1.1, z + 2.03), (x, y + .88, z + 2.19), .86, .58, "glass")
    m.beam((x - 1.14, y + 4.83, z + 1.34),
           (x + 1.14, y + 4.83, z + 2.0), .17, .10, "dark")


def _loop(m):
    x, y = LOOP_CENTER
    radius = LOOP_RADIUS
    z = float(elevation(x, y))
    width = 11.0

    # Two lanes side by side meet just above the tangent of a full circular loop.
    def section(t):
        yc = y + radius * math.sin(t)
        zc = z + radius * (1 - math.cos(t)) - 0.16
        xc = loop_east(t)
        vy = math.sin(t)
        vz = -math.cos(t)
        return [
            (xc - width / 2, yc + vy, zc + vz),
            (xc + width / 2, yc + vy, zc + vz),
            (xc + width / 2, yc, zc),
            (xc - width / 2, yc, zc),
        ]

    for i in range(32):
        a = i * math.tau / 32
        b = (i + 1) * math.tau / 32
        m.solid(section(b), section(a), "signal" if i % 8 < 2 else "steel")
    for sign in (-1, 1):
        sec = section(math.pi / 2 if sign == 1 else 3 * math.pi / 2)
        for edge in (0, 1):
            cx = sec[edge][0] + (-0.4 if edge == 0 else 0.4)
            m.beam(
                (cx + (-3.0 if edge == 0 else 3.0), y + sign * (radius + 7.0), z - 0.3),
                (cx, y + sign * radius, z + radius),
                0.7,
                0.8,
                "rust",
            )


def _flora(m, rng):
    """Illustration proxies use the exact same positions as the native SpeedTrees."""
    classes = np.asarray(_materials(1024))[::-1]

    def surface(x, y):
        return classes[
            min(1023, max(0, int(y + HALF))), min(1023, max(0, int(x + HALF)))
        ]

    for name, x, y, _ in vegetation(surface):
        if name not in TREES and name not in SHRUBS:
            continue
        m.zone = f"grove_{int((x + HALF) // 192)}_{int((y + HALF) // 192)}"
        z = float(elevation(x, y)) - 0.38
        tree = name in TREES
        h = rng.uniform(8.5, 15.0) if tree else rng.uniform(1.6, 3.1)
        r = h * (0.26 if tree else 0.72)
        if tree:
            m.column(x, y, z, 0.24, 0.13, h * 0.69, 6, "bark")
        for i in range(3):
            phi = 0.7 + i * math.tau / 3 + rng.uniform(-0.27, 0.27)
            dx = math.cos(phi) * r * 0.47
            dy = math.sin(phi) * r * 0.47
            base = z + (0.37 if tree else 0.11) * h + rng.uniform(0, 0.12) * h
            off = rng.uniform(0.70, 0.98) * r
            crown = "pine" if i % 2 else "needles"
            m.column(x + dx, y + dy, base, off * 0.47, off, h * 0.25, 7, crown, phi)
            m.column(
                x + dx,
                y + dy,
                base + h * 0.25,
                off,
                off * 0.28,
                h * 0.24,
                7,
                crown,
                phi,
            )


def _settlement_preview(m):
    # Roof silhouettes for the overview and minimap. The playable scene uses
    # the local native houses at these same positions.
    m.zone = "settlement_preview"
    for i, (_, x, y, w, d, height) in enumerate(SETTLEMENT_HOUSES):
        z = float(elevation(x, y))
        eaves = z + height * .68
        m.box(x, y, z, 2. * w - .7, 2. * d - .6, eaves - z, "mortar")
        vertices = [(x - w, y - d, eaves), (x + w, y - d, eaves),
                    (x + w, y + d, eaves), (x - w, y + d, eaves),
                    (x - w, y, z + height), (x + w, y, z + height)]
        m._add("shale" if i % 2 else "rust", vertices,
               ((0, 1, 5), (0, 5, 4), (4, 5, 2), (4, 2, 3), (0, 4, 3), (1, 2, 5)))


def geometry(*, illustrate_trees=False):
    """Render/collision meshes. Subdivision by zone and material limits draw calls."""
    m = _Mesh()
    rng = random.Random(74103)
    for number, hx in enumerate((-78.0, -26.0, 30.0), start=1):
        _hangar(m, f"Hangar_{number:02}", hx, 77.0, 34.0, 40.0)
    for hx in (-83.0, -35.0, 22.0, 69.0):
        _barracks(m, hx, -74.0)
    _command_post(m, 112.0, 56.0)
    _tower(m, 83.0, 61.0, 27.0)
    _tower(m, -117.0, -34.0, 16.0)
    for x in (67.0, 82.0, 97.0):
        m.column(x, 87.0, 31.8, 5.0, 5.0, 11.0, 12, "sage_light")
        m.column(x, 87.0, 42.8, 5.0, 3.9, 1.24, 12, "galvanized")
        m.column(x, 87.0, 43.95, 1.15, 0.95, 0.55, 8, "dark")
    for x, y, n in (
        (-103, -61, 2),
        (-101, 80, 2),
        (-56, 102, 3),
        (42, -88, 2),
        (86, -64, 2),
    ):
        for i in range(n):
            _container(m, x + i * 8.8, y, "sage" if i % 2 else "rust")
    for x, y in ((-91, -49), (69, -50)):
        _service_pump(m, x, y)
    for x, y, heading in ((-107, 39, 0.), (-61, 55, -math.pi / 2),
                          (-8, 55, -math.pi / 2), (53, 40, math.pi),
                          (-111, -43, 0.), (95, -45, math.pi)):
        _yard_light(m, x, y, heading)
    # Painted steel pennant on a mast. Off the spawn apron.
    m.column(75.0, 42.0, 31.6, 0.36, 0.17, 23.0, 8, "galvanized")

    # Folded sheet pennant with twin white chevrons on both sides.
    def pennant(x, band):
        t = (x - 75.0) / 9.0
        return (
            x,
            42.0 + 0.47 * math.sin(7.0 * t),
            54.15 - 0.64 * t + band * (1.0 - 0.33 * t),
        )

    for i in range(8):
        a, b = 75.1 + 9.0 * i / 8, 75.1 + 9.0 * (i + 1) / 8
        panel = [pennant(a, -3.0), pennant(b, -3.0), pennant(b, 0.0), pennant(a, 0.0)]
        m.solid([(px, py + 0.06, pz) for px, py, pz in panel], panel, "signal")
    for face in (-0.07, 0.12):
        for start in (76.0, 77.6):
            ends = [
                pennant(start, -0.65),
                pennant(start + 1.05, -1.5),
                pennant(start, -2.35),
            ]
            for a, b in pairwise(ends):
                m.beam(
                    (a[0], a[1] + face, a[2]),
                    (b[0], b[1] + face, b[2]),
                    0.12,
                    0.27,
                    "chalk",
                )

    m.zone = "range"
    _hangar(m, "Hangar_04", 252.0, -59.0, 25.0, 19.0, yaw=-90.)
    _tower(m, 224.0, 61.0, 13.0)
    for y in (-72., 70.):
        _yard_light(m, 278., y, -math.copysign(math.pi / 2, y))
    for i, y in enumerate((-54.0, -25.0, 3.0, 31.0, 59.0)):
        x = 415.0 + abs(i - 2) * 1.75
        z = float(elevation(x, y))
        for dy in (-2.8, 2.8):
            m.box(x + .5, y + dy, z - 0.5, 0.65, 0.7, 6.0, "steel")
        m.box(x, y, z + 3.3, 0.65, 8.0, 5.9, "chalk")
        # Recess-free facing rings oriented in the YZ plane toward the firing line.
        for ring, mat, offset in (
            (2.55, "rust", -0.40),
            (1.83, "chalk", -0.48),
            (0.92, "signal", -0.55),
        ):
            base = [
                (
                    x + offset,
                    y + ring * math.cos(k * math.tau / 12),
                    z + 6.3 + ring * math.sin(k * math.tau / 12),
                )
                for k in range(12)
            ]
            m.solid(base, [(a + 0.09, b, c) for a, b, c in base], mat)

    m.zone = "ruins"
    for x, y, w in (
        (-391.0, -44.0, 24.0),
        (-357.0, 54.0, 17.0),
        (-359.0, -56.0, 23.0),
        (-273.0, -61.0, 18.0),
        (-245.0, 57.0, 20.0),
    ):
        z = float(elevation(x, y)) - 0.4
        m.box(x, y, z, w, 1.7, 2.8, "mortar")
        for i in range(4):
            dx = x - w * 0.5 + (i + 0.25) * (w / 4)
            m.box(dx, y, z + 2.8, 1.2, 1.7, 5.0 + (2.4 if i % 3 else 0.0), "mortar")
            if i % 3 != 2:
                m.box(dx + w * 0.105, y, z + 7.0, w * 0.215, 1.65, 0.95, "plinth")
        m.box(x - w * 0.5, y + 5.5, z, 1.7, 10.6, 5.6, "mortar")
        for i in range(4):
            m.box(
                x + rng.uniform(-w * 0.4, w * 0.4),
                y + rng.uniform(3.0, 7.0),
                z + 0.28,
                rng.uniform(1.6, 3.2),
                1.0,
                rng.uniform(0.6, 1.6),
                "rust",
                rng.uniform(-1.0, 1.0),
            )

    m.zone = "ramps"
    for x, length, width, rise in STUNT_RAMPS:
        _ramp(m, x, 258.0, length, width, rise)
    _loop(m)
    _tower(m, 391.0, 209.0, 12.0)

    m.zone = "quarry"
    for x, y in (
        (-372.0, 329.0),
        (-425.0, 245.0),
        (-334.0, 342.0),
        (-267.0, 322.0),
        (-392.0, 273.0),
    ):
        z = float(elevation(x, y)) - 0.8
        for i in range(4):
            px = x + rng.uniform(-5, 5)
            py = y + rng.uniform(-4, 4)
            base = float(elevation(px, py)) - 0.65
            rad = rng.uniform(1.8, 3.7)
            m.column(
                px, py, base, rad, rad * 0.43, rng.uniform(1.8, 4.3), 7, "shale", x + i
            )
    _barracks(m, *QUARRY_OFFICE)
    _quarry_loading_yard(m)

    m.zone = "airfield"
    for number, (x, y) in enumerate(AIRFIELD_HANGARS, start=5):
        _hangar(m, f"Hangar_{number:02}", x, y, 37.0, 37.0, yaw=180.)
    _parked_trainer(m, 119., 371.)
    _parked_trainer(m, 191., 375.)
    for x, y in ((67., 371.), (170., 380.), (204., 371.)):
        _yard_light(m, x, y, math.pi / 2, 9.)
    _airfield_service_yard(m)
    m.zone = "airfield"
    _tower(m, -164.0, 365.0, 23.0)
    for x in (
        -385.0,
        -311.0,
        -235.0,
        -156.0,
        -78.0,
        0.0,
        78.0,
        156.0,
        234.0,
        310.0,
        385.0,
    ):
        for y in (396.5, 443.5):
            z = float(elevation(x, y))
            m.box(x, y, z - 0.18, 0.9, 0.9, 0.8, "ochre")
            m.box(x, y, z + 0.61, 0.55, 0.55, 0.24, "lamp")

    m.zone = "arena"
    # Four gates through angled concrete barriers; teams have unobstructed spawn ranks.
    for i in range(48):
        if i % 12 in (0, 1, 11) or i in (6, 7, 8, 17, 18, 19):
            continue
        a = i * math.tau / 48
        x = 300.0 + 124 * math.cos(a)
        y = -295.0 + 124 * math.sin(a)
        z = float(elevation(x, y)) - 0.35
        m.box(x, y, z, 14.2, 3.8, 3.0, "plinth", a + math.pi / 2)
        m.box(x, y, z + 3.0, 14.0, 2.5, 0.40, "chalk", a + math.pi / 2)
        if i % 3 == 0:
            m.box(x, y, z + 3.4, 1.1, 2.2, 3.1, "rust", a + math.pi / 2)
    for side in (-1, 1):
        for dx, dy, angle in (
            (-54, 35, 0.41),
            (36, 19, -0.37),
            (-14, 7, 1.02),
            (80, 14, 0.68),
        ):
            x = 300 + dx * side
            y = -295 + dy * side
            m.box(x, y, 31.7, 16.0, 5.8, 4.3, "mortar", angle)
            m.box(x, y, 36.0, 15.45, 5.2, 0.30, "ochre", angle)
    for x, y in ((189., -357.), (411., -357.), (189., -233.), (411., -233.)):
        _yard_light(m, x, y, math.atan2(-295. - y, 300. - x), 9.)
    _arena_banner(m, 323., -178., "signal")
    _arena_banner(m, 277., -412., "teal")

    _ravine_bridge(m)

    m.zone = "circuit"
    _tower(m, -267.0, -260.0, 11.0)
    _barracks(m, -316.0, -283.0)
    m.zone = "circuit_finish"
    _timing_gantry(m)
    m.zone = "circuit"
    for x, y in ((-175, -252), (-427, -347), (-283, -203), (-391, -396)):
        z = float(elevation(x, y))
        m.box(x, y, z - 0.3, 1.0, 1.0, 3.0, "steel")
        m.box(x, y, z + 2.7, 3.0, 0.45, 0.9, "ochre")
    for along in (-13.0, 13.0):
        for across in (-10.3, 10.3):
            x, y = _ford_point(along, across)
            z = float(elevation(x, y)) - 0.12
            m.column(x, y, z, 0.18, 0.18, 1.40, 6, "chalk")
            m.column(x, y, z + 1.40, 0.18, 0.18, 0.34, 6, "signal")
    if illustrate_trees:
        _settlement_preview(m)
        _flora(m, rng)
    vertices = [(FORD_CENTER[0], FORD_CENTER[1], FORD_WATER_LEVEL)]
    for i in range(64):
        phase = i * math.tau / 64
        x, y = _ford_point(14.0 * 1.24 * math.cos(phase), 33.0 * 1.24 * math.sin(phase))
        vertices.append((round(x, 3), round(y, 3), FORD_WATER_LEVEL))
    # Extend a little into the banks: the terrain, not a polygon edge, makes
    # the shoreline; the sprite uses the same terrain-and-water intersection.
    water = {
        "name": "sb_circuit_ford_water",
        "water": True,
        "solid": False,
        "color": [0.26, 0.46, 0.45, 1.0],
        "vertices": vertices,
        "triangles": [(0, 1 + i, 1 + (i + 1) % 64) for i in range(64)],
    }
    return {
        "format": "blitz-physics-geometry/v1",
        "axis": "dava_z_up",
        "objects": [*m.groups.values(), water],
    }


def _noise(x, y, spacing, seed):
    """Smooth seeded lattice noise in world space, invariant under image resizing."""
    u = x / spacing
    v = y / spacing
    ix = np.floor(u).astype(np.int64)
    iy = np.floor(v).astype(np.int64)
    tx = _smooth(u - ix)
    ty = _smooth(v - iy)

    def lattice(i, j):
        h = ((i * 73856093 + j * 19349663 + seed * 83492791) & 0xFFFFFFFF).astype(
            np.uint64
        )
        h = ((h ^ (h >> 16)) * np.uint64(2246822519)) & 0xFFFFFFFF
        h = (h ^ (h >> 13)) & 0xFFFF
        return h / 32767.5 - 1.0

    a = lattice(ix, iy)
    b = lattice(ix + 1, iy)
    c = lattice(ix, iy + 1)
    d = lattice(ix + 1, iy + 1)
    return (a + (b - a) * tx) * (1.0 - ty) + (c + (d - c) * tx) * ty


def _track_markings(canvas, ink):
    points = np.asarray(_main_track())
    distance = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
    # Fit whole dashes around the loop so there is no partial mark at the seam.
    count = round(distance[-1] / 15.0)
    spacing = distance[-1] / count
    for i in range(count):
        start, end = (i + 0.29) * spacing, (i + 0.71) * spacing
        distances = np.r_[start, distance[(distance > start) & (distance < end)], end]
        x = np.interp(distances, distance, points[:, 0])
        y = np.interp(distances, distance, points[:, 1])
        canvas.line(zip(x, y), 0.72, ink)


def _course_markings(c, white, yellow, wash):
    for y in (402.0, 438.0):
        c.line(((-391, y), (391, y)), 0.55, white)
    for x in range(-351, 352, 18):
        c.line(((x, 420.0), (x + 8, 420.0)), 0.85, white)
    for sign in (-1.0, 1.0):
        for y in (405.0, 408.0, 411.0, 414.0, 426.0, 429.0, 432.0, 435.0):
            c.line(((sign * 388, y), (sign * 373, y)), 1.5, white)
        for x in (251.0, 285.0):
            for y in (407.0, 433.0):
                c.line(((sign * x, y), (sign * (x + 7), y)), 1.45, white)
    # Seven-segment runway identifiers, oriented for a driver on approach.
    bars = (
        ((0, 1), (4, 1)),
        ((4, 1), (4, 4)),
        ((4, 4), (4, 7)),
        ((4, 7), (0, 7)),
        ((0, 7), (0, 4)),
        ((0, 4), (0, 1)),
        ((0, 4), (4, 4)),
    )
    for sign, digits in ((-1, ("abcdef", "abcdfg")), (1, ("abdeg", "abc"))):
        for di, digit in enumerate(digits):
            for segment, ends in zip("abcdefg", bars):
                if segment in digit:
                    c.line(
                        [
                            (
                                sign * (359.0 + (v - 4.0) * 1.55),
                                420.0 + sign * (u - 2.0 + (di - 0.5) * 7),
                            )
                            for u, v in ends
                        ],
                        0.92,
                        white,
                    )
    _track_markings(c, wash)
    for wx in (-101, -80, -59, -38, -17, 4, 25, 46, 67):
        c.line(((wx, -47.0), (wx, -39.0), (wx + 7.0, -39.0)), 0.65, wash)
    for wx in (-115, -97, -79, -61, -43, -25, -7, 11, 29, 47, 65, 83, 101):
        for sy in (-1.0, 1.0):
            c.line(((wx, sy * 53.0), (wx + 4.0, sy * 53.0)), 0.8, yellow)
    c.ring(0, 0, 47, 36, white, 0.70)
    c.ring(0, 0, 44, 33, wash, 0.55)
    c.line(((-12, -5), (0, 5), (12, -5)), 1.05, yellow)
    c.line(((-12, 0), (0, 10), (12, 0)), 1.05, yellow)
    for hx in (-78, -26, 30):
        for side in (-1, 1):
            c.line(
                (
                    (hx + side * 15.0, 55.0),
                    (hx + side * 15.0, 41.0),
                    (hx + side * 9.0, 41.0),
                ),
                0.65,
                yellow,
            )
    # Equipment bays, aircraft parking and a taxi guide connect the apron
    # to the runway. Recess markings leave the hangar doors unobstructed.
    for x, y in ((119.,371.), (191.,375.)):
        for edge in (-7., 7.):
            c.line(((x + edge, y - 7.2), (x + edge, y + 5.4)), .47, yellow)
        c.line(((x - 7., y - 7.2), (x + 7., y - 7.2)), .47, yellow)
    c.line(((75., 393.), (147., 393.), (160., 398.), (160., 413.)), .55, yellow)
    ax, ay = AIRFIELD_SERVICE
    for sign in (-1., 1.):
        c.line(((ax - 20., ay + sign * 11.), (ax + 19., ay + sign * 11.)), .50, yellow)
        c.line(((ax + sign * 21., ay - 9.), (ax + sign * 21., ay + 10.)), .50, yellow)
    for x in (-81., 59.):
        for i in range(4):
            u = x + 1.6 * i
            c.line(((u, -50.1), (u + 1.6, -47.9)), .55, yellow)
    for y in (-52, -27, -2, 23, 48):
        c.line(((236.0, y), (248.0, y)), 0.65, yellow)
    for x in (250.0, 278.0, 307.0, 335.0, 392.0):
        for y in (-66.0, 65.0):
            c.line(((x, y), (x, y + 3.0)), 0.8, white)
    for y in (-52., -27., -2., 23., 48.):
        for x in (251., 258., 265., 272., 279.):
            c.line(((x, y - 3.4), (x + 3.1, y - 3.4)), .36, wash)
    for side in (-1, 1):
        c.line(((-365., -62. + side * 1.3), (-340., -52. + side * 1.3),
                (-310., -28. + side * 1.3), (-298., -12. + side * 1.3)),
               .64, (100, 91, 72, 115))
        c.line(((-385. + side * .69, 368.), (-381. + side * .69, 359.),
                (-368. + side * .69, 351.), (-350. + side * .69, 350.)),
               .48, (92, 79, 64, 168))
    c.ring(300, -295, 111, 111, white, 0.85)
    for y in (-370.0, -220.0):
        c.line(((266.0, y), (334.0, y)), 0.8, white)
        c.line(((290.0, y), (300.0, y + math.copysign(8., -295. - y)), (310.0, y)),
               0.68, yellow)
    for x in (LOW_RAMP_X, *(ramp[0] for ramp in STUNT_RAMPS)):
        c.line(((x - 5, 225), (x, 231), (x + 5, 225)), 1.25, yellow)
    entry = loop_east(0.)
    for y in (224., 236., 248., 260.):
        c.line(((entry - 3., y - 2.), (entry, y + 2.), (entry + 3., y - 2.)), .7, yellow)
    for side in (-1, 1):
        c.line(((entry + side * 3.5, 220.), (entry + side * 3.5, 269.)), .43, wash)
    for side in (-1, 1):
        edge = LOW_RAMP_X + side * (LOW_RAMP_WIDTH / 2 - 0.55)
        c.line(((edge, LOW_RAMP_START - 3), (edge, LOW_RAMP_END + 3)), 0.55, white)
        for y in (LOW_RAMP_START, LOW_RAMP_CREST - 1.0, LOW_RAMP_END):
            for stripe in range(3):
                start = edge - side * stripe * 1.0
                c.line(((start, y), (start - side, y + 1.0)), 0.42, yellow)


def terrain_color(size=1024):
    """North-up ground albedo. Paint moves with the heightfield in the editor."""
    weights = np.asarray(terrain_mask(size, blended=True))
    base = np.array(
        ((85, 86, 81), (106, 115, 80), (119, 116, 103), (144, 132, 105)),
        dtype=np.float32,
    )
    result = np.empty((size, size, 3), dtype=np.uint8)
    xs = (np.arange(size, dtype=np.float32)[None, :] + 0.5) * (1024.0 / size) - HALF
    for j in range(0, size, 96):
        ys = HALF - (
            np.arange(j, min(size, j + 96), dtype=np.float32)[:, None] + 0.5
        ) * (1024.0 / size)
        mix = weights[j : j + 96].astype(np.float32) / 255.0
        broad = _noise(xs, ys, 48.0, 17)
        patches = _noise(xs, ys, 10.0, 5)
        grit = _noise(xs, ys, 1.65, 29)
        grit2 = _noise(xs, ys, 0.73, 9)
        delta = broad * 7.0 + patches * 5.0 + grit * 4.0 + grit2 * 2.0
        grass = mix[..., 1]
        stone = mix[..., 2]
        delta += grass * (patches * 6.0 + grit * 2.0) + stone * (
            6.0 * np.sin(0.37 * xs + 0.42 * ys + patches * 2.0) + grit * 4.0
        )
        colors = mix @ base + delta[..., None]
        colors[:, :, 0] += grass * (4.0 * broad - 3.0 * patches)
        colors[:, :, 2] -= grass * (3.0 * broad + 3.0 * patches)
        result[j : j + 96] = np.clip(colors, 0, 255).astype(np.uint8)
    paint = Image.new("RGBA", (2 * size, 2 * size))
    _course_markings(_Canvas(paint), (218, 219, 204, 255), (211, 169, 88, 255), (168, 171, 154, 255))
    paint = paint.resize((size, size), Image.Resampling.LANCZOS)
    return Image.alpha_composite(Image.fromarray(result).convert("RGBA"), paint).convert("RGB")


def minimap(size=512):
    """North-up overhead relief and roof outlines from the same solid meshes."""
    axis = (np.arange(size) + 0.5) * 1024.0 / size - HALF
    heights = elevation(axis[None, :], -axis[:, None])
    dz, dx = np.gradient(heights, 1024.0 / size)
    shade = np.clip(1.0 + 0.63 * dz - 0.58 * dx, 0.65, 1.20)
    color = np.asarray(terrain_color(size), dtype=float) * shade[:, :, None]
    ford_radius, _ = _ford_radius(axis[None, :], -axis[:, None])
    wet = (ford_radius <= 1.24) & (heights < FORD_WATER_LEVEL)
    color[wet] = 0.38 * color[wet] + 0.62 * np.array([64.0, 112.0, 123.0])
    image = Image.fromarray(np.clip(color, 0, 255).astype(np.uint8))
    c = _Canvas(image)
    surfaces = []
    for mesh in geometry(illustrate_trees=True)["objects"]:
        if mesh.get("water"):
            continue
        v = np.asarray(mesh["vertices"])
        color = np.asarray(mesh["color"][:3]) * 235.0
        for tri in mesh["triangles"]:
            pts = v[tri]
            a, b = pts[1] - pts[0], pts[2] - pts[0]
            normal = np.cross(a, b)
            if normal[2] <= 0.04:
                continue
            normal /= np.linalg.norm(normal)
            lighting = 0.65 + 0.25 * normal[2] - 0.16 * normal[0] + 0.13 * normal[1]
            rgb = tuple(np.clip(color * lighting, 0, 255).astype(int))
            surfaces.append((pts[:, 2].mean(), [c.point(p) for p in pts], rgb))
    surfaces.sort(key=lambda item: item[0])
    for _, polygon, ink in surfaces:
        c.draw.polygon(polygon, fill=ink)
    x, y = SETTLEMENT_WINDMILL
    c.draw.polygon([c.point((x + dx, y + dy))
                    for dx, dy in ((-5., -4.), (4., -4.), (4., 5.), (-5., 5.))],
                   fill=(102, 83, 59))
    return image
