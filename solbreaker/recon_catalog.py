"""Read confirmed tank classes from the pinned national vehicle lists."""
from functools import lru_cache
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from .dvpl import read

CLASSES = frozenset(('lightTank', 'mediumTank', 'heavyTank', 'AT-SPG', 'SPG'))


@lru_cache(maxsize=16)
def _national_classes(game, nation):
    try:
        data = ET.fromstring(read(game/'Data/XML/item_defs/vehicles'/nation/'list.xml.dvpl'))
    except ET.ParseError as exc:
        raise ValueError('Invalid vehicle class catalog') from exc
    rows = {}
    for item in data:
        if item.tag in rows:
            raise ValueError('Duplicate vehicle listing: ' + item.tag)
        classes = CLASSES.intersection((item.findtext('tags') or '').split())
        # An absent/ambiguous tag is not enough evidence to grant a bonus.
        rows[item.tag] = next(iter(classes)) if len(classes) == 1 else None
    return rows


def vehicle_class(game, nation, name):
    if not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]+', value)
               for value in (nation, name)):
        raise ValueError('Invalid vehicle class identity')
    return _national_classes(Path(game), nation).get(name)
