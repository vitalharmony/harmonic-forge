#!/usr/bin/env python3
"""`forge_onboard`'s per-project identity checks (harmonic-forge#804).

Kept out of `forge_onboard.py`, which is already far past R-0006's 300 lines.

Two halves of one contract. The lane tooling acts as `Project.account` through
that account's `gh-as` slot (`manifest_identity.py`); `git push` must act as the
same account, and git's credential helper is per-repository configuration that
no tracked file can set. So `check_identity` reports drift on both, and
`apply_identity` writes the helper, into the repo's COMMON `.git/config` -- one
write, which every `git worktree` of that checkout inherits.

`user.email` is reported, never prescribed: the right value is the operator's
(kenekted-platform commits as `marc@kenekted.ai`), and a check that demanded one
would be a check that guessed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from manifest import Project
from manifest_identity import slot_dir

OK, FAIL, SKIP = "ok", "FAIL", "skip"

#: git's key for a helper scoped to github.com over HTTPS.
HELPER_KEY = "credential.https://github.com.helper"


def expected_helper(slot: Path) -> str:
    """The credential helper line that authenticates a push as the slot's account."""
    return f"!GH_CONFIG_DIR={slot} /usr/bin/gh auth git-credential"


def _git_config(checkout: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(["git", "-C", str(checkout), "config", "--local", *args],
                            capture_output=True, text=True)
    return result.returncode, result.stdout.strip()


def _slot_login(slot: Path) -> str:
    """The login this slot authenticates as, or '' when it does not."""
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    env["GH_CONFIG_DIR"] = str(slot)
    try:
        result = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                                capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _applicable(project: Project):
    """(account, checkout) when this project has an identity to check, else None."""
    if not (project.repo and project.account and project.checkout):
        return None
    if not (project.checkout / ".git").exists():
        return None
    return project.account, project.checkout


def check_identity(project: Project, check_cls):
    """Slot present and authenticating as `account`; git helper points at it.

    `check_cls` is `forge_onboard.Check`, passed in so this module never imports
    the module that imports it.
    """
    target = _applicable(project)
    if target is None:
        return check_cls("identity", SKIP, "no registered checkout with an account")
    account, checkout = target
    slot = slot_dir(account)
    if not slot.is_dir():
        return check_cls("identity", FAIL,
                         f"slot {slot} is missing (create it with: gh-as --init {account})")
    login = _slot_login(slot)
    if not login:
        return check_cls("identity", FAIL,
                         f"slot {slot} is not authenticated (repair: gh-as --init {account})")
    if login.lower() != account.lower():
        return check_cls("identity", FAIL,
                         f"slot {slot} authenticates as {login}, but the manifest says {account}")
    _, helper = _git_config(checkout, "--get", HELPER_KEY)
    want = expected_helper(slot)
    if helper != want:
        found = helper or "(none)"
        return check_cls("identity", FAIL,
                         f"{checkout}: {HELPER_KEY} is {found}, expected {want} "
                         "(fix: forge-onboard --apply)")
    _, email = _git_config(checkout, "--get", "user.email")
    return check_cls("identity", OK,
                     f"slot {account} authenticated; git helper points at it; "
                     f"user.email {email or '(unset)'}")


def apply_identity(project: Project, check_cls, dry_run: bool = False) -> list:
    """Write the per-repo credential helper wherever it is missing or points elsewhere."""
    target = _applicable(project)
    if target is None:
        return []
    account, checkout = target
    want = expected_helper(slot_dir(account))
    _, have = _git_config(checkout, "--get", HELPER_KEY)
    if have == want:
        return [check_cls("identity helper", OK, "already points at the slot")]
    if dry_run:
        return [check_cls("identity helper", OK, f"would set {HELPER_KEY} in {checkout}")]
    code, err = _git_config(checkout, "--replace-all", HELPER_KEY, want)
    return [check_cls("identity helper", OK if code == 0 else FAIL,
                      f"set {HELPER_KEY} in {checkout}" if code == 0 else err)]
