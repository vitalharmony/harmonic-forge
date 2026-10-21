#!/usr/bin/env python3
"""Guard: a test run's temp files stay inside one per-run root (harmonic-forge#1114).

Each test drives `run_tests.main()` on a small fixture suite, with a scratch
directory standing in for the real temp dir, so the guards hold whether this
file runs alone or inside the full suite (which is itself contained).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import run_tests  # noqa: E402

PROBE = '''
import json, os, subprocess, sys, tempfile, unittest

# Module-level, like item_list_cache.py:33: runs when build_suite() imports it.
IMPORT_TMP = tempfile.gettempdir()


class Probe(unittest.TestCase):
    def test_probe(self):
        sub = subprocess.run([sys.executable, "-c", "import tempfile;print(tempfile.mkdtemp())"],
                             capture_output=True, text=True, check=True).stdout.strip()
        record = {"import_tmp": IMPORT_TMP, "inprocess": tempfile.mkdtemp(),
                  "subprocess": sub, "tmpdir_env": os.environ.get("TMPDIR"),
                  "real_tmp": os.environ.get("HF_REAL_TMPDIR")}
        with open(%(out)r, "w") as f:
            json.dump(record, f)
'''

LEAKER = '''
import tempfile, unittest


class Leaker(unittest.TestCase):
    def test_leaks(self):
        tempfile.mkdtemp()
'''


@contextlib.contextmanager
def _without_env(name: str):
    """Unset `name` for the block (an enclosing contained run sets it)."""
    with mock.patch.dict(os.environ):
        os.environ.pop(name, None)
        yield


class ContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="hf-1114-guard-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.parent = self.work / "tmp"
        self.parent.mkdir()
        self.saved_real = os.environ.get(run_tests.REAL_TMPDIR_ENV)
        tag = uuid.uuid4().hex[:10]
        suite_dir = self.work / "suite" / "leaf"
        suite_dir.mkdir(parents=True)
        self.out = self.work / "probe.json"
        (suite_dir / f"test_probe_{tag}.py").write_text(PROBE % {"out": str(self.out)})
        (suite_dir / f"test_leaker_{tag}.py").write_text(LEAKER)
        self.result = self._run_main(suite_dir.parent)

    def _run_main(self, suite_root: Path) -> int:
        """Run main() with self.parent as the real temp dir; restore everything after."""
        saved_cache = tempfile.tempdir
        saved_path = list(sys.path)
        env = {name: str(self.parent) for name in run_tests.TMP_ENV_VARS}
        try:
            with mock.patch.dict(os.environ, env), \
                    _without_env(run_tests.REAL_TMPDIR_ENV), \
                    mock.patch.object(run_tests, "ROOT", suite_root), \
                    mock.patch.object(run_tests, "TEST_DIRS", ["leaf"]), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                tempfile.tempdir = str(self.parent)
                return run_tests.main()
        finally:
            tempfile.tempdir = saved_cache
            sys.path[:] = saved_path

    def _record(self) -> dict:
        return json.loads(self.out.read_text())

    def _assert_in_run_root(self, path: str) -> None:
        rel = Path(path).relative_to(self.parent)  # raises if outside the scratch parent
        self.assertTrue(rel.parts[0].startswith("hf-test-run-"), f"{path} is outside the per-run root")

    def test_fixture_suite_ran(self) -> None:
        self.assertEqual(self.result, 0)
        self.assertTrue(self.out.exists())

    def test_inprocess_mkdtemp_is_contained(self) -> None:
        self._assert_in_run_root(self._record()["inprocess"])

    def test_subprocess_mkdtemp_is_contained(self) -> None:
        self._assert_in_run_root(self._record()["subprocess"])

    def test_import_time_gettempdir_is_contained(self) -> None:
        """The redirect precedes build_suite(): a module-level gettempdir() sees the root."""
        self._assert_in_run_root(self._record()["import_tmp"])

    def test_real_tmpdir_is_recorded_for_negative_assertions(self) -> None:
        """harmonic-forge#1114: tests can still name the real temp root."""
        self.assertEqual(self._record()["real_tmp"], str(self.parent))
        self.assertEqual(os.environ.get(run_tests.REAL_TMPDIR_ENV), self.saved_real)

    def test_main_leaves_no_toplevel_entry(self) -> None:
        self.assertEqual(sorted(os.listdir(self.parent)), [])

class HelperRestoreTests(unittest.TestCase):
    def test_contained_tempdir_restores_absent_vars(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in run_tests.TMP_ENV_VARS:
                os.environ.pop(name, None)
            saved = tempfile.tempdir
            with run_tests.contained_tempdir() as root:
                self.assertEqual(os.environ["TMP"], str(root))
                self.assertEqual(os.environ["TEMP"], str(root))
                self.assertEqual(tempfile.tempdir, str(root))
            for name in run_tests.TMP_ENV_VARS:
                self.assertNotIn(name, os.environ)
            self.assertEqual(tempfile.tempdir, saved)
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
