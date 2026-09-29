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


def effective_helpers(checkout: Path) -> list[str]:
    """The credential helpers git will actually consult for github.com, in order.

    Reads the MERGED config (system, global, then local), not just `--local`:
    helper lists ACCUMULATE across config files, so a local helper written after
    the operator's global `!/usr/bin/gh auth git-credential` sits BEHIND it and is
    never reached -- git stops at the first helper that answers (reproduced with
    `git credential fill`, harmonic-forge#804 preclose). An empty value RESETS the
    list, so only what follows the last empty value counts.
    """
    result = subprocess.run(
        ["git", "-C", str(checkout), "config", "--get-all", HELPER_KEY],
        capture_output=True, text=True)
    if result.returncode != 0:
        return []
    helpers: list[str] = []
    for value in result.stdout.splitlines():
        if value == "":
            helpers = []
        else:
            helpers.append(value)
    return helpers


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
    want = expected_helper(slot)
    helpers = effective_helpers(checkout)
    if helpers != [want]:
        found = " then ".join(helpers) or "(none)"
        return check_cls("identity", FAIL,
                         f"{checkout}: git will consult {found} for {HELPER_KEY}; expected only "
                         f"{want} (fix: forge-onboard --apply)")
    _, email = _git_config(checkout, "--get", "user.email")
    return check_cls("identity", OK,
                     f"slot {account} authenticated; git helper points at it; "
                     f"user.email {email or '(unset)'}")


def apply_identity(project: Project, check_cls, dry_run: bool = False) -> list:
    """Make the slot helper the ONLY effective github.com credential helper.

    Writes a local RESET (an empty value) followed by the slot helper, into the repo's
    common `.git/config` -- one write, inherited by every `git worktree`. The reset is
    what makes it effective: without it the helper is appended behind the global one.
    """
    target = _applicable(project)
    if target is None:
        return []
    account, checkout = target
    want = expected_helper(slot_dir(account))
    if effective_helpers(checkout) == [want]:
        return [check_cls("identity helper", OK, "already the only effective helper")]
    if dry_run:
        return [check_cls("identity helper", OK, f"would set {HELPER_KEY} in {checkout}")]
    code, err = _git_config(checkout, "--replace-all", HELPER_KEY, "")
    if code == 0:
        code, err = _git_config(checkout, "--add", HELPER_KEY, want)
    return [check_cls("identity helper", OK if code == 0 else FAIL,
                      f"set {HELPER_KEY} in {checkout}" if code == 0 else err)]
