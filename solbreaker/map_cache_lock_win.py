"""Shared and exclusive Win32 locks for cache leases."""
import ctypes as C
from ctypes import wintypes as W
import msvcrt


class Overlapped(C.Structure):
    _fields_ = [('internal', C.c_size_t), ('internal_high', C.c_size_t),
                ('offset', W.DWORD), ('offset_high', W.DWORD), ('event', W.HANDLE)]


_lock = C.WinDLL('kernel32', use_last_error=True).LockFileEx
_lock.argtypes = (W.HANDLE, W.DWORD, W.DWORD, W.DWORD, W.DWORD, C.POINTER(Overlapped))
_lock.restype = W.BOOL


def try_lock(stream, shared):
    flags = 1 | (0 if shared else 2)  # FAIL_IMMEDIATELY, EXCLUSIVE_LOCK
    if _lock(msvcrt.get_osfhandle(stream.fileno()), flags, 0, 1, 0, C.byref(Overlapped())):
        return True
    error = C.get_last_error()
    if error in (33, 997):  # LOCK_VIOLATION, IO_PENDING
        return False
    raise C.WinError(error)
