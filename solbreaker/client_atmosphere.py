"""Apply local lighting presets to verified scenes from the player's game."""
import hashlib
import json
from pathlib import Path
import struct

from . import dvpl

PRESETS = ('original', 'evening', 'fog')
MINES = '06_rudniki_rd'
LEGACY_MINES_EVENING_SHA256 = '9ef6142107a9ecf336b262f48aa51515b092474ca7e17e07f4398c071110f5f5'
SUMMER_SCENES = frozenset({MINES, '05_amigosville_am', '22_italy_it'})

# The pinned SC2 scene sets share the same directional light layout. The
# three fog colors are consecutive fields in each scene's render config.
# Each row gives the original digest, ambient blue, IBL multiplier, sky fog,
# fog end and the stock sunlight intensity.
SCENES = {
    MINES: (
        '9426da20cdd7d6cb6b589c3840c98ca49aa1cc138a88978ebfad043be7b38f5a',
        0x73385A, 0x733E3D, 0x73418A, 0x7341E4, 8.0),
    '05_amigosville_am': (
        '981b889f0273c57cbb7606bda59db0cdb9c9bf81ac64b39db7d17f16b3ff300f',
        0x7D0D93, 0x7D0F0F, 0x7D07D7, 0x7D0831, 7.0),
    '22_italy_it': (
        '9af79331349ebee3afb56525f72f7520ae92a2bc6baf7102b393e2fed82b35ba',
        0x496335, 0x4964B1, 0x495D79, 0x495DD3, 8.0),
    # Keep the old layout available to restore previously modified clients.
    '25_canyon_ca': (
        'a5ade3193be37f10f8b2d33d72f49fa9d1dad29549b9612faa02a26611c6be24',
        0x60FFEC, 0x6100E5, 0x60FA54, 0x60FAAE, 8.0),
}


def scene_path(name):
    return Path('3d/Maps')/name/(name+'.sc2.dvpl')


def install_weather_maps(lab, manifest):
    """Build selectable variants from the pinned game beside each base scene."""
    import yaml

    from .client_ui import local_directory
    from .connection import atomic_write
    from .map_catalog import MAP_CATALOG_RESOURCE, WEATHER_MAPS, weather_map_row

    data = lab/'client/Data'
    catalog = data/MAP_CATALOG_RESOURCE
    if not catalog.is_file() or manifest.get('weather_maps_version') == 1:
        return manifest
    source = Path(manifest.get('source_game', manifest['game'])).resolve(strict=True)
    rows = yaml.safe_load(dvpl.read(catalog))
    maps = rows['maps']
    local_directory(data, source)
    changed = False

    def install(relative, content):
        parent = data
        for part in relative.parts[:-1]:
            parent /= part
            if not parent.exists() and not parent.is_symlink():
                parent.mkdir()
            local_directory(parent, source)
            if parent.resolve(strict=True) != parent:
                raise ValueError('Weather maps require private client directories')
        target = data/relative
        if target.is_symlink() or not target.exists() or target.read_bytes() != content:
            atomic_write(target, content, replace_symlink=True)

    for key, (ident, base, preset) in WEATHER_MAPS.items():
        if base not in maps:
            continue
        if any(isinstance(row, dict) and row.get('id') == ident
               for name, row in maps.items() if name != key):
            raise ValueError(f'Weather map ID {ident} is already assigned')
        row = weather_map_row(maps[base], key)
        original = Path(maps[base]['localName'])
        directory = original.parent.name
        if directory not in SUMMER_SCENES or original.as_posix() != f'{directory}/{directory}.sc2':
            raise ValueError(f'Unexpected weather map source for {base}')
        relative = scene_path(directory)
        source_scene = source/'Data'/relative
        install(relative.with_name(key+'.sc2.dvpl'), scene_preset(source_scene.read_bytes(), directory, preset))
        install(relative.with_name(key+'.scg.dvpl'), source_scene.with_name(directory+'.scg.dvpl').read_bytes())
        for folder, filenames in (
            ('BattleLoadingScreen', ('BackgroundLoading.packed.webp.dvpl',)),
            ('BattleScreenHUD/minimap', ('MiniMapSmall.packed.webp.dvpl', 'MiniMapSmall@2x.packed.webp.dvpl')),
        ):
            art = Path('Gfx/UI')/folder
            for filename in filenames:
                install(art/key/filename, (source/'Data'/art/base/filename).read_bytes())
        if maps.get(key) != row:
            maps[key] = row
            changed = True
    if changed:
        atomic_write(catalog, dvpl.encode(yaml.safe_dump(
            rows, sort_keys=False, allow_unicode=True).encode()), replace_symlink=True)
    updated = dict(manifest, weather_maps_version=1)
    atomic_write(lab/'manifest.json', (json.dumps(updated, indent=2)+'\n').encode())
    return updated


def scene_preset(original, name, preset):
    expected, light, ibl, fog, fog_end, sunlight = SCENES[name]
    if hashlib.sha256(original).hexdigest() != expected:
        raise ValueError(f'Lighting presets require the pinned scene for {name}')
    if preset == 'original':
        return original
    scene = bytearray(dvpl.decode(original))
    stock_ibl, = struct.unpack_from('<f', scene, ibl)
    if preset == 'evening':
        ambient = (.79, .60, .53)
        sun = (.07, .32, 1.0)
        intensity, environment = sunlight*.53, stock_ibl*.52
        sky, horizon, haze = (.38, .37, .57), (1.0, .48, .22), (.59, .41, .49)
        distance, end = 215., 650.
    elif preset == 'fog':
        ambient = (.90, .87, .82)
        sun = (.76, .73, .68)
        intensity, environment = sunlight*.34, stock_ibl*.78
        sky, horizon, haze = (.62, .68, .74), (.71, .74, .76), (.64, .70, .74)
        distance, end = 160., 410.
        struct.pack_into('<f', scene, fog+0x51, .0075)  # fogDensity
    else:
        raise ValueError(f'Unknown lighting preset {preset}')
    values = {light+offset: (value,) for offset, value in
              zip((0, 9, 18, 36, 45, 54, 81), (*ambient, *sun, intensity))}
    values.update({ibl: (environment,), fog+0x2a: (distance,), fog_end: (end,)})
    for offset, color in ((0, sky), (0x15, horizon), (0x3c, haze)):
        values[fog+offset] = (*color, 1.)
    for offset, target in values.items():
        fmt = '<'+'f'*len(target)
        struct.pack_into(fmt, scene, offset, *target)
    return dvpl.encode(scene)


def apply_preset(lab, manifest, preset, *, mines_only=False):
    from .client_ui import local_directory
    from .connection import atomic_write

    if preset not in PRESETS:
        raise ValueError(f'Unknown lighting preset {preset}')
    tracked = manifest.get('map_lighting_overlays', {})
    legacy_mines = manifest.get('mines_evening_overlay', False)
    selected = ({MINES} if mines_only else SUMMER_SCENES) if preset != 'original' else set()
    names = selected | tracked.keys() | ({MINES} if legacy_mines else set())
    if not names:
        return
    source = Path(manifest.get('source_game', manifest['game'])).resolve(strict=True)
    manifest_path = lab/'manifest.json'
    pending = {}
    final = {}
    writes = []
    for name in SCENES:
        if name not in names:
            continue
        relative = scene_path(name)
        target = lab/'client/Data'/relative
        original = (source/'Data'/relative).read_bytes()
        wanted = preset if name in selected else 'original'
        with target.open('rb') as stream:
            current = hashlib.file_digest(stream, 'sha256').hexdigest()
        result = scene_preset(original, name, wanted)
        digest = hashlib.sha256(result).hexdigest()
        accepted = {SCENES[name][0], digest, *tracked.get(name, ())}
        if legacy_mines and name == MINES:
            accepted.add(LEGACY_MINES_EVENING_SHA256)
        if current not in accepted:
            raise ValueError(f'The prepared scene for {name} already has a different local modification')
        if wanted != 'original':
            final[name] = [digest]
        if current != digest:
            pending[name] = sorted({current, digest})
            writes.append((relative, target, result))
    if writes:
        # Record both hashes before any file changes so a later launch can
        # finish the change or restore the original after an interrupted write.
        before = dict(manifest, map_lighting_overlays={**tracked, **pending})
        atomic_write(manifest_path, (json.dumps(before, indent=2)+'\n').encode())
        for relative, target, result in writes:
            parent = lab/'client'
            for part in ('Data', *relative.parts[:-1]):
                parent /= part
                local_directory(parent, source)
                if parent.resolve(strict=True) != parent:
                    raise ValueError('Lighting presets require private client directories')
            atomic_write(target, result, replace_symlink=True)
    if writes or legacy_mines or tracked != final:
        after = dict(manifest)
        after.pop('mines_evening_overlay', None)
        if final:
            after['map_lighting_overlays'] = final
        else:
            after.pop('map_lighting_overlays', None)
        atomic_write(manifest_path, (json.dumps(after, indent=2)+'\n').encode())
