"""Run inside Blender to bake named GLB meshes into a portable interchange file."""
import json
from pathlib import Path
import sys

import bpy


def extract(source, output):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source))
    scene = bpy.context.scene
    names = ('hull', 'turret_01', 'gun_01', 'chassis_track_L', 'chassis_track_R')
    known = (*names, 'HP_gunFire_01', 'HP_Track_LFront', 'HP_Track_LRear',
             'HP_Track_RFront', 'HP_Track_RRear')
    collision_names = ('collision_hull', 'collision_turret_01', 'collision_gun_01')
    materials, groups, points = {}, [], {}
    output.mkdir(exist_ok=True)

    def part_name(obj):
        current = obj
        while current is not None:
            name = current.name.split('.')[0]
            if name in (*known, *collision_names):
                return name
            current = current.parent
        return None

    for obj in scene.objects:
        part = part_name(obj)
        if part is None:
            if obj.type == 'MESH':
                raise ValueError(f'Mesh {obj.name} needs a named vehicle part or parent')
            continue
        if obj.name.split('.')[0] == part and part in known:
            points[part] = list(obj.matrix_world.translation)
        if obj.type != 'MESH' or part.startswith('HP_'):
            continue
        mesh = obj.data
        mesh.calc_loop_triangles()
        world = obj.matrix_world
        normals = world.to_3x3().inverted_safe().transposed()
        uvs = mesh.uv_layers.active
        negative_scale = world.determinant() < 0
        if len(mesh.loop_triangles) > 160000:
            raise ValueError(f'{obj.name} exceeds 160,000 triangles')
        for tri in mesh.loop_triangles:
            material = mesh.materials[tri.material_index] if len(mesh.materials) else None
            material_name = material.name if material is not None else 'Default'
            if not part.startswith('collision_') and material_name not in materials:
                texture = f'albedo-{len(materials)+1}.png'
                color = (.23, .27, .19, 1.)
                image = None
                if material is not None and material.use_nodes:
                    shader = next((node for node in material.node_tree.nodes if node.type == 'BSDF_PRINCIPLED'), None)
                    links = shader.inputs['Base Color'].links if shader else ()
                    if shader:
                        color = shader.inputs['Base Color'].default_value
                    source_node = links[0].from_node if links else None
                    image = source_node.image if source_node is not None and source_node.type == 'TEX_IMAGE' else None
                if image is None:
                    image = bpy.data.images.new(texture, width=16, height=16, alpha=True)
                    image.pixels = list(color) * (16*16)
                if not all(1 <= dimension <= 4096 for dimension in image.size):
                    raise ValueError(f'Texture {image.name} exceeds 4096 pixels')
                image.file_format = 'PNG'
                image.save(filepath=str(output/texture))
                materials[material_name] = texture
            loops = tuple(reversed(tri.loops)) if negative_scale else tri.loops
            verts = []
            for loop_id in loops:
                loop = mesh.loops[loop_id]
                vertex = mesh.vertices[loop.vertex_index]
                p = world @ vertex.co
                n = (normals @ loop.normal).normalized()
                uv = uvs.data[loop_id].uv if uvs is not None else (0., 0.)
                # Author in Blender XYZ. DAVA uses the same axis order.
                verts.append([*p, *n, uv[0], 1.-uv[1]])
            groups.append([part, material_name, verts])
    with (output/'mesh.json').open('w', encoding='utf-8') as stream:
        json.dump(dict(groups=groups, materials=materials, points=points), stream,
                  separators=(',', ':'), allow_nan=False)


if __name__ == '__main__':
    source, output = map(Path, sys.argv[sys.argv.index('--')+1:])
    extract(source, output)
