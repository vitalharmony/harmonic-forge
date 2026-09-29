#!/usr/bin/env python3
"""Per-project GitHub identity for hooks (harmonic-forge#804).

Hooks fail open on anything they cannot resolve, so this never raises: an
unknown repo, or a resolver that cannot load, returns None -- "inherit the
caller's environment", exactly what every hook did before per-project identity
existed. Hooks pass the result as a per-subprocess `env=`, never by mutating
`os.environ`, and never probe `gh api user` (that costs a network call on every
tool call). The mutation-and-probe path is `apply_project_identity`, for lane
entrypoints only.

Imports `tools/onboard/`, never the reverse (`batch_auth.py:164`'s one-way rule).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ONBOARD = str(Path(__file__).resolve().parents[1] / "onboard")


def slot_env(repo: str | None) -> dict[str, str] | None:
    """The env to run `gh` for `repo` under, or None to inherit."""
    try:
        if _ONBOARD not in sys.path:
            sys.path.insert(0, _ONBOARD)
        from manifest_identity import slot_env_or_none  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- a hook must never fail on import
        return None
    return slot_env_or_none(repo)


def repo_from_checkout(cwd: str | Path | None) -> str | None:
    """The registered repo for `cwd`, from projects.toml, with no `gh` call."""
    try:
        if _ONBOARD not in sys.path:
            sys.path.insert(0, _ONBOARD)
        from manifest_identity import repo_for_path_or_none  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    return repo_for_path_or_none(cwd or Path.cwd())
