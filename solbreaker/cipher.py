"""BigWorld's Blowfish filter (each block is chained to previous plaintext)."""
import threading
from cryptography.hazmat.decrepit.ciphers.algorithms import Blowfish
from cryptography.hazmat.primitives.ciphers import Cipher, modes

MAGIC = b"\xef\xbe\xad\xde"


def _chain(data):
    plain = int.from_bytes(data, 'big')
    return (plain ^ (plain >> 64)).to_bytes(len(data), 'big')


def _unchain(data):
    plain = int.from_bytes(data, 'big')
    shift = 64
    while shift < len(data)*8:
        plain ^= plain >> shift
        shift *= 2
    return plain.to_bytes(len(data), 'big')


def crypt(data: bytes, key: bytes, *, decrypt=False) -> bytes:
    if not 4 <= len(key) <= 56 or len(data) % 8:
        raise ValueError("Invalid Blowfish key or block length")
    cipher = Cipher(Blowfish(key), modes.ECB())
    operation = cipher.decryptor() if decrypt else cipher.encryptor()
    if not decrypt:
        # E_i = Blowfish(P_i XOR P_{i-1}), with P_{-1}=0. Every input
        # block is known up front, so a single ECB update is sufficient.
        return operation.update(_chain(data)) + operation.finalize()
    mixed = operation.update(data) + operation.finalize()
    # D_i = P_i XOR P_{i-1}. Parallel prefix XOR recovers all plaintext
    # blocks, including non-power-of-two packet lengths and leading zeros.
    return _unchain(mixed)


class PacketCipher:
    """One session's ECB schedules; plaintext chaining resets per packet."""
    __slots__ = ('key', '_encryptor', '_decryptor', '_lock', '__weakref__')

    def __init__(self, key):
        if not 4 <= len(key) <= 56:
            raise ValueError("Invalid Blowfish key length")
        self.key = bytes(key)
        cipher = Cipher(Blowfish(self.key), modes.ECB())
        self._encryptor, self._decryptor = cipher.encryptor(), cipher.decryptor()
        self._lock = threading.Lock()

    def crypt(self, data, *, decrypt=False):
        if len(data) % 8:
            raise ValueError("Invalid Blowfish block length")
        # Every update is block aligned. ECB carries no chain between calls;
        # only the packet-local prefix XOR above implements the client's filter.
        if decrypt:
            with self._lock:
                mixed = self._decryptor.update(data)
            return _unchain(mixed)
        mixed = _chain(data)
        with self._lock:
            return self._encryptor.update(mixed)


def _packet_crypt(data, key, codec, *, decrypt=False):
    if codec is None:
        return crypt(data, key, decrypt=decrypt)
    if not isinstance(codec, PacketCipher) or codec.key != key:
        raise ValueError("Packet cipher does not match the session key")
    return codec.crypt(data, decrypt=decrypt)


def encrypt_packet(data: bytes, key: bytes, *, codec=None) -> bytes:
    padding = (-(len(data) + 5)) % 8
    # the client's filter strips a zero-terminated junk prefix before decrypting.
    # An empty prefix is sufficient and deterministic for the local server.
    return b"\0" + _packet_crypt(data + bytes(padding) + MAGIC + bytes([padding + 1]), key, codec)


def decrypt_packet(data: bytes, key: bytes, *, codec=None) -> bytes:
    delimiter = data.find(b"\0", 0, 256)
    if delimiter < 0:
        raise ValueError("Missing encrypted-packet junk delimiter")
    plain = _packet_crypt(data[delimiter + 1:], key, codec, decrypt=True)
    if len(plain) < 8 or plain[-5:-1] != MAGIC or not 1 <= plain[-1] <= 8:
        raise ValueError("Invalid encrypted packet footer")
    end = len(plain) - 4 - plain[-1]
    if end < 2:
        raise ValueError("Invalid encrypted packet length")
    return plain[:end]
