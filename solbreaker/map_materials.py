"""Collision exclusions supported by the pinned map's resource metadata."""

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
    seen = set()
    for _ in range(16):
        if material_id in seen:
            break
        seen.add(material_id)
        material = materials.get(material_id)
        if material is None:
            break
        if material.get('textures',{}).get('albedo') == _CANE_ALBEDO:
            return True
        material_id = material.get('parentMaterialKey')
        if material_id is None:
            break
    return False
