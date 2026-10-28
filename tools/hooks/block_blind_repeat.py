#!/usr/bin/env python3
"""PreToolUse(Bash): refuse a byte-identical repeat of a command that just failed (harmonic-forge#1216)."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import blind_repeat  # noqa: PLC0415
        reason = blind_repeat.refusal(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 -- AC6: any fault allows
        return
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason},
                          "systemMessage": reason}))


if __name__ == "__main__":
    main()
