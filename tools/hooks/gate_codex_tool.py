#!/usr/bin/env python3
"""Codex PreToolUse guard for persistent-mutation command paths.

The git-mutation deny below is LANE-conditional (harmonic-forge#152):
`LANE` is confirmed (harmonic-forge#148/#179) to propagate into this
hook's subprocess environment, so it is no longer true that Codex hooks
have no trustworthy Lane-role selector. `git commit`/`add`/`push`/
`merge`/`rebase` and pathspec-form `checkout`/`switch` are now denied
only for a genuinely Lane-3 session (three-case `LANE` precedence,
mirroring harmonic-forge#151's Claude-Code-side check) -- Lane 2 Codex
sessions get their normal git surface back. The other checks below
(sudo, shell/python `-c` indirection, package installs) remain
unconditional for every Codex shell session, and a `gate-*`-task check
symmetric with the Claude Code side is layered on separately. The Lane 3
skill remains explicit that a gate must start in a read-only Codex
permission session; this hook is defense in depth, not a replacement for
that session boundary.

Also denies raw GitHub issue posting (harmonic-forge#190), but only for a
Lane 1 (or LANE-unset) session -- Lane 2/3 Codex sessions may post their
own results directly, mirroring the LANE-conditional carve-out added to
the canonical Claude-Code-side hook at the same time. Before this, Codex
had no equivalent of the canonical hook's raw-post block at all, so every
Codex Lane 3 gate in this project's history posted its results directly
while Claude Lane 3 sessions were unconditionally blocked -- an
accidental asymmetry discovered live during a Lane 3 gate, not a deliberate
design. Both CLIs now enforce the identical rule.

A Codex Lane 3 session once treated the per-issue Lane 2 worktree
convention as applying to Lane 3 readiness too, creating fresh
`/tmp/<project>-<issue>-gate` worktrees via raw `git worktree add` and then
reporting missing venv/node_modules/.env as blockers -- three real gates
thrashed on this the same day. A project has exactly one configured Lane 3
checkout, the sibling `<project>-lane3` worktree; the
correct path is always `mise run gate-checkout <branch>` then `mise run
lane3-begin --issue <N>` from inside it. Two additions close this off
mechanically rather than relying on remembered prose: `git checkout`/
`switch`/`worktree add` are now denied outright for a genuinely Lane-3
session (not just the pathspec-form subset denied below), and a Lane-3-only
gate task is denied unless the cwd it runs from actually resolves to that
canonical sibling worktree.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# The Lane 3 cloud-CLI policy, shared with the Claude Code side so
# one allow-list cannot drift into two. Same cross-repo import idiom already
# used for the canonical hook.
# harmonic-forge#720: this hook now lives beside the policy it loads, so it
# loads its SIBLING -- never a hard-coded ~/harmonic-forge, which is a
# different checkout whenever this one is a worktree.
_HOOKS_DIR = Path(__file__).resolve().parent
_CLOUD_POLICY_PATH = _HOOKS_DIR / "lane3_cloud_cli_policy.py"
_cloud_spec = importlib.util.spec_from_file_location("lane3_cloud_cli_policy", _CLOUD_POLICY_PATH)
assert _cloud_spec and _cloud_spec.loader
_cloud_policy = importlib.util.module_from_spec(_cloud_spec)
_cloud_spec.loader.exec_module(_cloud_policy)

# The policy module already loaded the canonical hook; reuse that instance
# instead of exec'ing a second copy. One object across both
# modules means the Lane 3 probe and everything else here agree by
# construction -- two instances would silently diverge under patching.
_CANONICAL_HOOK_PATH = _cloud_policy._CANONICAL_PATH
_canonical = _cloud_policy._canonical

# harmonic-forge#407 Q1: the Claude-Code-side AE/gate-readiness-sweep
# self-authorization guard was registered only in one consuming repo's tracked
# `.claude/settings.json` -- a Codex-filled Lane 3 session had no
# equivalent at all. Same cross-repo import idiom as the cloud-CLI policy
# above: reuse the canonical hook's own `decision()` rather than
# re-deriving its LANE check and body-extraction logic a second time.
_AE_SWEEP_HOOK_PATH = _HOOKS_DIR / "deny_lane3_ae_self_post.py"
_ae_sweep_spec = importlib.util.spec_from_file_location("deny_lane3_ae_self_post", _AE_SWEEP_HOOK_PATH)
assert _ae_sweep_spec and _ae_sweep_spec.loader
_ae_sweep_hook = importlib.util.module_from_spec(_ae_sweep_spec)
_ae_sweep_spec.loader.exec_module(_ae_sweep_hook)


def deny(reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    return 0


def allow() -> int:
    return 0


def normalize(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def is_lane3_session(cwd: Path) -> bool:
    """Three-case LANE precedence (harmonic-forge#151): LANE == "3" is
    positive evidence of Lane 3; LANE set to anything else is positive
    evidence it is not; LANE unset falls back to the fresh LANE3_ACTIVE
    marker check.

    The logic lives in the shared layer
    (`lane3_cloud_cli_policy.is_lane3_session`) and this delegates to it.
    The shared cloud-CLI policy needs the same probe, and having it import
    this function would invert the dependency direction and
    cycle -- this module already imports that one. Moving the probe down
    and delegating up keeps exactly one copy."""
    return _cloud_policy.is_lane3_session(cwd)


def gate_task_denial(command: str, cwd: Path) -> str | None:
    """Segment-aware check for Lane-3-only mise tasks, symmetric with the
    Claude Code side (harmonic-forge#151). Tracks `cd` across shell
    control operators the same way the canonical decision() does, so a
    compound command like `cd <project>-lane3 && mise run gate-checkout foo`
    is caught, not just the bare form."""
    effective_cwd = cwd
    for segment in _canonical.command_segments(command):
        if len(segment) == 2 and segment[0] == "cd":
            target = Path(segment[1]).expanduser()
            effective_cwd = target if target.is_absolute() else effective_cwd / target
            continue
        task = _canonical.lane3_task_without_marker(segment, effective_cwd)
        if task is not None:
            lane = os.environ.get("LANE")
            if lane is not None:
                return (
                    f"{task!r} is Lane-3-only, and this session was launched as "
                    f"Lane {lane} (harmonic-forge#151)."
                )
            return (
                f"{task!r} is Lane-3-only and no fresh LANE3_ACTIVE marker exists "
                "for this worktree (harmonic-forge#138)."
            )
    return None


# A pre-close adversarial panel (5 fresh-context
# refuters, unanimous) found the round-1 resolver fatally self-referential
# -- it derived "canonical" from `git rev-parse --show-toplevel` of the
# very cwd being checked, so from a stray `/tmp/<project>-<N>-gate`
# worktree it answered with that worktree's own (nonexistent) `-lane3`
# sibling, and any directory an attacker (or a confused Codex session)
# happened to *name* `...-lane3` passed trivially. Worse: this hook script
# itself is invoked via `python3 "$(git rev-parse --show-toplevel)/scripts/
# gate_codex_tool.py"` (`.codex/hooks.json`) -- so a stray worktree runs
# ITS OWN copy of this file, meaning `Path(__file__)` is exactly as
# spoofable as cwd and cannot anchor the check either.
#
# The fix: anchor on something that is identical no matter which
# worktree's copy of this script is running. Every worktree of one repo
# shares the same `.git` (worktrees are a view over one object store), so
# `git worktree list --porcelain` returns the *authoritative, git-maintained*
# registry of every real worktree -- not a guessed path string -- and its
# FIRST entry is always the main worktree. The canonical Lane 3 location is
# that main worktree's `<name>-lane3` sibling, the same convention the
# platform's `lane3` launcher resolves (`tools/lane/lane3`), and it is
# trusted only if it is itself an actually-registered worktree. A path
# nobody registered is never "canonical" no matter what it's named.
#
# harmonic-forge#720: this used to hard-code one consuming repo's path and
# accept only that repo's remote. Deriving it from the registry gives that
# repo the same answer and every other repo its own.


def _canonical_lane3_worktree(cwd: Path) -> Path | None:
    """Non-spoofable canonical Lane 3 worktree for the repo `cwd` belongs
    to, or None if it cannot be established. Returns None (not a guess) if
    the registry can't be read or the `<main>-lane3` sibling isn't an
    actually-registered worktree of this repo."""
    listing = subprocess.run(
        ["git", "-C", str(cwd), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    if listing.returncode:
        return None
    try:
        worktrees = [
            Path(line.split(" ", 1)[1]).resolve()
            for line in listing.stdout.splitlines()
            if line.startswith("worktree ")
        ]
    except (IndexError, OSError):
        return None
    if not worktrees:
        return None
    main = worktrees[0]
    candidate = main.parent / f"{main.name}-lane3"
    try:
        if candidate.resolve() not in set(worktrees):
            return None
    except OSError:
        return None
    return candidate


# The previous single-set `LANE3_ONLY_TASKS` check
# excluded `lane3-begin`/`lane3-end` -- the panel confirmed live that
# `lane3-begin` is the literal command that failed with "dedicated
# worktree lacks required runtime configuration" in the incident above,
# and the guard never covered it at all. This location check needs the
# full gate-session surface, not just the three `mise-run-gate-*` tasks
# `LANE3_ONLY_TASKS` was defined for (a different concern: which tasks are
# Lane-3-only by role, not which tasks must run from the canonical path).
_GATE_LOCATION_TASKS = frozenset(_canonical.LANE3_ONLY_TASKS) | {"lane3-begin", "lane3-end"}

def _recovery_text(canonical: Path | None) -> str:
    where = str(canonical) if canonical else "<project>-lane3"
    return f"cd {where} && mise run gate-checkout <branch> && mise run lane3-begin --issue <N>"


def gate_worktree_location_denial(command: str, cwd: Path, is_lane3: bool) -> str | None:
    """A Lane-3-only gate task run from anywhere other than the
    canonical Lane 3 worktree (or a subdirectory of it -- mise resolves
    tasks from any descendant, and a gate legitimately `cd`s into
    `backend/`/`frontend/` for individual verification steps) recreates
    the exact runtime-config failure a fresh ad hoc worktree produces --
    venv/node_modules/.env only exist in the canonical checkout, never in
    one `git worktree add` just created. `gate_task_denial` above already
    blocks the wrong-LANE case (Lane 2/1 running a Lane-3-only task); this
    adds the right-LANE-wrong-location case, which that check cannot catch
    since it never inspects cwd.

    Fails CLOSED (denies) when the canonical location cannot be verified --
    the opposite posture of round 1. A location check whose entire job is
    "are you in the one safe place" must treat "I can't tell" as "no": the
    round-1 fail-open let gate commands from a plain non-registry
    directory (including the fabricated recovery path round 1 itself
    printed) run completely unguarded."""
    if not is_lane3:
        return None
    effective_cwd = cwd
    for segment in _canonical.command_segments(command):
        if len(segment) == 2 and segment[0] == "cd":
            target = Path(segment[1]).expanduser()
            effective_cwd = target if target.is_absolute() else effective_cwd / target
            continue
        if len(segment) < 3 or segment[0] != "mise" or segment[1] != "run" or segment[2] not in _GATE_LOCATION_TASKS:
            continue
        canonical = _canonical_lane3_worktree(effective_cwd)
        task = segment[2]
        if canonical is None:
            return (
                f"{task!r} must run from the canonical Lane 3 worktree, "
                f"which could not be verified from {effective_cwd}. "
                f"Recovery: {_recovery_text(None)}"
            )
        try:
            resolved_cwd = effective_cwd.resolve()
            resolved_canonical = canonical.resolve()
        except OSError:
            return (
                f"{task!r} must run from the canonical Lane 3 worktree "
                f"({canonical}), and {effective_cwd} could not be resolved. "
                f"Recovery: {_recovery_text(canonical)}"
            )
        if resolved_cwd != resolved_canonical and resolved_canonical not in resolved_cwd.parents:
            return (
                f"{task!r} must run from the canonical Lane 3 worktree "
                f"({canonical}) or a subdirectory of it, not {effective_cwd}. "
                f"Recovery: {_recovery_text(canonical)}"
            )
    return None


def _git_subcommand(argv: list[str]) -> tuple[str, list[str]] | None:
    """Skip git's own global options (`-C <path>`, `--git-dir=X`,
    `--no-pager`, etc.) that can appear before the subcommand, so `git -C
    /elsewhere worktree add ...` is inspected as `("worktree", ["add",
    ...])`, not misread as the flag itself being the subcommand. Verified
    live by the pre-close panel to be exactly the bypass form an ad hoc
    worktree gate command uses in practice (a Lane 3 session's cwd is the
    lane3 worktree, not the target repo root, so `git -C <root> worktree
    add` is the natural spelling, not an exotic one)."""
    i = 0
    value_flags = {"-C", "--git-dir", "--work-tree", "-c", "--exec-path", "--namespace"}
    no_value_flags = {"--no-pager", "--paginate", "-p", "--bare", "--literal-pathspecs"}
    while i < len(argv):
        token = argv[i]
        if token in value_flags:
            i += 2
            continue
        if any(token.startswith(f"{flag}=") for flag in value_flags) or token in no_value_flags:
            i += 1
            continue
        if token.startswith("-"):
            i += 1  # unrecognized flag -- skip conservatively rather than misreading it as the subcommand
            continue
        return token, argv[i + 1:]
    return None


def git_mutation_denial(command: str, cwd: Path, is_lane3: bool) -> str | None:
    """Segment-aware, wrapper-aware replacement for the
    `worktree add`/`checkout`/`switch` checks round 1 added to the
    whole-string-only `blocked_reason` below. All five pre-close refuters
    independently reproduced the same bypass: `blocked_reason` receives
    one flat `shlex.split` of the *entire* command and inspects only
    `args[0]`/`args[1]`, so `cd x && git worktree add y`, `git -C <path>
    worktree add y`, and `env git worktree add y` all read as `program !=
    "git"` or `subcommand != "worktree"` and pass unexamined -- verified
    live pre-fix. This walks every shell segment the same way
    `gate_task_denial`/`gate_worktree_location_denial` already do, resolves
    each segment's real program through the shared wrapper-stripping
    helper (`env`/`sudo`/`timeout`/.../`VAR=value`, already exercised by
    `cloud_cli_denial`'s own tests), and skips git's own global options via
    `_git_subcommand` before checking the subcommand.

    Deliberately does not duplicate `blocked_reason`'s pre-existing
    commit/add/push/merge/rebase checks, which have the identical
    compound-command gap but predate this check and are out of its scope --
    flagged, not silently left implying full coverage."""
    if not is_lane3:
        return None
    effective_cwd = cwd
    for segment in _canonical.command_segments(command):
        if len(segment) == 2 and segment[0] == "cd":
            target = Path(segment[1]).expanduser()
            effective_cwd = target if target.is_absolute() else effective_cwd / target
            continue
        resolved = _cloud_policy._resolve_program(segment)
        if resolved is None:
            continue
        program, argv = resolved
        if program != "git":
            continue
        sub = _git_subcommand(argv[1:])
        if sub is None:
            continue
        subcommand, rest = sub
        if subcommand == "worktree" and rest[:1] == ["add"]:
            return (
                "git worktree add is not allowed for Lane 3 -- "
                f"use the canonical Lane 3 worktree instead: {_recovery_text(_canonical_lane3_worktree(effective_cwd))}"
            )
        if subcommand in {"checkout", "switch"}:
            return (
                f"git {subcommand} is not allowed for Lane 3 -- "
                f"use the canonical Lane 3 worktree instead: {_recovery_text(_canonical_lane3_worktree(effective_cwd))}"
            )
    return None


def direct_post_denial(command: str, cwd: Path) -> str | None:
    """Segment-aware check mirroring the canonical decision()'s Lane-1-only
    raw-GitHub-post block (harmonic-forge#190). Codex previously had no
    equivalent of this check at all -- Codex Lane 2/3 sessions could always
    post directly, while Claude Code sessions of every lane were blocked
    unconditionally, an accidental asymmetry discovered live during a gate.
    Both tools now enforce the same rule: Lane 1 (or LANE unset) must use
    `mise run l1-post`/`l1-comment`; Lane 2/3 sessions post their own
    results directly, on both CLIs."""
    if os.environ.get("LANE") not in (None, "1"):
        return None
    effective_cwd = cwd
    for segment in _canonical.command_segments(command):
        if len(segment) == 2 and segment[0] == "cd":
            target = Path(segment[1]).expanduser()
            effective_cwd = target if target.is_absolute() else effective_cwd / target
            continue
        if _canonical.is_direct_transport(segment, effective_cwd, prefer_cwd=True):
            return (
                "raw GitHub issue posting bypasses the Lane 1 capability "
                "wrappers. Use `mise run l1-post` for a protocol artifact or "
                "`mise run l1-comment` for ordinary discussion."
            )
    return None


_BULK_COMMENTS_URL_RE = re.compile(r"/issues/\d+/comments(?:$|[/?])")

_FETCH_LANE1_CONTEXT_HINT = (
    "reads Lane 2's comment bodies too -- there is no server-side filter by "
    "author/role, and every observed contamination (four separate Lane 3 "
    "sessions on one issue) came from exactly this class of command. "
    "Use `python3 ~/harmonic-forge/tools/gh/fetch_lane1_context.py --repo "
    "OWNER/REPO --issue N` instead, or fetch one already-known comment ID "
    "directly via `gh api repos/OWNER/REPO/issues/comments/<id>` "
    "(harmonic-forge#253/harmonic-forge#258)."
)


def bulk_comment_read_denial(command: str, cwd: Path, is_lane3: bool) -> str | None:
    """harmonic-forge#258: four separate Lane 3 contamination incidents on
    one issue, each via a different bulk-comment-read command
    (`gh issue view --comments`, `gh api .../comments --paginate`, a
    truncated-preview variant of the same, and a second `gh api
    .../comments` reached for mid-session to check a gate precondition
    after the first fetch had already gone through
    `fetch_lane1_context.py` correctly). Prose telling Lane 3 to use the
    filtering script did not hold across four attempts; this makes the
    unsafe commands themselves unavailable instead. Unconditional for the
    whole Lane 3 session, not just pre-spec -- Lane 3 has no legitimate
    need for a bulk comment read at any point; a single already-known
    comment ID (`gh api repos/OWNER/REPO/issues/comments/<id>`, a
    different, non-bulk REST endpoint) remains available."""
    if not is_lane3:
        return None
    effective_cwd = cwd
    for segment in _canonical.command_segments(command):
        if len(segment) == 2 and segment[0] == "cd":
            target = Path(segment[1]).expanduser()
            effective_cwd = target if target.is_absolute() else effective_cwd / target
            continue
        if not segment or segment[0] != "gh":
            continue
        if "issue" in segment and "view" in segment and "--comments" in segment:
            return f"`gh issue view --comments` {_FETCH_LANE1_CONTEXT_HINT}"
        if segment[1:2] == ["api"]:
            for arg in segment[2:]:
                if _BULK_COMMENTS_URL_RE.search(arg):
                    return f"a bulk `gh api .../issues/<N>/comments` listing {_FETCH_LANE1_CONTEXT_HINT}"
    return None


def cloud_cli_denial(command: str, is_lane3: bool) -> str | None:
    """Deny non-read-only `kubectl`/`doctl` during a Lane 3 gate,
    delegating to the shared policy both CLIs use.

    Its own try/except rather than relying on any other call in this file:
    NC1 requires this check to fail CLOSED, and `main()`'s other
    segment-aware helpers do not guard `command_segments()` at all -- a
    crash there would propagate before this check ever ran, so borrowing
    their (absent) protection would be borrowing nothing."""
    if not is_lane3:
        return None
    try:
        return _cloud_policy.denial_reason(command)
    except (AttributeError, TypeError, ValueError):
        return _cloud_policy._MALFORMED_DENIAL


def ae_sweep_self_post_denial(command: str, cwd: Path) -> str | None:
    """harmonic-forge#407 Q1: Codex-side wiring for the Lane 3 AE/
    gate-readiness-sweep self-authorization guard. Delegates to the
    canonical hook's own `decision()` -- it already does the LANE=="3"
    check and every transport/body-extraction case; this only translates
    its Claude-Code-shaped return into this file's plain reason string."""
    result = _ae_sweep_hook.decision(command, cwd)
    return result.get("hookSpecificOutput", {}).get("permissionDecisionReason")


def blocked_reason(args: list[str], is_lane3: bool) -> str | None:
    if not args:
        return None
    program = args[0]
    if program == "sudo":
        return "sudo is not allowed"
    if program in {"bash", "sh", "zsh"} and any(arg in {"-c", "--command"} for arg in args):
        return "shell -c indirection is not allowed"
    if program in {"python", "python3"} and any(
        argument.startswith("-") and "c" in argument for argument in args[1:4]
    ):
        return "python -c indirection is not allowed"
    if program in {"npm", "pip", "pip3"} and any(
        argument in {"install", "uninstall", "ci"} for argument in args[1:]
    ):
        return f"{program} install/uninstall/ci is not allowed"
    if program == "git" and is_lane3:
        subcommand = args[1] if len(args) > 1 else ""
        if subcommand in {"commit", "add", "push", "merge", "rebase"}:
            return f"git {subcommand} is not allowed"
        # `worktree add`/`checkout`/`switch` moved to `git_mutation_denial`
        # -- this bare args[0]/args[1] form cannot see a
        # compound command or a `git -C <path>`/wrapper prefix, which five
        # independent pre-close reviews confirmed bypasses it in practice.
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return allow()
    if payload.get("tool_name") != "Bash":
        return allow()
    command = payload.get("tool_input", {}).get("command", "")
    cwd = Path(payload.get("cwd") or Path.cwd())
    is_lane3 = is_lane3_session(cwd)
    reason = blocked_reason(normalize(command), is_lane3)
    if reason is None:
        reason = git_mutation_denial(command, cwd, is_lane3)
    if reason is None:
        reason = gate_task_denial(command, cwd)
    if reason is None:
        reason = gate_worktree_location_denial(command, cwd, is_lane3)
    if reason is None:
        reason = direct_post_denial(command, cwd)
    if reason is None:
        reason = bulk_comment_read_denial(command, cwd, is_lane3)
    if reason is None:
        reason = cloud_cli_denial(command, is_lane3)
    if reason is None:
        reason = ae_sweep_self_post_denial(command, cwd)
    return deny(reason) if reason else allow()


if __name__ == "__main__":
    raise SystemExit(main())
