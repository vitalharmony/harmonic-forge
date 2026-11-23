#!/usr/bin/env python3
"""Guard: a test run that leaves an untracked file in the checkout fails (harmonic-forge#1518).

One test cleared `os.environ` and reached the real `gh`, which wrote
`.local/state/gh/device-id` into the working directory. `run_tests.py` now compares the
untracked files before and after the suite. These tests drive the comparison directly and
drive `run_tests.main()` end to end on fixture suites in throwaway git repos.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

# `run_tests.hermetic_lane_token` imports this from `ROOT / "onboard"`, and the fixture runs patch
# ROOT, so it must already be importable here (as test_run_tests_identity_probe_guard.py does).
sys.path.insert(0, str(TOOLS / "onboard"))
import manifest_identity  # noqa: E402,F401
import run_tests  # noqa: E402
import untracked_snapshot as us  # noqa: E402

PLANT = '''
import unittest
from pathlib import Path


class Plant(unittest.TestCase):
    def test_writes_into_the_checkout(self):
        # repo/tools/leaf/test_plant.py -> repo/PLANTED.txt
        (Path(__file__).resolve().parents[2] / "PLANTED.txt").write_text("planted\\n")
'''
CLEAN = '''
import unittest


class Clean(unittest.TestCase):
    def test_writes_nothing(self):
        self.assertTrue(True)
'''
FAILS = '''
import unittest


class Fails(unittest.TestCase):
    def test_fails(self):
        self.fail("a failing suite")
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _fixture(work: Path, *, repo: bool, tests: dict[str, str]) -> Path:
    """`work/repo/tools/leaf/<tests>`; a git work tree (ignoring bytecode) when `repo`."""
    root = work / "repo"
    leaf = root / "tools" / "leaf"
    leaf.mkdir(parents=True)
    # A unique module name per fixture: `unittest discover` refuses a name it already cached
    # from another directory (test_run_tests_tmp_containment.py does the same).
    tag = uuid.uuid4().hex[:8]
    for name, body in tests.items():
        (leaf / f"{name[:-3]}_{tag}.py").write_text(body, encoding="utf-8")
    if repo:
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    return root


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="hf-1518-snap-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.repo = _fixture(self.work, repo=True, tests={})

    def test_it_lists_untracked_files_and_nothing_else(self) -> None:
        (self.repo / "tracked.txt").write_text("t")
        _git(self.repo, "add", "tracked.txt")
        (self.repo / "loose.txt").write_text("u")
        (self.repo / "__pycache__").mkdir()
        (self.repo / "__pycache__" / "x.pyc").write_text("ignored")
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "nested.txt").write_text("n")
        self.assertEqual(us.snapshot(self.repo), {".gitignore", "loose.txt", "sub/nested.txt"})

    def test_a_path_with_a_space_or_a_newline_is_one_path(self) -> None:
        (self.repo / "a b.txt").write_text("x")
        (self.repo / "line\nbreak.txt").write_text("x")
        self.assertTrue({"a b.txt", "line\nbreak.txt"} <= us.snapshot(self.repo))

    def test_a_directory_that_is_not_a_work_tree_gives_none(self) -> None:
        plain = self.work / "plain"
        plain.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.work)}):
            self.assertIsNone(us.snapshot(plain))

    def test_a_missing_git_gives_none(self) -> None:
        with mock.patch.object(us.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(us.snapshot(self.repo))

    def test_new_paths_is_the_sorted_difference(self) -> None:
        self.assertEqual(us.new_paths({"a", "b"}, {"a", "b", "z", "c"}), ["c", "z"])
        self.assertEqual(us.new_paths({"a", "b"}, {"a"}), [])


class VerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="hf-1518-verdict-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.repo = _fixture(self.work, repo=True, tests={})

    def _verdict(self, before, code: int = 0, root=None) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            return us.verdict(before, root or self.repo, code), err.getvalue()

    def test_a_new_untracked_file_fails_the_run_and_is_named(self) -> None:
        before = us.snapshot(self.repo)
        (self.repo / "leaked.txt").write_text("x")
        (self.repo / ".local" / "state" / "gh").mkdir(parents=True)
        (self.repo / ".local" / "state" / "gh" / "device-id").write_text("id")
        code, err = self._verdict(before)
        self.assertEqual(code, 1)
        self.assertIn("leaked.txt", err)
        self.assertIn(".local/state/gh/device-id", err)
        self.assertIn("2 untracked file(s)", err)

    def test_a_file_that_was_already_untracked_is_not_this_runs(self) -> None:
        (self.repo / "before.txt").write_text("x")
        before = us.snapshot(self.repo)
        code, err = self._verdict(before)
        self.assertEqual((code, err), (0, ""))

    def test_the_suites_own_code_stands_when_nothing_is_new(self) -> None:
        before = us.snapshot(self.repo)
        self.assertEqual(self._verdict(before, 0)[0], 0)
        self.assertEqual(self._verdict(before, 1)[0], 1)

    def test_a_leak_fails_even_when_the_suite_passed_and_when_it_failed(self) -> None:
        before = us.snapshot(self.repo)
        (self.repo / "leaked.txt").write_text("x")
        self.assertEqual(self._verdict(before, 0)[0], 1)
        self.assertEqual(self._verdict(before, 1)[0], 1)

    def test_a_check_that_did_not_run_says_so_and_keeps_the_exit_code(self) -> None:
        for code in (0, 1):
            with self.subTest(code=code):
                result, err = self._verdict(None, code)
                self.assertEqual(result, code)
                self.assertIn("did not run", err)
                self.assertIn(f"exit code stands: {code}", err)

    def test_git_disappearing_after_the_suite_is_also_reported_not_passed(self) -> None:
        before = us.snapshot(self.repo)
        with mock.patch.object(us, "snapshot", return_value=None):
            code, err = self._verdict(before, 0)
        self.assertEqual(code, 0)
        self.assertIn("did not run", err)


class EndToEndTests(unittest.TestCase):
    """`run_tests.main()` on a fixture suite, with a scratch dir standing in for the temp dir."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="hf-1518-e2e-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.tmp_parent = self.work / "tmp"
        self.tmp_parent.mkdir()

    def _main(self, repo: Path, *, ceiling: bool = False) -> tuple[int, str, str]:
        saved_cache, saved_path = tempfile.tempdir, list(sys.path)
        env = {name: str(self.tmp_parent) for name in run_tests.TMP_ENV_VARS}
        if ceiling:
            env["GIT_CEILING_DIRECTORIES"] = str(self.work)
        out, err = io.StringIO(), io.StringIO()
        try:
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(run_tests, "ROOT", repo / "tools"), \
                    mock.patch.object(run_tests, "TEST_DIRS", ["leaf"]), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                tempfile.tempdir = str(self.tmp_parent)
                return run_tests.main(), out.getvalue(), err.getvalue()
        finally:
            tempfile.tempdir = saved_cache
            sys.path[:] = saved_path

    def test_a_test_that_writes_into_the_checkout_fails_the_run_and_names_the_file(self) -> None:
        repo = _fixture(self.work, repo=True, tests={"test_plant.py": PLANT})
        code, out, err = self._main(repo)
        self.assertEqual(code, 1)
        self.assertTrue((repo / "PLANTED.txt").exists(), "the fixture writer did not run")
        self.assertIn("PLANTED.txt", err)
        self.assertIn("harmonic-forge#1518", err)
        self.assertNotIn("[test] OK", out)

    def test_a_clean_suite_passes_even_with_untracked_files_already_present(self) -> None:
        repo = _fixture(self.work, repo=True, tests={"test_clean.py": CLEAN})
        (repo / "EXISTING.txt").write_text("already here")
        code, out, err = self._main(repo)
        self.assertEqual(code, 0, err)
        self.assertIn("[test] OK", out)
        self.assertNotIn("untracked file(s)", err)

    def test_kill_check_with_the_comparison_stubbed_the_planted_writer_goes_unseen(self) -> None:
        """Proves the planted-writer test above is not vacuous: the fixture really writes the
        file, and only the comparison notices it. Stub the comparison to report nothing and the
        same run passes."""
        repo = _fixture(self.work, repo=True, tests={"test_plant.py": PLANT})
        with mock.patch.object(us, "new_paths", return_value=[]):
            code, out, err = self._main(repo)
        self.assertEqual(code, 0)
        self.assertTrue((repo / "PLANTED.txt").exists())
        self.assertIn("[test] OK", out)

    def test_a_failing_suite_still_fails_and_a_leak_is_still_named(self) -> None:
        repo = _fixture(self.work, repo=True, tests={"test_fails.py": FAILS, "test_plant.py": PLANT})
        code, _out, err = self._main(repo)
        self.assertEqual(code, 1)
        self.assertIn("PLANTED.txt", err)

    def test_outside_a_work_tree_the_check_says_it_did_not_run_and_the_exit_code_stands(self) -> None:
        passing = _fixture(self.work / "a", repo=False, tests={"test_clean.py": CLEAN}) \
            if (self.work / "a").mkdir() is None else None
        code, out, err = self._main(passing, ceiling=True)
        self.assertEqual(code, 0)
        self.assertIn("[test] OK", out)
        self.assertIn("did not run", err)
        self.assertIn("exit code stands: 0", err)
        (self.work / "b").mkdir()
        failing = _fixture(self.work / "b", repo=False, tests={"test_fails.py": FAILS})
        code, _out, err = self._main(failing, ceiling=True)
        self.assertEqual(code, 1)
        self.assertIn("exit code stands: 1", err)

    def test_the_runner_is_wired_to_the_check(self) -> None:
        source = (TOOLS / "run_tests.py").read_text(encoding="utf-8")
        self.assertIn("untracked_snapshot.snapshot(ROOT.parent)", source)
        self.assertIn("untracked_snapshot.verdict(", source)


if __name__ == "__main__":
    unittest.main()
