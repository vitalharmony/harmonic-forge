#!/usr/bin/env python3
"""Tests for the read-only installed-lane source checker (F645)."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_installed_lane_sources as checker


class InstalledLaneSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "harmonic-forge" / "tools" / "lane"
        self.bin_dir = self.root / "bin"
        self.source.mkdir(parents=True)
        self.bin_dir.mkdir()
        for lane in checker.LANES:
            target = self.source / lane
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            target.chmod(0o755)
            (self.bin_dir / lane).symlink_to(target)
        self.addCleanup(self.temp.cleanup)

    def run_check(self) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = checker.check(self.source, self.bin_dir)
        return code, stderr.getvalue()

    def test_matching_links_pass(self) -> None:
        self.assertEqual(self.run_check(), (0, ""))

    def test_missing_lane_fails(self) -> None:
        (self.bin_dir / "lane2").unlink()
        code, stderr = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("lane2: MISSING", stderr)

    def test_different_source_fails(self) -> None:
        alternate = self.root / "release" / "tools" / "lane"
        alternate.mkdir(parents=True)
        target = alternate / "lane3"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o755)
        (self.bin_dir / "lane3").unlink()
        (self.bin_dir / "lane3").symlink_to(target)
        code, stderr = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("lane3: DRIFT", stderr)

    def test_non_symlink_fails(self) -> None:
        (self.bin_dir / "lane1").unlink()
        (self.bin_dir / "lane1").write_text("#!/bin/sh\n", encoding="utf-8")
        code, stderr = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("lane1: not a symlink", stderr)

    def test_check_never_repairs_drift(self) -> None:
        (self.bin_dir / "lane3").unlink()
        code, _ = self.run_check()
        self.assertEqual(code, 1)
        self.assertFalse((self.bin_dir / "lane3").exists())

    def test_missing_path_command_fails(self) -> None:
        with unittest.mock.patch.object(checker.shutil, "which", return_value=None):
            code = checker.check(self.source)
        self.assertEqual(code, 1)

    def test_lane1_is_default_authority(self) -> None:
        self.assertEqual(checker.check(bin_dir=self.bin_dir), 0)

    def test_skip_if_absent_only_skips_when_all_are_missing(self) -> None:
        for lane in checker.LANES:
            (self.bin_dir / lane).unlink()
        self.assertEqual(checker.check(bin_dir=self.bin_dir, skip_if_absent=True), 0)


if __name__ == "__main__":
    unittest.main()
