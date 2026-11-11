"""Version-locked recon bridge for the private 11.21.0.1235_7376187 client.

Applies only to the exact battle-statistics client (the end of the existing
private patch chain). The bridge sends the versioned hello from the reticle
or an ordinary Avatar control message, keeps every TFS method-37 envelope away from the
native listeners, accepts the ack and wire-v1 snapshots for its own battle,
reports the vehicle hit by ComputeMarker's reticle ray (TFS-LAB-A, >= 10 Hz)
and gates enemy 3D/minimap opacity, silhouettes and penetration overlays.
The stock lamp follows the recipient alert. Mesh visibility, aiming, weapon
parameters and allied markers are not written.

Code: native/recon_client.c and native/recon_wire.c, linked at VA 0x4b1c400 and
appended to the owned RX .mkray section; BLOB below is that build, pinned by
hash. State: STATE_SIZE bytes in the zero-initialised tail of .data (RW, NX), owned
by raising its VirtualSize up to the next section. No new imports, base
relocations, writable code or runtime VirtualProtect.

The two marker prototypes must additionally carry UIOpacityComponent (see
MARKER_OPACITY); without it the bridge finds no component and changes nothing.

The same fingerprinted installation also hosts the native battle-settings
navigation callback from client_battle_settings and its separate assembly
source. Legacy bridge images can be upgraded without rebuilding the bridge.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile


INPUT_SHA256 = "24d6b5a95649003c2353f7e341e880641e7f6e9eb1a522f1f18e862f7ff7fa8e"
BASE = 0x400000
TEXT_FILE_DELTA = 0xC00
SECTION_TABLE = 0x2D8
DATA_HEADER = SECTION_TABLE + 2 * 40
MKRAY_HEADER = SECTION_TABLE + 6 * 40
MKRAY_RVA = 0x471C000
MKRAY_RAW = 0x466BC00
MKRAY_USED = 0x2B8
CODE_VA = 0x4B1C400
CODE_OFFSET = CODE_VA - BASE - MKRAY_RVA
DATA_RVA = 0x3D0A000
DATA_VIRTUAL = 0x615C74
DATA_OWNED = 0x616000  # up to _RDATA at RVA 0x4320000
STATE_VA = 0x471FC80
STATE_SIZE = 0x370

# Hook site, original bytes replaced by jmp+nop, stub symbol.
HOOKS = (
    ("message", 0x177E3D5, "3bf0741d8b0e", "tfs_hook_message"),
    ("control", 0x1786630, "558bec518d45ff", "tfs_hook_control"),
    ("markers", 0x1BC9370, "558bec6aff", "tfs_hook_markers"),
    ("minimap", 0x1D9D410, "558bec6aff", "tfs_hook_minimap"),
    ("reset", 0x1B9AC00, "558bec6aff", "tfs_hook_reset"),
    ("ray", 0x1E11426, "84c00f85c2010000", "tfs_hook_ray"),
    ("aim_begin", 0x1E22F10, "558bec81ec18010000", "tfs_hook_aim_begin"),
    ("aim_end", 0x1E23043, "807b15008d5350", "tfs_hook_aim_end"),
    ("lamp_bind", 0x1D867E9, "898650010000", "tfs_hook_lamp_bind"),
    ("lamp_unbind", 0x1D7A380, "558bec6aff", "tfs_hook_lamp_unbind"),
    ("silhouette", 0x786F90, "558bec8a4508", "tfs_hook_silhouette"),
    ("armor", 0x1E21A30, "558bec6aff", "tfs_hook_armor"),
    ("armor_unbind", 0x1E16180, "568bf18b4e04", "tfs_hook_armor_unbind"),
    ("armor_destroy", 0x1B9E750, "558bec6aff", "tfs_hook_armor_destroy"),
)
# Unchanged code the stubs and the C bridge rely on (frame slots, return
# targets, callee prologues and returns).
from .client_battle_settings import NAVIGATION_VA, NAVIGATION_BEFORE, SAVE_REPLY_WINDOWS

WINDOWS = (
    (NAVIGATION_VA, NAVIGATION_BEFORE.hex()),
    *((va, before.hex()) for va, before in SAVE_REPLY_WINDOWS),
    (0x177E35E, "c78528ffffff00000000c7852cffffff00000000c78530ffffff00000000"),
    (0x177E38F, "c78538ffffff00000000c7853cffffff00000000c78540ffffff00000000"),
    (0x177E3BE, "8b770c83c4088b4f0c85c974088b47108d0481eb0233c0"),
    (0x177E3DB, "8d9538ffffff528d9528ffffff528b01ff9080000000"),
    (0x177E3F6, "c645fc098b8d38ffffff"),
    (0x1786637, "506a016a0ae8bf750801807dff00"),
    (0x1BC9375, "683db74203"),
    (0x1D9D415, "68cd6e0b03"),
    (0x1B9AC05, "68925f420364a100000000"),
    (0x1B9AC2D, "c70760ee8203"),          # BattleUILayer destructor
    (0x1D7AAD8, "c745f810d4d901"),        # minimap update callback
    (0x280DC00, "558bec6aff682d271003"),  # bundle begin, ret 0Ch
    (0x280DCE9, "c20c00"),
    (0x282E650, "558beca1487d9e0356578bf9"),
    (0x282E682, "c20400"),
    (0xCE0530, "558bec56ff75088bf1e8c22e1900"),
    (0xCE0558, "c20800"),
    (0x102AB30, "558bec6aff6828271503"),
    (0x102ACE5, "c20400"),
    (0x92CAC0, "558bec83ec08568b750c578bf9"),
    (0x92CB20, "c20800"),
    (0x770763, "c645fc018bcee8b2b21900"),  # ray result is stored before the hook
    (0x770774, "85c0754a0f2845d00f2945b0c645fc068d4d"),
    (0x1E11421, "e83af095fe84c0"),         # ComputeMarker reticle ray, returns 1e11426
    (0x1E2303E, "e87de1feff807b1500"),     # GetGunMarkerPosition called by ComputeMarker
    (0x7334D0, "8b4104c3"),                # GameLogic slot 1: entity = [logic+4]
    (0x1E1D6BB, "8b4f108b01ff5004568d487ce8249096ff"),  # base mailbox = entity+7Ch
    (0x1D867E0, "8d45d850e8977cf5fe"),  # UILamp resolves its Icon before binding
    (0xD05020, "558bec8a5508568bf18a4e4c8ac1d0e824013ac2"),
    (0xD050BA, "5e5dc20400"),            # UIControl::SetVisibilityFlag
    (0xD55CB0, "558bec56578bf98b7738"),  # DAVA::Entity::GetComponent
    (0xD55CF1, "c20800"),
    (0x786F96, "8841105dc20400"),        # SilhouetteComponent::SetEnabled tail
    (0xABA1C0, "558bec8b45088941085dc20400"), # DAVA::Component::SetEntity
    (0x1E21A35, "6895de480364a100000000"), # ArmorHighlightController update entry
    (0x1E21F86, "8be55dc3"),
    (0x7BE4D0, "558bec6aff68dda90e03"),  # TankVisual clears separate armor overlay
    (0x1B9E755, "68cdc2300364a100000000"), # GameLogicAware 18h deleting destructor
)
# Vtables referenced by the bridge, identified through MSVC RTTI.
VTABLES = (
    (0x382EE60, ".?AVBattleUILayer@@"),
    (0x382F348, ".?AVVehicleUIObject@@"),
    (0x383066C, ".?AVVehicleUIObjectMarkerImpl@@"),
    (0x3781EE4, ".?AVUIOpacityComponent@DAVA@@"),
    (0x384AA58, ".?AVMinimapObjectsController@@"),
    (0x384ACAC, ".?AVMinimapObjectsViewImpl@@"),
    (0x39DE150, ".?AVBWServerConnection@BW@@"),
    (0x39E2FB8, ".?AVUDPBundle@Mercury@BW@@"),
    (0x3801FC4, ".?AVAvatarGameLogic@@"),
    (0x37F86D0, ".?AVAvatarContextBattle@@"),
    (0x38023D4, ".?AVVehicleGameLogic@@"),
    (0x37F7D8C, ".?AVVehicle@@"),
    (0x3713AFC, ".?AVTankVisual@@"),
    (0x384876C, ".?AVUILamp@@"),
    (0x37115F4, ".?AVSilhouetteComponent@@"),
    (0x385547C, ".?AVArmorHighlightController@@"),
    (0x37F7058, ".?AVAvatar@@"),
)
WORDS = (
    (0x382EE60 + 39 * 4, 0x1BC9370),  # BattleUILayer slot 39 is the 3D hook
    (0x39DE150 + 0x40, 0x28097B0),
    (0x39E2FB8 + 4, 0x282E650),       # UDPBundle reserve
    (0x3801FC4 + 4, 0x7334D0),
    (0x38023D4 + 4, 0x7334D0),
)
IAT_GET_TICK_COUNT = 0x36EF0FC

METADATA = {
    "version": "recon-bridge-v1",
    "input_sha256": INPUT_SHA256,
    "output_sha256": None,
    "code_va": CODE_VA,
    "state_va": STATE_VA,
    "hooks": {name: va for name, va, _, _ in HOOKS},
    "wire": "recon_wire v1, hello TFS-LAB-H, reticle TFS-LAB-A",
    "settings_navigation": "GameTypeScreen.InfoPressed -> typed Training.showCreateRoom",
    "settings_save_result": 127,
    "settings_load_result": 128,
}

MARKER_OPACITY = {
    "UI/Screens/Battle/Minimap.yaml.dvpl": "VehicleMarker",
    "UI/Screens/Battle/VehicleUIObjectMarker/VehicleUIObjectMarker.yaml.dvpl":
        "VehicleUIObjectMarker",
}

CFLAGS = (
    "-m32", "-Os", "-std=c11", "-ffreestanding", "-fno-builtin", "-fno-pic", "-fno-pie",
    "-fno-stack-protector", "-fno-stack-clash-protection", "-fcf-protection=none",
    "-fno-asynchronous-unwind-tables", "-fno-unwind-tables", "-fno-jump-tables",
    "-fno-tree-loop-distribute-patterns", "-fno-ident", "-mno-sse", "-mno-mmx",
    "-mno-80387", "-mpreferred-stack-boundary=2", "-Wall", "-Wextra", "-Werror",
)
TOOLCHAIN = "gcc (Debian 14.2.0-19) 14.2.0"


def build_blob(native=None):
    """Compile and link the bridge; returns (code, {symbol: VA}).

    Refuses any output section except .text and any relocation that is not
    resolved by the final link, so the bytes are position independent.
    """
    native = Path(native or Path(__file__).resolve().parent / "native")
    with tempfile.TemporaryDirectory(prefix="tfs-recon-build-") as directory:
        work = Path(directory)
        objects = []
        for name in ("recon_client.c", "recon_wire.c"):
            target = work / (name[:-2] + ".o")
            subprocess.run(["gcc", *CFLAGS, "-I", str(native), "-c", str(native / name),
                            "-o", str(target)], check=True, capture_output=True)
            objects.append(str(target))
        elf = work / "recon.elf"
        subprocess.run(["ld", "-m", "elf_i386", "-nostdlib", "-static", "--build-id=none",
                        "-e", "tfs_hook_message", f"-Ttext={CODE_VA:#x}", "-o", str(elf),
                        *objects], check=True, capture_output=True)
        headers = subprocess.run(["objdump", "-h", str(elf)], check=True,
                                 capture_output=True, text=True).stdout
        sections = [line.split()[1] for line in headers.splitlines()
                    if line.strip()[:1].isdigit()]
        if sections != [".text"]:
            raise ValueError(f"Recon bridge has unexpected sections: {sections}")
        # Only tfs_delta's own link address may be absolute (client offset 7).
        for path, allowed in zip(objects, ([["00000007", "R_386_32", ".text"]], [])):
            listing = subprocess.run(["objdump", "-r", path], check=True,
                                     capture_output=True, text=True).stdout
            absolute = [line.split() for line in listing.splitlines()
                        if line.split()[1:2] and line.split()[1].startswith("R_386_")
                        and line.split()[1] != "R_386_PC32"]
            if absolute != allowed:
                raise ValueError("Recon bridge contains absolute relocations")
        symbols = {}
        table = subprocess.run(["nm", str(elf)], check=True, capture_output=True,
                               text=True).stdout
        for line in table.splitlines():
            value, _, name = line.split()
            symbols[name] = int(value, 16)
        code = work / "recon.bin"
        subprocess.run(["objcopy", "-O", "binary", "-j", ".text", str(elf), str(code)],
                       check=True, capture_output=True)
        return code.read_bytes(), symbols


def _offset(sections, rva, length=1):
    for _, _, start, raw_size, raw, *_ in sections:
        if start <= rva and rva + length <= start + raw_size:
            return raw + rva - start
    raise ValueError(f"RVA {rva:#x} is not backed by file bytes")


def _sections(data):
    if data[:2] != b"MZ" or struct.unpack_from("<I", data, 0x3C)[0] != 0x1E0 \
            or data[0x1E0:0x1E4] != b"PE\0\0" \
            or struct.unpack_from("<HH", data, 0x1E4) != (0x14C, 7) \
            or struct.unpack_from("<H", data, 0x1F4)[0] != 224:
        raise ValueError("Recon bridge requires the pinned seven-section PE32")
    checks = ((0x1F8, 0x10B, "H"), (0x1FC, 0x32EDC00, "I"), (0x214, BASE, "I"),
              (0x218, 0x1000, "I"), (0x21C, 0x200, "I"), (0x230, 0x471D000, "I"),
              (0x234, 0x400, "I"), (0x238, 0, "I"), (0x278, 0, "I"), (0x27C, 0, "I"))
    if any(struct.unpack_from("<" + fmt, data, at)[0] != expected
           for at, expected, fmt in checks):
        raise ValueError("Recon bridge optional header differs")
    sections = [struct.unpack_from("<8sIIIIIIHHI", data, SECTION_TABLE + i * 40)
                for i in range(7)]
    expected = (
        (b".text", 0x32ED73A, 0x1000, 0x32ED800, 0x400, 0x60000020),
        (b".rdata", 0xA1A884, 0x32EF000, 0xA1AA00, 0x32EDC00, 0x40000040),
        (b".data", DATA_VIRTUAL, DATA_RVA, 0x567A00, 0x3D08600, 0xC0000040),
        (b"_RDATA", 0x2C08, 0x4320000, 0x2E00, 0x4270000, 0x40000040),
        (b".rsrc", 0x45C10, 0x4323000, 0x45E00, 0x4272E00, 0x40000040),
        (b".reloc", 0x3B2F70, 0x4369000, 0x3B3000, 0x42B8C00, 0x42000040),
        (b".mkray", MKRAY_USED, MKRAY_RVA, 0x400, MKRAY_RAW, 0x60000020),
    )
    for section, (name, virtual, rva, size, raw, flags) in zip(sections, expected):
        if section != (name.ljust(8, b"\0"), virtual, rva, size, raw, 0, 0, 0, 0, flags):
            raise ValueError("Recon bridge section layout differs")
    if len(data) != MKRAY_RAW + 0x400 or any(data[MKRAY_RAW + MKRAY_USED:]):
        raise ValueError("Owned .mkray section is not the expected size or padding")
    if STATE_VA - BASE < DATA_RVA + DATA_VIRTUAL or STATE_VA + STATE_SIZE - BASE > DATA_RVA + DATA_OWNED:
        raise ValueError("Recon state does not fit the unused .data tail")
    return sections


def _import(data, sections, slot_va):
    """Return (dll, function) imported through an IAT slot, by name only."""
    def string(rva):
        at = _offset(sections, rva)
        return data[at:data.index(b"\0", at)].decode("ascii")
    directory, size = struct.unpack_from("<II", data, 0x1F8 + 96 + 8)
    for index in range(size // 20):
        lookup, _, _, name, first = struct.unpack_from(
            "<5I", data, _offset(sections, directory + index * 20, 20))
        if not name:
            break
        slot = slot_va - BASE - first
        if slot < 0 or slot % 4:
            continue
        thunks = lookup or first
        for position in range(0, slot + 4, 4):
            entry = struct.unpack_from("<I", data, _offset(sections, thunks + position, 4))[0]
            if not entry:
                break
            if position == slot:
                if entry & 0x80000000:
                    raise ValueError("Recon clock is imported by ordinal")
                return string(name), string(entry + 2)
    raise ValueError("Recon clock import slot not found")


def _relocated(data, sections, ranges):
    """True if a base relocation patches any byte of the RVA ranges."""
    directory, size = struct.unpack_from("<II", data, 0x1F8 + 96 + 5 * 8)
    cursor = _offset(sections, directory, size)
    end = cursor + size
    while cursor < end:
        page, block = struct.unpack_from("<II", data, cursor)
        if block < 8 or block % 2 or cursor + block > end:
            raise ValueError("Invalid base relocation block")
        if any(start < page + 0x1003 and page < stop for start, stop in ranges):
            for at in range(cursor + 8, cursor + block, 2):
                item = struct.unpack_from("<H", data, at)[0]
                kind, address = item >> 12, page + (item & 0xFFF)
                if kind and any(start - 3 <= address < stop for start, stop in ranges):
                    return True
        cursor += block
    return False


def _verify(data, sections):
    def read(va, length):
        at = _offset(sections, va - BASE, length)
        return data[at:at + length]
    for _, va, before, _ in HOOKS:
        if read(va, len(before) // 2).hex() != before:
            raise ValueError(f"Recon hook window differs at {va:#x}")
    for va, expected in WINDOWS:
        if read(va, len(expected) // 2).hex() != expected:
            raise ValueError(f"Recon instruction window differs at {va:#x}")
    for va, value in WORDS:
        if struct.unpack("<I", read(va, 4))[0] != value:
            raise ValueError(f"Recon vtable slot differs at {va:#x}")
    for va, name in VTABLES:
        locator = struct.unpack("<I", read(va - 4, 4))[0]
        descriptor = struct.unpack("<I", read(locator + 12, 4))[0]
        if read(descriptor + 8, len(name) + 1) != name.encode() + b"\0":
            raise ValueError(f"Recon vtable class differs at {va:#x}")
    if _import(data, sections, IAT_GET_TICK_COUNT) != ("KERNEL32.dll", "GetTickCount"):
        raise ValueError("Recon clock import differs")
    ranges = [(va - BASE, va - BASE + len(before) // 2) for _, va, before, _ in HOOKS]
    ranges.append((DATA_RVA + DATA_VIRTUAL, DATA_RVA + DATA_OWNED))
    ranges.append((MKRAY_RVA, MKRAY_RVA + 0x2000))
    if _relocated(data, sections, ranges):
        raise ValueError("A base relocation overlaps recon-owned bytes")


def patch(data, blob=None, entries=None):
    """Return the one deterministic bridge extension of the exact stats client."""
    blob = BLOB if blob is None else blob
    entries = ENTRIES if entries is None else entries
    if hashlib.sha256(data).hexdigest() != INPUT_SHA256:
        raise ValueError("Recon bridge requires the pinned battle-statistics client")
    sections = _sections(data)
    _verify(data, sections)
    if not blob or len(blob) > 0x1C00 or set(entries) != {name for name, *_ in HOOKS}:
        raise ValueError("Invalid recon bridge build")
    virtual = CODE_OFFSET + len(blob)
    raw = (virtual + 0x1FF) // 0x200 * 0x200
    result = bytearray(data)
    result.extend(bytes(raw - 0x400))
    result[MKRAY_RAW + CODE_OFFSET:MKRAY_RAW + virtual] = blob
    for name, va, before, _ in HOOKS:
        at = va - BASE - TEXT_FILE_DELTA
        stub = CODE_VA + entries[name]
        jump = b"\xe9" + struct.pack("<i", stub - (va + 5))
        result[at:at + len(before) // 2] = jump + b"\x90" * (len(before) // 2 - 5)
    struct.pack_into("<I", result, DATA_HEADER + 8, DATA_OWNED)
    struct.pack_into("<I", result, MKRAY_HEADER + 8, virtual)
    struct.pack_into("<I", result, MKRAY_HEADER + 16, raw)
    struct.pack_into("<I", result, 0x1FC, 0x32EDC00 + raw - 0x400)
    struct.pack_into("<I", result, 0x230, (MKRAY_RVA + virtual + 0xFFF) // 0x1000 * 0x1000)
    from .client_battle_settings import patch_navigation
    return patch_navigation(bytes(result))


def _allowed_changes():
    """Input file offsets the bridge may change; everything else must be equal."""
    allowed = set()
    from .client_battle_settings import NAVIGATION_OFFSET, NAVIGATION_RELOCATIONS
    allowed.update(range(NAVIGATION_OFFSET, NAVIGATION_OFFSET + len(NAVIGATION_BEFORE)))
    for va, before in SAVE_REPLY_WINDOWS:
        offset = va - BASE - TEXT_FILE_DELTA
        allowed.update(range(offset, offset + len(before)))
    for offset, _ in NAVIGATION_RELOCATIONS:
        allowed.update((offset, offset + 1))
    for at in (DATA_HEADER + 8, MKRAY_HEADER + 8, MKRAY_HEADER + 16, 0x1FC, 0x230):
        allowed.update(range(at, at + 4))
    for _, va, before, _ in HOOKS:
        at = va - BASE - TEXT_FILE_DELTA
        allowed.update(range(at, at + len(before) // 2))
    allowed.update(range(MKRAY_RAW + CODE_OFFSET, MKRAY_RAW + 0x400))
    return allowed


def upgrade(lab):
    """Install atomically after the statistics patch; idempotent and restartable."""
    from . import client_patch as cp
    from .stats_patch import OUTPUT_SHA256 as stats_output
    if stats_output != INPUT_SHA256 or not OUTPUT_SHA256:
        raise ValueError("Recon bridge is not pinned to the current client chain")
    lab = Path(lab)
    manifest_path = lab / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    source = Path(manifest["game"]) / "wotblitz.exe"
    target = lab / "client" / "wotblitz.exe"
    if cp._sha256(source) != cp.BUILD_SHA256 or manifest["exe_sha256"] != cp.BUILD_SHA256:
        raise ValueError("Game executable differs from the pinned PoC build")
    if target.is_symlink() or target.resolve(strict=True) == source.resolve(strict=True) \
            or os.path.samefile(source, target):
        raise ValueError("Lab executable aliases the pinned game executable")
    data = target.read_bytes()
    current = hashlib.sha256(data).hexdigest()
    recorded = manifest.get("client_exe_sha256", manifest["exe_sha256"])
    from .windows_profile_patch import recognizes
    from .client_tls import recognizes as recognizes_tls
    if recognizes(current, recorded) or recognizes_tls(current, recorded):
        return False
    predecessors = (INPUT_SHA256, LEGACY_OUTPUT_SHA256, NAVIGATION_OUTPUT_SHA256, SAVE_OUTPUT_SHA256, SETTINGS_OUTPUT_SHA256)
    known = (*predecessors, OUTPUT_SHA256)
    if current not in known or recorded not in known:
        raise ValueError("Recon bridge requires a supported recorded lab client")
    if current in predecessors:
        if recorded not in predecessors[:predecessors.index(current) + 1]:
            raise ValueError("Lab executable differs from its recorded fingerprint")
        from .client_battle_settings import patch_navigation
        output = patch(data) if current == INPUT_SHA256 else patch_navigation(data)
        if hashlib.sha256(output).hexdigest() != OUTPUT_SHA256:
            raise ValueError("Recon bridge produced an unknown executable")
        cp._write_client(target, output)
    elif recorded == current and manifest.get("recon_bridge") == METADATA:
        return False
    manifest["client_exe_sha256"] = OUTPUT_SHA256
    manifest["recon_bridge"] = dict(METADATA)
    cp._write_manifest(manifest_path, manifest)
    return True


def add_marker_opacity(document, relative):
    """Give one marker prototype the UIOpacityComponent the bridge drives.

    Pure transform of a parsed DAVA UI document. Opacity 1 keeps the native
    look; the component is the only addition. An unexpected existing
    component is refused rather than overwritten.
    """
    name = MARKER_OPACITY[relative]
    matches = [item for item in document.get("Prototypes", []) if item.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"Unsupported marker prototype: {name}")
    components = matches[0].setdefault("components", {})
    existing = components.get("UIOpacityComponent")
    if existing is None:
        components["UIOpacityComponent"] = {"opacity": 1.0}
    elif existing != {"opacity": 1.0}:
        raise ValueError(f"Unexpected native opacity on {name}")
    return document



# Build of native/recon_client.c + recon_wire.c with CFLAGS/TOOLCHAIN above.

# Build of native/recon_client.c + recon_wire.c with CFLAGS/TOOLCHAIN above.
ENTRIES = {'message': 12, 'control': 121, 'markers': 197, 'minimap': 257, 'reset': 317, 'ray': 377, 'aim_begin': 831, 'aim_end': 894, 'lamp_bind': 445, 'lamp_unbind': 506, 'silhouette': 566, 'armor': 633, 'armor_unbind': 710, 'armor_destroy': 771}
BLOB = bytes.fromhex(
    "e800000000582d05c4b104c39c6089e781ec1002000083e4f00fae0424dbe3d92c2457e8d8ffffffffb53cffffffffb538ffffffffb52cffffffffb5"
    "28ffffff50e8fc07000083c41485c0751b8d4424040fae088b2424619d39c60f84951fc6fc8b0ee9731fc6fc8d4424040fae088b2424619de97d1fc6"
    "fc51ff742408e836000000599c6089e781ec1002000083e4f00fae0424dbe3d92c2457e860ffffff5150e8bc08000083c4088d4424040fae088b2424"
    "619dc204005589e5518d45ffe972a1c6fc9c6089e781ec1002000083e4f00fae0424dbe3d92c2457e81fffffff5150e8970b000083c4088d4424040f"
    "ae088b2424619d5589e56affe974ce0afd9c6089e781ec1002000083e4f00fae0424dbe3d92c2457e8e3feffff5150e8ea0d000083c4088d4424040f"
    "ae088b2424619d5589e56affe9d80e28fd9c6089e781ec1002000083e4f00fae0424dbe3d92c2457e8a7feffff5150e8bd08000083c4088d4424040f"
    "ae088b2424619d5589e56affe98ce607fd9c6089e781ec1002000083e4f00fae0424dbe3d92c24570fb64f1ce867feffff515550e89c0f000083c40c"
    "8d4424040fae088b2424619d84c00f8538502ffde9714e2ffd8986500100009c6089e781ec1002000083e4f00fae0424dbe3d92c2457e821feffff56"
    "50e8dc05000083c4088d4424040fae088b2424619de9f5a126fd9c6089e781ec1002000083e4f00fae0424dbe3d92c2457e8eafdffff5150e8ef0500"
    "0083c4088d4424040fae088b2424619d5589e56affe94fdd25fd5589e59c6089e781ec1002000083e4f00fae0424dbe3d92c2457e8abfdffffff7508"
    "5150e83d08000083c40c8945088d4424040fae088b2424619d8a4508e91da9c6fb9c6089e781ec1002000083e4f00fae0424dbe3d92c2457e86bfdff"
    "ff5150e8cf08000083c40885c075168d4424040fae088b2424619d5589e56affe97c5330fd8d4424040fae088b2424619dc39c6089e781ec10020000"
    "83e4f00fae0424dbe3d92c2457e81efdffff5150e84608000083c4088d4424040fae088b2424619d5689ce8b4e04e9839a2ffd9c6089e781ec100200"
    "0083e4f00fae0424dbe3d92c2457e8e1fcffff5150e80908000083c4088d4424040fae088b2424619d5589e56affe9162008fd9c6089e781ec100200"
    "0083e4f00fae0424dbe3d92c2457e8a5fcffff50e8430f000083c4048d4424040fae088b2424619d5589e581ec18010000e99b6730fd9c6089e781ec"
    "1002000083e4f00fae0424dbe3d92c2457e866fcffff5350e82c0f000083c4088d4424040fae088b2424619d807b15008d5350e98e6830fdffa0fcf0"
    "6e03648b15240000008b0885c975048910eb0439d17506b801000000c3ff403c31c0c383780402750f2b501431c081fab80b00000f96c0c331c0c355"
    "31c989e5538b587883f931740d39d973093954887c740741ebee31c0eb05b8010000005b5dc35589e557565353c645f3ff85d2750731c0e9c3000000"
    "89c389d08b1285d274ef89ce8d8b58707f03390a75e3837a1c0074dd8b523085d274d68b128d8b50e19d0339ca75ca8d8bb0978002394a4075bf8d4d"
    "f38d9300dc80025189c16a016a1fffd2807df30089c175a585c074a18b008d93b82f9e0339d075958b400481c350e6820239d875888b5d0cc1e3028d"
    "530a52ffd085c00f8474ffffff89f1c7005446532d31d2c740044c41422d88480889d68b7d0889d183e6fc83e1038b3437c1e103d3ee89f1884c1009"
    "4239da72e0c644180901b8010000008d65f45b5e5f5dc35531c989e55653bb0100000089ded3e685d6750931f68974c8048934c84183f91075e95b5e"
    "5dc35505c0ca920089e55389d331d283ec088955f88d55f4528d55f8894df489d952ffd08b45f885c074073b4304750231c08b5dfcc9c35589e5538b"
    "55088b4d1089d001d139c874098a5d0c408858ffebf389d05b5dc35589e55389c331c089430489430889431889431c89835c0300008d435083c3606a"
    "106a0050e8b6ffffff68e00000006a0053e8a9ffffff8b5dfc83c418c9c35589e55789cf5689c6535383ba800200000089d37532c782800200005549"
    "4f70c7828402000061636974c7828802000079436f6dc7828c020000706f6e65c782900200006e7400008d93940200006a186a00528955f0e846ffff"
    "ff8b55f08d8380020000c783a4020000120000008983940200008d863005ce00c783a8020000120000006a00528b4d0cffd089c183c40c85c074148d"
    "86e41e7803390175088b450c394108740231c98d978000000089f88955f08b1089c339550875088b550c395004740c8b55f083c00839d075e531db81"
    "c630ab020185c9741e837d1000741d85db7414837914007507680000803fffd631c0894304890383c8ffeb3385db751e31c0833cc70074084083f810"
    "75f4ebe78d1cc78b450889038b450c8943048379140074046a00ffd689d829f8c1f8038d65f45b5e5f5dc35589e5578b45088b5510568b750c01c289"
    "c739d77403a4ebf95e5f5dc35589e557565389c38d8503ffffff81ecd4010000888d02ffffffc1e102515250e8beffffff83c40c837b0402c6840d03"
    "ffffff007405ff4334eb428d435083c1028dbd02ffffff8db520feffff56ff7318ff7370505157e8d00f000083c41885c074d38b45088d7b60b93800"
    "0000f3a5ff4330c74318010000008943148d65f45b5e5f5dc35589e5538b45088b4d108b550c01c139c874118a1a3818740719c083c801eb064042eb"
    "eb31c05b5dc35589e5568b750c538b5d0885f674378d836c8784033906752d81c380fc710489d8e8dafbffff85c0741c8b83c402000085c0740439f0"
    "750e31c089b3c40200008983cc0200005b5e5dc35589e58b5508648b0d24000000398a80fc7104751f8d8280fc71048b88c4020000394d0c750e31d2"
    "8990cc0200008990c40200005dc35589e5575653528b750c8b55088b7d1085f60f84ff00000039f70f82f700000029f731c083ff070f8eec00000089"
    "f883e0030f85df000000813e544653310f85d5000000817e0445434f4e0f85c8000000ff82b8fc71048d9a80fc710489d8e81cfbffff85c00f84a100"
    "00008b45183945140f859500000081fff30000008b55080f8f8600000089f9c1e90283f908755d89d0e8e2faffff837e080189c7756d837b04017567"
    "8b530885d274608b46140b46180b461c0b4610745239560c754d89d883c610e873fcffff8b46fc8943088d43506a105650e8d9fdffffff432c83c40c"
    "c7430402000000897b14eb2283ff2b7e1a89d0894df0e87dfaffff8b4df089f25089d8e8c8fdffff58eb03ff4334b801000000eb0231c08d65f45b5e"
    "5f5dc35589e556538b45088d9880fc710489d8e84afaffff85c00f84960000008b4508e834faffff837b040289c675142b43143d88130000767c89d8"
    "e8defbffffff43408b431085c0740c89f229c281facf070000765f85f6b8010000000f45c6837b040089431075210f3131c68b430cba010000004089"
    "430c69c0b979379e39c6740489f231c28953088d43086a01b948000000508b45088b550ce821faffff5a5985c0740cff4324c7430401000000eb03ff"
    "43288d65f85b5e5dc35589e556538b450c8b750885c074698d9e80fc71043b4344755e8b8680fc710485c07454648b152400000039d0754989d8e82c"
    "fbffff31c08943448983680300008983640300008d8600fe710481c650ff710468800000006a0050e8e2faffff68800000006a0056e8d5faffff31d2"
    "ff434083c4188953108d65f85b5e5dc35589e5578b550c8b4508565385d27479648b0d24000000398880fc7104756a8bb0d8ff71048db880fc710485"
    "f6755a8d88f4157103390a75508b4a088d9850ff71048d90d0ff7104833b007435394b047530807d1000750931c08943048903eb28e8baf8ffff89c2"
    "89f8e8d8f8ffff85c074168b1389f8e8e3f8ffff85c0740ceb0783c30839d375bf8b75105b89f05e5f5dc35589e58b5508648b0d24000000398a80fc"
    "710475258d8280fc71048b8864030000394d0c751431d289905c0300008990680300008990640300005dc35589e5575653578b7d0c8b750885ff742a"
    "8d867c548503390775208d9e80fc710489d8e82ff8ffff85c0740f807f140075135756e88bffffff5b5e31c9894df0e9c400000031d289bb64030000"
    "89f0899368030000e8f7f7ffff89c289c189d8e813f8ffff8945f085c074662b4b2081f9e8030000775b8b471085c074548d96d42380033910754a8b"
    "500485d274438d8e8c7d7f03390a75398b787485ff74328d86fc3a7103390775288b521c8b4b1c31c039c1741c83f810741739948340010000740340"
    "ebeb89d8e8c2f7ffff85c0740d31c089835c030000e95cffffff3bbb5c03000074126a0181c6d0e47b0089f9ffd689bb5c030000ff8360030000c783"
    "68030000010000008b45f08d65f45b5e5f5dc35589e557565381eca00000008b750c8b7d0885f60f846d0200008d8760ee820339060f855f0200008d"
    "9f80fc710489d8e80ef7ffff898568ffffff85c00f844402000089f8e8f3f6ffff8b8bc4020000898570ffffff85c9746d8d876c878403390175638b"
    "9570ffffff89d8e8f3f6ffff89c28b83cc02000085d2743085c075188a414cc783cc02000001000000d0e883e0018983c802000031d2837b74008d87"
    "2050d0000f95c252ffd0eb1a85c07416ffb3c80200008d872050d000ffd031c9898bcc0200008b434439c6741c85c074188b8570ffffff2b43203de8"
    "0300007708ff433ce99d0100008b8570ffffff8973448943208b8e9c01000085c90f848301000089c289d8e857f6ffff31d2898554ffffff8b4104b9"
    "01000000899558ffffff898574ffffff89956cffffff89955cffffff8d51ff8b849574ffffff85c00f840801000080780d000f85fe000000ff855cff"
    "ffff83bd5cffffff40770e8d710189b564ffffff83fe20760a31c089431ce90f0100008b3089b49574ffffff8b500889948d74ffffff8b50108b4014"
    "85d20f84e600000085c00f84de0000008d8f48f3820339080f85d00000003950280f85c700000083b8a0000000020f85ba00000083bd6cffffff0f77"
    "168bb56cffffff8994b3400100008d760189b56cffffff8b407085c00f84900000008d8f6c06830339080f85820000008b4014898560ffffff85c074"
    "7583bd54ffffff008b8568ffffff740789d8e85cf5ffff508d8b8001000089f8ffb560ffffff5289dae808f7ffff83c40c89c185c0783fb801000000"
    "d3e0098558ffffffeb3085d2740789d1e9dbfeffff8b9558ffffff8d8380010000e819f6ffff8b856cffffff89431c8b8570ffffff894320eb088b95"
    "64ffffffebcc8d65f45b5e5f5dc35589e557565381eca40000008b45088b7d0c898570ffffff85ff0f84070200000558aa840339070f85fa0100008b"
    "8570ffffff8d9880fc710489d8e874f4ffff89855cffffff85c00f84d90100008b872c010000898564ffffff85c00f84c50100008b8570ffffff8bb5"
    "64ffffff05acac840339060f85ac0100008b8570ffffffe828f4ffff89c18b434839c7741885c0741489c82b434c3de80300007708ff433ce9800100"
    "00897b4889ca89d8894b4ce81ff4ffff898550ffffff8b431c89856cffffff83f81077262b4b2081f9e8030000771b31c039856cffffff7419408b94"
    "833c01000089948570ffffffebe731d289956cffffff8b856cffffff8db574ffffff898568ffffff31c08b8cc30002000031d285c97426399568ffff"
    "ff7408390c96741942ebf08b9568ffffff898c9574ffffff8d4a01898d68ffffff4083f81075c731c031f6898554ffffff39b568ffffff0f84b40000"
    "008b84b574ffffff8d9730010000898560ffffff89c18b8570ffffffe8a1f4ffff85c00f84860000008b480c8b8564ffffff8d50048b8570ffffffe8"
    "82f4ffff85c0746b8b400c898558ffffff85c0745e8b856cffffff39c67318f68550ffffff01740f8b9560ffffff89d8e822f3ffffeb068b855cffff"
    "ff508d8b0002000089daffb558ffffffffb560ffffff8b8570ffffffe8bdf4ffff83c40c89c185c0780db801000000d3e0098554ffffff46e940ffff"
    "ff8b9554ffffff8d65f48d83000200005b5e5f5de9cef3ffff8d65f45b5e5f5dc35589e557565383ec1c8b750c8b5d0885f60f84480100008d834330"
    "e2013946040f85390100008dbb80fc710489f8e852f2ffff85c00f8424010000837d100074068b46a48945108b46dc85c00f840d0100008b401085c0"
    "0f84020100008d93c41f800339100f85f4000000837804000f84ea0000008b80f801000085c00f84dc0000008d93d0867f0339100f85ce0000008b48"
    "3085c90f84c30000008b51088b410c85d20f84b500000039d00f82ad0000003941100f82a400000029d089c1c1f902894de03d000100000f8f8f0000"
    "00837d10008db3d42380038d8b8c7d7f030f9545e731c08945ec8945e88945f08975dc894dd88b4df0394de074498b45f08b348285f674588b4ddc39"
    "0e75518b4e0485c9744a8b45d8390175438b767485f6743c8d83fc3a710339067532397510750f807de70074098b411cff45ec8945e8ff45f0ebaf83"
    "7dec017713c787c0020000010000008b45e88987bc02000083c41c5b5e5f5dc35589e5538b45088d9880fc710489d8e80af1ffff85c0740e31c08983"
    "bc0200008983c00200005b5dc35589e557565383ec4c8b75088b7d0c8d9e80fc710489d8e8d9f0ffffa8010f844404000085ff0f843c0400008b5710"
    "85d20f84310400008d86c41f800339020f85230400008b420485c00f84180400008b8bc0020000894dd485c974098bbbbc020000897dd483c07c8955"
    "e48945c05056e820f6ffff8b55e48b8af80100008955dc5f5885c90f84600300008d86d0867f0339010f85520300008b4130894de085c00f84440300"
    "008b50088b780c8955d085d20f843303000039d70f822b0300003978100f822203000029d781ff000100000f8f140300008b86802f67048945bc85c0"
    "0f84030300008b83640300008945e489f0e8f6efffff837de4008945b8744a8b55e48d867c5485038b4de039028b55dc75378b45e4c745d801000000"
    "8b40048945e08b45e48b40108945c88b45e039c2742231d28955d885c074193948040f94c00fb6c08945d8eb0b31d28955d88955c88955e031c0c1ff"
    "02897da88945b08945b48945c48b55c43955a80f84090200008b45d08b7dc48b04b885c00f84670200008d96d423800339100f85590200008b500485"
    "d20f844e0200008d8e8c7d7f03390a0f85400200008b4874894ddc85c90f84320200008b7ddc8d8efc3a7103390f0f85210200008b4de039c80f94c1"
    "0fb6c9094dd88b4dc839c80f94c00fb6c00945b08b421c31d28945cc8b45b82b43203de803000077268b531c31c039d0731483f810740f8b7dcc3bbc"
    "8340010000740740ebe831d2eb05ba010000008dbed0ff71048d8650ff7104897dac8b4dcc89c73b0875088b4ddc3b4804740a83c0083945ac75e731"
    "ff85d2750885ff0f841d0100008955ac8b4ddc8d86b05cd5006a00ff75bcffd089c185c00f84000100008d86f415710339010f85f20000008b45dc3b"
    "41080f85e6000000807910010f87dc0000008b55acff835003000085d274228b55b889d8e866eeffff85c074148b55cc89d8894dace86deeffff8b4d"
    "ac85c0740985ff7511e9a40000008d93d002000085ff755beb31807910007522c78358030000010000008d86906f78006a01ffd031c0ff8354030000"
    "89835803000031c08947048907eb6780791000746131c083bcc3d00200000074084083f81075f0eb4d8d3cc28b45cc89078b45dc8947048079100074"
    "288955dc8d86906f7800c78358030000010000006a00ffd031c0ff83540300008b55dc89835803000029d7b80100000089f9c1f903d3e00945b4ff45"
    "c4e9ebfdffff8b55b48d83d0020000e8abeeffff8b55b08555d8745983bb6c030000007550837de400744a8b45e480781400750b5056e8b0f4ffff5f"
    "58eb368bbb68030000ff75e456e8d9f4ffff5a5985ff7421a801751d31ff8b4de48d86301ae201c7836c03000001000000ffd089bb6c030000837b04"
    "02757689f0e8f6ecffff89c78b45d43983ac020000751683bbb802000000740d89f82b83b002000083f863764c8b43088b55c0b9410000008945e88b"
    "83b4020000408983b40200008945ec8b45d48945f08d45e86a035089f0e808edffff5a5985c074158b45d4ff83b802000089bbb00200008983ac0200"
    "008d65f45b5e5f5dc38b00c35589e557565381ece40000008b5d088b4d0c85db7406837d1000750731c0e9980100008d41d23dc400000077ef837d1c"
    "0074e9837d180177e30fb6038d50f580fa3177d88d04850200000039c875cd807c0bff0075c68d4301e89fffffff3d5446533175b78d4305e890ffff"
    "ff3d45434f4e75a88d4309e881ffffff48759d8d41fa83caff898510ffffff31c00fb674030131f2be0800000089d783e201f7dad1ef81e22083b8ed"
    "31fa4e75ec8bbd10ffffff4039f872d58d440bfbf7d2e83affffff39d00f8551ffffff31c031d20fb64c030d4009ca83f81075f385d20f8438ffffff"
    "8d7b0d6a10ff751057e867efffff83c40c85c00f851fffffff8d9514ffffff68e00000006a0052e80fedffff8d9514ffffff6a105752e898eeffff8d"
    "431de8d6feffff83c418898524ffffff394514720af64518010f85ddfeffff8d4321e8b6feffff898528ffffff89c18d4325e8a6feffff89c283f831"
    "0f87bafeffff83f9010f87b1feffff0fb60383e80b39d00f85a3feffff89952cffffff83c32931c939d6741e89d883c304e86bfeffff39c10f8382fe"
    "ffff8984b530ffffff89c146ebde8b7d1c8db514ffffffb938000000b801000000f3a58d65f45b5e5f5dc3"
)
BLOB_SHA256 = "f12c2716bc15205d7b15f1fb940655bbfe33a7fd88003c6bd1b8b65c7effc669"
LEGACY_OUTPUT_SHA256 = "c6b5638928dbe86e841d8cb16ec47182b3c47d1af005b2bad5c2a6c6ec3b40b4"
NAVIGATION_OUTPUT_SHA256 = "08d548378d3357a952d96827ac5662f252de7a755c305f6500d957dd361e789f"
SAVE_OUTPUT_SHA256 = "b5e5bfc5e640f52f96d97dcaeb28a8c908b7a9502b8167ac4c6f81e199bef2d6"
SETTINGS_OUTPUT_SHA256 = "50b153c29f8520d1172ba387f16b3870bb8e1cba07b69a2a5d4968a9d3d82f88"
OUTPUT_SHA256 = "a8cd7bd4e1c70e25f43f8a0d5b3c645325e0590cb255211212f307fbfdee039d"
METADATA["output_sha256"] = OUTPUT_SHA256
