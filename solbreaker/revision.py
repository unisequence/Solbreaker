"""Stable identity for the locally running server implementation.

The runner cannot safely replace a process it did not start: it may be a
manual local server with an active battle.  Instead it compares this digest
with ``/health`` before launching a client.  The digest covers all shipped
server Python/Rust sources, the installed native simulation libraries, and the
pinned game provenance used to build catalogs.
"""
import hashlib
import json
from pathlib import Path
import sys


_MANIFEST_PROVENANCE = ("version", "exe_sha256", "snapshot_sha256")


def implementation_revision(lab: Path) -> str:
    """Return a deterministic revision for code and pinned catalog inputs."""
    lab = Path(lab)
    manifest = json.loads((lab / "manifest.json").read_text())
    package = Path(__file__).resolve().parent
    digest = hashlib.sha256(b"solbreaker-implementation-v1\0")
    for source in sorted(package.glob("*.py")):
        digest.update(source.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    native = package/'native/sim_core'
    sources = [native/'Cargo.toml',native/'Cargo.lock',native/'abi.h',
               *sorted((native/'src').glob('*.rs'))]
    suffix = '.dll' if sys.platform == 'win32' else '.dylib' if sys.platform == 'darwin' else '.so'
    sources.append(native/('tfs_sim_core'+suffix))
    impact = package/'native/impact_core'
    sources.extend([impact/'Cargo.toml',impact/'Cargo.lock',
                    *sorted((impact/'src').glob('*.rs')),
                    impact/('tfs_impact_core'+suffix)])
    physics = package/'native/physics_core'
    sources.extend([physics/'Cargo.toml', physics/'Cargo.lock',
                    *sorted((physics/'src').glob('*.rs')),
                    physics/('solbreaker_physics_core.dll' if sys.platform == 'win32'
                             else 'libsolbreaker_physics_core.so')])
    for source in sources:
        if source.is_file():
            digest.update(source.relative_to(package).as_posix().encode('utf-8'))
            digest.update(b'\0')
            digest.update(source.read_bytes())
            digest.update(b'\0')
    for key in _MANIFEST_PROVENANCE:
        digest.update(key.encode("ascii"))
        digest.update(b"=")
        digest.update(str(manifest.get(key, "")).encode("utf-8"))
        digest.update(b"\0")
    return "v1-" + digest.hexdigest()
