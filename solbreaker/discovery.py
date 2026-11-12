"""Find explicitly shared IPv4 servers without scanning a subnet."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import http.client
import ipaddress
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit

from cryptography.exceptions import UnsupportedAlgorithm

from .build_info import BUILD_SHA256, VERSION
from .connection import CONNECTION_PROFILE_MAX_BYTES, validate_connection_profile
from .network import PRIVATE, ipv4

PORT = 39068
MAGIC = b'Solbreaker-LAN\x01'
NONCE_SIZE = 16
MAX_SERVERS = 16
TAILNET = ipaddress.IPv4Network('100.64.0.0/10')


def discoverable_ip(value):
    try:
        address = ipaddress.IPv4Address(ipv4(value))
    except ValueError:
        return False
    return not address.is_loopback


class Responder(asyncio.DatagramProtocol):
    def __init__(self, network):
        self.network = network
        self.port = struct.pack('!H', urlsplit(network.advertise_http).port)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, packet, peer):
        if (len(packet) == len(MAGIC) + NONCE_SIZE + 2 and packet.startswith(MAGIC)
                and packet[-2:] == b'\x00\x00' and discoverable_ip(peer[0])
                and self.network.allows(peer[0])):
            # Keep the response the same size as the query. The actual profile
            # is fetched separately over HTTP after receiving this reply.
            self.transport.sendto(packet[:-2] + self.port, peer)


async def advertise(network):
    endpoint = urlsplit(network.advertise_http)
    if (network.version != 1 or endpoint.scheme != 'http' or not discoverable_ip(endpoint.hostname)
            or endpoint.hostname != network.advertise_login.host
            or endpoint.hostname != network.advertise_base.host
            or network.listen_http.host not in (endpoint.hostname, '0.0.0.0')
            or endpoint.port != network.listen_http.port):
        return None
    loop = asyncio.get_running_loop()
    # Receive directed broadcasts on Windows as well as Linux. Gameplay stays
    # bound to the selected address, and replies are limited by the server ACL.
    transport, _ = await loop.create_datagram_endpoint(
        lambda: Responder(network), local_addr=('0.0.0.0', PORT), allow_broadcast=True)
    return transport


@dataclass(frozen=True)
class DiscoveredServer:
    name: str
    address: str
    port: int
    server_id: str
    profile: bytes


def fetch_server(address, port):
    client = http.client.HTTPConnection(address, port, timeout=0.8)
    try:
        client.request('GET', '/tfs/connection/v1', headers={'Accept': 'application/json'})
        response = client.getresponse()
        if response.status != 200:
            return None
        profile = response.read(CONNECTION_PROFILE_MAX_BYTES + 1)
        if len(profile) > CONNECTION_PROFILE_MAX_BYTES:
            return None
        document = json.loads(profile)
        if not isinstance(document, dict) or document.get('version') != 1:
            return None
        network, _, _, name = validate_connection_profile(
            document, dict(version=VERSION, exe_sha256=BUILD_SHA256, http_port=18080, login_port=20016))
        endpoint = urlsplit(network.advertise_http)
        if (endpoint.scheme != 'http' or (endpoint.hostname, endpoint.port) != (address, port)
                or network.advertise_login.host != address or network.advertise_base.host != address):
            return None
        return DiscoveredServer(name, address, port, document['server_id'], profile)
    except (OSError, http.client.HTTPException, ValueError, TypeError, UnsupportedAlgorithm, RecursionError):
        return None
    finally:
        client.close()


def tailscale_peers(addresses):
    if not addresses:
        return {}
    executable = shutil.which('tailscale')
    if executable is None and sys.platform == 'win32':
        candidate = Path(os.environ.get('ProgramFiles', r'C:\Program Files'))/'Tailscale/tailscale.exe'
        if candidate.is_file():
            executable = str(candidate)
    if executable is None:
        return {}
    try:
        result = subprocess.run([executable, 'status', '--json'], capture_output=True, check=True,
                                timeout=2, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        status = json.loads(result.stdout)
        local = addresses.intersection(status['Self']['TailscaleIPs'])
        if status['BackendState'] != 'Running' or not local:
            return {}
        peers = set()
        for peer in (status.get('Peer') or {}).values():
            for address in peer.get('TailscaleIPs') or ():
                ip = ipaddress.ip_address(address)
                if ip.version == 4 and ip in TAILNET:
                    peers.add(str(ip))
                    if len(peers) == 128:
                        return {source: peers for source in local}
        return {source: peers for source in local}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, AttributeError, TypeError):
        return {}


def discover(interfaces, *, timeout=1.25):
    interfaces = tuple(interfaces)
    destinations = {}
    for interface in interfaces:
        if interface.network.prefixlen <= 30:
            destinations.setdefault(str(interface.ip), set()).add(str(interface.network.broadcast_address))
    tailscale = tailscale_peers({str(interface.ip) for interface in interfaces if interface.ip in TAILNET})
    for source, peers in tailscale.items():
        destinations.setdefault(source, set()).update(peers)

    prefix = MAGIC + secrets.token_bytes(NONCE_SIZE)
    query = prefix + b'\x00\x00'
    sockets = {}
    candidates = set()
    try:
        for source, targets in destinations.items():
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                probe.bind((source, 0))
            except OSError:
                probe.close()
                continue
            probe.setblocking(False)
            subnet = next(interface.network for interface in interfaces if str(interface.ip) == source)
            sockets[probe] = (subnet, tailscale.get(source, ()))
            for target in targets:
                try:
                    probe.sendto(query, (target, PORT))
                except OSError:
                    continue

        deadline = time.monotonic() + timeout
        while sockets and len(candidates) < MAX_SERVERS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select(sockets, (), (), remaining)
            for probe in ready:
                try:
                    packet, (address, source_port) = probe.recvfrom(len(query) + 1)
                except OSError:
                    continue
                subnet, peers = sockets[probe]
                if (source_port == PORT and len(packet) == len(query) and packet.startswith(prefix)
                        and discoverable_ip(address)
                        and (ipaddress.IPv4Address(address) in subnet or address in peers)):
                    port = struct.unpack('!H', packet[-2:])[0]
                    if 1024 <= port <= 65535:
                        candidates.add((address, port))
                        if len(candidates) == MAX_SERVERS:
                            break
    finally:
        for probe in sockets:
            probe.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = executor.map(lambda target: fetch_server(*target), candidates)
        return sorted((server for server in results if server is not None),
                      key=lambda server: (server.name.casefold(), server.address, server.port))


def interface_scope(interface, name):
    if interface.ip in TAILNET and 'tailscale' in name.casefold():
        return str(TAILNET)
    if any(interface.network.subnet_of(network) for network in PRIVATE):
        return str(interface.network)
    return None
