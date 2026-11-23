"""Install a separate arena on the private game client or dedicated server."""
import base64
import gzip
import hashlib
import json
import math
from pathlib import Path
import struct
import time

import numpy as np
import yaml

from . import dvpl
from .connection import atomic_write, manifest_client_path
from .dava_proving_scene import compile_scene

KEY = 'solbreaker_proving_ground'
MAP_ID = 107
PARENT = Path('3d/Maps/06_rudniki_rd')
DESCRIPTOR = 'private/proving-ground.json'
BUNDLE = Path(__file__).with_name('assets')/'proving-ground.json.gz'
TEAM_SPAWNS = [[[x,z] for x in range(-30,31,10)] for z in (-14,14)]
COMPILER_VERSION = 19
SOURCE_HASHES = {'sc2': '9426da20cdd7d6cb6b589c3840c98ca49aa1cc138a88978ebfad043be7b38f5a',
                 'scg': 'd340e5b0598e52ff055ca9bfd8de7fda27994e72eb09ff003459643a9df4d0d2'}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _current_resource(client, relative, digest):
    path = client/relative
    return (path.is_file() and path.resolve(strict=True) == path and not path.is_symlink()
            and _digest(path.read_bytes()) == digest)


def _marked_catalog(catalog, original):
    rows = yaml.safe_load(dvpl.read(catalog if catalog.exists() else original))
    maps = rows['maps']
    if any(row.get('id') == MAP_ID for name,row in maps.items() if name != KEY):
        raise ValueError(f'Map ID {MAP_ID} is already in use')
    row = dict(maps['rudniki'],id=MAP_ID,localName=f'{PARENT.name}/{KEY}.sc2',
               avaliableInTrainingRoom=True, availableModes=[0],
               assaultRespawnPoints={side:[dict(respawnNumber=1,points=pts)]
                                    for side,pts in zip(('allies','enemies'),TEAM_SPAWNS)})
    row.pop('tags',None)
    if maps.get(KEY) == row:
        return None
    maps[KEY] = row
    return dvpl.encode(yaml.safe_dump(rows,sort_keys=False,allow_unicode=True).encode())


def _sound_registration(target, source):
    document = yaml.safe_load(dvpl.read(target if target.exists() else source))
    changed = False
    for section in ('ambient','music_play'):
        values = document[section]
        if values.get(KEY) != values['rudniki']:
            values[KEY] = values['rudniki']
            changed = True
    if changed:
        return dvpl.encode(yaml.safe_dump(document,sort_keys=False,allow_unicode=True).encode())
    return None


def _height_lookup(raw):
    size,tile = struct.unpack_from('<II', raw)
    if (size,tile) != (1024,16):
        raise ValueError('Unexpected proving ground heightfield')
    heights = np.frombuffer(raw, dtype='<u2', offset=8).reshape(
        size//tile, size//tile, tile, tile).transpose(0,2,1,3).reshape(size,size)
    def height(x,z):
        i,j = max(0,min(size-1,x+512)), max(0,min(size-1,z+512))
        ix,iz = math.floor(i),math.floor(j)
        dx,dz = i-ix,j-iz
        line0 = heights[iz,ix]*(1-dx) + heights[iz,min(ix+1,size-1)]*dx
        line1 = heights[min(iz+1,size-1),ix]*(1-dx) + heights[min(iz+1,size-1),min(ix+1,size-1)]*dx
        return float((line0*(1-dz) + line1*dz) * (100 / 65535))
    return height


def install(lab, manifest, *, bundle=BUNDLE):
    """Install atomically, publishing the descriptor after every companion exists."""
    from .platform_io import PrivateFileBusy, private_lock
    lab = Path(lab).resolve(strict=True)
    deadline = time.monotonic() + 60
    while True:
        try:
            with private_lock(lab/'private', 'proving-ground.lock'):
                return _install(lab, manifest, bundle)
        except PrivateFileBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.15)


def _install(lab, manifest, bundle):
    from .client_ui import local_directory
    from .dev_map import _relative_path

    lab = Path(lab).resolve(strict=True)
    client = lab/'client'
    data = client/'Data'
    private = lab/'private'
    source = Path(manifest.get('source_game',manifest['game'])).resolve(strict=True)
    if (manifest_client_path(manifest['client']) != client or client.resolve(strict=True) != client
            or data.resolve(strict=True) != data or private.resolve(strict=True) != private
            or lab.is_relative_to(source) or source.is_relative_to(lab)):
        raise ValueError('Proving ground requires a private Solbreaker installation')
    bundle_bytes = bundle.read_bytes()
    revision = f'{COMPILER_VERSION}-' + _digest(bundle_bytes)
    descriptor = lab/DESCRIPTOR
    catalog = data/'maps.yaml.dvpl'
    source_catalog = source/'Data/maps.yaml.dvpl'
    if not catalog.is_file() and not source_catalog.is_file():
        # A minimal client-only repair layout can launch without map assets.
        return
    catalog_bytes = _marked_catalog(catalog, source_catalog)
    sound = data/'sounds.yaml.dvpl'
    sound_bytes = (_sound_registration(sound,source/'Data'/sound.name)
                   if (client/'wotblitz.exe').is_file() else None)
    if descriptor.is_file() and not descriptor.is_symlink():
        try:
            record = json.loads(descriptor.read_text(encoding='utf-8'))
        except (ValueError,UnicodeError):
            record = {}
        if (isinstance(record,dict) and record.get('revision') == revision
                and record.get('files') and all(
                    _current_resource(client, _relative_path(name), digest)
                    for name,digest in record['files'].items())):
            if catalog_bytes is not None:
                atomic_write(catalog,catalog_bytes,replace_symlink=True)
            if sound_bytes is not None:
                atomic_write(sound,sound_bytes,replace_symlink=True)
            return
    layout = json.loads(gzip.decompress(bundle_bytes))
    if layout['format'] != 'solbreaker-proving-ground/v1':
        raise ValueError('Unsupported authored proving ground')
    files = {str(_relative_path(name).as_posix()):base64.b64decode(payload,validate=True)
             for name,payload in layout['resources'].items()}
    heightmap = files[(PARENT/'landscape'/f'{KEY}.heightmap').as_posix()]
    donor = source/'Data'/PARENT/PARENT.name
    original = {}
    for ext, expected in SOURCE_HASHES.items():
        raw = donor.with_suffix('.'+ext+'.dvpl').read_bytes()
        if _digest(raw) != expected:
            raise ValueError(f'Proving ground requires the pinned {ext.upper()} map resources')
        original[ext] = dvpl.decode(raw)
    codes = files[(PARENT/'blitz'/f'{KEY}.mkm').as_posix()][24:]
    def surface(x,z):
        i = max(0,min(1023,int(x+512))) + 1024*max(0,min(1023,int(z+512)))
        return (codes[i//4] >> (2*(i%4))) & 3
    scene, meshes, index = compile_scene(original['sc2'], original['scg'],
                                         layout['objects'], _height_lookup(heightmap), surface)
    files.update({(PARENT/(KEY+'.sc2')).as_posix():scene,
                  (PARENT/(KEY+'.scg')).as_posix():meshes,
                  (PARENT/'blitz'/(KEY+'.lka')).as_posix():index})
    hashes = {}
    for relative, content in files.items():
        path = data/(relative+'.dvpl')
        parent = data
        for part in path.parent.relative_to(data).parts:
            parent /= part
            if not parent.is_symlink():
                parent.mkdir(exist_ok=True)
            local_directory(parent,source)
            if parent.resolve(strict=True) != parent:
                raise ValueError('Proving ground resources require private directories')
        encoded = dvpl.encode(content)
        hashes[path.relative_to(client).as_posix()] = _digest(encoded)
        atomic_write(path,encoded,replace_symlink=True)
    if catalog_bytes is not None:
        atomic_write(catalog,catalog_bytes,replace_symlink=True)
    if sound_bytes is not None:
        atomic_write(sound,sound_bytes,replace_symlink=True)
    doc = dict(format='solbreaker-dev-map/v1',name=KEY,base_key=KEY,wire_id=MAP_ID,
               revision=revision,team_spawns=TEAM_SPAWNS,files=hashes)
    atomic_write(descriptor,(json.dumps(doc,indent=2)+'\n').encode())
    from .map_catalog import _load_map_spec
    _load_map_spec.cache_clear()
