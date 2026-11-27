"""Private provision slots in the native battle HUD."""
import struct
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from solbreaker.arena import Arena
from solbreaker.battle import event_messages, reconnect_message_parts
from solbreaker.consumables import ConsumableState, resolve_consumable


class BattleProvisionTests(unittest.TestCase):
    def test_accelerated_aiming_applies_accuracy_only_until_expiry(self):
        game_item = SimpleNamespace(tag='fireControlSystem', ident=28, script='FireControlSystem',
                                    parameters=(('aimingTimeFactor', '0.7'), ('cooldown', '55'),
                                                ('duration', '20'), ('shotDispersionAngleFactor', '0.7')))
        with patch('solbreaker.consumables.load_catalog', return_value=SimpleNamespace(consumables=(game_item,))):
            spec = resolve_consumable('.', game_item.tag)
        arena = Arena([dict(id=1, nickname='Owner')], b'', SimpleNamespace(height=lambda x, z: 0.),
                      now=1., solo=True)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        tank = arena.vehicle_for(1)
        shell = SimpleNamespace(compact_id=tank.shell_id, speed=750.)
        tank.definition = SimpleNamespace(weapon=SimpleNamespace(shells=(shell,)),
                                          aiming=SimpleNamespace(dispersion_radius=.4, aiming_time=2.,
                                                                 after_shot_dispersion=3.))
        tank.aiming_time_factor, tank.shot_dispersion_factor = 1.5, 2.
        state = ConsumableState(spec)
        tank.consumables = (state,)
        rates = tank.gun.yaw_rate, tank.gun.pitch_rate
        before = arena.aim_marker(tank, 1.)[2]

        self.assertTrue(arena.use_consumable(1, spec.ident, now=1.))
        self.assertEqual((tank.gun.yaw_rate, tank.gun.pitch_rate), rates)
        self.assertAlmostEqual(arena.aim_marker(tank, 1.)[2], before*.7)
        tank.last_shot_at = 1.
        expected = before*.7*(1. + 2.*math.exp(-1. / (2.*1.5*.7*.5)))
        self.assertAlmostEqual(arena.shot_dispersion(tank, 2.), expected)
        arena._consumables(tank, state.active_until)
        self.assertAlmostEqual(arena.aim_marker(tank, state.active_until)[2],
                               before*(1. + 2.*math.exp(-20. / (2.*1.5*.5))))

    def test_ready_and_reconnect_deliver_sparse_provisions_only_to_the_owner(self):
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena([dict(id=1, nickname='Owner'), dict(id=2, nickname='Peer')],
                      b'', terrain, now=1., solo=False)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        events = arena.drain_events()
        own = arena.vehicle_for(1)
        # ID zero (ration_other) is distinct from an empty provision slot.
        own.definition = SimpleNamespace(aiming=None, weapon=None, provisions=(None, 0, 19))
        expected = tuple(
            b'\x13\x4c' + struct.pack('<BIHBhh', 13, cd, present, present, 0, 0)
            for cd, present in ((0, 0), (0xfd, 1), (0x13fd, 1)))
        owner, peer = arena.players[1], arena.players[2]
        self.assertIn(b''.join(expected), event_messages(arena, owner, events))
        self.assertNotIn(b'\x13\x4c\x0d', event_messages(arena, peer, events))
        reconnect = reconnect_message_parts(arena, owner)
        index = reconnect.index(expected[0])
        self.assertEqual(reconnect[index:index+3], expected)
        self.assertFalse(any(part.startswith(b'\x13\x4c\x0d')
                             for part in reconnect_message_parts(arena, peer)))
