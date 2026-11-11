"""Enable certificate-chain validation in the pinned private 11.21 client.

The account HTTP transport and DAVA downloader explicitly pass zero to
CURLOPT_SSL_VERIFYPEER (64). VERIFYHOST retains libcurl's default value 2.
Two setup calls additionally select the private CA bundle explicitly.
The position-independent thunk extends the existing owned RX section.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import sys

from .certificate_authorities import ca_file, validate_ca


OUTPUTS = {
    'a8cd7bd4e1c70e25f43f8a0d5b3c645325e0590cb255211212f307fbfdee039d':
        '0ac40a0ba3f1df41118c3fdf8ebdaaaa20d480972ae0d42ffd36f6e327df087f',
    'd0334eeb1cf0900da470a52a63663a38b689f93de87cc78692e3148b7134dd8f':
        '8f138f55456a52cf5bfb1c19f724f71d369154ee9b0e06e5e9dafb2bd5e87139',
}
PATCHES = (0xFB0980, 0x4F0E3E)
CALLS = ((0xFB0999, 0x13B1599), (0x4F0E43, 0x8F1A43))
THUNK_RVA = 0x471E800
THUNK_RAW = 0x466E400
SETOPT_VA = 0x235D550
METADATA = {'version': 'curl-verify-peer-v1', 'verify_peer': True, 'ca_bundle': 'Data/tfs-ca.pem'}


def _thunk():
    """cdecl(handle, option, value), forwarding setup and adding CAINFO."""
    base = 0x400000 + THUNK_RVA
    code = bytearray.fromhex('5589e5ff7510ff750cff7508e8')
    code += struct.pack('<i', SETOPT_VA - (base + len(code) + 4))
    code += bytes.fromhex('83c40c85c07500')
    failure_branch = len(code) - 1
    code += bytes.fromhex('e80000000058')  # Obtain the ASLR-adjusted instruction address.
    pc = len(code) - 1
    code += b'\x05' + bytes(4)
    string_operand = len(code) - 4
    code += b'\x50\x68' + struct.pack('<I', 10065)  # CAINFO, same CURL handle.
    code += bytes.fromhex('ff7508e8')
    code += struct.pack('<i', SETOPT_VA - (base + len(code) + 4))
    code += bytes.fromhex('83c40c')
    code[failure_branch] = len(code) - failure_branch - 1
    code += bytes.fromhex('5dc3')
    struct.pack_into('<i', code, string_operand, len(code) - pc)
    code += b'Data/tfs-ca.pem\x00'
    return bytes(code)


def base_hash(current):
    return next((before for before, after in OUTPUTS.items() if current == after), current)


def recognizes(current, recorded):
    before = base_hash(current)
    from . import windows_profile_patch as profile
    if current == OUTPUTS[profile.OUTPUT_SHA256]:
        return recorded in (before, current, profile.INPUT_SHA256, OUTPUTS[profile.INPUT_SHA256])
    return current != before and recorded in (before, current)


def patch(data):
    before = hashlib.sha256(data).hexdigest()
    if before not in OUTPUTS:
        raise ValueError('TLS validation requires the pinned recon client')
    result = bytearray(data)
    for offset in PATCHES:
        if result[offset:offset+4] != b'\x6a\x00\x6a\x40':
            raise ValueError('TLS option instruction differs from the pinned build')
        result[offset+1] = 1
    if (len(data) != THUNK_RAW
            or struct.unpack_from('<IIII', data, 0x3D0) != (0x2735, 0x471C000, 0x2800, 0x466BC00)):
        raise ValueError('TLS extension requires the pinned owned RX section')
    for offset, va in CALLS:
        before_call = b'\xe8' + struct.pack('<i', SETOPT_VA - va - 5)
        if result[offset:offset+5] != before_call:
            raise ValueError('TLS setup call differs from the pinned build')
        result[offset:offset+5] = b'\xe8' + struct.pack('<i', 0x400000 + THUNK_RVA - va - 5)
    code = _thunk()
    result.extend(code.ljust(512, b'\x00'))
    struct.pack_into('<I', result, 0x3D0, 0x2800 + len(code))
    struct.pack_into('<I', result, 0x3D8, 0x2A00)
    struct.pack_into('<I', result, 0x1FC, 0x32F0200)
    if hashlib.sha256(result).hexdigest() != OUTPUTS[before]:
        raise ValueError('Unexpected TLS validation client image')
    return bytes(result)


def remove(data):
    current = hashlib.sha256(data).hexdigest()
    before = base_hash(current)
    if before == current:
        raise ValueError('Expected the pinned TLS validation client')
    result = bytearray(data)
    for offset in PATCHES:
        result[offset+1] = 0
    for offset, va in CALLS:
        result[offset:offset+5] = b'\xe8' + struct.pack('<i', SETOPT_VA - va - 5)
    del result[THUNK_RAW:]
    struct.pack_into('<I', result, 0x3D0, 0x2735)
    struct.pack_into('<I', result, 0x3D8, 0x2800)
    struct.pack_into('<I', result, 0x1FC, 0x32F0000)
    if hashlib.sha256(result).hexdigest() != before:
        raise ValueError('Unexpected TLS predecessor image')
    return bytes(result)


def upgrade(lab):
    from . import client_patch as cp
    from .platform_runtime import require_executable_idle
    lab = Path(lab)
    path = lab / 'manifest.json'
    manifest = json.loads(path.read_text())
    target = lab / 'client/wotblitz.exe'
    source = Path(manifest['game']) / 'wotblitz.exe'
    if (manifest['exe_sha256'] != cp.BUILD_SHA256 or cp._sha256(source) != cp.BUILD_SHA256
            or target.is_symlink() or os.path.samefile(source, target)):
        raise ValueError('TLS validation requires a separate pinned client copy')
    require_executable_idle(target)
    data = target.read_bytes()
    current = hashlib.sha256(data).hexdigest()
    recorded = manifest.get('client_exe_sha256', manifest['exe_sha256'])
    if current in OUTPUTS and recorded == current:
        cp._write_client(target, patch(data))
        current = OUTPUTS[current]
    elif not recognizes(current, recorded):
        raise ValueError('TLS validation requires a supported recorded client')
    elif recorded == current and manifest.get('http_tls_client') == METADATA:
        return False
    manifest['client_exe_sha256'] = current
    manifest['http_tls_client'] = dict(METADATA)
    cp._write_manifest(path, manifest)
    return True


def configure_environment(environment, lab, manifest):
    from .connection import atomic_write, ensure_lab
    lab, _ = ensure_lab(lab)
    path = ca_file(lab, manifest)
    atomic_write(Path(lab) / 'client/Data/tfs-ca.pem', path.read_bytes())
    environment['SSL_CERT_FILE'] = (str(path) if sys.platform == 'win32'
                                    else 'Z:' + str(path).replace('/', '\\'))
