"""Pin a launch-under-test's forge root (harmonic-forge#1526).

A lane launch resolves its forge root from `HARMONIC_FORGE_ROOT`, then
`instance.toml` `forge_root`, then the launcher's own checkout, and runs
`<root>/sync_rules.py --pull`: a real `git pull --ff-only` when the root is a
real checkout. `baseline_capture.capture_cell` and the launcher tests' own
`run_script` call `pin_forge_root` after their environment overrides, so a
launch under test never falls back to the checkout under test. Its own module
so `baseline_capture.py` stays within the line cap; it re-exports the name.
"""
from __future__ import annotations

from pathlib import Path


def pin_forge_root(env: dict, lane_dir: Path) -> None:
    """Default `XDG_CONFIG_HOME` to `$HOME/.config` of `env`, and raise
    RuntimeError when `HARMONIC_FORGE_ROOT` is empty or absent while
    `lane_dir` or any parent holds `sync_rules.py`: the launcher-checkout
    fallback would then pull there. Raises before any subprocess. A lane dir
    with no `sync_rules.py` above it (a copied fixture forge) may run with the
    root unset: that is the launcher-checkout source."""
    env.setdefault("XDG_CONFIG_HOME", f"{env['HOME']}/.config")
    if env.get("HARMONIC_FORGE_ROOT"):
        return
    here = Path(lane_dir).resolve()
    if any((d / "sync_rules.py").exists() for d in (here, *here.parents)):
        raise RuntimeError(
            f"{lane_dir}: HARMONIC_FORGE_ROOT is empty, so the launcher-checkout "
            f"fallback would run a real `git pull --ff-only` there (harmonic-forge#1526)")
