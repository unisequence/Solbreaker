"""Keep UDP replies on the local address to which the client connected.

Linux wildcard sockets need IP_PKTINFO for replies on a secondary address.
Explicit binds use asyncio's existing datagram transport unchanged.
See https://man7.org/linux/man-pages/man2/IP_PKTINFO.2const.html.
"""
import asyncio
from collections import OrderedDict
import socket
import sys


class SourceDatagramTransport(asyncio.DatagramTransport):
    MAX_PEERS=1024

    def __init__(self,loop,sock,protocol):
        self.loop,self.socket,self.protocol=loop,sock,protocol
        self.sources=OrderedDict()
        self.closed=False
        protocol.connection_made(self)
        loop.add_reader(sock.fileno(),self.read_ready)

    def read_ready(self):
        for _ in range(64):
            try:data,control,flags,addr=self.socket.recvmsg(4097,socket.CMSG_SPACE(12))
            except (BlockingIOError,InterruptedError):return
            except OSError as exc:
                if not self.closed:self.protocol.error_received(exc)
                return
            if flags & (socket.MSG_TRUNC|socket.MSG_CTRUNC):continue
            source=next((value[4:8] for level,kind,value in control
                         if level==socket.IPPROTO_IP and kind==getattr(socket,'IP_PKTINFO',8) and len(value)==12),None)
            if source is None:continue
            self.sources[addr]=source;self.sources.move_to_end(addr)
            while len(self.sources)>self.MAX_PEERS:self.sources.popitem(last=False)
            try:self.protocol.datagram_received(data,addr)
            except Exception as exc:
                self.loop.call_exception_handler(dict(message='UDP protocol callback failed',exception=exc,protocol=self.protocol))
            if self.closed:return

    def sendto(self,data,addr):
        if self.closed:return
        source=self.sources.get(addr)
        info=[] if source is None else [(socket.IPPROTO_IP,getattr(socket,'IP_PKTINFO',8),bytes(4)+source+bytes(4))]
        try:self.socket.sendmsg([data],info,0,addr)
        except OSError as exc:
            # Datagram send failures are reported like asyncio's UDP transport.
            # Mercury retains reliable packets and retries them on its own timer.
            self.protocol.error_received(exc)

    def get_extra_info(self,name,default=None):
        if name=='sockname':return self.socket.getsockname() if not self.closed else default
        if name=='socket':return self.socket
        return default

    def get_protocol(self):return self.protocol
    def is_closing(self):return self.closed
    def close(self):
        if self.closed:return
        self.closed=True
        self.loop.remove_reader(self.socket.fileno());self.socket.close();self.sources.clear()
        self.loop.call_soon(self.protocol.connection_lost,None)

    def abort(self):self.close()


async def open_datagram(factory,local_addr):
    loop=asyncio.get_running_loop()
    if local_addr[0]!='0.0.0.0':
        return await loop.create_datagram_endpoint(factory,local_addr=local_addr)
    if not sys.platform.startswith('linux'):
        raise ValueError('Wildcard UDP requires Linux IP_PKTINFO; use an explicit listen IPv4 address')
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP,getattr(socket,'IP_PKTINFO',8),1)
        sock.setblocking(False);sock.bind(local_addr)
        protocol=factory()
        return SourceDatagramTransport(loop,sock,protocol),protocol
    except BaseException:
        sock.close()
        raise
