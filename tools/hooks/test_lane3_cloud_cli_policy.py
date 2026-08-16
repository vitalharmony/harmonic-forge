#!/usr/bin/env python3
"""Tests for hrse#327's Lane 3 cloud-CLI policy.

The live gate that motivated this issue ran `kubectl delete pod <nonexistent>`
and got `NotFound` back from the real Kubernetes API — i.e. nothing refused
it, the API just had no such object. Every deny case below is the shape that
gate should have hit instead.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lane3_cloud_cli_policy as policy

HOOK = Path(__file__).resolve().parent / "lane3_cloud_cli_policy.py"


class SafePrefixTests(unittest.TestCase):
    """AC1 — mirrors .devin/agents/lane3-gate/AGENT.md:67-76 exactly."""

    def test_every_safe_prefix_is_allowed(self):
        for prefix in policy.SAFE_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertIsNone(policy.denial_reason(" ".join(prefix) + " some-target"))

    def test_the_list_is_the_devin_list(self):
        self.assertEqual(len(policy.SAFE_PREFIXES), 10)
        self.assertIn(("kubectl", "get"), policy.SAFE_PREFIXES)
        self.assertIn(("doctl", "kubernetes", "cluster", "list"), policy.SAFE_PREFIXES)
        # Never a bare guarded program — that would match delete/create/apply.
        self.assertNotIn(("kubectl",), policy.SAFE_PREFIXES)
        self.assertNotIn(("doctl",), policy.SAFE_PREFIXES)


class MutatingCommandTests(unittest.TestCase):
    """AC2 — refused by the mechanism itself, not by a downstream NotFound."""

    def test_bare_mutating_commands_are_denied(self):
        for command in (
            "kubectl delete pod p", "kubectl apply -f x.yaml", "kubectl create ns foo",
            "doctl kubernetes cluster delete abc", "doctl compute droplet delete 1",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(policy.denial_reason(command))

    def test_guarded_program_with_no_subcommand_is_denied(self):
        self.assertIsNotNone(policy.denial_reason("kubectl"))
        self.assertIsNotNone(policy.denial_reason("doctl"))

    def test_denial_names_the_command_and_the_allowed_alternatives(self):
        reason = policy.denial_reason("kubectl delete pod p")
        self.assertIn("kubectl delete", reason)
        self.assertIn("kubectl get", reason)
        self.assertIn("hrse#327", reason)


class CompoundCommandTests(unittest.TestCase):
    """The bypass class segment-awareness exists to close."""

    def test_and_chained_mutation_is_denied(self):
        self.assertIsNotNone(policy.denial_reason("true && kubectl delete ns/foo"))

    def test_semicolon_chained_mutation_is_denied(self):
        self.assertIsNotNone(policy.denial_reason("echo x; doctl kubernetes cluster delete abc"))

    def test_a_safe_command_chained_after_a_mutation_is_still_denied(self):
        self.assertIsNotNone(policy.denial_reason("kubectl delete pod p && kubectl get nodes"))

    def test_two_safe_commands_chained_are_allowed(self):
        self.assertIsNone(policy.denial_reason("kubectl get nodes && doctl kubernetes cluster list"))


class WrapperBypassTests(unittest.TestCase):
    """hrse#327 NC2 — all four confirmed live against the real parser, each
    of which puts the WRAPPER in segment[0] and hides the real program."""

    def test_env_assignment_wrapper_is_resolved_through(self):
        self.assertIsNotNone(policy.denial_reason("env KUBECONFIG=x kubectl delete pod p"))

    def test_sudo_wrapper_is_resolved_through(self):
        self.assertIsNotNone(policy.denial_reason("sudo kubectl delete pod p"))

    def test_timeout_wrapper_and_its_operand_are_resolved_through(self):
        self.assertIsNotNone(policy.denial_reason("timeout 30 kubectl delete pod p"))

    def test_xargs_wrapper_is_resolved_through(self):
        self.assertIsNotNone(policy.denial_reason("xargs kubectl delete pod"))

    def test_bare_variable_assignment_prefix_is_resolved_through(self):
        self.assertIsNotNone(policy.denial_reason("KUBECONFIG=x kubectl delete pod p"))

    def test_absolute_path_is_resolved_by_basename(self):
        # Stricter than the Devin file's string-prefix match, deliberately.
        self.assertIsNotNone(policy.denial_reason("/usr/bin/kubectl delete pod p"))

    def test_wrappers_do_not_block_a_safe_command(self):
        self.assertIsNone(policy.denial_reason("timeout 30 kubectl get nodes"))


class FailClosedTests(unittest.TestCase):
    """hrse#327 NC1 — a safety guard fails closed, unlike the quality guards."""

    def test_unparseable_command_is_denied(self):
        # An unbalanced quote makes shlex raise; the guard must deny, not crash
        # and not fall through to allow.
        reason = policy.denial_reason('kubectl delete pod "unterminated')
        self.assertIsNotNone(reason)
        self.assertIn("could not be parsed", reason)

    def test_non_string_input_is_denied_rather_than_raising(self):
        self.assertIsNotNone(policy.denial_reason(None))  # type: ignore[arg-type]


class NoOpinionTests(unittest.TestCase):
    def test_non_guarded_programs_get_no_opinion(self):
        for command in ("git status", "mise run check", "ls -la", "python3 script.py"):
            with self.subTest(command=command):
                self.assertIsNone(policy.denial_reason(command))

    def test_heredoc_prose_mentioning_a_mutation_is_not_a_command(self):
        # shell_parse masks heredoc bodies, so prose about kubectl delete in a
        # report body must not be read as an attempt to run it.
        command = "cat <<'EOF' > report.md\nWe must never run kubectl delete pod x\nEOF"
        self.assertIsNone(policy.denial_reason(command))


class HookEntryPointTests(unittest.TestCase):
    """The Claude-side wiring: LANE gating and the deny payload shape."""

    def _run(self, command: str, lane: str | None, cwd: str = "/tmp") -> str:
        import os

        env = dict(os.environ)
        if lane is None:
            env.pop("LANE", None)
        else:
            env["LANE"] = lane
        payload = json.dumps({"tool_name": "Bash", "cwd": cwd,
                              "tool_input": {"command": command}})
        result = subprocess.run([sys.executable, str(HOOK)], input=payload,
                                text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_lane3_denies_a_mutating_command_with_the_pretooluse_shape(self):
        out = self._run("kubectl delete pod p", lane="3")
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("hrse#327", decision["permissionDecisionReason"])

    def test_lane3_allows_a_safe_command_silently(self):
        self.assertEqual(self._run("kubectl get nodes", lane="3").strip(), "")

    def test_lane2_gets_no_opinion_even_on_a_mutating_command(self):
        # AC3: a Lane-3-scoped concern. LANE explicitly set to 2 is positive
        # evidence this is not Lane 3.
        self.assertEqual(self._run("kubectl delete pod p", lane="2").strip(), "")

    def test_non_bash_tool_is_ignored(self):
        payload = json.dumps({"tool_name": "Read", "cwd": "/tmp",
                              "tool_input": {"command": "kubectl delete pod p"}})
        result = subprocess.run([sys.executable, str(HOOK)], input=payload,
                                text=True, capture_output=True)
        self.assertEqual(result.stdout.strip(), "")

    def test_malformed_payload_does_not_crash(self):
        result = subprocess.run([sys.executable, str(HOOK)], input="not json",
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
