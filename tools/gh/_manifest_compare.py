"""Whether two copies of a dependency manifest install the same thing
(harmonic-forge#1539).

l1-post's scratch check links the source checkout's install only when the
manifests beside it equal the attested commit's. A byte compare made every
restart commit's `version` bump in `frontend/package.json` look like a
dependency change and forced a full `npm ci`.

`manifests_equal(name, ours, theirs, gyp)`:
- identical bytes are equal, before any parsing;
- a `package.json` basename is equal when the two differ ONLY in the
  top-level `version` key. Both sides are parsed keeping key order and
  duplicate keys, so a reordered `overrides` block or any key npm adds later
  compares different, with no list of npm's keys to keep complete (the
  pass-1 allowlist design failed on exactly that, REFORGE
  issuecomment-7121562087);
- the `version` exemption is withdrawn, and the file compared by bytes, when
  either side has a root install-time script or a `binding.gyp` sits beside
  it (`gyp`): npm exports `npm_package_version` to those steps, so a version
  bump can change what they build;
- a missing side, invalid JSON or a non-object top level is unequal;
- every other name (the lockfile, requirements files) compares bytes.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

INSTALL_SCRIPTS = frozenset(
    ("preinstall", "install", "postinstall", "prepare", "preprepare", "postprepare",
     "dependencies"))
IGNORED_KEYS = frozenset(("version",))


class _Object(list):
    """A JSON object as its key/value pairs, in file order, duplicates kept."""


def _pairs(raw: bytes) -> _Object | None:
    """The top-level object's pairs; None when the file is not a JSON object."""
    try:
        data = json.loads(raw, object_pairs_hook=_Object)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return data if isinstance(data, _Object) else None


def _runs_install_step(pairs: _Object) -> bool:
    for key, value in pairs:
        if key != "scripts":
            continue
        if not isinstance(value, _Object):
            return True  # a scripts value npm cannot read as an object: fail closed
        if any(name in INSTALL_SCRIPTS for name, _ in value):
            return True
    return False


def has_gyp(name: str, *roots: Path) -> bool:
    """Whether a `binding.gyp` sits beside manifest `name` under any root."""
    parent = PurePosixPath(name).parent
    return any((Path(root) / parent / "binding.gyp").exists() for root in roots)


def manifests_equal(name: str, ours: bytes | None, theirs: bytes | None,
                    gyp: bool = True) -> bool:
    """Whether manifest `name` (bytes, or None when absent) installs the same.
    `gyp` defaults to True, the closed direction, for a caller that cannot say."""
    if ours == theirs:
        return True
    if ours is None or theirs is None or PurePosixPath(name).name != "package.json" or gyp:
        return False
    ours_pairs, theirs_pairs = _pairs(ours), _pairs(theirs)
    if ours_pairs is None or theirs_pairs is None:
        return False
    if _runs_install_step(ours_pairs) or _runs_install_step(theirs_pairs):
        return False

    def kept(pairs: _Object) -> list:
        return [pair for pair in pairs if pair[0] not in IGNORED_KEYS]
    return kept(ours_pairs) == kept(theirs_pairs)
