"""Create a Windows desktop shortcut through the Unicode shell link interface."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from uuid import UUID

PROJECT = Path(__file__).resolve().parents[1]


def guid(value):
    return (ctypes.c_ubyte * 16).from_buffer_copy(UUID(value).bytes_le)


def create_shortcut(root=PROJECT):
    root = root.resolve(strict=True)
    launcher = root/'launch-gui.vbs'
    icon = root/'solbreaker/assets/solbreaker.ico'
    if not launcher.is_file() or not icon.is_file():
        raise FileNotFoundError('Extract the full Solbreaker archive first.')
    host = Path(os.environ['SystemRoot'])/'System32/wscript.exe'

    ole = ctypes.OleDLL('ole32')
    shell = ctypes.OleDLL('shell32')
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    void = ctypes.c_void_p
    wide = wintypes.LPCWSTR

    def call(interface, slot, result, arguments, *values):
        table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(void))).contents
        method = ctypes.WINFUNCTYPE(result, void, *arguments)(table[slot])
        return method(interface, *values)

    def release(interface):
        if interface:
            call(interface, 2, wintypes.ULONG, ())

    desktop_id = guid('B4BFCC3A-DB2C-424C-B029-7FE99A87C641')
    shell_link = guid('00021401-0000-0000-C000-000000000046')
    shell_link_w = guid('000214F9-0000-0000-C000-000000000046')
    persist_file = guid('0000010B-0000-0000-C000-000000000046')

    # COINIT_APARTMENTTHREADED. An existing STA on the GUI thread is balanced
    # by the same CoUninitialize call as a standalone invocation.
    ole.CoInitializeEx(None, 2)
    link = void()
    file = void()
    desktop = void()
    try:
        shell.SHGetKnownFolderPath(ctypes.byref(desktop_id), 0, None, ctypes.byref(desktop))
        destination = Path(ctypes.wstring_at(desktop))/'Solbreaker.lnk'
        ole.CoCreateInstance(ctypes.byref(shell_link), None, 1,
                             ctypes.byref(shell_link_w), ctypes.byref(link))
        hresult = ctypes.HRESULT
        call(link, 20, hresult, (wide,), str(host))  # IShellLinkW.SetPath
        call(link, 11, hresult, (wide,), f'"{launcher}"')  # SetArguments
        call(link, 9, hresult, (wide,), str(root))  # SetWorkingDirectory
        call(link, 17, hresult, (wide, ctypes.c_int), str(icon), 0)  # SetIconLocation
        call(link, 7, hresult, (wide,), 'Solbreaker')  # SetDescription
        call(link, 0, hresult, (void, ctypes.POINTER(void)), ctypes.byref(persist_file), ctypes.byref(file))
        call(file, 6, hresult, (wide, wintypes.BOOL), str(destination), True)  # IPersistFile.Save
        return destination
    finally:
        release(file)
        release(link)
        if desktop:
            ole.CoTaskMemFree(desktop)
        ole.CoUninitialize()


if __name__ == '__main__':
    create_shortcut()
