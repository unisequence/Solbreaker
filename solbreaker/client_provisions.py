"""Expose the pinned inert desert provision in the local client overlay."""
from . import dvpl

RESOURCE = 'XML/item_defs/vehicles/common/provisions/common.xml.dvpl'


def patched_resource(source):
    data = dvpl.read(source)
    start = data.find(b'<desertPower>')
    end_tag = b'</desertPower>'
    end = data.find(end_tag, start)
    if start < 0 or end < 0 or data.find(b'<desertPower>', start + 1) >= 0:
        raise ValueError('Unsupported desert provision definition')
    end += len(end_tag)
    block = data[start:end]
    marker = b'<isAbility/>'
    geometry = b'<geometryFilter>\r\n\t\t\t<include>desert</include>\r\n\t\t</geometryFilter>'
    icon = b'<icon>provision_desert_power</icon>'
    if (b'<id>113</id>' not in block or block.count(marker) != 1 or
            block.count(geometry) != 1 or block.count(icon) != 1):
        raise ValueError('Unsupported desert provision restrictions')
    block = (block.replace(marker, b'').replace(geometry, b'')
                  .replace(icon, b'<icon>safety_set</icon>'))
    for before, after, count in (
        (b'<firmGroundPassabilityIncrease>30</firmGroundPassabilityIncrease>',
         b'<firmGroundPassabilityIncrease>0</firmGroundPassabilityIncrease>', 2),
        (b'<mediumGroundPassabilityIncrease>30</mediumGroundPassabilityIncrease>',
         b'<mediumGroundPassabilityIncrease>0</mediumGroundPassabilityIncrease>', 2),
        (b'<repairSpeedFactor>1.5</repairSpeedFactor>', b'<repairSpeedFactor>1</repairSpeedFactor>', 1),
        (b'<repairSpeedFactor>50</repairSpeedFactor>', b'<repairSpeedFactor>0</repairSpeedFactor>', 1),
    ):
        if block.count(before) != count:
            raise ValueError('Unsupported desert provision bonuses')
        block = block.replace(before, after)
    return dvpl.encode(data[:start] + block + data[end:])
