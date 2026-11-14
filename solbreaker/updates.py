"""Stage a GitHub release and replace its managed files after the launcher exits."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from urllib.request import Request, urlopen
from zipfile import ZipFile

REPOSITORY = 'unisequence/Solbreaker'
STAGING = '.solbreaker-update'
RESTART = 42
CHECKSUMS = 'SHA256SUMS'
PORTABLE = 'runtime/python'
CHECKOUT_RELEASE = 'runtime/release'
MAX_DOWNLOAD = 512 << 20
MAX_UNPACKED = 2 << 30
RELEASE_DIRS = {'.github', 'docs', 'examples', 'licenses', 'requirements',
                'solbreaker', 'tests', 'tools', 'wheels'}
RELEASE_FILES = {'.gitignore', 'CHANGELOG.md', 'CONTRIBUTING.md', 'LICENSE',
                 'README.md', 'README.ru.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md',
                 'pyproject.toml', 'release-files.txt', 'setup.ps1', 'setup.sh',
                 'start-server.ps1', 'start-server.sh', 'launch-gui.cmd', 'launch-gui.ps1',
                 'launch-gui.sh', 'launch-gui.vbs', 'launch-tui.cmd', 'launch-tui.sh'}


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    size: int
    digest: str


def package_kind(root):
    if (not os.environ.get('SOLBREAKER_RELEASE_LAUNCHER')
            or platform.machine().lower() not in ('amd64', 'x86_64')):
        return None
    checkout = (root/'.git').exists()
    packaged = (root/CHECKSUMS).is_file()
    if sys.platform == 'linux' and (checkout or packaged):
        return 'source'
    if (sys.platform == 'win32' and packaged and not checkout
            and Path(sys.executable).resolve().parent == (root/PORTABLE).resolve()):
        return 'windows-x64'
    return None


def request(url):
    return urlopen(Request(url, headers={'User-Agent': 'Solbreaker-Updater'}), timeout=20)


def check_release(current, kind):
    from packaging.version import Version

    installed = Version(current)
    with request(f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=100') as response:
        data = response.read((8 << 20) + 1)
    if len(data) > 8 << 20:
        raise ValueError('GitHub release list exceeds the size limit')
    releases = json.loads(data)
    if not isinstance(releases, list):
        raise ValueError('Invalid GitHub release list')
    candidates = []
    for release in releases:
        tag = release['tag_name']
        if release['draft'] or not re.fullmatch(r'v\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?', tag):
            continue
        version = Version(tag)
        if version <= installed or (not installed.is_prerelease and
                                    (version.is_prerelease or release['prerelease'])):
            continue
        candidates.append((version, tag, release['assets']))
    for version, tag, assets in sorted(candidates, key=lambda candidate: candidate[0], reverse=True):
        filename = f'Solbreaker-{tag[1:]}-{kind}.zip'
        for asset in assets:
            if asset['name'] != filename or asset['state'] != 'uploaded':
                continue
            digest = asset.get('digest') or ''
            size = asset['size']
            url = f'https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}'
            if (asset['browser_download_url'] != url or not 0 < size <= MAX_DOWNLOAD
                    or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest)):
                raise ValueError(f'Invalid GitHub asset metadata for {filename}')
            return Release(str(version), url, size, digest.removeprefix('sha256:'))
    return None


def managed_path(name):
    path = PurePosixPath(name)
    if (not path.parts or path.as_posix() != name or path.is_absolute()
            or any(part == '..' or part[-1] in '. ' or
                   re.search(r'[\\:\x00-\x1f<>"|?*]', part) or
                   re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])',
                                part.split('.')[0], re.IGNORECASE) for part in path.parts)):
        raise ValueError(f'Invalid update path: {name!r}')
    if (name in RELEASE_FILES or name in (CHECKSUMS, 'runtime/manager.json')
            or (len(path.parts) > 1 and path.parts[0] in RELEASE_DIRS)
            or (len(path.parts) > 2 and path.parts[:2] == ('runtime', 'python'))):
        return path
    raise ValueError(f'Unexpected update path: {name!r}')


def manifest(text):
    files = {}
    folded = set()
    for line in text.splitlines():
        digest, separator, name = line.partition('  ')
        managed_path(name)
        if not separator or not re.fullmatch(r'[a-f0-9]{64}', digest) or name.casefold() in folded:
            raise ValueError('Invalid release checksum manifest')
        folded.add(name.casefold())
        files[name] = digest
    if not {'pyproject.toml', 'solbreaker/__init__.py'}.issubset(files) or CHECKSUMS in files:
        raise ValueError('Incomplete release checksum manifest')
    return files


def download(release, destination):
    digest = hashlib.sha256()
    size = 0
    with request(release.url) as response, destination.open('xb') as stream:
        while chunk := response.read(1 << 20):
            size += len(chunk)
            if size > release.size:
                raise ValueError('Update download exceeds the published size')
            digest.update(chunk)
            stream.write(chunk)
    if size != release.size or digest.hexdigest() != release.digest:
        raise ValueError('Update download failed SHA-256 verification')


def unpack(archive, payload, kind):
    with ZipFile(archive) as bundle:
        members = {}
        folded = set()
        total = 0
        for member in bundle.infolist():
            name = member.filename.removeprefix('Solbreaker/')
            if name == member.filename or not stat.S_ISREG(member.external_attr >> 16):
                raise ValueError('Unexpected ZIP entry in the Solbreaker package')
            managed_path(name)
            total += member.file_size
            if total > MAX_UNPACKED or name.casefold() in folded:
                raise ValueError('Invalid ZIP size or duplicate update path')
            folded.add(name.casefold())
            members[name] = member
        checksums = members.get(CHECKSUMS)
        if checksums is None or checksums.file_size > 4 << 20:
            raise ValueError('Missing or oversized release checksum manifest')
        raw = bundle.read(checksums)
        files = manifest(raw.decode('utf-8'))
        if set(members) != set(files) | {CHECKSUMS}:
            raise ValueError('ZIP files differ from the release checksum manifest')
        if kind in ('windows-x64', 'server-windows-x64'):
            required = {f'{PORTABLE}/python.exe', f'{PORTABLE}/solbreaker-bundle.json'}
            if kind == 'server-windows-x64':
                required.update({'setup.ps1', 'start-server.ps1', 'tools/start-server.cmd',
                                 'launch-tui.cmd', 'tools/server_depot.py'})
            else:
                required.update({'launch-gui.ps1', 'launch-gui.vbs', f'{PORTABLE}/pythonw.exe'})
        else:
            if kind == 'server-linux-x86_64':
                required = {'setup.sh', 'start-server.sh', 'launch-tui.sh', 'tools/server_depot.py',
                            'solbreaker/native/sim_core/tfs_sim_core.so',
                            'solbreaker/native/impact_core/tfs_impact_core.so',
                            'solbreaker/native/armor_query/tfs_armor_query.so',
                            'solbreaker/native/rapier_dynamic/libtfs_rapier_dynamic.so',
                            'solbreaker/native/physics_core/libsolbreaker_physics_core.so'}
            else:
                required = {'launch-gui.sh'}
            if any(name.startswith((PORTABLE+'/', 'wheels/')) for name in files):
                raise ValueError('Unexpected Windows runtime in the source package')
        if not required.issubset(files):
            raise ValueError('Incomplete update package')

        payload.mkdir()
        (payload/CHECKSUMS).write_bytes(raw)
        for name, expected in files.items():
            target = payload/name
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(members[name]) as source, target.open('xb') as stream:
                digest = hashlib.sha256()
                while chunk := source.read(1 << 20):
                    digest.update(chunk)
                    stream.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f'Invalid SHA-256 for {name}')
            if name.endswith('.sh'):
                target.chmod(0o755)
        return files


def prepare(root, current, kind):
    release = check_release(current, kind)
    if release is None:
        return None
    stage = root/STAGING
    stage.mkdir(mode=0o700)
    try:
        archive = stage/'download.zip'
        payload = stage/'payload'
        download(release, archive)
        files = unpack(archive, payload, kind)
        archive.unlink()
        with (payload/'pyproject.toml').open('rb') as stream:
            version = tomllib.load(stream)['project']['version']
        from packaging.version import Version
        if Version(version) != Version(release.version):
            raise ValueError('Package version differs from the GitHub release tag')
        if kind == 'source':
            build_native(root, payload, files)
        shutil.copyfile(__file__, stage/'apply.py')
    except BaseException:
        shutil.rmtree(stage)
        raise
    return release


def linux_artifacts(root):
    paths = {path for path in root.glob('solbreaker/native/*/*.so')
             if path.name in {f'tfs_{path.parent.name}.so', f'libtfs_{path.parent.name}.so',
                              'libsolbreaker_physics_core.so'}}
    paths.update(root.glob('solbreaker/_contact_queries*.so'))
    return {path.relative_to(root).as_posix() for path in paths}


def build_native(root, payload, after):
    artifacts = linux_artifacts(root)
    if not artifacts:
        return

    def build_input(name):
        return name.startswith('solbreaker/native/') or name in (
            'tools/build_native.py', 'tools/build_contact_queries.py')

    inputs = {name: digest for name, digest in after.items() if build_input(name)}
    if (root/'.git').exists():
        # An initial installation can reuse already built libraries when the
        # checkout and the release have the same native sources.
        before = {name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                  for name in inputs if (root/name).is_file()}
        if before == inputs:
            for name in artifacts:
                target = payload/name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root/name, target)
            return
    else:
        before = manifest((root/CHECKSUMS).read_text(encoding='utf-8'))
        if {name: digest for name, digest in before.items() if build_input(name)} == inputs:
            return
    environment = dict(os.environ, CARGO_TARGET_DIR=str(payload.parent/'cargo'))
    log = root/'runtime/launcher-update.log'
    log.parent.mkdir(exist_ok=True)
    with log.open('wb') as output:
        try:
            subprocess.run([sys.executable, str(payload/'tools/build_native.py')], cwd=payload,
                           env=environment, stdout=output, stderr=subprocess.STDOUT, check=True)
        except subprocess.CalledProcessError as error:
            raise ValueError(f'Native build failed. See {log}') from error


def safe_destination(root, name):
    path = root/name
    if path.resolve() != path:
        raise ValueError(f'Update destination uses a symbolic link or junction: {path}')
    return path


def replace_release(root, stage, *, portable):
    payload = stage/'payload'
    before = manifest((root/CHECKSUMS).read_text(encoding='utf-8'))
    after = manifest((payload/CHECKSUMS).read_text(encoding='utf-8'))
    names = sorted(set(before) | set(after) | {CHECKSUMS})
    names = [name for name in names if name != 'runtime/manager.json' and not name.startswith(PORTABLE+'/')]
    incoming = {name: payload/name for name in names if name in after or name == CHECKSUMS}

    artifacts = linux_artifacts(payload) if not portable else set()
    if artifacts:
        old_artifacts = linux_artifacts(root)
        before.update(dict.fromkeys(old_artifacts))
        names.extend(sorted(artifacts | old_artifacts))
        incoming.update({name: payload/name for name in artifacts})

    if portable:
        # The helper runs from the staged interpreter. Copy that directory to a
        # separate path so its loaded DLLs are never moved or overwritten.
        shutil.copytree(payload/PORTABLE, stage/'python')
        names.insert(0, PORTABLE)
        incoming[PORTABLE] = stage/'python'

    destinations = {name: safe_destination(root, name) for name in names}
    for name, path in destinations.items():
        if path.exists() and (path.is_dir() != (name == PORTABLE)
                              or name not in before and name not in (CHECKSUMS, PORTABLE)):
            raise ValueError(f'Update conflicts with a local file: {path}')

    transaction = stage/'transaction'
    transaction.touch(exist_ok=False)
    changes = []
    try:
        for name, target in destinations.items():
            backup = stage/'previous'/name
            if target.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                target.replace(backup)
            changes.append((target, backup))
            if name in incoming:
                target.parent.mkdir(parents=True, exist_ok=True)
                incoming[name].replace(target)
        if not portable:
            installer = [sys.executable, str(root/'tools/install_dependencies.py'), '--profile', 'gui']
            if subprocess.run(installer + ['--check'], cwd=root).returncode:
                subprocess.run(installer, cwd=root, check=True)
    except BaseException:
        for target, backup in reversed(changes):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            if backup.exists():
                backup.replace(target)
        transaction.unlink()
        raise
    transaction.unlink()


def install_from_checkout(root, stage):
    destination = safe_destination(root, CHECKOUT_RELEASE)
    if destination.exists():
        raise FileExistsError(f'Release installation already exists: {destination}')
    transaction = stage/'transaction'
    transaction.touch(exist_ok=False)
    try:
        (stage/'payload').rename(destination)
    except BaseException:
        transaction.unlink()
        raise
    try:
        subprocess.run([sys.executable, '-m', 'venv', str(destination/'.venv')], check=True)
        subprocess.run([str(destination/'.venv/bin/python'),
                        str(destination/'tools/install_dependencies.py'), '--profile', 'gui'],
                       cwd=destination, check=True)
    except BaseException:
        shutil.rmtree(destination)
        transaction.unlink()
        raise
    transaction.unlink()


def apply():
    stage = Path(__file__).resolve().parent
    root = stage.parent
    if stage.name != STAGING:
        raise ValueError('Updater must run from the staged release')
    portable = sys.platform == 'win32'
    if portable:
        payload = stage/'payload'
        subprocess.run([sys.executable, '-B', str(payload/'tools/install_dependencies.py'),
                        '--profile', 'gui', '--check'], cwd=payload, check=True)
    if not portable and (root/'.git').exists():
        install_from_checkout(root, stage)
    else:
        replace_release(root, stage, portable=portable)


if __name__ == '__main__':
    apply()
