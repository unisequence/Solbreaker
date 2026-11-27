"""Map environment geometry from local render metadata."""
from math import dist, floor

_CANE_NODE = 'env_rd_cane.sc2 State 0'
_CANE_ALBEDO = '../00_global_content/environmet/images/foliage.tex'


def permeable_water_plant(node, kind, material_id, materials):
    """The Rudniki cane cards are passable; fences and trunks stay physical.

    Cane is authored as a 5-HP destructible. This local rule omits its solid
    mesh entirely, so touching water plants cannot lift or block a tank.
    Its visual destruction effect is consequently not sent by the server.
    """
    if kind != 4 or node.get('name') != _CANE_NODE:
        return False
    return any(material.get('textures', {}).get('albedo') == _CANE_ALBEDO
               for material in material_chain(materials, material_id))


def material_chain(materials, ident):
    """Walk inherited render metadata, including incomplete material tables."""
    seen = set()
    while ident not in seen and ident in materials:
        seen.add(ident)
        material = materials[ident]
        yield material
        ident = material.get('parentMaterialKey')


def foliage_material(chain):
    return any(
        (str(mat.get('fxName', '')).lower().startswith('~res:/materials/speedtree')
         and 'leaf' in str(mat.get('materialName', '')).lower())
        or mat.get('textures', {}).get('albedo') in (_CANE_ALBEDO, 'grass/images/vino_bush.tex')
        for mat in chain)


def water_material(chain):
    return any(str(mat.get('fxName', '')).lower().startswith('~res:/materials/water')
               for mat in chain)


class MapEnvironment:
    """Spatial lookups for water and foliage, separate from solid physics."""
    CELL = 32.
    FOLIAGE_REACH = 30.
    FOLIAGE_BONUS = .35

    def __init__(self, water=(), foliage=()):
        self.water, self.foliage = {}, {}
        for triangle in water:
            a, b, c = triangle
            bx, bz = b[0] - a[0], b[2] - a[2]
            cx, cz = c[0] - a[0], c[2] - a[2]
            determinant = bx*cz - bz*cx
            if abs(determinant) <= 1e-9:
                continue
            row = (a, b, c, bx, bz, cx, cz, 1./determinant)
            self._insert(self.water, row, (min(a[0], b[0], c[0]), min(a[2], b[2], c[2])),
                         (max(a[0], b[0], c[0]), max(a[2], b[2], c[2])))
        for low, high, key in foliage:
            if high[1] - low[1] > .02 and max(high[0] - low[0], high[2] - low[2]) > .02:
                # Each canopy has thickness even when authored as crossed cards.
                low, high = tuple(v-.15 for v in low), tuple(v+.15 for v in high)
                self._insert(self.foliage, (low, high, key), (low[0], low[2]), (high[0], high[2]))

    def _insert(self, index, row, low, high):
        for x in range(floor(low[0]/self.CELL), floor(high[0]/self.CELL) + 1):
            for z in range(floor(low[1]/self.CELL), floor(high[1]/self.CELL) + 1):
                index.setdefault((x, z), []).append(row)

    def water_height(self, x, z):
        level = None
        cell = floor(x/self.CELL), floor(z/self.CELL)
        for a, b, c, bx, bz, cx, cz, inverse in self.water.get(cell, ()):
            dx, dz = x - a[0], z - a[2]
            u, v = (dx*cz-dz*cx)*inverse, (bx*dz-bz*dx)*inverse
            if min(u, v, 1. - u - v) >= -1e-8:
                height = a[1] + u*(b[1] - a[1]) + v*(c[1] - a[1])
                level = height if level is None else max(level, height)
        return level

    def foliage_bonus(self, start, end, destroyed, fired=False):
        """Bonus for foliage on this ray near the target, independent of other checkpoints."""
        if not self.foliage:
            return 0.
        length = dist(start, end)
        if length <= 1e-8:
            return 0.
        lo = max(0., 1.-self.FOLIAGE_REACH/length)
        delta = tuple(b-a for a, b in zip(start, end))
        near = tuple(a+lo*d for a, d in zip(start, delta))
        bonus = 0.
        for x in range(floor(min(near[0], end[0])/self.CELL),
                       floor(max(near[0], end[0])/self.CELL) + 1):
            for z in range(floor(min(near[2], end[2])/self.CELL),
                           floor(max(near[2], end[2])/self.CELL) + 1):
                for low, high, key in self.foliage.get((x, z), ()):
                    if key is not None and key in destroyed:
                        continue
                    first, last = lo, 1.
                    for a, d, mn, mx in zip(start, delta, low, high):
                        if abs(d) <= 1e-9:
                            if not mn <= a <= mx:
                                break
                            continue
                        t0, t1 = (mn - a)/d, (mx - a)/d
                        first = max(first, min(t0, t1))
                        last = min(last, max(t0, t1))
                        if first > last:
                            break
                    else:
                        # Nearby foliage loses most of its effect after firing.
                        if fired and last > 1.-15./length:
                            bonus = self.FOLIAGE_BONUS * .25
                        else:
                            return self.FOLIAGE_BONUS
        return bonus
