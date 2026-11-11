"""Portable native library selection and spawned owner socket transport."""
import unittest
import socket
from unittest.mock import patch

from solbreaker.arena_owner import ArenaOwner, Command
from solbreaker.owner_channel import FramedChannel, configured_ipc
from solbreaker.rapier_dynamic import library_path


class EchoHandler:
    def apply(self, command):
        return command.payload, ()

    def close(self):
        pass


class NativeWindowsTests(unittest.TestCase):
    def test_rapier_selects_native_windows_dll(self):
        with patch('solbreaker.rapier_dynamic.sys.platform', 'win32'):
            self.assertEqual(library_path().name, 'tfs_rapier_dynamic.dll')

    def test_socket_transport_is_available_on_windows(self):
        with patch('solbreaker.owner_channel.os.name', 'nt'):
            self.assertEqual(configured_ipc('socket'), 'socket')

    def test_spawned_owner_transfers_socket_handles(self):
        with ArenaOwner(EchoHandler, ipc='socket', timeout=10) as owner:
            result = owner.apply(Command('control', 1., 1, b'\x01\x00'))
            self.assertEqual(result.state, b'\x01\x00')

    def test_tcp_fallback_disables_nagle_for_frame_delivery(self):
        fallback = socket._fallback_socketpair
        with patch('solbreaker.owner_channel.socket.socketpair', fallback):
            channel = FramedChannel(max_message=64)
        self.addCleanup(channel.close)
        self.assertEqual(channel.writer.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)
        channel.put(b'frame', timeout=1)
        self.assertEqual(channel.get(timeout=1), b'frame')
