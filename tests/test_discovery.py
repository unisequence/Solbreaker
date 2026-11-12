"""Discovery keeps discovery packets and imported profiles on the chosen network."""
import ipaddress
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from solbreaker import discovery, dvpl
from solbreaker.build_info import BUILD_SHA256, VERSION
from solbreaker.connection import configure_lan, key_id
from solbreaker.network import load_network, parse_config


def lan_network():
    listen = dict(http='192.168.9.10:18117', login='192.168.9.10:20026', base='192.168.9.10:20027')
    advertise = dict(listen, http='http://'+listen['http'])
    return parse_config(dict(version=1, listen=listen, advertise=advertise,
                             services={'http': advertise['http']},
                             access={'client_networks': ['192.168.9.0/24']}))


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(65537, 2048).public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def test_response_is_same_size_and_only_sent_to_allowed_peers(self):
        responder = discovery.Responder(lan_network())
        transport = Mock()
        responder.connection_made(transport)
        query = discovery.MAGIC + bytes(discovery.NONCE_SIZE + 2)
        responder.datagram_received(query, ('192.168.9.20', 22123))
        transport.sendto.assert_called_once_with(query[:-2]+struct.pack('!H', 18117), ('192.168.9.20', 22123))
        responder.datagram_received(query, ('192.168.10.20', 22123))
        responder.datagram_received(query + b'x', ('192.168.9.20', 22123))
        self.assertEqual(transport.sendto.call_count, 1)

    def test_profile_must_have_a_valid_key_build_and_match_the_responder(self):
        profile = dict(version=1, game_version=VERSION, exe_sha256=BUILD_SHA256,
                       server_id=key_id(self.key), login_public_key=self.key.decode('ascii'),
                       server_name='Nearby server', advertise=lan_network().document()['advertise'])
        with patch.object(discovery.http.client, 'HTTPConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps(profile).encode()
            self.assertEqual(discovery.fetch_server('192.168.9.10', 18117).name, 'Nearby server')
            self.assertIsNone(discovery.fetch_server('192.168.9.11', 18117))
            for modified in (dict(profile, server_id='wrong'), dict(profile, game_version='wrong'),
                             dict(profile, advertise=dict(profile['advertise'], base='192.168.9.11:20027'))):
                response.read.return_value = json.dumps(modified).encode()
                self.assertIsNone(discovery.fetch_server('192.168.9.10', 18117))

    def test_tailscale_uses_known_ipv4_peers_and_local_address(self):
        state = dict(BackendState='Running', Self={'TailscaleIPs': ['100.64.1.10']},
                     Peer={'a': {'TailscaleIPs': ['100.64.2.20', 'fd7a::2', '192.168.1.5']}})
        with patch.object(discovery.shutil, 'which', return_value='tailscale'), \
             patch.object(discovery.subprocess, 'run', return_value=Mock(stdout=json.dumps(state))):
            self.assertEqual(discovery.tailscale_peers({'100.64.1.10'}), {'100.64.1.10': {'100.64.2.20'}})
            self.assertEqual(discovery.tailscale_peers({'100.64.1.11'}), {})
        self.assertEqual(discovery.interface_scope(ipaddress.IPv4Interface('100.64.1.10/32'),
                                                  'Tailscale'), '100.64.0.0/10')
        self.assertIsNone(discovery.interface_scope(ipaddress.IPv4Interface('26.5.1.10/8'), 'Radmin VPN'))

    def test_configuring_sharing_updates_the_managed_profile_and_preserves_ports(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root/'state'
            game = root/'game'
            game.mkdir()
            (state/'client/Data').mkdir(parents=True)
            (state/'private').mkdir()
            manifest = dict(game=str(game), client=str(state/'client'), version=VERSION,
                            exe_sha256=BUILD_SHA256, http_port=18117, login_port=20026)
            (state/'manifest.json').write_text(json.dumps(manifest))
            (state/'regions.yaml').write_text(yaml.safe_dump(
                {'Common': {}, 'Regions': {'Solbreaker': {'wgnwn': 'http://127.0.0.1:18117'}}}))
            (state/'client/Data/loginapp_wot.pubkey.dvpl').write_bytes(dvpl.encode(self.key))
            configure_lan(state, '192.168.9.10', '192.168.9.0/24')
            profile = json.loads((state/'connection.json').read_bytes())
            self.assertEqual(profile['server_id'], key_id(self.key))
            self.assertEqual(profile['advertise'], lan_network().document()['advertise'])
            self.assertFalse(load_network(state).allows('192.168.10.20'))

            configure_lan(state, '127.0.0.1', '127.0.0.0/8')
            restored = json.loads((state/'connection.json').read_bytes())
            self.assertEqual(restored['server_id'], profile['server_id'])
            self.assertEqual(restored['advertise']['base'], '127.0.0.1:20027')
