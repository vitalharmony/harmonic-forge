#!/usr/bin/env python3
"""SessionStart hook: record the session's launch model (harmonic-forge#656 AC4).

`SessionStart` is the one Claude Code hook whose payload carries `model`.
A fresh session has no model-bearing transcript entry yet, so without this a
lane launched with `--model opus` would resolve through settings (`sonnet`)
and be blocked on its first `deep` trigger. Writes the payload `model` to
`~/.cache/harmonic-forge/session_model/<session_id>`; `session_model.
current_model` reads it after the transcript and before settings.

harmonic-forge#1211: after `/model`, a `/clear` or compaction would record the
stale launch flag. On those sources the predecessor transcript's newest model
(the one the session is actually on) is recorded first; each start also points
`by-pid/<claude pid>` at its own transcript for the next one. `resume` is
excluded: a new `--resume` process has no predecessor, and a resumed transcript
already carries its model.

Silent, and never fails the session: any error is swallowed. Claude Code only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_model  # noqa: E402

#: Sources that continue the same process's work in a new session id.
CONTINUING_SOURCES = frozenset({"clear", "compact"})


def record(payload: dict, record_dir: Path | None = None,
           proc_root: str = "/proc") -> Path | None:
    pid = None
    model = None
    if payload.get("source") in CONTINUING_SOURCES:
        try:
            pid = session_model.claude_pid(proc_root)
            model = session_model.predecessor_model(pid, record_dir=record_dir,
                                                    proc_root=proc_root)
        except Exception:  # noqa: BLE001 -- the pointer is best-effort; the record is not
            model = None
    model = model or payload.get("model") or session_model.launch_model()
    path = _write_record(payload, model, record_dir)
    # Pointer bookkeeping runs after the record and in its own failure boundary,
    # so it can never cost a session its record (harmonic-forge#1211 pass 1).
    try:
        pid = pid if pid is not None else session_model.claude_pid(proc_root)
        session_model.prune_pointers(record_dir, proc_root)
        transcript = payload.get("transcript_path")
        if pid is not None and isinstance(transcript, str):
            session_model.write_pointer(pid, transcript, model.strip() if isinstance(model, str) else None,
                                        record_dir=record_dir, proc_root=proc_root)
    except Exception:  # noqa: BLE001
        pass
    return path


def _write_record(payload: dict, model, record_dir: Path | None) -> Path | None:
    if not isinstance(model, str) or not model.strip():
        return None
    path = session_model.record_path(payload.get("session_id") or "", record_dir)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.strip() + "\n", encoding="utf-8")
    return path


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            record(payload)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
