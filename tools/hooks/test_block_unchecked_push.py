"""harmonic-forge#1135: PreToolUse refusal of an unchecked Tooling Exception push."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import block_unchecked_push as B  # noqa: E402
import check_receipt as C  # noqa: E402

TE_BRANCH = "fix/975-x"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "f").write_text("x")
        _git(self.repo, "add", "f")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
        _git(self.repo, "branch", TE_BRANCH)
        _git(self.repo, "remote", "add", "origin", "https://github.com/vitalharmony/harmonic-forge.git")
        _git(self.repo, "remote", "add", "upstream", "https://github.com/someone/other.git")
        self.slugs = []
        labels = mock.patch.object(C, "labels", side_effect=self._labels)
        labels.start()
        self.addCleanup(labels.stop)
        receipt = mock.patch.object(C, "receipt_path", return_value=self.root / "no-receipt")
        receipt.start()
        self.addCleanup(receipt.stop)

    def _labels(self, slug, issue):
        self.slugs.append(slug)
        return ["tooling-exception"]

    def decide(self, command, cwd=None):
        return B.decide({"tool_input": {"command": command}, "cwd": str(cwd or self.repo)})


class Denies(Base):
    def test_unchecked_te_push_is_denied(self):
        self.assertIn("mise run check", self.decide(f"git push origin {TE_BRANCH}"))

    def test_plus_prefix_is_stripped(self):
        self.assertIsNotNone(self.decide(f"git push origin +HEAD:{TE_BRANCH}"))

    def test_every_refspec_is_checked(self):
        self.assertIsNotNone(self.decide(f"git push origin main {TE_BRANCH}"))

    def test_slug_comes_from_the_pushed_remote(self):
        self.decide(f"git push upstream {TE_BRANCH}")
        self.assertEqual(self.slugs, [C.repo_slug("https://github.com/someone/other.git")])

    def test_literal_cds_apply_in_order(self):
        sub = self.repo / "sub"
        sub.mkdir()
        self.assertIsNotNone(self.decide(f"cd sub && cd .. && git push origin {TE_BRANCH}"))

    def test_main_emits_deny_json(self):
        out = io.StringIO()
        payload = {"tool_input": {"command": f"git push origin {TE_BRANCH}"}, "cwd": str(self.repo)}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
            B.main()
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")


class Allows(Base):
    def test_push_after_another_command_is_left_to_pre_push(self):
        self.assertIsNone(self.decide(f"mise run check && git push origin {TE_BRANCH}"))

    def test_subshell_is_left_to_pre_push(self):
        self.assertIsNone(self.decide(f"(cd /tmp); git push origin {TE_BRANCH}"))

    def test_cd_away_from_the_repo_is_judged_there(self):
        self.assertIsNone(self.decide(f"cd {self.root} && git push origin {TE_BRANCH}"))

    def test_receipt_present_allows(self):
        with mock.patch.object(C, "receipt_path", return_value=self.repo / "f"):
            self.assertIsNone(self.decide(f"git push origin {TE_BRANCH}"))

    def test_non_te_branch_allows(self):
        with mock.patch.object(C, "labels", return_value=["bug"]):
            self.assertIsNone(self.decide(f"git push origin {TE_BRANCH}"))

    def test_label_failure_allows(self):
        with mock.patch.object(C, "labels", return_value=None):
            self.assertIsNone(self.decide(f"git push origin {TE_BRANCH}"))

    def test_deletion_allows(self):
        self.assertIsNone(self.decide(f"git push origin :{TE_BRANCH}"))

    def test_unmodelled_options_allow(self):
        for flag in ("--dry-run", "--no-verify", "--all", "--mirror", "--tags", "--delete"):
            with self.subTest(flag=flag):
                self.assertIsNone(self.decide(f"git push {flag} origin {TE_BRANCH}"))

    def test_configured_push_refspec_allows(self):
        _git(self.repo, "config", "remote.origin.push", "refs/heads/*:refs/heads/*")
        self.assertIsNone(self.decide(f"git push origin {TE_BRANCH}"))

    def test_non_push_fast_exits_without_spawning_git(self):
        with mock.patch.object(B.subprocess, "run") as run:
            self.assertIsNone(self.decide("ls -la"))
        run.assert_not_called()

    def test_own_fault_never_blocks(self):
        with mock.patch.object(B, "decide", side_effect=RuntimeError), \
             mock.patch("sys.stdin", io.StringIO("{}")), mock.patch("sys.stdout", io.StringIO()) as out:
            B.main()
        self.assertEqual(out.getvalue(), "")


class Wired(unittest.TestCase):
    """AC4: forge's settings register the hook on PreToolUse Bash, with a short timeout."""

    def test_registered(self):
        settings = json.loads((Path(__file__).resolve().parents[2] / ".claude" / "settings.json").read_text())
        hooks = [h for e in settings["hooks"]["PreToolUse"] if e.get("matcher") == "Bash" for h in e["hooks"]]
        entry = next(h for h in hooks if "block_unchecked_push.py" in h.get("command", ""))
        self.assertLessEqual(entry.get("timeout", 60), 5)


class SharedAnswer(unittest.TestCase):
    """AC1: pre-push and the hook ask the same question."""

    def test_refuses_delegates_to_push_refusal(self):
        with mock.patch.object(C, "push_refusal", return_value="why") as refusal:
            self.assertTrue(C._refuses("o/r", f"refs/heads/x {'a' * 40} refs/heads/{TE_BRANCH} {'0' * 40}", {}))
        refusal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
