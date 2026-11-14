"""List and verify the pinned Steam files used by a dedicated server."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path


CATALOG = (Path(__file__).resolve().parents[1] / 'solbreaker' / 'assets'
           / 'build-11.21.0.1235_7376187-files.json.gz')
ROOT_FILES = {'wotblitz.exe', 'Data/version.txt.dvpl', 'Data/maps.yaml.dvpl',
              'Data/regions_publish.yaml.dvpl'}
MAP_SUFFIXES = ('.sc2.dvpl', '.scg.dvpl', '.heightmap.dvpl',
                '.lka.dvpl', '.mkm.dvpl', '.yaml.dvpl')
TANK_SUFFIXES = ('.sc2.dvpl', '.scg.dvpl', '.yaml.dvpl')


def server_files():
    # Keep all geometry in these trees so hidden maps, alternate layouts
    # and vehicle configurations can still resolve their assets.
    catalog = json.loads(gzip.decompress(CATALOG.read_bytes()))
    files = [(name, size, checksum) for name, size, checksum in catalog
             if name in ROOT_FILES or name.startswith('Data/XML/item_defs/')
             or (name.startswith('Data/3d/Maps/') and name.endswith(MAP_SUFFIXES))
             or (name.startswith('Data/3d/Tanks/') and name.endswith(TANK_SUFFIXES))]
    if not ROOT_FILES.issubset(name for name, _, _ in files):
        raise ValueError('The pinned catalog is missing required server files')
    return files


def verify(game, files):
    for name, size, expected in files:
        path = game / name
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError(f'Missing or incomplete server resource: {name}')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != expected:
            raise ValueError(f'SHA-256 mismatch: {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='command', required=True)
    listing = actions.add_parser('filelist', help='Generate the file list for DepotDownloader')
    listing.add_argument('--output', type=Path, help='Write the list as UTF-8 to this file')
    check = actions.add_parser('verify', help='Verify the downloaded files against the pinned catalog')
    check.add_argument('game', type=Path)
    args = parser.parse_args()
    try:
        files = server_files()
        if args.command == 'filelist':
            content = '\n'.join(name for name, _, _ in files) + '\n'
            if args.output:
                args.output.write_text(content, encoding='utf-8', newline='\n')
            else:
                print(content, end='')
        else:
            verify(args.game, files)
            print(f'Verified {len(files):,} files ({sum(size for _, size, _ in files):,} bytes)')
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
