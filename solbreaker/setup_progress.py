"""Preparation stages shared by command-line output and the launcher."""
import json


def report(message, **progress):
    print(json.dumps(dict(event='setup_progress', message=message, **progress)), flush=True)


def report_copy(completed, total):
    report(f'Копируем ресурсы игры — {completed // (1 << 20)} из {total // (1 << 20)} МиБ',
           completed=completed, total=total)
