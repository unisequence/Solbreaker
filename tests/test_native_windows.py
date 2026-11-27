"""Portable native library selection and spawned owner socket transport."""
import asyncio
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from solbreaker.arena_owner import ArenaOwner, AsyncArenaOwner, Command, OwnerError
from solbreaker.owner_channel import FramedChannel, configured_ipc
from tools.rapier_fixture import library_path


class EchoHandler:
    def apply(self, command):
        return command.payload, ()

    def close(self):
        pass


class SlowHandler(EchoHandler):
    def __init__(self):
        time.sleep(0.25)

    def apply(self, command):
        time.sleep(0.5)
        return super().apply(command)


class FailingHandler(EchoHandler):
    def apply(self, command):
        raise NameError('failed while refilling the magazine')


def failing_factory():
    raise RuntimeError('failed to build vehicle collision shape')


class NativeWindowsTests(unittest.TestCase):
    def test_rapier_selects_native_windows_dll(self):
        with patch('tools.rapier_fixture.sys.platform', 'win32'):
            self.assertEqual(library_path().name, 'tfs_rapier_dynamic.dll')

    def test_socket_transport_is_available_on_windows(self):
        with patch('solbreaker.owner_channel.os.name', 'nt'):
            self.assertEqual(configured_ipc('socket'), 'socket')

    def test_spawned_owner_transfers_socket_handles(self):
        with ArenaOwner(EchoHandler, ipc='socket', timeout=10) as owner:
            result = owner.apply(Command('control', 1., 1, b'\x01\x00'))
            self.assertEqual(result.state, b'\x01\x00')

    def test_startup_reports_the_factory_exception(self):
        for ipc in ('socket', 'queue'):
            with self.subTest(ipc=ipc), self.assertRaisesRegex(
                    OwnerError, 'owner failed to initialize: RuntimeError: failed to build vehicle collision shape'):
                ArenaOwner(failing_factory, ipc=ipc)

    def test_startup_timeout_does_not_extend_command_timeout(self):
        async def run_owner():
            async with await AsyncArenaOwner.create(SlowHandler, timeout=0.15, startup_timeout=5) as owner:
                with self.assertRaisesRegex(OwnerError, 'TimeoutError'):
                    await owner.apply(Command('motion', 1., 1))

        with patch.dict('os.environ', {'SOLBREAKER_OWNER_IPC': 'socket'}):
            asyncio.run(run_owner())

    def test_socket_transport_reports_the_workers_exception(self):
        async def run_owner():
            async with await AsyncArenaOwner.create(FailingHandler, timeout=5) as owner:
                with self.assertRaisesRegex(OwnerError, 'motion: NameError: failed while refilling'):
                    await owner.apply(Command('motion', 1., 1))

        with patch.dict('os.environ', {'SOLBREAKER_OWNER_IPC': 'socket'}):
            asyncio.run(run_owner())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows filesystem and Bullet runtime')
    def test_bullet_loads_unicode_mesh_without_short_alias(self):
        import pybullet as bullet
        from solbreaker.mesh_files import bullet_mesh_paths, create_bullet_mesh

        client = bullet.connect(bullet.DIRECT)
        self.addCleanup(bullet.disconnect, client)
        with tempfile.TemporaryDirectory(prefix='солбрейкер-') as folder:
            mesh = Path(folder) / 'карта.obj'
            mesh.write_text('v 0 0 0\nv 1 0 0\nv 0 0 1\nf 1 2 3\n', encoding='ascii')
            filename = str(mesh)
            shape = create_bullet_mesh(bullet, filename, physicsClientId=client,
                                       flags=bullet.GEOM_FORCE_CONCAVE_TRIMESH)
            self.assertGreaterEqual(shape, 0)
            with patch('solbreaker.mesh_files._short_windows_path', side_effect=lambda path: path):
                with bullet_mesh_paths([filename, '']) as (compatible, empty):
                    self.assertTrue(compatible.isascii())
                    self.assertEqual(empty, '')
                    self.assertEqual(Path(compatible).read_bytes(), mesh.read_bytes())
                    shape = bullet.createCollisionShapeArray(
                        shapeTypes=[bullet.GEOM_MESH, bullet.GEOM_BOX],
                        fileNames=[compatible, empty],
                        halfExtents=[(.5, .5, .5)] * 2,
                        physicsClientId=client)
                    self.assertGreaterEqual(shape, 0)
                self.assertFalse(Path(compatible).exists())
                self.assertTrue(mesh.exists())

    def test_tcp_fallback_disables_nagle_for_frame_delivery(self):
        fallback = socket._fallback_socketpair
        with patch('solbreaker.owner_channel.socket.socketpair', fallback):
            channel = FramedChannel(max_message=64)
        self.addCleanup(channel.close)
        self.assertEqual(channel.writer.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)
        channel.put(b'frame', timeout=1)
        self.assertEqual(channel.get(timeout=1), b'frame')
