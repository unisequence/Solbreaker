"""Install the locked release dependencies into the active private environment."""
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import tomllib
import venv

ROOT = Path(__file__).resolve().parents[1]
STAMP_NAME = '.solbreaker-dependencies.json'
PROFILES = ('server', 'gui', 'tui', 'support')
BOOTSTRAP_NAMES = frozenset(('pip', 'setuptools', 'wheel', 'packaging'))


def selected_groups(profile, system, version, native):
    if system not in ('linux', 'win32') or version < (3, 11):
        raise ValueError('The locked installer requires CPython 3.11+ on Linux or Windows x64')
    if system == 'win32' and (native or profile == 'server') and version >= (3, 13):
        raise ValueError('The Windows server requires CPython 3.11/3.12; the native bundle requires 3.12 x64')
    if native and (system != 'win32' or version != (3, 12)):
        raise ValueError('The native Windows bundle requires CPython 3.12 x64')
    numpy = 'numpy-legacy' if version < (3, 12) or (system == 'win32' and version < (3, 13)) else 'numpy-modern'
    groups = ['bootstrap', 'base', numpy]
    if system == 'linux' or profile == 'server' or native:
        groups.append('native-windows-physics' if native else 'physics')
    if profile != 'server':
        groups.append(profile)
    return groups


def records_for(profile):
    lock = json.loads((ROOT/'requirements/lock.json').read_text(encoding='utf-8'))
    if lock['schema'] != 1:
        raise ValueError('Unsupported dependency lock schema')
    native = (ROOT/'wheels/pybullet-3.2.7-cp312-cp312-win_amd64.whl').exists()
    groups = selected_groups(profile, sys.platform, sys.version_info[:2], native)
    records = []
    for name in groups:
        records.extend([lock['native_windows_physics']] if name == 'native-windows-physics'
                       else lock['groups'][name])
    return records, native


def fingerprint(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def matches(record, installed):
    try:
        return (installed.get(record['name']) == fingerprint(record)
                and metadata.version(record['name']) == record['version'])
    except metadata.PackageNotFoundError:
        return False


def read_stamp():
    path = Path(sys.prefix)/STAMP_NAME
    if path.is_symlink():
        raise ValueError('Dependency receipt must not be a symbolic link')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) and isinstance(value.get('packages', {}), dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def project_fingerprint():
    content = (ROOT/'pyproject.toml').read_bytes() + str(ROOT).encode() + sys.version.encode()
    return hashlib.sha256(content).hexdigest()


def project_matches(stamp):
    try:
        with (ROOT/'pyproject.toml').open('rb') as stream:
            version = tomllib.load(stream)['project']['version']
        return (stamp.get('project') == project_fingerprint()
                and metadata.version('solbreaker-server') == version)
    except metadata.PackageNotFoundError:
        return False


def pip_command(arguments, *, python=None):
    environment = {k: v for k, v in os.environ.items() if not k.startswith('PIP_')}
    environment['PIP_CONFIG_FILE'] = os.devnull
    subprocess.run([str(python or sys.executable), '-m', 'pip', '--isolated', '--disable-pip-version-check',
                    *arguments], env=environment, cwd=ROOT, check=True)


def install_records(records, *, physics=False, native=False, python=None):
    if not records:
        return
    lines = []
    for record in records:
        hashes = sorted({item['sha256'] for item in record['files']})
        if not hashes or any(len(value) != 64 or any(c not in '0123456789abcdef' for c in value)
                             for value in hashes):
            raise ValueError('Dependency lock contains an invalid SHA-256')
        lines.append(record['name']+'=='+record['version']+' '+
                     ' '.join('--hash=sha256:'+value for value in hashes))
    with tempfile.TemporaryDirectory(prefix='solbreaker-dependencies-') as temporary:
        requirements = Path(temporary)/'requirements.txt'
        requirements.write_text('\n'.join(lines)+'\n')
        linux_bullet_wheel = (sys.platform == 'linux' and sys.version_info[:2] == (3, 11))
        if physics and not native and not linux_bullet_wheel:
            # The upstream source detects NumPy in its build environment. Keep
            # its build inputs explicit and separate from runtime NumPy 2.x.
            build = Path(temporary)/'build-environment'
            venv.EnvBuilder(with_pip=True).create(build)
            builder = build/('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            bootstrap = json.loads((ROOT/'requirements/lock.json').read_text(encoding='utf-8'))['groups']['bootstrap']
            install_records(bootstrap, python=builder)
            wheels = Path(temporary)/'wheels'
            wheels.mkdir()
            pip_command(['wheel', '--require-hashes', '--no-deps', '--no-build-isolation',
                         '--no-cache-dir', '--index-url', 'https://pypi.org/simple',
                         '-r', str(requirements), '--wheel-dir', str(wheels)], python=builder)
            built, = wheels.glob('*.whl')
            # This locally built artifact originates from the hash-verified
            # upstream input above. Verify its bytes again during installation.
            digest = hashlib.sha256(built.read_bytes()).hexdigest()
            requirements.write_text(built.as_uri()+' --hash=sha256:'+digest+'\n')
            pip_command(['install', '--require-hashes', '--no-deps', '--no-index',
                         '--force-reinstall', '-r', str(requirements)])
            return
        args = ['install', '--require-hashes', '--no-deps', '--no-build-isolation',
                '--force-reinstall', '-r', str(requirements)]
        if all(record['name'] in BOOTSTRAP_NAMES for record in records):
            args.append('--no-cache-dir')
        if native:
            args += ['--no-index', '--find-links', str(ROOT/'wheels'), '--only-binary=:all:']
        else:
            args += ['--index-url', 'https://pypi.org/simple']
            if not physics or linux_bullet_wheel:
                args.append('--only-binary=:all:')
        pip_command(args, python=python)


def install(profile, *, check=False):
    if platform.python_implementation() != 'CPython' or platform.machine().lower() not in ('x86_64', 'amd64'):
        raise ValueError('The locked installer supports CPython x64')
    records, native = records_for(profile)
    bundle = ROOT/'runtime/python/solbreaker-bundle.json'
    if (sys.platform == 'win32' and Path(sys.executable).resolve().parent == bundle.parent.resolve()
            and Path(sys.prefix).resolve() == bundle.parent.resolve()):
        stamp = json.loads(bundle.read_text(encoding='utf-8'))
        ready = (stamp.get('schema') == 1
                 and stamp.get('project_sha256') == hashlib.sha256((ROOT/'pyproject.toml').read_bytes()).hexdigest()
                 and all(matches(record, stamp.get('packages', {})) for record in records))
        if not check and not ready:
            raise ValueError('Bundled dependencies are incomplete. Extract the complete release archive again')
        return ready
    stamp = read_stamp()
    installed = stamp.get('packages', {})
    pending = [record for record in records if not matches(record, installed)]
    if check:
        return not pending and project_matches(stamp)
    if sys.prefix == sys.base_prefix:
        raise ValueError('Run the installer with the project virtual environment Python')
    sys.path.insert(0, str(ROOT))
    from solbreaker.platform_io import private_lock
    with private_lock(Path(sys.prefix), 'dependencies.lock'):
        # Re-read after taking the lock so a completed concurrent install can be reused.
        stamp = read_stamp()
        installed = stamp.get('packages', {})
        pending = [record for record in records if not matches(record, installed)]
        install_records([record for record in pending if record['name'] in BOOTSTRAP_NAMES])
        install_records([record for record in pending
                         if record['name'] not in BOOTSTRAP_NAMES and record['name'] != 'pybullet'])
        install_records([record for record in pending if record['name'] == 'pybullet'],
                        physics=True, native=native)
        if pending or not project_matches(stamp):
            pip_command(['install', '--no-deps', '--no-build-isolation', '-e', str(ROOT)])
        pip_command(['check'])
        for record in records:
            if metadata.version(record['name']) != record['version']:
                raise ValueError('Installed dependency differs from lock: '+record['name'])
            installed[record['name']] = fingerprint(record)
        stamp = dict(project=project_fingerprint(), packages=installed)
        descriptor, temporary = tempfile.mkstemp(prefix='.dependencies-', dir=sys.prefix)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(stamp, stream, sort_keys=True)
            os.replace(temporary, Path(sys.prefix)/STAMP_NAME)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=PROFILES, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        success = install(args.profile, check=args.check)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Dependency setup failed: {error}\n')
    parser.exit(0 if success else 1)


if __name__ == '__main__':
    main()
