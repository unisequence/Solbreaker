"""Deterministic placement of the pinned client's native vegetation instances.

The scene builder copies assets from the player's local map. The preview uses
these same coordinates with original illustration geometry.
"""

import math
import random

TREES = ("tree_04.sc2", "tree_k1.sc2", "tree_k2.sc2")
SHRUBS = ("bush02_low_1.sc2", "vino_bush_lt.sc2")
FLOWERS = ("flowers01.sc2", "flowers02.sc2")
QUARRY_YARD = (-395., 193.)
SETTLEMENT_WINDMILL = (-390., 167.)
SETTLEMENT_HOUSES = (
    ("bld_pl_villagehouse_01.sc2", -262., 114., 8.2, 8.7, 7.8),
    ("bld_pl_villagehouse_02.sc2", -307., 112., 8., 7., 8.),
    ("bld_pl_villagehouse_03.sc2", -350., 114., 8., 7., 8.6),
    ("bld_rd_ruralHouse.sc2", -277., 155., 11., 10.4, 10.8),
    ("bld_pl_villagehouse_01.sc2", -330., 154., 8.2, 8.7, 7.8),
    ("bld_rd_barn708.sc2", -369., 152., 5.4, 6.2, 4.6),
)
_INTERIOR = (
    (-67, 124),
    (22, 142),
    (144, 102),
    (169, -49),
    (70, -140),
    (-70, -129),
    (-166, -54),
    (-158, 60),
    (-224, 98),
    (-321, 136),
    (-411, 160),
    (-82, 226),
    (50, 245),
    (147, 235),
    (330, 140),
    (152, -284),
    (68, -375),
    (-38, -355),
    (-137, -347),
    (-364, -144),
    (410, 156),
    (-230, -122),
    (-20, 305),
    (36, -200),
    (-202, 135),
    (-57, 182),
    (123, 174),
    (285, 154),
    (-165, -173),
    (109, -219),
    (-174, -282),
    (-108, -309),
    (12, -327),
    (101, -348),
    (122, -258),
    (294, -143),
    (373, -117),
    (126, -76),
    (-223, -119),
    (-269, 127),
    (-381, 125),
    (-165, 258),
)

# Linked belts between sectors with breaks at the access roads. Placing
# groves on a few continuous ridges gives each approach a clearer silhouette.
_WOODLINES = (
    (-379, 119), (-350, 126), (-294, 134), (-262, 150), (-245, 172),
    (-141, 145), (-104, 164), (-82, 184), (-39, 200), (3, 197),
    (31, 212), (63, 236), (106, 245),
    (218, 105), (253, 118), (295, 132), (348, 126), (387, 114),
    (-118, -268), (-94, -285), (-65, -303), (-34, -315), (-6, -340),
    (30, -353), (69, -331), (105, -315),
    (208, -149), (240, -145), (269, -142), (310, -136), (367, -133),
    (-389, 112), (-367, 175), (-351, 165), (-309, 169), (-267, 169),
    (-331, -356), (-300, -347), (-263, -330), (-203, -354),
    (-375, -426), (-328, -445), (-266, -421), (-197, -429),
    (-109, 218), (1, 259), (79, 265), (157, 139), (406, -159),
    (-161, -348), (-133, -362), (-107, -373), (-79, -389), (-56, -408),
    (-44, -369), (-59, -285), (-35, -263), (9, -269), (37, -287),
    (41, -378), (63, -397), (89, -393), (116, -364), (141, -346),
    (81, -291), (108, -264), (-418, 217), (-418, 151),
    # Infill the inner approaches and the long verges between test sectors.
    (-123, 150), (-92, 279), (-8, 280), (69, 279),
    (113, 141), (154, 63), (161, -106), (182, -112),
    (232, -121), (321, -124), (357, 125),
    (4, -406), (96, -411), (-123, -404),
)

# The rows are kept behind the hangar entrances, along the sides of the
# barracks and in the pockets of grass east and west of the command post.
_BUILDING_GROVES = (
    (77, 71), (110, 84), (119, 90), (131, 31),
    (-61, -77), (-66, -90), (-7, -77), (-8, -91), (82, -87),
    (-132, -69), (-127, 73), (-127, 89),
    (133, -25), (140, -52),
    (-288., 108.), (-329., 113.), (-355., 164.), (-297., 169.), (-253., 153.),
)
_NARROW_BEDS = ((92, 77), (129, 47), (45, -75), (-322., 149.), (-288., 115.),
                (-74., -316.), (43., -347.), (77., -308.), (-417., 174.))
_HANGAR_BEDS = ((-52, 68), (-52, 85), (-52, 98), (2, 68), (2, 85), (2, 98))
_VERGE_BEDS = ((-190., -184.), (-154., -194.), (-108., -211.), (-57., -219.),
               (23., -215.), (62., -208.), (101., -201.), (134., -163.),
               (-117., -296.), (-82., -310.), (-47., -344.), (32., -344.),
               (60., -312.), (94., -337.), (122., -315.), (-406., 167.))
_MEADOW_BEDS = (
    (-162., 129.), (-139., 141.), (-103., 157.), (-71., 173.), (-30., 156.),
    (42., 170.), (83., 153.), (107., 135.), (142., 116.), (161., 82.),
    (170., 51.), (181., 20.), (186., -30.), (174., -65.), (156., -113.),
    (182., -116.), (210., -122.), (246., -121.), (284., -127.),
    (-383., 171.), (-375., 163.), (-357., 151.), (-297., 158.), (-264., 151.),
    (-113., -390.), (-14., -378.), (58., -412.), (94., -409.),
)


def _protected(x, y):
    return (
        abs(x) < 133
        and -102 < y < 113
        or 189 < x < 448
        and -94 < y < 95
        or -424 < x < -215
        and -99 < y < 95
        or 181 < x < 419
        and 174 < y < 352
        or -413 < x < 413
        and 315 < y < 451
        or math.hypot(x - 300.0, y + 295.0) < 141.0
        or ((x + 309.0) / 145.0) ** 2 + ((y - 265.0) / 111.0) ** 2 < 1.0
        or -335 < x < -245
        and -312 < y < -244
    )


def _scattered_vegetation(surface):
    # Landscaped strips between aircraft sheds, beside the command building
    # and behind the barracks. These lie inside the protected facility block,
    # whose vehicle doors, service pumps and apron are intentionally open.
    beds = ((55,88), (95,63),
            (123,75), (-124,54), (-123,-56), (1,-88), (-55,-85), (109,-76))
    for i, (cx, cy) in enumerate(beds):
        if surface(cx,cy) != 1:
            continue
        yield TREES[i%len(TREES)], cx,cy,True
        for j, (dx,dy) in enumerate(((-2,3),(2,-3),(1,4),(-2,-2))):
            if surface(cx+dx,cy+dy) == 1:
                name = SHRUBS[(i+j)%len(SHRUBS)] if j < 2 else FLOWERS[(i+j)%len(FLOWERS)]
                yield name,cx+dx,cy+dy,False
    rng = random.Random(10731)
    sites = []
    # Irregular woodlines enclose the playable bowl. Keep deliberate breaks at
    # the runway ends and the mouths of the range and stunt hills.
    for i in range(28):
        a = math.tau * (i + 0.25) / 28.0
        ux, uy = math.cos(a), math.sin(a)
        radius = rng.uniform(459.0, 479.0) / max(abs(ux), abs(uy))
        sites.append((ux * radius, uy * radius, True))
    sites.extend((x, y, False) for x, y in _INTERIOR)
    sites.extend((x, y, False) for x, y in _WOODLINES)
    occupied = {}
    for cx, cy, edge in sites:
        main = TREES[rng.randrange(len(TREES))]
        orientation = rng.uniform(-math.pi, math.pi)
        co, si = math.cos(orientation), math.sin(orientation)
        places = rng.randrange(11, 16) if edge else rng.randrange(10, 15)
        for slot in range(places):
            tree = slot < (8 if edge else 4)
            shrub = not tree and slot < (11 if edge else 8)
            name = (
                (main if rng.random() < 0.68 else rng.choice(TREES))
                if tree
                else rng.choice(SHRUBS if shrub else FLOWERS)
            )
            # The widest native crown has a 6 m radial envelope. Eight sample
            # directions plus an inner ring keep it entirely off a 12 m road.
            clear = 9.0 if tree else (8.0 if shrub else 4.5)
            allowed = (1, 2) if edge else (1,)
            for _ in range(10):
                u, v = rng.gauss(0.0, 17.0 if edge else 13.0), rng.gauss(0.0, 10.0)
                x, y = cx + u * co - v * si, cy + u * si + v * co
                if (
                    max(abs(x), abs(y)) > 495.0
                    or _protected(x, y)
                    or surface(x, y) not in allowed
                ):
                    continue
                if any(
                    surface(
                        x + distance * math.cos(a * math.pi / 4),
                        y + distance * math.sin(a * math.pi / 4),
                    )
                    not in allowed
                    for a in range(8)
                    for distance in (clear * 0.5, clear)
                ):
                    continue
                key = (math.floor(x / 8.0), math.floor(y / 8.0))
                separation = 5.0 if tree else 2.8
                if any(
                    math.hypot(x - px, y - py) < separation
                    for gx in range(key[0] - 1, key[0] + 2)
                    for gy in range(key[1] - 1, key[1] + 2)
                    for px, py in occupied.get((gx, gy), ())
                ):
                    continue
                occupied.setdefault(key, []).append((x, y))
                yield name, x, y, tree
                break


def _room_for_plant(surface, occupied, x, y, margin):
    if surface(x, y) != 1:
        return False
    if any(math.hypot(x - px, y - py) < margin for px, py in occupied):
        return False
    return all(
        surface(x + margin * math.cos(a * math.pi / 4), y + margin * math.sin(a * math.pi / 4)) == 1
        for a in range(8)
    )


def vegetation(surface):
    """Yield (local asset name, east, north, destructible) for clear verges only."""
    occupied = []
    for plant in _scattered_vegetation(surface):
        _, x, y, _ = plant
        occupied.append((x, y))
        yield plant
    for i, (x, y) in enumerate(_BUILDING_GROVES):
        if _room_for_plant(surface, occupied, x, y, 5.0):
            occupied.append((x, y))
            yield TREES[i % len(TREES)], x, y, True
    for i, (x, y) in enumerate((*_BUILDING_GROVES, *_NARROW_BEDS)):
        direction = -1 if i % 2 else 1
        offsets = ((-2.8, 2.6 * direction), (2.9, -1.2 * direction), (1.4, 4.3 * direction))
        for j, (dx, dy) in enumerate(offsets):
            px, py = x + dx, y + dy
            if _room_for_plant(surface, occupied, px, py, 2.5):
                occupied.append((px, py))
                species = SHRUBS if j < 2 else FLOWERS
                yield species[i % len(species)], px, py, False


def ground_cover(surface, height):
    """Small noncolliding tufts, batched in local cells for the renderer."""
    rng = random.Random(85301)
    cells = {}
    field_sites = [(cx + rng.uniform(-12., 12.), cy + rng.uniform(-12., 12.), False, .61)
                   for cy in range(-403, 404, 27) for cx in range(-403, 404, 27)]
    field_sites.extend((x, y, False, .88) for x, y in _VERGE_BEDS)
    field_sites.extend((x, y, True, .61) for x, y in _HANGAR_BEDS)
    field_sites.extend((x, y, False, .83) for x, y in _MEADOW_BEDS)
    for x, y, bed, tip in field_sites:
        if surface(x, y) != 1 or (not bed and (_protected(x, y) or rng.random() < .20)):
            continue
        cell = (math.floor(x / 96), math.floor(y / 96))
        for _ in range(rng.randint(18, 36)):
            px = x + rng.gauss(0, 1.1 if bed else 6)
            py = y + rng.gauss(0, 2.0 if bed else 4.5)
            if (not bed and _protected(px, py)) or surface(px, py) != 1:
                continue
            base = height(px, py) - .035
            vertices, triangles = cells.setdefault(cell, ([], []))
            for i in range(4):
                angle = (i + rng.random()) * math.tau / 4
                dx, dy = math.cos(angle), math.sin(angle)
                spread, blade = rng.uniform(.17, .31), rng.uniform(.22, tip)
                width = rng.uniform(.035, .067)
                index = len(vertices)
                vertices.extend((
                    (px - dy * width, py + dx * width, base),
                    (px + dy * width, py - dx * width, base),
                    (px + dx * spread, py + dy * spread, base + blade),
                ))
                # Render both sides and keep the silhouettes readable at tank height.
                triangles.extend(((index, index + 1, index + 2), (index, index + 2, index + 1)))
    palette = ((.44, .51, .27, 1.), (.52, .57, .31, 1.), (.47, .52, .26, 1.))
    for (x, y), (vertices, triangles) in cells.items():
        yield dict(name=f'sb_grass_{x}_{y}', color=palette[(x + y) % len(palette)],
                   vertices=vertices, triangles=triangles, solid=False)
