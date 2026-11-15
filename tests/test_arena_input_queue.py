"""Live BaseApp inputs share the arena timer's simulation budget."""
from collections import Counter, deque
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from solbreaker.arena_commands import ArenaCommand
from solbreaker.base import BaseEndpoint, ChannelOverflow


class ArenaInputQueueTests(TestCase):
    def setUp(self):
        self.endpoint = BaseEndpoint.__new__(BaseEndpoint)
        self.arena = SimpleNamespace(result_id='arena', pending_inputs=deque(),
            pending_input_counts=Counter(), retiring=False, closed=False, _input_only=False,
            players={i: SimpleNamespace(attached=True) for i in (1, 2)})
        self.endpoint.clients = {
            i: SimpleNamespace(arena_id='arena', account_id=i, phase='battle', reconnect_replay=False)
            for i in (1, 2)
        }

    def enqueue(self, account_id, action, *args):
        self.endpoint.queue_arena_input(account_id, self.endpoint.clients[account_id], self.arena,
                                        ArenaCommand(account_id, action, args, 100.))

    def test_tick_applies_fifo_without_entering_simulation(self):
        calls = []
        self.arena.aim_angles = lambda *args: calls.append(('aim', args, self.arena._input_only))
        self.arena.sandbox_control = lambda *args: calls.append(('sandbox', args, self.arena._input_only))
        self.arena.fire = lambda *args: calls.append(('fire', args, self.arena._input_only))
        self.enqueue(1, 'aim_angles', .2, .3)
        self.enqueue(2, 'sandbox', 'control')
        self.enqueue(1, 'fire')
        self.assertEqual(calls, [])
        self.endpoint._apply_arena_inputs(self.arena, float('inf'))
        self.assertEqual(calls, [('aim', (1, .2, .3), True),
                                 ('sandbox', (2, 'control'), True),
                                 ('fire', (1, 100.), True)])
        self.assertFalse(self.arena.pending_input_counts)
        self.assertFalse(self.arena._input_only)

    def test_budget_defers_work_and_replaced_session_cannot_fire(self):
        self.arena.fire = Mock()
        self.endpoint.INPUTS_PER_SESSION = 1
        self.enqueue(1, 'fire')
        with self.assertRaises(ChannelOverflow):
            self.enqueue(1, 'fire')
        self.enqueue(2, 'fire')
        with patch('solbreaker.base.time.monotonic', return_value=10.):
            self.endpoint._apply_arena_inputs(self.arena, 10.)
        self.assertEqual(len(self.arena.pending_inputs), 2)
        first = self.endpoint.clients[1]
        self.endpoint.clients[1] = SimpleNamespace(**vars(first))
        self.endpoint._apply_arena_inputs(self.arena, float('inf'))
        self.arena.fire.assert_called_once_with(2, 100.)
