import asyncio
import base64
from contextlib import closing
import pickle
from pathlib import Path
import re
import struct
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
import zlib

from solbreaker.account import (client_options_loaded, client_options_saved, create_account,
                               pack_client_options)
from solbreaker.mercury import packed_blob, read_blob
from solbreaker.storage import AccountStore
from solbreaker.social_chat import (ChatBusy, ChatRejected, MAX_PENDING, XMPP_MUC, XMPP_RESOLVER,
                                    SocialChatService, _digest_fields, _digest_response,
                                    handle_xmpp, listener_address, server_configuration)


class Accounts:
    def __init__(self):
        self.rows = {number: SimpleNamespace(id=number, nickname=f'Player{number}')
                     for number in range(1, 5)}

    def get(self, account_id):
        try:
            return self.rows[account_id]
        except KeyError:
            raise ValueError('Unknown local account') from None


class Rooms:
    def __init__(self):
        self.room = SimpleNamespace(id='ABC123', members={1: object(), 2: object()})

    def membership(self, account_id):
        return self.room if account_id in self.room.members else None


class SocialChatConfigurationTests(unittest.TestCase):
    def test_friendship_persists_and_only_recipient_can_approve(self):
        with TemporaryDirectory() as directory:
            private = Path(directory)
            with closing(AccountStore(private)) as store:
                peer = store.complete_registration('peer@example.test', 'Peer', bytes(16), bytes(32)).id
                self.assertEqual(store.change_friendship(1, peer, 'subscribe'), 'requested')
                self.assertIsNone(store.change_friendship(1, peer, 'subscribed'))
                self.assertEqual(store.friendships(1), ((peer, 'Peer', 'outgoing'),))
                self.assertEqual(store.friendships(peer)[0][2], 'incoming')
            with closing(AccountStore(private)) as store:
                self.assertEqual(store.change_friendship(peer, 1, 'subscribed'), 'accepted')
                self.assertEqual(store.friendships(1), ((peer, 'Peer', 'both'),))
                self.assertEqual(store.friendships(peer)[0][2], 'both')
                self.assertEqual(store.change_friendship(peer, 1, 'remove'), 'removed')
                self.assertEqual(store.friendships(1), ())
                self.assertEqual(store.change_friendship(1, peer, 'subscribe'), 'requested')
                self.assertEqual(store.change_friendship(peer, 1, 'unsubscribed'), 'removed')
                self.assertEqual(store.friendships(peer), ())

    def test_native_friends_only_option_survives_reopen_and_merges_partial_saves(self):
        group = b'__formfacindep'
        option = b'ChatMessagesOnlyFromFriends'
        with TemporaryDirectory() as directory:
            private = Path(directory)
            with closing(AccountStore(private)) as store:
                unrelated = {b'Desktop': {b'Accelerometer': b'false'}}
                self.assertEqual(client_options_saved(
                    store, 1, packed_blob(pack_client_options(unrelated))), b'\x5b\x03\0\0\0')
                self.assertEqual(client_options_saved(
                    store, 1, packed_blob(pack_client_options({group: {option: b'false'}}))),
                    b'\x5b\x03\0\0\0')
                # The native client never needs pickle object construction.
                unsafe = zlib.compress(b'ccollections\nOrderedDict\n.')
                self.assertNotEqual(client_options_saved(store, 1, packed_blob(unsafe)), b'\x5b\x03\0\0\0')
            with closing(AccountStore(private)) as store:
                wire = client_options_loaded(store.client_options(1))
                self.assertEqual(wire[0], 100)
                payload, end = read_blob(wire, 6)
                self.assertEqual(end, len(wire))
                self.assertEqual(pickle.loads(zlib.decompress(payload), encoding='bytes'),
                                 unrelated | {group: {option: b'false'}})

    def test_local_nickname_search_is_bounded_literal_and_excludes_requester(self):
        with TemporaryDirectory() as directory, closing(AccountStore(Path(directory))) as store:
            for number in range(24):
                store.complete_registration(f'test{number}@example.test', f'Pilot_{number:02}',
                                            bytes(16), bytes(32))
            store.complete_registration('other@example.test', 'Pilot000', bytes(16), bytes(32))
            excluded, _ = store.search_nicknames('pilot_00', exclude_account_id=1)[0]
            result = store.search_nicknames('PILOT_', exclude_account_id=excluded)
            self.assertEqual(len(result), 20)
            self.assertEqual(result[0][1], 'Pilot_01')
            self.assertNotIn('Pilot000', {nickname for _, nickname in result})
            self.assertFalse(store.search_nicknames('pil%', exclude_account_id=1))

    def test_native_xchat_uses_port_adjacent_to_advertised_base(self):
        network = SimpleNamespace(advertise_base=SimpleNamespace(
            host='192.168.10.5', port=20017))
        config = server_configuration(network)
        self.assertEqual(config['xmpp_host'], '192.168.10.5')
        self.assertEqual(config['xmpp_port'], 20018)
        self.assertEqual(config['xmpp_connections'], [['192.168.10.5', 20018]])
        self.assertEqual(config['xmpp_muc']['muc-service'],
                         {'hostname': '192.168.10.5', 'rooms': ['global']})
        self.assertEqual(config['xmpp_muc']['muc-service-training'],
                         {'hostname': '192.168.10.5'})
        packet = create_account({'id': 1, 'nickname': 'Commander'}, server_settings=config)
        self.assertEqual(struct.unpack_from('<Q', packet, 9), (1,))
        nickname, offset = read_blob(packet, 17)
        profile, offset = read_blob(packet, offset)
        settings, offset = read_blob(packet, offset)
        self.assertEqual(nickname, b'Commander')
        self.assertEqual(pickle.loads(profile), {})
        self.assertEqual(pickle.loads(settings), config)
        self.assertEqual(offset, len(packet))
        self.assertEqual(listener_address(SimpleNamespace(
            listen_base=SimpleNamespace(host='0.0.0.0', port=20017))),
            ('0.0.0.0', 20018))

    def test_native_xchat_rejects_unavailable_adjacent_port(self):
        network = SimpleNamespace(advertise_base=SimpleNamespace(
            host='127.0.0.1', port=65535))
        with self.assertRaises(ValueError):
            server_configuration(network)


class SocialChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 10.
        self.events = []
        self.rooms = Rooms()
        self.service = SocialChatService(Accounts(), lambda: self.rooms,
            event=lambda name, **fields: self.events.append((name, fields)),
            monotonic=lambda: self.now, wall_clock=lambda: 123.)
        self.received = {number: [] for number in range(1, 5)}
        self.subscriptions = [self.service.subscribe(number, self.received[number].append)
                              for number in self.received]

    async def flush(self):
        while self.service._scheduled:
            await asyncio.sleep(0)

    async def test_global_chat_is_deferred_and_preserves_text_without_filtering(self):
        message = self.service.send(1, '  Блин, это общий чат!  ')
        self.assertFalse(any(self.received.values()))
        await self.flush()
        self.assertEqual(message.text, '  Блин, это общий чат!  ')
        self.assertEqual([rows[0] for rows in self.received.values()], [message] * 4)
        self.assertEqual(message.created_at, 123.)

    async def test_room_chat_reaches_only_current_members(self):
        message = self.service.send(1, 'Комната', kind='room')
        await self.flush()
        self.assertEqual(self.received[1], [message])
        self.assertEqual(self.received[2], [message])
        self.assertFalse(self.received[3])
        self.assertFalse(self.received[4])
        self.assertEqual(message.room_id, 'ABC123')

    async def test_stale_room_target_and_membership_changes_do_not_leak_chat(self):
        with self.assertRaises(ChatRejected):
            self.service.send(1, 'Старая комната', kind='room', expected_room='OLD')
        message = self.service.send(1, 'Текущая комната', kind='room', expected_room='ABC123')
        del self.rooms.room.members[2]
        self.rooms.room.members[3] = object()
        await self.flush()
        self.assertEqual(self.received[1], [message])
        self.assertFalse(self.received[2])
        self.assertFalse(self.received[3])

    async def test_direct_chat_reaches_sender_and_recipient_sessions(self):
        second = self.service.subscribe(2, self.received[2].append)
        message = self.service.send(1, 'Лично', kind='direct', recipient=2)
        await self.flush()
        self.assertEqual(self.received[1], [message])
        self.assertEqual(self.received[2], [message, message])
        self.assertFalse(self.received[3])
        self.service.unsubscribe(second)

    async def test_sender_rate_limit_and_validation_are_bounded(self):
        self.service.send(1, 'Первое')
        with self.assertRaises(ChatBusy):
            self.service.send(1, 'Слишком быстро')
        self.now += .5
        with self.assertRaises(ChatRejected):
            self.service.send(1, 'x' * 501)
        with self.assertRaises((ChatRejected, ValueError)):
            self.service.send(1, 'Лично', kind='direct', recipient=999)
        with self.assertRaises(ChatRejected):
            self.service.send(3, 'Не в комнате', kind='room')
        await self.flush()

    async def test_pending_queue_has_a_hard_limit(self):
        for sender in range(1, 5):
            self.service._after[sender] = 0.
        for number in range(MAX_PENDING):
            sender = number % 4 + 1
            self.service._after[sender] = 0.
            self.service.send(sender, str(number))
        self.service._after[1] = 0.
        with self.assertRaises(ChatBusy):
            self.service.send(1, 'overflow')
        await self.flush()


class XmppAccounts(Accounts):
    def __init__(self):
        super().__init__()
        self.store = SimpleNamespace(search_nicknames=self.search_nicknames,
                                     friendships=lambda account_id: ())
        self.web = {
            'another-live-token': SimpleNamespace(account_id=1, expires=time.time() + 60),
            'local-spa-token': SimpleNamespace(account_id=1, expires=time.time() + 60),
        }

    def search_nicknames(self, prefix, *, exclude_account_id):
        return tuple((account.id, account.nickname) for account in self.rows.values()
                     if account.id != exclude_account_id and account.nickname.startswith(prefix))

    @staticmethod
    def valid(collection, token):
        session = collection.get(token)
        return session is not None and session.expires > time.time()


class XmppTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.accounts = XmppAccounts()
        self.received = []
        self.rooms = Rooms()
        self.social = SocialChatService(self.accounts, lambda: self.rooms,
            event=lambda name, **fields: self.events.append((name, fields)))
        self.other_subscription = self.social.subscribe(2, self.received.append)
        network = SimpleNamespace(
            advertise_base=SimpleNamespace(host='chat.local', port=20017),
            allows=lambda address: address == '127.0.0.1')
        self.server = SimpleNamespace(accounts=self.accounts, network=network,
                                      social_chat=self.social,
                                      event=lambda name, **fields: self.events.append((name, fields)))
        self.listener = await asyncio.start_server(
            lambda reader, writer: handle_xmpp(self.server, reader, writer), '127.0.0.1', 0)
        port = self.listener.sockets[0].getsockname()[1]
        self.reader, self.writer = await asyncio.open_connection('127.0.0.1', port)

    async def asyncTearDown(self):
        self.writer.close()
        await self.writer.wait_closed()
        self.listener.close()
        await self.listener.wait_closed()
        self.social.unsubscribe(self.other_subscription)

    async def read_through(self, marker):
        return (await asyncio.wait_for(self.reader.readuntil(marker), 1)).decode()

    async def authenticate(self):
        stream = ("<stream:stream to='chat.local' xmlns='jabber:client' "
                  "xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>")
        self.writer.write(stream.encode())
        features = await self.read_through(b'</stream:features>')
        self.assertIn('DIGEST-MD5', features)
        self.writer.write(
            b"<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='DIGEST-MD5'/>")
        challenge = await self.read_through(b'</challenge>')
        encoded = re.search(r'<challenge[^>]*>([^<]+)</challenge>', challenge).group(1)
        fields = _digest_fields(base64.b64decode(encoded).decode())
        response = dict(username='1', realm='chat.local', nonce=fields['nonce'],
                        cnonce='client-nonce', nc='00000001', qop='auth',
                        **{'digest-uri': 'xmpp/chat.local'})
        response['response'] = _digest_response('1', 'chat.local', 'local-spa-token',
            response['nonce'], response['cnonce'], response['nc'], response['qop'],
            response['digest-uri'])
        raw = ','.join(f'{key}="{value}"' for key, value in response.items())
        self.writer.write(("<response xmlns='urn:ietf:params:xml:ns:xmpp-sasl'>"
            + base64.b64encode(raw.encode()).decode() + '</response>').encode())
        await self.read_through(b'</challenge>')
        self.writer.write(b"<response xmlns='urn:ietf:params:xml:ns:xmpp-sasl'/>")
        self.assertIn('success', await self.read_through(b'/>'))
        self.writer.write(stream.encode())
        self.assertIn('xmpp-bind', await self.read_through(b'</stream:features>'))

    async def test_digest_login_bind_and_direct_message(self):
        await self.authenticate()
        self.writer.write(
            b"<iq type='set' id='bind-1'><bind xmlns='urn:ietf:params:xml:ns:xmpp-bind'>"
            b'<resource>wotbi</resource></bind></iq>')
        bound = await self.read_through(b'</iq>')
        self.assertIn('1@chat.local/wotbi', bound)
        self.writer.write(
            b"<iq type='get' id='roster-1'><query xmlns='jabber:iq:roster'/></iq>")
        roster = await self.read_through(b'</iq>')
        self.assertIn("xmlns='jabber:iq:roster'", roster)
        self.writer.write(
            (f"<iq type='get' id='lookup-1'><nickname-prefix-search xmlns='{XMPP_RESOLVER}' "
             "prefix='Player'/></iq>").encode())
        lookup = await self.read_through(b'</iq>')
        self.assertIn("<item id='2' nickname='Player2'>"
                      "<wgexts xmlns='http://wargaming.net/xmpp#v3'/></item>", lookup)
        self.assertNotIn('Player1', lookup)
        self.writer.write(b"<message to='2@wrong.local' type='chat' id='bad-target'>"
                          b'<body>this must not deliver</body></message>')
        error = await self.read_through(b'</message>')
        self.assertIn('not-acceptable', error)
        message = "<message to='2@chat.local' type='chat'><body>Привет, damn</body></message>"
        encoded = message.encode()
        split = encoded.index("П".encode()) + 1
        self.writer.write(encoded[:split])
        await self.writer.drain()
        # A TCP read can end between bytes of one Cyrillic character.
        await asyncio.sleep(.01)
        self.writer.write(encoded[split:])
        delivery = await self.read_through(b'</message>')
        self.assertIn('Привет, damn', delivery)
        self.assertIn("<wgexts xmlns='http://wargaming.net/xmpp#v3'>"
                      '<nickname>Player1</nickname><userid>1</userid></wgexts>', delivery)
        while self.social._scheduled:
            await asyncio.sleep(0)
        self.assertEqual([message.text for message in self.received], ['Привет, damn'])
        self.assertFalse(any(name == 'xmpp_rejected' for name, unused in self.events))
        self.assertTrue(any(name == 'xmpp_bound' for name, unused in self.events))

    async def test_stream_rejects_a_domain_other_than_server_configuration(self):
        self.writer.write(
            b"<stream:stream to='attacker.invalid' xmlns='jabber:client' "
            b"xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>")
        self.assertEqual(await asyncio.wait_for(self.reader.read(), 1), b'')
        self.assertTrue(any(name == 'xmpp_rejected' for name, unused in self.events))

    async def test_roster_restores_friends_and_undecided_incoming_requests(self):
        self.accounts.store.friendships = lambda account_id: (
            (3, 'Player3', 'incoming'), (2, 'Player2', 'both'))
        await self.authenticate()
        self.writer.write(
            b"<iq type='set' id='bind'><bind xmlns='urn:ietf:params:xml:ns:xmpp-bind'/></iq>")
        await self.read_through(b'</iq>')
        self.writer.write(
            b"<iq type='get' id='roster'><query xmlns='jabber:iq:roster'/></iq>")
        roster = await self.read_through(b'</iq>')
        self.assertIn("jid='2@chat.local' name='Player2' subscription='both'", roster)
        self.assertNotIn('Player3', roster)
        invite = await self.read_through(b'</presence>')
        self.assertIn("from='3@chat.local' to='1@chat.local/wotbi' type='subscribe'", invite)
        self.assertIn('<nickname>Player3</nickname><userid>3</userid>', invite)

    async def test_room_invitation_join_and_membership_revocation(self):
        await self.authenticate()
        self.writer.write(b"<iq type='set' id='bind'><bind xmlns='urn:ietf:params:xml:ns:xmpp-bind'/></iq>")
        await self.read_through(b'</iq>')
        self.writer.write(b'<presence/>')
        channel = f'{int(self.rooms.room.id, 16)}-common@chat.local'
        invite = await self.read_through(b'</message>')
        self.assertIn(f"from='{channel}'", invite)
        self.assertIn(f"xmlns='{XMPP_MUC}#user'", invite)
        blocked = f"<presence to='1-common@chat.local/1'><x xmlns='{XMPP_MUC}'/></presence>"
        self.writer.write(blocked.encode())
        self.assertIn('forbidden', await self.read_through(b'</presence>'))
        join = f"<presence to='{channel}/1'><x xmlns='{XMPP_MUC}'/></presence>"
        self.writer.write(join.encode())
        self.assertIn("<status code='110'/>", await self.read_through(b'</presence>'))
        message = f"<message type='groupchat' to='{channel}'><body>Private room</body></message>"
        self.writer.write(message.encode())
        self.assertIn('Private room', await self.read_through(b'</message>'))
        self.assertEqual([row.text for row in self.received], ['Private room'])
        del self.rooms.room.members[1]
        self.social.room_changed(1)
        self.assertIn("type='unavailable'", await self.read_through(b'</presence>'))
        self.writer.write(message.encode())
        self.assertIn('not-acceptable', await self.read_through(b'</message>'))
        self.assertEqual([row.text for row in self.received], ['Private room'])
