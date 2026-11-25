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
from solbreaker.connection import configure_lan, connect_client, key_id, validate_connection_profile
from solbreaker.management_tasks import refresh_lan_profile
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

    def test_xchat_reserves_tcp_port_after_base(self):
        for section in ('listen', 'advertise'):
            with self.subTest(section=section):
                document = lan_network().document()
                document[section]['base'] = '192.168.9.10:65535'
                with self.assertRaisesRegex(ValueError, 'adjacent XChat port'):
                    parse_config(document)
        document = lan_network().document()
        document['listen']['http'] = '0.0.0.0:20028'
        with self.assertRaisesRegex(ValueError, 'same TCP socket'):
            parse_config(document)
        document['listen']['http'] = '192.168.9.11:20028'
        self.assertEqual(parse_config(document).listen_http.port, 20028)

    def test_launch_finds_same_server_after_its_address_changes(self):
        selected = dict(version=1, game_version=VERSION, exe_sha256=BUILD_SHA256,
                        server_id=key_id(self.key), login_public_key=self.key.decode('ascii'),
                        advertise=lan_network().document()['advertise'])
        moved = dict(selected, advertise={key: value.replace('192.168.9.10', '192.168.9.12')
                                         for key, value in selected['advertise'].items()})
        other = discovery.DiscoveredServer('Other', '192.168.9.10', 18117, 'other-key', b'{}')
        same = discovery.DiscoveredServer('Same', '192.168.9.12', 18117, selected['server_id'],
                                          json.dumps(moved).encode())
        manifest = dict(version=VERSION, exe_sha256=BUILD_SHA256, http_port=18117, login_port=20026)
        with (patch.object(discovery, 'fetch_server', return_value=other),
              patch.object(discovery, 'local_interfaces', return_value=[]),
              patch.object(discovery, 'discover', return_value=[other, same]) as discover):
            self.assertEqual(refresh_lan_profile(Path('.'), manifest, selected), moved)
            discover.return_value = [other]
            with self.assertRaisesRegex(ValueError, 'другой сервер'):
                refresh_lan_profile(Path('.'), manifest, selected)

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

    def test_supported_vpn_scopes_require_matching_adapters(self):
        for address, name, expected in (
                ('26.5.1.10/8', 'Radmin VPN', '26.0.0.0/8'),
                ('26.5.1.10/32', 'Ethernet Famatech Radmin VPN Ethernet Adapter', '26.0.0.0/8'),
                ('25.5.1.10/32', 'ham0 ham0', '25.0.0.0/8'),
                ('25.5.1.10/24', 'Hamachi', '25.5.1.0/24'),
                ('26.5.1.10/8', 'Wi-Fi', None),
                ('25.5.1.10/8', 'Radmin VPN', None),
                ('26.5.1.10/7', 'Radmin VPN', None),
                ('8.8.8.1/24', 'Radmin VPN', None),
                ('192.168.10.6/24', 'ZeroTier One', '192.168.10.0/24')):
            with self.subTest(address=address, adapter=name):
                self.assertEqual(discovery.interface_scope(ipaddress.IPv4Interface(address), name), expected)

    def test_windows_uses_driver_description_for_renamed_vpn_adapter(self):
        from PySide6.QtNetwork import QNetworkInterface

        def adapter(index, name, ip, flags):
            device = Mock()
            device.index.return_value = index
            device.name.return_value = f'{{device-{index}}}'
            device.humanReadableName.return_value = name
            device.flags.return_value = flags
            address = Mock()
            address.ip.return_value.toString.return_value = ip
            address.prefixLength.return_value = 32
            device.addressEntries.return_value = [address]
            return device

        up = QNetworkInterface.IsUp | QNetworkInterface.IsRunning
        devices = [
            adapter(8, 'Ethernet 3', '26.5.1.10', QNetworkInterface.IsUp),
            adapter(10, 'Ethernet 4', '26.6.1.10', up),
            adapter(11, 'Ethernet 5', '25.5.1.10', up),
            adapter(12, 'Offline Radmin VPN', '26.8.1.10', QNetworkInterface.IsUp),
        ]
        with (patch.object(QNetworkInterface, 'allInterfaces', return_value=devices),
              patch.object(discovery.sys, 'platform', 'win32'),
              patch.object(discovery, '_windows_adapter_descriptions', return_value={
                  8: 'Famatech RadminVPN Ethernet Adapter',
                  10: 'Intel Ethernet Adapter',
                  11: 'LogMeIn Hamachi Virtual Ethernet Adapter',
              }) as descriptions):
            result = discovery.local_interfaces()
        self.assertEqual([(name, str(interface), scope) for name, interface, scope in result], [
            ('Famatech RadminVPN Ethernet Adapter', '26.5.1.10/8', '26.0.0.0/8'),
            ('LogMeIn Hamachi Virtual Ethernet Adapter', '25.5.1.10/8', '25.0.0.0/8'),
        ])
        descriptions.assert_called_once()

    def test_overlay_probe_uses_the_vpn_source_and_accepts_the_profile(self):
        for octet, name in ((26, 'Radmin VPN'), (25, 'Hamachi')):
            with self.subTest(adapter=name):
                host = f'{octet}.2.3.4'
                source = f'{octet}.2.3.5'
                interface = ipaddress.IPv4Interface(f'{source}/8')
                advertise = dict(http=f'http://{host}:18117', login=f'{host}:20026', base=f'{host}:20027')
                profile = dict(version=1, game_version=VERSION, exe_sha256=BUILD_SHA256,
                               server_id=key_id(self.key), login_public_key=self.key.decode('ascii'),
                               advertise=advertise)
                with (patch.object(discovery.http.client, 'HTTPConnection') as factory,
                      patch.object(discovery, 'local_interfaces', return_value=[]) as interfaces):
                    self.assertIsNone(discovery.fetch_server(host, 18117))
                    factory.assert_not_called()
                    interfaces.return_value = [(name, interface, str(interface.network))]
                    response = factory.return_value.getresponse.return_value
                    response.status = 200
                    response.read.return_value = json.dumps(profile).encode()
                    self.assertEqual(discovery.fetch_server(host, 18117).address, host)
                    factory.assert_called_once_with(host, 18117, timeout=0.8, source_address=(source, 0))
                manifest = dict(version=VERSION, exe_sha256=BUILD_SHA256, http_port=18117, login_port=20026)
                network, *_ = validate_connection_profile(profile, manifest)
                self.assertTrue(network.allows(host))
                self.assertFalse(network.allows(source))

    def test_public_config_requires_explicit_hosts_for_overlay_ranges(self):
        document = lan_network().document()
        document['version'] = 2
        document['advertise']['http'] = document['advertise']['http'].replace('http:', 'https:')
        document['access']['client_networks'].append('26.0.0.0/8')
        with self.assertRaisesRegex(ValueError, 'Client networks'):
            parse_config(document)

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

            for host, subnet, peer in (('26.5.1.10', '26.0.0.0/8', '26.5.1.11'),
                                       ('25.5.1.10', '25.0.0.0/8', '25.5.1.11')):
                with self.subTest(subnet=subnet):
                    configure_lan(state, host, subnet)
                    configured = json.loads((state/'connection.json').read_bytes())
                    self.assertEqual(configured['server_id'], profile['server_id'])
                    self.assertEqual(configured['advertise']['http'], f'http://{host}:18117')
                    self.assertEqual(configured['advertise']['login'], f'{host}:20026')
                    network = load_network(state)
                    self.assertEqual(network.listen_base.pair, (host, 20027))
                    self.assertTrue(network.allows(peer))
                    self.assertFalse(network.allows('8.8.8.8'))

            configure_lan(state, '127.0.0.1', '127.0.0.0/8')
            restored = json.loads((state/'connection.json').read_bytes())
            self.assertEqual(restored['server_id'], profile['server_id'])
            self.assertEqual(restored['advertise']['base'], '127.0.0.1:20027')

            first = connect_client(state, state/'connection.json')
            second = connect_client(state, state/'connection.json')
            self.assertTrue(first['restart_required'])
            self.assertFalse(second['restart_required'])
