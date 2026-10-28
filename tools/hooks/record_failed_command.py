#!/usr/bin/env python3
"""PostToolUseFailure(Bash): record a failed command for the repeat block (harmonic-forge#1216)."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import blind_repeat  # noqa: PLC0415
        blind_repeat.record(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 -- AC6: a recorder fault never affects the session
        pass


if __name__ == "__main__":
    main()
