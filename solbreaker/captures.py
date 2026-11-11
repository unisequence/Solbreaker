"""Bounded diagnostic captures; no directory scans in the packet hot path."""
from itertools import islice
import os
from pathlib import Path
import tempfile
import time


class BoundedCapture:
    def __init__(self, directory, limit):
        if type(limit) is not int or limit<0:
            raise ValueError('Capture limit must be nonnegative')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.count = sum(1 for _ in islice(self.directory.iterdir(),limit))
        self.limit = limit

    def write(self, kind, payload):
        if self.count>=self.limit:
            return False
        fd, _ = tempfile.mkstemp(prefix=f'{time.time_ns()}-', suffix=f'-{kind}.bin',
                                 dir=self.directory)
        with os.fdopen(fd,'wb') as output:
            output.write(payload)
        self.count += 1
        return True
