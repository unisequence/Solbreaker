"""Build the native kernels serially from their locked Rust sources."""
import argparse
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--physics-core', action='store_true',
                        help='Compatibility option; the native ground controller is always built')
    args = parser.parse_args()
    if (sys.platform not in ('linux', 'win32') or platform.machine().lower() not in ('amd64', 'x86_64')
            or not 1 <= args.jobs <= 8):
        parser.error('The server build supports Linux/Windows x86-64 and 1..8 build jobs')
    root = Path(__file__).resolve().parents[1]/'solbreaker/native'
    names = dict(sim_core='tfs_sim_core', impact_core='tfs_impact_core',
                 armor_query='tfs_armor_query', rapier_dynamic='tfs_rapier_dynamic')
    names['physics_core'] = 'solbreaker_physics_core'
    for directory, name in names.items():
        crate = root/directory
        subprocess.run(['cargo', 'build', '--locked', '--release', '--jobs', str(args.jobs),
                        '--manifest-path', str(crate/'Cargo.toml')], check=True)
        target = Path(os.environ.get('CARGO_TARGET_DIR', crate/'target')).resolve()
        filename = f'{name}.dll' if sys.platform == 'win32' else f'lib{name}.so'
        built = target/'release'/filename
        destination = crate/(filename if directory in ('rapier_dynamic', 'physics_core') or sys.platform == 'win32'
                             else f'{name}.so')
        descriptor, temporary = tempfile.mkstemp(prefix='.native-', dir=crate)
        os.close(descriptor)
        try:
            shutil.copyfile(built, temporary)
            os.chmod(temporary, 0o755)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        print(destination, flush=True)
    subprocess.run([sys.executable, Path(__file__).with_name('build_contact_queries.py'),
                    '--optional'], check=True)


if __name__ == '__main__':
    main()
