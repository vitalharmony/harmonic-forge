"""harmonic-forge#1052: harness-run kill checks fail on base and pass on head,
from scratch checkouts, never touching the worktree under review."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kill_runner


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


#: A Lane-1 test: passes only when f() returns 2 (the head's behavior). It also
#: records its own cwd, so a test can prove where it ran.
CHECK = ("import os, sys\nfrom pathlib import Path\n"
         "log = os.environ.get('KR_CWD_LOG')\n"
         "if log:\n    open(log, 'a').write(str(Path.cwd().resolve()) + '\\n')\n"
         "sys.path.insert(0, os.getcwd())\nfrom f import f\n"
         "sys.exit(0 if f() == 2 else 1)\n")


class KillRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        # Under the real ~/.cache, not /tmp: the scratch root is refused under
        # /tmp (harmonic-forge#949).
        (Path.home() / ".cache").mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=Path.home() / ".cache", prefix="kill-runner-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"HRSE_SCRATCH_ROOT": str(self.root / "scratch"),
                                           "KR_CWD_LOG": str(self.root / "cwd.log")})
        env.start()
        self.addCleanup(env.stop)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        git(self.source, "config", "user.name", "test")
        git(self.source, "config", "user.email", "test@example.test")
        (self.source / "f.py").write_text("def f():\n    return 1\n")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-qm", "base")
        self.base = git(self.source, "rev-parse", "HEAD")
        # The agent's worktree: a linked worktree on its own branch.
        self.worktree = self.root / "agent-worktree"
        git(self.source, "worktree", "add", "-q", "-b", "fix/1-x", str(self.worktree))
        (self.worktree / "f.py").write_text("def f():\n    return 2\n")
        git(self.worktree, "commit", "-qam", "head")
        self.head = git(self.worktree, "rev-parse", "HEAD")
        self.bundle = self.make_bundle({"tests/kr_check.py": CHECK})
        cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

    def make_bundle(self, files: dict[str, str], checks: list | None = None) -> Path:
        bundle = Path(tempfile.mkdtemp(dir=self.root, prefix="bundle-"))
        for rel, text in files.items():
            (bundle / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
            (bundle / "files" / rel).write_text(text)
        (bundle / "files").mkdir(exist_ok=True)
        if checks is None:
            checks = [{"ac": "AC1", "test": ["python3", "tests/kr_check.py"]}]
        (bundle / "checks.json").write_text(json.dumps(checks))
        return bundle

    def run_tool(self, *, bundle: Path | None = None, base: str | None = None,
                 head: str | None = None) -> tuple[int, dict | None, str]:
        argv = ["run", "--repo", "o/r", "--issue", "1", "--base", base or self.base,
                "--head", head or self.head, "--bundle", str(bundle or self.bundle),
                "--source-repo", str(self.source)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = kill_runner.main(argv)
        return rc, (json.loads(out.getvalue()) if out.getvalue() else None), err.getvalue()

    def test_fails_on_base_passes_on_head(self):
        rc, report, err = self.run_tool()
        self.assertEqual(rc, 0, err)
        self.assertEqual(report["checks"], [{"ac": "AC1", "base_rc": 1, "head_rc": 0}])
        self.assertEqual(report["bundle_sha256"], kill_runner.bundle_digest(self.bundle))

    def test_passes_on_base_is_reported(self):
        always = self.make_bundle({"tests/ok.py": "pass\n"},
                                  [{"ac": "AC1", "test": ["python3", "tests/ok.py"]}])
        rc, report, err = self.run_tool(bundle=always)
        self.assertEqual(rc, 0, err)
        self.assertEqual(report["checks"], [{"ac": "AC1", "base_rc": 0, "head_rc": 0}])

    def test_fails_on_head_is_reported(self):
        rc, report, err = self.run_tool(head=self.base)
        self.assertEqual(rc, 0, err)
        self.assertEqual(report["checks"], [{"ac": "AC1", "base_rc": 1, "head_rc": 1}])

    def test_overlay_path_escape_is_refused(self):
        outside = self.root / "outside.txt"
        outside.write_text("x")
        bundle = self.make_bundle({})
        (bundle / "files" / "link.py").symlink_to(outside)
        rc, report, err = self.run_tool(bundle=bundle)
        self.assertEqual(rc, 2)
        self.assertIsNone(report)
        self.assertIn("symlink", err)
        self.assertFalse((self.root / "cwd.log").exists(), "a check ran despite the refusal")

    def test_overlay_through_checkout_symlink_is_refused(self):
        (self.worktree / "tests").symlink_to(self.root)
        git(self.worktree, "add", "-A")
        git(self.worktree, "commit", "-qm", "symlinked dir")
        head = git(self.worktree, "rev-parse", "HEAD")
        rc, _report, err = self.run_tool(head=head)
        self.assertEqual(rc, 2)
        self.assertIn("overlay", err)
        self.assertFalse((self.root / "cwd.log").exists())

    def test_overlay_over_head_changed_path_is_refused(self):
        bundle = self.make_bundle({"f.py": "def f():\n    return 2\n"},
                                  [{"ac": "AC1", "test": ["python3", "-c", "pass"]}])
        rc, report, err = self.run_tool(bundle=bundle)
        self.assertEqual(rc, 2)
        self.assertIsNone(report)
        self.assertIn("head changed", err)

    def test_digest_changes_with_one_byte_edit_in_files(self):
        before = kill_runner.bundle_digest(self.bundle)
        path = self.bundle / "files" / "tests" / "kr_check.py"
        path.write_text(path.read_text() + " ")
        self.assertNotEqual(before, kill_runner.bundle_digest(self.bundle))

    def test_digest_changes_with_checks_edit_and_rename(self):
        before = kill_runner.bundle_digest(self.bundle)
        (self.bundle / "checks.json").write_text(
            json.dumps([{"ac": "AC2", "test": ["python3", "tests/kr_check.py"]}]))
        edited = kill_runner.bundle_digest(self.bundle)
        self.assertNotEqual(before, edited)
        (self.bundle / "files" / "tests" / "kr_check.py").rename(
            self.bundle / "files" / "tests" / "kr_check2.py")
        self.assertNotEqual(edited, kill_runner.bundle_digest(self.bundle))

    def test_worktree_is_never_cwd(self):
        rc, _report, err = self.run_tool()
        self.assertEqual(rc, 0, err)
        cwds = (self.root / "cwd.log").read_text().split()
        self.assertEqual(len(cwds), 2)
        scratch = (self.root / "scratch").resolve()
        for cwd in cwds:
            self.assertIn(scratch, Path(cwd).parents)
            self.assertNotIn(self.worktree.resolve(), [Path(cwd), *Path(cwd).parents])

    def test_worktree_path_is_never_opened(self):
        # An unreadable worktree: any read, cwd or git call there would fail.
        self.worktree.chmod(0)
        self.addCleanup(self.worktree.chmod, 0o755)
        rc, report, err = self.run_tool()
        self.assertEqual(rc, 0, err)
        self.assertEqual(report["checks"], [{"ac": "AC1", "base_rc": 1, "head_rc": 0}])

    def test_git_never_runs_in_the_worktree(self):
        calls = []
        real = kill_runner.run_command

        def recording(argv, **kwargs):
            calls.append((list(argv), kwargs.get("cwd")))
            return real(argv, **kwargs)

        with mock.patch.object(kill_runner, "run_command", recording):
            rc, _report, err = self.run_tool()
        self.assertEqual(rc, 0, err)
        git_calls = [argv for argv, _cwd in calls if argv[0] == "git"]
        self.assertTrue(git_calls)
        for argv in git_calls:
            self.assertEqual(argv[1:3], ["-C", str(self.source.resolve())], argv)

    def test_cwd_inside_worktree_is_refused(self):
        os.chdir(self.worktree)
        rc, report, err = self.run_tool()
        self.assertEqual(rc, 2)
        self.assertIsNone(report)
        self.assertIn("worktree under review", err)

    def test_cwd_inside_worktree_at_another_commit_is_refused(self):
        # The worktree has moved past the head under review: still refused.
        (self.worktree / "g.py").write_text("x = 1\n")
        git(self.worktree, "add", "-A")
        git(self.worktree, "commit", "-qm", "later")
        os.chdir(self.worktree)
        rc, report, err = self.run_tool()
        self.assertEqual(rc, 2)
        self.assertIsNone(report)
        self.assertIn("worktree under review", err)

    def test_overlay_over_head_changed_non_ascii_path_is_refused(self):
        (self.worktree / "tést.py").write_text("x = 1\n")
        git(self.worktree, "add", "-A")
        git(self.worktree, "commit", "-qm", "non-ascii")
        head = git(self.worktree, "rev-parse", "HEAD")
        bundle = self.make_bundle({"tést.py": "x = 2\n"},
                                  [{"ac": "AC1", "test": ["python3", "-c", "pass"]}])
        rc, report, err = self.run_tool(bundle=bundle, head=head)
        self.assertEqual(rc, 2)
        self.assertIsNone(report)
        self.assertIn("head changed", err)

    def test_digest_covers_the_bytes_that_ran(self):
        # An edit landing while the run waits on the scratch lock: the reported
        # digest and the results both reflect the same (edited) bytes.
        real = kill_runner._scratch.scratch_dir
        check = self.bundle / "files" / "tests" / "kr_check.py"

        @contextlib.contextmanager
        def edit_during_wait(*args, **kwargs):
            check.write_text("import sys\nsys.exit(0)\n")
            with real(*args, **kwargs) as parent:
                yield parent

        with mock.patch.object(kill_runner._scratch, "scratch_dir", edit_during_wait):
            rc, report, err = self.run_tool()
        self.assertEqual(rc, 0, err)
        self.assertEqual(report["bundle_sha256"], kill_runner.bundle_digest(self.bundle))
        self.assertEqual(report["checks"], [{"ac": "AC1", "base_rc": 0, "head_rc": 0}])

    def test_bad_ref_and_bad_bundle_exit_two(self):
        rc, _report, _err = self.run_tool(head="no-such-ref")
        self.assertEqual(rc, 2)
        empty = self.make_bundle({}, [])
        rc, _report, err = self.run_tool(bundle=empty)
        self.assertEqual(rc, 2)
        self.assertIn("non-empty", err)
        bad = self.make_bundle({}, [{"ac": "AC1", "test": []}])
        rc, _report, err = self.run_tool(bundle=bad)
        self.assertEqual(rc, 2)

    def test_timeout_records_rc_124(self):
        slow = self.make_bundle({"tests/slow.py": "import time\ntime.sleep(30)\n"},
                                [{"ac": "AC1", "test": ["python3", "tests/slow.py"]}])
        argv = ["run", "--repo", "o/r", "--issue", "1", "--base", self.base, "--head", self.head,
                "--bundle", str(slow), "--source-repo", str(self.source), "--timeout", "1"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(kill_runner.main(argv), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["checks"], [{"ac": "AC1", "base_rc": 124, "head_rc": 124}])


if __name__ == "__main__":
    unittest.main()
