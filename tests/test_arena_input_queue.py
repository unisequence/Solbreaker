"""Live BaseApp inputs share the arena timer's simulation budget."""
from collections import Counter, deque
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from solbreaker.arena_commands import ArenaCommand
from solbreaker.arena_owner import AddressedPayload, Command, MotionPayload, Result
from solbreaker.arena_owner_transport import ArenaOwnerTransport
from solbreaker.base import BaseEndpoint, ChannelOverflow
from solbreaker.motion import tick_sync


class ArenaClockTests(TestCase):
    def test_invalid_time_does_not_interrupt_the_next_fixed_steps(self):
        from solbreaker.arena import Arena

        players = [dict(id=1, nickname='Driver')]
        terrain = SimpleNamespace(height=lambda x, z: 0.)
        arena = Arena(players, b'', terrain, solo=True, now=1.)
        self.addCleanup(arena.close)
        arena.ready(1, 1.)
        for invalid in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    Arena(players, b'', terrain, solo=True, now=invalid)
                previous = arena.last_tick, arena.accumulator, arena.dropped_time, arena.step_count
                with self.assertRaises(ValueError):
                    arena.step(invalid)
                self.assertEqual((arena.last_tick, arena.accumulator, arena.dropped_time,
                                  arena.step_count), previous)
                arena.step(previous[0] + .1)
                self.assertEqual(arena.step_count, previous[3] + 6)


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

    def test_inline_arena_does_not_send_events_to_loading_client(self):
        self.endpoint.send = Mock()
        self.endpoint.clients[1].battle_spawned = False
        self.endpoint.clients[2].battle_spawned = True
        self.endpoint.clients[2].result_sent = False
        self.arena.results = lambda: ()
        self.arena.reports = {}
        with patch('solbreaker.base.event_messages', return_value=b'equipment') as messages:
            self.endpoint._flush_arena_results(self.arena, ())
        messages.assert_called_once_with(self.arena, self.arena.players[2], ())
        self.endpoint.send.assert_called_once_with(2, self.endpoint.clients[2], b'equipment')

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


class OwnerMotionDeliveryTests(IsolatedAsyncioTestCase):
    async def test_vehicle_updates_wait_for_spawn_but_leave_can_reset_loading_client(self):
        clients = {i: SimpleNamespace(arena_id='arena', account_id=i, phase='battle',
                    account_created=True, battle_spawned=i == 2, last_sync_tick=7) for i in (1, 2)}
        endpoint = SimpleNamespace(clients=clients, send=Mock())
        updates = (AddressedPayload(1, b'pending equipment'), AddressedPayload(2, b'live equipment'))
        step = Result(1, b'', updates, arena_id='arena')
        leave = Result(2, b'', (*updates, AddressedPayload(1, b'\x04\0')), arena_id='arena')
        route = SimpleNamespace(arena_id='arena', close=AsyncMock(),
                                request=AsyncMock(side_effect=(step, leave)))
        transport = ArenaOwnerTransport(endpoint, route)
        self.addAsyncCleanup(transport.close)

        await transport.submit_nowait(Command('step', 10., 0))
        endpoint.send.assert_called_once_with(2, clients[2], b'live equipment')

        endpoint.send.reset_mock()
        await transport.submit_client(1, clients[1], Command('leave', 11., 1))
        self.assertEqual([call.args for call in endpoint.send.call_args_list],
                         [(2, clients[2], b'live equipment'), (1, clients[1], b'\x04\0')])

    async def test_step_delivers_events_before_motion_and_checks_session_after_reply(self):
        clients = {i: SimpleNamespace(arena_id='arena', account_id=i, phase='battle',
                    account_created=True, battle_spawned=True, last_sync_tick=7) for i in (1, 2)}
        endpoint = SimpleNamespace(clients=clients, send=Mock())
        owner, departed = clients.values()
        route = SimpleNamespace(arena_id='arena', close=AsyncMock())

        async def request(command):
            self.assertEqual(command.op, 'step')
            self.assertEqual(command.motion_viewers, ((1, 7), (2, 7)))
            endpoint.clients[2] = SimpleNamespace(**vars(departed))
            return Result(1, b'', (AddressedPayload(1, b'event'), AddressedPayload(2, b'stale event')),
                          arena_id='arena', motions=(MotionPayload(1, 8, b'live'), MotionPayload(2, 8, b'stale')))

        route.request = AsyncMock(side_effect=request)
        transport = ArenaOwnerTransport(endpoint, route)
        self.addAsyncCleanup(transport.close)
        await transport.submit_nowait(Command('step', 10., 0))
        route.request.assert_awaited_once()
        self.assertEqual([(call.args[0], call.args[2], call.kwargs) for call in endpoint.send.call_args_list],
                         [(1, b'event', {}), (1, tick_sync(8)+b'live', {'reliable': False})])
        self.assertEqual(owner.last_sync_tick, 8)
        self.assertEqual(departed.last_sync_tick, 7)
