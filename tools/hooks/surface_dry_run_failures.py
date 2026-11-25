#!/usr/bin/env python3
"""UserPromptSubmit: surface a failed background `l1_post --dry-run` once, to the
session that ran it (harmonic-forge#1544).

A background dry run reaches the session only as a task notification, so its
refusal can sit unread in a log while Lane 1 goes to post. `l1_post` records a
failed dry run under its own session's directory
(tools/gh/_dry_run_failures.py); this hook reads only the prompting session's
directory (`session_id` from the payload, else `CLAUDE_CODE_SESSION_ID`),
prints one line per entry, and only then deletes each file it showed, each in
its own try, so nothing is consumed unless it was emitted. It fails open: any
error injects nothing and exits 0, never blocking a prompt.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gh"))


def _session(raw: str) -> str:
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = {}
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    return str(sid or os.environ.get("CLAUDE_CODE_SESSION_ID", "")).strip()


def _line(data: dict) -> str:
    text = " ".join(str(data.get("text", "")).split())
    if len(text) > 600:  # keep the head and the tail: argparse's error: line is last
        text = f"{text[:150]} ... {text[-450:]}"
    if data.get("kind") == "unparsed":
        return f"l1_post run FAILED before its arguments parsed: {text}"
    sha = str(data.get("sha") or "")[:12] or "?"
    if data.get("exit") == 3:
        return (f"l1_post dry run must WAIT (exit 3) for {data.get('repo')}#{data.get('issue')} "
                f"{data.get('kind')} @ {sha}: {text}")
    return (f"l1_post dry run FAILED for {data.get('repo')}#{data.get('issue')} "
            f"{data.get('kind')} @ {sha}: {text}")


def main(stdin=sys.stdin) -> int:
    try:
        import _dry_run_failures as drf
        sid = _session(stdin.read())
        if not sid or sid == drf.NO_SESSION:
            return 0
        entries = drf.pending(sid)
        if not entries:
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(_line(data) for _, data in entries)}}))
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - fail open, never block a prompt
        return 0
    for path, _ in entries:
        try:
            drf.consume(path)
        except Exception:  # noqa: BLE001 - an unconsumed entry shows again next prompt
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
