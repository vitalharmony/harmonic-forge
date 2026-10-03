#!/usr/bin/env python3
"""Tests for hrse-gate-run, the one auto-approved object for a Lane 3
production run (harmonic-forge#878 sticky-wicket PATCH).

Root ownership cannot be produced in a test, so the manifest's ownership check
is exercised by patching the expected path to a temp file (refused: not root)
and by stubbing `_immutable` for the cases that test later steps. Every
subprocess (broker, sudo, git) is stubbed; nothing here runs sudo.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hrse_gate_run as w  # noqa: E402

SHA = "c" * 40
RULE = "(hrse-gate) NOPASSWD: /usr/local/libexec/hrse-gate consume *"


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.worktree = root / "HRSE2-lane3"
        (self.worktree / "scripts").mkdir(parents=True)
        self.script = self.worktree / "scripts" / "gate_production_run.py"
        self.script.write_text("print('reviewed gate script')\n")
        self.manifest = {
            "worktree": str(self.worktree), "python": sys.executable,
            "script_sha256": hashlib.sha256(self.script.read_bytes()).hexdigest(),
            "broker_sha256": "b" * 64,
        }
        self.calls: list[list[str]] = []
        self.sudo_rules = [RULE]
        self.consume_rc = 0

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_run(self, argv):
        self.calls.append(argv)
        rc, out = 0, ""
        if argv[:2] == [str(w.BROKER), "--version"]:
            out = self.manifest["broker_sha256"]
        elif argv[:3] == ["sudo", "-n", "-l"]:
            out = "\n".join(f"    {r}" for r in self.sudo_rules)
        elif argv[:2] == ["git", "-C"]:
            out = SHA
        elif argv[:4] == ["sudo", "-n", "-u", "hrse-gate"]:
            rc, out = self.consume_rc, "consumed abc"
        return mock.Mock(returncode=rc, stdout=out, stderr="refused" if rc else "")

    def run_wrapper(self, *argv, lane="3"):
        with mock.patch.dict(os.environ, {"LANE": lane}), \
                mock.patch.object(w, "load_manifest", return_value=self.manifest), \
                mock.patch.object(w, "_run", side_effect=self._fake_run), \
                mock.patch.object(w.os, "execv") as execv, \
                mock.patch.object(w.os, "chdir"), \
                mock.patch("sys.stderr"):
            rc = w.main(list(argv))
        return rc, execv

    def consumed(self):
        return [c for c in self.calls if c[:4] == ["sudo", "-n", "-u", "hrse-gate"]]


class HappyPathTests(_Case):
    def test_apply_spends_one_grant_then_execs_the_verified_script_with_apply(self):
        rc, execv = self.run_wrapper("--issue", "1892", "--script", "scripts/1-1892-revive.py",
                                     "--mode", "apply")
        self.assertEqual(self.consumed()[0][-3:], ["1892", SHA, "script=scripts/1-1892-revive.py,apply"])
        argv = execv.call_args.args[1]
        self.assertEqual(argv[1], str(self.script))
        self.assertEqual(argv[-3:], ["--script", "scripts/1-1892-revive.py", "--apply"])

    def test_a_dry_run_and_a_count_name_their_own_actions(self):
        self.run_wrapper("--issue", "1892", "--script", "scripts/1-1892-revive.py")
        self.run_wrapper("--issue", "1867", "--count-label", "Task")
        actions = [c[-1] for c in self.consumed()]
        self.assertEqual(actions, ["script=scripts/1-1892-revive.py", "count-label=Task"])


class RefusalTests(_Case):
    def assert_refused_without_consuming(self, *argv, lane="3"):
        rc, execv = self.run_wrapper(*argv, lane=lane)
        self.assertEqual(rc, w.EXIT_REFUSED)
        self.assertEqual(self.consumed(), [], "a grant was spent on a refused run")
        execv.assert_not_called()

    def test_outside_lane3(self):
        self.assert_refused_without_consuming("--issue", "1", "--count-label", "Task", lane="1")

    def test_a_script_that_does_not_match_the_reviewed_digest(self):
        self.script.write_text("print('agent-edited')\n")
        self.assert_refused_without_consuming("--issue", "1", "--count-label", "Task")

    def test_a_stale_broker(self):
        self.manifest["broker_sha256"] = "d" * 64
        with mock.patch.object(w, "_run", side_effect=lambda a: mock.Mock(
                returncode=0, stdout="e" * 64, stderr="")):
            rc, _ = self.run_wrapper("--issue", "1", "--count-label", "Task")
        self.assertEqual(rc, w.EXIT_REFUSED)

    def test_a_widened_sudo_rule_on_the_host(self):
        """Survivor S5: the host's rules, not the install doc."""
        for rules in ([RULE, "(root) NOPASSWD: /usr/local/libexec/hrse-gate grant *"],
                      ["(root) NOPASSWD: /usr/local/libexec/hrse-gate consume *"],
                      []):
            with self.subTest(rules=rules):
                self.calls.clear()
                self.sudo_rules = rules
                self.assert_refused_without_consuming("--issue", "1", "--count-label", "Task")

    def test_a_non_migration_script_or_label(self):
        for argv in (("--script", "scripts/gate_production_run.py"), ("--script", "/etc/passwd"),
                     ("--count-label", "Task) DETACH DELETE (n")):
            with self.subTest(argv=argv):
                self.assert_refused_without_consuming("--issue", "1", *argv)

    def test_a_non_decimal_issue(self):
        self.assert_refused_without_consuming("--issue", "²", "--count-label", "Task")

    def test_mode_on_a_count(self):
        self.assert_refused_without_consuming("--issue", "1", "--count-label", "Task", "--mode", "apply")

    def test_no_grant_means_no_run(self):
        self.consume_rc = 3
        rc, execv = self.run_wrapper("--issue", "1", "--count-label", "Task")
        self.assertEqual(rc, w.EXIT_REFUSED)
        execv.assert_not_called()

    def test_a_manifest_the_agent_could_write_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = Path(tmp) / "manifest.json"
            m.write_text(json.dumps(self.manifest))
            with mock.patch.object(w, "MANIFEST", m):
                with self.assertRaises(w.Refused):
                    w.load_manifest()  # owned by this uid, not root


class PermissionDecisionTests(unittest.TestCase):
    """Survivor S2: evaluate the permission DECISION for the sanctioned command,
    not the bytes of one file. Claude Code checks deny, then ask, then allow; a
    `Bash(prefix:*)` rule matches a command prefix and a `Bash(glob)` rule a
    glob. HRSE2's tracked ask rules are read live when this checkout sits beside
    it, and fall back to the copy below (hrse#2119)."""

    HRSE2_ASK_FALLBACK = ["Bash(* --apply *)", "Bash(* --apply)"]
    POLICY = HERE.parent / "lane" / "policies" / "claude-lane3.json"

    @staticmethod
    def _matches(rule: str, command: str) -> bool:
        body = rule[len("Bash("):-1]
        if body.endswith(":*"):
            return command == body[:-2] or command.startswith(body[:-2] + " ")
        return fnmatch.fnmatchcase(command, body)

    def _hrse2_ask(self) -> list[str]:
        settings = Path.home() / "Harmonic_Projects" / "HRSE2" / ".claude" / "settings.json"
        if settings.is_file():
            ask = json.loads(settings.read_text()).get("permissions", {}).get("ask", [])
            return [r for r in ask if r.startswith("Bash(")]
        return self.HRSE2_ASK_FALLBACK

    def _decision(self, command: str) -> str:
        if any(self._matches(r, command) for r in self._hrse2_ask()):
            return "ask"
        allow = json.loads(self.POLICY.read_text())["permissions"]["allow"]
        return "allow" if any(self._matches(r, command) for r in allow) else "default"

    def test_the_sanctioned_commands_are_allowed_not_asked(self):
        for command in (
            "/usr/local/libexec/hrse-gate-run --issue 1892 --script scripts/1-1892-revive.py --mode apply",
            "/usr/local/libexec/hrse-gate-run --issue 1892 --script scripts/1-1892-revive.py",
            "/usr/local/libexec/hrse-gate-run --issue 1867 --count-label Task",
        ):
            with self.subTest(command=command):
                self.assertEqual(self._decision(command), "allow")

    def test_an_ad_hoc_apply_still_asks_and_a_relative_path_is_not_allowed(self):
        self.assertEqual(self._decision(
            "backend/.venv/bin/python scripts/1-1892-revive.py --apply"), "ask")
        self.assertEqual(self._decision(
            "backend/.venv/bin/python scripts/gate_production_run.py --issue 1892"), "default")
        self.assertEqual(self._decision(
            "./hrse-gate-run --issue 1892 --count-label Task"), "default")


if __name__ == "__main__":
    unittest.main()
