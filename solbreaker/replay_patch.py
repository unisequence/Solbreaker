"""Pinned replay controls extension; modifies private client copies only.

The original rewind and speed handlers are no-ops. These small position
independent callbacks use the existing replay player's seek and time-scale
methods. The two callback operands already have HIGHLOW relocation entries.
"""
import hashlib
import json
import os
from pathlib import Path
import struct


INPUT_SHA256 = "386f2e0f46cf22d9fc45e8440da7cf9426e2ff015484c948e5e7dc6864e97ca3"
OUTPUT_SHA256 = "7417ea284900655070219295078edecb807d41fff7632096d0118e7d9013fc99"
OWNED_CODE_SHA256 = "4dfafb72829d0e3fc760e9687eb7e07179c2f1f241e71f0646791bd34542d3a8"
SECTION_RVA = 0x471C000
SECTION_RAW = 0x466BC00
REWIND_RVA = SECTION_RVA + 512
SPEED_RVA = SECTION_RVA + 560
SEEK_RESET_RVA = SECTION_RVA + 672
SEEK_SAVE_RVA = 0xFE5F5F
TIMELINE_OFFSET = 0x1A169F1
REWIND_OPERAND_RVA = 0x1A08F3B
SPEED_OPERAND_RVA = 0x1A08E29
METADATA = {
    "version": "replay-controls-v3",
    "input_sha256": INPUT_SHA256,
    "output_sha256": OUTPUT_SHA256,
    "rewind_seconds": 10,
    "minimum_speed": 0.0625,
    "maximum_speed": 8,
    "backward_seek_multiplier": 1,
}


def _thunk(rva, speed=False):
    """Emit ia32 thiscall callbacks; no absolute operands or new relocations."""
    code = bytearray()
    labels = {}
    branches = []

    def branch(op, label):
        code.extend((op, 0))
        branches.append((len(code)-1, label))

    def call(target):
        code.append(0xE8)
        code.extend(struct.pack("<i", target-(rva+len(code)+4)))

    if speed:
        # UI callbacks receive const DAVA::Any&, not a raw float. The native
        # Any::Get<float> at VA 0078b6a0 returns argument+4 after checking Type.
        # Check the same Type first so malformed events cannot throw. Resolve
        # its existing global through call/pop; no absolute relocation needed.
        code.extend(bytes.fromhex("8b44240485c0"))
        branch(0x74, "return")
        code.extend(bytes.fromhex("e8000000005a81c2"))
        code.extend(struct.pack("<i", 0x4273C20-(rva+len(code)-3)))
        code.extend(bytes.fromhex("8b123910"))  # cmp [Any], float Type*
        branch(0x75, "return")
        code.extend(bytes.fromhex("5189c1"))  # save screen; ECX=Any
        call(0x38B6A0)
        code.extend(bytes.fromhex("8b0059"))  # float bits; restore screen ECX
        code.extend(bytes.fromhex("3d0000803d"))  # >= 1/16
        branch(0x72, "return")
        code.extend(bytes.fromhex("3d00000041"))  # <= 8; rejects NaN/Inf/negative
        branch(0x77, "return")
    code.extend(bytes.fromhex("85c9"))
    branch(0x74, "return")
    code.extend(bytes.fromhex("8b892001000085c9"))
    branch(0x74, "return")
    code.extend(bytes.fromhex("8b89b001000085c9"))
    branch(0x74, "return")
    if speed:
        code.extend(bytes.fromhex("80792d00"))
        branch(0x75, "seeking")
        code.extend(bytes.fromhex("894114"))
        call(0x100EC00)  # VA 0140ec00 minus image base
        branch(0xEB, "return")
        labels["seeking"] = len(code)
        code.extend(bytes.fromhex("894130"))
    else:
        code.extend(bytes.fromhex("68000020c1"))
        call(0xFE5EA0)  # VA 013e5ea0, callee pops float argument
    labels["return"] = len(code)
    code.extend(b"\xc2\x04\x00" if speed else b"\xc3")
    for at, label in branches:
        distance = labels[label]-at-1
        if not -128 <= distance <= 127:
            raise ValueError("Replay thunk branch exceeds short range")
        code[at] = distance & 255
    return bytes(code)


def _seek_speed_thunk():
    """Preserve flags/stack; keep forward speed, reset backward speed to 1x.

    The common seek method has already computed player+0x2c (target < current).
    Replace its saved-speed assignment, leaving its pause snapshot untouched.
    Native seek completion restores this saved multiplier from player+0x30.
    """
    code = bytearray.fromhex("9c807b2c008b43147405b80000803f8943309de9")
    code.extend(struct.pack("<i", SEEK_SAVE_RVA+6-(SEEK_RESET_RVA+len(code)+4)))
    return bytes(code)


def _layout(data):
    if len(data) != SECTION_RAW+512 or data[:2] != b"MZ":
        raise ValueError("Replay input length or DOS header differs")
    if struct.unpack_from("<I", data, 0x3C)[0] != 0x1E0 \
            or data[0x1E0:0x1E4] != b"PE\0\0" \
            or struct.unpack_from("<HH", data, 0x1E4) != (0x14C, 7) \
            or struct.unpack_from("<H", data, 0x1F4)[0] != 224:
        raise ValueError("Replay patch requires the pinned seven-section PE32")
    checks = ((0x1F8, 0x10B, "H"), (0x1FC, 0x32EDA00, "I"),
              (0x214, 0x400000, "I"), (0x218, 0x1000, "I"),
              (0x21C, 0x200, "I"), (0x230, 0x471D000, "I"),
              (0x234, 0x400, "I"), (0x278, 0, "I"))
    if any(struct.unpack_from("<"+fmt, data, at)[0] != expected
           for at, expected, fmt in checks):
        raise ValueError("Replay optional header layout differs")
    sections = [struct.unpack_from("<8sIIIIIIHHI", data, 0x2D8+i*40)
                for i in range(7)]
    expected_original = (
        (b".text", 0x32ED73A, 0x1000, 0x32ED800, 0x400, 0x60000020),
        (b".rdata", 0xA1A884, 0x32EF000, 0xA1AA00, 0x32EDC00, 0x40000040),
        (b".data", 0x615C74, 0x3D0A000, 0x567A00, 0x3D08600, 0xC0000040),
        (b"_RDATA", 0x2C08, 0x4320000, 0x2E00, 0x4270000, 0x40000040),
        (b".rsrc", 0x45C10, 0x4323000, 0x45E00, 0x4272E00, 0x40000040),
        (b".reloc", 0x3B2F70, 0x4369000, 0x3B3000, 0x42B8C00, 0x42000040),
    )
    for section, expected in zip(sections, expected_original):
        name, virtual, rva, size, raw, flags = expected
        if section != (name.ljust(8, b"\0"), virtual, rva, size, raw, 0, 0, 0, 0, flags):
            raise ValueError("Replay original section layout differs")
    if sections[-1] != (b".mkray\0\0", 468, SECTION_RVA, 512,
                        SECTION_RAW, 0, 0, 0, 0, 0x60000020):
        raise ValueError("Replay extension requires the owned RX section")
    if hashlib.sha256(data[SECTION_RAW:]).hexdigest() != OWNED_CODE_SHA256:
        raise ValueError("Existing marker and hold-fire code differs")

    def offset(rva, length):
        for _, _, start, raw_size, raw, *_ in sections:
            if start <= rva and rva+length <= start+raw_size:
                return raw+rva-start
        raise ValueError("Replay RVA is not backed by a complete section span")

    reloc_rva, reloc_size = struct.unpack_from("<II", data, 0x280)
    if (reloc_rva, reloc_size) != (0x4369000, 0x3B2F70):
        raise ValueError("Replay relocation directory differs")
    cursor = offset(reloc_rva, reloc_size)
    end = cursor+reloc_size
    wanted = {REWIND_OPERAND_RVA, SPEED_OPERAND_RVA}
    while cursor < end:
        if end-cursor < 8:
            raise ValueError("Truncated replay relocation block")
        page, size = struct.unpack_from("<II", data, cursor)
        if size < 8 or size % 2 or cursor+size > end:
            raise ValueError("Invalid replay relocation block")
        for at in range(cursor+8, cursor+size, 2):
            item = struct.unpack_from("<H", data, at)[0]
            if item >> 12 == 3:
                wanted.discard(page+(item & 0xFFF))
        cursor += size
    if wanted:
        raise ValueError("Replay callback operands lack existing HIGHLOW relocations")
    return offset


def patch(data):
    """Return the one deterministic extension of the exact hold-fire image."""
    if hashlib.sha256(data).hexdigest() != INPUT_SHA256:
        raise ValueError("Replay patch requires the pinned hold-fire client")
    offset = _layout(data)
    result = bytearray(data)
    for at, before in ((TIMELINE_OFFSET, bytes.fromhex("0f82fa000000")),
                       (offset(SEEK_SAVE_RVA, 6), bytes.fromhex("8b4314894330")),
                       (offset(REWIND_OPERAND_RVA, 4), struct.pack("<I", 0x6A3400)),
                       (offset(SPEED_OPERAND_RVA, 4), struct.pack("<I", 0x6A93C0))):
        if result[at:at+len(before)] != before:
            raise ValueError("Replay instruction window differs")
    result[TIMELINE_OFFSET:TIMELINE_OFFSET+6] = b"\x90"*6
    at = offset(SEEK_SAVE_RVA, 6)
    result[at:at+6] = b"\xe9"+struct.pack("<i", SEEK_RESET_RVA-SEEK_SAVE_RVA-5)+b"\x90"
    struct.pack_into("<I", result, offset(REWIND_OPERAND_RVA, 4), 0x400000+REWIND_RVA)
    struct.pack_into("<I", result, offset(SPEED_OPERAND_RVA, 4), 0x400000+SPEED_RVA)
    result.extend(bytes(512))
    for rva, code in ((REWIND_RVA, _thunk(REWIND_RVA)),
                      (SPEED_RVA, _thunk(SPEED_RVA, True)),
                      (SEEK_RESET_RVA, _seek_speed_thunk())):
        at = SECTION_RAW+rva-SECTION_RVA
        result[at:at+len(code)] = code
    virtual_size = SEEK_RESET_RVA-SECTION_RVA+len(_seek_speed_thunk())
    if virtual_size > 1024:
        raise ValueError("Replay callbacks exceed the owned section extension")
    struct.pack_into("<I", result, 0x3D0, virtual_size)
    struct.pack_into("<I", result, 0x3D8, 1024)
    struct.pack_into("<I", result, 0x1FC, 0x32EDC00)
    return bytes(result)


def upgrade(lab: Path):
    """Install atomically and recover a binary/manifest interrupted update."""
    # Imported here because client_patch imports our output fingerprint.
    from . import client_patch as cp
    lab = Path(lab)
    manifest_path = lab/"manifest.json"
    manifest = json.loads(manifest_path.read_text())
    source = Path(manifest["game"])/"wotblitz.exe"
    target = lab/"client"/"wotblitz.exe"
    if cp._sha256(source) != cp.BUILD_SHA256 or manifest["exe_sha256"] != cp.BUILD_SHA256:
        raise ValueError("Game executable differs from the pinned PoC build")
    if target.is_symlink() or target.resolve(strict=True) == source.resolve(strict=True) \
            or os.path.samefile(source, target):
        raise ValueError("Lab executable aliases the pinned game executable")
    data = target.read_bytes()
    current = hashlib.sha256(data).hexdigest()
    recorded = manifest.get("client_exe_sha256", manifest["exe_sha256"])
    if cp._is_recon_client(current, recorded):
        return False
    from .stats_patch import OUTPUT_SHA256 as stats_hash
    if current == stats_hash and recorded in (OUTPUT_SHA256, stats_hash):
        return False
    if current not in (INPUT_SHA256, OUTPUT_SHA256) or recorded not in (INPUT_SHA256, OUTPUT_SHA256):
        raise ValueError("Replay patch requires a supported recorded lab client")
    if current == INPUT_SHA256:
        if recorded != current:
            raise ValueError("Lab executable differs from its recorded fingerprint")
        output = patch(data)
        if hashlib.sha256(output).hexdigest() != OUTPUT_SHA256:
            raise ValueError("Replay patch produced an unknown executable")
        cp._write_client(target, output)
    elif recorded == current and manifest.get("replay_controls") == METADATA:
        return False
    manifest["client_exe_sha256"] = OUTPUT_SHA256
    manifest["replay_controls"] = dict(METADATA)
    cp._write_manifest(manifest_path, manifest)
    return True
