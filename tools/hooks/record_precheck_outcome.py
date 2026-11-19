#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure (Bash): the outcome row for a call the
precheck gate decided on (harmonic-forge#1448 AC1).

Joins the run to the gate's row by the hashed `tool_use_id` and session (never
by fingerprint: the fingerprint hashes live state, which a push or rebase
moves). No gate row with that tuid in that session -> no row, so only calls
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

import json
import re
import sys
from pathlib import Path

TAIL_BYTES = 256 * 1024
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


def gate_row(path: Path, session: str, tuid: str) -> dict | None:
    """The gate row this run joins to, from the log's tail.

    One Bash command can log several gate rows under one tuid (e.g. an
    allowed-receipt then a precheck). Every row for (session, tuid) is read;
    the allowed-receipt row wins when one exists, so a failed run of a
    receipted call is attributed to that call's tool/mode, else the newest."""
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    matches = []
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("tuid") == tuid and data.get("session") == session \
                and data.get("outcome") in GATE_OUTCOMES:
            matches.append(data)
    for data in matches:
        if data.get("outcome") == "allowed-receipt":
            return data
    return matches[0] if matches else None


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
    path = gate_receipt.store_dir() / "decisions.jsonl"
    joined = gate_row(path, session, tuid)
    if joined is None:
        return
    failed = event == "PostToolUseFailure"
    decision_row.append(path, decision_row.row(
        "ran-failed" if failed else "ran-ok", tool=joined.get("tool"), mode=joined.get("mode"),
        klass=joined.get("class"), fp=joined.get("fp"), sid=session, tuid=tuid,
        exit=exit_code(payload) if failed else 0, hashed=True))


def main() -> None:
    try:
        record(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 -- AC6: a recorder fault never affects the session
        pass


if __name__ == "__main__":
    main()
