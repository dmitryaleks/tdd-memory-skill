"""Runs the end-to-end walk as a subprocess and fails if any check fails.

The walk itself lives in fixtures/e2e.py so a human can run it and read the
transcript; this makes it part of the suite as well.
"""

import subprocess
import sys
import unittest

from _harness import ROOT


class TestEndToEnd(unittest.TestCase):

    def test_the_whole_loop_walks(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "fixtures" / "e2e.py")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", timeout=600)
        tail = "\n".join(proc.stdout.splitlines()[-25:])
        self.assertEqual(proc.returncode, 0, tail)
        self.assertIn("0 failed", tail)


if __name__ == "__main__":
    unittest.main()
