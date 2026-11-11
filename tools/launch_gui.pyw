"""Windowless Windows entry point with a persistent startup error log."""
import ctypes
from pathlib import Path
import sys
import traceback


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    log = root/'runtime/launcher-gui.log'
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8', buffering=1) as stream:
            previous = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = stream
            try:
                from solbreaker.desktop import run
                run(Path(sys.argv[1]))
            except Exception:
                traceback.print_exc()
                raise
            finally:
                sys.stdout, sys.stderr = previous
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, f'{error}\n\nLog: {log}', 'Solbreaker', 0x10)
        sys.exit(1)
