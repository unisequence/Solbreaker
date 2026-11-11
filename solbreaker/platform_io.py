"""Guarded private-file IO and locks, with lazily loaded native backends.

Windows handle and locking branches require native Windows acceptance. Linux
never imports ctypes Windows bindings or msvcrt. Callers own returned file fds.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import re
import stat
import sys


LOCK_NAMES = frozenset(('server.lock', 'client-config.lock', 'dependencies.lock'))


class PrivateFileBusy(ValueError):
    pass


def _platform():
    return sys.platform


def _name(filename):
    if (not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', filename)
            or filename.endswith('.')
            or filename.split('.', 1)[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}):
        raise ValueError('Expected a plain private filename')
    return filename


def _same(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _directory_fd(private):
    # Walk without resolving links. A pinned dirfd for every step avoids the
    # check-then-follow race, including ancestors of the private directory.
    private = Path(os.path.abspath(os.path.expanduser(os.fspath(private))))
    descriptor = os.open(private.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in private.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if os.fstat(descriptor).st_uid != os.getuid():
            raise ValueError('Private directory belongs to another user')
        result, descriptor = descriptor, None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_linux(private, filename, create, writable, exclusive):
    directory = _directory_fd(private)
    descriptor = None
    try:
        try:
            before = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
            raise ValueError('Private file must be a regular file with one link')
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT
        if exclusive:
            flags |= os.O_EXCL
        descriptor = os.open(filename, flags, 0o600, dir_fd=directory)
        opened = os.fstat(descriptor)
        current = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_uid != os.getuid() or not _same(opened, current)
                or (before is not None and not _same(before, opened))):
            raise ValueError('Private file changed during validation')
        result, descriptor = descriptor, None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


# Win32 constants are data, not imports. OPEN_REPARSE_POINT prevents a leaf
# junction/symlink from redirecting CreateFileW. No FILE_SHARE_DELETE keeps each
# validated directory and the file from being renamed while its handle is held.
_READ = 0x80000000
_WRITE = 0x40000000
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_CREATE_NEW = 1
_OPEN_REPARSE = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_DIRECTORY = 0x10
_REPARSE = 0x400


class _WindowsAPI:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        class Information(ctypes.Structure):
            _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                        ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                        ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                        ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                        ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]
        self.Information = Information
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Information)]
        self.kernel.GetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel.GetFileType.argtypes = [wintypes.HANDLE]
        self.kernel.GetFileType.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def open(self, path, access, disposition, flags):
        handle = self.kernel.CreateFileW(str(path), access, 3, None, disposition, flags, None)
        if handle == self.ctypes.c_void_p(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return handle

    def information(self, handle):
        info = self.Information()
        if not self.kernel.GetFileInformationByHandle(handle, self.ctypes.byref(info)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return dict(attributes=info.attributes, links=info.links,
                    identity=(info.volume, info.index_high, info.index_low),
                    disk=self.kernel.GetFileType(handle) == 1)

    def close(self, handle):
        self.kernel.CloseHandle(handle)

    def descriptor(self, handle, writable):
        import msvcrt
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_BINARY | os.O_NOINHERIT
        return msvcrt.open_osfhandle(handle, flags)


def _windows_api():
    return _WindowsAPI()


@contextmanager
def hold_windows_directory(path):
    """Pin existing non-reparse ancestors across a pathname-based replacement.

    CreateFileW omits FILE_SHARE_DELETE, so Windows cannot rename/swap any
    validated ancestor until the caller completes its atomic file operation.
    Native Windows acceptance is separate from the mocked API contract tests.
    """
    if _platform() != 'win32':
        raise ValueError('Windows directory guard requires Windows')
    api = _windows_api()
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    held = []
    try:
        current = Path(path.anchor)
        for part in ('', *path.parts[1:]):
            if part:
                current /= part
            handle = api.open(current, 0, _OPEN_EXISTING, _OPEN_REPARSE | _BACKUP_SEMANTICS)
            held.append(handle)
            info = api.information(handle)
            if info['attributes'] & _REPARSE or not info['attributes'] & _DIRECTORY or not info['disk']:
                raise ValueError('Directory must be regular, without reparse points')
        yield path
    finally:
        for handle in reversed(held):
            api.close(handle)


def _open_windows(private, filename, create, writable, exclusive, *, create_directories=True):
    api = _windows_api()
    private = Path(os.path.abspath(os.path.expanduser(os.fspath(private))))
    held = []
    handle = None
    try:
        current = Path(private.anchor)
        for part in ('', *private.parts[1:]):
            if part:
                current /= part
            try:
                directory = api.open(current, 0, _OPEN_EXISTING, _OPEN_REPARSE | _BACKUP_SEMANTICS)
            except FileNotFoundError:
                if not create_directories:
                    raise
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                directory = api.open(current, 0, _OPEN_EXISTING, _OPEN_REPARSE | _BACKUP_SEMANTICS)
            held.append(directory)
            info = api.information(directory)
            if info['attributes'] & _REPARSE or not info['attributes'] & _DIRECTORY or not info['disk']:
                raise ValueError('Private directory must not be a reparse point')
        path = private / filename
        disposition = _CREATE_NEW if exclusive else _OPEN_ALWAYS if create else _OPEN_EXISTING
        handle = api.open(path, _READ | (_WRITE if writable else 0), disposition, _OPEN_REPARSE)
        info = api.information(handle)
        if info['attributes'] & (_REPARSE | _DIRECTORY) or not info['disk'] or info['links'] != 1:
            raise ValueError('Private file must be regular, without reparse points or hardlinks')
        # Re-open the same path while ancestors and file cannot be renamed.
        check = api.open(path, _READ, _OPEN_EXISTING, _OPEN_REPARSE)
        try:
            observed = api.information(check)
            if observed['identity'] != info['identity'] or observed['attributes'] & (_REPARSE | _DIRECTORY):
                raise ValueError('Private file changed during validation')
        finally:
            api.close(check)
        descriptor = api.descriptor(handle, writable)
        handle = None  # The CRT descriptor now owns the handle.
        return descriptor
    finally:
        if handle is not None:
            api.close(handle)
        for directory in reversed(held):
            api.close(directory)


def open_private_file(private, filename, *, create=False, writable=False, exclusive=False, truncate=False):
    """Return a noninheritable fd; validate before any truncate or payload IO.

    Parent directories are made with owner-only POSIX mode. Windows inherits
    its parent ACL; this function does not claim to replace Windows ACL policy.
    """
    filename = _name(filename)
    if any(type(value) is not bool for value in (create, writable, exclusive, truncate)):
        raise ValueError('Private IO options must be boolean')
    if exclusive and not create or truncate and not writable:
        raise ValueError('Exclusive needs create; truncate needs writable')
    platform = _platform()
    if platform.startswith('linux'):
        descriptor = _open_linux(private, filename, create, writable, exclusive)
    elif platform == 'win32':
        descriptor = _open_windows(private, filename, create, writable, exclusive)
    else:
        raise ValueError('Private IO adapter is unavailable for this platform')
    try:
        os.set_inheritable(descriptor, False)
        if truncate:
            os.ftruncate(descriptor, 0)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_regular_file(path, maximum):
    """Bounded public-file read, without creating paths or following links.

    Unlike private lock names, actual public filenames may contain Unicode and
    spaces. Neither POSIX ownership nor a lock-name regex applies to sources.
    """
    if type(maximum) is not int or not 0 <= maximum <= 64 << 20:
        raise ValueError('Expected a bounded public-file byte limit (0..64 MiB)')
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    platform = _platform()
    if platform == 'win32':
        descriptor = _open_windows(path.parent, path.name, False, False, False,
                                   create_directories=False)
    elif platform.startswith('linux'):
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                observed = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not _same(info, observed):
                    raise ValueError('Expected a regular public file with one link')
            except BaseException:
                os.close(descriptor)
                raise
        finally:
            os.close(directory)
    else:
        raise ValueError('Public-file reader is unavailable on this platform')
    with os.fdopen(descriptor, 'rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('Public configuration file exceeds its byte limit')
    return raw


@contextmanager
def private_lock(private, filename):
    """Nonblocking private lock; keeps the inode opened for the whole scope."""
    if filename not in LOCK_NAMES:
        raise ValueError('Unknown private lock name')
    descriptor = open_private_file(private, filename, create=True, writable=True)
    try:
        if _platform().startswith('linux'):
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PrivateFileBusy('Private file is locked by another process') from None
        else:
            import msvcrt
            # Locking beyond EOF is supported by the Windows byte-range lock;
            # do not initialise/truncate bytes before acquiring the lock.
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                raise PrivateFileBusy('Private file is locked by another process') from None
        # A contender must not silently own an unlinked old lock inode after
        # replacement between open validation and acquiring the OS lock.
        check = open_private_file(private, filename)
        try:
            if not _same(os.fstat(descriptor), os.fstat(check)):
                raise ValueError('Private lock path changed while acquiring ownership')
        finally:
            os.close(check)
        # Closing the fd releases both flock and the Windows byte-range lock.
        yield
    finally:
        os.close(descriptor)
