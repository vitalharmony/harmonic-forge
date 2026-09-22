#!/usr/bin/env python3
"""The runnable half of ADR-008's two lane3-gate call sites.

The exit code is the contract: a gate script branches on it without parsing,
so `blocked` having its own code — distinct from both pass and fail — is the
property under test.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_adapter  # noqa: E402

MODULE = '''
def swept(**kwargs):
    return {"status": "pass", "evidence": ["nothing left behind"]}


def dirty(**kwargs):
    return {"status": "fail", "evidence": ["3 fixture nodes remain"]}
'''


def _repo(manifest: dict | None) -> Path:
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    (tmp / "scripts").mkdir()
    (tmp / "scripts" / "m.py").write_text(MODULE, encoding="utf-8")
    if manifest is not None:
        (tmp / ".claude").mkdir()
        (tmp / ".claude" / "gate-adapter.json").write_text(json.dumps(manifest),
                                                           encoding="utf-8")
    return tmp


CLEAN = {"residue_sweep": {"module": "scripts/m.py", "entrypoint": "swept"}}
DIRTY = {"residue_sweep": {"module": "scripts/m.py", "entrypoint": "dirty"}}
LEASE = {"lease": {"module": "scripts/m.py", "check_owner": "dirty"}}


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = run_adapter.main(argv)
    return code, out.getvalue()


class ExitCodeTests(unittest.TestCase):
    def test_pass_exits_zero(self) -> None:
        code, text = _run(["residue_sweep", "--repo", str(_repo(CLEAN))])
        self.assertEqual(code, 0)
        self.assertIn("PASS", text)
        self.assertIn("nothing left behind", text)

    def test_fail_exits_one(self) -> None:
        code, text = _run(["residue_sweep", "--repo", str(_repo(DIRTY))])
        self.assertEqual(code, 1)
        self.assertIn("FAIL", text)

    def test_blocked_exits_two_and_is_neither_pass_nor_fail(self) -> None:
        """ADR-008 AC4. A gate branching on `rc == 0` would read a missing
        adapter as a clean sweep if blocked collapsed into either code."""
        code, text = _run(["residue_sweep", "--repo", str(_repo(None))])
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED", text)
        self.assertIn("no adapter manifest", text)

    def test_a_declared_capability_with_no_entrypoint_suffix_uses_the_default(self) -> None:
        code, _ = _run(["residue_sweep", "--repo", str(_repo(CLEAN))])
        self.assertEqual(code, 0)

    def test_an_explicit_entrypoint_suffix_is_honored(self) -> None:
        code, text = _run(["lease:check_owner", "--repo", str(_repo(LEASE)),
                           "--worktree", "/tmp/x", "--report-only"])
        self.assertEqual(code, 1)
        self.assertIn("lease.check_owner", text)

    def test_json_mode_emits_the_raw_finding(self) -> None:
        _, text = _run(["residue_sweep", "--repo", str(_repo(CLEAN)), "--json"])
        self.assertEqual(json.loads(text)["status"], "pass")

    def test_the_cli_runs_as_a_script(self) -> None:
        out = subprocess.run(
            [sys.executable, str(HERE / "run_adapter.py"), "residue_sweep",
             "--repo", str(_repo(None))],
            capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("BLOCKED", out.stdout)


class SkillWiringTests(unittest.TestCase):
    def test_the_platform_skill_names_this_command(self) -> None:
        """The call site is a skill instruction; an instruction naming no
        runnable command is how a capability ships and reaches nobody."""
        skill = (HERE.parents[1] / "skills" / "lane3-gate-platform" / "SKILL.md")
        text = skill.read_text(encoding="utf-8")
        self.assertIn("run_adapter.py residue_sweep", text)
        self.assertIn("run_adapter.py lease:check_owner", text)


if __name__ == "__main__":
    unittest.main()
