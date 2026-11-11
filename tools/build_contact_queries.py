"""Build the optional contact collector against CPython's stable 3.11+ ABI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import sysconfig
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--cc', help='Compiler command; no shell is used')
    parser.add_argument('--platform', choices=('linux', 'win32'), default=sys.platform)
    parser.add_argument('--python-include', action='append', type=Path)
    parser.add_argument('--python-lib', type=Path, help='Windows python3.lib or libpython3.a')
    parser.add_argument('--optional', action='store_true', help='Keep Python queries if compilation is unavailable')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = (args.output_dir or root/'solbreaker').resolve()
    windows = args.platform == 'win32'
    compiler = shlex.split(args.cc or ('cl' if windows else sysconfig.get_config_var('CC') or 'cc'))
    includes = args.python_include or [Path(sysconfig.get_path('include'))]
    library = args.python_lib or Path(sys.base_prefix)/'libs/python3.lib'
    source = root/'solbreaker/native/contact_queries.c'
    name = '_contact_queries.pyd' if windows else '_contact_queries.abi3.so'
    try:
        if sys.version_info < (3, 11) or args.platform not in ('linux', 'win32'):
            raise ValueError('Contact collector supports CPython 3.11+ on Linux/Windows')
        if not any((folder/'Python.h').is_file() for folder in includes):
            raise ValueError('Python development headers are missing')
        if windows and not library.is_file():
            raise ValueError('Windows stable-ABI Python import library is missing')
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.contact-build-', dir=destination) as directory:
            stage = Path(directory)
            artifact = stage/name
            if windows and Path(compiler[0]).stem.lower() in ('cl', 'clang-cl'):
                command = [*compiler, '/nologo', '/LD', '/O2', '/MD', '/fp:strict',
                    *('/I'+str(folder.resolve()) for folder in includes), str(source),
                    '/link', str(library.resolve()), '/OUT:'+str(artifact)]
            else:
                command = [*compiler, '-O2', '-g0', '-shared', '-fno-fast-math', '-ffp-contract=off',
                    *(['-static-libgcc'] if windows else ['-fPIC']),
                    *('-I'+str(folder.resolve()) for folder in includes), str(source),
                    *([str(library.resolve())] if windows else []), '-o', str(artifact)]
            subprocess.run(command, cwd=stage, check=True)
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            os.chmod(artifact, 0o755)
            os.replace(artifact, destination/name)
        print(json.dumps(dict(artifact=str(destination/name), sha256=digest, stable_abi='3.11')))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        if not args.optional:
            raise
        print(f'Contact accelerator unavailable; retaining Python queries: {error}', file=sys.stderr)


if __name__ == '__main__':
    main()
