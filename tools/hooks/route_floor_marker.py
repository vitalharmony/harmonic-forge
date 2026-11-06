"""One per-session marker: "this session's requests go out on the high-tier model"
(harmonic-forge#1250).

The hooks that already know the issue write it, at assignment
(`tier_model_trigger_check.py`) and at a denied write (`model_tier_gate.py`). The
route-floor mod (`tools/routing/mod`) only reads it, on each request. A marker only
ever raises, so a stale one costs tokens at most.

The mod builds the same path itself: `tools/routing/test_floor_marker_contract.py`
holds the two together.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_model  # noqa: E402


def eligible(env) -> bool:
    """A Lane 2 session whose launcher loaded the mod. Nothing else is ever raised."""
    return env.get("LANE") == "2" and env.get("LANE_ROUTE_MOD") == "1"


def marker_path(session_id, home: str | None = None) -> Path | None:
    """The marker's path, or None for an id that cannot name a file."""
    if not isinstance(session_id, str) or not session_model._SESSION_ID_RE.match(session_id):
        return None
    root = Path(home) if home is not None else Path.home()
    return root / ".cache" / "harmonic-forge" / "route_floor" / session_id


def write(session_id) -> bool:
    """True only when the marker is on disk. Never raises: the caller's block stands on False."""
    try:
        path = marker_path(session_id)
        if path is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return True
    except (OSError, RuntimeError):
        return False


def exists(session_id) -> bool:
    try:
        path = marker_path(session_id)
        return path is not None and os.path.exists(path)
    except (OSError, RuntimeError):
        return False


def clear(session_id) -> None:
    try:
        path = marker_path(session_id)
        if path is not None:
            path.unlink(missing_ok=True)
    except (OSError, RuntimeError):
        return
