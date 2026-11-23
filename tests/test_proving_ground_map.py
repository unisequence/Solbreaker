import base64
import gzip
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageDraw

from solbreaker import dvpl, proving_ground_layout as ground
from solbreaker.connection import ensure_lab
from solbreaker.dava_proving_scene import _mesh
from solbreaker.dava_scene import Archive
from solbreaker.proving_ground_map import BUNDLE, KEY, _marked_catalog, _sound_registration, install
from solbreaker.proving_ground_signs import sign_meshes
from tools.build_proving_ground import battle_loading_sprite


class PolygonReader(Archive):
    """Remember on-wire scalar tags independently of Python's int conversion."""
    def value(self, kind, version):
        value = super().value(kind,version)
        if kind == 4:
            self.previous_string = value
        elif kind in (2,7):
            self.types[self.previous_string] = kind
        return value


class GroundResourceTests(unittest.TestCase):
    def test_installation_parent_alias_does_not_block_server_or_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = root/'installation/data'
            client = state/'client'
            (client/'Data').mkdir(parents=True)
            (state/'private').mkdir()
            game = root/'original'
            game.mkdir()
            bundle = root/'bundle'
            bundle.touch()
            alias = root/'alias'
            try:
                alias.symlink_to(state.parent, target_is_directory=True)
            except OSError as error:
                if getattr(error, 'winerror', None) != 1314:
                    raise
                self.skipTest('Windows symlink creation requires privileges')

            manifest = {'client': str(alias/'data/client'), 'game': str(game)}
            (state/'manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(ensure_lab(alias/'data')[0], state)
            install(alias/'data', manifest, bundle=bundle)

            foreign = root/'other-client'
            foreign.symlink_to(client, target_is_directory=True)
            manifest['client'] = str(foreign)
            (state/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'separate prepared lab client'):
                ensure_lab(alias/'data')
            with self.assertRaisesRegex(ValueError, 'private Solbreaker installation'):
                install(alias/'data', manifest, bundle=bundle)

            manifest['client'] = str(alias/'data/client')
            (state/'manifest.json').write_text(json.dumps(manifest))
            (client/'Data').rmdir()
            (client/'Data').symlink_to(game, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'separate prepared lab client'):
                ensure_lab(alias/'data')
            with self.assertRaisesRegex(ValueError, 'private Solbreaker installation'):
                install(alias/'data', manifest, bundle=bundle)

    def test_native_polygon_counts_are_int32(self):
        item = dict(name='floor',vertices=[[0,0,0],[4,0,0],[0,3,0]],
                    triangles=[[0,1,2]],color=[.7,.7,.7,1])
        _,_,_,pg = _mesh(item,0,{})
        r = PolygonReader(pg)
        r.types = {}
        fields = r.load()
        self.assertEqual(fields['vertexCount'],3)
        self.assertEqual(len(r.types),9)
        self.assertEqual(set(r.types.values()),{2})

    def test_ford_uses_animated_water_on_a_noncolliding_mesh(self):
        item = dict(name='sb_ford',vertices=[[0,0,1],[4,0,1],[0,3,1]],
                    triangles=[[0,1,2]],color=[.2,.4,.4,1],water=True,solid=False)
        parent = (10,struct.pack('<Q',123))
        node,mat,_,chunk = _mesh(item,0,{},parent)
        fields = Archive(chunk).load()
        self.assertEqual((fields['vertexFormat'],fields['textureCoordCount']),(0x9b,2))
        self.assertEqual(len(fields['vertices']),fields['vertexCount']*52)
        self.assertEqual(mat[1]['parentMaterialKey'],parent)
        components = node[1]['components'][1]
        self.assertNotIn('CollisionTypeComponent',[f['comp.typename'][1] for k,f in components.values() if k==8])

    def test_private_row_and_audio_preserve_other_modes(self):
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            game, private = parent/'game.dvpl',parent/'private.dvpl'
            original = {'maps':{'rudniki':dict(id=6,tags='rd0',localName='06_rudniki_rd/06_rudniki_rd.sc2'),
                                'user_map':dict(id=100)}}
            game.write_bytes(dvpl.encode(yaml.safe_dump(original).encode()))
            private.write_bytes(_marked_catalog(private,game))
            rows = yaml.safe_load(dvpl.read(private))['maps']
            self.assertEqual(rows['user_map'],original['maps']['user_map'])
            self.assertEqual(rows[KEY]['id'],107)
            self.assertNotIn('tags',rows[KEY])
            self.assertIsNone(_marked_catalog(private,game))
            private.unlink()
            sounds = {'ambient':{'rudniki':'wind'},'music_play':{'rudniki':'track'},'sounds_general':{'a':1}}
            game.write_bytes(dvpl.encode(yaml.safe_dump(sounds).encode()))
            private.write_bytes(_sound_registration(private,game))
            patched = yaml.safe_load(dvpl.read(private))
            self.assertEqual(patched['ambient'][KEY],'wind')
            self.assertEqual(patched['music_play'][KEY],'track')
            self.assertEqual(patched['sounds_general'],{'a':1})
            self.assertIsNone(_sound_registration(private,game))

    def test_baked_bundle_contains_only_original_inputs(self):
        layout = json.loads(gzip.decompress(BUNDLE.read_bytes()))
        self.assertTrue(all(obj['name'].startswith('sb_') for obj in layout['objects']))
        paths = layout['resources']
        self.assertFalse(any(path.endswith(('.scg','.sc2','.lka','.dvpl')) for path in paths))
        self.assertEqual(sum(path.endswith('.heightmap') for path in paths),1)
        self.assertTrue(any('MiniMapSmall@2x' in path for path in paths))
        for layer,size in (('detail',512),('color',4096)):
            key = next(name for name in paths if f'solbreaker_range_{layer}.dx11.pvr' in name)
            header = base64.b64decode(paths[key][:72])
            self.assertEqual(header[8:16], b'rgba'+bytes([8])*4)
            self.assertEqual(struct.unpack_from('<II',header,24),(size,size))
        self.assertFalse(any(obj.get('marking') for obj in layout['objects']))
        color = next(data for name, data in paths.items() if name.endswith('range_color.dx11.pvr'))
        pixels = np.frombuffer(base64.b64decode(color), dtype=np.uint8, count=4096**2 * 4, offset=52)
        pixels = pixels.reshape(4096, 4096, 4)
        centerline = np.asarray(ground._main_track())
        x = ((centerline[:, 0] + 512) * 4).astype(int)
        y = ((512 - centerline[:, 1]) * 4).astype(int)
        self.assertGreater(np.count_nonzero(pixels[y, x, 0] > 145), 35)

    def test_main_track_is_continuous_on_the_surface_map(self):
        path = np.asarray(ground._main_track())
        np.testing.assert_array_equal(path[0], path[-1])
        segments = np.diff(path, axis=0)
        lengths = np.linalg.norm(segments, axis=1)
        self.assertGreater(lengths.sum(), 1500)
        normals = np.column_stack((-segments[:, 1], segments[:, 0])) / lengths[:, None]
        centers = path[:-1] + np.linspace(0, 1, 9)[:, None, None] * segments
        # Check the lane interior on both sides of every spline joint.
        for size in (1024, 2048):
            mask = np.asarray(ground._materials(size))
            for off in (-7, -4, 0, 4, 7):
                points = (centers + off * normals).reshape(-1, 2)
                x = ((512 + points[:, 0]) * size / 1024).astype(int)
                y = ((512 - points[:, 1]) * size / 1024).astype(int)
                self.assertTrue(np.all(mask[y, x] == 0))

    def test_roadside_posts_do_not_stand_on_asphalt(self):
        mask = np.asarray(ground._materials(1024))
        for mesh in sign_meshes(ground.elevation):
            if mesh.get('solid', True) and mesh['name'] != 'sb_waypost_01_steel':
                for x, y, _ in mesh['vertices']:
                    self.assertNotEqual(mask[int(512 - y), int(512 + x)], 0, mesh['name'])

    def test_ring_keeps_a_gentle_grade_and_level_shoulder(self):
        points = np.asarray(ground._main_track())
        distance = np.r_[0., np.linalg.norm(np.diff(points, axis=0), axis=1).cumsum()]
        samples = np.arange(0., distance[-1])
        east, north = [np.interp(samples, distance, p) for p in points.T]
        height = ground.elevation(east, north)
        self.assertLess(np.max(np.abs(np.diff(height))), .11)
        self.assertLess(np.max(np.abs(np.diff(height, n=2))), .06)
        dx, dy = np.gradient(east), np.gradient(north)
        left = ground.elevation(east - 8 * dy, north + 8 * dx)
        right = ground.elevation(east + 8 * dy, north - 8 * dx)
        self.assertLess(np.max(np.abs(left - right)), .25)

    def test_quarry_office_foundation_is_supported_across_its_footprint(self):
        x, y = ground.QUARRY_OFFICE
        east, north = np.meshgrid(x + np.arange(-12., 13.), y + np.arange(-5., 6.))
        footprint = ground.elevation(east, north)
        self.assertLess(np.max(np.abs(footprint - ground.elevation(x, y))), .10)

    def test_finish_gantry_spans_the_suspension_circuit(self):
        center, tangent = ground._circuit_finish()
        across = np.array((-tangent[1], tangent[0]))
        mask = np.asarray(ground._materials(1024))
        self.assertEqual(mask[int(512 - center[1]), int(512 + center[0])], 0)
        meshes = {mesh['name']: np.asarray(mesh['vertices'])
                  for mesh in ground.geometry()['objects']
                  if mesh['name'].startswith('sb_circuit_finish_')}
        steel = meshes['sb_circuit_finish_steel']
        feet = steel[steel[:, 2] < ground.elevation(*center) + 1.]
        np.testing.assert_allclose(sorted((feet[:, :2] - center) @ across)[::4],
                                   [-12.275, 11.725], atol=.002)
        self.assertTrue(np.all(mask[(512 - feet[:, 1]).astype(int),
                                   (512 + feet[:, 0]).astype(int)] != 0))
        plaque = meshes['sb_circuit_finish_chalk']
        np.testing.assert_allclose(plaque[:, :2].mean(axis=0), center, atol=.002)
        self.assertLess(plaque[:, 2].max(), meshes['sb_circuit_finish_rust'][:, 2].min())

    def test_loading_background_preserves_shape_in_native_four_by_three_panel(self):
        image = Image.new('RGB', (1600, 1200))
        ImageDraw.Draw(image).ellipse((600, 400, 999, 799), fill='white')
        with Image.open(io.BytesIO(battle_loading_sprite(image))) as sprite:
            displayed = sprite.convert('L').resize(image.size, Image.Resampling.LANCZOS)
        left, top, right, bottom = displayed.point(lambda p: 255 if p >= 128 else 0).getbbox()
        self.assertAlmostEqual(right - left, bottom - top, delta=2)

    def test_render_mask_blends_only_the_material_boundary(self):
        hard = np.asarray(ground.terrain_mask(2048))
        soft = np.asarray(ground.terrain_mask(2048, blended=True))
        self.assertGreater(np.count_nonzero((soft > 0) & (soft < 255)), 10000)
        self.assertGreater(np.mean(np.argmax(soft, axis=2) == np.argmax(hard, axis=2)), .999)
