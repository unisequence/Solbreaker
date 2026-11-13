"""Private provision slots in the native battle HUD."""
import struct
from types import SimpleNamespace
import unittest

from solbreaker.arena import Arena
from solbreaker.battle import event_messages, reconnect_message_parts


class BattleProvisionTests(unittest.TestCase):
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
