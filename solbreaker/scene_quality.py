"""Private client repair for the pinned map's invalid collision quality gate.

Only serialized booleans change. Meshes, IDs, LKA mappings and collision data
stay byte-for-byte intact. The immutable installed game is never written.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import dvpl
from .dava_scene import Archive, components
from .client_ui import local_directory


RESOURCES = ('Data/3d/Maps/34_forgecity_fc/34_forgecity_fc.sc2.dvpl',)


class _LocatedArchive(Archive):
    def __init__(self, data, strings=None, *, origin=0, locations=None):
        super().__init__(data,strings)
        self.origin=origin
        self.locations={} if locations is None else locations
        self.booleans=[]

    def value(self, kind, version):
        if kind==1:
            self.booleans.append(self.origin+self.pos)
        if kind==8:
            size=self.number('I')
            origin=self.origin+self.pos
            child=_LocatedArchive(self.take(size),self.strings,
                                  origin=origin,locations=self.locations)
            result=child.load()
            if child.pos!=len(child.data):
                raise ValueError('Trailing nested quality archive')
            return result
        return super().value(kind,version)

    def load(self):
        result=super().load()
        # Retain the dictionaries as well as their offsets: IDs cannot be
        # recycled when the scene metadata is superseded by its entity archive.
        self.locations[id(result)]=(result,tuple(self.booleans))
        return result


def _read(raw):
    reader=_LocatedArchive(raw)
    if reader.take(4)!=b'SFV2' or reader.number('I') not in (35,41,43,47,48):
        raise ValueError('Unsupported quality repair scene')
    reader.number('I')  # Root node count; no hierarchy is added or removed.
    reader.load()
    reader.take(reader.number('I'))
    if reader.data[reader.pos:reader.pos+4] not in (b'KA\x02\x00',b'KA\x01\x00'):
        raise ValueError('Quality repair requires a keyed scene hierarchy')
    result=reader.load()
    if reader.pos!=len(raw) or '#hierarchy' not in result:
        raise ValueError('Incomplete quality repair scene')
    return result,reader.locations


def repair_scene(raw):
    data,locations=_read(raw)
    result=bytearray(raw)
    repairs=[]

    def walk(nodes):
        for node in nodes:
            comps=components(node)
            quality=comps.get('QualitySettingsComponent',{})
            kind=comps.get('CollisionTypeComponent',{}).get('CollisionType')
            # An empty required quality can never equal ON or OFF. Such a
            # critical static collider must not disappear with decoration.
            if (kind==6 and quality.get('filterByType')==0
                    and quality.get('requiredGroup')
                    and not quality.get('requiredQuality')
                    and not quality.get('modelType')):
                expected={'comp.typename','filterByType','requiredGroup'}
                if set(quality)!=expected:
                    raise ValueError('Unreviewed invalid quality gate fields')
                _,offsets=locations[id(quality)]
                if len(offsets)!=1 or result[offsets[0]]!=0:
                    raise ValueError('Quality filter is not one serialized false boolean')
                offset=offsets[0]
                result[offset]=1  # Type filtering with no model type is unconditional.
                repairs.append(dict(node_id=node['id'],name=node['name'],
                                    collision_type=kind,boolean_offset=offset))
            walk(node.get('#hierarchy',[]))
    walk(data['#hierarchy'])
    patched=bytes(result)
    if repairs:
        checked,_=_read(patched)
        repaired={row['node_id'] for row in repairs}
        def verify(nodes):
            for node in nodes:
                if node['id'] in repaired:
                    if components(node)['QualitySettingsComponent']['filterByType']!=1:
                        raise ValueError('Quality repair did not round-trip')
                    repaired.remove(node['id'])
                verify(node.get('#hierarchy',[]))
        verify(checked['#hierarchy'])
        if repaired:
            raise ValueError('Quality repair lost a scene node')
    return patched,repairs


def expected_resource(relative, content):
    """The exact client bytes expected by the resource-parity QA tool."""
    relative=str(relative)
    compressed=relative.endswith('.dvpl')
    if (relative if compressed else relative+'.dvpl') not in RESOURCES:
        return content,[]
    raw,repairs=repair_scene(dvpl.decode(content) if compressed else content)
    return ((dvpl.encode(raw) if compressed else raw) if repairs else content),repairs


def install(lab):
    lab=Path(lab).resolve(strict=True)
    manifest=json.loads((lab/'manifest.json').read_text())
    game=Path(manifest['game']).resolve(strict=True)
    source=Path(manifest.get('source_game',manifest['game'])).resolve(strict=True)
    changed=[]
    resources=[name for compressed in RESOURCES
               for name in (compressed,compressed.removesuffix('.dvpl'))]
    for relative in resources:
        target=lab/'client'/relative
        if not target.is_file():
            continue
        content,repairs=expected_resource(relative,target.read_bytes())
        if not repairs:
            continue
        parent=lab/'client'
        for part in Path(relative).parts[:-1]:
            parent/=part
            if parent.is_symlink():
                resolved=parent.resolve(strict=True)
                trusted=next((root for root in (game,source) if resolved.is_relative_to(root)),None)
                if trusted is None:
                    raise ValueError(f'Quality resource parent is outside the pinned game: {parent}')
                local_directory(parent,trusted)
            elif parent.resolve(strict=True)!=parent.absolute():
                raise ValueError(f'Quality resource parent is not private: {parent}')
        fd,temporary=tempfile.mkstemp(prefix='.quality-repair-',dir=target.parent)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(content)
            os.replace(temporary,target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        changed.append(dict(resource=relative,repairs=repairs,
                            sha256=hashlib.sha256(content).hexdigest()))
    return changed
