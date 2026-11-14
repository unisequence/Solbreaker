"""Build reviewed source archives and optional pinned Windows native packages."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from solbreaker.platform_io import read_regular_file

FILES = ('README.md', 'README.ru.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md',
         'pyproject.toml', '.gitignore', 'setup.sh', 'setup.ps1',
         'start-server.sh', 'start-server.ps1', 'launch-gui.sh', 'launch-gui.ps1',
         'launch-gui.cmd', 'launch-gui.vbs', 'launch-tui.sh', 'THIRD_PARTY_NOTICES.md',
         'release-files.txt', 'launch-tui.cmd')
FOLDERS = ('solbreaker', 'tools', 'tests', 'examples', 'licenses', 'docs', 'requirements', '.github')
OPTIONAL = ('LICENSE', 'LICENSE.md', 'LICENSE.txt')
CRATES = ('sim_core', 'impact_core', 'armor_query', 'rapier_dynamic', 'physics_core')
SOURCE_CRATES = CRATES
WINDOWS_LIBRARIES = {crate: 'tfs_'+crate+'.dll' for crate in CRATES}
WINDOWS_LIBRARIES['physics_core'] = 'solbreaker_physics_core.dll'
LINUX_LIBRARIES = {crate: f'tfs_{crate}.so' for crate in CRATES}
LINUX_LIBRARIES.update(rapier_dynamic='libtfs_rapier_dynamic.so',
                       physics_core='libsolbreaker_physics_core.so')
WHEEL = 'pybullet-3.2.7-cp312-cp312-win_amd64.whl'
GAME_CATALOG = 'solbreaker/assets/build-11.21.0.1235_7376187-files.json.gz'
FORBIDDEN_SUFFIXES = frozenset(('.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3',
    '.db', '.log', '.pcap', '.pcapng', '.dvpl', '.exe', '.dll', '.so', '.dylib',
    '.pyd', '.obj', '.gltf', '.glb', '.zip', '.whl'))
SECRET = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|'
                    rb'\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|'
                    rb'sk-(?:proj-|ant-)[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b')


def source_name(name):
    path = PurePosixPath(name)
    if (not name or not path.parts or name != path.as_posix() or path.is_absolute() or '\\' in name
            or ':' in name or '..' in path.parts
            or (name not in FILES + OPTIONAL and path.parts[0] not in FOLDERS)):
        raise ValueError('Invalid release input path: '+name)
    if path.name.lower().startswith('.env') or path.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise ValueError('Forbidden release input: '+name)
    return name


def build_artifact(name):
    path = PurePosixPath(name)
    if len(path.parts) == 4 and path.parts[:2] == ('solbreaker', 'native') and path.parts[2] in SOURCE_CRATES:
        if path.parts[2] == 'physics_core':
            return path.name in ('libsolbreaker_physics_core.so', 'solbreaker_physics_core.dll')
        return (path.name.endswith('.build.json') or path.name in
                {prefix+'tfs_'+path.parts[2]+suffix for prefix in ('', 'lib')
                 for suffix in ('.so', '.dll', '.dylib')})
    return (len(path.parts) == 2 and path.parts[0] == 'solbreaker'
            and path.name.startswith(('_support_checks', '_contact_queries')) and path.suffix in ('.so', '.pyd'))


def read_input(path, limit=64 << 20):
    before = path.lstat()
    if limit <= 64 << 20:
        content = read_regular_file(path, limit)
    else:
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('Expected a bounded regular release archive')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError('Release archive changed while opening')
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError('Release archive exceeds its size limit')
    after = path.lstat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if tuple(getattr(before, key) for key in fields) != tuple(getattr(after, key) for key in fields):
        raise ValueError('Release input changed while reading: '+str(path))
    return content


def source_inputs():
    manifest = read_input(ROOT/'release-files.txt').decode('utf-8')
    names = [source_name(line) for line in manifest.splitlines() if line and not line.startswith('#')]
    if len(names) != len(set(names)) or not set(FILES).issubset(names):
        raise ValueError('Release file list has duplicates or missing required root files')
    selected = set(names)
    for folder in FOLDERS:
        parent = ROOT/folder
        if parent.is_symlink():
            raise ValueError('Source archive refuses symbolic links: '+str(parent))
        for directory, children, files in os.walk(parent, followlinks=False):
            directory = Path(directory)
            for child in children[:]:
                path = directory/child
                relative = path.relative_to(ROOT).as_posix()
                if path.is_symlink():
                    raise ValueError('Source archive refuses symbolic links: '+relative)
                if child == '__pycache__' or relative in {'solbreaker/native/'+crate+'/target' for crate in SOURCE_CRATES}:
                    children.remove(child)
            for filename in files:
                path = directory/filename
                relative = path.relative_to(ROOT).as_posix()
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Expected a regular source input: '+relative)
                if relative not in selected and not build_artifact(relative):
                    raise ValueError('Unreviewed file in release source directory: '+relative)
    selected.update(name for name in OPTIONAL if (ROOT/name).exists())
    return {name: read_input(ROOT/name) for name in sorted(selected)}


def validate_contents(payloads):
    game_hashes = set()
    if GAME_CATALOG in payloads:
        with gzip.GzipFile(fileobj=io.BytesIO(payloads[GAME_CATALOG])) as stream:
            raw = stream.read((16 << 20) + 1)
        if len(raw) > 16 << 20:
            raise ValueError('Game integrity catalog exceeds size limit')
        game_hashes = {item[2] for item in json.loads(raw)}
    for name, content in payloads.items():
        if SECRET.search(content):
            raise ValueError('Possible secret in release input: '+name)
        if hashlib.sha256(content).hexdigest() in game_hashes:
            raise ValueError('Release input matches an original game file: '+name)


def package(output, *, windows_native=None, windows_wheel=None, windows_runtime=None,
            linux_native=None, server_only=False, windows_physics=None):
    if bool(windows_native) != bool(windows_wheel):
        raise ValueError('Provide both the Windows DLL directory and the Python 3.12 Bullet wheel')
    if windows_runtime and not windows_native:
        raise ValueError('Portable runtime requires the accepted Windows native inputs')
    if windows_physics and not windows_native:
        raise ValueError('A rebuilt Windows controller requires the remaining pinned Windows inputs')
    if linux_native and (windows_native or not server_only):
        raise ValueError('Linux libraries are only supported in a dedicated Linux package')
    if server_only and not (windows_runtime or linux_native):
        raise ValueError('The dedicated package requires native libraries')
    payloads = source_inputs()
    validate_contents(payloads)
    if server_only:
        for name in FILES:
            if name.startswith('launch-gui.'):
                del payloads[name]
    if linux_native:
        binaries = {f'solbreaker/native/{crate}/{name}': read_input(Path(linux_native)/crate/name)
                    for crate, name in LINUX_LIBRARIES.items()}
        for name, content in binaries.items():
            if (content[:7] != b'\x7fELF\x02\x01\x01' or
                    content[16:20] != b'\x03\x00\x3e\x00'):
                raise ValueError('Expected a Linux x86-64 shared library: '+name)
        validate_contents({GAME_CATALOG: payloads[GAME_CATALOG], **binaries})
        payloads.update(binaries)
        payloads['runtime/manager.json'] = b'{"reuse_native": true, "play_mode": "local"}\n'
    if windows_native:
        expected = json.loads(payloads['requirements/native-artifacts.json'])['sha256']
        binaries = {'solbreaker/native/'+crate+'/'+filename:
                    Path(windows_native)/filename for crate, filename in WINDOWS_LIBRARIES.items()}
        # Older accepted native sets retain the Python contact-query fallback.
        contact = Path(windows_native)/'_contact_queries.pyd'
        if contact.exists():
            binaries['solbreaker/_contact_queries.pyd'] = contact
        wheel = Path(windows_wheel)
        if wheel.name != WHEEL:
            raise ValueError('Expected the accepted CPython 3.12 x64 Bullet wheel')
        binaries['wheels/'+WHEEL] = wheel
        for name, path in binaries.items():
            if not re.fullmatch(r'[0-9a-f]{64}', str(expected.get(path.name, ''))):
                raise ValueError('No accepted Windows SHA-256 for '+path.name+
                                 '; validate the native build before packaging')
            if not path.is_file():
                raise ValueError('Required Windows native package input is missing: '+path.name)
            content = read_input(path)
            if hashlib.sha256(content).hexdigest() != expected[path.name]:
                raise ValueError('Native package input differs from accepted SHA-256: '+path.name)
            payloads[name] = content
        if windows_physics:
            name = 'solbreaker/native/physics_core/solbreaker_physics_core.dll'
            controller = read_input(Path(windows_physics))
            offset = int.from_bytes(controller[60:64], 'little')
            if controller[:2] != b'MZ' or controller[offset:offset+6] != b'PE\0\0\x64\x86':
                raise ValueError('Expected an x86-64 Windows ground controller')
            validate_contents({GAME_CATALOG: payloads[GAME_CATALOG], name: controller})
            payloads[name] = controller
        payloads['runtime/manager.json'] = (json.dumps(dict(reuse_native=True, play_mode='local', gui_theme='dark'))+'\n').encode()
    if windows_runtime:
        from tools.windows_bundle import payloads as runtime_payloads
        payloads.update(runtime_payloads(windows_runtime, payloads, read_input, server_only=server_only))
    hashes = [hashlib.sha256(content).hexdigest()+'  '+name for name, content in sorted(payloads.items())]
    payloads['SHA256SUMS'] = ('\n'.join(hashes)+'\n').encode()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stream = output.open('xb')
    try:
        with stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(payloads.items()):
                entry = zipfile.ZipInfo('Solbreaker/'+name)
                entry.create_system = 3
                mode = 0o755 if name.endswith('.sh') else 0o644
                entry.external_attr = (stat.S_IFREG | mode) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, content)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return dict(archive=str(output.absolute()), files=len(hashes), bytes=output.stat().st_size,
                sha256=hashlib.sha256(output.read_bytes()).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--windows-native', type=Path)
    parser.add_argument('--windows-wheel', type=Path)
    parser.add_argument('--windows-runtime', type=Path, help='Directory of pinned portable CPython and locked Windows wheels')
    parser.add_argument('--linux-native', type=Path, help='Directory of native crates with freshly built Linux libraries')
    parser.add_argument('--server-only', action='store_true')
    args = parser.parse_args()
    print(json.dumps(package(args.output, windows_native=args.windows_native,
                             windows_wheel=args.windows_wheel, windows_runtime=args.windows_runtime,
                             linux_native=args.linux_native, server_only=args.server_only)))


if __name__ == '__main__':
    main()
