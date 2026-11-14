"""Assemble the private Windows runtime from pinned upstream archives and wheels."""
import hashlib
import io
import json
import re
from pathlib import Path, PurePosixPath
import tarfile
import zipfile

RUNTIME = {
    'version': '3.12.16',
    'filename': 'cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only.tar.gz',
    'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-x86_64-pc-windows-msvc-install_only.tar.gz',
    'sha256': '590dfcbe3a34c07301e6332e13e2188b98655cd072b08afa45a7998dc4c22bc5',
}


def safe_name(name):
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or name != path.as_posix()
            or '..' in path.parts or '\\' in name or ':' in name
            or any(part.endswith(('.', ' ')) for part in path.parts)):
        raise ValueError('Unsafe runtime archive path: '+name)
    return path


def compatible_wheel(name):
    if name.endswith('-none-any.whl'):
        return True
    match = re.search(r'-cp(\d+)-(cp\d+|abi3)-win_amd64.whl$', name)
    return bool(match and ((match[1] == '312' and match[2] == 'cp312')
                           or (match[2] == 'abi3' and int(match[1]) <= 312)))


def records(lock, *, server_only=False):
    groups = ('bootstrap', 'base', 'numpy-legacy', 'tui')
    if not server_only:
        groups += ('gui',)
    return sum([lock['groups'][group] for group in groups], []) + [lock['native_windows_physics']]


def payloads(directory, source, read_input, *, server_only=False):
    """Only exact hash-locked archives become runtime files. Never copy a live environment."""
    directory = Path(directory)
    archive = read_input(directory/RUNTIME['filename'])
    if hashlib.sha256(archive).hexdigest() != RUNTIME['sha256']:
        raise ValueError('Portable CPython differs from the pinned SHA-256')
    result = {}
    folded = set()

    def add(name, content):
        safe_name(name)
        if name.casefold() in folded:
            raise ValueError('Duplicate runtime path: '+name)
        if len(content) > 64 << 20 or sum(map(len, result.values())) + len(content) > 900 << 20:
            raise ValueError('Runtime exceeds the package size limit')
        folded.add(name.casefold())
        result[name] = content

    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as stream:
        for member in stream:
            path = safe_name(member.name.rstrip('/'))
            if path.parts[0] != 'python':
                raise ValueError('Unexpected CPython archive root')
            if member.isdir():
                continue
            if not member.isfile() or member.size > 64 << 20:
                raise ValueError('Expected a bounded regular CPython file')
            # Replace upstream packaging tools with our locked wheels. Debug
            # symbols and compiler inputs are unnecessary at runtime.
            if ('site-packages' in path.parts or '__pycache__' in path.parts
                    or path.suffix in ('.pdb', '.lib', '.pyc') or path.parts[1:2] == ('include',)):
                continue
            add('runtime/'+path.as_posix(), stream.extractfile(member).read())
    lock = json.loads(source['requirements/lock.json'])
    installed = {}
    wheel_inputs = []
    for record in records(lock, server_only=server_only):
        candidates = [directory/item['filename'] for item in record['files']
                      if compatible_wheel(item['filename']) and (directory/item['filename']).is_file()]
        if len(candidates) != 1:
            raise ValueError('Provide exactly one locked Windows wheel for '+record['name'])
        wheel = candidates[0]
        content = read_input(wheel, 160 << 20)
        digest = hashlib.sha256(content).hexdigest()
        if not any(item['filename'] == wheel.name and item['sha256'] == digest for item in record['files']):
            raise ValueError('Wheel differs from locked SHA-256: '+wheel.name)
        wheel_inputs.append(dict(filename=wheel.name, sha256=digest))
        installed[record['name']] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        with zipfile.ZipFile(io.BytesIO(content)) as stream:
            for member in stream.infolist():
                path = safe_name(member.filename.rstrip('/'))
                if member.is_dir():
                    continue
                if member.file_size > 64 << 20 or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Unsafe wheel member: '+member.filename)
                parts = path.parts
                if parts[0].endswith('.data'):
                    if len(parts) < 3 or parts[1] not in ('purelib', 'platlib'):
                        raise ValueError('Unsupported wheel installation scheme: '+member.filename)
                    parts = parts[2:]
                add('runtime/python/Lib/site-packages/'+'/'.join(parts), stream.read(member))
    # CPython resolves these paths relative to its DLL, including after moving
    # the distribution. _pth also excludes registry, PYTHONPATH and user-site.
    add('runtime/python/python312._pth', b'Lib\nDLLs\nLib\\site-packages\n..\\..\nimport site\n')
    receipt = dict(schema=1, runtime=RUNTIME, wheels=wheel_inputs, packages=installed,
                   project_sha256=hashlib.sha256(source['pyproject.toml']).hexdigest())
    add('runtime/python/solbreaker-bundle.json', (json.dumps(receipt, indent=2)+'\n').encode())
    return result
