"""harmonic-forge#1086: a replay that conflicts only on exempt JSON version keys.

`preclose_passes.accepts` rule (b) replays the reviewed commit onto the current
base with `git merge-tree`. When both the reviewed branch and main bump the
same `"version"` line, that replay conflicts, so a version-only rebase was
never accepted. This module decides whether such a conflict is version-only:
every conflicted path is a `VERSION_EXEMPT` file, mode 100644 on all four
sides, and each side differs from its own base only in the exempt keys.
"""
from __future__ import annotations

import os

import preclose_passes as pp


def _entry(repo_dir, commit: str, path: str) -> tuple[str, str] | None:
    """(mode, blob) of `path` at `commit`, or None when it is absent."""
    out = pp.run_git(repo_dir, "ls-tree", "-z", commit, "--", path)
    if out.returncode or not out.stdout:
        return None
    meta, _, name = out.stdout.rstrip("\0").partition("\t")
    if name != path:
        return None
    mode, kind, blob = meta.split()
    return (mode, blob) if kind == "blob" else None


def _same_but_exempt(repo_dir, a_blob: str, b_blob: str, exempt) -> bool:
    a = pp._strict_json(pp._git_out(repo_dir, "cat-file", "blob", a_blob))
    b = pp._strict_json(pp._git_out(repo_dir, "cat-file", "blob", b_blob))
    return pp._strict_equal(pp._without_exempt(a, exempt), pp._without_exempt(b, exempt))


def version_only_conflict(repo_dir, conflicted: list[str], rec_base: str, rec_head: str,
                          cur_base: str, cur_head: str) -> bool:
    """True only when every conflicted path is version-only (see the module
    docstring). Any missing entry, other mode, or JSON parse failure is False."""
    if not conflicted:
        return False
    for path in conflicted:
        exempt = pp.VERSION_EXEMPT.get(os.path.basename(path))
        if exempt is None:
            return False
        entries = [_entry(repo_dir, c, path) for c in (rec_base, rec_head, cur_base, cur_head)]
        if any(e is None or e[0] != "100644" for e in entries):
            return False
        (_, rb), (_, rh), (_, cb), (_, ch) = entries
        if not (_same_but_exempt(repo_dir, rb, rh, exempt)
                and _same_but_exempt(repo_dir, cb, ch, exempt)):
            return False
    return True
