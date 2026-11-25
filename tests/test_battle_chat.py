import asyncio
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from solbreaker.base import BaseEndpoint, ClientSession
from solbreaker.battle_chat import (ACTION_KIND, MARKER_KIND, BattleChat, REQUEST_HEADER,
                                    TEAM_CHANNEL, COMMON_CHANNEL, TEXT_ACTION, TEXT_COMMAND)
from solbreaker.mercury import parse_packet, encode_packet, packed_blob


class BattleChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.endpoint = BaseEndpoint.__new__(BaseEndpoint)
        self.endpoint.server = Mock()
        self.endpoint.send = Mock()
        self.endpoint.owner_service = Mock()
        self.endpoint.battle_chat = BattleChat(self.endpoint)
        self.endpoint.clients = {
            (1, number): ClientSession(b'key', number, phase='battle', account_id=number,
                                       arena_id='here' if number < 4 else 'elsewhere',
                                       account_created=True, battle_spawned=True)
            for number in range(1, 5)
        }
        players = {
            1: SimpleNamespace(team=1, nickname='Sender', vehicle_id=101),
            2: SimpleNamespace(team=1, nickname='Ally'),
            3: SimpleNamespace(team=2, nickname='Enemy'),
            4: SimpleNamespace(team=1, nickname='Elsewhere'),
        }
        self.arena = SimpleNamespace(players=players, vehicles={101: object()},
                                     owner_backed=True)
        self.endpoint.arenas = {'here': self.arena, 'elsewhere': self.arena}

    def input(self, command=26, target=0, channel=TEAM_CHANNEL, message=b''):
        body = REQUEST_HEADER.pack(1, command, channel, target, 0, 0) + packed_blob(message) + b'\0'
        self.packet(ACTION_KIND, body)

    def packet(self, kind, body):
        packet = parse_packet(encode_packet(
            struct.pack('<BH', kind, len(body)) + body))
        self.endpoint.handle_elements((1, 1), self.endpoint.clients[(1, 1)], packet)

    async def test_quick_command_is_deferred_and_reaches_only_its_team(self):
        self.input()
        self.endpoint.send.assert_not_called()
        self.endpoint.owner_service.submit.assert_not_called()
        await asyncio.sleep(0)
        send_calls = self.endpoint.send.call_args_list
        self.assertEqual([call.args[0] for call in send_calls], [(1, 1), (1, 2)])
        payload = send_calls[0].args[2]
        self.assertEqual(payload[:2], b'\x13\x6a')
        self.assertEqual(payload[12], 20)
        self.assertEqual(payload, send_calls[1].args[2])

    async def test_invalid_target_and_spam_do_not_reach_the_arena(self):
        self.input(command=29, target=1000)
        self.input(command=28, target=100)
        self.input(command=255)
        self.input()
        for _ in range(1000):
            self.input()
        await asyncio.sleep(0)
        self.assertEqual(self.endpoint.send.call_count, 2)
        self.endpoint.owner_service.submit.assert_not_called()

    async def test_minimap_cell_and_vehicle_target_reach_allies(self):
        for command, target in ((28, 5), (29, 101)):
            self.endpoint.clients[(1, 1)].battle_chat_after = 0
            self.endpoint.send.reset_mock()
            self.input(command=command, target=target)
            self.endpoint.send.assert_not_called()
            await asyncio.sleep(0)
            self.assertEqual([call.args[0] for call in self.endpoint.send.call_args_list],
                             [(1, 1), (1, 2)])
        self.endpoint.owner_service.submit.assert_not_called()

    async def test_ground_marker_reaches_only_allies_and_skips_simulation(self):
        marker = bytes.fromhex('0a110a0f0de508b542155db3eac21d245ffb41')
        invalid = bytes((10, 7, 10, 5)) + struct.pack('<Bf', 13, float('nan'))
        self.packet(MARKER_KIND, b'\0' * 4 + packed_blob(invalid))
        self.packet(MARKER_KIND, b'\0' * 4 + packed_blob(marker))
        self.endpoint.send.assert_not_called()
        self.endpoint.owner_service.submit.assert_not_called()
        await asyncio.sleep(0)
        send_calls = self.endpoint.send.call_args_list
        self.assertEqual([call.args[0] for call in send_calls], [(1, 1), (1, 2)])
        payload = struct.pack('<IQ', 101, 1) + packed_blob(marker)
        self.assertEqual(send_calls[0].args[2], bytes((19, 105, len(payload))) + payload)

    async def test_common_chat_delivers_utf8_only_inside_its_battle(self):
        self.input(TEXT_COMMAND, channel=COMMON_CHANNEL, message=b'\xff')
        self.input(TEXT_COMMAND, channel=COMMON_CHANNEL, message=b' '*12)
        message = 'Проверка, блин!'.encode()
        self.input(TEXT_COMMAND, channel=COMMON_CHANNEL, message=message)
        await asyncio.sleep(0)
        self.assertEqual([call.args[0] for call in self.endpoint.send.call_args_list],
                         [(1, 1), (1, 2), (1, 3)])
        payload = self.endpoint.send.call_args.args[2]
        self.assertEqual(payload[12], TEXT_ACTION)
        self.assertIn(message, payload)
        self.endpoint.owner_service.submit.assert_not_called()

    async def test_only_text_can_use_the_common_channel(self):
        self.input(channel=COMMON_CHANNEL)
        await asyncio.sleep(0)
        self.endpoint.send.assert_not_called()
