"""Local configuration host and LoginApp protocol endpoint."""
import asyncio
import ipaddress
import os
from collections import Counter
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import struct
import secrets
import time
import re
import socket
import signal
import sys
import threading
from urllib.parse import urlsplit, parse_qs

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .mercury import parse_packet, login_elements, encode_reply, packed_blob, read_blob
from .auth import LocalAccounts
from .resource_source import validate_snapshot
from .cipher import crypt
from .base import BaseEndpoint
from .localization import additional_labels
from .market import market_response
from .network import load_network
from .connection import regions_config, connection_profile, CONNECTION_PROFILE_MAX_BYTES
from .datagram import open_datagram
from .revision import implementation_revision
from .catalog import SHOP_REVISION

LOG = logging.getLogger("solbreaker")


class LocalServer:
    def __init__(self, lab: Path):
        from .diagnostics import install
        self.diagnostics = install()
        self.lab = lab
        self.developer_mode = os.environ.get("SOLBREAKER_DEVELOPER_MODE") == "1"
        self.manifest = json.loads((lab / "manifest.json").read_text())
        from .server_identity import server_name
        self.server_name = server_name(self.manifest)
        from .training_config import effective_training_config, training_config_path
        self.training_config = effective_training_config(self.manifest, training_config_path(lab))
        self.training_bot = self.training_config.bots_enabled
        from .recon_config import requested_enabled
        self.recon_enabled = requested_enabled()
        if self.manifest.get('client_only'):
            raise ValueError('This is a client-only lab; start the configured remote server separately')
        self.network = load_network(lab,self.manifest)
        from .tls import server_context
        self.http_tls_context = server_context(lab, self.manifest, self.network)
        validate_snapshot(self.manifest)
        from .sim_native import backend_status
        from .drive_workers import configured_workers
        self.simulation_backend = backend_status()
        # Capture the resolved startup setting.  A later launcher must not
        # mistake an already-running server for one started with its own env.
        self.simulation_workers = configured_workers()
        owner_setting = os.environ.get('SOLBREAKER_ARENA_OWNERS', '0')
        if owner_setting not in ('0', '1'):
            raise ValueError('SOLBREAKER_ARENA_OWNERS must be 0 or 1')
        self.arena_owners = owner_setting == '1'
        from .owner_channel import configured_ipc
        self.owner_ipc = configured_ipc()
        self.dynamic_backend = os.environ.get('SOLBREAKER_DYNAMIC_BACKEND', 'bullet')
        if self.dynamic_backend not in ('bullet', 'rapier'):
            raise ValueError('SOLBREAKER_DYNAMIC_BACKEND must be bullet or rapier')
        parallel = os.environ.get('SOLBREAKER_RAPIER_PARALLEL_DRIVE', '0')
        if parallel not in ('0', '1'):
            raise ValueError('SOLBREAKER_RAPIER_PARALLEL_DRIVE must be 0 or 1')
        self.rapier_parallel_drive = parallel == '1'
        self.rapier_queries = os.environ.get('SOLBREAKER_RAPIER_QUERIES', 'bullet')
        if self.rapier_queries not in ('bullet', 'shadow', 'rapier'):
            raise ValueError('SOLBREAKER_RAPIER_QUERIES must be bullet, shadow or rapier')
        if self.rapier_queries != 'bullet' and self.dynamic_backend != 'rapier':
            raise ValueError('SOLBREAKER_RAPIER_QUERIES requires SOLBREAKER_DYNAMIC_BACKEND=rapier')
        import yaml
        regions = yaml.safe_load((lab / "regions.yaml").read_bytes())
        regions = regions_config(regions,self.network)
        self.regions = yaml.safe_dump(regions, sort_keys=False, allow_unicode=True).encode()
        self.key = serialization.load_pem_private_key((lab / "private/login.pem").read_bytes(), None)
        self.counters = Counter()
        self.base = None
        self.logins = {}
        self.base_port = self.network.listen_base.port
        profile = connection_profile(lab)
        if profile['advertise'] != self.network.document()['advertise']:
            raise ValueError('Connection profile does not match the server network')
        self.server_id = profile['server_id']
        self.implementation_revision = implementation_revision(lab)
        public = self.key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
        import hashlib
        if hashlib.sha256(public).hexdigest()!=self.server_id:
            raise ValueError('Lab client public key does not match the server LoginApp key')
        # Public-key export only: build once, never hash/read game resources or
        # disclose account/session/private-key state during an HTTP request.
        self.connection_profile_json = json.dumps(profile, separators=(',', ':')).encode('utf-8')
        if len(self.connection_profile_json) > CONNECTION_PROFILE_MAX_BYTES:
            raise ValueError('Connection profile is too large')
        autocreate = self.manifest.get('account_autocreate', True)
        if type(autocreate) is not bool:
            raise ValueError('account_autocreate must be boolean')
        self.accounts = LocalAccounts(lab / "private", self.network.advertise_http,
                                      autocreate=autocreate)

    def event(self, name, **fields):
        self.counters[name] += 1
        recorder = getattr(self, 'diagnostics', None)
        if recorder is not None:
            allowed = {'arena_id', 'account_id', 'vehicle_id', 'map_id', 'map_key', 'phase',
                       'duration_ms', 'simulation_steps', 'dropped_seconds', 'owner', 'winner',
                       'finish_reason', 'kind', 'target', 'actor', 'step', 'operation'}
            recorder.record('server_event', name=name,
                            fields={key: value for key, value in fields.items() if key in allowed})
        level = logging.DEBUG if name in {"base_packet", "base_element", "account_request", "avatar_request",
                                         "training_motion", "training_input"} else logging.INFO
        # Region/latency probes are routine LoginApp traffic. Keep counting and
        # replying to every ping; expose per-packet details in developer/debug logs.
        if ((name == 'ping_replied' or name == 'login_element' and fields.get('kind') == 2)
                and not getattr(self, 'developer_mode', False)):
            level = logging.DEBUG
        if LOG.isEnabledFor(level):
            LOG.log(level, json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": name, **fields}))

    async def http(self, reader, writer):
        try:
            peer = writer.get_extra_info('peername')
            if not peer or not self.network.allows(peer[0]):
                self.event('http_address_denied')
                return
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if len(header) > 16384:
                raise ValueError("Header too large")
            method, target, _ = header.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
            path = urlsplit(target).path
            headers = {}
            for line in header.split(b"\r\n")[1:]:
                if b":" in line:
                    key, value = line.split(b":", 1)
                    headers[key.decode("ascii").lower()] = value.strip().decode("latin1")
            size = int(headers.get("content-length", "0"))
            if not 0 <= size <= 65536 or "transfer-encoding" in headers:
                raise ValueError("Unsupported request body")
            body = await asyncio.wait_for(reader.readexactly(size), 5)
            fields = parse_qs(body.decode("utf-8", errors="replace")) if "application/x-www-form-urlencoded" in headers.get("content-type", "") else {}
            tls = writer.get_extra_info('ssl_object')
            self.event("http_request", method=method, path=path, body_fields=sorted(fields),
                       tls_version=tls.version() if tls is not None else None)
            mime = "application/json"
            extra_headers = ""
            connection_path = path.startswith('/tfs/connection/')
            fixtures, auth_response, market = {}, None, None
            if not connection_path:
                fixtures_file = self.lab / "http-fixtures.json"
                fixtures = json.loads(fixtures_file.read_text()) if fixtures_file.exists() else {}
                auth_response = await self.accounts.handle_async(method, path, fields, headers)
                market = market_response(self.accounts, method, path, body, headers)
            if connection_path:
                if method in ('GET', 'HEAD') and path == '/tfs/connection/v1':
                    status, payload = 200, self.connection_profile_json
                else:
                    status, payload = 404, b'{"error":"Not found"}'
            elif method in ("GET", "HEAD") and (path.startswith("/conf/regions_") and path.endswith(".yaml") or path == "/regions.yaml"):
                status, payload, mime = 200, self.regions, "application/yaml"
            elif method in ("GET", "HEAD") and path == "/health":
                status = 200
                payload = json.dumps({"status": "local-poc", "version": self.manifest["version"],
                    "lab": str(self.lab), "server_id": self.server_id,
                    "implementation_revision": self.implementation_revision,
                    "catalog_revision": SHOP_REVISION,
                    "simulation_backend": getattr(self,"simulation_backend",None),
                    "simulation_workers": getattr(self, "simulation_workers", None),
                    "arena_owners": getattr(self, "arena_owners", None),
                    "owner_ipc": getattr(self, "owner_ipc", 'queue'),
                    "dynamic_backend": getattr(self, "dynamic_backend", None),
                    "rapier_parallel_drive": getattr(self, "rapier_parallel_drive", False),
                    "rapier_queries": getattr(self, "rapier_queries", 'bullet'),
                    "developer_mode": getattr(self, "developer_mode", False),
                    "diagnostic_status": dict(queued_records=self.diagnostics.queue.qsize(),
                        queued_peak_records=self.diagnostics.peak_queue.qsize(),
                        dropped_records=self.diagnostics.dropped_records,
                        dropped_peak_records=self.diagnostics.dropped_peak_records, writer_error=self.diagnostics.failed)
                        if getattr(self, 'diagnostics', None) is not None else None,
                    "diagnostic_log": str(self.diagnostics.path) if getattr(self, 'diagnostics', None) is not None else None,
                    "log_file": next((handler.baseFilename for handler in logging.getLogger().handlers
                                      if isinstance(handler, logging.FileHandler)), None),
                    "training_bot": getattr(self, "training_bot", False),
                    "training_config": self.training_config.to_dict(),
                    "recon_enabled": getattr(self, "recon_enabled", False),
                    "recon_server_test": getattr(self, "recon_enabled", False)}).encode()
            elif path == "/debug/health":
                # Diagnostics may contain session addresses and account data.
                # Never allow fixtures or LAN peers to bypass this boundary.
                if (method in ("GET", "HEAD") and getattr(self, "developer_mode", False)
                        and ipaddress.ip_address(peer[0]).is_loopback):
                    status = 200
                    payload = json.dumps({"status": "local-poc", "version": self.manifest["version"],
                        "lab": str(self.lab), "server_id": self.server_id,
                        "implementation_revision": self.implementation_revision,
                        "catalog_revision": SHOP_REVISION, "developer_mode": True,
                        "capabilities": ["native-login", "hangar", "training", "shared-battle", "rooms"],
                        "progress": {k: v for k, v in self.accounts.get(1).progress.items() if k != "recent_ids"},
                        "runtime": self.base.metrics() if self.base else {},
                        "counters": dict(self.counters)}).encode()
                else:
                    status, payload = 404, b'{"error":"Not found"}'
            elif path == '/rooms' or path.startswith('/rooms/'):
                status,document = self.base.rooms.http(method,path,headers,body) if self.base else (
                    503,{'error':'Game endpoint is not ready'})
                payload = json.dumps(document).encode()
            elif method in ("GET", "HEAD") and re.fullmatch(r"/localizations/[A-Za-z_-]+\.yaml", path):
                locale = path.rsplit('/',1)[1][:-5]
                status, payload, mime = 200, additional_labels(locale, self.server_name), "application/yaml"
            elif auth_response is not None:
                status, document, response_headers = auth_response
                payload = json.dumps(document).encode()
                extra_headers = "".join(f"{key}: {value}\r\n" for key, value in response_headers.items())
            elif market is not None:
                status, document = market
                payload = json.dumps(document).encode()
                self.event("market_response", method=method, path=path, status=status)
            elif path in fixtures:
                fixture = fixtures[path]
                status = int(fixture.get("status", 200))
                payload = json.dumps(fixture["body"]).encode()
                for name, value in fixture.get("headers", {}).items():
                    if any(c in str(name) + str(value) for c in "\r\n"):
                        raise ValueError("Invalid fixture header")
                    extra_headers += f"{name}: {value}\r\n"
            else:
                status = 501
                payload = b'{"error":"This client endpoint is not implemented"}'
            reason = {200: "OK", 404: "Not Found", 202: "Accepted", 401: "Unauthorized", 429: "Too Many Requests",
                      501: "Not Implemented", 503: "Service Unavailable"}.get(status, "Response")
            response = f"HTTP/1.1 {status} {reason}\r\nContent-Type: {mime}\r\nContent-Length: {len(payload)}\r\nConnection: close\r\nCache-Control: no-store\r\n{extra_headers}\r\n".encode()
            writer.write(response + (b"" if method == "HEAD" else payload))
            await writer.drain()
        except (ValueError, UnicodeError, asyncio.TimeoutError, asyncio.LimitOverrunError, asyncio.IncompleteReadError, ConnectionError):
            self.event("http_rejected")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    def inspect_login(self, body):
        if len(body) < 5:
            raise ValueError("Truncated login request")
        protocol = struct.unpack_from("<I", body)[0]
        # compatible build 11.21 sends protocol followed directly by RSA blocks. The
        # encryption-indicator byte in modern PC WoT is absent here.
        raw = body[4:]
        encrypted = True
        if encrypted:
            size = self.key.key_size // 8
            if not raw or len(raw) % size:
                raise ValueError("Invalid RSA block size")
            raw = b"".join(self.key.decrypt(raw[p:p + size], padding.OAEP(
                mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
                for p in range(0, len(raw), size))
        # Do not persist credential/key payloads. Schema work needs length/version.
        blobs = []
        pos = 1
        for _ in range(3):
            blob, pos = read_blob(raw, pos)
            blobs.append(blob)
        if len(raw) - pos != (16 if raw[0] & 1 else 0) + 4:
            raise ValueError("Unexpected client login trailer")
        self.event("login_decoded", protocol=protocol, encrypted=bool(encrypted), clear_size=len(raw),
                   data_flags=raw[0], field_sizes=[len(b) for b in blobs], remaining=len(raw)-pos)
        identity = json.loads(blobs[0])
        if not isinstance(identity, dict):
            raise ValueError("Invalid login identity")
        token = identity.get("token", identity.get("token2"))
        if not self.accounts.valid(self.accounts.game, token):
            raise ValueError("Local game token expired; sign in again")
        # Token2 reconnects can omit/reset the account claim. The persisted
        # credential remains bound to its account on this server.
        account = self.accounts.get(self.accounts.game[token].account_id)
        if "token" in identity and identity.get("account_id") != str(account.id):
            raise ValueError("Local account ID mismatch")
        if not 4 <= len(blobs[2]) <= 56:
            raise ValueError("Invalid session cipher key")
        now = time.time()
        self.logins = {k: v for k, v in self.logins.items() if v[1] > now}
        if len(self.logins) >= 64:
            del self.logins[next(iter(self.logins))]
        login_key = secrets.randbits(32)
        self.logins[login_key] = (blobs[2], now + 60, account.id)
        advertised = self.network.advertise_base
        result = socket.inet_aton(advertised.host) + struct.pack(">H", advertised.port) + b"\0\0" + struct.pack("<I", login_key)
        # Native reply reader consumes another u32 after the login key.
        result += struct.pack("<I", 0)
        result += packed_blob(json.dumps({"dbid": account.id,
            "name": account.nickname, "token2": token}, separators=(",", ":")).encode())
        result += bytes((-len(result)) % 8)
        return b"\x01" + crypt(result, blobs[2])


class LoginEndpoint(asyncio.DatagramProtocol):
    def __init__(self, server):
        self.server = server
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if not self.server.network.allows(addr[0]):
            self.server.event('login_address_denied')
            return
        if len(data) > 4096:
            return
        # The prefix is optional in Mercury. Select only a completely valid
        # off-channel packet; do not infer gameplay framing from a single byte.
        errors = []
        for prefix in (False, True):
            try:
                packet = parse_packet(data, prefix)
                elements = list(login_elements(packet))
                break
            except (ValueError, struct.error) as exc:
                errors.append(str(exc))
        else:
            self.server.event("udp_unsupported", size=len(data), header=data[:8].hex(), reasons=errors)
            return
        for kind, request_id, payload in elements:
            self.server.event("login_element", kind=kind, size=len(payload), request=request_id,
                              flags=packet.flags, prefix=packet.prefix)
            if request_id is None:
                continue
            if kind == 2:
                reply = payload
                self.server.event("ping_replied")
            elif kind == 1:
                reply = (b"hostName\0" + self.server.server_name.encode('utf-8')
                         + b"\0ownerName\0local\0usersCount\0" + b"0\0")
            elif kind == 0:
                try:
                    reply = self.server.inspect_login(payload)
                except ValueError as exc:
                    self.server.event("login_decode_failed", reason=str(exc))
                    reply = bytes([68]) + packed_blob(b"Local login failed; sign in again")
            else:
                continue
            self.transport.sendto(encode_reply(packet, request_id, reply), addr)


async def serve(lab: Path):
    server = LocalServer(lab)
    transports = []
    try:
        udp, _ = await open_datagram(lambda: LoginEndpoint(server),
            local_addr=server.network.listen_login.pair)
        transports.append(udp)
        base, _ = await open_datagram(lambda: BaseEndpoint(server),
            local_addr=server.network.listen_base.pair)
        transports.append(base)
        http = await asyncio.start_server(server.http, *server.network.listen_http.pair, limit=16384,
                                         ssl=getattr(server, 'http_tls_context', None))
        from .discovery import advertise
        try:
            discovery = await advertise(server.network)
        except OSError as error:
            LOG.warning('LAN discovery is unavailable: %s', error)
        else:
            if discovery is not None:
                transports.append(discovery)
        server.event("started", listen=server.network.document()['listen'],
                     advertise=server.network.document()['advertise'],server_id=server.server_id,
                     developer_mode=server.developer_mode, training_bot=getattr(server, 'training_bot', False),
                     simulation_backend=server.simulation_backend,
                     recon_enabled=getattr(server, "recon_enabled", False),
                     recon_server_test=getattr(server, "recon_enabled", False))
        async with http:
            await http.serve_forever()
    finally:
        for transport in transports:transport.close()
        try:
            # Datagram connection_lost closes timers and starts owner shutdown.
            # Wait for child physics and settlement work before closing storage.
            await asyncio.sleep(0)
            close_task = getattr(server.base, 'owner_close_task', None)
            if close_task is not None:
                await close_task
        finally:
            server.accounts.close()
            server.event('stopped')


async def serve_cli(lab: Path, *, stop_on_stdin=False):
    """Drain owned server state on SIGTERM or an optional private stdin pipe.

    A daemon reader avoids platform-specific asyncio pipe/signal APIs. It is
    only enabled by the launcher-child flag; ordinary CLI stdin is untouched.
    Library callers retain normal cancellation.
    """
    loop=asyncio.get_running_loop()
    task=asyncio.current_task()
    requested=False
    def stop():
        nonlocal requested
        if not requested:
            requested=True
            task.cancel()
    signal_installed = False
    previous_handler = None
    if sys.platform != 'win32':
        loop.add_signal_handler(signal.SIGTERM,stop)
        signal_installed = True
    else:
        # Windows asyncio loops do not implement add_signal_handler. Python's
        # signal handler runs on the main thread; don't override a host thread.
        if threading.current_thread() is threading.main_thread():
            previous_handler = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *unused: loop.call_soon_threadsafe(stop))
    reader_done = threading.Event()
    if stop_on_stdin:
        input_fd = sys.stdin.fileno()
        peek_input = None
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            peek_input = kernel.PeekNamedPipe
            peek_input.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                   wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
            peek_input.restype = wintypes.BOOL
            input_handle = msvcrt.get_osfhandle(input_fd)
        def watch_input():
            pending = b''
            while not reader_done.is_set():
                try:
                    size = 1024
                    if peek_input is not None:
                        # A pending Windows pipe read can block CPython's stdin
                        # initialization in a spawned arena process.
                        available = wintypes.DWORD()
                        if not peek_input(input_handle, None, 0, None, ctypes.byref(available), None):
                            raise ctypes.WinError(ctypes.get_last_error())
                        if not available.value:
                            reader_done.wait(.05)
                            continue
                        size = min(size, available.value)
                    data = os.read(input_fd, size)
                except (OSError, ValueError):
                    data = b''
                pending = (pending + data)[-2048:]
                lines = pending.split(b'\n')
                pending = lines[-1]
                if not data or any(line.strip() == b'stop' for line in lines[:-1]):
                    if not reader_done.is_set():
                        try:
                            loop.call_soon_threadsafe(stop)
                        except RuntimeError:
                            pass  # The owned process is already leaving.
                    return
        threading.Thread(target=watch_input, name='tfs-owned-server-input', daemon=True).start()
    try:
        from .storage import account_access
        with account_access(lab / 'private'):
            await serve(lab)
    except asyncio.CancelledError:
        if not requested:
            raise
    finally:
        reader_done.set()
        if signal_installed:
            loop.remove_signal_handler(signal.SIGTERM)
        elif previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
