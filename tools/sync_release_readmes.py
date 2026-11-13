"""Update the download link in both READMEs using paginated GitHub releases."""
import json
import os
from pathlib import Path
import re
import sys

TAG = re.compile(r'v(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?', re.ASCII)
STAGE = {'alpha': 0, 'beta': 1, 'rc': 2, None: 3}
DOWNLOAD = re.compile(r'(?<=<!-- release-download:start -->\n)[^\n]+(?=\n<!-- release-download:end -->)')
READMES = {'README.md': 'Download', 'README.ru.md': 'Скачать'}


def latest_release(pages):
    candidates = []
    for page in pages:
        for release in page:
            tag = release['tag_name']
            match = TAG.fullmatch(tag)
            if release['draft'] or not match:
                continue
            version = tag[1:]
            required = {'SHA256SUMS', f'Solbreaker-{version}-source.zip',
                        f'Solbreaker-{version}-windows-x64.zip'}
            uploaded = {asset['name'] for asset in release['assets'] if asset['state'] == 'uploaded'}
            if required <= uploaded:
                major, minor, patch, stage, build = match.groups()
                order = (int(major), int(minor), int(patch), STAGE[stage], int(build or 0))
                candidates.append((order, tag))
    return max(candidates, default=((), None))[1]


def update_readmes(root, repository, tag):
    link = f'https://github.com/{repository}/releases/tag/{tag}'
    changes = {}
    for name, label in READMES.items():
        path = root / name
        content = path.read_text(encoding='utf-8')
        updated, count = DOWNLOAD.subn(f'[{label} {tag[1:]}]({link})', content)
        if count != 1:
            raise ValueError(f'{name} must have exactly one release download link')
        if updated != content:
            changes[path] = updated
    for path, updated in changes.items():
        path.write_text(updated, encoding='utf-8')


if __name__ == '__main__':
    tag = latest_release(json.load(sys.stdin))
    if tag:
        update_readmes(Path.cwd(), os.environ['GH_REPO'], tag)
