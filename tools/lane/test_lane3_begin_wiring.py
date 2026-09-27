#!/usr/bin/env python3
"""harmonic-forge#792: this repo's own `lane3-begin` must run the readiness
check before it marks the worktree Lane 3-active.

Before #792 the task only touched `LANE3_ACTIVE`, so harmonic-forge#791's
round-approval fix protected consumer repos (HRSE2 wires it into its own
`lane3-begin`) and no gate run against a harmonic-forge issue. The check's
behaviour is tested in `tools/gh/test_check_lane3_ready.py`; this pins only
the wiring, which no other test sees.
"""
import json
import os
import shutil
import subprocess
import tempfile
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


STUB = """import json, os, sys
open(os.environ["STUB_ARGV"], "w").write(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ["STUB_EXIT"]))
"""


def _toml_str(value: str) -> str:
    return "\'\'\'\n" + value + "\'\'\'"


@unittest.skipUnless(shutil.which("mise"), "mise not installed")
class Lane3BeginEndToEndTests(unittest.TestCase):
    """Preclose finding: the greps above cannot tell "the flag is spelled in
    the script" from "the flag reaches the script". This runs the real task
    body through mise, with a stub in place of the readiness check."""

    def _run(self, stub_exit: int) -> tuple[int, list, bool]:
        task = tomllib.loads(MISE_TOML.read_text(encoding="utf-8"))["tasks"]["lane3-begin"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "tools" / "gh").mkdir(parents=True)
            (root / "tools" / "gh" / "check_lane3_ready.py").write_text(STUB)
            (root / "mise.toml").write_text(
                "[tasks.lane3-begin]\n"
                f"usage = {_toml_str(task['usage'])}\n"
                f"run = {_toml_str(task['run'])}\n")
            argv_file = root / "argv.json"
            env = {**os.environ, "STUB_ARGV": str(argv_file), "STUB_EXIT": str(stub_exit),
                   "MISE_TRUSTED_CONFIG_PATHS": str(root)}
            result = subprocess.run(["mise", "run", "lane3-begin", "--issue", "792"],
                                    cwd=root, env=env, capture_output=True, text=True)
            argv = json.loads(argv_file.read_text()) if argv_file.exists() else None
            return result.returncode, argv, (root / ".git" / "LANE3_ACTIVE").exists()

    def test_a_refusal_leaves_no_marker(self):
        code, argv, marked = self._run(1)
        self.assertNotEqual(code, 0)
        self.assertFalse(marked)

    def test_the_issue_flag_reaches_the_check_and_success_marks(self):
        code, argv, marked = self._run(0)
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["--issue", "792"])
        self.assertTrue(marked)


if __name__ == "__main__":
    unittest.main()
