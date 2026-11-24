#!/usr/bin/env python3
"""UserPromptSubmit: surface a failed background `l1_post --dry-run` once
(harmonic-forge#1544).

A background dry run reaches the session only as a task notification, so its
refusal can sit unread in a log while Lane 1 goes to post. `l1_post` records a
failed dry run (tools/gh/_dry_run_failures.py); this hook injects one line per
unshown entry into the next prompt's context and marks it shown, so each is
shown once. It fails open: any error injects nothing and exits 0, never
blocking a prompt.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gh"))


def lines() -> list[str]:
    import _dry_run_failures as drf
    out = []
    for path, data in drf.pending():
        sha = str(data.get("sha") or "")[:12] or "?"
        text = " ".join(str(data.get("text", "")).split())[:600]
        out.append(f"l1_post dry run FAILED for {data.get('repo')}#{data.get('issue')} "
                   f"{data.get('kind')} @ {sha}: {text}")
        drf.mark_shown(path, data)
    return out


def main() -> int:
    try:
        found = lines()
    except Exception:  # noqa: BLE001 - fail open, never block a prompt
        return 0
    if found:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": "\n".join(found)}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
