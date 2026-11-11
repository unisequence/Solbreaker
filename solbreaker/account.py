"""Account messages recovered from the installed compatible build 11.21 client."""
import struct
import time
import json
import zlib

from .mercury import packed_blob


def pickle_value(value):
    """Encode only the primitive Python 2 pickle values used in our messages."""
    if value is None:
        return b"N"
    if isinstance(value, bool):
        return b"\x88" if value else b"\x89"
    if isinstance(value, int):
        if 0 <= value <= 255:
            return b"K" + bytes([value])
        if -0x80000000 <= value <= 0x7fffffff:
            return b"J" + struct.pack("<i", value)
        return b"L" + str(value).encode() + b"L\n"
    if isinstance(value, float):
        return b"G" + struct.pack(">d", value)
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return (b"U" + bytes([len(value)]) if len(value) < 256 else b"T" + struct.pack("<I", len(value))) + value
    if isinstance(value, dict):
        return b"}(" + b"".join(pickle_value(k) + pickle_value(v) for k, v in value.items()) + b"u"
    if isinstance(value, list):
        return b"](" + b"".join(pickle_value(v) for v in value) + b"e"
    if isinstance(value, tuple):
        return b"(" + b"".join(pickle_value(v) for v in value) + b"t"
    raise TypeError(type(value).__name__)


def pickle_document(value):
    return b"\x80\x02" + pickle_value(value) + b"."


def variable16(kind, body):
    if len(body) >= 65535:
        raise ValueError("Message is too large")
    return bytes([kind]) + struct.pack("<H", len(body)) + body


def create_account(account):
    # ServerConnection::createBasePlayer reads entity ID u32 and type u16.
    # Account::readBaseProperties reads u64 and three packed byte strings.
    properties = (struct.pack("<Q", account["id"]) + packed_blob(account["nickname"].encode())
                  + packed_blob(pickle_document({})) + packed_blob(pickle_document({})))
    return variable16(5, struct.pack("<IH", 100, 0) + properties)


def show_gui(account, prebattle_id=0):
    now = time.time()
    payload = packed_blob(pickle_document({"databaseID": account["id"], "inRandomQueue": False,
        "inTutorialQueue": False, "isAogasEnabled": False, "prebattleID": prebattle_id,
        "serverUTC": now, "sessionStartedAt": now, "aogasStartedAt": 0.0}))
    # ClientInterface::entityMethod starts at 59. Account method 29 is showGUI,
    # with a one-byte variable length according to getMethodStreamSize.
    if len(payload) >= 255:
        raise ValueError("showGUI exceeds the compact message length")
    return bytes([59 + 29, len(payload)]) + payload


def account_method(method, payload):
    if not 0 <= method <= 42:
        raise ValueError("Unknown Account method")
    fixed = [0] * 14 + [1] * 3 + [4] * 2 + [5, 12, 16]
    if method < 22:
        if len(payload) != fixed[method]:
            raise ValueError("Invalid fixed Account method payload size")
        return bytes([59 + method]) + payload
    if method < 37:
        if len(payload) >= 255:
            raise ValueError("Extended variable8 messages are not implemented")
        return bytes([59 + method, len(payload)]) + payload
    return variable16(59 + method, payload)


def command_response(request_id, result, data=None, message="", *, raw=None):
    """onCmdResponse: request u16, result i16, text, optional pickled value."""
    payload = (struct.pack("<Hh", request_id, result) + packed_blob(message.encode())
               + packed_blob(raw if raw is not None else pickle_document(data) if data is not None else b""))
    return account_method(38, payload)


def random_queue_message(enqueued):
    # Pinned Account::onMethod 1/2 -> AccountGameLogic vtable 0x4c/0x54.
    # These set/clear inRandomQueue and notify the stock queue controller.
    return account_method(1 if enqueued else 2, b'')


def _profile_update_message(snapshot):
    return account_method(40, packed_blob(pickle_document(snapshot)))


def _merge_profile_part(target, part):
    """Copy map layers while collecting disjoint, complete profile records."""
    merged = dict(target)
    for key, value in part.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_profile_part(merged[key], value)
        else:
            merged[key] = value
    return merged


def _bounded_profile_messages(snapshot, first_encoder):
    """Encode the whole ordered batch before exposing any bytes to transport."""
    for key in ('rev',) + (('prevRev',) if 'prevRev' in snapshot else ()):
        if type(snapshot.get(key)) is not int:
            raise ValueError(f'Account profile requires integer {key}')
    try:
        # Preserve the existing wire bytes and response form for small profiles.
        return first_encoder(snapshot)
    except ValueError as exc:
        if str(exc) != 'Message is too large':
            raise

    from .profile import account_profile_parts
    for ammo_only in (True, False):
        baseline, parts = account_profile_parts(snapshot, ammo_only=ammo_only)
        try:
            first_encoder(baseline)
        except ValueError as exc:
            if str(exc) != 'Message is too large':
                raise
            continue
        break
    else:
        raise ValueError('Account bootstrap metadata exceeds the message limit')

    messages = []
    current = baseline
    encoder = first_encoder
    following = {'prevRev': snapshot['rev'], 'rev': snapshot['rev']}
    offset = 0
    while True:
        # Prefix sizes increase monotonically because records are disjoint.
        # Search for the largest fitting prefix instead of serializing an
        # almost-full 758-vehicle profile once for every preference record.
        low, high = 0, len(parts) - offset
        encoded = encoder(current)
        while low < high:
            count = (low + high + 1) // 2
            candidate = current
            for part in parts[offset:offset + count]:
                candidate = _merge_profile_part(candidate, part)
            try:
                candidate_bytes = encoder(candidate)
            except ValueError as exc:
                if str(exc) != 'Message is too large':
                    raise
                high = count - 1
            else:
                low = count
                encoded = candidate_bytes
        if low == 0 and offset < len(parts) and messages:
            # No truncation or splitting inside a vehicle's saved layouts.
            # An unrepresentable record fails before the batch can be sent.
            encoder(_merge_profile_part(current, parts[offset]))
        messages.append(encoded)
        offset += low
        if offset == len(parts):
            break
        current = following
        encoder = _profile_update_message
    return b''.join(messages)


def account_update(snapshot):
    """One Account40 update, or ordered same-revision Account40 chunks.

    Native 0x1471e30 accepts equal revisions and merges maps recursively.
    Each chunk notifies observers separately; this is not a wire transaction.
    """
    return _bounded_profile_messages(snapshot, _profile_update_message)


def account_sync_response(request_id, snapshot):
    """One command100 response followed, if needed, by Account40 deltas.

    A fresh Account's snapshot omits prevRev to initialize the native cache.
    Only the first message completes the request ID. Later chunks have a
    present prevRev equal to the already installed revision, and never repeat
    a command response or change the established pickle/compression format.
    """
    return _bounded_profile_messages(
        snapshot, lambda value: command_response(request_id, 0, value))


def spa_token_response(request_id, token_type, account_id, token=None):
    """Account method 27: request u16, token type u8, packed pickle.

    SPAAuthTokenRequester::ParsePickledAuthData reads token/databaseID/error.
    This callback is independent of onCmdResponse.
    """
    data = {"databaseID": account_id, "token": token or "",
            "error": "" if token else "LOCAL_SERVICE_UNAVAILABLE"}
    return account_method(27, struct.pack("<HB", request_id, token_type)
                          + packed_blob(pickle_document(data)))


def command_header(method, payload):
    # Generated native writers use u16 request + u16 command. Other base
    # methods (chat, telemetry, readiness) have different, independent schemas.
    if method in (3, 5, 6, 8, 9, 10, 11, 12, 13, 14) and len(payload) >= 4:
        return struct.unpack_from("<HH", payload)
    return None


def resource_response(request_id, value):
    """RES_STREAM followed by a Mercury resource containing a zlib pickle."""
    data = zlib.compress(pickle_document(value))
    if len(data) > 60000:
        raise ValueError("Local resource exceeds the single-fragment limit")
    # The transport can split this bundle over multiple reliable UDP packets.
    header = variable16(52, struct.pack("<H", request_id) + packed_blob(b""))
    fragment = variable16(53, struct.pack("<HBB", request_id, 0, 1) + data)
    return command_response(request_id, 1) + header + fragment


def balance_update(previous_revision, progress, *, garage=None):
    # AccountGameLogic::update (0x1845fa0) unpickles method 40. A present
    # prevRev selects a delta; unrelated cache/inventory fields stay intact.
    delta = dict(prevRev=previous_revision,rev=progress['revision'],
                 stats=dict(credits=progress['credits'], gold=progress.get('gold', 0)))
    if garage is not None:
        delta['stats'].update(freeXP=garage['free_xp'],
                             vehTypeXP={v['type_cd']:v['xp']
                                        for v in garage.get('type_xp',garage['vehicles'])})
    return account_method(40,packed_blob(pickle_document(delta)))


def local_http_response(operation):
    """Native HTTP-over-Account envelope for the local shop prerequisites."""
    if operation == 27:
        # Local currency metadata only; this endpoint does not contact Steam
        # or implement purchases.
        document = {"response": {"result": "OK", "params": {"country": "US", "currency": "USD"}}}
    elif operation == 28:
        document = []  # The local account has no DLC purchase entitlements.
    elif operation == 14:
        # ClanRequesterImpl accepts a root clan object or a nested clan.
        # A local account without a clan has neither clan nor role details.
        document = {"clan": None, "role_details": None}
    else:
        return None
    # Native PyValue::asTuple rejects a Python list, even with the same items.
    return zlib.compress(pickle_document((200, json.dumps(document, separators=(",", ":")))))
