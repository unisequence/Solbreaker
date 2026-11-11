"""Enable the existing battle statistics panel in the pinned private client."""
import hashlib
import json
import os
from pathlib import Path

INPUT_SHA256 = '7417ea284900655070219295078edecb807d41fff7632096d0118e7d9013fc99'
OUTPUT_SHA256 = '24d6b5a95649003c2353f7e341e880641e7f6e9eb1a522f1f18e862f7ff7fa8e'
OFFSET = 0x19b5a52
BEFORE = b'\x72\x1a'
AFTER = b'\x90\x90'
METADATA = {'version':'battle-statistics-v1','input_sha256':INPUT_SHA256,
            'output_sha256':OUTPUT_SHA256,'scope':'HUD progress threshold'}


def patch(data):
    if hashlib.sha256(data).hexdigest() != INPUT_SHA256 or data[OFFSET:OFFSET+2] != BEFORE:
        raise ValueError('Unsupported battle statistics patch input')
    output = bytearray(data)
    output[OFFSET:OFFSET+2] = AFTER
    if hashlib.sha256(output).hexdigest() != OUTPUT_SHA256:
        raise ValueError('Unexpected battle statistics patch output')
    return bytes(output)


def upgrade(lab):
    from . import client_patch as cp
    lab=Path(lab); manifest_path=lab/'manifest.json'; manifest=json.loads(manifest_path.read_text())
    source=Path(manifest['game'])/'wotblitz.exe'; target=lab/'client/wotblitz.exe'
    if cp._sha256(source)!=cp.BUILD_SHA256 or manifest['exe_sha256']!=cp.BUILD_SHA256:
        raise ValueError('Game executable differs from the pinned PoC build')
    if target.is_symlink() or target.resolve(strict=True)==source.resolve(strict=True) or os.path.samefile(source,target):
        raise ValueError('Lab executable aliases the pinned game executable')
    data=target.read_bytes(); current=hashlib.sha256(data).hexdigest()
    recorded=manifest.get('client_exe_sha256',manifest['exe_sha256'])
    if cp._is_recon_client(current, recorded):
        return False
    if current not in (INPUT_SHA256,OUTPUT_SHA256) or recorded not in (INPUT_SHA256,OUTPUT_SHA256):
        raise ValueError('Battle statistics require a supported recorded lab client')
    if current==INPUT_SHA256:
        if recorded!=current: raise ValueError('Lab executable differs from its recorded fingerprint')
        cp._write_client(target,patch(data))
    elif recorded==current and manifest.get('battle_statistics')==METADATA:
        return False
    manifest['client_exe_sha256']=OUTPUT_SHA256
    manifest['battle_statistics']=dict(METADATA)
    cp._write_manifest(manifest_path,manifest)
    return True
