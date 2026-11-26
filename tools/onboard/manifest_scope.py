"""Which repos a grant may name: the home-repo rule (harmonic-forge#1596).

A **home repo** is registered in `projects.toml` and its owner equals its
declared `account` (ASCII lowercase). BATCH, IMC, FORCE, UI and the IMC
retirement on close scope by it instead of an operator-org literal. On the live
manifest it gives today's partition: H/F/I/O home; K/Y/D and P not.

**R1 (harmonic-forge#1527).** A grant writer reads `projects.toml` alone. This
module imports `manifest` and `manifest_protocol` only and must never import
the per-checkout overlay resolver or any path-keyed project lookup: an app
repo's own `.claude/forge.toml` can never make itself home.

**Fail closed.** Any manifest error, and a malformed repo name, is "not home":
an unreadable manifest grants nothing. `manifest_error` lets a caller for whom
"home" adds a requirement (l1_post's UI check) refuse instead of skipping it.
"""
from __future__ import annotations

from pathlib import Path

import manifest as _manifest
from manifest_protocol import ManifestError, normalize_repo


def home_project(repo: str, manifest: Path | None = None) -> _manifest.Project | None:
    """The `projects.toml` entry for `repo` when it is a home repo, else None."""
    try:
        key = normalize_repo(repo)
        project = _manifest.by_repo(manifest).get(key)
    except (ManifestError, OSError):
        return None
    if project is None or not project.account or not project.account.isascii():
        return None
    # ASCII lower, never casefold: "ſ".casefold() is "s" (pitch revision 4).
    return project if key.split("/")[0] == project.account.lower() else None


def manifest_error(manifest: Path | None = None) -> str | None:
    """Why `projects.toml` cannot be loaded, or None when it loads."""
    try:
        _manifest.load(manifest)
    except (ManifestError, OSError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None
