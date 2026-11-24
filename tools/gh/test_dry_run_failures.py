#!/usr/bin/env python3
"""harmonic-forge#1544: a failed `l1_post --dry-run` is recorded once and
surfaced on the next prompt; a clean dry run clears it; the hook fails open."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _dry_run_failures as drf  # noqa: E402

HOOK = HERE.parent / "hooks" / "surface_dry_run_failures.py"
ARGV = ["--repo", "o/r", "--issue", "7", "--kind", "ae-and-sweep", "--sha", "a" * 40, "--dry-run"]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, main, argv=ARGV):
        with redirect_stderr(io.StringIO()):
            try:
                drf.run(main, argv)
            except SystemExit as exc:
                return exc.code
        return 0


class Recording(Base):
    def test_a_fail_exit_writes_one_entry_with_its_text(self) -> None:
        def main():
            raise SystemExit("[l1-post] overlap with feat/1505")
        self.run_main(main)
        entries = drf.pending()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][1]["text"], "[l1-post] overlap with feat/1505")
        self.assertEqual(entries[0][1]["sha"], "a" * 40)

    def test_an_argparse_exit_records_its_stderr(self) -> None:
        def main():
            sys.stderr.write("l1_post.py: error: the following arguments are required: --branch\n")
            raise SystemExit(2)
        self.run_main(main)
        self.assertIn("--branch", drf.pending()[0][1]["text"])

    def test_a_wait_exit_records_nothing(self) -> None:
        def main():
            raise SystemExit(drf.WAIT_EXIT)
        self.run_main(main)
        self.assertEqual(drf.pending(), [])

    def test_a_traceback_is_recorded(self) -> None:
        def main():
            raise ValueError("boom")
        with self.assertRaises(ValueError), redirect_stderr(io.StringIO()):
            drf.run(main, ARGV)
        self.assertIn("ValueError: boom", drf.pending()[0][1]["text"])

    def test_a_clean_dry_run_clears_the_entry(self) -> None:
        self.run_main(lambda: (_ for _ in ()).throw(SystemExit("[l1-post] refused")))
        self.assertEqual(len(drf.pending()), 1)
        self.run_main(lambda: None)
        self.assertEqual(drf.pending(), [])

    def test_a_real_post_is_never_recorded(self) -> None:
        def main():
            raise SystemExit("[l1-post] refused")
        self.run_main(main, [a for a in ARGV if a != "--dry-run"])
        self.assertEqual(drf.pending(), [])

    def test_the_exit_is_unchanged(self) -> None:
        def main():
            raise SystemExit("[l1-post] refused")
        self.assertEqual(self.run_main(main), "[l1-post] refused")


class Hook(Base):
    def hook(self) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HOOK)], capture_output=True, text=True,
                              env={**os.environ, "HOME": self.home}, input="{}")

    def test_an_entry_is_injected_once(self) -> None:
        drf.record("o/r", "7", "ae-and-sweep", "b" * 40, "[l1-post] refused")
        first = self.hook()
        self.assertEqual(first.returncode, 0)
        ctx = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("l1_post dry run FAILED for o/r#7 ae-and-sweep @ bbbbbbbbbbbb: [l1-post] refused", ctx)
        second = self.hook()
        self.assertEqual((second.returncode, second.stdout), (0, ""))

    def test_a_malformed_entry_injects_nothing(self) -> None:
        drf.state_dir().mkdir(parents=True)
        (drf.state_dir() / "o_r#7-ae.json").write_text("{not json")
        result = self.hook()
        self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
