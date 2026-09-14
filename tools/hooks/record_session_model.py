#!/usr/bin/env python3
"""SessionStart hook: record the session's launch model (harmonic-forge#656 AC4).

`SessionStart` is the one Claude Code hook whose payload carries `model`.
A fresh session has no model-bearing transcript entry yet, so without this a
lane launched with `--model opus` would resolve through settings (`sonnet`)
and be blocked on its first `deep` trigger. Writes the payload `model` to
`~/.cache/harmonic-forge/session_model/<session_id>`; `session_model.
current_model` reads it after the transcript and before settings.

Silent, and never fails the session: any error is swallowed. Claude Code only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_model  # noqa: E402


def record(payload: dict, record_dir: Path | None = None) -> Path | None:
    model = payload.get("model") or session_model.launch_model()
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
