"""Which issue a lane worktree is building (harmonic-forge#843).

One resolver for the downshift reminder's two "deep issue in hand" probes.

**The branch is authoritative** (AC2): `watch_lane_posts.resolve_worktree`,
the belt's own resolver, reads the worktree's current branch and maps an
issue prefix (`l2/f843-…`) through the onboarding manifest, so a forge issue
is never mistaken for an hrse issue of the same number. It is used rather than
`discover_from_worktree`, which drops the reason and so cannot tell a detached
HEAD from a branch that names no issue.

**Detached HEAD only** -- a Lane 2 mid-rebase, or a worktree checked out at a
bare SHA -- falls back to the path: the issue number from `<stem>-<N>-impl`
and the repository from that worktree's own `origin` remote. No prefix or
dir-stem table is involved; the onboarded prefixes are single letters (`h`,
`f`, ...) and do not match `hrse2-`/`forge-` stems. Every other unresolved
case (not a git worktree, a branch naming no issue) is not counted.

`watch_lane_posts` runs `onboard_manifest.prefix_repos()` at import time,
which can raise, so it is imported inside `issue_for_worktree` and every
caller wraps that call in its own fail-quiet guard.

`resolve_worktree`'s git calls carry no timeout of their own; the reminder
bounds them with a whole-probe deadline.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_IMPL_DIR_RE = re.compile(r"-(?P<number>\d+)-impl$")
_IMPL_ROOTS = (
    str(Path.home() / "Harmonic_Projects" / ".worktrees"),
    "/tmp",
)


def is_impl_worktree(path: str) -> bool:
    """AC2's filter only: a `<stem>-<N>-impl` directory directly under one of
    the two implementation-worktree roots. It never decides repo or issue."""
    parent, name = str(Path(path).parent), Path(path).name
    roots = {root for root in _IMPL_ROOTS} | {str(Path(root).resolve()) for root in _IMPL_ROOTS}
    return parent in roots and bool(_IMPL_DIR_RE.search(name))


def issue_for_worktree(path: str) -> tuple[str, int] | None:
    """`(repo, issue)` the worktree at `path` is building, or None."""
    gh_dir = str(Path(__file__).resolve().parent.parent / "gh")
    if gh_dir not in sys.path:
        sys.path.insert(0, gh_dir)
    import watch_lane_posts  # noqa: PLC0415 -- may raise; callers fail quiet

    pair, reason = watch_lane_posts.resolve_worktree(path)
    if pair is not None:
        return pair[0].lower(), pair[1]
    if not reason.startswith("detached HEAD"):
        return None
    match = _IMPL_DIR_RE.search(Path(path).name)
    repo = watch_lane_posts._worktree_repo(path) if match else None
    return (repo.lower(), int(match.group("number"))) if repo else None
