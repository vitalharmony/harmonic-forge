#!/usr/bin/env python3
"""harmonic-forge#792: this repo's own `lane3-begin` must run the readiness
check before it marks the worktree Lane 3-active.

Before #792 the task only touched `LANE3_ACTIVE`, so harmonic-forge#791's
round-approval fix protected consumer repos (HRSE2 wires it into its own
`lane3-begin`) and no gate run against a harmonic-forge issue. The check's
behaviour is tested in `tools/gh/test_check_lane3_ready.py`; this pins only
the wiring, which no other test sees.
"""
import tomllib
import unittest
from pathlib import Path

MISE_TOML = Path(__file__).resolve().parents[2] / "mise.toml"


def _run_body() -> str:
    tasks = tomllib.loads(MISE_TOML.read_text(encoding="utf-8"))["tasks"]
    return tasks["lane3-begin"]["run"]


class Lane3BeginWiringTests(unittest.TestCase):
    def test_the_readiness_check_runs(self) -> None:
        self.assertIn("tools/gh/check_lane3_ready.py", _run_body())

    def test_it_runs_before_the_marker_is_written(self) -> None:
        """A refusal must leave no marker: the check has to come first, and
        `set -e` has to make its failure stop the task."""
        body = _run_body()
        self.assertIn("set -e", body)
        self.assertLess(body.index("check_lane3_ready.py"), body.index("LANE3_ACTIVE"))

    def test_the_issue_flag_is_passed_through(self) -> None:
        self.assertIn('--issue "$usage_issue"', _run_body())


if __name__ == "__main__":
    unittest.main()
