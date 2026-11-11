"""Install the server environment, native kernels, state and optional client."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--game', type=Path, required=True, help='Owned pinned game directory')
    result.add_argument('--state', type=Path, default=ROOT/'data')
    result.add_argument('--client', type=Path, help='New independent client directory')
    result.add_argument('--windows-client', action='store_true', help='Copy resources for native Windows')
    result.add_argument('--proton', type=Path)
    result.add_argument('--steam', type=Path)
    result.add_argument('--host', default='127.0.0.1', help='Private IPv4 address of this server')
    result.add_argument('--http-port', type=int, default=18080)
    result.add_argument('--login-port', type=int, default=20016)
    result.add_argument('--jobs', type=int, choices=range(1, 9), default=2)
    result.add_argument('--skip-build', action='store_true', help='Reuse and validate existing native libraries')
    result.add_argument('--skip-dependencies', action='store_true', help='Reuse a prepared virtual environment')
    result.add_argument('--resume', action='store_true', help='Reuse matching existing server state')
    return result


def run(command):
    subprocess.run([str(value) for value in command], cwd=ROOT, check=True)


def install(args):
    if sys.platform not in ('linux', 'win32') or platform.machine().lower() not in ('x86_64', 'amd64'):
        raise ValueError('Run setup.sh on Linux x86-64 or setup.ps1 on Windows x86-64')
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11 or newer is required')
    args.game = args.game.expanduser().resolve(strict=True)
    args.state = args.state.expanduser().absolute()
    if args.client is not None:
        args.client = args.client.expanduser().absolute()
        if args.client.exists() or args.client.is_symlink():
            raise ValueError('Client directory already exists. Choose a new directory')
        if not args.windows_client and (args.proton is None or args.steam is None):
            raise ValueError('Linux client requires --proton and --steam')
    if args.windows_client and args.client is None:
        raise ValueError('--windows-client requires --client')
    if args.client is not None and (args.client == args.state or args.client in args.state.parents
                                    or args.state in args.client.parents):
        raise ValueError('Server state and client must use separate directories')
    if args.state.exists() and not args.resume:
        raise ValueError('State already exists. Use --resume to retain its accounts and keys')
    bundled = (sys.platform == 'win32' and Path(sys.executable).resolve().parent == (ROOT/'runtime/python').resolve())
    if bundled:
        args.skip_build = True
    if not args.skip_build:
        tools = ('cargo', 'rustc', 'cc') if sys.platform == 'linux' else ('cargo', 'rustc')
        missing = [name for name in tools if shutil.which(name) is None]
        if missing:
            raise ValueError('Missing build tools: '+', '.join(missing)+'. Install Rust >=1.85 and a C/C++ toolchain')
        version = subprocess.check_output(['rustc', '--version'], text=True)
        match = re.match(r'rustc (\d+)\.(\d+)', version)
        if match is None or tuple(map(int, match.groups())) < (1, 85):
            raise ValueError('Rust 1.85 or newer is required')
    if bundled:
        python = Path(sys.executable)
        run([python, ROOT/'tools/install_dependencies.py', '--profile', 'server', '--check'])
    else:
        environment = ROOT/('.venv-gui' if sys.platform == 'win32' else '.venv')
        python = environment/('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        if environment.exists() and not python.exists():
            raise ValueError('Existing .venv is incomplete or belongs to another platform. Choose a clean project environment')
        if not python.exists():
            if args.skip_dependencies:
                raise ValueError('--skip-dependencies requires an existing .venv')
            print('Creating Python environment', flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
        if not args.skip_dependencies:
            print('Installing Python dependencies', flush=True)
            run([python, ROOT/'tools/install_dependencies.py', '--profile', 'server'])
    # Remaining work uses the installed dependencies in the project's environment.
    run([python, ROOT/'tools/setup_state.py', '--options', json.dumps(vars(args), default=str)])


def main():
    options = parser()
    args = options.parse_args()
    try:
        install(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        options.exit(1, f'Setup failed: {error}\n')


if __name__ == '__main__':
    main()
