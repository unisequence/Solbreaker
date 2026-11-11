"""Verified independent copies of owned game resources."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time

def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def disk_allocation_unit(path):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetDiskFreeSpaceW.argtypes = [wintypes.LPCWSTR, *(ctypes.POINTER(wintypes.DWORD),) * 4]
        kernel.GetDiskFreeSpaceW.restype = wintypes.BOOL
        sectors, size, free, total = (wintypes.DWORD() for _ in range(4))
        root = str(Path(path).resolve().anchor)
        if not kernel.GetDiskFreeSpaceW(root, ctypes.byref(sectors), ctypes.byref(size),
                                        ctypes.byref(free), ctypes.byref(total)):
            raise ctypes.WinError(ctypes.get_last_error())
        return sectors.value * size.value
    return max(512, os.statvfs(path).f_frsize)


def resource_tree_facts(source, *, allocation_unit=4096):
    """Bounded streaming metadata inventory, not a resource-content hash.

    Full-copy plans reject links/reparse/special files. Existing Linux resource
    views keep their old link policy and do not call this inventory.
    """
    if type(allocation_unit) is not int or not 512 <= allocation_unit <= 1 << 26:
        raise ValueError('Invalid resource allocation unit')
    source = Path(source)
    root_info = source.lstat()
    if _link(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError('Resource source must be an ordinary directory')
    fingerprint = hashlib.sha256()
    logical = allocated = files = directories = 0
    for base, dirs, names in os.walk(source, followlinks=False):
        dirs.sort(); names.sort()
        for name in dirs + names:
            path = Path(base) / name
            info = path.lstat()
            if _link(info) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError(f'Resource copy refuses links or special files: {path}')
            if stat.S_ISDIR(info.st_mode):
                directories += 1
                allocated += allocation_unit
            else:
                files += 1
                logical += info.st_size
                allocated += ((info.st_size + allocation_unit - 1) // allocation_unit) * allocation_unit
            # Windows can report a directory's index size as either zero or
            # allocated bytes after traversal. Its entries are recorded below.
            content_size = info.st_size if stat.S_ISREG(info.st_mode) else 0
            record = (path.relative_to(source).as_posix(), stat.S_IFMT(info.st_mode), content_size,
                      info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino)
            fingerprint.update(json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode())
            fingerprint.update(b'\n')
    return dict(bytes=logical, allocated_bytes=allocated + allocation_unit, files=files,
                directories=directories + 1, fingerprint=fingerprint.hexdigest())


def _source_fd(path):
    if os.name == 'nt':
        # Share the native handle-level reparse and identity checks. The low
        # level opener receives a single actual filename, not user path input.
        from .platform_io import _open_windows
        return _open_windows(path.parent, path.name, False, False, False)
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                       dir_fd=directory)
    finally:
        os.close(directory)


def verified_copy_file(source, destination):
    """Copy an independently owned regular file; never follow a source link."""
    source, destination = Path(source).absolute(), Path(destination)
    before = source.lstat()
    if _link(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f'Resource source is not regular: {source}')
    descriptor = _source_fd(source)
    with os.fdopen(descriptor, 'rb') as original:
        opened = os.fstat(original.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(opened.st_mode):
            raise ValueError('Resource source changed before copying')
        digest = hashlib.sha256()
        with destination.open('xb') as copied:
            while chunk := original.read(1 << 20):
                digest.update(chunk)
                copied.write(chunk)
            copied.flush()
            os.fsync(copied.fileno())
        after = os.fstat(original.fileno())
    current = source.lstat()
    # On Windows Python 3.12, lstat and fstat can expose different ctime
    # meanings for the same file. Compare each source of metadata with itself.
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    signature = lambda info: (*identity(info), info.st_ctime_ns)
    if (identity(before) != identity(opened) or signature(before) != signature(current)
            or signature(opened) != signature(after) or identity(after) != identity(current)
            or _link(current)):
        raise ValueError('Resource source changed during copying')
    if sha256(destination) != digest.hexdigest() or os.path.samefile(source, destination):
        raise ValueError('Resource copy does not match its independent source')
    return str(destination)


def copy_resource_tree(source, destination, *, inventory_path=None, progress=None):
    source, destination = Path(source), Path(destination)
    before = resource_tree_facts(source)
    completed = 0
    last_report = 0.
    context = Path(inventory_path).open('x', encoding='utf-8') if inventory_path else nullcontext(None)
    with context as inventory:
        def copy_file(original, copied):
            nonlocal completed, last_report
            result = verified_copy_file(original, copied)
            if inventory is not None:
                entry = dict(path=Path(original).relative_to(source).as_posix(),
                             bytes=Path(copied).stat().st_size, sha256=sha256(Path(copied)))
                inventory.write(json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n')
            if progress is not None:
                completed += Path(copied).stat().st_size
                now = time.monotonic()
                if now-last_report >= 1 or completed == before['bytes']:
                    progress(completed, before['bytes'])
                    last_report = now
            return result
        shutil.copytree(source, destination, symlinks=False, copy_function=copy_file)
        if inventory is not None:
            inventory.flush()
            os.fsync(inventory.fileno())
    if resource_tree_facts(source) != before:
        raise ValueError('Resource tree changed during copying')
    return before


def activate_no_replace(source, destination):
    """Same-volume atomic rename which never replaces an existing target."""
    if sys.platform.startswith('linux'):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renameat2', None)
        if rename is None:
            raise ValueError('Safe activation needs renameat2(RENAME_NOREPLACE)')
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
    elif sys.platform == 'win32':
        os.rename(source, destination)
    else:
        raise ValueError('Safe activation is unavailable for this platform')


