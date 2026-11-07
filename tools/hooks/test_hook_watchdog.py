#!/usr/bin/env python3
"""harmonic-forge#1274 (kept by the reforge): `hook_watchdog.Watchdog` bounds
every path of `block_route_tightened_merge.py`, including `run_gh`'s one
retry, so a slow GitHub can never carry the hook past its 25 s registration
timeout, which would let the merge run unchecked.

Kill check: removing the watchdog from `block_route_tightened_merge.main()`
(`watchdog = hook_watchdog.Watchdog(...).start()` never started) fails
`test_route_hook_retry_hang_is_denied_within_the_budget`.
Run: `python3 tools/hooks/test_hook_watchdog.py`."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HRSE, HEAD = "vitalharmony/hrse", "c" * 40

#: Runs the hook in a child process against a fake `gh` on PATH, with the
#: watchdog shortened to argv[2] seconds and a real (temp) identity slot, so
#: `run_gh`'s retry path is the live one.
_DRIVER = """
import os, sys
sys.path.insert(0, os.environ["HOOKS_DIR"]); sys.path.insert(0, os.environ["GH_DIR"])
import hook_identity, hook_watchdog
hook_watchdog.SECONDS = float(sys.argv[2])
hook_identity.slot_env = lambda repo: {**os.environ, "GH_CONFIG_DIR": os.environ["FAKE_SLOT"]}
__import__(sys.argv[1]).main()
"""

_FAKE_GH = """#!/bin/sh
n=$(cat "$FAKE_DIR/count" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$FAKE_DIR/count"
case "$FAKE_MODE" in
  slow) sleep 5; exit 1 ;;
  502-then-hang)
    if [ "$n" = 1 ]; then echo "gh: Server Error (HTTP 502)" >&2; exit 1; fi
    sleep 30 ;;
esac
exit 1
"""


class WatchdogBound(unittest.TestCase):
    def run_child(self, module: str, mode: str, seconds: float) -> tuple[float, list, int]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name in ("bin", "slot", "home"):
            (root / name).mkdir()
        gh = root / "bin" / "gh"
        gh.write_text(_FAKE_GH)
        gh.chmod(0o755)
        env = {**os.environ, "PATH": f"{root / 'bin'}:{os.environ.get('PATH', '')}",
               "HOME": str(root / "home"), "FAKE_DIR": str(root), "FAKE_MODE": mode,
               "FAKE_SLOT": str(root / "slot"), "HOOKS_DIR": str(HERE),
               "GH_DIR": str(HERE.parent / "gh")}
        payload = json.dumps({"tool_name": "Bash", "tool_input": {
            "command": f"gh pr merge 9 --repo {HRSE} --squash --match-head-commit {HEAD}"}})
        start = time.monotonic()
        result = subprocess.run([sys.executable, "-c", _DRIVER, module, str(seconds)],
                                input=payload, capture_output=True, text=True, env=env,
                                timeout=60, check=False)
        elapsed = time.monotonic() - start
        outputs = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        count_file = root / "count"
        count = int(count_file.read_text().strip() or 0) if count_file.exists() else 0
        return elapsed, outputs, count

    def deny_reason(self, outputs: list) -> str:
        self.assertEqual(len(outputs), 1, outputs)  # exactly one decision
        decision = outputs[0].get("hookSpecificOutput") or {}
        self.assertEqual(decision.get("permissionDecision"), "deny", outputs)
        return decision.get("permissionDecisionReason", "")

    def test_route_hook_retry_hang_is_denied_within_the_budget(self):
        elapsed, outputs, calls = self.run_child("block_route_tightened_merge", "502-then-hang", 2.0)
        self.assertIn("did not finish within 2 s", self.deny_reason(outputs))
        self.assertEqual(calls, 2)  # run_gh's retry is kept; the watchdog bounds it
        self.assertLess(elapsed, 4.5)

    def test_route_hook_slow_read_is_denied_within_the_budget(self):
        elapsed, outputs, _ = self.run_child("block_route_tightened_merge", "slow", 2.0)
        self.assertIn("did not finish within 2 s", self.deny_reason(outputs))
        self.assertLess(elapsed, 4.5)


if __name__ == "__main__":
    unittest.main()
