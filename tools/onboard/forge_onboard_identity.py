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

import hashlib
import os
import subprocess
from pathlib import Path

from manifest import Project
from manifest_identity import ProbeUnavailable, _probe_login, slot_dir

OK, FAIL, SKIP = "ok", "FAIL", "skip"

#: git's key for a helper scoped to github.com over HTTPS.
HELPER_KEY = "credential.https://github.com.helper"


def expected_helper(slot: Path) -> str:
    """The credential helper line that authenticates a push as the slot's account."""
    # `gh` ranks GH_TOKEN/GITHUB_TOKEN above GH_CONFIG_DIR, so an exported token would
    # silently make a push authenticate as the token's account. Strip both, exactly as
    # `gh-as` and `manifest_identity` do (harmonic-forge#804 preclose).
    return f"!env -u GH_TOKEN -u GITHUB_TOKEN GH_CONFIG_DIR={slot} /usr/bin/gh auth git-credential"


def _git_config(checkout: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(["git", "-C", str(checkout), "config", "--local", *args],
                            capture_output=True, text=True)
    # git writes its diagnostics to stderr; a failed call must surface them.
    return result.returncode, (result.stdout if result.returncode == 0 else result.stderr).strip()


def _local_helpers(checkout: Path) -> list[str]:
    """The checkout's own `--local` helper values, in order, EMPTY entries preserved.

    An empty value is the reset that makes the slot helper the only effective one, so
    a restore that dropped it (as `.strip()` on the joined output does) would leave the
    global helper answering first.
    """
    result = subprocess.run(
        ["git", "-C", str(checkout), "config", "--local", "--get-all", HELPER_KEY],
        capture_output=True, text=True)
    return result.stdout.split("\n")[:-1] if result.returncode == 0 else []


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
    """The login this slot authenticates as, or '' when its credential is missing or rejected.

    Raises `ProbeUnavailable` when the probe could not decide (outage, 5xx, budget refusal):
    that says nothing about the slot and must not read as "re-run gh-as --init".
    """
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    env["GH_CONFIG_DIR"] = str(slot)
    return _probe_login(env)


#: Token prefixes by kind. Classification is by PREFIX ONLY: the value is never printed, logged
#: or returned to a caller that could (harmonic-forge#805, R-0368).
_TOKEN_KINDS = (("github_pat_", "fine-grained"), ("gho_", "oauth"), ("ghu_", "oauth"),
                ("ghp_", "classic"))
FINE_GRAINED = "fine-grained"


def _token(config_dir: Path | None) -> str:
    """The token `gh` resolves under `config_dir` (None: the default config), or ''.

    Held only long enough to classify or hash it; callers must not print it.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    env.pop("GH_CONFIG_DIR", None)
    if config_dir is not None:
        env["GH_CONFIG_DIR"] = str(config_dir)
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                                timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def token_kind(token: str) -> str:
    """`fine-grained` | `oauth` | `classic` | `unknown` | `none`, from the prefix alone."""
    if not token:
        return "none"
    for prefix, kind in _TOKEN_KINDS:
        if token.startswith(prefix):
            return kind
    return "unknown"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _applicable(project: Project):
    """(account, checkout-or-None) when this project has an account to check, else None.

    The slot is a machine-global resource, so it is checked even when the project has
    no local clone yet; only the git-helper half needs a checkout.
    """
    if not project.account:
        return None
    checkout = project.checkout
    if not (checkout and (checkout / ".git").exists()):
        checkout = None
    return project.account, checkout


def check_identity(project: Project, check_cls):
    """Slot present and authenticating as `account`; git helper points at it.

    `check_cls` is `forge_onboard.Check`, passed in so this module never imports
    the module that imports it.
    """
    target = _applicable(project)
    if target is None:
        return check_cls("identity", SKIP, "no account registered")
    account, checkout = target
    slot = slot_dir(account)
    if not slot.is_dir():
        return check_cls("identity", FAIL,
                         f"slot {slot} is missing (create it with: gh-as --init {account})")
    try:
        login = _slot_login(slot)
    except ProbeUnavailable as exc:
        return check_cls("identity", FAIL,
                         f"could not verify slot {slot} (gh api user failed: {exc}); this is not "
                         "evidence the slot is broken, re-run when GitHub is reachable")
    if not login:
        return check_cls("identity", FAIL,
                         f"slot {slot} is not authenticated (repair: gh-as --init {account})")
    if login.lower() != account.lower():
        return check_cls("identity", FAIL,
                         f"slot {slot} authenticates as {login}, but the manifest says {account}")
    token = _token(slot)
    kind = token_kind(token)
    if kind in ("none", "unknown"):
        # An unreadable credential (locked keyring, gh missing, timeout) is not "no exception
        # needed": it must never pass, exception or not.
        return check_cls("identity", FAIL,
                         f"the token for slot {slot} is {'unreadable (gh auth token failed)' if kind == 'none' else 'of an unrecognized kind'}; "
                         "its type cannot be checked, exception or not")
    exception = (project.token_exception or "").strip()
    if kind != FINE_GRAINED and not exception:
        return check_cls(
            "identity", FAIL,
            f"slot {slot} holds a {kind} token, not a fine-grained PAT, and projects.toml records "
            "no token_exception for it (R-0368). Create the PAT, then load it: GH_CONFIG_DIR="
            f"{slot} gh auth login --with-token --insecure-storage; or record token_exception "
            "with its reason")
    if kind == FINE_GRAINED and token and _digest(token) == _digest(_token(None)):
        return check_cls(
            "identity", FAIL,
            f"slot {slot} resolves to the same credential as the default gh login: the token "
            "was stored in the shared OS keyring, so it replaced that login too. Reload it with "
            "--insecure-storage")
    note = f"token {kind}" + (f" (exception: {exception[:70]})" if kind != FINE_GRAINED else "")
    if checkout is None:
        return check_cls("identity", OK,
                         f"slot {account} authenticated, {note}; no local checkout, git helper "
                         "not checked")
    want = expected_helper(slot)
    helpers = effective_helpers(checkout)
    if not helpers:
        return check_cls("identity", FAIL,
                         f"{checkout}: NO {HELPER_KEY} at all, so git push cannot authenticate "
                         "(an interrupted forge-onboard --apply leaves this); expected "
                         f"{want} (fix: forge-onboard --apply)")
    if helpers != [want]:
        found = " then ".join(helpers)
        return check_cls("identity", FAIL,
                         f"{checkout}: git will consult {found} for {HELPER_KEY}; expected only "
                         f"{want} (fix: forge-onboard --apply)")
    _, email = _git_config(checkout, "--get", "user.email")
    return check_cls("identity", OK,
                     f"slot {account} authenticated, {note}; git helper points at it; "
                     f"user.email {email or '(unset)'}")


def _restore_helpers(checkout: Path, prior: list[str]) -> None:
    _git_config(checkout, "--unset-all", HELPER_KEY)
    for value in prior:
        _git_config(checkout, "--add", HELPER_KEY, value)


def apply_identity(project: Project, check_cls, dry_run: bool = False) -> list:
    """Make the slot helper the ONLY effective github.com credential helper.

    Writes a local RESET (an empty value) followed by the slot helper, into the repo's
    common `.git/config` -- one write, inherited by every `git worktree`. The reset is
    what makes it effective: without it the helper is appended behind the global one.
    """
    target = _applicable(project)
    if target is None or target[1] is None:
        return []
    account, checkout = target
    slot = slot_dir(account)
    if not slot.is_dir():
        # Writing the helper first would wipe the working global one and leave a dead
        # slot behind; refuse until the slot exists.
        return [check_cls("identity helper", FAIL,
                          f"slot {slot} is missing; refusing to write a helper that points "
                          f"at it (create it with: gh-as --init {account})")]
    want = expected_helper(slot)
    if effective_helpers(checkout) == [want]:
        return [check_cls("identity helper", OK, "already the only effective helper")]
    if dry_run:
        return [check_cls("identity helper", OK, f"would set {HELPER_KEY} in {checkout}")]
    prior = _local_helpers(checkout)
    code, err = _git_config(checkout, "--replace-all", HELPER_KEY, "")
    if code == 0:
        try:
            code, err = _git_config(checkout, "--add", HELPER_KEY, want)
        except BaseException:
            # Ctrl-C / SIGTERM between the reset and the add: put the prior values back
            # before propagating, or the checkout is left with no github.com helper.
            _restore_helpers(checkout, prior)
            raise
        if code != 0:
            # The reset already landed: restore what was there, or the checkout is left
            # with no github.com helper at all and every push from it (and its worktrees)
            # fails.
            _restore_helpers(checkout, prior)
            err = f"{err} (prior local helpers restored)"
    return [check_cls("identity helper", OK if code == 0 else FAIL,
                      f"set {HELPER_KEY} in {checkout}" if code == 0 else err)]
