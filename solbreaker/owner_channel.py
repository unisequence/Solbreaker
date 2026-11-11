"""Framed local channels for the owner's single outstanding request.

This is not a general queue. ArenaOwner serializes requests, limits messages,
and retires the owner after an unknown outcome. A failed partial write poisons
its sender so shutdown cannot append another frame to that partial message.
"""
import math
import os
import queue
import socket
import struct
import time


class ChannelError(OSError):
    pass


def configured_ipc(value=None):
    value = os.environ.get('SOLBREAKER_OWNER_IPC', 'queue') if value is None else value
    if value not in ('queue', 'socket'):
        raise ValueError('SOLBREAKER_OWNER_IPC must be queue or socket')
    return value


class FramedChannel:
    def __init__(self, *, max_message):
        if type(max_message) is not int or not 1<=max_message<=1024*1024:
            raise ValueError('invalid owner channel message limit')
        self.writer,self.reader = socket.socketpair()
        if self.writer.family in (socket.AF_INET, socket.AF_INET6):
            self.writer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.max_message = max_message
        self.buffer = bytearray()
        self.closed = self.write_failed = False

    def close_sender(self):
        self.writer.close()

    def close_receiver(self):
        self.reader.close()

    @staticmethod
    def _timeout(value):
        if value is not None and (not isinstance(value,(int,float))
                                 or not math.isfinite(value) or value<0):
            raise ValueError('invalid owner channel timeout')
        return value

    def put(self, value, *, timeout=None):
        if self.closed or self.write_failed:
            raise ChannelError('owner channel sender unavailable')
        if value is not None and (type(value) is not bytes or len(value)>self.max_message):
            raise ValueError('invalid owner channel message')
        timeout = self._timeout(timeout)
        size = 0xffffffff if value is None else len(value)
        frame = struct.pack('<I',size)+(value or b'')
        self.writer.settimeout(timeout)
        try:
            self.writer.sendall(frame)
        except BaseException as exc:
            self.write_failed = True
            if isinstance(exc,(socket.timeout,BlockingIOError)):
                raise queue.Full from exc
            raise

    def put_nowait(self, value):
        return self.put(value,timeout=0.)

    def get(self, *, timeout=None):
        if self.closed:
            raise ChannelError('owner channel receiver unavailable')
        timeout = self._timeout(timeout)
        deadline = None if timeout is None else time.monotonic()+timeout
        while True:
            required = 4
            if len(self.buffer)>=4:
                size = struct.unpack_from('<I',self.buffer)[0]
                if size==0xffffffff:
                    del self.buffer[:4]
                    return None
                if size>self.max_message:
                    raise ChannelError('oversized owner channel frame')
                required += size
                if len(self.buffer)>=required:
                    value = bytes(self.buffer[4:required])
                    del self.buffer[:required]
                    return value
            self.reader.settimeout(None if deadline is None else max(0.,deadline-time.monotonic()))
            try:
                data = self.reader.recv(min(65536,required-len(self.buffer)))
            except (socket.timeout,BlockingIOError) as exc:
                # Keep a partial frame while ArenaOwner polls its process and
                # overall deadline. No command is repeated by this channel.
                raise queue.Empty from exc
            if not data:
                raise ChannelError('owner channel closed during receive')
            self.buffer.extend(data)

    def get_nowait(self):
        return self.get(timeout=0.)

    def cancel_join_thread(self):
        # There is no feeder thread. The owner protocol supplies backpressure.
        pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.writer.close()
        self.reader.close()
        self.buffer.clear()
