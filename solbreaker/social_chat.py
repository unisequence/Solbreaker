"""Bounded social chat and its XMPP transport, outside arena simulation and IPC."""
from dataclasses import dataclass
from collections import deque
import asyncio
import base64
import codecs
import hashlib
import hmac
import html
import re
import secrets
import time
from xml.etree import ElementTree


MAX_TEXT_BYTES = 500
MAX_PENDING = 256
DELIVERIES_PER_TURN = 32
MESSAGE_INTERVAL = .5
MAX_XML_BYTES = 65536
MAX_WRITE_BUFFER = 131072
XMPP_IDLE_SECONDS = 120
XMPP_RESOLVER = 'http://wargaming.net/xmpp#spa-resolver'
XMPP_EXTENSION_VERSION = 3
XMPP_EXTENSIONS = f'http://wargaming.net/xmpp#v{XMPP_EXTENSION_VERSION}'
XMPP_MUC = 'http://jabber.org/protocol/muc'
XMPP_ROSTER = 'jabber:iq:roster'
GLOBAL_CHANNEL = 'global'


def server_configuration(network):
    """Return native Account server settings for local XChat.

    XChat is a separate TCP service in the pinned client.  Keep its endpoint
    adjacent to BaseApp so existing LAN/VPN network profiles do not need a
    second address setting.
    """
    host = network.advertise_base.host
    port = network.advertise_base.port + 1
    if port > 65535:
        raise ValueError('BaseApp port 65535 leaves no adjacent XChat port')
    return {
        'xmpp_host': host,
        'xmpp_port': port,
        'xmpp_connections': [[host, port]],
        'xmpp_muc': {
            'muc-service': {'hostname': host, 'rooms': [GLOBAL_CHANNEL]},
            'muc-service-clans': {'hostname': 'invalidHost'},
            'muc-service-training': {'hostname': host},
        },
    }


def listener_address(network):
    """Return the TCP bind address paired with the advertised XChat port."""
    port = network.listen_base.port + 1
    if port > 65535:
        raise ValueError('BaseApp port 65535 leaves no adjacent XChat port')
    return network.listen_base.host, port


class ChatRejected(ValueError):
    """The authenticated sender requested an invalid chat operation."""


class ChatBusy(ChatRejected):
    """The bounded social queue or sender rate limit is currently full."""


@dataclass(frozen=True)
class SocialMessage:
    id: int
    kind: str
    sender: int
    nickname: str
    text: str
    created_at: float
    room_id: str | None = None
    recipient: int | None = None


class SocialChatService:
    """Validate, queue and fan out global, room and direct messages.

    Delivery callbacks must only enqueue bytes into their transport.  They are
    isolated from each other: one broken client cannot stop the remaining
    recipients.  Messages are intentionally ephemeral in this first version.
    """

    def __init__(self, accounts, rooms, *, event=None, monotonic=time.monotonic,
                 wall_clock=time.time):
        self.accounts = accounts
        self._rooms = rooms
        self._event = event or (lambda name, **fields: None)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._subscribers = {}
        self._next_subscription = 1
        self._next_message = 1
        self._after = {}
        self._pending = deque()
        self._scheduled = False

    def subscribe(self, account_id, callback, room_changed=None, friend_changed=None):
        """Register an authenticated, non-blocking transport callback."""
        self.accounts.get(account_id)
        if not callable(callback):
            raise ChatRejected('Chat delivery callback is required')
        subscription = self._next_subscription
        self._next_subscription += 1
        self._subscribers[subscription] = (account_id, callback, room_changed, friend_changed)
        return subscription

    def unsubscribe(self, subscription):
        self._subscribers.pop(subscription, None)

    @staticmethod
    def _text(value):
        if not isinstance(value, str) or not value.strip() or '\0' in value:
            raise ChatRejected('Enter a chat message')
        try:
            encoded = value.encode('utf-8')
        except UnicodeError:
            raise ChatRejected('Chat message is not valid UTF-8') from None
        if len(encoded) > MAX_TEXT_BYTES:
            raise ChatRejected(f'Chat message exceeds {MAX_TEXT_BYTES} bytes')
        # No server-side profanity filter: preserve the authenticated user's
        # exact text, including whitespace and casing.
        return value

    def member_room(self, account_id):
        manager = self._rooms()
        room = manager.membership(account_id) if manager is not None else None
        return room if room is not None and account_id in room.members else None

    def room_changed(self, account_id, *, refresh=False):
        asyncio.get_running_loop().call_soon(self._notify_room, account_id, refresh)

    def _notify_room(self, account_id, refresh):
        room = self.member_room(account_id)
        for subscriber, _, callback, _ in tuple(self._subscribers.values()):
            if subscriber == account_id and callback is not None:
                try:
                    callback(room, refresh=refresh)
                except (ConnectionError, OSError, RuntimeError, ValueError) as error:
                    self._event('social_chat_delivery_failed', account_id=account_id,
                                kind='room', operation=type(error).__name__)

    def notify_friend(self, account_id, peer_id, event):
        for subscriber, _, _, callback in tuple(self._subscribers.values()):
            if subscriber == account_id and callback is not None:
                try:
                    callback(peer_id, event)
                except (ConnectionError, OSError, RuntimeError, ValueError) as error:
                    self._event('social_chat_delivery_failed', account_id=account_id,
                                kind='friend', operation=type(error).__name__)

    def friend_online(self, account_id):
        return any(account_id == subscriber for subscriber, _, _, _ in self._subscribers.values())

    def send(self, sender, text, *, kind='global', recipient=None, expected_room=None):
        account = self.accounts.get(sender)
        text = self._text(text)
        now = self._monotonic()
        if now < self._after.get(sender, 0.):
            raise ChatBusy('Wait before sending another message')
        if len(self._pending) >= MAX_PENDING:
            raise ChatBusy('Chat queue is full')

        room = None
        if kind == 'global':
            recipients = None
        elif kind == 'room':
            room = self.member_room(sender)
            if room is None:
                raise ChatRejected('Join a room before using room chat')
            if expected_room is not None and room.id != expected_room:
                raise ChatRejected('The chat belongs to a different room')
            recipients = frozenset(room.members)
        elif kind == 'direct':
            if type(recipient) is not int:
                raise ChatRejected('Choose a direct-message recipient')
            self.accounts.get(recipient)
            recipients = frozenset((sender, recipient))
        else:
            raise ChatRejected('Unsupported chat channel')

        room_id = room.id if room is not None else None
        message = SocialMessage(self._next_message, kind, sender, account.nickname,
                                text, self._wall_clock(), room_id, recipient)
        self._next_message += 1
        self._after[sender] = now + MESSAGE_INTERVAL
        self._pending.append((message, recipients, room))
        self._event('social_chat_queued', account_id=sender, kind=kind,
                    target=recipient or room_id)
        if not self._scheduled:
            self._scheduled = True
            asyncio.get_running_loop().call_soon(self._drain)
        return message

    def _drain(self):
        for _ in range(min(len(self._pending), DELIVERIES_PER_TURN)):
            message, recipients, room = self._pending.popleft()
            for account_id, callback, _, _ in tuple(self._subscribers.values()):
                if recipients is not None and account_id not in recipients:
                    continue
                if room is not None and account_id not in room.members:
                    continue
                try:
                    callback(message)
                except (ConnectionError, OSError, RuntimeError, ValueError) as error:
                    self._event('social_chat_delivery_failed', account_id=account_id,
                                kind=message.kind, operation=type(error).__name__)
        self._scheduled = bool(self._pending)
        if self._scheduled:
            asyncio.get_running_loop().call_soon(self._drain)


_ATTRIBUTE = re.compile(r"([A-Za-z_:][\w:.-]*)\s*=\s*(['\"])(.*?)\2", re.S)
_DIGEST_FIELD = re.compile(
    r'(?:^|,)\s*([A-Za-z][\w-]*)=(?:"((?:\\.|[^"])*)"|([^,]*))')


def _xml_attributes(source):
    return {name: value for name, unused, value in _ATTRIBUTE.findall(source)}


def _take_stream(buffer):
    start = buffer.find('<stream:stream')
    if start < 0:
        return None, buffer[-64:]
    end = buffer.find('>', start)
    if end < 0:
        return None, buffer[start:]
    return _xml_attributes(buffer[start:end + 1]), buffer[end + 1:]


def _take_stanza(buffer):
    buffer = buffer.lstrip()
    if buffer.startswith('</stream:stream>'):
        return 'stream-close', buffer[len('</stream:stream>'):]
    match = re.match(r'<(?:[A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)\b', buffer)
    if match is None:
        return None, buffer
    tag = match.group(1)
    opening = buffer.find('>')
    if opening < 0:
        return None, buffer
    if buffer[opening - 1] == '/':
        return buffer[:opening + 1], buffer[opening + 1:]
    closing = re.search(rf'</(?:[A-Za-z_][\w.-]*:)?{re.escape(tag)}\s*>',
                        buffer[opening + 1:])
    if closing is None:
        return None, buffer
    end = opening + 1 + closing.end()
    return buffer[:end], buffer[end:]


def _digest_fields(raw):
    fields = {}
    for match in _DIGEST_FIELD.finditer(raw):
        value = match.group(2) if match.group(2) is not None else match.group(3).strip()
        fields[match.group(1)] = re.sub(r'\\(.)', r'\1', value)
    return fields


def _digest_response(username, realm, password, nonce, cnonce, counter, qop,
                     digest_uri, *, server=False):
    initial = hashlib.md5(f'{username}:{realm}:{password}'.encode()).digest()
    ha1 = hashlib.md5(initial + f':{nonce}:{cnonce}'.encode()).hexdigest()
    prefix = '' if server else 'AUTHENTICATE'
    ha2 = hashlib.md5(f'{prefix}:{digest_uri}'.encode()).hexdigest()
    return hashlib.md5(
        f'{ha1}:{nonce}:{counter}:{cnonce}:{qop}:{ha2}'.encode()).hexdigest()


def _local_name(element):
    return element.tag.rsplit('}', 1)[-1]


class XmppConnection:
    """Small native-XChat transport; strategy and arena ticks never enter here."""

    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.buffer = ''
        self._utf8 = codecs.getincrementaldecoder('utf-8')()
        self.domain = server.network.advertise_base.host
        self.nonce = None
        self.account_id = None
        self.resource = 'wotbi'
        self.subscription = None
        self.ready = False
        self.next_search = 0.
        self.channels = {}
        self.invited_room = None
        self.invite_after = 0.

    def _write(self, data):
        if self.writer.is_closing():
            raise ConnectionError('XChat connection is closed')
        transport = self.writer.transport
        if transport.get_write_buffer_size() + len(data) > MAX_WRITE_BUFFER:
            raise ChatBusy('XChat delivery queue is full')
        self.writer.write(data.encode())

    def _stream(self, features):
        stream_id = secrets.token_hex(8)
        self._write("<?xml version='1.0'?><stream:stream"
                    " xmlns='jabber:client' xmlns:stream='http://etherx.jabber.org/streams'"
                    f" from='{html.escape(self.domain)}' id='{stream_id}' version='1.0'>"
                    f'<stream:features>{features}</stream:features>')

    async def _read(self):
        # A bound client can sit in the hangar without sending stanzas. Only
        # enforce the deadline for authentication or unfinished messages.
        read = self.reader.read(4096)
        if self.ready and not self.buffer.strip():
            data = await read
        else:
            data = await asyncio.wait_for(read, XMPP_IDLE_SECONDS)
        if not data:
            raise ConnectionError('XChat peer disconnected')
        try:
            chunk = self._utf8.decode(data)
        except UnicodeError:
            raise ChatRejected('XChat requires UTF-8') from None
        self.buffer += chunk
        if len(self.buffer.encode('utf-8')) > MAX_XML_BYTES:
            raise ChatRejected('XChat stanza buffer is too large')

    async def _stream_open(self, features):
        while True:
            attributes, remaining = _take_stream(self.buffer)
            if attributes is not None:
                self.buffer = remaining
                requested = attributes.get('to')
                if requested and requested != self.domain:
                    raise ChatRejected('Unexpected XChat domain')
                self._stream(features)
                await self.writer.drain()
                return
            await self._read()

    async def _stanza(self):
        while True:
            stanza, remaining = _take_stanza(self.buffer)
            if stanza is not None:
                self.buffer = remaining
                return stanza
            await self._read()

    @staticmethod
    def _element(stanza):
        # Prefix declarations normally live on stream:stream and are outside
        # the isolated stanza.  Standard client stanzas use default namespaces,
        # which ElementTree can parse directly.
        return ElementTree.fromstring(stanza)

    async def authenticate(self):
        await self._stream_open(
            "<mechanisms xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>"
            '<mechanism>DIGEST-MD5</mechanism></mechanisms>')
        auth = self._element(await self._stanza())
        if _local_name(auth) != 'auth' or auth.attrib.get('mechanism') != 'DIGEST-MD5':
            raise ChatRejected('XChat requires DIGEST-MD5 authentication')
        self.nonce = secrets.token_urlsafe(24)
        challenge = (f'realm="{self.domain}",nonce="{self.nonce}",qop="auth",'
                     'charset=utf-8,algorithm=md5-sess')
        encoded = base64.b64encode(challenge.encode()).decode()
        self._write("<challenge xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>"
                    f'{encoded}</challenge>')
        await self.writer.drain()

        response = self._element(await self._stanza())
        if _local_name(response) != 'response' or not response.text:
            raise ChatRejected('XChat authentication response is missing')
        try:
            fields = _digest_fields(base64.b64decode(response.text, validate=True).decode())
            account_id = int(fields['username'])
            realm = fields.get('realm', self.domain)
            proof = fields['response']
            if (fields['username'] != str(account_id) or account_id <= 0
                    or realm != self.domain or fields['nonce'] != self.nonce
                    or fields['qop'] != 'auth' or fields['nc'] != '00000001'
                    or fields['digest-uri'] != f'xmpp/{self.domain}' or not fields['cnonce']
                    or re.fullmatch(r'[0-9a-f]{32}', proof) is None):
                raise ValueError('Unexpected DIGEST-MD5 parameters')
            arguments = (fields['nonce'], fields['cnonce'], fields['nc'], fields['qop'],
                         fields['digest-uri'])
        except (KeyError, ValueError, UnicodeError):
            raise ChatRejected('Invalid XChat authentication response') from None
        # The Account service issues the short-lived SPA token before XChat
        # opens. Match the response against this account's live tokens so
        # simultaneous logins cannot replace one another's credential.
        accounts = self.server.accounts
        token = next((candidate for candidate, session in accounts.web.items()
                      if session.account_id == account_id
                      and accounts.valid(accounts.web, candidate)
                      and hmac.compare_digest(proof, _digest_response(
                          fields['username'], realm, candidate, *arguments))), None)
        if token is None:
            raise ChatRejected('XChat authentication failed')
        accounts.get(account_id)
        self.account_id = account_id
        rspauth = _digest_response(fields['username'], realm, token, *arguments, server=True)
        encoded = base64.b64encode(f'rspauth={rspauth}'.encode()).decode()
        self._write("<challenge xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>"
                    f'{encoded}</challenge>')
        await self.writer.drain()
        final = self._element(await self._stanza())
        if _local_name(final) != 'response':
            raise ChatRejected('XChat authentication did not finish')
        self._write("<success xmlns='urn:ietf:params:xml:ns:xmpp-sasl'/>")
        await self.writer.drain()

    def _jid(self, account_id, resource=None):
        jid = f'{account_id}@{self.domain}'
        return jid + '/' + resource if resource else jid

    def _user_metadata(self, account_id, nickname):
        return (f"<wgexts xmlns='{XMPP_EXTENSIONS}'>"
                f'<nickname>{html.escape(nickname)}</nickname>'
                f'<userid>{account_id}</userid></wgexts>')

    def _channel(self, jid):
        target, separator, domain = jid.partition('@')
        if not separator or domain != self.domain:
            raise ChatRejected('Invalid chat channel domain')
        if target == GLOBAL_CHANNEL:
            return 'global', None
        match = re.fullmatch(r'([1-9][0-9]{0,7})-common', target)
        if match and 0 < (room_id := int(match[1])) <= 0xffffff:
            return 'room', f'{room_id:06X}'
        raise ChatRejected('Unknown chat channel')

    def _peer(self, jid):
        ident, separator, domain = jid.partition('@')
        if (not separator or domain.split('/', 1)[0] != self.domain
                or re.fullmatch(r'[1-9][0-9]{0,18}', ident) is None or int(ident) >= 1 << 63):
            raise ChatRejected('Invalid XChat recipient')
        return int(ident)

    def _roster_item(self, peer, nickname, state):
        jid = html.escape(self._jid(peer))
        subscription = 'none' if state == 'outgoing' else state
        request = " ask='subscribe'" if state == 'outgoing' else ''
        return (f"<item jid='{jid}' name='{html.escape(nickname)}' subscription='{subscription}'{request}>"
                f'{self._user_metadata(peer, nickname)}</item>')

    def _friend_presence(self, peer, kind='', *, resource=None):
        nickname = self.server.accounts.get(peer).nickname
        sender = html.escape(self._jid(peer, resource))
        recipient = html.escape(self._jid(self.account_id, self.resource))
        kind = f" type='{kind}'" if kind else ''
        self._write(f"<presence from='{sender}' to='{recipient}'{kind}>"
                    f'{self._user_metadata(peer, nickname)}</presence>')

    def _friend_changed(self, peer, event):
        if event in ('subscribe', 'subscribed', 'unsubscribed'):
            self._friend_presence(peer, event)
            return
        if event in ('online', 'offline'):
            self._friend_presence(peer, '' if event == 'online' else 'unavailable', resource='wotbi')
            return
        nickname = self.server.accounts.get(peer).nickname
        item = self._roster_item(peer, nickname, event)
        self._write(f"<iq type='set' id='sb-roster-{secrets.token_hex(8)}'>"
                    f"<query xmlns='{XMPP_ROSTER}'>{item}</query></iq>")

    def _friend_action(self, target, action):
        peer = self._peer(target)
        store = self.server.accounts.store
        transition = store.change_friendship(self.account_id, peer, action)
        notify = self.server.social_chat.notify_friend
        if transition == 'requested':
            notify(self.account_id, peer, 'outgoing')
            notify(peer, self.account_id, 'subscribe')
        elif transition == 'accepted':
            notify(peer, self.account_id, 'subscribed')
            notify(self.account_id, peer, 'both')
            notify(peer, self.account_id, 'both')
            if self.server.social_chat.friend_online(peer):
                notify(self.account_id, peer, 'online')
            notify(peer, self.account_id, 'online')
        elif transition == 'removed':
            notify(peer, self.account_id, 'unsubscribed')
            notify(self.account_id, peer, 'remove')
            notify(peer, self.account_id, 'remove')

    def _friend_availability(self, event):
        for peer, _, state in self.server.accounts.store.friendships(self.account_id):
            if state == 'both':
                self.server.social_chat.notify_friend(peer, self.account_id, event)

    def _room_changed(self, room, *, refresh=False):
        room_id = room.id if room is not None else None
        now = time.monotonic()
        if (self.invited_room == room_id
                and (room_id is None or not refresh or now < self.invite_after)):
            return
        previous = self.channels.pop(('room', self.invited_room), None)
        if previous is not None:
            self._channel_presence(previous, unavailable=True)
        self.invited_room = None
        if room_id is None:
            return
        channel = f'{int(room_id, 16)}-common@{self.domain}'
        recipient = html.escape(self._jid(self.account_id, self.resource))
        # The training screen uses the mediated invitation to select its chat.
        self._write(f"<message from='{html.escape(channel)}' to='{recipient}'>"
                    f"<x xmlns='{XMPP_MUC}#user'><invite from='{html.escape(self.domain)}'/>"
                    '</x></message>')
        self.invited_room = room_id
        self.invite_after = now + MESSAGE_INTERVAL

    def _handle_presence(self, element):
        if not self.ready:
            return
        target = element.get('to', '')
        kind = element.get('type', '')
        if kind in ('subscribe', 'subscribed', 'unsubscribe', 'unsubscribed'):
            try:
                self._friend_action(target, kind)
            except ValueError:
                self.server.event('xmpp_friend_rejected', account_id=self.account_id)
            return
        if not target:
            if kind != 'unavailable':
                self._room_changed(self.server.social_chat.member_room(self.account_id))
            return
        room_jid, separator, resource = target.partition('/')
        try:
            channel = self._channel(room_jid)
            kind, room_id = channel
            if element.get('type') == 'unavailable':
                occupant = self.channels.pop(channel, None)
                if occupant is not None:
                    self._channel_presence(occupant, unavailable=True)
                    if kind == 'room' and self.invited_room == room_id:
                        self.invited_room = None
                return
            if (not separator or resource != str(self.account_id)
                    or element.find(f'{{{XMPP_MUC}}}x') is None):
                return
            if kind == 'room':
                room = self.server.social_chat.member_room(self.account_id)
                if room is None or room.id != room_id:
                    raise ChatRejected('The chat belongs to a different room')
        except ChatRejected:
            self._write(f"<presence from='{html.escape(target)}' type='error'>"
                        "<error type='auth'><forbidden "
                        "xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/></error></presence>")
            return
        self.channels[channel] = target
        self._channel_presence(target)

    def _channel_presence(self, occupant, *, unavailable=False):
        account = self.server.accounts.get(self.account_id)
        jid = html.escape(self._jid(self.account_id, self.resource))
        kind = " type='unavailable'" if unavailable else ''
        role = 'none' if unavailable else 'participant'
        self._write(f"<presence from='{html.escape(occupant)}' to='{jid}'{kind}>"
                    f"<x xmlns='{XMPP_MUC}#user'><item affiliation='member' role='{role}' "
                    f"jid='{jid}'/><status code='110'/></x>"
                    f"{self._user_metadata(self.account_id, account.nickname)}</presence>")

    def _message(self, message):
        sender_metadata = self._user_metadata(message.sender, message.nickname)
        if message.kind == 'direct':
            sender = self._jid(message.sender, 'wotbi')
            kind = 'chat'
        else:
            occupant = self.channels.get((message.kind, message.room_id))
            if occupant is None:
                return
            room_jid = occupant.split('/', 1)[0]
            sender = (occupant if message.sender == self.account_id
                      else f'{room_jid}/{message.sender}')
            kind = 'groupchat'
        body = html.escape(message.text)
        recipient = html.escape(self._jid(self.account_id, self.resource))
        self._write(f"<message from='{html.escape(sender)}' to='{recipient}' "
                    f"type='{kind}' id='sb-{message.id}'>"
                    f'<body>{body}</body>{sender_metadata}</message>')

    def _iq_result(self, element, body=''):
        identifier = html.escape(element.attrib.get('id', ''))
        self._write(f"<iq type='result' id='{identifier}'>{body}</iq>")

    def _handle_iq(self, element):
        if element.get('type') in ('result', 'error'):
            return
        children = list(element)
        child = children[0] if children else None
        name = _local_name(child) if child is not None else ''
        if name == 'bind':
            resource = next((node.text for node in child if _local_name(node) == 'resource'), None)
            if resource and len(resource.encode()) <= 64:
                self.resource = resource
            jid = html.escape(self._jid(self.account_id, self.resource))
            self._iq_result(element,
                "<bind xmlns='urn:ietf:params:xml:ns:xmpp-bind'>" f'<jid>{jid}</jid></bind>')
            if self.subscription is None:
                self.subscription = self.server.social_chat.subscribe(
                    self.account_id, self._message, self._room_changed, self._friend_changed)
            self.ready = True
            self.server.event('xmpp_bound', account_id=self.account_id)
        elif name in ('session', 'ping'):
            self._iq_result(element)
        elif self.ready and child is not None and child.tag == f'{{{XMPP_ROSTER}}}query':
            if element.get('type') == 'get':
                friends = self.server.accounts.store.friendships(self.account_id)
                roster = ''.join(self._roster_item(peer, nickname, state)
                                 for peer, nickname, state in friends if state != 'incoming')
                self._iq_result(element, f"<query xmlns='{XMPP_ROSTER}'>{roster}</query>")
                for peer, _, state in friends:
                    if state == 'incoming':
                        self._friend_changed(peer, 'subscribe')
                    elif state == 'both' and self.server.social_chat.friend_online(peer):
                        self._friend_changed(peer, 'online')
                self._friend_availability('online')
            elif element.get('type') == 'set' and len(child) == 1 and child[0].get('subscription') == 'remove':
                try:
                    self._friend_action(child[0].get('jid', ''), 'remove')
                except ValueError:
                    self.server.event('xmpp_friend_rejected', account_id=self.account_id)
                self._iq_result(element)
            else:
                self._iq_result(element)
        elif child is not None and child.tag == '{urn:xmpp:blocking}blocklist' and element.get('type') == 'get':
            self._iq_result(element, "<blocklist xmlns='urn:xmpp:blocking'/>")
        elif (self.ready and child is not None
              and child.tag == f'{{{XMPP_RESOLVER}}}nickname-prefix-search'
              and element.get('type') == 'get'):
            now = time.monotonic()
            matches = ()
            if now >= self.next_search:
                self.next_search = now + MESSAGE_INTERVAL
                matches = self.server.accounts.store.search_nicknames(
                    child.get('prefix'), exclude_account_id=self.account_id)
            # The native resolver discards items missing the wgexts child,
            # even when the account has no clan or other extensions.
            items = ''.join(
                f"<item id='{account_id}' nickname='{html.escape(nickname)}'>"
                f"<wgexts xmlns='{XMPP_EXTENSIONS}'/></item>"
                for account_id, nickname in matches)
            self._iq_result(element,
                f"<nickname-prefix-search xmlns='{XMPP_RESOLVER}'>"
                f"{items}</nickname-prefix-search>")
        else:
            identifier = html.escape(element.attrib.get('id', ''))
            self._write(f"<iq type='error' id='{identifier}'><error type='cancel'>"
                        "<service-unavailable xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/>"
                        '</error></iq>')

    def _handle_message(self, element):
        body = next((node.text for node in element if _local_name(node) == 'body'), None)
        if not body:
            return
        kind = element.attrib.get('type', 'chat')
        if kind == 'error':
            return
        if kind == 'chat':
            self.server.social_chat.send(self.account_id, body, kind='direct',
                                         recipient=self._peer(element.get('to', '')))
        elif kind == 'groupchat':
            channel = self._channel(element.get('to', ''))
            if channel not in self.channels:
                raise ChatRejected('Join the chat channel before sending a message')
            channel_kind, room_id = channel
            self.server.social_chat.send(self.account_id, body, kind=channel_kind,
                                         expected_room=room_id)
        else:
            raise ChatRejected('Unsupported XChat message target')

    async def run(self):
        try:
            self.writer.transport.set_write_buffer_limits(high=MAX_WRITE_BUFFER, low=32768)
            await self.authenticate()
            await self._stream_open(
                "<bind xmlns='urn:ietf:params:xml:ns:xmpp-bind'/><session"
                " xmlns='urn:ietf:params:xml:ns:xmpp-session'/>")
            while True:
                stanza = await self._stanza()
                if stanza == 'stream-close':
                    return
                element = self._element(stanza)
                name = _local_name(element)
                if name == 'iq':
                    self._handle_iq(element)
                elif name == 'message' and self.ready:
                    try:
                        self._handle_message(element)
                    except ValueError:
                        # A bad target, unsupported extension or rate-limited
                        # message must not log the player out of chat.
                        identifier = html.escape(element.attrib.get('id', ''))
                        self._write(f"<message type='error' id='{identifier}'>"
                                    "<error type='cancel'><not-acceptable "
                                    "xmlns='urn:ietf:params:xml:ns:xmpp-stanzas'/></error></message>")
                        self.server.event('xmpp_message_rejected', account_id=self.account_id)
                elif name == 'presence':
                    self._handle_presence(element)
                else:
                    raise ChatRejected('Unsupported XChat stanza')
                await self.writer.drain()
        finally:
            if self.subscription is not None:
                self.server.social_chat.unsubscribe(self.subscription)
                if not self.server.social_chat.friend_online(self.account_id):
                    self._friend_availability('offline')


async def handle_xmpp(server, reader, writer):
    """Authenticate one bounded native chat connection."""
    peer = writer.get_extra_info('peername')
    try:
        if not peer or not server.network.allows(peer[0]):
            server.event('xmpp_address_denied')
            return
        server.event('xmpp_connected')
        await XmppConnection(server, reader, writer).run()
    except ChatRejected as error:
        server.event('xmpp_rejected', operation=str(error))
    except (ConnectionError, OSError, asyncio.TimeoutError,
            ElementTree.ParseError, ValueError) as error:
        server.event('xmpp_rejected', operation=type(error).__name__)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
