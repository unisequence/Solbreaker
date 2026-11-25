import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import socket
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from solbreaker.base import BaseEndpoint
from solbreaker.benchmark_report import build_html, convert, html_output
from solbreaker.cipher import decrypt_packet, encrypt_packet
from solbreaker.datagram import SourceDatagramTransport, open_datagram
from solbreaker.mercury import base_elements, encode_packet, parse_packet
from solbreaker.server_benchmark import (_control_offsets, _wait_for_battle, MotionSample,
                                         Peer, Timeline, _verdict, durations, tank_count)


class ServerBenchmarkTests(unittest.TestCase):
    def test_timeline_records_windowed_cpu_memory_steps_and_latency(self):
        arena = SimpleNamespace(result_id='arena', step_count=100)
        endpoint = SimpleNamespace(ticks=[], input_wait={'arena': []})
        with (patch('solbreaker.server_benchmark.time.monotonic', side_effect=(10, 10.8, 11.2, 11.7)),
              patch('solbreaker.server_benchmark.time.process_time', side_effect=(2, 2.6, 2.8)),
              patch('solbreaker.server_benchmark.owner_cpu_times', side_effect=({7: 8}, {7: 9.2}, {})),
              patch('solbreaker.server_benchmark.process_memory', side_effect=((40, 50), (60, 70),
                                                                             (None, None), (65, 70)))):
            timeline = Timeline(endpoint, [arena], [7])
            timeline.sample([])
            self.assertEqual(timeline.rows, [])
            arena.step_count += 72
            endpoint.ticks.extend((.002, .003))
            endpoint.input_wait['arena'].append(.017)
            timeline.sample([.004])
            arena.step_count += 29
            timeline.sample([.004, .006], final=True)
        first, last = timeline.rows
        self.assertEqual(first['steps_per_second'], [60])
        self.assertEqual(first['server_cpu_cores'], .5)
        self.assertEqual(first['worker_cpu_cores'], 1)
        self.assertEqual(first['worker_rss_bytes_sum'], 40)
        self.assertEqual(first['server_rss_bytes'], 60)
        self.assertEqual(first['tick_p99_ms'], 3)
        self.assertEqual(first['input_p99_ms'], [17])
        self.assertEqual(last['loop_delay_p99_ms'], 6)
        self.assertEqual(last['input_p99_ms'], [None])
        self.assertIsNone(last['worker_cpu_cores'])
        self.assertIsNone(last['worker_rss_bytes_sum'])

    def test_report_preserves_legacy_windows_and_escapes_data(self):
        window = {'elapsed_seconds': 10, 'steps_per_second': [60],
                  'label': '</script><script>alert(1)</script>'}
        data = {'schema': 1, 'complete': True, 'version': '<0.3>',
                'runs': [{'map': '<b>map</b>', 'scenario': 'bots', 'verdict': 'ok',
                          'arenas': [{'steps_per_second': 60}], 'windows': [window]}]}
        html = build_html(data)
        self.assertIn('1 of 1 runs passed', html)
        self.assertIn('&lt;b&gt;map&lt;/b&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('"legacy": true', html)
        self.assertIn('"steps_per_second": [60]', html)
        self.assertNotIn('<script src=', html)
        self.assertNotIn('<link ', html)
        with TemporaryDirectory() as folder:
            source = Path(folder) / 'results.json'
            source.write_text(json.dumps(data))
            self.assertEqual(convert(source), html_output(source))
            self.assertEqual(html_output(source).read_text(), html)
            with self.assertRaises(FileExistsError):
                convert(source)
            self.assertEqual(json.loads(source.read_text()), data)

    def test_spread_offsets_interleave_arenas(self):
        left, right = (SimpleNamespace(result_id=name) for name in ('left', 'right'))
        offsets = _control_offsets([(left,), (left,), (right,), (right,)])
        self.assertEqual(sorted(offsets, key=offsets.get), [0, 2, 1, 3])

    def test_nearest_rank_keeps_spikes_in_small_samples(self):
        self.assertEqual(durations([.001, .001, .080])['p99_ms'], 80)
        self.assertIsNone(durations([])['p99_ms'])

    def test_overload_is_distinct_from_stalled_motion_and_server_faults(self):
        row = {'steps_per_second': 60, 'dropped_seconds': 0, 'input_overflows': 0,
               'nonfinite_samples': 0, 'controls_sent': 300, 'controls_queued': 300,
               'input_queue': durations([.010]), 'motion_gap': durations([.033])}
        self.assertEqual(_verdict([row], {}), 'ok')
        self.assertEqual(_verdict([row, dict(row, steps_per_second=55)], {}), 'overloaded')
        self.assertEqual(_verdict([dict(row, input_overflows=1)], {}), 'overloaded')
        self.assertEqual(_verdict([dict(row, controls_queued=290)], {}), 'overloaded')
        self.assertEqual(_verdict([dict(row, motion_gap=durations([.140]))], {}), 'stalls')
        self.assertEqual(_verdict([row], {'missing_test_clients': 1}), 'fault')

    def test_tank_count_respects_scenario_and_map_capacity(self):
        self.assertEqual(tank_count('solo', 7, None), 2)
        self.assertEqual(tank_count('bots', 5, None), 10)
        self.assertEqual(tank_count('players', 7, 6), 6)
        with self.assertRaises(ValueError):
            tank_count('bots', 5, 14)
        with self.assertRaises(ValueError):
            tank_count('solo', 7, 4)

    def test_peer_sends_controls_and_acks_as_normal_loopback_packets(self):
        sent = []
        peer = Peer(SimpleNamespace(key=b'benchmark'))
        peer.server_address = ('127.0.0.1', 23456)
        peer.connection_made(SimpleNamespace(sendto=lambda data, address: sent.append((data, address))))
        peer.controls(5, SimpleNamespace(pose=SimpleNamespace(position=(2., 3., 4.))))
        remote = encrypt_packet(encode_packet(b'', sequence=8), peer.cipher.key)
        peer.datagram_received(remote, peer.server_address)
        packets = [parse_packet(decrypt_packet(data[4:], peer.cipher.key)) for data, _ in sent]
        self.assertEqual([kind for kind, _, _ in base_elements(packets[0])], [145, 149, 138])
        self.assertEqual(packets[1].acks, (8,))
        self.assertEqual([address for _, address in sent], [peer.server_address] * 2)

    def test_persistent_depth_requires_consecutive_in_bounds_samples(self):
        sample = MotionSample()
        terrain = SimpleNamespace(MIN=0, MAX_X=10, MIN_Z=0, MAX_Z=10, height=lambda x, z: 5)
        pose = SimpleNamespace(position=(2, 3, 2), pitch=0, yaw=0, roll=0)
        vehicle = SimpleNamespace(alive=True, pose=pose)
        arena = SimpleNamespace(terrain=terrain, vehicles={1: vehicle})
        sample.observe(arena, 0)
        sample.observe(arena, .2)
        pose.position = (11, 0, 2)
        sample.observe(arena, .3)
        pose.position = (2, 2, 2)
        sample.observe(arena, .8)
        self.assertEqual(sample.report(), {'nonfinite_samples': 0, 'below_heightmap_samples': 3,
                                          'max_below_heightmap_m': 3,
                                          'max_below_heightmap_seconds': .2})

class BattleMotionCacheTests(unittest.TestCase):
    def test_only_same_team_shares_world_bytes_and_owner_bytes_are_separate(self):
        endpoint = SimpleNamespace(_motion_world_cache={})
        frame = object()
        arena = SimpleNamespace(result_id='arena')
        players = [SimpleNamespace(team=team, vehicle_id=vehicle)
                   for team, vehicle in [(1, 3), (1, 4), (2, 5)]]

        def encode_world(arena, frame, *, team):
            return bytes([team])

        def encode_player(arena, player, frame, *, world_messages=None):
            return world_messages + bytes([player.vehicle_id])

        with (patch('solbreaker.base.world_motion_messages', side_effect=encode_world) as world,
              patch('solbreaker.base.motion_messages', side_effect=encode_player)):
            updates = [BaseEndpoint._battle_motion_messages(endpoint, arena, player, frame)
                       for player in players]
        self.assertEqual(updates, [b'\x01\x03', b'\x01\x04', b'\x02\x05'])
        self.assertEqual(world.call_count, 2)


@unittest.skipUnless(sys.platform == 'linux', 'Linux readiness batching')
class BenchmarkDatagramTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_base_socket_receives_burst_and_replies(self):
        loop = asyncio.get_running_loop()
        received = loop.create_future()
        packets = []

        class Echo(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, address):
                packets.append(data)
                if len(packets) == 80:
                    self.transport.sendto(data, address)
                    received.set_result(None)

        transport, _ = await open_datagram(Echo, ('127.0.0.1', 0), batch_receive=True)
        self.addAsyncCleanup(asyncio.sleep, 0)
        self.addCleanup(transport.close)
        self.assertIsInstance(transport, SourceDatagramTransport)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.setblocking(False)
            address = transport.get_extra_info('sockname')
            expected = [bytes([index]) for index in range(80)]
            for data in expected:
                peer.sendto(data, address)
            await asyncio.wait_for(received, timeout=2)
            response, source = await asyncio.wait_for(loop.sock_recvfrom(peer, 128), timeout=2)
        self.assertEqual(packets, expected)
        self.assertEqual((response, source), (expected[-1], address))

class MetadataPreloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_map_decode_waits_until_live_arena_finishes(self):
        arena = SimpleNamespace(finished_at=0, closed=False)
        endpoint = SimpleNamespace(map_catalog={2: 'map'}, map_specs={}, dev_map_specs={},
                                   arena_owners_enabled=True, map_spec_errors={}, game='game',
                                   arenas=SimpleNamespace(arenas={'battle': arena}),
                                   server=SimpleNamespace(event=Mock()))
        spec = object()

        async def finish_battle(_):
            self.assertEqual(endpoint.map_specs, {})
            arena.finished_at = 1

        with (patch('solbreaker.base.asyncio.sleep', side_effect=finish_battle) as pause,
              patch('solbreaker.base.asyncio.to_thread', new=AsyncMock(return_value=spec)) as decode):
            await BaseEndpoint._preload_map_metadata(endpoint)
        pause.assert_awaited_once()
        decode.assert_awaited_once()
        self.assertEqual(endpoint.map_specs, {2: spec})

class BenchmarkReadyTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_last_arena_before_starting_the_load(self):
        ready = SimpleNamespace(result_id='first', started=True)
        loading = SimpleNamespace(result_id='last', started=False)
        endpoint = SimpleNamespace(arena_faults={}, arenas={'first': ready, 'last': loading})

        async def finish_loading(interval):
            self.assertEqual(interval, .025)
            loading.started = True

        with patch('solbreaker.server_benchmark.asyncio.sleep', side_effect=finish_loading) as pause:
            await _wait_for_battle(endpoint, [ready, loading], float('inf'))
        pause.assert_awaited_once()

    async def test_disappearing_arena_fails_before_load(self):
        loading = SimpleNamespace(result_id='failed', started=False)
        endpoint = SimpleNamespace(arena_faults={}, arenas={})
        with self.assertRaisesRegex(RuntimeError, 'did not start'):
            await _wait_for_battle(endpoint, [loading], float('inf'))
