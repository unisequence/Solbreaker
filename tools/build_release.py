"""Assemble updater-compatible packages from reviewed, pinned inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from urllib.request import Request, urlopen
from zipfile import ZipFile

from package_source import ROOT, WINDOWS_LIBRARIES, WHEEL, package

from solbreaker import __version__, updates
from tools.windows_bundle import RUNTIME, records

WINDOWS_INPUT_TAG = 'v0.1.0-alpha.5'
WINDOWS_INPUT_COMMIT = 'f4298e538a0af6b567f984f20f37a0a7c5c7fe91'
WINDOWS_INPUT_SHA256 = 'e0ecca50d20919de18b26b7d62f76e17a44ecf95879bbdcdb70f6cc92f3a809d'
WINDOWS_INPUT_ZIP = f'Solbreaker-{WINDOWS_INPUT_TAG[1:]}-windows-x64.zip'
WINDOWS_INPUT_URL = f'https://github.com/{updates.REPOSITORY}/releases/download/{WINDOWS_INPUT_TAG}/{WINDOWS_INPUT_ZIP}'
DEPOT_TAG = 'DepotDownloader_3.4.0'
DEPOT_URL = f'https://github.com/SteamRE/DepotDownloader/releases/download/{DEPOT_TAG}'
DEPOT_ARCHIVES = {
    'linux-x64': 'a999dec66b4850fc961bd50366696d23c2d0fad7b18790e6a5647b2f19097a53',
    'windows-x64': '41c9e9f0df54b3ad02e67a11726756e5c73283bd7c2e1b04acfa5ae4c2ed3767',
}
DEPOT_SOURCE = 'https://codeload.github.com/SteamRE/DepotDownloader/tar.gz/c553ef4d60c00a4f5fd16c9fe017f569001589ff'
# Identical notice files in the upstream bundles' .NET 9.0.0 and 9.0.4 runtimes.
DOTNET_SOURCE = 'https://raw.githubusercontent.com/dotnet/runtime/v9.0.0'
DEPOT_NOTICES = {
    'DepotDownloader-source.tar.gz': (DEPOT_SOURCE, '169c45f707f9d14c5c080483244ae8841afe79a8bd3bc113dfe63933a1300247'),
    'DOTNET-LICENSE.txt': (f'{DOTNET_SOURCE}/LICENSE.TXT', 'cfc21f5e8bd655ae997eec916138b707b1d290b83272c02a95c9f821b8c87310'),
    'DOTNET-THIRD-PARTY-NOTICES.txt': (f'{DOTNET_SOURCE}/THIRD-PARTY-NOTICES.TXT', 'fa3e9d0a2c52476f04fe798d7dc4f6e7edb7b68a3d2f4d5f59a39bce957eedc8'),
}


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(url, path, expected, limit):
    with urlopen(Request(url, headers={'User-Agent': 'Solbreaker-Release'}), timeout=60) as source:
        with path.open('xb') as output:
            digest = hashlib.sha256()
            size = 0
            while chunk := source.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise ValueError(f'Download exceeds limit: {path.name}')
                digest.update(chunk)
                output.write(chunk)
    if digest.hexdigest() != expected:
        raise ValueError(f'Download differs from pinned SHA-256: {path.name}')


def stage_depot(directory):
    notices = {}
    for name, (url, expected) in DEPOT_NOTICES.items():
        path = directory/name
        download(url, path, expected, 2 << 20)
        notices[name] = path.read_bytes()
    bundles = {}
    for platform, expected in DEPOT_ARCHIVES.items():
        filename = f'DepotDownloader-{platform}.zip'
        path = directory/filename
        download(f'{DEPOT_URL}/{filename}', path, expected, 64 << 20)
        binary = 'DepotDownloader.exe' if platform == 'windows-x64' else 'DepotDownloader'
        with ZipFile(path) as archive:
            if sorted(archive.namelist()) != sorted((binary, 'LICENSE')):
                raise ValueError(f'Unexpected files in {filename}')
            bundles[platform] = {**notices, binary: archive.read(binary), 'LICENSE': archive.read('LICENSE')}
    return bundles


def verify_native_source(*, rebuilt_crates=(), rebuilt_contact=False):
    paths = ['tools/build_native.py']
    if not rebuilt_contact:
        paths.extend(('tools/build_contact_queries.py', 'solbreaker/native/contact_queries.c'))
    paths += [f'solbreaker/native/{crate}/{name}' for crate in WINDOWS_LIBRARIES
              if crate not in rebuilt_crates
              for name in ('Cargo.toml', 'Cargo.lock', 'abi.h', 'build.rs', 'src')]
    result = subprocess.run(['git', 'diff', '--quiet', WINDOWS_INPUT_COMMIT, '--', *paths], cwd=ROOT)
    if result.returncode:
        raise ValueError('Windows native sources differ from the accepted build. Refresh the reviewed Windows inputs.')


def stage_windows(directory, native_archive, *, rebuilt_crates=(), rebuilt_contact=False):
    verify_native_source(rebuilt_crates=rebuilt_crates, rebuilt_contact=rebuilt_contact)
    if native_archive is None:
        native_archive = directory/WINDOWS_INPUT_ZIP
        download(WINDOWS_INPUT_URL, native_archive, WINDOWS_INPUT_SHA256, updates.MAX_DOWNLOAD)
    elif sha256(native_archive) != WINDOWS_INPUT_SHA256:
        raise ValueError('Windows input archive differs from pinned SHA-256')

    native = directory/'native'
    native.mkdir()
    inputs = {native/name: f'solbreaker/native/{crate}/{name}' for crate, name in WINDOWS_LIBRARIES.items()}
    if not rebuilt_contact:
        inputs[native/'_contact_queries.pyd'] = 'solbreaker/_contact_queries.pyd'
    inputs[directory/WHEEL] = f'wheels/{WHEEL}'
    with ZipFile(native_archive) as archive:
        for destination, name in inputs.items():
            with archive.open('Solbreaker/'+name) as source, destination.open('xb') as output:
                shutil.copyfileobj(source, output)

    lock = json.loads((ROOT/'requirements/lock.json').read_text(encoding='utf-8'))
    requirements = directory/'windows.txt'
    requirements.write_text('\n'.join(
        f"{record['name']}=={record['version']} " +
        ' '.join(f"--hash=sha256:{file['sha256']}" for file in record['files'])
        for record in records(lock) if record != lock['native_windows_physics']
    )+'\n', encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'pip', '--isolated', 'download', '--no-deps',
                    '--only-binary=:all:', '--require-hashes', '--index-url', 'https://pypi.org/simple',
                    '--platform', 'win_amd64', '--implementation', 'cp', '--python-version', '3.12',
                    '--abi', 'cp312', '--dest', str(directory), '-r', str(requirements)], check=True)
    download(RUNTIME['url'], directory/RUNTIME['filename'], RUNTIME['sha256'], 64 << 20)
    return native


def release_version(tag):
    match = re.fullmatch(r'v(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?', tag)
    if not match:
        raise ValueError('Expected a version tag supported by the updater')
    version, stage, build = match.groups()
    suffix = {'alpha': 'a', 'beta': 'b', 'rc': 'rc'}
    expected = version + (suffix[stage]+build if stage else '')
    project = tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8'))['project']
    if project['version'] != expected or __version__ != expected:
        raise ValueError(f'{tag} requires version {expected} in pyproject.toml and solbreaker/__init__.py')
    return tag[1:]


def build(tag, output, native_archive, linux_native=None, windows_physics=None, windows_rapier=None,
          windows_contact=None, *, linux_contact=None):
    if linux_contact and not linux_native:
        raise ValueError('Linux contact collector requires dedicated Linux libraries')
    version = release_version(tag)
    with tempfile.TemporaryDirectory(prefix='solbreaker-release-') as temporary:
        work = Path(temporary)
        rebuilt_crates = {crate for crate, path in (('physics_core', windows_physics),
                                                    ('rapier_dynamic', windows_rapier)) if path is not None}
        native = stage_windows(work, native_archive, rebuilt_crates=rebuilt_crates,
                               rebuilt_contact=windows_contact is not None)
        depot = stage_depot(work)
        source = work/f'Solbreaker-{version}-source.zip'
        windows = work/f'Solbreaker-{version}-windows-x64.zip'
        server = work/f'Solbreaker-{version}-server-windows-x64.zip'
        package(source, depot_bundle=depot['linux-x64'])
        windows_inputs = dict(windows_native=native, windows_wheel=work/WHEEL,
                              windows_runtime=work, windows_physics=windows_physics,
                              windows_rapier=windows_rapier, windows_contact=windows_contact)
        package(windows, **windows_inputs, depot_bundle=depot['windows-x64'])
        package(server, **windows_inputs, server_only=True)
        archives = [(source, 'source'), (windows, 'windows-x64'), (server, 'server-windows-x64')]
        if linux_native:
            linux = work/f'Solbreaker-{version}-server-linux-x86_64.zip'
            package(linux, linux_native=linux_native, linux_contact=linux_contact, server_only=True)
            archives.append((linux, 'server-linux-x86_64'))
        for archive, kind in archives:
            if archive.stat().st_size > updates.MAX_DOWNLOAD:
                raise ValueError(f'Archive exceeds updater limit: {archive.name}')
            updates.unpack(archive, work/kind, kind)
        checksums = work/updates.CHECKSUMS
        checksums.write_text(''.join(f'{sha256(path)}  {path.name}\n' for path, _ in archives),
                             encoding='ascii', newline='\n')
        output.mkdir()
        for path in [*(archive for archive, _ in archives), checksums]:
            shutil.copyfile(path, output/path.name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--windows-input', type=Path, help='Local copy of the pinned Windows release archive')
    parser.add_argument('--linux-native', type=Path, help='Freshly built Linux native crate directory')
    parser.add_argument('--linux-contact', type=Path, help='Contact collector built for the Linux bundle')
    parser.add_argument('--windows-physics', type=Path, help='Ground controller DLL built from this checkout')
    parser.add_argument('--windows-rapier', type=Path, help='Rapier DLL built from this checkout')
    parser.add_argument('--windows-contact', type=Path, help='Contact query module built from this checkout')
    args = parser.parse_args()
    build(args.tag, args.output, args.windows_input, args.linux_native, args.windows_physics,
          args.windows_rapier, args.windows_contact, linux_contact=args.linux_contact)
