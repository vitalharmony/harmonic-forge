"""harmonic-forge#1608 Test Case 4: a Bash refusal hook decides the same way
on a bare command and on the Code Mode form `cd '<dir>' && <command>`.

Code Mode can hand a script's shell call to the PreToolUse hooks with a
leading `cd` (code.claude.com/docs/en/code-mode). Each hook here runs as the
CLI runs it: a subprocess fed a synthetic PreToolUse payload, with `LANE`
removed and `HOME` and `cwd` set to a temp dir, so no operator state (for
example a live `gh_scan_override`) can flip the expected decision.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent

# (hook script, command, expected permissionDecision)
CASES = (
    (TOOLS / "hooks" / "block_irreversible_ops.py", "git clean -fd", "ask"),
    (TOOLS / "gh" / "block_closing_keywords.py",
     "gh pr create --title t --body 'Closes #12'", "deny"),
    (TOOLS / "hooks" / "block_raw_board_scan.py",
     "gh project item-list 3 --owner vitalharmony", "ask"),
    (TOOLS / "hooks" / "guard_gh_rest_budget.py",
     "gh issue list --repo vitalharmony/harmonic-forge --limit 1000", "deny"),
)


def _decide(hook: Path, command: str, tmp: Path) -> tuple[int, str | None, str]:
    payload = {"session_id": "compound-forms", "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_use_id": "repl_probe1", "cwd": str(tmp),
               "tool_input": {"command": command}}
    env = {k: v for k, v in os.environ.items() if k != "LANE"}
    env["HOME"] = str(tmp)
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("XDG_CACHE_HOME", None)
    proc = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=tmp, env=env, timeout=60)
    try:
        out = json.loads(proc.stdout or "{}")
    except ValueError:
        out = {}
    decision = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
    return proc.returncode, decision, proc.stderr


class CompoundForms(unittest.TestCase):

    def test_bare_and_cd_prefixed_forms_decide_the_same(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            for hook, command, expected in CASES:
                for form in (command, f"cd '{tmp}' && {command}"):
                    with self.subTest(hook=hook.name, form=form):
                        rc, decision, stderr = _decide(hook, form, tmp)
                        self.assertEqual(rc, 0, stderr)
                        self.assertEqual(decision, expected, stderr)


if __name__ == "__main__":
    unittest.main()
