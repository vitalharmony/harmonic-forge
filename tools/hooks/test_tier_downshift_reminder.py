#!/usr/bin/env python3
"""Unit tests for tier_downshift_reminder.py (F769)."""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import model_tier_gate  # noqa: E402
import tier_downshift_reminder as reminder  # noqa: E402
import tier_model_trigger_check  # noqa: E402
import worktree_issue  # noqa: E402


class DownshiftReminderTests(unittest.TestCase):
    payload = {"transcript_path": "/transcript", "cwd": "/cwd"}

    def run_hook(self, *, lane="2", model="claude-opus-5", branch=False, posted=False):
        with patch.object(reminder.os.path, "isfile", return_value=True), \
             patch.object(reminder, "_is_deep_branch", return_value=branch), \
             patch.object(reminder, "_posted_deep", return_value=posted), \
             patch.object(reminder, "_probe_in_hand", return_value=False):
            return reminder.run(self.payload, env={"LANE": lane} if lane else {}, model=model)

    def test_high_tier_without_branch_issue_reminds(self):
        out = self.run_hook()
        self.assertEqual(out["systemMessage"],
                         "This lane is on claude-opus-5 with no deep-tier issue in hand. "
                         "Switch down: /model sonnet")

    def test_deep_branch_is_silent(self):
        self.assertIsNone(self.run_hook(branch=True))

    def test_deep_post_is_silent(self):
        self.assertIsNone(self.run_hook(posted=True))

    def test_sonnet_is_silent(self):
        self.assertIsNone(self.run_hook(model="claude-sonnet-5"))

    def test_lane_unset_is_silent(self):
        self.assertIsNone(self.run_hook(lane=""))

    def test_board_failure_is_silent(self):
        with patch.object(reminder.os.path, "isfile", return_value=True), \
             patch.object(reminder, "_is_deep_branch",
                          side_effect=RuntimeError("board unavailable")):
            self.assertIsNone(reminder.run(self.payload, env={"LANE": "2"},
                                            model="claude-opus-5"))

    def test_post_lookup_failure_is_silent(self):
        with patch.object(reminder.os.path, "isfile", return_value=True), \
             patch.object(reminder, "_is_deep_branch", return_value=False), \
             patch.object(reminder, "_posted_deep", side_effect=RuntimeError("board unavailable")):
            self.assertIsNone(reminder.run(self.payload, env={"LANE": "2"},
                                            model="claude-opus-5"))

    def test_missing_transcript_is_silent(self):
        self.assertIsNone(reminder.run(self.payload, env={"LANE": "2"},
                                        model="claude-opus-5"))

    def test_posted_deep_uses_backstop_posts_and_target_lookup(self):
        calls = []
        with patch.object(reminder.backstop, "scan_turn", return_value=([("post", None, "a")], False)), \
             patch.object(reminder.backstop, "posted_targets",
                          return_value=[("vitalharmony/hrse", 1)]), \
             patch.object(reminder.tier_model_trigger_check, "_boards", return_value={}), \
             patch.object(reminder.tier_model_trigger_check, "lookup_tier",
                          side_effect=lambda repo, issue, boards: (calls.append((repo, issue)) or ("deep", None))):
            self.assertTrue(reminder._posted_deep("/transcript", "/cwd"))
        self.assertEqual(calls, [("vitalharmony/hrse", 1)])

    def test_posted_board_failure_raises_for_quiet_caller(self):
        with patch.object(reminder.backstop, "scan_turn", return_value=([("post", None, "a")], False)), \
             patch.object(reminder.backstop, "posted_targets",
                          return_value=[("vitalharmony/hrse", 1)]), \
             patch.object(reminder.tier_model_trigger_check, "_boards", return_value={}), \
             patch.object(reminder.tier_model_trigger_check, "lookup_tier",
                          return_value=(model_tier_gate.LOOKUP_FAILED, "403")):
            with self.assertRaises(RuntimeError):
                reminder._posted_deep("/transcript", "/cwd")

    def test_truncated_turn_scan_raises_for_quiet_caller(self):
        with patch.object(reminder.backstop, "scan_turn", return_value=([], True)):
            with self.assertRaises(RuntimeError):
                reminder._posted_deep("/transcript", "/cwd")

    def test_post_cap_raises_for_quiet_caller(self):
        calls = [(f"post{i}", None, str(i)) for i in range(reminder._MAX_POST_TIER_READS + 1)]
        with patch.object(reminder.backstop, "scan_turn", return_value=(calls, False)), \
             patch.object(reminder.backstop, "posted_targets",
                          side_effect=lambda command, cwd_repo, cwd=None: [("vitalharmony/hrse", int(command[4:]))]):
            with self.assertRaises(RuntimeError):
                reminder._posted_deep("/transcript", "/cwd")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class InHandProbeTests(unittest.TestCase):
    """harmonic-forge#843 AC2/AC3/AC5, over real fixture git repos and
    worktrees. Only the board Tier and the issue-state reads are stubbed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.wt_root = self.root / ".worktrees"
        self.wt_root.mkdir()
        self.lane2 = self.make_repo("HRSE2-lane2", "https://github.com/vitalharmony/hrse.git")
        self.forge = self.make_repo("harmonic-forge",
                                    "https://github.com/vitalharmony/harmonic-forge.git")
        patcher = patch.object(worktree_issue, "_IMPL_ROOTS", (str(self.wt_root), "/nonexistent"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tiers: dict = {}
        self.states: dict = {}
        self.reads: list = []
        self.fail_git = None

    def make_repo(self, name, origin):
        repo = self.root / name
        repo.mkdir()
        git("init", "-q", "-b", "main", cwd=repo)
        git("remote", "add", "origin", origin, cwd=repo)
        (repo / "f").write_text("x")
        git("add", "f", cwd=repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed", cwd=repo)
        return repo

    def worktree(self, repo, name, branch=None):
        path = self.wt_root / name
        if branch:
            git("worktree", "add", "-q", "-b", branch, str(path), cwd=repo)
        else:
            git("worktree", "add", "-q", "--detach", str(path), cwd=repo)
        return path

    def transcript(self, *commands):
        path = self.root / "t.jsonl"
        entries = [{"type": "user", "message": {"role": "user", "content": "answer my question"}}]
        for i, command in enumerate(commands):
            entries.append({"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5",
                "content": [{"type": "tool_use", "id": f"u{i}", "name": "Bash", "input": {"command": command}}]}})
            entries.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"u{i}", "content": "ok"}]}})
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return str(path)

    def run_hook(self, transcript, lane="2"):
        real_timed_run = model_tier_gate.timed_run

        def lookup(repo, number, boards, ttl=0):
            self.reads.append(("tier", repo, number, ttl))
            tier = self.tiers.get((repo, number))
            return (tier, "HTTP 403") if tier is model_tier_gate.LOOKUP_FAILED else (tier, None)

        def timed_run(cmd, timeout=3, env=None):
            if cmd[0] == "gh":
                repo_issue = cmd[2].split("repos/", 1)[1]
                repo, number = repo_issue.rsplit("/issues/", 1)
                self.reads.append(("state", repo, int(number)))
                state = self.states.get((repo, int(number)), "open")
                if state is None:
                    return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502")
                return subprocess.CompletedProcess(cmd, 0, state + "\n", "")
            if self.fail_git and tuple(cmd[3:5]) == self.fail_git:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: dubious ownership")
            return real_timed_run(cmd, timeout=timeout, env=env)

        env = {"LANE": lane, "HARMONIC_FORGE_ROOT": str(self.forge)}
        with patch.object(tier_model_trigger_check, "lookup_tier", side_effect=lookup), \
             patch.object(tier_model_trigger_check, "_boards", return_value={}), \
             patch.object(model_tier_gate, "timed_run", side_effect=timed_run), \
             patch.object(reminder, "_is_deep_branch", return_value=False), \
             patch.object(reminder, "_posted_deep", return_value=False):
            return reminder.run({"transcript_path": transcript, "cwd": str(self.lane2)},
                                env=env, model="claude-opus-5")

    def test_hrse1908_shape_is_silent(self):
        """Session cwd HRSE2-lane2, an open deep issue's impl worktree, and a
        turn that only answers a question: no reminder."""
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.tiers[("vitalharmony/hrse", 1908)] = "deep"
        self.assertIsNone(self.run_hook(self.transcript()))
        self.assertEqual(self.reads[0], ("tier", "vitalharmony/hrse", 1908, model_tier_gate._CACHE_TTL))

    def test_closed_issue_reminds(self):
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.tiers[("vitalharmony/hrse", 1908)] = "deep"
        self.states[("vitalharmony/hrse", 1908)] = "closed"
        self.assertIn("Switch down", self.run_hook(self.transcript())["systemMessage"])

    def test_standard_tier_reminds_without_a_state_read(self):
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.tiers[("vitalharmony/hrse", 1908)] = "standard"
        self.assertIn("Switch down", self.run_hook(self.transcript())["systemMessage"])
        self.assertNotIn("state", [r[0] for r in self.reads])

    def test_branch_naming_another_repos_issue_wins(self):
        """Repo AND number come from the branch: the path says hrse2-999."""
        path = self.worktree(self.lane2, "hrse2-999-impl", "l2/f843-downshift")
        self.assertEqual(worktree_issue.issue_for_worktree(str(path)),
                         ("vitalharmony/harmonic-forge", 843))

    def test_impl_worktree_filter(self):
        root = str(self.wt_root)
        self.assertTrue(worktree_issue.is_impl_worktree(f"{root}/hrse2-1908-impl"))
        self.assertFalse(worktree_issue.is_impl_worktree(f"{root}/hrse2-1908-review"))
        self.assertFalse(worktree_issue.is_impl_worktree(f"{root}/nested/hrse2-1908-impl"))
        self.assertFalse(worktree_issue.is_impl_worktree(str(self.lane2)))

    def test_tier_lookup_failure_is_silent(self):
        """AC4: a failed board read must suppress, never remind."""
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.tiers[("vitalharmony/hrse", 1908)] = model_tier_gate.LOOKUP_FAILED
        self.assertIsNone(self.run_hook(self.transcript()))

    def test_issue_state_read_failure_is_silent(self):
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.tiers[("vitalharmony/hrse", 1908)] = "deep"
        self.states[("vitalharmony/hrse", 1908)] = None  # gh read fails
        self.assertIsNone(self.run_hook(self.transcript()))

    def test_git_failure_inside_a_repo_is_silent(self):
        """#843 preclose: a nonzero `git worktree list` in a real repo is
        undecidable, not "no worktrees"."""
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.tiers[("vitalharmony/hrse", 1908)] = "deep"
        self.fail_git = ("worktree", "list")
        self.assertIsNone(self.run_hook(self.transcript()))

    def test_chained_relative_cd_into_a_worktree_counts(self):
        """#843 preclose: `cd .worktrees && cd hrse2-1908-impl` resolves the
        second hop against the first."""
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.tiers[("vitalharmony/hrse", 1908)] = "deep"
        self.assertIsNone(self.run_hook(
            self.transcript("cd ../.worktrees && cd hrse2-1908-impl && git status"), lane="1"))

    def test_detached_head_falls_back_to_path_and_origin(self):
        path = self.worktree(self.forge, "forge-843-impl")
        self.assertEqual(worktree_issue.issue_for_worktree(str(path)),
                         ("vitalharmony/harmonic-forge", 843))
        self.tiers[("vitalharmony/harmonic-forge", 843)] = "deep"
        self.assertIsNone(self.run_hook(self.transcript()))

    def test_branch_naming_no_issue_is_not_counted(self):
        path = self.worktree(self.lane2, "hrse2-55-impl", "scratch")
        self.assertIsNone(worktree_issue.issue_for_worktree(str(path)))

    def test_lane1_ignores_worktrees_but_counts_a_cd_into_one(self):
        """AC2 is LANE=2 only; AC3 counts for every lane."""
        path = self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.tiers[("vitalharmony/hrse", 1908)] = "deep"
        self.assertIn("Switch down", self.run_hook(self.transcript(), lane="1")["systemMessage"])
        self.reads.clear()
        self.assertIsNone(self.run_hook(self.transcript(f"cd {path} && git status"), lane="1"))
        self.assertIsNone(self.run_hook(self.transcript(f"git -C {path} log -1"), lane="1"))

    def test_live_worktree_is_checked_before_stale_closed_ones(self):
        """#843 preclose: three leftover closed-deep worktrees would use the
        whole budget; the most recently used worktree is checked first."""
        for n in (826, 828, 2115):
            self.worktree(self.lane2, f"hrse2-{n}-impl", f"feat/{n}-x")
            self.tiers[("vitalharmony/hrse", n)] = "deep"
            self.states[("vitalharmony/hrse", n)] = "closed"
        time.sleep(1.1)
        live = self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        (live / "touched").write_text("x")
        git("add", "touched", cwd=live)
        self.tiers[("vitalharmony/hrse", 1908)] = "deep"
        self.assertIsNone(self.run_hook(self.transcript()))
        self.assertEqual(self.reads[0][1:3], ("vitalharmony/hrse", 1908))

    def test_read_budget_truncates_and_never_raises(self):
        for n in range(101, 108):
            self.worktree(self.lane2, f"hrse2-{n}-impl", f"feat/{n}-x")
            self.tiers[("vitalharmony/hrse", n)] = "deep"
            self.states[("vitalharmony/hrse", n)] = "closed"
        out = self.run_hook(self.transcript())
        self.assertLessEqual(len(self.reads), reminder._MAX_PROBE_READS)
        self.assertIn("Switch down", out["systemMessage"])

    def test_deadline_is_silent(self):
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        with patch.object(reminder, "_PROBE_DEADLINE_SECONDS", 0.2), \
             patch.object(worktree_issue, "issue_for_worktree", side_effect=lambda p: time.sleep(1)):
            self.assertIsNone(self.run_hook(self.transcript()))

    def test_broken_manifest_is_silent_not_a_traceback(self):
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        with patch.dict(sys.modules, {"watch_lane_posts": None}):
            self.assertIsNone(self.run_hook(self.transcript()))


if __name__ == "__main__":
    unittest.main()
