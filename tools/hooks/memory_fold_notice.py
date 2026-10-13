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
import tempfile
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


def fold_line(cwd: str) -> tuple[str, str]:
    """(line, kinds) from fold_due.py, or ("", "") when nothing is owed."""
    try:
        out = subprocess.run([sys.executable, str(FOLD_DUE), "--repo", cwd],
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return "", ""
    lines = out.stdout.strip().splitlines()
    if not lines or not lines[0].startswith("memory-fold owed:"):
        return "", ""
    kinds = lines[1][len("kinds:"):].strip() if len(lines) > 1 and lines[1].startswith("kinds:") else lines[0]
    return lines[0], kinds


def _key(session: str, kinds: str) -> str:
    """Suppression is per session and per SET OF TRIGGER KINDS, never per line
    text: a count in the line ("16 new ...") must not re-send it (preclose
    pass 1), and one session's notice must not silence another's."""
    return hashlib.sha256(f"{session}\0{kinds}".encode()).hexdigest()[:16]


def _load(path: Path) -> dict:
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return seen if isinstance(seen, dict) else {}


def is_suppressed(key: str, now: float, path: Path) -> bool:
    last = _load(path).get(key)
    return isinstance(last, (int, float)) and now - last < SUPPRESS_SECONDS


def mark_sent(key: str, now: float, path: Path) -> bool:
    """Record a delivered notice. False when the state cannot be written."""
    seen = {k: v for k, v in _load(path).items()
            if isinstance(v, (int, float)) and now - v < SUPPRESS_SECONDS}
    seen[key] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".notice-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(seen, handle)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def decide(payload: dict, env: dict, now: float, line_fn=fold_line,
           suppress_path: Path | None = None) -> tuple[dict | None, str | None]:
    """(output, suppression key to record after delivery)."""
    if env.get("LANE") != "1":
        return None, None
    event = payload.get("hook_event_name") or "UserPromptSubmit"
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or _wrapped(prompt):
            return None, None
    line, kinds = line_fn(payload.get("cwd") or os.getcwd())
    if not line:
        return None, None
    path = suppress_path or _suppress_path()
    key = _key(str(payload.get("session_id") or ""), kinds)
    # A session start (new context, /clear, resume) always gets the line.
    if event == "UserPromptSubmit" and is_suppressed(key, now, path):
        return None, None
    text = f"{line}. {PROCEDURE}"
    return ({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}, key)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        now = time.time()
        path = _suppress_path()
        result, key = decide(payload if isinstance(payload, dict) else {}, os.environ, now,
                             suppress_path=path)
        if result and key:
            # Recorded only after the line is written out, so a failed delivery
            # is not suppressed. An unwritable state is said, not hidden.
            ctx = result["hookSpecificOutput"]
            if not os.access(path.parent if path.parent.exists() else path.parent.parent, os.W_OK):
                ctx["additionalContext"] += (f" (Suppression state at {path} is not writable, "
                                             "so this line repeats on every prompt until it is.)")
            print(json.dumps(result))
            sys.stdout.flush()
            mark_sent(key, now, path)
    except Exception:  # noqa: BLE001 -- always exit 0
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
