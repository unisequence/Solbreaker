"""Private experiment: the socket owner protocol without per-RPC thread hops.

The process constructor and cleanup retain the existing threaded adapter.
Only the already opt-in socket request exchange changes. No request retries.
"""
import asyncio
import pickle
import struct
import time


async def _put(channel, raw, timeout):
    from solbreaker.owner_channel import ChannelError
    if channel.closed or channel.write_failed:
        raise ChannelError('owner channel sender unavailable')
    if type(raw) is not bytes or len(raw) > channel.max_message:
        raise ValueError('invalid owner channel message')
    frame = struct.pack('<I', len(raw)) + raw
    channel.writer.setblocking(False)
    try:
        async with asyncio.timeout(timeout):
            await asyncio.get_running_loop().sock_sendall(channel.writer, frame)
    except BaseException:
        channel.write_failed = True
        raise


async def _get(channel, process, timeout):
    from solbreaker.owner_channel import ChannelError
    if channel.closed:
        raise ChannelError('owner channel receiver unavailable')
    loop = asyncio.get_running_loop()
    channel.reader.setblocking(False)
    deadline = time.monotonic() + timeout
    while True:
        required = 4
        if len(channel.buffer) >= 4:
            size = struct.unpack_from('<I', channel.buffer)[0]
            if size == 0xffffffff:
                del channel.buffer[:4]
                return None
            if size > channel.max_message:
                raise ChannelError('oversized owner channel frame')
            required += size
            if len(channel.buffer) >= required:
                value = bytes(channel.buffer[4:required])
                del channel.buffer[:required]
                return value
        # Consume bytes already present without another task/context switch.
        # The socket is nonblocking; an unavailable chunk goes through the
        # event loop below. Preserve one receive task across health checks.
        try:
            data = channel.reader.recv(min(65536, required - len(channel.buffer)))
        except (BlockingIOError, InterruptedError):
            data = None
        if data is not None:
            if not data:
                raise ChannelError('owner channel closed during receive')
            channel.buffer.extend(data)
            continue
        # Cancelling a polling timeout could discard bytes already read but
        # not appended to buffer; only retire/cancel at the overall deadline.
        receive = asyncio.create_task(loop.sock_recv(
            channel.reader, min(65536, required - len(channel.buffer))))
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('owner receive timed out')
                done, _ = await asyncio.wait((receive,), timeout=min(.1, remaining))
                if done:
                    data = receive.result()
                    break
                if not process.is_alive():
                    raise ChannelError('owner exited during receive')
        finally:
            if not receive.done():
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
        if not data:
            raise ChannelError('owner channel closed during receive')
        channel.buffer.extend(data)


async def apply(owner, command):
    from solbreaker.arena_owner import Command, CommandRejected, OwnerError, Result, _TIMED, _freeze
    if owner._closed:
        raise OwnerError('owner is closed')
    if type(command) is not Command:
        raise ValueError('unsupported message')
    command.validate()
    if (command.op in _TIMED and owner._last_at is not None
            and command.at < owner._last_at):
        raise ValueError(f'timestamp moved backwards: {command.op} {command.at} < {owner._last_at}')
    sequence = owner._seq + 1
    raw = _freeze((sequence, command))
    try:
        await _put(owner._in, raw, owner._timeout)
        reply = pickle.loads(await _get(owner._out, owner._process, owner._timeout))
        if (isinstance(reply, tuple) and len(reply) == 3 and reply[0] == 'rejected'
                and reply[1] == sequence and type(reply[2]) is str):
            owner._seq = sequence
            raise CommandRejected(reply[2], sequence)
        if (not isinstance(reply, tuple) or len(reply) != 2 or reply[0] != 'result'
                or type(reply[1]) is not Result or reply[1].sequence != sequence):
            raise OwnerError(f'owner failed; command outcome unknown: {reply!r}')
    except CommandRejected:
        raise
    except BaseException as exc:
        raise OwnerError('owner channel failed; command outcome unknown') from exc
    owner._seq = sequence
    if command.op in _TIMED:
        owner._last_at = command.at
    return reply[1]

