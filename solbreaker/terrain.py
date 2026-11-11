"""Height lookup from validated installed map terrain for the pinned build."""
import math
import struct
from pathlib import Path

from .dvpl import read
from .map_catalog import load_map_spec


class MapTerrain:
    """Heightmap/MKM sampler for one validated local map."""
    _MKM_MAGIC = b"kkm\0"
    _MKM_VERSION = 1

    def __init__(self, game: Path, map_spec=None):
        self.map_spec = map_spec or load_map_spec(game, "rudniki")
        x0, z0, y0, x1, z1, y1 = self.map_spec.landscape_bbox
        self.MIN = x0
        self.MIN_Z = z0
        self.WIDTH = x1 - x0
        self.DEPTH = z1 - z0
        self.HEIGHT_MIN = y0
        self.HEIGHT = y1 - y0
        if not math.isclose(self.WIDTH, self.DEPTH, rel_tol=0, abs_tol=1e-4):
            raise ValueError("Unsupported non-square terrain domain")
        raw = read(self.map_spec.heightmap_path)
        self.size, tile = struct.unpack_from("<II", raw)
        if (self.size < 2 or tile < 1 or self.size % tile
                or len(raw) != 8 + self.size ** 2 * 2):
            raise ValueError(f"Unexpected {self.map_spec.key} heightmap layout")
        self.heights = [0] * (self.size ** 2)
        offset = 8
        for ty in range(0, self.size, tile):
            for tx in range(0, self.size, tile):
                for y in range(tile):
                    at = (ty + y) * self.size + tx
                    self.heights[at:at + tile] = struct.unpack_from(f"<{tile}H", raw, offset)
                    offset += tile * 2
        # MapSettings/TerrainDataComponent identifies this companion. The
        # native MKM reader accepts four 2-bit codes per byte, low bits first.
        # Codes remain evidence-only; their drive-surface meaning is unproved.
        mkm = read(self.map_spec.mkm_path)
        if len(mkm) < 24:
            raise ValueError(f"Unexpected {self.map_spec.key} MKM layout")
        magic, version, size, payload_size, reserved0, reserved1 = struct.unpack_from("<4sIIIII", mkm)
        if (magic != self._MKM_MAGIC or version != self._MKM_VERSION
                or size < 1 or payload_size != size * size // 4
                or (reserved0, reserved1) != (0, 0) or len(mkm) != 24 + payload_size):
            raise ValueError(f"Unexpected {self.map_spec.key} MKM layout")
        self.surface_size = size
        self.surface_codes = mkm[24:]

    @property
    def MAX_X(self):
        return self.MIN + self.WIDTH

    @property
    def MAX_Z(self):
        return self.MIN_Z + self.DEPTH

    def height(self, x, z):
        if not (math.isfinite(x) and math.isfinite(z)):
            raise ValueError("Invalid terrain coordinate")
        fx = max(0.0, min(self.size - 1.0, (x - self.MIN) * self.size / self.WIDTH))
        fz = max(0.0, min(self.size - 1.0, (z - self.MIN_Z) * self.size / self.DEPTH))
        ix, iz = int(fx), int(fz)
        dx, dz = fx - ix, fz - iz

        def h(a, b):
            return self.heights[min(b, self.size - 1) * self.size + min(a, self.size - 1)]

        row0 = h(ix, iz) * (1 - dx) + h(ix + 1, iz) * dx
        row1 = h(ix, iz + 1) * (1 - dx) + h(ix + 1, iz + 1) * dx
        return self.HEIGHT_MIN + (row0 * (1 - dz) + row1 * dz) * self.HEIGHT / 65535

    def surface_code(self, x, z):
        """Return the pinned MKM contact code at x/z, or None outside this map.

        This is an evidence-only sampler. Values 0..3 are carried through so
        a calibration run can establish their physical meaning later.
        """
        if not (math.isfinite(x) and math.isfinite(z)):
            raise ValueError("Invalid terrain coordinate")
        if not (self.MIN <= x < self.MAX_X and self.MIN_Z <= z < self.MAX_Z):
            return None
        ix = int((x - self.MIN) * self.surface_size / self.WIDTH)
        iz = int((z - self.MIN_Z) * self.surface_size / self.DEPTH)
        index = iz * self.surface_size + ix
        packed = self.surface_codes[index >> 2]
        return (packed >> (2 * (index & 3))) & 3


# Existing call sites and focused tests use this class name. Its no-argument
# selection remains Mines; BaseApp uses MapTerrain with its configured spec.
MinesTerrain = MapTerrain
