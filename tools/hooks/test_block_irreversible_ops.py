#!/usr/bin/env python3
"""Tests for block_irreversible_ops.py (harmonic-forge#245).

The previous version shipped with 40 passing tests and failed on its reviewer's
FIRST real command. Two structural omissions caused that, and both are covered
here as their own classes:

* **Multi-clause commands mixing a safe operation with an unrelated absolute
  path** -- `cd /home/mmangus/harmonic-forge && rm -rf node_modules`,
  `ls /home/mmangus && rm -rf /tmp/scratch`. Every one of the 40 original cases
  used a single clause, so the whole class was invisible.
* **The hook reading its own source or a quoted mention.** `grep -rn 'rm -rf'
  tools/` was denied. Worse, one original case asserted that appending the
  *sentence* "git push --force" to a notes file SHOULD be challenged -- the
  self-test ratified the defect rather than catching it.

Both classes are now guarded by construction rather than by rule count: rules
inspect parsed tokens per invocation, so a string in an argument is data.

`gh issue close`/`gh pr merge` moved out entirely to `batch_gate.py`/
`batch_auth.decide()` (harmonic-forge#336's reforge, 2026-08-22) -- this hook
now covers only `git clean -fd`/`-xfd`. Their tests moved to
`test_batch_auth.py`/`test_batch_gate.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "block_irreversible_ops.py"
sys.path.insert(0, str(HOOK.parent))
import block_irreversible_ops as hook  # noqa: E402


def concerns(command: str) -> list[str]:
    result = hook.find_concerns(command)
    return [] if result is None else result


class AsksTests(unittest.TestCase):
    """The one genuinely unrecoverable operation this hook still covers."""

    def test_git_clean_fd(self):
        self.assertTrue(concerns("git clean -fd"))

    def test_git_clean_xfd_and_bundled_orders(self):
        for cmd in ("git clean -xfd", "git clean -fdx", "git clean -dfx",
                    "git clean --force --directory"):
            with self.subTest(cmd=cmd):
                self.assertTrue(concerns(cmd), cmd)

    def test_git_clean_behind_global_flags(self):
        """harmonic-forge#369: `tokens[1:2]` used to pin the subcommand to
        index 1, so any global flag before `clean` bypassed this entirely.
        `git -C` is the standing idiom for agents working in worktrees --
        exactly the population this guards."""
        for cmd in (
            "git -C /tmp/x clean -fd",
            "git -C /tmp/scratch clean -xfd",
            "git --git-dir=/tmp/x/.git clean -fd",
            "git --git-dir /tmp/x/.git clean -fd",
            "git --work-tree=/tmp/x clean -xfd",
            "git --work-tree /tmp/x clean -xfd",
            "git -c core.pager=cat clean -fd",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(concerns(cmd), cmd)

    def test_git_clean_dry_run_stays_unprompted(self):
        """`-n`/`--dry-run` deletes nothing -- no regression from the
        global-flag generalization above."""
        for cmd in ("git clean -n", "git clean --dry-run",
                    "git -C /tmp/x clean -n"):
            with self.subTest(cmd=cmd):
                self.assertFalse(concerns(cmd), cmd)


class MultiClauseTests(unittest.TestCase):
    """The class all 40 original tests omitted, and that broke round 4."""

    def test_safe_op_with_an_unrelated_absolute_path(self):
        for cmd in (
            "cd /home/mmangus/harmonic-forge && rm -rf node_modules",
            "ls /home/mmangus && rm -rf /tmp/scratch",
            "cd /home/mmangus && npm test",
            "cd ~ && ls -la",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(concerns(cmd), [], cmd)

    def test_a_flagged_op_in_ONE_clause_is_still_caught(self):
        self.assertTrue(concerns("cd /tmp/repo && git clean -fd"))

    def test_a_safe_clause_does_not_suppress_a_later_flagged_one(self):
        self.assertTrue(concerns("git status && git clean -fd"))

    def test_two_distinct_clauses_report_once_each(self):
        found = concerns("git clean -fd && git status && git clean -xfd")
        self.assertEqual(len(found), 1)  # same reason string, deduped

    def test_pipelines_and_semicolons_segment_correctly(self):
        self.assertEqual(concerns("git log | head -5; ls /home/mmangus"), [])
        self.assertTrue(concerns("git log | head -5; git clean -fd"))


class NotDataTests(unittest.TestCase):
    """A mention of an operation is not the operation.

    The original suite asserted the opposite -- writing the sentence
    "git push --force" into a file was encoded as correct-to-challenge.
    """

    def test_a_quoted_mention_is_not_an_invocation(self):
        for cmd in (
            "grep -rn 'rm -rf' /home/mmangus/harmonic-forge/tools",
            "grep -rn 'git clean -fd' tools/hooks",
            "echo 'git push --force' >> notes.md",
            'git commit -m "document git clean -fd in the runbook"',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(concerns(cmd), [], cmd)

    def test_a_heredoc_body_is_masked_not_parsed(self):
        cmd = ("cat > /tmp/notes.md <<'EOF'\n"
               "Run git clean -fd to wipe untracked files.\n"
               "EOF")
        self.assertEqual(concerns(cmd), [])

    def test_the_hook_can_read_its_own_source(self):
        """Round 4's second denied call was running its own self-test."""
        self.assertEqual(concerns(f"python3 {HOOK}"), [])
        self.assertEqual(concerns(f"cat {HOOK}"), [])


class NotFlaggedTests(unittest.TestCase):
    """Deliberately dropped: reflog-recoverable, so not worth the friction."""

    def test_reflog_recoverable_operations_are_not_challenged(self):
        for cmd in ("git reset --hard HEAD~1", "git branch -D feature/x",
                    "git stash drop", "git push --force origin main",
                    "git push --force-with-lease"):
            with self.subTest(cmd=cmd):
                self.assertEqual(concerns(cmd), [], cmd)

    def test_rm_is_not_handled_here_at_all(self):
        """permissions.deny owns this tier. Static parsing cannot resolve
        globs, command substitution or an earlier cd, so it must not try."""
        for cmd in ("rm -rf /", "rm -rf ~", "cd ~ && rm -rf .",
                    "rm -rf $(echo ~)", "rm -rf node_modules"):
            with self.subTest(cmd=cmd):
                self.assertEqual(concerns(cmd), [], cmd)

    def test_ordinary_read_only_work_passes(self):
        for cmd in ("gh issue list --repo vitalharmony/harmonic-forge",
                    "gh pr checks 1000", "git status", "git clean -n",
                    "git clean --dry-run", "ls -la", "npm run build",
                    "gh api repos/o/r/issues/1"):
            with self.subTest(cmd=cmd):
                self.assertEqual(concerns(cmd), [], cmd)

    def test_issue_close_and_pr_merge_are_no_longer_this_hooks_concern(self):
        """Moved entirely to batch_gate.py/batch_auth.decide()
        (harmonic-forge#336's reforge) -- this hook must not have an
        opinion on either any more, covered or not."""
        for cmd in (
            "gh issue close 245 --repo vitalharmony/hrse",
            "gh api repos/vitalharmony/hrse/issues/245 -X PATCH -f state=closed",
            "gh pr merge 993 --squash --delete-branch",
            "gh api -X PUT repos/vitalharmony/hrse/pulls/993/merge -f delete_branch=true",
            "gh pr merge 993 --squash",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(concerns(cmd), [], cmd)


class RobustnessTests(unittest.TestCase):
    def test_env_prefixes_and_absolute_paths_still_resolve_the_program(self):
        for cmd in ("env GH_TOKEN=x git clean -fd",
                    "GH_TOKEN=x git clean -fd",
                    "/usr/bin/git clean -fd"):
            with self.subTest(cmd=cmd):
                self.assertTrue(concerns(cmd), cmd)

    def test_an_untokenizable_command_fails_open(self):
        """A hook that blocks what it cannot parse gets unregistered."""
        self.assertIsNone(hook.find_concerns("echo 'unbalanced"))

    def test_empty_and_whitespace_commands_are_safe(self):
        self.assertEqual(concerns(""), [])
        self.assertEqual(concerns("   \n  "), [])


class EndToEndTests(unittest.TestCase):
    """Through the real stdin/stdout contract, not just find_concerns."""

    def _run(self, payload: dict) -> tuple[int, str]:
        proc = subprocess.run([sys.executable, str(HOOK)],
                              input=json.dumps(payload), capture_output=True,
                              text=True)
        return proc.returncode, proc.stdout

    def test_a_flagged_command_emits_ask(self):
        code, out = self._run({"tool_name": "Bash",
                               "tool_input": {"command": "git clean -fd"}})
        self.assertEqual(code, 0)
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "ask")
        self.assertIn("untracked", decision["permissionDecisionReason"].lower())

    def test_a_safe_command_emits_nothing(self):
        code, out = self._run({"tool_name": "Bash",
                               "tool_input": {"command": "git status"}})
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_a_non_bash_tool_is_ignored(self):
        code, out = self._run({"tool_name": "Read",
                               "tool_input": {"file_path": "/etc/passwd"}})
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_malformed_stdin_fails_open(self):
        proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
