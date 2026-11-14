"""Exercise authored data through the actual DAVA reader and package checks."""
from base64 import b64decode, b64encode
from dataclasses import replace
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from unittest import TestCase
from zipfile import ZipFile

import numpy as np

from solbreaker.aiming import GunAim
from solbreaker.workshop.dava_export import ClientScene
from solbreaker.workshop.glb import make_model
from solbreaker.workshop.models import POINT_NODE, TRACK_NAMES, check_model, model_preview, move_muzzle, move_visual_node, texture_dds
from solbreaker.workshop.mounting import check_mount, seat_mount
from solbreaker.workshop.package import export, unpack
from solbreaker.workshop.project import TankProject
from solbreaker.workshop.parts import catalog_matches
from solbreaker.workshop.running_gear import fit_to_model, fitted_parts, transform_suspension


class TankWorkshopTests(TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.project = TankProject(
            id='test.trainer', name='Учебный танк', visual_scene='visual.sc2',
            visual_geometry='visual.scg', collision_scene='collision.sc2',
            collision_geometry='collision.scg')
        triangle = [[0., 0., 0., 1., 0., 0., 0., 0.],
                    [0., 1., 0., 1., 0., 0., 1., 0.],
                    [0., 0., 1., 1., 0., 0., 0., 1.]]
        visible = {(part, 'material'): [triangle] for part in (
            'hull', 'turret_01', 'gun_01', 'chassis_track_L', 'chassis_track_R')}
        collision = {(part, 'armor'): [triangle] for part in ('hull', 'turret_01')}
        for label, parts, hit in (('visual', visible, False), ('collision', collision, True)):
            scene, geometry = make_model(parts, {}, {POINT_NODE: (0., 2., 1.)}, collision=hit)
            (self.root/f'{label}.sc2').write_bytes(scene)
            (self.root/f'{label}.scg').write_bytes(geometry)

    def test_edit_muzzle_and_export_installable_sources(self):
        scene, geometry = (self.root/'visual.sc2', self.root/'visual.scg')
        _, markers = check_model(self.project, self.root)
        raw_scene, raw_geometry = move_muzzle(scene.read_bytes(), geometry.read_bytes(),
                                             markers[POINT_NODE], (.2, 1.3, 3.))
        scene.write_bytes(raw_scene)
        geometry.write_bytes(raw_geometry)
        _, markers = model_preview(scene, geometry)
        np.testing.assert_allclose(markers[POINT_NODE], (.2, 1.3, 3.), atol=1e-6)

        self.project.save(self.root/'tank.toml')
        reopened = TankProject.open(self.root/'tank.toml')
        self.assertEqual(self.project.to_bytes(), reopened.to_bytes())
        output = self.root/'trainer.tank.zip'
        self.assertEqual(len(export(reopened, self.root, output)), 64)
        with unpack(output) as (project, root):
            self.assertEqual(reopened.to_bytes(), project.to_bytes())
            self.assertEqual((root/'visual.scg').read_bytes(), raw_geometry)
        with self.assertRaisesRegex(ValueError, 'new package'):
            export(reopened, self.root, output)

    def test_archive_cannot_add_undeclared_paths(self):
        output = self.root/'trainer.tank.zip'
        export(self.project, self.root, output)
        with ZipFile(output, 'a') as archive:
            archive.writestr('../escape', 'unexpected')
        with self.assertRaisesRegex(ValueError, 'unexpected or missing resources'):
            with unpack(output):
                self.fail('Unpacking an undeclared entry succeeded')
        self.assertFalse((self.root.parent/'escape').exists())
        with self.assertRaisesRegex(ValueError, 'inside the project'):
            replace(self.project, visual_scene='../asset.sc2').validate(complete=True)

    def test_stock_model_archive_contains_only_metadata(self):
        donors = {'hull': 'china/Ch01_Type59', 'turret': 'ussr/KV2', 'gun': 'usa/M4_Sherman'}
        project = TankProject(stock_model=True, base_vehicle='ussr/T-44',
                              part_donors=donors, running_gear='germany/PzVI')
        output = self.root/'stock.tank.zip'
        export(project, self.root, output)
        with ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {'tank.toml', 'build.json'})
        with unpack(output) as (restored, _):
            self.assertTrue(restored.stock_model)
            self.assertEqual(restored.base_vehicle, project.base_vehicle)
            self.assertEqual(restored.part_donors, donors)
            self.assertEqual(restored.running_gear, 'germany/PzVI')

    def test_donor_search_matches_name_nation_and_hyphen(self):
        self.assertEqual([entry[0] for entry in catalog_matches('T‑54 СССР')], ['ussr/T-54'])
        self.assertEqual([entry[0] for entry in catalog_matches('Tiger Германия')], ['germany/PzVI'])
        self.assertEqual(catalog_matches('Sherman СССР'), [])
        self.assertEqual([entry[0] for entry in catalog_matches('Object 140 USSR')], ['ussr/Object_140'])
        self.assertEqual([entry[0] for entry in catalog_matches('Cromwell United Kingdom')], ['uk/GB21_Cromwell'])

    def test_scene_fingerprint_position_preserves_extended_metadata(self):
        raw = (self.root/'visual.sc2').read_bytes()
        basic = ClientScene(raw)
        new = bytearray(basic.header)
        struct.pack_into('<I', new, 4, 48)
        struct.pack_into('<I', new, basic.geometry_fingerprint_offset-8, 20)
        new += b'12345678'
        scene = ClientScene(bytes(new)+raw[len(basic.header):], versions=(48,))
        offset = scene.geometry_fingerprint_offset
        self.assertEqual(scene.header[offset:offset+8], basic.header[-8:])
        self.assertEqual(scene.header[offset+8:], b'12345678')

    def test_visual_part_translation_moves_only_the_selected_mesh(self):
        scene, geometry = self.root/'visual.sc2', self.root/'visual.scg'
        parts, points = model_preview(scene, geometry)
        delta = np.array([.2, .3, -.1])
        raw_scene, raw_geometry = move_visual_node(scene.read_bytes(), geometry.read_bytes(), 'turret_01', delta)
        scene.write_bytes(raw_scene)
        geometry.write_bytes(raw_geometry)
        moved, markers = model_preview(scene, geometry)
        np.testing.assert_allclose(moved['turret_01'], parts['turret_01']+delta, atol=1e-6)
        np.testing.assert_array_equal(moved['gun_01'], parts['gun_01'])
        np.testing.assert_array_equal(markers[POINT_NODE], points[POINT_NODE])

    def test_turret_mount_checks_surfaces_and_moves_gun_with_turret(self):
        plate = np.array([[[-1., 0., -1.], [1., 0., -1.], [1., 0., 1.]],
                          [[-1., 0., -1.], [1., 0., 1.], [-1., 0., 1.]]])
        collision = {'hull': plate+(0., 1., 0.), 'turret_01': plate}
        points = dict(hull=(0., 0., 0.), turret=(0., 1.25, 0.), gun=(0., 1.5, .5), muzzle=(0., 1.5, 2.))
        project = replace(self.project, points=points)
        result = check_mount(None, project, 'turret', collision)
        self.assertEqual(result.state, 'gap')
        self.assertAlmostEqual(result.offset, -.25)
        self.assertTrue(seat_mount(None, project, 'turret', collision))
        self.assertEqual(check_mount(None, project, 'turret', collision).state, 'seated')
        self.assertEqual(project.points['gun'], (0., 1.25, .5))
        self.assertEqual(project.points['muzzle'], (0., 1.25, 2.))
        project.points['turret'] = (0., .75, 0.)
        self.assertEqual(check_mount(None, project, 'turret', collision).state, 'overlap')
        project.points['turret'] = (0., 3., 0.)
        self.assertFalse(seat_mount(None, project, 'turret', collision))
        self.assertEqual(check_mount(None, project, 'gun', collision).state, 'unknown')

    def test_donor_archive_can_use_a_model_without_authored_tracks(self):
        triangle = [[0., 0., 0., 1., 0., 0., 0., 0.],
                    [0., 1., 0., 1., 0., 0., 1., 0.],
                    [0., 0., 1., 1., 0., 0., 0., 1.]]
        visible = {(part, 'material'): [triangle] for part in ('hull', 'turret_01', 'gun_01')}
        scene, geometry = make_model(visible, {}, {POINT_NODE: (0., 2., 1.)}, collision=False)
        (self.root/'visual.sc2').write_bytes(scene)
        (self.root/'visual.scg').write_bytes(geometry)
        with self.assertRaisesRegex(ValueError, 'choose running gear'):
            check_model(self.project, self.root)

        project = replace(self.project, running_gear='ussr/T-54')
        project.gear_fit['shift'] = .25
        output = self.root/'donor.tank.zip'
        export(project, self.root, output)
        with unpack(output) as (installed, sources):
            self.assertEqual(installed.running_gear, 'ussr/T-54')
            self.assertEqual(installed.gear_fit['shift'], .25)
            self.assertEqual((sources/'visual.sc2').read_bytes(), scene)
            self.assertEqual(set(check_model(installed, sources)[0]), {'hull', 'turret_01', 'gun_01'})

    def test_running_gear_fit_preserves_the_body_and_matches_track_envelope(self):
        left = np.array([[[-2., 0., -3.], [-1., 1., 3.], [-2., 1., -3.]]])
        right = left + (3., 0., 0.)
        donor = dict(zip(TRACK_NAMES, (left, right)))
        original = {key: value*(.8, 1.2, 1.1)+(0., .15, -.3) for key, value in donor.items()}
        original['hull'] = np.ones((1, 3, 3))
        fitted = fitted_parts(original, donor, fit_to_model(original, donor))
        self.assertIs(fitted['hull'], original['hull'])
        for key in TRACK_NAMES:
            np.testing.assert_allclose(fitted[key], original[key], atol=1e-7)

    def test_suspension_fit_keeps_chain_indices_and_contact_flags(self):
        chain = b64encode(struct.pack('<ffI', 2., 3., 1)).decode() + '\f'
        wheels = b64encode(struct.pack('<Iff', 7, .5, -.25)).decode()
        raw = {'leftTrackChains': {0: chain}, 'rightTrackChain': chain, 'wheels': wheels}
        settings = dict(width=1., height=2., length=3., shift=.5, lift=-.25)
        fitted = transform_suspension({'suspension': raw}, settings)
        self.assertEqual(set(fitted['leftTrackChains']), {'00'})
        self.assertEqual(fitted['leftTrackChains']['00'], fitted['rightTrackChain'])
        self.assertEqual(struct.unpack('<ffI', b64decode(fitted['rightTrackChain'])), (6.5, 5.75, 1))
        self.assertEqual(struct.unpack('<Iff', b64decode(fitted['wheels'])), (7, 1., -.5))
        self.assertEqual(raw['leftTrackChains'], {0: chain})

    def test_projectile_origin_follows_turret_yaw_and_gun_pitch(self):
        gun = GunAim()
        gun.TURRET = gun.GUN = (0., 0., 0.)
        gun.muzzle_offset = (.5, 0., 2.)
        gun.yaw, gun.pitch = np.pi/2, -np.pi/6
        origin, direction = gun.ray((1., 2., 3.), 0.)
        np.testing.assert_allclose(origin, (1.+np.sqrt(3), 3., 2.5))
        np.testing.assert_allclose(direction, (np.sqrt(3)/2, .5, 0.), atol=1e-12)

    def test_png_compiles_into_a_mipmapped_dds(self):
        import struct
        try:
            from PySide6.QtGui import QImage
        except ImportError:
            self.skipTest('Optional GUI dependencies are not installed')
        image = QImage(16, 8, QImage.Format.Format_RGBA8888)
        image.fill(0xff447722)
        source = self.root/'albedo.png'
        self.assertTrue(image.save(str(source)))
        dds = texture_dds(source)
        self.assertEqual(dds[:4], b'DDS ')
        self.assertEqual(struct.unpack_from('<III', dds, 12), (8, 16, 64))
        self.assertEqual(struct.unpack_from('<I', dds, 28), (5,))
        self.assertEqual(len(dds), 148 + 4*(128+32+8+2+1))
