"""Build the editable Blender scene and render the two proving-ground illustrations.

Run using the project Python and a Blender 4.3+ executable on PATH:
    python tools/render_proving_ground.py --output /path/to/art
"""

import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path


def export(output, blender):
    import numpy as np

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from build_proving_ground import wayfinder
    from solbreaker import proving_ground_layout as layout
    from solbreaker.proving_ground_signs import SIGNS, sign_meshes

    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        scratch = Path(tmp)
        document = layout.geometry(illustrate_trees=True)
        document["objects"].extend(sign_meshes(layout.elevation))
        for number, title, *_ in SIGNS:
            wayfinder(number, title).save(scratch / f"sign-{number}.png")
        (scratch / "geometry.json").write_text(
            json.dumps(document, separators=(",", ":"))
        )
        layout.terrain_color(2048).save(scratch / "ground.png")
        grid = np.linspace(-512, 512, 257)
        np.save(scratch / "heights.npy", layout.elevation(grid[None, :], grid[:, None]))
        subprocess.run(
            [
                blender,
                "--background",
                "--factory-startup",
                "--threads",
                "4",
                "--python-exit-code",
                "1",
                "--python",
                str(Path(__file__).resolve()),
                "--",
                str(scratch),
                str(output),
            ],
            check=True,
        )


def build(scratch, output):
    import bpy
    import numpy as np
    from mathutils import Vector
    from mathutils.noise import fractal, ridged_multi_fractal

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 12
    scene.cycles.use_denoising = True
    scene.cycles.max_bounces = 4
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 4
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.exposure = 0.15

    def collection(name):
        group = bpy.data.collections.new(name)
        scene.collection.children.link(group)
        return group

    def mesh(name, vertices, faces, group):
        data = bpy.data.meshes.new(name)
        data.from_pydata(vertices, [], faces)
        data.update()
        obj = bpy.data.objects.new(name, data)
        group.objects.link(obj)
        return obj

    def world_xyz(nodes, links, scale=1.0):
        coords = nodes.new("ShaderNodeNewGeometry")
        n = nodes.new("ShaderNodeVectorMath")
        n.operation = "SCALE"
        n.inputs["Scale"].default_value = scale
        links.new(coords.outputs["Position"], n.inputs[0])
        return n.outputs["Vector"]

    def basis(name, color):
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = (*color, 1)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        bsdf.inputs["Base Color"].default_value = (*color, 1)
        bsdf.inputs["Roughness"].default_value = 0.76
        return mat, bsdf

    def grid_faces(n):
        return [
            (j * n + i, j * n + i + 1, (j + 1) * n + i + 1, (j + 1) * n + i)
            for j in range(n - 1)
            for i in range(n - 1)
        ]

    def srgb_linear(value):
        return ((value + 0.055) / 1.055) ** 2.4 if value > 0.04045 else value / 12.92

    landscape = collection("Playable terrain")
    heights = np.load(scratch / "heights.npy")
    n = len(heights)
    axis = np.linspace(-512, 512, n)
    coords = [
        (x, y, float(heights[j, i]))
        for j, y in enumerate(axis)
        for i, x in enumerate(axis)
    ]
    field = mesh("1024 m terrain", coords, grid_faces(n), landscape)
    for polygon in field.data.polygons:
        polygon.use_smooth = True
    mat, bsdf = basis("Course surface", (0.24, 0.27, 0.18))
    links = mat.node_tree.links
    nodes = mat.node_tree.nodes
    uv = nodes.new("ShaderNodeVectorMath")
    uv.operation = "ADD"
    uv.inputs[1].default_value = (0.5, 0.5, 0.0)
    links.new(world_xyz(nodes, links, 1.0 / 1024), uv.inputs[0])
    image = nodes.new("ShaderNodeTexImage")
    image.image = bpy.data.images.load(str(scratch / "ground.png"))
    image.image.pack()
    image.extension = "EXTEND"
    links.new(uv.outputs["Vector"], image.inputs["Vector"])
    links.new(image.outputs["Color"], bsdf.inputs["Base Color"])
    field.data.materials.append(mat)

    groups = {}
    materials = {}
    for item in json.loads((scratch / "geometry.json").read_text())["objects"]:
        tag = item["name"].split("_")[1]
        if tag not in groups:
            groups[tag] = collection("Course / " + tag.title())
        if "sign" in item:
            obj = mesh(item["name"][3:], item["vertices"], [(0, 1, 2, 3)], groups[tag])
            uv_data = obj.data.uv_layers.new(name="Sector artwork").data
            for loop, corner in zip(
                obj.data.loops, ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
            ):
                uv_data[loop.index].uv = corner
            mat, bsdf = basis("Sector " + item["sign"], (0.3, 0.3, 0.3))
            image = mat.node_tree.nodes.new("ShaderNodeTexImage")
            image.image = bpy.data.images.load(
                str(scratch / ("sign-" + item["sign"] + ".png"))
            )
            image.image.pack()
            # Reverse the U axis on the back of a printed direction board.
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            coords = nodes.new("ShaderNodeTexCoord")
            side = nodes.new("ShaderNodeNewGeometry")
            mirror = nodes.new("ShaderNodeVectorMath")
            mirror.operation = "MULTIPLY_ADD"
            mirror.inputs[1].default_value = (-1.0, 1.0, 1.0)
            mirror.inputs[2].default_value = (1.0, 0.0, 0.0)
            links.new(coords.outputs["UV"], mirror.inputs[0])
            mix = nodes.new("ShaderNodeMixRGB")
            links.new(side.outputs["Backfacing"], mix.inputs[0])
            links.new(coords.outputs["UV"], mix.inputs[1])
            links.new(mirror.outputs[0], mix.inputs[2])
            links.new(mix.outputs[0], image.inputs["Vector"])
            mat.node_tree.links.new(image.outputs["Color"], bsdf.inputs["Base Color"])
            obj.data.materials.append(mat)
            continue
        obj = mesh(item["name"][3:], item["vertices"], item["triangles"], groups[tag])
        if item.get("water"):
            mat, bsdf = basis("Shallow circuit ford", (0.082, 0.215, 0.22))
            bsdf.inputs["Roughness"].default_value = 0.17
            bsdf.inputs["Transmission Weight"].default_value = 0.52
            bsdf.inputs["IOR"].default_value = 1.333
            bsdf.inputs["Alpha"].default_value = 0.76
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            waves = nodes.new("ShaderNodeTexNoise")
            waves.inputs["Scale"].default_value = 3.9
            waves.inputs["Detail"].default_value = 2.5
            links.new(world_xyz(nodes, links), waves.inputs["Vector"])
            ripple = nodes.new("ShaderNodeBump")
            ripple.inputs["Strength"].default_value = 0.19
            ripple.inputs["Distance"].default_value = 0.10
            links.new(waves.outputs["Fac"], ripple.inputs["Height"])
            links.new(ripple.outputs["Normal"], bsdf.inputs["Normal"])
            obj.data.materials.append(mat)
            continue
        rgb = tuple(item["color"][:3])
        label = item["name"].split(tag + "_", 1)[1]
        if rgb not in materials:
            linear = tuple(srgb_linear(v) for v in rgb)
            mat, bsdf = basis(label, linear)
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            noise = nodes.new("ShaderNodeTexNoise")
            noise.inputs["Scale"].default_value = 0.60
            noise.inputs["Detail"].default_value = 3.0
            links.new(world_xyz(nodes, links), noise.inputs["Vector"])
            ramp = nodes.new("ShaderNodeValToRGB")
            ramp.color_ramp.elements[0].color = (*[v * 0.70 for v in linear], 1)
            ramp.color_ramp.elements[1].color = (
                *[min(1.0, v * 1.28) for v in linear],
                1,
            )
            links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
            links.new(ramp.outputs[0], bsdf.inputs["Base Color"])
            if label in ("steel", "galvanized", "dark"):
                bsdf.inputs["Metallic"].default_value = 0.40
            if label == "glass":
                bsdf.inputs["Metallic"].default_value = 0.45
                bsdf.inputs["Roughness"].default_value = 0.22
            if label == "lamp":
                bsdf.inputs["Emission Color"].default_value = (1.0, 0.73, 0.26, 1.0)
                bsdf.inputs["Emission Strength"].default_value = 1.6
            materials[rgb] = mat
        obj.data.materials.append(materials[rgb])

    # Distant ridgelines are illustration scenery, outside the playable world.
    backdrop = collection("Illustration / distant ridgelines")
    # Continue the edge of the square mesh to the rock foothills. This layer is
    # below the sampled play area and closes the lateral gaps in an oblique view.
    corners = [(-509.0, -509.0), (509.0, -509.0), (509.0, 509.0), (-509.0, 509.0)]
    ring = []
    for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1]):
        for i in range(100):
            t = i / 100.0
            x = ax * (1 - t) + bx * t
            y = ay * (1 - t) + by * t
            iy = int((y + 512.0) / 4.0)
            ix = int((x + 512.0) / 4.0)
            ring.append((x, y, max(3.0, float(heights[iy, ix]) - 5.0)))
    verts = []
    faces = []
    steps = 30
    for j in range(steps):
        t = j / (steps - 1)
        for i, (x, y, h) in enumerate(ring):
            scale = 1.0 + 3.6 * t
            xx = x * scale
            yy = y * scale
            field = 28.0 + 18.0 * fractal(
                Vector((xx * 0.009, yy * 0.009, 8.0)), 0.8, 2.0, 4
            )
            blend = min(1.0, t * 10.0)
            verts.append((xx, yy, h * (1.0 - blend) + field * blend))
            if j:
                a = (j - 1) * len(ring) + i
                b = (j - 1) * len(ring) + (i + 1) % len(ring)
                faces.append((a, a + len(ring), b + len(ring), b))
    foothills = mesh("Foothills beyond the range", verts, faces, backdrop)
    foothills.data.materials.append(basis("Hillside scrub", (0.129, 0.151, 0.094))[0])
    for p in foothills.data.polygons:
        p.use_smooth = True
    for band, (start, depth, amplitude, ink) in enumerate(
        (
            (485, 690, 127, (0.08, 0.13, 0.13)),
            (1000, 750, 187, (0.16, 0.21, 0.255)),
            (1670, 850, 282, (0.26, 0.31, 0.355)),
        )
    ):
        verts = []
        faces = []
        nx, ny = 420, 66
        for j in range(ny):
            t = j / (ny - 1)
            for i in range(nx):
                x = -3300.0 + 6600.0 * i / (nx - 1)
                y = start + t * depth
                ridge = 0.41 + fractal_height(fractal, x, y, band)
                relief = ridged_multi_fractal(
                    Vector((x * 0.0029, y * 0.0045, band + 0.4)),
                    0.78,
                    2.16,
                    6,
                    1.0,
                    2.0,
                )
                h = (
                    -34.0
                    + amplitude
                    * (ridge + 0.42 * relief)
                    * math.sin(t * math.pi) ** 0.85
                )
                verts.append((x, y, h))
        for j in range(ny - 1):
            for i in range(nx - 1):
                k = j * nx + i
                faces.append((k, k + 1, k + nx + 1, k + nx))
        obj = mesh("Ridgeline " + str(band + 1), verts, faces, backdrop)
        mat, bsdf = basis("Atmospheric ridge " + str(band + 1), ink)
        bsdf.inputs["Roughness"].default_value = 1.0
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        noise = nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 0.05
        noise.inputs["Detail"].default_value = 5.0
        links.new(world_xyz(nodes, links), noise.inputs["Vector"])
        rock = nodes.new("ShaderNodeValToRGB")
        rock.color_ramp.elements[0].color = (*[v * 0.42 for v in ink], 1.0)
        rock.color_ramp.elements[1].color = (
            *[min(v * 1.45 + 0.014, 1.0) for v in ink],
            1.0,
        )
        links.new(noise.outputs["Fac"], rock.inputs[0])
        links.new(rock.outputs[0], bsdf.inputs["Base Color"])
        obj.data.materials.append(mat)
        for p in obj.data.polygons:
            p.use_smooth = True

    world = bpy.data.worlds.new("Amber sky")
    scene.world = world
    world.use_nodes = True
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    background = nodes.get("Background")
    background.inputs["Strength"].default_value = 0.65
    tex = nodes.new("ShaderNodeTexCoord")
    split = nodes.new("ShaderNodeSeparateXYZ")
    links.new(tex.outputs["Generated"], split.inputs["Vector"])
    sky = nodes.new("ShaderNodeValToRGB")
    sky.color_ramp.elements[0].color = (1.0, 0.74, 0.51, 1.0)
    sky.color_ramp.elements[1].color = (0.25, 0.44, 0.65, 1.0)
    mid = sky.color_ramp.elements.new(0.28)
    mid.color = (0.57, 0.68, 0.77, 1.0)
    links.new(split.outputs["Z"], sky.inputs["Fac"])
    stretch = nodes.new("ShaderNodeVectorMath")
    stretch.operation = "MULTIPLY"
    stretch.inputs[1].default_value = (3.0, 2.0, 8.0)
    links.new(tex.outputs["Generated"], stretch.inputs[0])
    cloud = nodes.new("ShaderNodeTexNoise")
    links.new(stretch.outputs[0], cloud.inputs["Vector"])
    cloud.inputs["Scale"].default_value = 2.45
    cloud.inputs["Detail"].default_value = 6.0
    mask = nodes.new("ShaderNodeValToRGB")
    mask.color_ramp.elements[0].position = 0.34
    mask.color_ramp.elements[1].position = 0.68
    mask.color_ramp.elements[1].color = (0.78, 0.78, 0.78, 1.0)
    links.new(cloud.outputs["Fac"], mask.inputs[0])
    blend = nodes.new("ShaderNodeMixRGB")
    blend.inputs[2].default_value = (1.25, 1.15, 0.96, 1.0)
    links.new(sky.outputs[0], blend.inputs[1])
    links.new(mask.outputs[0], blend.inputs[0])
    links.new(blend.outputs[0], background.inputs["Color"])
    rig = collection("Illustration / light and cameras")
    light = bpy.data.lights.new("Late afternoon key", "SUN")
    light.energy = 4.35
    light.color = (1.0, 0.78, 0.52)
    light.angle = math.radians(1.1)
    sun = bpy.data.objects.new("Sun", light)
    rig.objects.link(sun)
    sun.rotation_euler = Vector((0.78, 0.31, -0.34)).to_track_quat("-Z", "Y").to_euler()

    views = (
        ("overview", (84, -835, 341), (-3, 28, 69), 32.0, (1600, 1200)),
        ("card", (48, -18, 40), (89, 66, 52), 31.0, (720, 950)),
    )
    cameras = []
    for name, loc, aim, length, resolution in views:
        lens = bpy.data.cameras.new(name.title())
        lens.lens = length
        lens.clip_end = 8000.0
        camera = bpy.data.objects.new(name.title(), lens)
        rig.objects.link(camera)
        camera.location = loc
        camera.rotation_euler = (
            (Vector(aim) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
        )
        cameras.append((name, camera, resolution))
    scene.camera = cameras[0][1]
    scene.render.resolution_x, scene.render.resolution_y = cameras[0][2]
    scene.render.resolution_percentage = 100
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(
        filepath=str(output / "proving-ground.blend"), compress=True
    )
    for name, camera, res in cameras:
        scene.camera = camera
        scene.render.resolution_x, scene.render.resolution_y = res
        scene.render.filepath = str(output / (name + ".png"))
        bpy.ops.render.render(write_still=True)


def fractal_height(fractal, x, y, band):
    from mathutils import Vector

    return 0.52 * fractal(
        Vector((x * 0.0019, y * 0.0042, band + 0.39)), 1.0, 2.1, 5
    ) + 0.15 * math.sin(x * 0.009 + band)


if __name__ == "__main__":
    if "--" in sys.argv:
        paths = sys.argv[sys.argv.index("--") + 1 :]
        build(*(Path(p).resolve() for p in paths))
    else:
        p = argparse.ArgumentParser(description=__doc__)
        p.add_argument("--output", type=Path, required=True)
        p.add_argument("--blender", default="blender")
        a = p.parse_args()
        export(a.output.resolve(), a.blender)
