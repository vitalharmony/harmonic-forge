#!/usr/bin/env python3
"""Tell Lane 1 when a memory fold is owed (harmonic-forge#982).

`UserPromptSubmit` and `SessionStart` hook. Active only when `LANE=1`. It runs
`tools/memory/fold_due.py` and, when a fold is owed, injects one line as
`additionalContext` (never `systemMessage`) carrying its own procedure.

* `UserPromptSubmit` fires on `/loop` tick prompts as well as typed ones, so a
  long-running Lane 1 sees it without a new session. Harness-wrapped prompts
  (Monitor events, subagent hand-backs; `batch_provenance.injected_envelope`)
  are skipped.
* A given set of triggers is injected at most once per 24 hours. The
  suppression state sits beside fold_due's marker, so a tick loop is not
  nagged.

It always exits 0: a notice about owed work must never break a prompt.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "memory"))

SUPPRESS_SECONDS = 24 * 3600
FOLD_DUE = _HERE.parent / "memory" / "fold_due.py"
PROCEDURE = ("Queue it behind your current task (this is not a belt event; R-0375's "
             "queueing applies by this instruction). Mention it once; then run "
             "/memory-triage and its \"Fold run\" section.")


def _suppress_path() -> Path:
    import fold_due  # noqa: PLC0415
    return fold_due.state_dir() / "notice.json"


def _wrapped(prompt: str) -> bool:
    try:
        import batch_provenance  # noqa: PLC0415
        return batch_provenance.injected_envelope(prompt) is not None
    except Exception:  # noqa: BLE001 -- cannot tell: treat as wrapped, stay quiet
        return True


def fold_line(cwd: str) -> str:
    try:
        out = subprocess.run([sys.executable, str(FOLD_DUE), "--repo", cwd],
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    line = out.stdout.strip().splitlines()
    return line[0] if line and line[0].startswith("memory-fold owed:") else ""


def suppressed(line: str, now: float, path: Path | None = None) -> bool:
    """True when this trigger line was already injected in the last 24 hours.
    Records it otherwise."""
    path = path or _suppress_path()
    key = hashlib.sha256(line.encode()).hexdigest()[:16]
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(seen, dict):
            seen = {}
    except (OSError, ValueError):
        seen = {}
    last = seen.get(key)
    if isinstance(last, (int, float)) and now - last < SUPPRESS_SECONDS:
        return True
    seen = {k: v for k, v in seen.items()
            if isinstance(v, (int, float)) and now - v < SUPPRESS_SECONDS}
    seen[key] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(seen), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return False


def decide(payload: dict, env: dict, now: float, line_fn=fold_line,
           suppress_path: Path | None = None) -> dict | None:
    if env.get("LANE") != "1":
        return None
    event = payload.get("hook_event_name") or "UserPromptSubmit"
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or _wrapped(prompt):
            return None
    line = line_fn(payload.get("cwd") or os.getcwd())
    if not line or suppressed(line, now, suppress_path):
        return None
    return {"hookSpecificOutput": {"hookEventName": event,
                                   "additionalContext": f"{line}. {PROCEDURE}"}}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = decide(payload if isinstance(payload, dict) else {}, os.environ, time.time())
    except Exception:  # noqa: BLE001 -- always exit 0
        return 0
    if result:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
