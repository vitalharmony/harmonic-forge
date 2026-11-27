"""harmonic-forge#1566: the forge root a hook names, from the one resolver.

`resolve(own_file)` is `instance.resolve_forge_root` (HARMONIC_FORGE_ROOT, then
instance.toml `forge_root`, then the checkout `own_file` sits in), the order the
lane launcher uses (#1526). Any failure answers that own checkout, never a
home-relative literal: the callers here only name paths in advisory output.

check_receipt does not use this: its forge identity is the meeting point of a
recorder and a reader that run from different checkouts, which only the
launcher-exported root can settle (harmonic-forge#1630; operator D29-D31).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping

_SETTINGS = Path(__file__).resolve().parents[1] / "settings"


def _instance():
    if str(_SETTINGS) not in sys.path:
        sys.path.insert(0, str(_SETTINGS))
    import instance  # noqa: PLC0415
    return instance


def own_checkout(own_file: str) -> Path:
    """The checkout `own_file` (a file under `tools/<dir>/`) sits in."""
    return Path(own_file).resolve().parents[2]


def resolve(own_file: str, environ: Mapping[str, str] | None = None) -> Path:
    try:
        return _instance().resolve_forge_root(own_checkout(own_file), environ)[0]
    except Exception:  # noqa: BLE001 - advisory callers: never crash a hook
        return own_checkout(own_file)
