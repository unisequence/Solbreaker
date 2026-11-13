"""Check Git blobs against the source allowlist and original-client catalog."""
import argparse
import gzip
import hashlib
import json
import subprocess
import sys

from package_source import GAME_CATALOG, SECRET, source_name


def git(*args):
    return subprocess.check_output(('git', *args))


def audit(revision, base):
    # Also use the base catalog so a change to the catalog cannot hide a match.
    catalog_versions = {revision, base} - {None}
    game_hashes = {
        row[2] for rev in catalog_versions
        for row in json.loads(gzip.decompress(git('show', f'{rev}:{GAME_CATALOG}')))
    }
    revisions = [revision]
    if base:
        revisions.extend(git('rev-list', f'{base}..{revision}').decode().splitlines())
    checked = set()
    for rev in dict.fromkeys(revisions):
        allowlist = git('show', f'{rev}:release-files.txt').decode().splitlines()
        allowed = {source_name(line) for line in allowlist if line and not line.startswith('#')}
        allowed.add('AGENTS.md')
        for entry in git('ls-tree', '-rz', rev).split(b'\0'):
            if not entry:
                continue
            info, path = entry.split(b'\t', 1)
            mode, kind, oid = info.decode().split()
            name = path.decode('utf-8')
            if name not in allowed or kind != 'blob' or mode not in ('100644', '100755'):
                raise ValueError(f'Unapproved Git entry in {rev}: {name!r}')
            if oid in checked:
                continue
            size = int(git('cat-file', '-s', oid))
            if size > 64 << 20:
                raise ValueError(f'Oversized Git blob: {name!r}')
            content = git('cat-file', 'blob', oid)
            if hashlib.sha256(content).hexdigest() in game_hashes:
                raise ValueError(f'Original game file in Git: {name!r}')
            if SECRET.search(content):
                raise ValueError(f'Possible secret in Git: {name!r}')
            checked.add(oid)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--base')
    args = parser.parse_args()
    try:
        audit(args.revision, args.base)
    except (ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
