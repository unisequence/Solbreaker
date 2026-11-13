"""Enable the native target-lock input for missiles in the pinned client."""
import hashlib

from . import client_patch

INPUT_SHA256 = '8f138f55456a52cf5bfb1c19f724f71d369154ee9b0e06e5e9dafb2bd5e87139'
OUTPUT_SHA256 = '2dc4ab995f04313d93b998b366067538b035d45a696b4ced046de356ca7b5a3d'
PATCHES = (
    # VA 0x1df93ab. Keep the normal enemy selection and toggle even if
    # the selected shot descriptor has isATGM set.
    (0x19f87ab, bytes.fromhex('0f85e1000000'), b'\x90'*6),
    # AvatarGameLogic::updateVehicleSetting, VA 0x18473d4. Use the
    # conventional shell-change exit instead of broadcasting an unlock.
    (0x14467d4, bytes.fromhex('0f8408010000'), bytes.fromhex('e90901000090')),
)


def recognizes(current, recorded):
    return current == OUTPUT_SHA256 and recorded in (INPUT_SHA256, OUTPUT_SHA256)


def patch(data):
    if hashlib.sha256(data).hexdigest() != INPUT_SHA256:
        raise ValueError('ATGM autoaim requires the pinned prepared client')
    result = bytearray(data)
    for offset, before, after in PATCHES:
        end = offset+len(before)
        if data[offset:end] != before:
            raise ValueError('ATGM autoaim instruction window differs')
        result[offset:end] = after
    if hashlib.sha256(result).hexdigest() != OUTPUT_SHA256:
        raise ValueError('Unexpected ATGM autoaim client image')
    return result


def upgrade(lab, manifest, current):
    """Called after validating the private client path and taking its lock."""
    recorded = manifest.get('client_exe_sha256')
    if current == recorded and current != INPUT_SHA256:
        return manifest
    # A previous attempt can stop after replacing the image but before the
    # manifest is saved. Both exact fingerprints are known in that case.
    if (current not in (INPUT_SHA256, OUTPUT_SHA256) or recorded != INPUT_SHA256
            or manifest.get('exe_sha256') != client_patch.BUILD_SHA256):
        raise ValueError('Клиент отличается от подготовленной копии')
    target = lab/'client/wotblitz.exe'
    if target.resolve(strict=True) != target or target.stat().st_nlink != 1:
        raise ValueError('ATGM autoaim requires a private executable')
    if current == INPUT_SHA256:
        client_patch._write_client(target, patch(target.read_bytes()))
    updated = dict(manifest, client_exe_sha256=OUTPUT_SHA256)
    client_patch._write_manifest(lab/'manifest.json', updated)
    return updated
