"""harmonic-forge#1566: the forge root a hook names, from the one resolver.

`resolve(own_file)` is `instance.resolve_forge_root` (HARMONIC_FORGE_ROOT, then
instance.toml `forge_root`, then the checkout `own_file` sits in), the order the
lane launcher uses (#1526). Any failure answers that own checkout, never a
home-relative literal: the callers here only name paths in advisory output.

`resolve_declared()` is only what is declared (the env value, then
instance.toml), None when neither resolves, with no own-checkout fallback.
check_receipt compares it with the checkout it runs from: a different declared
checkout means a wrapper may have run forge from elsewhere, so its forge
identity is withheld (harmonic-forge#1566, operator D25).
"""
from __future__ import annotations

import os
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


def resolve_declared(environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    try:
        instance = _instance()
        if not env.get("HARMONIC_FORGE_ROOT") and instance.forge_root(env) is None:
            return None
        # The launcher checkout is only the last fallback, unreachable here:
        # one of the two declarations above is set. It must still exist,
        # because `launch_view` resolves the hook-set path beside it.
        return instance.resolve_forge_root(own_checkout(__file__), env)[0]
    except Exception:  # noqa: BLE001 - unresolved is None, never a guess
        return None
