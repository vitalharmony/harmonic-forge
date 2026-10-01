#!/usr/bin/env python3
"""Unit tests for tier_downshift_reminder.py (F769)."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
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
        with patch.object(reminder.backstop, "scan_turn", return_value=([("post", None, "a")], False, [])), \
             patch.object(reminder.backstop, "posted_targets",
                          return_value=[("vitalharmony/hrse", 1)]), \
             patch.object(reminder.tier_model_trigger_check, "_boards", return_value={}), \
             patch.object(reminder.tier_model_trigger_check, "lookup_tier",
                          side_effect=lambda repo, issue, boards: (calls.append((repo, issue)) or ("deep", None))):
            self.assertTrue(reminder._posted_deep("/transcript", "/cwd"))
        self.assertEqual(calls, [("vitalharmony/hrse", 1)])

    def test_posted_board_failure_raises_for_quiet_caller(self):
        with patch.object(reminder.backstop, "scan_turn", return_value=([("post", None, "a")], False, [])), \
             patch.object(reminder.backstop, "posted_targets",
                          return_value=[("vitalharmony/hrse", 1)]), \
             patch.object(reminder.tier_model_trigger_check, "_boards", return_value={}), \
             patch.object(reminder.tier_model_trigger_check, "lookup_tier",
                          return_value=(model_tier_gate.LOOKUP_FAILED, "403")):
            with self.assertRaises(RuntimeError):
                reminder._posted_deep("/transcript", "/cwd")

    def test_truncated_turn_scan_raises_for_quiet_caller(self):
        with patch.object(reminder.backstop, "scan_turn", return_value=([], True, [])):
            with self.assertRaises(RuntimeError):
                reminder._posted_deep("/transcript", "/cwd")

    def test_post_cap_raises_for_quiet_caller(self):
        calls = [(f"post{i}", None, str(i)) for i in range(reminder._MAX_POST_TIER_READS + 1)]
        with patch.object(reminder.backstop, "scan_turn", return_value=(calls, False, [])), \
             patch.object(reminder.backstop, "posted_targets",
                          side_effect=lambda command, cwd_repo: [("vitalharmony/hrse", int(command[4:]))]):
            with self.assertRaises(RuntimeError):
                reminder._posted_deep("/transcript", "/cwd")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class InHandProbeTests(unittest.TestCase):
    """harmonic-forge#843 reforge (AC2, AC3, AC5): real fixture repos and
    worktrees; only the board Tier and the GraphQL state read are stubbed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.wt_root = self.root / ".worktrees"
        self.wt_root.mkdir()
        self.lane2 = self.make_repo("HRSE2-lane2", "https://github.com/vitalharmony/hrse.git")
        self.forge = self.make_repo("harmonic-forge",
                                    "https://github.com/vitalharmony/harmonic-forge.git")
        patcher = patch.object(worktree_issue, "_IMPL_ROOTS", (str(self.wt_root),))
        patcher.start()
        self.addCleanup(patcher.stop)
        reminder._TURN_SCANS.clear()
        self.tiers, self.states, self.graphql = {}, {}, []
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
        args = ["-b", branch] if branch else ["--detach"]
        git("worktree", "add", "-q", *args, str(path), cwd=repo)
        return path

    def transcript(self, *commands):
        path = self.root / f"t{len(commands)}{abs(hash(commands))}.jsonl"
        entries = [{"type": "user", "message": {"role": "user", "content": "answer my question"}}]
        for i, command in enumerate(commands):
            entries.append({"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5",
                "content": [{"type": "tool_use", "id": f"u{i}", "name": "Bash", "input": {"command": command}}]}})
            entries.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"u{i}", "content": "ok"}]}})
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return str(path)

    def run_hook(self, transcript, lane="2"):
        real = worktree_issue.model_tier_gate.timed_run

        def lookup(repo, number, boards, ttl=0):
            tier = self.tiers.get((repo, number))
            return (tier, "HTTP 403") if tier is model_tier_gate.LOOKUP_FAILED else (tier, None)

        def timed_run(cmd, timeout=3, env=None):
            if cmd[:3] == ["gh", "api", "graphql"]:
                query = cmd[-1]
                self.graphql.append(query)
                data = {}
                for alias, owner, name, number in re.findall(
                        r'(c\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\) '
                        r'\{ issue\(number: (\d+)\)', query):
                    state = self.states.get((f"{owner}/{name}", int(number)), "OPEN")
                    data[alias] = None if state is None else {"issue": {"state": state}}
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"data": data}), "")
            if self.fail_git and self.fail_git in " ".join(cmd):
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: dubious ownership")
            return real(cmd, timeout=timeout, env=env)

        env = {"LANE": lane, "HARMONIC_FORGE_ROOT": str(self.forge)}
        with patch.object(tier_model_trigger_check, "lookup_tier", side_effect=lookup), \
             patch.object(tier_model_trigger_check, "_boards", return_value={}), \
             patch.object(model_tier_gate, "timed_run", side_effect=timed_run), \
             patch.object(reminder, "_is_deep_branch", return_value=False):
            return reminder.run({"transcript_path": transcript, "cwd": str(self.lane2)},
                                env=env, model="claude-opus-5")

    def deep(self, repo, number, state="OPEN"):
        self.tiers[(repo, number)] = "deep"
        self.states[(repo, number)] = state

    def test_hrse1908_shape_is_silent(self):
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.deep("vitalharmony/hrse", 1908)
        self.assertIsNone(self.run_hook(self.transcript()))

    def test_closed_or_standard_reminds(self):
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.deep("vitalharmony/hrse", 1908, state="CLOSED")
        self.assertIn("Switch down", self.run_hook(self.transcript())["systemMessage"])
        self.tiers[("vitalharmony/hrse", 1908)] = "standard"
        self.assertIn("Switch down", self.run_hook(self.transcript())["systemMessage"])

    def test_branch_wins_over_the_path_for_repo_and_number(self):
        path = self.worktree(self.lane2, "hrse2-999-impl", "l2/f843-downshift")
        self.assertEqual(worktree_issue.issue_for_worktree(str(path)),
                         ("vitalharmony/harmonic-forge", 843))

    def test_detached_head_falls_back_to_path_number_and_origin(self):
        path = self.worktree(self.forge, "forge-843-impl")
        self.assertEqual(worktree_issue.issue_for_worktree(str(path)),
                         ("vitalharmony/harmonic-forge", 843))

    def test_non_repo_is_none_and_git_failure_inside_one_raises(self):
        self.assertIsNone(worktree_issue.issue_for_worktree(str(self.root / "nowhere")))
        path = self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.fail_git = "rev-parse --abbrev-ref"
        with patch.object(model_tier_gate, "timed_run",
                          return_value=subprocess.CompletedProcess([], 128, "", "fatal")):
            with self.assertRaises(RuntimeError):
                worktree_issue.issue_for_worktree(str(path))

    def test_git_failure_inside_a_worktree_is_silent(self):
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.tiers[("vitalharmony/hrse", 1908)] = "standard"
        self.fail_git = "worktree list"
        self.assertIsNone(self.run_hook(self.transcript()))

    def test_impl_worktree_filter(self):
        root = str(self.wt_root)
        self.assertTrue(worktree_issue.is_impl_worktree(f"{root}/hrse2-1908-impl"))
        self.assertFalse(worktree_issue.is_impl_worktree(f"{root}/hrse2-1908-review"))
        self.assertFalse(worktree_issue.is_impl_worktree(f"{root}/nested/hrse2-1908-impl"))

    def test_seven_deep_worktrees_one_open_is_one_read_and_silent(self):
        for n in (101, 103, 105, 107, 109, 111):
            self.worktree(self.lane2, f"hrse2-{n}-impl", f"feat/{n}-x")
            self.deep("vitalharmony/hrse", n, state="CLOSED")
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.deep("vitalharmony/hrse", 1908)
        self.assertIsNone(self.run_hook(self.transcript()))
        self.assertEqual(len(self.graphql), 1)

    def test_an_unreadable_alias_does_not_fail_the_others(self):
        self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.worktree(self.lane2, "hrse2-777-impl", "l2/f777-x")
        self.deep("vitalharmony/hrse", 1908)
        self.deep("vitalharmony/harmonic-forge", 777, state=None)  # alias returns null
        self.assertIsNone(self.run_hook(self.transcript()))
        # The distinguishing case: the readable issue is CLOSED and the other
        # alias is unreadable. A null is "unknown, not in hand" (NC3b), so the
        # reminder fires; failing the whole probe on one null would be silence.
        self.states[("vitalharmony/hrse", 1908)] = "CLOSED"
        reminder._TURN_SCANS.clear()
        self.assertIn("Switch down", self.run_hook(self.transcript("true"))["systemMessage"])

    def test_lane1_ignores_worktrees_but_counts_entering_one(self):
        path = self.worktree(self.lane2, "hrse2-1908-impl", "feat/1908-rule-editor")
        self.deep("vitalharmony/hrse", 1908)
        self.assertIn("Switch down", self.run_hook(self.transcript(), lane="1")["systemMessage"])
        for command in (f"cd {path} && git status", f"git -C {path} log -1",
                        "cd ../.worktrees && cd hrse2-1908-impl && ls"):
            with self.subTest(command=command):
                self.assertIsNone(self.run_hook(self.transcript(command), lane="1"))


if __name__ == "__main__":
    unittest.main()
