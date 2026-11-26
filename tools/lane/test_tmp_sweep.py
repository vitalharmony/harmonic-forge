"""harmonic-forge#1600 AC2/AC3: the stale temp-dir sweeper removes only the
current user's top-level stale `tmp` + 8-char directories, and is silent when
it removes nothing."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmp_sweep  # noqa: E402

OLD = 3 * 60 * 60


class SweepTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="tmp-sweep-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "realtmp"
        self.root.mkdir()
        self.now = time.time()

    def entry(self, name: str, *, age: int = OLD, parent: Path | None = None) -> Path:
        path = (parent or self.root) / name
        path.mkdir()
        (path / "state.json").write_text("{}")
        os.utime(path, (self.now - age, self.now - age))
        return path

    def test_table(self) -> None:
        stale = self.entry("tmpab_9cdef")
        kept = [self.entry("tmpfresh0_1", age=60),           # modified under 2 hours ago
                self.entry("tmp1234567"),                    # 7 chars
                self.entry("tmpabcdefghi"),                  # 9 chars
                self.entry("tmp-abcdefgh"),                  # `tmp-` prefix
                self.entry("tmpABCDEFGH")]                   # outside [a-z0-9_]
        holder = self.entry("keepnested")
        nested = self.entry("tmpnested01", parent=holder)  # not top level
        os.utime(holder, (self.now - OLD, self.now - OLD))
        target = self.entry("target", parent=Path(self.scratch.name))
        link = self.root / "tmplinkxyz0"
        link.symlink_to(target, target_is_directory=True)
        os.utime(link, (self.now - OLD, self.now - OLD), follow_symlinks=False)
        removed = tmp_sweep.sweep(self.root, self.now, os.getuid(), False)
        self.assertEqual(removed, [stale])
        self.assertFalse(stale.exists())
        for path in (*kept, holder, nested, link, target / "state.json"):
            self.assertTrue(path.exists(), path)

    def test_other_owner_is_kept(self) -> None:
        stale = self.entry("tmpowned_by")
        self.assertEqual(tmp_sweep.sweep(self.root, self.now, os.getuid() + 1, False), [])
        self.assertTrue(stale.exists())

    def test_dry_run_lists_and_removes_nothing(self) -> None:
        stale = self.entry("tmpdryrun00")
        self.assertEqual(tmp_sweep.sweep(self.root, self.now, os.getuid(), True), [stale])
        self.assertTrue(stale.exists())

    def run_main(self, *argv: str) -> str:
        out = io.StringIO()
        with patch.dict(os.environ, {tmp_sweep.REAL_TMPDIR_ENV: str(self.root)}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(tmp_sweep.main(list(argv)), 0)
        return out.getvalue()

    def test_cli_prints_only_when_it_removed_something(self) -> None:
        self.entry("tmpfresh0_1", age=60)
        self.assertEqual(self.run_main(), "")
        self.entry("tmpstale_01")
        self.assertIn("tmpstale_01", self.run_main("--dry-run"))
        self.assertEqual(self.run_main(), "tmp-sweep: removed 1\n")
        self.assertEqual(self.run_main(), "")

    def test_missing_root_is_silent(self) -> None:
        self.assertEqual(tmp_sweep.sweep(self.root / "absent", self.now, os.getuid(), False), [])


if __name__ == "__main__":
    unittest.main()
