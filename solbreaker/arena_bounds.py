"""Spatial rules used by the arena owner and its ground controller."""
from typing import NamedTuple
import math


class CircularZone(NamedTuple):
    x: float
    z: float
    radius: float

    def __call__(self, position):
        x, _, z = position
        return math.hypot(x - self.x, z - self.z) <= self.radius
