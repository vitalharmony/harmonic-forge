#!/usr/bin/env python3
"""Tests for platform_link_report.py (harmonic-forge#543).

The three-way split is the whole design, so each branch is asserted through the
function rather than by comparing constants: verified -> 0, undeclared -> 0 with
a loud message, drift -> 1. Plus the guarantee #543's AC2 names explicitly —
this reports and never repairs, because a gate that fixes its own preconditions
cannot report on them.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

import platform_link_report as reporter  # noqa: E402

_spec = importlib.util.spec_from_file_location("sync_rules", _ROOT / "sync_rules.py")
assert _spec and _spec.loader
sync_rules = importlib.util.module_from_spec(_spec)
sys.modules["sync_rules"] = sync_rules
_spec.loader.exec_module(sync_rules)


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.claude = self.project / ".claude"

    def _declare(self, skills: list[str]) -> None:
        target = sync_rules.manifest_path(self.project)
        target.parent.mkdir(parents=True, exist_ok=True)
        rendered = ", ".join(f'"{s}"' for s in skills)
        target.write_text(f"skills = [{rendered}]\n", encoding="utf-8")

    def _link_rules_and_agents(self) -> None:
        for name in sync_rules.UNIVERSAL_RULE_FILES:
            t = self.claude / "rules" / name
            t.parent.mkdir(parents=True, exist_ok=True)
            t.symlink_to(sync_rules.RULES_DIR / name)
        for name in sync_rules._universal_agent_files():
            t = self.claude / "agents" / name
            t.parent.mkdir(parents=True, exist_ok=True)
            t.symlink_to(sync_rules.AGENTS_DIR / name)

    def _run(self) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = reporter.report(self.project, sync_rules=sync_rules)
        return code, err.getvalue()

    def test_verified_checkout_passes_quietly(self) -> None:
        self._declare([])
        self._link_rules_and_agents()
        code, err = self._run()
        self.assertEqual(code, 0)
        self.assertNotIn("DRIFT", err)

    def test_drift_fails_the_gate(self) -> None:
        """A DECLARED link that is missing is wrong wherever it is seen."""
        self._declare(["_stub"])
        self._link_rules_and_agents()
        code, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("DRIFT", err)

    def test_drift_message_names_the_missing_path(self) -> None:
        """AC1: 'produces a message naming the missing path'."""
        self._declare(["_stub"])
        self._link_rules_and_agents()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            reporter.report(self.project, sync_rules=sync_rules)
        self.assertIn("_stub", err.getvalue())

    def test_undeclared_reports_loudly_but_does_not_fail(self) -> None:
        """A branch cannot fix a file it does not have."""
        self._link_rules_and_agents()
        code, err = self._run()
        self.assertEqual(code, 0)
        self.assertIn("does not declare", err)
        self.assertIn("hooks-install", err)

    def test_undeclared_is_never_silent(self) -> None:
        """Non-fatal is not the same as unreported — #540's whole defect."""
        self._link_rules_and_agents()
        _, err = self._run()
        self.assertTrue(err.strip(), "undeclared must say something")

    def test_missing_rule_link_is_drift(self) -> None:
        self._declare([])
        self._link_rules_and_agents()
        (self.claude / "rules" / sync_rules.UNIVERSAL_RULE_FILES[0]).unlink()
        code, _ = self._run()
        self.assertEqual(code, 1)

    def test_reports_and_never_repairs(self) -> None:
        """AC2: Lane 3's zero-mutation guarantee.

        A gate that repairs its own preconditions cannot report on them, so the
        drift must still be there afterwards and nothing may be created.
        """
        self._declare(["_stub"])
        self._link_rules_and_agents()
        before = {p: p.stat().st_mtime_ns for p in self.project.rglob("*")}
        self.assertEqual(self._run()[0], 1)
        after = {p: p.stat().st_mtime_ns for p in self.project.rglob("*")}
        self.assertEqual(after, before)
        self.assertFalse((self.claude / "skills" / "_stub").exists())
        self.assertEqual(self._run()[0], 1, "still drifted on a second run")

    def test_nonexistent_project_cannot_run(self) -> None:
        code, err = 0, ""
        e = io.StringIO()
        with contextlib.redirect_stderr(e):
            code = reporter.report(self.project / "nope", sync_rules=sync_rules)
        err = e.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)

    def test_cli_accepts_project_flag(self) -> None:
        self._declare([])
        self._link_rules_and_agents()
        with contextlib.redirect_stderr(io.StringIO()), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reporter.main(["--project", str(self.project)]), 0)


if __name__ == "__main__":
    unittest.main()
