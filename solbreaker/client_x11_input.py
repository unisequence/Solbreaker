"""Attach the X11 input workarounds only to a prepared Proton client."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

from .connection import atomic_write


SOURCE = Path(__file__).with_name('native') / 'x11_repeat_filter.c'
LIBRARY = 'solbreaker-input.so'
ARCHITECTURES = (
    (32, Path('/lib/ld-linux.so.2'), 'lib/i386-linux-gnu'),
    (64, Path('/lib64/ld-linux-x86-64.so.2'), 'lib/x86_64-linux-gnu'),
)


class FilterUnavailable(ValueError):
    pass


def _loader_directory(loader, fallback):
    if not loader.is_file():
        return fallback
    result = subprocess.run([str(loader), '--list-diagnostics'], capture_output=True, text=True, timeout=5)
    match = re.search(r'^dl_dst_lib="([a-zA-Z0-9_/-]+)"$', result.stdout, re.MULTILINE)
    if match and all(part not in ('', '..') for part in match[1].split('/')):
        return match[1]
    return fallback


def _libraries(cache):
    directories = [_loader_directory(loader, fallback) for _, loader, fallback in ARCHITECTURES]
    if len(set(directories)) != len(directories):
        raise FilterUnavailable('Обе архитектуры Proton используют один путь для LD_PRELOAD')
    for (bits, _, _), directory in zip(ARCHITECTURES, directories):
        target = cache / directory / LIBRARY
        stamp = target.with_suffix('.sha256')
        if (target.is_file() and stamp.is_file()
                and hashlib.sha256(target.read_bytes()).hexdigest() == stamp.read_text().strip()):
            continue
        compiler = shutil.which('gcc')
        if compiler is None:
            raise FilterUnavailable('Для фильтра нужны GCC с поддержкой 32 бит и заголовки X11')
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.build-', dir=target.parent) as temporary:
            output = Path(temporary) / LIBRARY
            result = subprocess.run(
                [compiler, f'-m{bits}', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                 str(SOURCE), '-ldl', '-pthread', '-o', str(output)],
                capture_output=True, text=True, timeout=45)
            if result.returncode:
                raise FilterUnavailable(f'Не удалось собрать фильтр X11 ({bits} бит): '
                                        + result.stderr.strip()[-1200:])
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            os.replace(output, target)
            atomic_write(stamp, (digest + '\n').encode())


@contextmanager
def preload(environment, lab, executable):
    if (sys.platform != 'linux' or platform.machine() != 'x86_64' or not environment.get('DISPLAY')
            or environment.get('SOLBREAKER_X11_REPEAT_FILTER', '1') != '1'):
        yield environment
        return

    try:
        revision = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:20]
        cache = lab / 'runtime' / 'input-filter' / revision
        _libraries(cache)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Solbreaker: фильтр автоповтора недоступен. {error}', file=sys.stderr, flush=True)
        yield environment
        return

    # ld.so splits LD_PRELOAD on spaces and colons. Keep the alias alive for
    # the client and all processes started by its Proton launcher.
    with tempfile.TemporaryDirectory(prefix='solbreaker-input-', dir='/tmp') as temporary:
        alias = Path(temporary) / 'cache'
        alias.symlink_to(cache, target_is_directory=True)
        entry = str(alias / '$LIB' / LIBRARY)
        yield dict(environment, SOLBREAKER_X11_REPEAT_TARGET=str(executable),
                   LD_PRELOAD=':'.join(filter(None, (entry, environment.get('LD_PRELOAD')))))
