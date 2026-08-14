#!/usr/bin/env python3
"""Tests for block_stale_script_execution.py (hrse#861).

Every defect the independent review found is pinned here as a test.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import block_stale_script_execution as hook  # noqa: E402

HOOK = Path(__file__).resolve().parent / "block_stale_script_execution.py"
SCRIPT = "scripts/1-backfill_activity_direction.py"
PLAIN = "scripts/run_health_scheduler.py"


def bash(command: str, cwd: str = "/tmp") -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def run_hook(payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout or "{}")


class ScriptDetectionTests(unittest.TestCase):
    def only(self, command: str) -> str | None:
        found = hook.analyze(command, None)
        return found[0][0] if found else None

    def test_python3(self) -> None:
        self.assertEqual(self.only(f"python3 {SCRIPT} --report"), SCRIPT)

    def test_versioned_interpreter(self) -> None:
        self.assertEqual(self.only(f"python3.11 {SCRIPT}"), SCRIPT)

    def test_direct_execution_normalises_dot_slash(self) -> None:
        self.assertEqual(self.only(f"./{SCRIPT} --report"), SCRIPT)

    def test_shell_script(self) -> None:
        self.assertEqual(self.only("bash tools/setup.sh"), "tools/setup.sh")

    def test_full_interpreter_path(self) -> None:
        self.assertEqual(self.only(f"/usr/bin/python3 {SCRIPT}"), SCRIPT)

    def test_venv_interpreter(self) -> None:
        self.assertEqual(self.only(f"backend/.venv/bin/python {SCRIPT}"), SCRIPT)

    # --- wrapper prefixes: all previously missed entirely ---
    def test_env_prefix(self) -> None:
        self.assertEqual(self.only(f"env python3 {SCRIPT}"), SCRIPT)

    def test_sudo_prefix(self) -> None:
        self.assertEqual(self.only(f"sudo python3 {SCRIPT}"), SCRIPT)

    def test_nohup_prefix(self) -> None:
        self.assertEqual(self.only(f"nohup python3 {SCRIPT}"), SCRIPT)

    def test_var_assignment_prefix(self) -> None:
        self.assertEqual(self.only(f"PYTHONPATH=. python3 {SCRIPT}"), SCRIPT)

    def test_timeout_with_duration(self) -> None:
        self.assertEqual(self.only(f"timeout 5 python3 {SCRIPT}"), SCRIPT)

    def test_uv_run(self) -> None:
        self.assertEqual(self.only(f"uv run {SCRIPT}"), SCRIPT)

    def test_value_taking_short_option(self) -> None:
        """-X consumes its value; the script follows it."""
        self.assertEqual(self.only(f"python3 -X importtime {SCRIPT}"), SCRIPT)

    def test_module_invocation_is_not_a_script(self) -> None:
        self.assertIsNone(self.only("python3 -m pytest"))

    def test_py_as_a_flag_value_is_not_the_script(self) -> None:
        found = hook.analyze(f"python3 {SCRIPT} --manifest other.py", None)
        self.assertEqual([f[0] for f in found], [SCRIPT])

    def test_non_script_commands(self) -> None:
        for command in ("git status", "ls scripts/", "echo python3 x.py"):
            with self.subTest(command=command):
                self.assertEqual(hook.analyze(command, None), [])

    def test_unparseable_returns_none(self) -> None:
        self.assertIsNone(hook.analyze("python3 x.py --note 'unclosed", None))


class CwdTrackingTests(unittest.TestCase):
    """The review's C1: `cd X && ...` was ignored, both directions."""

    def test_cd_changes_the_effective_cwd(self) -> None:
        found = hook.analyze(f"cd /tmp/impl && python3 {SCRIPT} --execute", "/base")
        self.assertEqual(found, [(SCRIPT, "/tmp/impl", True)])

    def test_relative_cd_resolves_against_payload_cwd(self) -> None:
        found = hook.analyze(f"cd sub && python3 {SCRIPT}", "/base")
        self.assertEqual(found[0][1], "/base/sub")

    def test_without_cd_the_payload_cwd_is_used(self) -> None:
        found = hook.analyze(f"python3 {SCRIPT}", "/base")
        self.assertEqual(found[0][1], "/base")


class WriteModeTests(unittest.TestCase):
    def writes(self, command: str) -> bool:
        return hook.analyze(command, None)[0][2]

    def test_execute_flag(self) -> None:
        self.assertTrue(self.writes(f"python3 {PLAIN} --execute"))

    def test_report_flag_is_read_only(self) -> None:
        self.assertFalse(self.writes(f"python3 {SCRIPT} --report"))

    def test_flag_name_inside_a_quoted_value_does_not_count(self) -> None:
        """The review's C2 false-deny."""
        self.assertFalse(self.writes(
            f"python3 {SCRIPT} --report --thread-id 'skip --execute for now'"))

    def test_write_flag_in_another_segment_does_not_leak(self) -> None:
        found = hook.analyze(
            f"python3 {SCRIPT} --report && python3 {PLAIN} --execute", None)
        self.assertEqual([f[2] for f in found], [False, True])

    def test_migration_prefix_writes_by_default(self) -> None:
        """13 scripts/1-* write to the graph with no gating flag."""
        self.assertTrue(self.writes(f"python3 {SCRIPT}"))

    def test_non_migration_script_without_flags_is_read_only(self) -> None:
        self.assertFalse(self.writes(f"python3 {PLAIN}"))

    def test_read_only_migration_diagnostics_are_not_writes(self) -> None:
        """Review F1: these have no flags, so they could never opt out."""
        for script in ("scripts/1-verify_migration.py",
                       "scripts/1-diagnostic_threads.py",
                       "scripts/1-retrigger_dan.py"):
            with self.subTest(script=script):
                self.assertFalse(hook.segment_is_write(["python3", script], script))

    def test_unprefixed_write_script_is_a_write(self) -> None:
        """Review F3: 21 write statements, no 1- prefix, no gating flag."""
        s = "scripts/migrate_communication_channels.py"
        self.assertTrue(hook.segment_is_write(["python3", s], s))

    def test_mutates_live_is_a_write_flag(self) -> None:
        self.assertTrue(hook.segment_is_write(
            ["python3", "scripts/l1_post.py", "--mutates-live"], "scripts/l1_post.py"))

    def test_warn_reaches_the_model_not_only_the_user(self) -> None:
        """Review F2: additionalContext is the field the model reads."""
        with mock.patch("builtins.print") as printed:
            hook._warn("careful")
        payload = json.loads(printed.call_args[0][0])
        self.assertEqual(payload["systemMessage"], "careful")
        self.assertEqual(
            payload["hookSpecificOutput"]["additionalContext"], "careful")
        self.assertNotIn("permissionDecision", payload["hookSpecificOutput"])

    def test_prefixed_flag_does_not_match(self) -> None:
        self.assertFalse(self.writes(f"python3 {PLAIN} --write-report x"))


class StalenessTests(unittest.TestCase):
    def test_head_contains_only_exit_1_means_not_ancestor(self) -> None:
        """The review's C7: exit 128 is an error and must fail open."""
        for code, expected in ((0, True), (1, False), (128, True)):
            with self.subTest(code=code):
                proc = subprocess.CompletedProcess([], code, "", "")
                with mock.patch.object(hook, "_git", return_value=proc):
                    self.assertEqual(hook.head_contains("abc", None), expected)

    def test_head_contains_fails_open_when_git_missing(self) -> None:
        with mock.patch.object(hook, "_git", return_value=None):
            self.assertTrue(hook.head_contains("abc", None))

    def test_untracked_path_has_no_upstream_commit(self) -> None:
        proc = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(hook, "_git", return_value=proc):
            self.assertIsNone(hook.upstream_commit_for("x.py", None))


class DecisionTests(unittest.TestCase):
    def _decide(self, command: str, stale: bool, env: dict | None = None) -> dict:
        commit = "6a2d907deadbeef"
        with mock.patch.object(hook, "upstream_commit_for", return_value=commit), \
             mock.patch.object(hook, "head_contains", return_value=not stale), \
             mock.patch.dict("os.environ", env or {}, clear=False), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    def _denied(self, payload: dict) -> bool:
        return (payload.get("hookSpecificOutput") or {}).get(
            "permissionDecision") == "deny"

    def test_stale_write_denies(self) -> None:
        payload = self._decide(f"python3 {SCRIPT} --execute", stale=True)
        self.assertTrue(self._denied(payload))
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("6a2d907", reason)
        self.assertIn(hook.OVERRIDE_ENV, reason)

    def test_stale_read_warns_and_allows(self) -> None:
        payload = self._decide(f"python3 {SCRIPT} --report", stale=True)
        self.assertFalse(self._denied(payload))
        self.assertIn("Warning", payload["systemMessage"])

    def test_current_checkout_allows_silently(self) -> None:
        self.assertEqual(self._decide(f"python3 {SCRIPT} --execute", stale=False), {})

    def test_override_env_allows(self) -> None:
        """The review's C3: a deliberately-pinned worktree needs an out."""
        payload = self._decide(f"python3 {SCRIPT} --execute", stale=True,
                               env={hook.OVERRIDE_ENV: "1"})
        self.assertEqual(payload, {})

    def test_non_script_command_allows(self) -> None:
        self.assertEqual(self._decide("git status", stale=True), {})


class ProcessTests(unittest.TestCase):
    def test_non_bash_allowed(self) -> None:
        self.assertEqual(run_hook({"tool_name": "Read"}), {})

    def test_malformed_payload_allowed(self) -> None:
        result = subprocess.run([sys.executable, str(HOOK)], input="not json",
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout or "{}"), {})

    def test_unrelated_bash_allowed(self) -> None:
        self.assertEqual(run_hook(bash("git status")), {})

    def test_cwd_outside_a_repo_allows(self) -> None:
        self.assertEqual(run_hook(bash(f"python3 {SCRIPT} --execute", "/tmp")), {})


if __name__ == "__main__":
    unittest.main()
