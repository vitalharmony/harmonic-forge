#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure (Bash): the outcome row for a call the
precheck gate decided on (harmonic-forge#1448 AC1).

Joins the run to the gate's row by the hashed `tool_use_id` and session (never
by fingerprint: the fingerprint hashes live state, which a push or rebase
moves), read from the gate's per-call file, which is deleted once joined
(stale ones are pruned after 24 hours). No gate row with that tuid in that session -> no row, so only calls
the gate decided on are recorded. The row copies tool, mode, class and fp from
the gate row and adds `ran-ok` (exit 0: PostToolUse fires only on success and
its tool_response has no exit field) or `ran-failed` (exit from a strict
`Exit code N` first line of the harness's `error`, else null; stderr is never
read). Never an argv token.

A recorder fault never affects the session (record_failed_command.py's AC6
rule): the whole body is inside `except Exception: pass`, nothing is printed,
and the exit is always 0.
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path

EXIT_LINE = re.compile(r"^Exit code (-?\d+)$")
#: The gate's per-call decisions a post-run row may join to.
GATE_OUTCOMES = ("precheck", "allowed-receipt", "allowed-override", "refused",
                 "guarded-elsewhere", "error")


def exit_code(payload: dict) -> int | None:
    error = payload.get("error")
    if not isinstance(error, str):
        return None
    found = EXIT_LINE.match(error.split("\n", 1)[0].strip())
    return int(found.group(1)) if found else None


def gate_row(store: Path, session: str, tuid: str) -> dict | None:
    """The gate row this run joins to, read from the gate's per-call file
    `<store>/calls/<session>-<tuid>.json` (decision_row.write_call), never by
    scanning the shared log. The gate keeps an allowed-receipt row there over a
    later row under the same tuid, so a receipted call keeps its attribution."""
    import decision_row  # noqa: PLC0415
    path = decision_row.call_path(store, session, tuid)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("tuid") != tuid or data.get("session") != session \
            or data.get("outcome") not in GATE_OUTCOMES:
        return None
    return data


def record(payload: dict) -> None:
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here.parent / "precheck"))
    import decision_row  # noqa: PLC0415
    import gate_receipt  # noqa: PLC0415
    if payload.get("tool_name") != "Bash":
        return
    event = payload.get("hook_event_name")
    if event not in ("PostToolUse", "PostToolUseFailure"):
        return
    raw_tuid = payload.get("tool_use_id")
    if not isinstance(raw_tuid, str) or not raw_tuid:
        return
    sid = str(payload.get("session_id") or "").strip() or gate_receipt.NO_SESSION
    session, tuid = decision_row.digest(sid), decision_row.digest(raw_tuid)
    store = gate_receipt.store_dir()
    joined = gate_row(store, session, tuid)
    if joined is None:
        return
    failed = event == "PostToolUseFailure"
    decision_row.append(store / "decisions.jsonl", decision_row.row(
        "ran-failed" if failed else "ran-ok", tool=joined.get("tool"), mode=joined.get("mode"),
        klass=joined.get("class"), fp=joined.get("fp"), sid=session, tuid=tuid,
        exit=exit_code(payload) if failed else 0, hashed=True))
    with contextlib.suppress(OSError):
        decision_row.call_path(store, session, tuid).unlink()
    decision_row.prune_calls(store)


def main() -> None:
    try:
        record(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 -- AC6: a recorder fault never affects the session
        pass


if __name__ == "__main__":
    main()
