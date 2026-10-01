"""Executable F846 kill-check contracts; every fixture is a throwaway repo."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kill_check as kill  # noqa: E402
import preclose_check as preclose  # noqa: E402

REPO = "vitalharmony/hrse"
ISSUE = 1208


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout.strip()


class KillCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self.cwd)
        home = patch.dict(os.environ, {"HOME": str(self.root / "home")})
        home.start()
        self.addCleanup(home.stop)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "test")
        git(self.repo, "config", "user.email", "test@example.test")
        git(self.repo, "remote", "add", "origin", "https://github.com/vitalharmony/hrse.git")
        (self.repo / "f.py").write_text("def f():\n    return 1\n")
        (self.repo / "check.py").write_text(
            "from f import f\nfrom pathlib import Path\n"
            "assert not Path('__pycache__').exists()\nassert f() == 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        (self.repo / "seed.txt").write_text("change\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "subject")
        self.patch_file = self.root / "stub.patch"
        self.patch_file.write_text("--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2\n")
        self.checks_file = self.root / "checks.json"
        self.write_checks()

    def write_checks(self, *, test: list[str] | None = None, patch_path: str | None = None) -> None:
        self.checks_file.write_text(json.dumps([{
            "ac": "AC1", "mechanism": "f returns 1", "patch": patch_path or str(self.patch_file),
            "test": test or ["python3", "check.py"]}]))

    def execute(self, *, timeout: int = 300, checks: str | None = None) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return kill.run(argparse.Namespace(repo=REPO, issue=ISSUE, base="origin/main",
                                               head="HEAD", checks=checks or str(self.checks_file),
                                               timeout=timeout))

    def receipt(self) -> dict:
        return kill.read_receipt(REPO, ISSUE)

    def test_killed_same_length_stub_and_receipt(self) -> None:
        self.assertEqual(self.execute(), 0)
        item = self.receipt()["checks"][0]
        self.assertEqual(item["baseline_rcs"], [0, 0])
        self.assertNotEqual(item["mutated_rc"], 0)
        self.assertEqual(item["verdict"], "killed")
        self.assertEqual(self.receipt()["status"], "pass")
        self.assertEqual(self.receipt()["head_sha"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(item["patch_text"], self.patch_file.read_text())
        self.assertEqual(item["patch_sha256"],
                         hashlib.sha256(item["patch_text"].encode()).hexdigest())
        self.assertEqual(list(kill.scratch_parent().glob("kill-check-*")), [])

    def test_vacuous_test_is_not_a_kill(self) -> None:
        (self.repo / "check.py").write_text("assert True\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "vacuous")
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.receipt()["checks"][0]["verdict"], "vacuous")

    def test_relative_patch_path_and_nonapplying_patch(self) -> None:
        relative = os.path.relpath(self.patch_file, self.repo)
        self.write_checks(patch_path=relative)
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.receipt()["checks"][0]["patch_path"], str(self.patch_file))
        self.patch_file.write_text(self.patch_file.read_text().replace("return 1", "return 9"))
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.receipt()["checks"][0]["verdict"], "patch-failed")

    def test_broken_and_flaky_baselines(self) -> None:
        self.write_checks(test=["python3", "-c", "raise AssertionError('broken')"])
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.receipt()["checks"][0]["verdict"], "broken-test")
        self.assertEqual(list(kill.scratch_parent().glob("kill-check-*")), [])
        (self.repo / "flaky.py").write_text(
            "from pathlib import Path\n"
            "p = Path('ran')\n"
            "assert not p.exists()\n"
            "p.write_text('x')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "flaky")
        self.write_checks(test=["python3", "flaky.py"])
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.receipt()["checks"][0]["verdict"], "flaky")

    def test_third_run_state_failure_is_not_a_kill(self) -> None:
        (self.repo / "third.py").write_text(
            "from pathlib import Path\n"
            "p = Path('count')\n"
            "n = int(p.read_text()) + 1 if p.exists() else 1\n"
            "p.write_text(str(n))\n"
            "assert n < 3\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "third")
        self.write_checks(test=["python3", "third.py"])
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.receipt()["checks"][0]["verdict"], "flaky")

    def test_origin_main_available_and_timeout(self) -> None:
        self.write_checks(test=["python3", "-c", "import subprocess; "
                                "assert subprocess.check_output(['git','remote','get-url','origin']); "
                                "assert subprocess.check_output(['git','rev-parse','origin/main'])"])
        self.assertEqual(self.execute(), 1)  # Baseline works; it does not assert f().
        self.assertEqual(self.receipt()["checks"][0]["baseline_rcs"], [0, 0])
        self.write_checks(test=["python3", "-c", "import time; time.sleep(2)"])
        self.assertEqual(self.execute(timeout=1), 1)
        self.assertEqual(self.receipt()["checks"][0]["verdict"], "timeout")
        (self.repo / "sleep_on_stub.py").write_text(
            "from f import f\nimport time\n"
            "if f() != 1:\n    time.sleep(2)\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "mutated timeout")
        self.write_checks(test=["python3", "sleep_on_stub.py"])
        self.assertEqual(self.execute(timeout=1), 1)
        self.assertEqual(self.receipt()["checks"][0]["baseline_rcs"], [0, 0])
        self.assertEqual(self.receipt()["checks"][0]["control_rc"], 0)
        self.assertEqual(self.receipt()["checks"][0]["verdict"], "timeout")

    def test_repo_state_unchanged_and_tmpdir_ignored(self) -> None:
        (self.repo / "uncommitted.txt").write_text("keep\n")
        before = (git(self.repo, "status", "--porcelain"),
                  git(self.repo, "worktree", "list"), git(self.repo, "rev-parse", "HEAD"))
        with patch.dict(os.environ, {"TMPDIR": "/tmp/claude-x"}):
            self.assertEqual(self.execute(), 0)
        after = (git(self.repo, "status", "--porcelain"),
                 git(self.repo, "worktree", "list"), git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(after, before)
        self.assertEqual(self.receipt()["scratch_parent"], str(kill.scratch_parent()))

    def test_reaper_removes_only_old_scratch(self) -> None:
        parent = kill.scratch_parent()
        old, fresh = parent / "kill-check-old", parent / "kill-check-fresh"
        old.mkdir()
        fresh.mkdir()
        ancient = time.time() - kill.SCRATCH_AGE_SECONDS - 10
        os.utime(old, (ancient, ancient))
        kill.reap_old(parent)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_atomic_replace_preserves_prior_receipt_on_error(self) -> None:
        self.assertEqual(self.execute(), 0)
        before = kill.receipt_path(REPO, ISSUE).read_bytes()
        with patch.object(kill.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                kill.write_receipt(REPO, ISSUE, {"status": "fail"})
        self.assertEqual(kill.receipt_path(REPO, ISSUE).read_bytes(), before)

    def test_materialize_exception_and_cleanup_failure(self) -> None:
        with patch.object(kill, "materialize", side_effect=RuntimeError("unpack failed")):
            self.assertEqual(self.execute(), 1)
        self.assertEqual(self.receipt()["checks"][0]["verdict"], "error")
        self.assertEqual(list(kill.scratch_parent().glob("kill-check-*")), [])
        with patch.object(kill, "remove_tree", return_value="permission denied"):
            self.assertEqual(self.execute(), 1)
        self.assertEqual(self.receipt()["checks"][0]["verdict"], "cleanup-failed")

    def test_patch_file_change_does_not_change_applied_bytes(self) -> None:
        original = self.patch_file.read_text()
        real_command = kill.run_command
        calls = 0

        def change_after_baselines(argv, **kwargs):
            nonlocal calls
            result = real_command(argv, **kwargs)
            if argv == ["python3", "check.py"]:
                calls += 1
                if calls == 2:
                    self.patch_file.write_text("not a diff\n")
            return result

        with patch.object(kill, "run_command", side_effect=change_after_baselines):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(self.receipt()["checks"][0]["patch_text"], original)
        self.assertEqual(self.receipt()["checks"][0]["verdict"], "killed")

    def test_plan_receipt_waiver_and_force(self) -> None:
        args = argparse.Namespace(repo=REPO, issue=ISSUE, base="origin/main", head="HEAD",
                                  tier="fast", force=False, reforge=False,
                                  allow_dirty=False, allow_repo_mismatch=False)
        with self.assertRaises(SystemExit) as missing:
            preclose.plan(args)
        self.assertIn("mise run kill-check", str(missing.exception))
        self.assertEqual(self.execute(), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(preclose.plan(args), 0)
        receipt = self.receipt()
        receipt["status"] = "fail"
        kill.write_receipt(REPO, ISSUE, receipt)
        with self.assertRaises(SystemExit):
            preclose.plan(args)
        args.force = True
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(preclose.plan(args), 0)
        args.force = False
        with self.assertRaises(SystemExit):
            kill.waive(argparse.Namespace(repo=REPO, issue=ISSUE, base="origin/main",
                                          head="HEAD", reason=" "))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(kill.waive(argparse.Namespace(
                repo=REPO, issue=ISSUE, base="origin/main", head="HEAD",
                reason="operator: waive these checks")), 0)
            self.assertEqual(preclose.plan(args), 0)

    def test_patch_identical_rebase_receipt(self) -> None:
        self.assertEqual(self.execute(), 0)
        first = self.receipt()
        git(self.repo, "checkout", "-q", "--detach", "origin/main")
        (self.repo / "unrelated.txt").write_text("u\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "unrelated")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "rebase", "-q", "origin/main")
        self.assertNotEqual(first["head_sha"], git(self.repo, "rev-parse", "HEAD"))
        self.assertTrue(kill.covering_receipt(REPO, ISSUE, git(self.repo, "rev-parse", "HEAD"),
                                              preclose.local_patch_id("origin/main", "HEAD")))
        args = argparse.Namespace(repo=REPO, issue=ISSUE, base="origin/main", head="HEAD",
                                  tier="fast", force=False, reforge=False,
                                  allow_dirty=False, allow_repo_mismatch=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(preclose.plan(args), 0)


if __name__ == "__main__":
    unittest.main()
