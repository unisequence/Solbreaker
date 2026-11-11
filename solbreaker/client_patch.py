"""Patch only the isolated, fingerprinted native client.

The picker patch changes only the single-region fast path in the pre-login
flow. The cosmetic timing patch changes five float immediates in the damage
popup views (entry animation 0.13 s -> 0, hold 1.5 s -> 3.5 s) so that hits
group within four seconds on the private UI path. The marker dispatcher and hold-fire controller extension are installed after
these patches. None changes authentication, server state or the Steam copy.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile


BUILD_SHA256 = "5030ee4b9f6ec7db1c06efa2b92f9dd7243574be5d8d9578c118119318bc9608"
PICKER_OFFSET = 0x10C8818  # VA 0x14c9418, pre-login region determination.
BEFORE = bytes.fromhex("0f847b010000")
AFTER = bytes.fromhex("e97c01000090")
# Picker-only client produced before the damage popup timing patch.
LEGACY_CLIENT_SHA256 = "4a0a1ae8ec5dc89c48cf1f19bdd5b69e8be774d355713cf9c60d062944185603"
# Picker plus damage popup timing, before the marker/controller extensions.
TIMING_CLIENT_SHA256 = "ed96e536fc747236d93535ff73d4402a9aca062b5108428e3fd820ee4a4b5abd"
# VehicleUIObjectDamageView entry 0x1bc55d0 and Squad hide 0x1bc5770 immediates:
# entry animation 0.13 -> 0 (x3), hold 1.5 -> 3.5 (x2).
TIMING_PATCHES = (
    (0x17C4A4B, bytes.fromhex("b81e053e"), bytes.fromhex("00000000")),
    (0x17C4A89, bytes.fromhex("b81e053e"), bytes.fromhex("00000000")),
    (0x17C4AE0, bytes.fromhex("b81e053e"), bytes.fromhex("00000000")),
    (0x17C4C54, bytes.fromhex("0000c03f"), bytes.fromhex("00006040")),
    (0x17C4C66, bytes.fromhex("0000c03f"), bytes.fromhex("00006040")),
)
TIMING_METADATA = {"version": "4seconds", "entry_animation_seconds": 0.0,
                   "hold_seconds": 3.5, "fade_seconds": 0.5}

# ComputeMarker's one RayTestBW call is the only caller that bypasses the
# latter's one-metre subdivision. The original path remains exact for every
# other caller, including the physics queries. The thunk uses only relative
# addresses, so it needs no new PE base relocations and works under ASLR.
MARKER_CLIENT_SHA256 = "44abb4061da57c45e80c4d2470eebf1d0039d79038c7b836f9167b776edf0d18"
from solbreaker.replay_patch import OUTPUT_SHA256 as REPLAY_CLIENT_SHA256
from solbreaker.stats_patch import OUTPUT_SHA256 as STATS_CLIENT_SHA256
HOLD_FIRE_CLIENT_SHA256 = "386f2e0f46cf22d9fc45e8440da7cf9426e2ff015484c948e5e7dc6864e97ca3"
MARKER_BRANCH_OFFSET = 0x36FA52  # VA 0x770652, ImageBase 0x400000.
MARKER_BRANCH_RVA = 0x370652
MARKER_CALLER_RVA = 0x1A11426
MARKER_SECTION_RVA = 0x471C000
MARKER_SECTION_NAME = b".mkray\0\0"
MARKER_BRANCH_BEFORE = bytes.fromhex("7230f30f1055e8")
MARKER_METADATA = {
    "version": "marker-whole-chord-v1",
    "input_sha256": TIMING_CLIENT_SHA256,
    "output_sha256": MARKER_CLIENT_SHA256,
    "scope": "ComputeMarker@0x1e111c0:return@0x1e11426",
    "branch_rva": MARKER_BRANCH_RVA,
    "trampoline_rva": MARKER_SECTION_RVA,
    "trampoline_section": ".mkray",
}


def _is_recon_client(current, recorded):
    """Recognize the exact final image and its one recoverable predecessor."""
    from solbreaker import client_recon
    from solbreaker.client_tls import recognizes as recognizes_tls
    if recognizes_tls(current, recorded):
        return True
    from solbreaker.windows_profile_patch import recognizes
    if recognizes(current, recorded):
        return True
    versions = (client_recon.INPUT_SHA256, client_recon.LEGACY_OUTPUT_SHA256,
                client_recon.NAVIGATION_OUTPUT_SHA256, client_recon.SAVE_OUTPUT_SHA256,
                client_recon.SETTINGS_OUTPUT_SHA256, client_recon.OUTPUT_SHA256)
    return current in versions[1:] and recorded in versions[:versions.index(current) + 1]



def _marker_thunk(section_rva):
    """ia32 PIC dispatcher; preserve EAX, EFLAGS and the original SSE state.

    RayTestBW's pinned prologue saves the entry ESP in EBX after pushing EBX;
    [EBX+4] therefore holds the original caller return address. Do not install
    a runtime RayTestBW onLeave hook alongside this patch: that hook can replace
    the return address with its own leave trampoline and hide the marker scope.
    """
    code = bytearray.fromhex("9c50e8000000005805")  # pushfd/push eax/call $+5/pop eax/add eax,imm32
    code += struct.pack("<i", MARKER_CALLER_RVA - (section_rva + 7))
    code += bytes.fromhex("3943045874119d0f82")  # cmp [ebx+4],eax/pop eax/je marker/popfd/jb original short
    code += struct.pack("<i", 0x370684 - (section_rva + 26))
    code += bytes.fromhex("f30f1055e8e9")  # original movss xmm2,[ebp-18h], then continue
    code += struct.pack("<i", 0x370659 - (section_rva + 36))
    code += bytes.fromhex("9de9")  # marker: popfd/jmp original remaining-length path
    code += struct.pack("<i", 0x370684 - (section_rva + 42))
    if len(code) != 42:
        raise ValueError("Invalid marker trampoline layout")
    return bytes(code)


def _patch_marker_ray(data):
    """Append one RX section to the exact timing client, never reuse a cave."""
    if hashlib.sha256(data).hexdigest() != TIMING_CLIENT_SHA256:
        raise ValueError("Marker ray patch requires the pinned timing client")
    result = bytearray(data)
    if len(result) < 0x400 or result[:2] != b"MZ":
        raise ValueError("Invalid pinned PE image")
    pe = struct.unpack_from("<I", result, 0x3C)[0]
    if pe != 0x1E0 or result[pe:pe + 4] != b"PE\0\0":
        raise ValueError("PE signature differs from the pinned client")
    machine, count = struct.unpack_from("<HH", result, pe + 4)
    optional_size = struct.unpack_from("<H", result, pe + 20)[0]
    optional = pe + 24
    if (machine, count, optional_size) != (0x14C, 6, 0xE0) \
            or struct.unpack_from("<H", result, optional)[0] != 0x10B:
        raise ValueError("Marker ray patch requires the pinned PE32 layout")
    base = struct.unpack_from("<I", result, optional + 28)[0]
    section_align, file_align = struct.unpack_from("<II", result, optional + 32)
    image_size, header_size = struct.unpack_from("<II", result, optional + 56)
    if (base, section_align, file_align, image_size, header_size) != \
            (0x400000, 0x1000, 0x200, MARKER_SECTION_RVA, 0x400):
        raise ValueError("PE alignments or image size differ from the pinned client")
    # Security entries refer to file offsets; refusing certificates/overlays
    # keeps every existing raw section and directory at the original offset.
    if struct.unpack_from("<II", result, optional + 96 + 4 * 8) != (0, 0):
        raise ValueError("Cannot append marker code to a signed PE image")
    table = optional + optional_size
    new_header = table + count * 40
    if new_header + 40 > header_size or any(result[new_header:new_header + 40]):
        raise ValueError("No unused PE section header for marker code")
    raw_end = 0
    virtual_end = 0
    for index in range(count):
        at = table + index * 40
        name = result[at:at + 8]
        virtual_size, rva, raw_size, raw_offset = struct.unpack_from("<IIII", result, at + 8)
        if name.rstrip(b"\0") == b".mkray" or raw_offset % file_align or rva % section_align \
                or raw_size % file_align or raw_offset < header_size \
                or raw_offset + raw_size > len(result):
            raise ValueError("Invalid or already-patched PE section layout")
        raw_end = max(raw_end, raw_offset + raw_size)
        virtual_end = max(virtual_end, rva + max(virtual_size, raw_size))
    if raw_end != len(result) or len(result) % file_align \
            or (virtual_end + section_align - 1) // section_align * section_align != image_size:
        raise ValueError("Pinned PE has an unexpected overlay or section boundary")
    windows = (
        (0x36F860, bytes.fromhex("538bdc83ec0883e4f083c404558b6b04896c24048bec6aff")),
        (MARKER_BRANCH_OFFSET, MARKER_BRANCH_BEFORE),
        (0x1A10821, bytes.fromhex("e83af095fe84c0")),
    )
    for offset, expected in windows:
        if result[offset:offset + len(expected)] != expected:
            raise ValueError(f"Marker instruction window differs at {offset:#x}")
    code = _marker_thunk(image_size)
    raw_size = (len(code) + file_align - 1) // file_align * file_align
    result[new_header:new_header + 40] = struct.pack(
        "<8sIIIIIIHHI", MARKER_SECTION_NAME, len(code), image_size,
        raw_size, len(result), 0, 0, 0, 0, 0x60000020)  # code, executable, readable; not writable
    struct.pack_into("<H", result, pe + 6, count + 1)
    size_of_code = struct.unpack_from("<I", result, optional + 4)[0]
    struct.pack_into("<I", result, optional + 4, size_of_code + raw_size)
    struct.pack_into("<I", result, optional + 56,
                     image_size + (len(code) + section_align - 1) // section_align * section_align)
    struct.pack_into("<I", result, optional + 64, 0)  # ordinary user-mode PE checksum is optional
    result.extend(code + bytes(raw_size - len(code)))
    branch = b"\xe9" + struct.pack("<i", image_size - (MARKER_BRANCH_RVA + 5)) + b"\x90\x90"
    result[MARKER_BRANCH_OFFSET:MARKER_BRANCH_OFFSET + len(branch)] = branch
    return bytes(result)


def _write_client(target, data):
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".marker-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()  # Windows cannot replace a file while this handle is open.
            os.chmod(temporary, 0o755)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def upgrade_marker_ray(lab: Path):
    """Install the marker-only dispatcher after upgrade_damage_popup_timing.

    Source executable, lab file, and manifest hashes are independently pinned.
    An interrupted executable/manifest update is recoverable only when the file
    is already the one exact deterministic output. Returns manifest-change bool.
    """
    manifest_path = lab / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    source = Path(manifest["game"]) / "wotblitz.exe"
    target = lab / "client" / "wotblitz.exe"
    if _sha256(source) != BUILD_SHA256 or manifest["exe_sha256"] != BUILD_SHA256:
        raise ValueError("Game executable differs from the pinned PoC build")
    if target.is_symlink() or target.resolve(strict=True) == source.resolve(strict=True) \
            or os.path.samefile(source, target):
        raise ValueError("Lab executable aliases the pinned game executable")
    data = target.read_bytes()
    current = hashlib.sha256(data).hexdigest()
    recorded = manifest.get("client_exe_sha256", manifest["exe_sha256"])
    if _is_recon_client(current, recorded):
        return False
    if current == STATS_CLIENT_SHA256 and recorded in (REPLAY_CLIENT_SHA256, STATS_CLIENT_SHA256):
        return False
    if current == REPLAY_CLIENT_SHA256 and recorded in (HOLD_FIRE_CLIENT_SHA256, REPLAY_CLIENT_SHA256):
        return False  # Replay image retains all earlier patches.
    if current == HOLD_FIRE_CLIENT_SHA256 and recorded in (MARKER_CLIENT_SHA256, HOLD_FIRE_CLIENT_SHA256):
        return False  # The hold-fire image retains the complete marker patch.
    if current not in (TIMING_CLIENT_SHA256, MARKER_CLIENT_SHA256) \
            or recorded not in (TIMING_CLIENT_SHA256, MARKER_CLIENT_SHA256):
        raise ValueError("Marker patch requires a supported recorded lab client")
    if current == TIMING_CLIENT_SHA256:
        if recorded != current:
            raise ValueError("Lab executable differs from its recorded fingerprint")
        patched = _patch_marker_ray(data)
        if hashlib.sha256(patched).hexdigest() != MARKER_CLIENT_SHA256:
            raise ValueError("Marker ray patch produced an unknown executable")
        _write_client(target, patched)
    elif recorded == MARKER_CLIENT_SHA256 and manifest.get("marker_ray") == MARKER_METADATA:
        return False
    manifest["client_exe_sha256"] = MARKER_CLIENT_SHA256
    manifest["marker_ray"] = dict(MARKER_METADATA)
    _write_manifest(manifest_path, manifest)
    return True


def install_picker(source: Path, target: Path):
    if source.resolve() == target.absolute():
        raise ValueError("Refusing to patch the installed executable")
    data = bytearray(source.read_bytes())
    if hashlib.sha256(data).hexdigest() != BUILD_SHA256:
        raise ValueError("Compatible with build 11.21.0.1235_7376187")
    if data[PICKER_OFFSET:PICKER_OFFSET + len(BEFORE)] != BEFORE:
        raise ValueError("Region picker instruction does not match the pinned build")
    for offset, before, _ in TIMING_PATCHES:
        if data[offset:offset + len(before)] != before:
            raise ValueError(f"Damage popup timing bytes at {offset:#x} do not match the pinned build")
    data[PICKER_OFFSET:PICKER_OFFSET + len(BEFORE)] = AFTER
    if hashlib.sha256(data).hexdigest() != LEGACY_CLIENT_SHA256:
        raise ValueError("Picker-only executable differs from the known client")
    for offset, before, after in TIMING_PATCHES:
        data[offset:offset + len(before)] = after
    if hashlib.sha256(data).hexdigest() != TIMING_CLIENT_SHA256:
        raise ValueError("Patched executable differs from the known timing client")
    # Atomic replacement removes a lab symlink without following it.
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".picker-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()  # Windows cannot replace a file while this handle is open.
            os.chmod(temporary, 0o755)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(path: Path, manifest):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".manifest-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()  # Windows cannot replace a file while this handle is open.
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def upgrade_damage_popup_timing(lab: Path):
    """Upgrade a picker-only lab client to the damage popup timing client.

    Only the two known executables are accepted, and the executable must match
    the manifest fingerprint (or the new digest after an interrupted manifest
    update). Unknown executables are rejected even if the manifest records their hash. Returns True when the manifest was updated.
    """
    manifest_path = lab / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    source = Path(manifest["game"]) / "wotblitz.exe"
    target = lab / "client" / "wotblitz.exe"
    if _sha256(source) != BUILD_SHA256 or manifest["exe_sha256"] != BUILD_SHA256:
        raise ValueError("Game executable differs from the pinned PoC build")
    if target.is_symlink() or target.resolve(strict=True) == source.resolve(strict=True) \
            or os.path.samefile(source, target):
        raise ValueError("Lab executable aliases the pinned game executable")
    current = _sha256(target)
    recorded = manifest.get("client_exe_sha256", manifest["exe_sha256"])
    if _is_recon_client(current, recorded):
        return False
    if current == STATS_CLIENT_SHA256 and recorded in (REPLAY_CLIENT_SHA256, STATS_CLIENT_SHA256):
        return False
    if current == REPLAY_CLIENT_SHA256 and recorded in (HOLD_FIRE_CLIENT_SHA256, REPLAY_CLIENT_SHA256):
        return False  # Replay image retains all earlier patches.
    if current == HOLD_FIRE_CLIENT_SHA256 and recorded in (MARKER_CLIENT_SHA256, HOLD_FIRE_CLIENT_SHA256):
        return False  # Timing bytes are unchanged in this exact derivative.
    if current not in (LEGACY_CLIENT_SHA256, TIMING_CLIENT_SHA256, MARKER_CLIENT_SHA256):
        raise ValueError("Lab executable is not a supported pinned client")
    if recorded not in (LEGACY_CLIENT_SHA256, TIMING_CLIENT_SHA256, MARKER_CLIENT_SHA256):
        raise ValueError("Lab executable differs from its recorded fingerprint")
    if current == MARKER_CLIENT_SHA256:
        if recorded not in (TIMING_CLIENT_SHA256, MARKER_CLIENT_SHA256):
            raise ValueError("Lab executable differs from its recorded fingerprint")
        if recorded == current and manifest.get("damage_popup_timing") == TIMING_METADATA:
            return False
        manifest["client_exe_sha256"] = current
        manifest["damage_popup_timing"] = dict(TIMING_METADATA)
        _write_manifest(manifest_path, manifest)
        return True
    if recorded == MARKER_CLIENT_SHA256:
        raise ValueError("Lab executable differs from its recorded fingerprint")
    if current == LEGACY_CLIENT_SHA256:
        if recorded != LEGACY_CLIENT_SHA256:
            raise ValueError("Lab executable differs from its recorded fingerprint")
        if install_picker(source, target) != TIMING_CLIENT_SHA256:
            raise ValueError("Damage popup timing upgrade produced an unknown executable")
    elif recorded == TIMING_CLIENT_SHA256 and manifest.get("damage_popup_timing") == TIMING_METADATA:
        return False
    manifest["client_exe_sha256"] = TIMING_CLIENT_SHA256
    manifest["damage_popup_timing"] = dict(TIMING_METADATA)
    _write_manifest(manifest_path, manifest)
    return True


# Existing RX .mkray section has 470 unused raw bytes. Keep its 42-byte
# marker dispatcher unchanged, align the controller thunk to the next 64 bytes.
HOLD_FIRE_RVA = MARKER_SECTION_RVA + 64
HOLD_FIRE_HOOK_RVA = 0x19F9E70
HOLD_FIRE_HOOK_OFFSET = 0x19F9270
HOLD_FIRE_BEFORE = bytes.fromhex("558bec83ec18")
HOLD_FIRE_METADATA = {
    "version": "hold-fire-v1",
    "input_sha256": MARKER_CLIENT_SHA256,
    "output_sha256": HOLD_FIRE_CLIENT_SHA256,
    "scope": "KeyboardMouseInputBattleController::Update",
}


def _hold_fire_thunk():
    """Repeat the bound fire action only while held, focused and gun-ready.

    Relative calls only: no relocations, writable globals, threads or timers.
    Native input binding/keyboard state support remapped controls. Window focus
    and the UI keyboard handler exclude menus and text editing. Clearing cached
    mouse buttons while inactive prevents a release consumed by UI from latching.
    The Avatar gun-state checks mirror 0x182c1b9..0x182c23c, including a pump
    magazine that can fire before its full refill ends. The normal fire publisher
    retains all existing shot validation, cooldown, ammunition and shot effects.
    GPRs/EFLAGS are saved and the original prologue is replayed exactly.
    """
    start = 0x400000 + HOLD_FIRE_RVA
    code = bytearray()
    labels = {}
    fix = []
    def emit(h):
        code.extend(bytes.fromhex(h))
    def call(va):
        emit('e8')
        code.extend(struct.pack('<i',va-(start+len(code)+4)))
    def jump(label,op='e9'):
        emit(op)
        fix.append((len(code),label))
        code.extend(b'\0'*4)
    def label(s):
        labels[s] = len(code)
    # Preserve caller state, then use the same action binding and physical-state
    # query as KeyboardMouseInputBattleController::Update for its camera keys.
    emit('9c60 89ce 83ec08')
    # Primary window: relative cursor capture and keyboard focus.
    call(0xab0560)
    emit('85c0')
    jump('inactive','0f84')
    emit('83b8b000000002')
    jump('inactive','0f85')
    emit('80b8a700000000')
    jump('inactive','0f84')
    call(0xab01f0)
    emit('8b483c')
    call(0xb5a6d0)  # UIInputSystem, including the active text keyboard handler.
    emit('85c0')
    jump('inactive','0f84')
    emit('8b8090000000 85c0')
    jump('inactive','0f84')
    emit('83780800')
    jump('inactive','0f85')
    emit('c7042400000000 89e0 50 8d4e30')
    call(0x1dddd50)
    emit('8b5804 8b00 3d00010000')
    jump('mouse','0f84')
    emit('3d00020000')
    jump('mouse','0f84')
    emit('3d00040000')
    jump('done','0f85')
    call(0xab01f0)
    emit('8b4870 8b01 ff5020 85c0')
    jump('done','0f84')
    emit('53 8d4c2408 51 89c1')
    call(0x921d50)
    emit('803800')
    jump('done','0f84')
    jump('fire')
    label('mouse')
    emit('83fb10')
    jump('done','0f83')
    emit('807c333800')
    jump('done','0f84')
    # Context -> Avatar; mirror native ready/pump checks before publishing.
    label('fire')
    emit('8b4e08 85c9')
    jump('done','0f84')
    call(0x7334d0)
    emit('85c0')
    jump('done','0f84')
    emit('89c1 8b00 ff90a0000000 85c0')
    jump('done','0f84')
    emit('89c7 80bfc205000000')
    jump('done','0f85')
    emit('8b87c4050000 85c0')
    jump('publish','0f84')
    for state in (2,5,8):
        emit('83f8'+bytes([state]).hex())
        jump('done','0f84')
    emit('8b8f04020000')
    call(0x1bacf50)
    emit('84c0')
    jump('done','0f84')
    emit('8d8fb4040000')
    call(0x1d52de0)
    emit('83780c00')
    jump('done','0f8e')
    label('publish')
    emit('8b4e6c 85c9')
    jump('done','0f84')
    call(0x1dee700)
    jump('done')
    label('inactive')
    emit('31c0 894638 89463c 894640 894644')
    label('done')
    emit('83c408 619d 558bec83ec18')
    jump('original')
    for at,s in fix:
        dest=0x1df9e76 if s=='original' else start+labels[s]
        struct.pack_into('<i',code,at,dest-(start+at+4))
    if len(code) > 448:
        raise ValueError("Hold-fire code exceeds the marker section padding")
    return bytes(code)


def _patch_hold_fire(data):
    if hashlib.sha256(data).hexdigest() != MARKER_CLIENT_SHA256:
        raise ValueError("Hold-fire patch requires the pinned marker client")
    result = bytearray(data)
    if result[HOLD_FIRE_HOOK_OFFSET:HOLD_FIRE_HOOK_OFFSET+6] != HOLD_FIRE_BEFORE:
        raise ValueError("Hold-fire input update prologue differs")
    pe = struct.unpack_from("<I", result, 0x3c)[0]
    if pe != 0x1e0 or struct.unpack_from("<H", result, pe+6)[0] != 7:
        raise ValueError("Hold-fire patch requires the pinned seven-section PE")
    header = pe + 24 + 224 + 6*40
    name, virtual, rva, size, raw = struct.unpack_from("<8sIIII", result, header)
    if (name, virtual, rva, size) != (MARKER_SECTION_NAME, 42, MARKER_SECTION_RVA, 512) \
            or raw+size != len(result) \
            or struct.unpack_from("<I", result, header+36)[0] != 0x60000020:
        raise ValueError("Hold-fire section layout differs")
    if result[raw:raw+42] != _marker_thunk(MARKER_SECTION_RVA) or any(result[raw+42:]):
        raise ValueError("Hold-fire requires unchanged marker code and empty padding")
    code = _hold_fire_thunk()
    result[raw+64:raw+64+len(code)] = code
    struct.pack_into("<I", result, header+8, 64+len(code))
    result[HOLD_FIRE_HOOK_OFFSET:HOLD_FIRE_HOOK_OFFSET+6] = (
        b"\xe9" + struct.pack("<i", HOLD_FIRE_RVA-HOLD_FIRE_HOOK_RVA-5) + b"\x90")
    return bytes(result)


def upgrade_hold_fire(lab: Path):
    """Upgrade a private copy; recover a completed binary/unfinished manifest."""
    manifest_path = lab / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    source = Path(manifest["game"]) / "wotblitz.exe"
    target = lab / "client" / "wotblitz.exe"
    if _sha256(source) != BUILD_SHA256 or manifest["exe_sha256"] != BUILD_SHA256:
        raise ValueError("Game executable differs from the pinned PoC build")
    if target.is_symlink() or target.resolve(strict=True) == source.resolve(strict=True) \
            or os.path.samefile(source, target):
        raise ValueError("Lab executable aliases the pinned game executable")
    data = target.read_bytes()
    current = hashlib.sha256(data).hexdigest()
    recorded = manifest.get("client_exe_sha256", manifest["exe_sha256"])
    if _is_recon_client(current, recorded):
        return False
    if current == STATS_CLIENT_SHA256 and recorded in (REPLAY_CLIENT_SHA256, STATS_CLIENT_SHA256):
        return False
    if current == REPLAY_CLIENT_SHA256 and recorded in (HOLD_FIRE_CLIENT_SHA256, REPLAY_CLIENT_SHA256):
        return False  # Replay image retains all earlier patches.
    if current not in (MARKER_CLIENT_SHA256, HOLD_FIRE_CLIENT_SHA256) \
            or recorded not in (MARKER_CLIENT_SHA256, HOLD_FIRE_CLIENT_SHA256):
        raise ValueError("Hold-fire requires a supported recorded lab client")
    if current == MARKER_CLIENT_SHA256:
        if recorded != current:
            raise ValueError("Lab executable differs from its recorded fingerprint")
        data = _patch_hold_fire(data)
        if hashlib.sha256(data).hexdigest() != HOLD_FIRE_CLIENT_SHA256:
            raise ValueError("Hold-fire patch produced an unknown executable")
        _write_client(target, data)
    elif recorded == current and manifest.get("hold_fire") == HOLD_FIRE_METADATA:
        return False
    manifest["client_exe_sha256"] = HOLD_FIRE_CLIENT_SHA256
    manifest["hold_fire"] = dict(HOLD_FIRE_METADATA)
    _write_manifest(manifest_path, manifest)
    return True
