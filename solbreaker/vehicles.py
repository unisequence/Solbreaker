"""Compatibility helpers for the original stock M2 inventory."""
from pathlib import Path

from .vehicle_data import load_vehicle


def stock_m2(game: Path) -> bytes:
    return load_vehicle(game).descriptor


def stock_ammo(game: Path) -> list[int]:
    return load_vehicle(game).inventory['shells']
