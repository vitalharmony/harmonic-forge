"""git's own answers about a checkout, for the `.claude/forge.toml` resolver
(harmonic-forge#1527), split from it for R-0006's 300-line cap. Every git call
removes the caller's GIT_* location variables, so the environment a hook or
launcher inherits never redirects which repository is asked. Stdlib only.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from manifest_protocol import ManifestError, origin_repo

_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


def checkout_root(start: Path) -> Path | None:
    """The nearest of `start` (resolved) and its ancestors holding a `.git` entry."""
    here = Path(start).resolve()
    for candidate in (here, *here.parents):
        if os.path.lexists(candidate / ".git"):
            return candidate
    return None


def _git_lines(*args: str) -> list[str] | None:
    """`git <args>`'s stdout lines with the caller's GIT_* location variables
    removed, or None when git fails."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV}
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True,
                                timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.splitlines() if result.returncode == 0 else None


def _layout(where: Path) -> tuple[Path, Path, Path] | None:
    """git's `(common dir, git dir, top level)` for `where`, all absolute."""
    out = _git_lines("-C", str(where), "rev-parse", "--path-format=absolute",
                     "--git-common-dir", "--git-dir", "--show-toplevel")
    if out is None or len(out) != 3:
        return None
    return Path(out[0]).resolve(), Path(out[1]).resolve(), Path(out[2]).resolve()


def main_checkout(root: Path) -> Path:
    """The main checkout of a checkout root, from git's own answer (R3).

    git names the shared (common) directory; a checkout whose own git dir is
    that directory IS the main worktree. A linked worktree's main is git's
    pointer to it: the common directory's `core.worktree` (a submodule) or, by
    git's own convention, the common directory's parent. Either candidate must
    then report that same common directory as its own git dir, so a bare
    repo's worktree and a `--separate-git-dir` repo's linked worktree, where git
    records no main worktree, raise instead of naming a guessed directory."""
    layout = _layout(root)
    if layout is None or layout[2] != Path(root).resolve():
        raise ManifestError(f"{root}: git does not report it as a checkout root")
    common, gitdir, top = layout
    if gitdir == common:
        return top
    pointer = _git_lines("--git-dir", str(common), "config", "--get", "core.worktree")
    candidate = (common / pointer[0]).resolve() if pointer else common.parent
    found = _layout(candidate) if candidate.is_dir() else None
    if found is None or found[0] != common or found[1] != common or found[2] != candidate:
        raise ManifestError(f"{root}: git names no main worktree for {common}")
    return candidate


def origin_of(main: Path) -> str:
    """The normalized repo `main`'s origin names, with GIT_* location variables
    removed so the caller's environment never redirects which repo is asked."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV}
    try:
        result = subprocess.run(["git", "-C", str(main), "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError(f"cannot read the origin of {main}: {exc}") from exc
    url = result.stdout.strip()
    if result.returncode != 0 or not url:
        raise ManifestError(f"cannot read the origin of {main}: it has no origin remote")
    try:
        return origin_repo(url)
    except ManifestError as exc:
        raise ManifestError(f"cannot read the origin of {main}: {exc}") from exc
