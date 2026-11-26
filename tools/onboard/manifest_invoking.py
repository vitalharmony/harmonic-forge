"""The posting entrypoints' repo lookup, falling back to the invoking checkout's
overlay (harmonic-forge#1579).

**R1's third consumer, by operator ruling (harmonic-forge#1579 Q2).** Besides the
launcher and `ci_plan`, only this module reads the overlay, for
`manifest_identity.apply_project_identity` and for `require_onboarded` in
`l1_post` and `preclose_check`. Never a hook: hooks, `slot_env`, `account_for`
and every global lookup and prefix map read `projects.toml` alone
(`test_manifest_invoking` pins it).

**The fallback is narrow.** A `projects.toml` hit always wins and reads no
overlay. On a miss, the process cwd's checkout is resolved by
`manifest_overlay.project_for_checkout` (origin-bound, main checkout only) and
used only when it names exactly the repo passed, and never when the repo's owner
is an account `projects.toml` declares (operator ruling, second round).

**Who may post (Q3(b)).** An overlay-only account is the repo's owner. A
user-owned repo keeps login == account. No login equals an organization, so
`org_push` accepts one only when GitHub says it has push; anything else refuses.

**Identity and the comment only (re-scope, rulings D13 and D14).** An
overlay-only repo gets no board and no CI account: `refuse_board_ci` is the one
guard, called by the board resolvers (`l1_post.resolve_project_board`,
`gh_issue.resolve_board_for_repo` and gh_issue's pre-create check) and by
gate_ci's CI reader, so each refuses by name. Posting runs on the ambient login
outside a lane only; inside one it refuses (`apply_project_identity`).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import manifest_overlay
import manifest_slots
from manifest import ManifestError, Project, by_repo, manifest_path, require_onboarded_repo
from manifest_git import checkout_root
from manifest_protocol import normalize_repo

def _fallback(key: str, cwd: str | Path | None, path: Path | None, miss: str) -> Project:
    """The cwd checkout's overlay Project for exactly `key`, or a refusal."""
    root = checkout_root(Path(cwd) if cwd else Path.cwd())
    if root is None:
        raise ManifestError(miss)
    project = manifest_overlay.project_for_checkout(root, path)
    if project is None or project.repo != key:
        raise ManifestError(f"{miss} or in this checkout's .claude/forge.toml")
    owner = key.split("/")[0]
    if owner.casefold() in manifest_slots.declared(path):
        raise ManifestError(
            f"{key} is registered only by its .claude/forge.toml, but its owner '{owner}' "
            "is an account projects.toml declares; register the repo there (harmonic-forge#1579)")
    return project


def project_for_repo(repo: str, cwd: str | Path | None = None,
                     path: Path | None = None) -> Project:
    """`projects.toml`'s Project for `repo`, else the invoking checkout's."""
    key = normalize_repo(repo)
    project = by_repo(path).get(key)
    return project or _fallback(key, cwd, path, f"no [[project]] entry for {key}; refusing "
                                "to fall back to the global gh login")


def require_onboarded(repo: str, cwd: str | Path | None = None,
                      path: Path | None = None) -> Project:
    """`manifest.require_onboarded_repo`, with the same overlay fallback and messages."""
    target = path or manifest_path()
    key = normalize_repo(repo)
    if key in by_repo(path):
        return require_onboarded_repo(key, path)
    project = _fallback(key, cwd, path, f"{target}: no [[project]] entry for {key}; "
                        "the registry is closed")
    if not project.onboarded:
        raise ManifestError(f"{target}: {key} is declared with onboarded = false; refusing")
    if project.protocol is None:
        raise ManifestError(f"{target}: {key} has no [project.protocol] declaration")
    return project


class OverlayOnlyRefusal(ManifestError):
    """Ruling D13: a board or CI step on a repo known only from its forge.toml."""


#: The one named refusal (ruling D13). "the follow-up" is filled in by Lane 1.
BOARD_CI_REFUSAL = ("{repo} is registered only by its forge.toml; board/CI support is "
                    "the follow-up (harmonic-forge#1579 ruling D13), so its {step} refuses")


def refuse_board_ci(repo: str, step: str, cwd: str | Path | None = None,
                    path: Path | None = None) -> None:
    """Ruling D13, the one guard: a repo known only from its forge.toml gets no
    board and no CI account. Raises `OverlayOnlyRefusal` naming `step` when `repo`
    is not in `projects.toml` and the cwd checkout's overlay registers it. A
    `projects.toml` repo returns at once and reads no overlay; a repo neither
    registers returns too, so the caller's own lookup refuses it as before. No
    overlay board field is ever read, so a forge.toml cannot pick a board (round 2,
    the hostile-author survivor)."""
    key = normalize_repo(repo)
    if key in by_repo(path):
        return
    try:
        _fallback(key, cwd, path, key)
    except ManifestError:
        return
    raise OverlayOnlyRefusal(BOARD_CI_REFUSAL.format(repo=key, step=step))


def org_push(repo: str, path: Path | None = None) -> bool:
    """True only when `repo` is overlay-only, organization-owned, and the current
    `gh` login has push on it. Raises SystemExit when GitHub cannot answer."""
    key = normalize_repo(repo)
    if key in by_repo(path):
        return False  # a registered repo keeps login == account
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{key}", "--jq", ".owner.type, .permissions.push"],
            capture_output=True, text=True, timeout=30, env=os.environ)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"identity: could not read push permission on {key} ({exc})") from exc
    if result.returncode != 0:
        detail = ((result.stderr or "").strip().splitlines() or [f"gh exited {result.returncode}"])[0]
        raise SystemExit(f"identity: could not read push permission on {key} ({detail})")
    return result.stdout.split() == ["Organization", "true"]
