"""Exercise the local batch transport with an actual spawned socket owner."""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from solbreaker.arena_owner import (AddressedPayload, AsyncArenaOwner, Command,
                                  CommandRejected, HandlerOutput, OwnerError)
from solbreaker.arena_owner_registry import ArenaRoute
from solbreaker.arena_owner_transport import ArenaOwnerTransport


class Handler:
    def __init__(self):
        self.operations = []

    def preflight(self, command):
        if command.account_id == 2:
            raise CommandRejected('test rejection')

    def apply(self, command):
        self.operations.append(command.op)
        return HandlerOutput('test', ','.join(self.operations).encode(), ())

    def close(self):
        pass


class EventHandler(Handler):
    def apply(self, command):
        output = super().apply(command)
        events = (AddressedPayload(command.account_id, output.state),)
        return HandlerOutput(output.arena_id, output.state, events)


class OwnerBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = patch.dict(os.environ, SOLBREAKER_OWNER_IPC='socket')
        self.context.start()
        self.addCleanup(self.context.stop)
        os.environ.pop('SOLBREAKER_OWNER_INPUT_BATCH', None)
        owner = await AsyncArenaOwner.create(Handler)
        self.route = ArenaRoute('test', owner, limit=64)
        self.addAsyncCleanup(self.route.close)

    async def test_socket_preserves_sequences_across_preflight_rejection(self):
        results = await self.route.request_input_batch((
            Command('control', 1., 1, b'\x01\x00'),
            Command('fire', 10., 2),
            Command('fire', 3., 1),
        ))
        self.assertEqual([row.sequence for row in results], [1, 2, 3])
        self.assertIsInstance(results[1], CommandRejected)
        self.assertEqual(results[2].state, b'control,fire')
        step = await self.route.request(Command('step', 4., 0))
        self.assertEqual((step.sequence, step.state), (4, b'control,fire,step'))

    async def test_transport_keeps_steps_as_barriers(self):
        client = SimpleNamespace(arena_id='test', account_id=1, phase='battle',
                                 account_created=True, battle_spawned=True, last_sync_tick=None)
        endpoint = SimpleNamespace(clients={('test', 1): client})
        transport = ArenaOwnerTransport(endpoint, self.route)
        self.addAsyncCleanup(transport.close)
        batches = []
        original = self.route.request_input_batch

        async def counted(rows):
            batches.append(tuple(row.op for row in rows))
            return await original(rows)

        self.route.request_input_batch = counted
        commands = (
            Command('control', 1., 1, b'\x01\x00'),
            Command('fire', 2., 1),
            Command('step', 3., 0),
            Command('control', 4., 1, b'\x00\x00'),
            Command('fire', 5., 1),
        )
        futures = [transport.submit_client(('test', 1), client, command)
                   if command.account_id else transport.submit_nowait(command)
                   for command in commands]
        results = await asyncio.gather(*futures)
        self.assertEqual(batches, [('control', 'fire'), ('control', 'fire')])
        self.assertEqual([row.sequence for row in results], list(range(1, 6)))
        self.assertEqual(results[2].state, b'control,fire,step')
        self.assertEqual(results[4].state, b'control,fire,step,control,fire')

    async def test_stale_session_is_rejected_before_transfer(self):
        address = ('test', 1)
        client = SimpleNamespace(arena_id='test', account_id=1, phase='battle',
                                 account_created=True, battle_spawned=True, last_sync_tick=None)
        endpoint = SimpleNamespace(clients={address: client})
        transport = ArenaOwnerTransport(endpoint, self.route)
        self.addAsyncCleanup(transport.close)
        pending = [transport.submit_client(address, client, Command('fire', at, 1))
                   for at in (1., 2.)]
        endpoint.clients.clear()
        for future in pending:
            with self.assertRaises(CommandRejected):
                await future
        step = await transport.submit_nowait(Command('step', 3., 0))
        self.assertEqual((step.sequence, step.state), (1, b'step'))

    async def test_batch_does_not_expand_queue_capacity(self):
        address = ('test', 1)
        client = SimpleNamespace(arena_id='test', account_id=1, phase='battle',
                                 account_created=True, battle_spawned=True, last_sync_tick=None)
        endpoint = SimpleNamespace(clients={address: client})
        transport = ArenaOwnerTransport(endpoint, self.route, limit=3)
        self.addAsyncCleanup(transport.close)
        submitted, release = asyncio.Event(), asyncio.Event()
        request = self.route.request_input_batch

        async def block_batch(commands):
            submitted.set()
            await release.wait()
            return await request(commands)

        self.route.request_input_batch = block_batch
        pair = [transport.submit_client(address, client, Command('fire', at, 1))
                for at in (1., 2.)]
        await asyncio.wait_for(submitted.wait(), 5.)
        self.assertEqual(transport._batch_reserved, 1)
        more = [transport.submit_nowait(Command('step', at, 0)) for at in (3., 4.)]
        self.assertEqual(transport._queue.qsize() + transport._batch_reserved, 3)
        release.set()
        results = await asyncio.gather(*pair, *more)
        self.assertEqual([row.sequence for row in results], list(range(1, 5)))

    async def test_replaced_session_receives_no_output_from_an_accepted_prefix(self):
        owner = await AsyncArenaOwner.create(EventHandler)
        route = ArenaRoute('test', owner, limit=64)
        self.addAsyncCleanup(route.close)
        address = ('test', 1)
        client = SimpleNamespace(arena_id='test', account_id=1, phase='battle',
                                 account_created=True, battle_spawned=True, last_sync_tick=None)
        replacement = SimpleNamespace(**vars(client))
        sent = []
        endpoint = SimpleNamespace(clients={address: client},
                                   send=lambda addr, session, body: sent.append((addr, session, body)))
        disconnected = []

        def delivered(command, result):
            if result.sequence == 1:
                endpoint.clients[address] = replacement
                disconnected.append(transport.submit_nowait(Command('disconnect', 3., 1)))

        transport = ArenaOwnerTransport(endpoint, route, on_delivered=delivered)
        self.addAsyncCleanup(transport.close)
        commands = [transport.submit_client(address, client, Command('fire', at, 1))
                    for at in (1., 2.)]
        first, second = await asyncio.gather(*commands)
        final, = await asyncio.gather(*disconnected)
        self.assertEqual([r.sequence for r in (first, second, final)], [1, 2, 3])
        self.assertIs(sent[0][1], client)
        self.assertIs(sent[1][1], replacement)
        self.assertEqual([entry[2] for entry in sent], [b'fire', b'fire,fire,disconnect'])
        with self.assertRaises(ValueError):
            transport.submit_client(address, client, Command('fire', 4., 1))

    async def test_delivery_failure_quarantines_the_batch_without_retry(self):
        address = ('test', 1)
        client = SimpleNamespace(arena_id='test', account_id=1, phase='battle',
                                 account_created=True, battle_spawned=True, last_sync_tick=None)
        endpoint = SimpleNamespace(clients={address: client})
        attempts = []

        def delivered(command, result):
            attempts.append(result.sequence)
            if result.sequence == 2:
                raise RuntimeError('test publication failed')

        transport = ArenaOwnerTransport(endpoint, self.route, on_delivered=delivered)
        self.addAsyncCleanup(transport.close)
        futures = [transport.submit_client(address, client, Command('fire', at, 1))
                   for at in (1., 2., 3.)]
        outcomes = await asyncio.gather(*futures, return_exceptions=True)
        await transport.close()
        self.assertEqual(outcomes[0].sequence, 1)
        self.assertTrue(all(isinstance(item, OwnerError) for item in outcomes[1:]))
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(transport.undelivered_result[1].sequence, 2)
        self.assertIsNotNone(transport.fault)

    async def test_socket_opt_out_keeps_individual_requests(self):
        address = ('test', 1)
        client = SimpleNamespace(arena_id='test', account_id=1, phase='battle',
                                 account_created=True, battle_spawned=True)
        endpoint = SimpleNamespace(clients={address: client})
        with patch.dict(os.environ, SOLBREAKER_OWNER_INPUT_BATCH='0'):
            transport = ArenaOwnerTransport(endpoint, self.route)
        self.addAsyncCleanup(transport.close)
        with patch.object(self.route, 'request_input_batch', side_effect=AssertionError('unexpected batch')):
            first, second = await asyncio.gather(
                transport.submit_client(address, client, Command('fire', 1., 1)),
                transport.submit_client(address, client, Command('fire', 2., 1)),
            )
        self.assertEqual([first.sequence, second.sequence], [1, 2])
        self.assertEqual(second.state, b'fire,fire')

    async def test_queue_backend_uses_individual_requests(self):
        with patch.dict(os.environ, SOLBREAKER_OWNER_IPC='queue'):
            owner = await AsyncArenaOwner.create(Handler)
        route = ArenaRoute('test', owner, limit=64)
        self.addAsyncCleanup(route.close)
        address = ('test', 1)
        client = SimpleNamespace(arena_id='test', account_id=1, phase='battle',
                                 account_created=True, battle_spawned=True, last_sync_tick=None)
        endpoint = SimpleNamespace(clients={address: client})
        transport = ArenaOwnerTransport(endpoint, route)
        self.addAsyncCleanup(transport.close)
        futures = [transport.submit_client(address, client, Command('fire', at, 1))
                   for at in (1., 2.)]
        first, second = await asyncio.gather(*futures)
        self.assertFalse(route.supports_input_batch)
        self.assertEqual([first.sequence, second.sequence], [1, 2])
        self.assertEqual(second.state, b'fire,fire')


if __name__ == '__main__':
    unittest.main()
