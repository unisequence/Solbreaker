import struct
import unittest

from solbreaker.dava_proving_scene import _entity, _shift, _Table, _u
from solbreaker.dava_scene import Archive, components
from solbreaker.proving_ground_landscape_detail import ground_cover


class FoliageTests(unittest.TestCase):
    def test_ground_cover_follows_height_and_avoids_non_grass(self):
        def surface(x, y):
            return 1 if -140 < x < 120 and 125 < y < 260 else 0

        def height(x, y):
            self.assertEqual(surface(x, y), 1)
            return 34.0 + 0.03 * x

        meshes = list(ground_cover(surface, height))
        self.assertTrue(meshes)
        self.assertFalse(any(mesh['solid'] for mesh in meshes))
        for mesh in meshes:
            a, b, tip = mesh['vertices'][:3]
            self.assertAlmostEqual((a[2]+b[2])/2, height((a[0]+b[0])/2, (a[1]+b[1])/2)-.035)
            self.assertGreater(tip[2], a[2])

    def test_relocated_tree_keeps_discrete_lods_and_forgets_donor_visibility_cell(self):
        transform = dict.fromkeys(
            ('tc.localTranslation', 'tc.worldTranslation'), (12, struct.pack('<3f', 1., 2., 3.))
        )
        tree = _entity('tree', 2, [
            ('TransformComponent', transform), ('LodTransitionComponent', {}),
            ('SpeedTreeComponent', {}), ('LodComponent', {'distance': _u(50)}),
            ('RenderComponent', {'rc.renderObj': (8, {'ro.sOclIndex': _u(17)})}),
        ])[1]
        child = _entity('leaf', 3, [
            ('TransformComponent', transform),
            ('RenderComponent', {'rc.renderObj': (8, {'ro.sOclIndex': _u(23)})}),
        ])
        tree['#hierarchy'] = 27, [child]
        _shift(tree, (10., 20., 30.), iter((100, 101)))

        decoded = Archive(_Table().encode(tree)).load()
        leaf, = decoded['#hierarchy']
        parts = components(decoded)
        self.assertIn('SpeedTreeComponent', parts)
        self.assertEqual(parts['LodComponent']['distance'], 50)
        self.assertNotIn('LodTransitionComponent', parts)
        self.assertEqual(components(leaf)['TransformComponent']['tc.localTranslation'], (1., 2., 3.))
        for node in (decoded, leaf):
            entries = node['components']
            self.assertEqual(set(entries), {'count'} | {f'{i:04}' for i in range(entries['count'])})
            self.assertEqual(components(node)['RenderComponent']['rc.renderObj']['ro.sOclIndex'], 65535)
            self.assertEqual(components(node)['TransformComponent']['tc.worldTranslation'], (11., 22., 33.))
