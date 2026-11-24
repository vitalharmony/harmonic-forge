"""Whether two copies of a dependency manifest install the same thing
(harmonic-forge#1539).

l1-post's scratch check links the source checkout's install only when the
manifests beside it equal the attested commit's. A byte compare made every
restart commit's `version` bump in `frontend/package.json` look like a
dependency change and forced a full `npm ci`.

`manifests_equal(name, ours, theirs)`:
- identical bytes are equal, before any parsing;
- a `package.json` basename compares only what npm resolves or runs at
  install: the dependency keys, `os`, `cpu`, and the root's own install
  lifecycle scripts; a missing side or invalid JSON is unequal;
- every other name (the lockfile, requirements files) compares bytes.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath

PACKAGE_KEYS = (
    "dependencies", "devDependencies", "peerDependencies", "optionalDependencies",
    "overrides", "resolutions", "bundleDependencies", "bundledDependencies",
    "engines", "workspaces", "packageManager", "os", "cpu",
)
INSTALL_SCRIPTS = ("preinstall", "install", "postinstall", "prepare", "preprepare", "postprepare")
_ABSENT = object()


def _install_view(raw: bytes) -> dict | None:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    view = {key: data.get(key, _ABSENT) for key in PACKAGE_KEYS}
    scripts = data.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    view["scripts"] = {key: scripts.get(key, _ABSENT) for key in INSTALL_SCRIPTS}
    return view


def manifests_equal(name: str, ours: bytes | None, theirs: bytes | None) -> bool:
    """Whether manifest `name` (bytes, or None when absent) installs the same."""
    if ours == theirs:
        return True
    if ours is None or theirs is None or PurePosixPath(name).name != "package.json":
        return False
    ours_view, theirs_view = _install_view(ours), _install_view(theirs)
    return ours_view is not None and ours_view == theirs_view
