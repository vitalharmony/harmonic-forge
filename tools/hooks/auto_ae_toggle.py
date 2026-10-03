#!/usr/bin/env python3
"""`UserPromptSubmit` hook for `/auto-ae` (harmonic-forge#874), PROBE-ONLY.

This is implementation step 1 of #874. It exists to answer two load-bearing
assumptions before any toggle logic is written:

1. Does an operator-typed `/auto-ae …` reach `UserPromptSubmit` at all, and
   in what envelope (the `<command-name>` markers `batch_provenance.py`
   lists)?
2. Does a model-invoked Skill call fire no `UserPromptSubmit`?

So, when `LANE=1` and the prompt contains `auto-ae` (case-insensitive), it
appends ONE json line to `~/.local/state/auto-ae/probe.jsonl` holding the
timestamp, the prompt text, `CLAUDE_CODE_ENTRYPOINT`, `LANE`, and the
payload's top-level KEY NAMES only -- never the `session_id` or
`transcript_path` values.

WHAT IT DOES NOT DO
-------------------
It decides nothing. It writes no state file, grants nothing, emits no
permission decision and no `additionalContext`: its only output is a one-line
`systemMessage`. Any internal failure is swallowed (fail closed = do nothing
beyond saying the probe was not recorded, and saying that only for a Lane 1
prompt that mentions `auto-ae`); it never raises and always exits 0.
The toggle itself, its provenance rules and the PreToolUse guard are later
steps of #874 and do not exist yet.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

PROBE_RELPATH = Path(".local") / "state" / "auto-ae" / "probe.jsonl"
_TRIGGER_RE = re.compile(r"auto-ae", re.IGNORECASE)
RECORDED = "auto-ae probe recorded; nothing toggled"


def probe_path() -> Path:
    return Path.home() / PROBE_RELPATH


def record(payload: dict, lane: str | None, entrypoint: str | None,
           now: float | None = None) -> bool:
    """Append one probe line. True when written; False when not applicable."""
    if lane != "1":
        return False
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not _TRIGGER_RE.search(prompt):
        return False
    now = time.time() if now is None else now
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "prompt": prompt,
        "CLAUDE_CODE_ENTRYPOINT": entrypoint,
        "LANE": lane,
        "payload_keys": sorted(str(k) for k in payload.keys()),
    }
    path = probe_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return True


def main() -> int:
    message = None
    raw = b""
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict) and record(
                payload, os.environ.get("LANE"),
                os.environ.get("CLAUDE_CODE_ENTRYPOINT")):
            message = RECORDED
    except Exception as exc:  # noqa: BLE001 - a probe never costs a prompt
        # The failure message is gated exactly like the success path -- LANE=1
        # AND the token -- so an unparseable, unrelated prompt stays silent
        # (AC2). The raw bytes are the only place the token can be looked for
        # when the payload itself would not parse (harmonic-forge#880 pass 1).
        if (os.environ.get("LANE") == "1"
                and _TRIGGER_RE.search(raw.decode("utf-8", "replace"))):
            message = f"auto-ae probe NOT recorded ({type(exc).__name__}); nothing toggled"
    if message:
        print(json.dumps({"systemMessage": message}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
