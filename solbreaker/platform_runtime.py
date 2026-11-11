"""Read-only process/Steam discovery; no game launch or profile mutation.

Windows uses lazy Toolhelp/limited-query handles. Native Windows acceptance is
still required. A Steam directory proves neither ownership nor authentication.
"""
import ntpath
import os
from pathlib import Path
import sys


MAX_COMMAND_BYTES = 65536


class ProcessInspectionError(ValueError):
    """Potentially matching processes could not be safely ruled out."""


def _platform():
    return sys.platform


def _gone(error):
    return isinstance(error, (FileNotFoundError, ProcessLookupError))


def _read_bounded(path, limit):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ProcessInspectionError(f'Process metadata exceeds its limit: {path.name}')
    return raw


def _linux_processes(executable, proc_root):
    proc_root = Path(proc_root)
    if not proc_root.is_dir():
        raise ProcessInspectionError('Cannot inspect client processes: /proc is unavailable')
    expected = str(executable)
    potential_names = {executable.name.casefold()[:15], 'wine', 'wine64',
                       'wine-preloader', 'wine64-preloade', 'wineserver'}
    result = []
    try:
        processes = proc_root.iterdir()
        for process in processes:
            if not process.name.isdecimal():
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue  # A private lab is accessible only to its owner.
                try:
                    comm = os.fsdecode(_read_bounded(process / 'comm', 256)).strip().casefold()
                except OSError:
                    comm = None
                try:
                    raw = _read_bounded(process / 'cmdline', MAX_COMMAND_BYTES)
                except OSError as exc:
                    if _gone(exc):
                        continue
                    if comm is None or comm in potential_names:
                        raise ProcessInspectionError(f'Cannot inspect potentially matching client PID {process.name}') from exc
                    continue
                first = os.fsdecode(raw.split(b'\0', 1)[0])
                matched = first == expected or first[:2].lower() == 'z:' and first[2:].replace('\\', '/') == expected
                try:
                    image = os.readlink(process / 'exe')
                except OSError as exc:
                    if _gone(exc):
                        # A genuine Wine process has a host image; synthetic
                        # cmdline-only fixtures can still prove an exact argv.
                        if not matched and not raw:
                            continue
                        image = None
                    elif matched:
                        image = None
                    elif comm is None or comm in potential_names:
                        raise ProcessInspectionError(f'Cannot inspect client image for PID {process.name}') from exc
                    else:
                        image = None
                if image is not None:
                    matched = matched or image.removesuffix(' (deleted)') == expected
                if matched:
                    result.append(dict(pid=int(process.name), executable=expected))
                elif first and Path(first).name.casefold() == executable.name.casefold() and not Path(first).is_absolute():
                    # Relative argv under Wine cannot prove which executable
                    # was loaded. Do not announce an idle lab from this case.
                    raise ProcessInspectionError(f'Client PID {process.name} has an ambiguous relative executable')
            except OSError as exc:
                if not _gone(exc):
                    raise ProcessInspectionError(f'Cannot inspect client PID {process.name}') from exc
    except OSError as exc:
        raise ProcessInspectionError('Cannot enumerate client processes') from exc
    return sorted(result, key=lambda entry: entry['pid'])


def _windows_path(path):
    value = str(path)
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


class _WindowsProcesses:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        class Entry(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                        ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                        ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                        ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                        ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
        self.Entry = Entry
        specs = {
            'CreateToolhelp32Snapshot': ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            'Process32FirstW': ([wintypes.HANDLE, ctypes.POINTER(Entry)], wintypes.BOOL),
            'Process32NextW': ([wintypes.HANDLE, ctypes.POINTER(Entry)], wintypes.BOOL),
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'GetExitCodeProcess': ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in specs.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    def entries(self):
        c = self.ctypes
        handle = self.kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            entry = self.Entry()
            entry.dwSize = c.sizeof(entry)
            ready = self.kernel.Process32FirstW(handle, c.byref(entry))
            while ready:
                yield entry.th32ProcessID, entry.szExeFile
                entry.dwSize = c.sizeof(entry)
                ready = self.kernel.Process32NextW(handle, c.byref(entry))
            code = c.get_last_error()
            if code != 18:  # ERROR_NO_MORE_FILES, not a generic failure.
                raise c.WinError(code)
        finally:
            self.kernel.CloseHandle(handle)

    def image(self, pid):
        from ctypes import wintypes
        c = self.ctypes
        handle = self.kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            code = c.get_last_error()
            if code == 87:  # Snapshot entry exited before OpenProcess.
                return None
            raise c.WinError(code)
        try:
            buffer = c.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, c.byref(length)):
                code = c.get_last_error()
                exit_code = wintypes.DWORD()
                if self.kernel.GetExitCodeProcess(handle, c.byref(exit_code)) and exit_code.value != 259:
                    return None
                raise c.WinError(code)
            return buffer.value
        finally:
            self.kernel.CloseHandle(handle)


def _windows_process_api():
    return _WindowsProcesses()


def _same_windows_image(image, executable):
    if _windows_path(image) == _windows_path(executable):
        return True
    if os.name == 'nt':
        # File identity also handles 8.3 spellings and other canonical aliases.
        # Inaccessible matching-name files must not establish an idle lab.
        try:
            return os.path.samefile(image, executable)
        except OSError as exc:
            raise ProcessInspectionError('Cannot compare a potentially matching client image') from exc
    return False  # Mocked contract tests on Linux do not emulate NT file IDs.


def _windows_processes(executable):
    try:
        api = _windows_process_api()
        entries = api.entries()
    except OSError as exc:
        raise ProcessInspectionError('Cannot create Windows process snapshot') from exc
    expected = _windows_path(executable)
    expected_name = ntpath.basename(expected)
    result = []
    try:
        for pid, name in entries:
            if ntpath.basename(name).casefold() != expected_name:
                continue
            try:
                image = api.image(pid)
            except OSError as exc:
                raise ProcessInspectionError(f'Cannot inspect potentially matching Windows client PID {pid}') from exc
            if image is not None and _same_windows_image(image, executable):
                result.append(dict(pid=pid, executable=str(executable)))
    except OSError as exc:
        raise ProcessInspectionError('Cannot enumerate Windows client processes') from exc
    finally:
        close = getattr(entries, 'close', None)
        if close:
            close()
    return sorted(result, key=lambda entry: entry['pid'])


def find_executable_processes(executable, *, proc_root=None):
    """Exact image/argv matches only; ambiguous/inaccessible candidates refuse.

    Linux includes exact Wine Z: argv and current-user processes. A process
    snapshot is not a session lock; callers must acquire the shared lab lock
    before offline mutation and keep it until the operation finishes.
    """
    executable = Path(executable).expanduser()
    platform = _platform()
    if platform.startswith('linux'):
        if not executable.is_absolute():
            raise ValueError('Expected an absolute executable path')
        return _linux_processes(executable.resolve(), proc_root or Path('/proc'))
    if platform == 'win32':
        if not ntpath.isabs(str(executable)) or not ntpath.splitdrive(str(executable))[0]:
            raise ValueError('Expected an absolute executable path')
        if proc_root is not None:
            raise ValueError('proc_root is a Linux-only inspection option')
        if os.name == 'nt':
            executable = executable.resolve(strict=True)
        return _windows_processes(executable)
    raise ProcessInspectionError('Process inspection is unavailable on this platform')


def require_executable_idle(executable, *, proc_root=None):
    matches = find_executable_processes(executable, proc_root=proc_root)
    if matches:
        pids = ', '.join(str(entry['pid']) for entry in matches)
        raise ProcessInspectionError(f'Close this client before changing its files (PID {pids})')


def _registry_steam_paths():
    import winreg
    paths = []
    views = (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            try:
                with winreg.OpenKey(hive, r'Software\Valve\Steam', 0, winreg.KEY_READ | view) as key:
                    for name in ('SteamPath', 'InstallPath', 'SteamExe'):
                        try:
                            value, kind = winreg.QueryValueEx(key, name)
                            if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                                path = os.path.expandvars(value)
                                paths.append(ntpath.dirname(path) if name == 'SteamExe' else path)
                        except OSError:
                            pass
            except OSError:
                pass  # Read-only discovery can still use standard/manual paths.
    return paths


def steam_installations():
    """Existing Steam client directories only; no process, network or writes."""
    platform = _platform()
    if platform.startswith('linux'):
        candidates = [Path.home() / '.local/share/Steam', Path.home() / '.steam/steam', Path.home() / '.steam/root']
        data = os.environ.get('XDG_DATA_HOME')
        if data and Path(data).is_absolute():
            candidates.insert(0, Path(data) / 'Steam')
        names = ('steam.sh', 'steam', 'ubuntu12_32/steam')
    elif platform == 'win32':
        candidates = [Path(value) for value in _registry_steam_paths() if value]
        for name in ('ProgramFiles(x86)', 'ProgramFiles'):
            value = os.environ.get(name)
            if value and Path(value).is_absolute():
                candidates.append(Path(value) / 'Steam')
        names = ('steam.exe',)
    else:
        raise ValueError('Steam discovery is unavailable on this platform')
    result, seen = [], set()
    for path in candidates:
        if not path.is_absolute() or not path.is_dir() or not any((path / name).is_file() for name in names):
            continue
        resolved = path.resolve()
        key = _windows_path(resolved) if platform == 'win32' else str(resolved)
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


