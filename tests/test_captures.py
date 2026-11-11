"""Capture filenames remain unique when the system clock repeats."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from solbreaker.captures import BoundedCapture


class CaptureTests(unittest.TestCase):
    def test_repeated_clock_and_kind_preserve_payloads_and_limit(self):
        with TemporaryDirectory() as temporary, patch('solbreaker.captures.time.time_ns', return_value=123):
            directory = Path(temporary)/'captures'
            capture = BoundedCapture(directory, 3)
            self.assertTrue(capture.write(140, b'first'))
            self.assertTrue(capture.write(140, b'second'))
            restarted = BoundedCapture(directory, 3)
            self.assertTrue(restarted.write(140, b'third'))
            self.assertFalse(restarted.write(140, b'over limit'))
            files = list(directory.glob('123-*-140.bin'))
            self.assertEqual(len(files), 3)
            self.assertEqual({path.read_bytes() for path in files}, {b'first', b'second', b'third'})
