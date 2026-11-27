"""harmonic-forge#1608 P3: remind_gate_readiness_sweep on an unknown `~user` path."""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remind_gate_readiness_sweep as hook  # noqa: E402

#: `Path.expanduser()` raises RuntimeError on an unknown `~user`, so a reverted
#: guard raises out of main() instead of printing the empty decision.
UNKNOWN_HOME = "~nosuchuser-1608/x"


def run_main(command: str) -> str:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    out = io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", out):
        hook.main()
    return out.getvalue().strip()


class Issue1608UnknownUserHome(unittest.TestCase):
    def test_a_file_under_an_unknown_user_home_prints_no_reminder(self):
        self.assertEqual(json.loads(run_main(f"mise run lane-comment -- --file {UNKNOWN_HOME}")), {})

    def test_find_file_arg_reads_both_forms(self):
        self.assertEqual(hook.find_file_arg("x --file a.md"), "a.md")
        self.assertEqual(hook.find_file_arg("x --file=b.md"), "b.md")


if __name__ == "__main__":
    unittest.main()
