#!/usr/bin/env python3
"""Per-project GitHub identity for lane tooling (harmonic-forge#804).

`projects.toml` records an `account` per project, but until this module only the
belt read it. Every other tool ran `gh` as whichever login was globally active,
so `vitalharmony` got a `404` on `kenekted/kenekted-platform` and none of the lane
tooling could operate there at all.

**The mechanism is one `GH_CONFIG_DIR` per process, set at the entrypoint.** Every
`gh` child inherits it, so no call site needs wrapping and a new runner cannot
reintroduce the bug. It points at the project's `gh-as` slot
(`${GH_ACCT_HOME:-~/.config/gh-accounts}/<account>`). The global login is never
switched: no `gh auth switch`, no write to `hosts.yml`.

**An inherited `GH_TOKEN` is stripped, not tolerated.** `gh` gives `GH_TOKEN`
precedence over `GH_CONFIG_DIR` (tested 2026-09-28: `GH_TOKEN`=vitalharmony plus
the harmonicarchitect slot resolves to `vitalharmony`), so leaving one exported
would silently defeat the slot while every check still looked wired.

**Fails loudly on an unknown repo or a wrong login.** An unregistered repo raises
rather than falling back to the global login, and a slot that authenticates as a
different login exits non-zero before any write (R-0015).

Lives beside `manifest.py`, not in it: `manifest.py` was 294 lines and R-0006 caps a
source file at 300. Hooks import from `tools/onboard/`, never the reverse
(`batch_auth.py:164`'s one-way rule), and `slot_env` makes no `gh` call, so a hook
can use it without a network probe.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from manifest import ManifestError, Project, by_repo, load
from manifest_protocol import normalize_repo

#: Both take precedence over `GH_CONFIG_DIR` in `gh`, so both are removed.
STRIPPED_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")

#: slot dir -> login it was verified to authenticate as, per process.
_VERIFIED: dict[str, str] = {}


def slot_dir(account: str) -> Path:
    """The `gh-as` config directory for an account."""
    home = os.environ.get("GH_ACCT_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".config" / "gh-accounts"
    return base / account


def account_for(repo: str, path: Path | None = None) -> str:
    """The account a repo's lane tooling acts as. Never falls back."""
    key = normalize_repo(repo)
    project = by_repo(path).get(key)
    if project is None:
        raise ManifestError(
            f"no [[project]] entry for {key}; refusing to fall back to the "
            "global gh login")
    if not project.account:
        raise ManifestError(
            f"{key} is registered but declares no `account`; its identity "
            "cannot be resolved")
    return project.account


def project_for_path(path: str | Path, manifest: Path | None = None) -> Project:
    """The project whose checkout or lane worktree contains `path`.

    Matches the checkout, any `Project.worktrees` entry, or a subdirectory of
    either, preferring the deepest match. Worktrees are named from the CHECKOUT
    DIRECTORY and may live outside its parent (harmonic-forge's `worktree_dir`),
    which is why this reads `Project.worktrees` rather than guessing a sibling.
    """
    target = Path(path).expanduser().resolve()
    best: tuple[int, Project] | None = None
    for project in load(manifest):
        roots = [r for r in [project.checkout, *project.worktrees] if r is not None]
        for root in roots:
            if target == root or root in target.parents:
                if best is None or len(root.parts) > best[0]:
                    best = (len(root.parts), project)
    if best is None:
        raise ManifestError(f"{target} is not inside any registered checkout or lane worktree")
    return best[1]


def workspaces(manifest: Path | None = None) -> list[str]:
    """Every workspace an onboarded project declares, sorted and distinct (harmonic-forge#917).

    The belt's canonical table is generated from this, one entry per lane per
    workspace. Adding a workspace also adds three belt commands, which
    `watch_lane_posts.py`'s module docstring and `skills/belt-and-suspenders/
    DESIGN.md` list by hand; `test_belt_skill_matches_table.py` fails until both
    show them, by design (harmonic-forge#917 preclose). Here rather than in
    `manifest.py`, which is at R-0006's cap.
    """
    return sorted({p.workspace for p in load(manifest) if p.onboarded and p.workspace})


def workspace_for(path: str | Path | None, manifest: Path | None = None) -> str:
    """The workspace of the onboarded project that owns `path` (harmonic-forge#917).

    A checkout or its `-lane2`/`-lane3` worktree resolves through
    `project_for_path`. A per-issue worktree (`~/Harmonic_Projects/.worktrees/
    <repo>-<issue>-impl`) is registered nowhere, so on a miss this asks git for
    the worktree's common dir, whose parent is the main checkout, and retries
    there. Raises `ManifestError` naming `path` when neither resolves, including
    a stale worktree git no longer recognizes: an unresolved session must never
    read as some default workspace.
    """
    if not path:
        raise ManifestError("no path to resolve a workspace from")
    target = Path(path).expanduser()
    try:
        project = project_for_path(target, manifest)
    except ManifestError:
        try:
            result = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "--path-format=absolute",
                 "--git-common-dir"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ManifestError(f"{target}: cannot resolve a workspace ({exc})") from exc
        if result.returncode != 0 or not result.stdout.strip():
            raise ManifestError(
                f"{target} is not inside any onboarded checkout, lane worktree, or a "
                "git worktree of one; no workspace") from None
        project = project_for_path(Path(result.stdout.strip()).parent, manifest)
    if not project.onboarded or not project.workspace:
        raise ManifestError(f"{target}: {project.name} declares no workspace")
    return project.workspace


def slot_env(repo: str, path: Path | None = None) -> dict[str, str]:
    """`os.environ` with the repo's slot injected and tokens removed. No probe.

    For hooks and multi-repo sweeps, which pass it as a per-subprocess `env=`
    rather than mutating the process environment.
    """
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_TOKEN_VARS}
    env["GH_CONFIG_DIR"] = str(slot_dir(account_for(repo, path)))
    return env


def slot_env_or_none(repo: str | None, path: Path | None = None) -> dict[str, str] | None:
    """`slot_env`, or None when the repo is missing or unregistered.

    For hooks, which fail open on anything they cannot resolve rather than block
    a session: None means "inherit the caller's environment", exactly what they
    did before this module existed. Never raises and never probes.
    """
    if not repo:
        return None
    try:
        return slot_env(repo, path)
    except ManifestError:
        return None


def repo_for_path_or_none(path: str | Path | None) -> str | None:
    """The registered repo whose checkout or lane worktree contains `path`, else None.

    For hooks: it lets them name the repo (and so its slot) before any `gh`
    call, and never raises, so an unregistered directory just falls back to what
    the hook did before.
    """
    if not path:
        return None
    try:
        return project_for_path(path).repo
    except (ManifestError, OSError):
        return None


class ProbeUnavailable(RuntimeError):
    """`gh api user` failed for a reason that says nothing about the slot's credential."""


#: stderr fragments `gh` prints when the credential itself is missing or rejected.
AUTH_FAILURE_MARKERS = ("401", "not logged in", "bad credentials", "authentication",
                         "gh auth login", "no oauth token")


def _probe_login(env: dict[str, str] | None = None) -> str:
    """The login `gh` authenticates as under `env` (default: the current environment).

    Returns '' when the credential is missing or rejected. Raises `ProbeUnavailable`
    when the probe could not decide (network, 5xx, timeout, budget refusal): that is
    not evidence the slot is broken, and must not send the operator to re-login.
    """
    try:
        result = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                                capture_output=True, text=True, timeout=30,
                                env=os.environ if env is None else env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeUnavailable(f"{type(exc).__name__}: {exc}") from exc
    if result.returncode == 0:
        return result.stdout.strip()
    stderr = (result.stderr or "").strip()
    if any(marker in stderr.lower() for marker in AUTH_FAILURE_MARKERS):
        return ""
    raise ProbeUnavailable(stderr.splitlines()[0] if stderr else f"gh exited {result.returncode}")


def apply_project_identity(repo: str, path: Path | None = None) -> str:
    """Point this process's `gh` at the repo's slot, and refuse a wrong login.

    Call once per entrypoint, right after the repo is resolved and before the
    first `gh` call. Returns the account. The probe runs once per slot per
    process; a mismatch exits non-zero before the caller can write anything.
    """
    account = account_for(repo, path)
    slot = slot_dir(account)
    os.environ["GH_CONFIG_DIR"] = str(slot)
    for var in STRIPPED_TOKEN_VARS:
        os.environ.pop(var, None)

    login = _VERIFIED.get(str(slot))
    if login is None:
        try:
            login = _probe_login()
        except ProbeUnavailable as exc:
            raise SystemExit(
                f"identity: could not verify slot {slot} for account {account} "
                f"(gh api user failed: {exc}); this is not evidence the slot is broken, "
                "retry when GitHub is reachable") from exc
        if not login:
            raise SystemExit(
                f"identity: slot {slot} for account {account} is not "
                f"authenticated (repair with: gh-as --init {account})")
        _VERIFIED[str(slot)] = login
    if login.lower() != account.lower():
        raise SystemExit(
            f"identity mismatch: {repo} needs {account}, slot authenticates as {login}")
    return account
