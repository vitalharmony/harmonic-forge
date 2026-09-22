#!/usr/bin/env python3
"""Refuse a Lane 3 task when ANOTHER live session's gate owns this worktree.

`gate-checkout` already refuses when a live process has its cwd inside the
worktree (`check_worktree_busy.py`), which misses the case that actually
caused the incident this exists for: **a gate sitting idle between test runs
has no process with cwd inside the worktree at all**, so a second session's
checkout yanks the branch out from under it with no lock and no error.

## Why this is not simply "refuse if the marker is fresh"

`lane3-begin` writes `LANE3_ACTIVE` into the same worktree that then runs
`gate-checkout`, so a fresh marker is the NORMAL state at that moment.
Refusing on freshness alone deadlocks every gate -- and that is not
hypothetical: an earlier version did exactly this and "deadlocked every
subsequent gate".

The check therefore has to separate *my own* gate's marker from *someone
else's*, which needs an owner in the marker rather than an empty `touch`.

## What identifies an owner, and why it is an ancestor PID

Measured on one machine, 2026-09-05: the shell running a `mise` task is a
fresh process on every invocation (`$$` differed between two consecutive
calls in one session), so neither `$$` nor `$PPID` survives from
`lane3-begin` to `gate-checkout`. What does survive is the **session-level
ancestor** -- the agent process both tasks descend from. The owner is the
nearest ancestor that is not a shell or `mise`. A second session in the same
worktree resolves to a *different* one, which is exactly the pair this has to
tell apart. Recording the whole chain would be wrong -- two sessions share
their upper ancestors (the daemon, `systemd`), so a chain-overlap test would
call every session "mine".

## The three outcomes

- recorded owner is in **my** ancestry  -> my own gate, allow
- recorded owner is alive but not mine  -> another live gate, REFUSE
- recorded owner is dead                -> ask the repo's LEASE adapter

A marker with no recorded owner allows with a warning rather than refusing.

## What is platform and what is the repo's (harmonic-forge#721)

The ancestry walk, the marker format and the three outcomes above are
protocol: identical in every repo. **Whether a dead owner's gate is still
running** is not -- it depends on that repo's own lease, receipt and process
model -- so this asks the `lease` adapter declared in the repo's
`.claude/gate-adapter.json` (ADR-008 Exception 3). With no adapter declared,
the lease question is reported as BLOCKED and the guard allows, because its
own anti-deadlock rule above outranks it.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))

import adapter  # noqa: E402

#: Matches `block_lane1_status_claims.LANE3_MARKER_MAX_AGE_SECONDS` (12h).
#: An older marker is not evidence of anything — `lane3-end` clears it, and
#: a leftover one outlives the session that wrote it.
MARKER_MAX_AGE_SECONDS = 12 * 3600

#: Process names that are plumbing between the session and the task, never
#: the session itself.
#: `git` joined this set when the audit landed. `session_owner()` is now also called
#: from `.githooks/post-checkout`, whose ancestry is
#: python3 <- git <- bash <- ... <- session. Without `git` here the walk stops
#: at the git process, which is per-invocation, so two consecutive checkouts
#: recorded two different "sessions" and the audit answered nothing. It is
#: safe for the original marker path because `git` never appears in a
#: `lane3-begin` / `gate-checkout` ancestry — those run under mise and bash.
_TRANSPARENT = ("bash", "sh", "dash", "zsh", "mise", "python3", "python", "git")


def parse_stat(raw: str) -> tuple[str, int] | None:
    """`(comm, ppid)` from one `/proc/<pid>/stat` line.

    Split out from the file read so the parsing is testable with a
    hand-built line — the hazard here cannot be reproduced from the real
    `/proc`, because it only appears for a process whose name contains a
    parenthesis or a space.

    `comm` is parenthesised and MAY contain both, so the field ends at the
    LAST ')' rather than the first, and the fields after it are split on
    whitespace rather than the whole line being split. Splitting the line
    naively is the classic way to misread this file, and the consequence is
    a garbage ppid — an ancestry walk that silently goes somewhere else.
    """
    close = raw.rfind(")")
    open_paren = raw.find("(")
    if close == -1 or open_paren == -1 or close < open_paren:
        return None
    comm = raw[open_paren + 1:close]
    rest = raw[close + 1:].split()
    if len(rest) < 2:
        return None
    try:
        return comm, int(rest[1])
    except ValueError:
        return None


def _stat(pid: int) -> tuple[str, int] | None:
    """`(comm, ppid)` for a live pid, or None. `/proc` only — no `ps` fork."""
    try:
        return parse_stat(Path(f"/proc/{pid}/stat").read_text())
    except OSError:
        return None


def ancestry(pid: int | None = None, limit: int = 32) -> list[int]:
    """This process's ancestor pids, nearest first, stopping at init."""
    chain: list[int] = []
    current = os.getpid() if pid is None else pid
    for _ in range(limit):
        info = _stat(current)
        if info is None:
            break
        chain.append(current)
        current = info[1]
        if current <= 1:
            break
    return chain


def session_owner(pid: int | None = None) -> int | None:
    """The nearest ancestor that is not shell/mise plumbing.

    That is the process a session actually is, and it is what stays alive
    between `lane3-begin` and `gate-checkout` while every shell between them
    is replaced.
    """
    for candidate in ancestry(pid):
        info = _stat(candidate)
        if info is None:
            continue
        if info[0] not in _TRANSPARENT:
            return candidate
    return None


def marker_owner(marker: Path) -> int | None:
    """The pid `lane3-begin` recorded, or None for an ownerless marker."""
    try:
        text = marker.read_text().strip()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("owner_pid="):
            try:
                return int(line.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None



def _worktree_of(marker: Path) -> Path | None:
    """The worktree a LANE3_ACTIVE marker belongs to, or None.

    `lane3-begin` writes it to `$(git rev-parse --absolute-git-dir)`, which is
    `<worktree>/.git` in a normal clone and `<main>/.git/worktrees/<name>` in a
    linked worktree. Resolved structurally rather than by shelling out: `git -C
    <gitdir> rev-parse --show-toplevel` FAILS from inside a git dir ("must be
    run in a work tree"), which silently returned None for every marker — and
    None means "cannot prove the lease is ours", i.e. allow. So the guard was
    correct-by-accident and would have stopped refusing the moment that changed.
    No subprocess also means no fork in front of every gate-checkout.
    """
    gitdir = marker.parent

    # A LINKED worktree's git dir is `<main>/.git/worktrees/<name>/`, and it
    # carries a `gitdir` file pointing at `<worktree>/.git`. That is the only
    # thing tying it back to its own working tree.
    pointer = gitdir / "gitdir"
    if pointer.is_file():
        try:
            return Path(pointer.read_text(encoding="utf-8").strip()).parent
        except OSError:
            return None

    # A normal checkout's git dir is `<worktree>/.git`.
    if gitdir.name == ".git":
        return gitdir.parent

    return None


def refusal(marker: Path, now: float | None = None,
            pid: int | None = None) -> str | None:
    """The refusal message, or None to allow."""
    try:
        age = (time.time() if now is None else now) - marker.stat().st_mtime
    except OSError:
        return None  # no marker at all: nothing is claiming this worktree
    if age > MARKER_MAX_AGE_SECONDS:
        return None  # stale by time; `lane3-end` never ran, but nobody is waiting

    owner = marker_owner(marker)
    if owner is None:
        print(f"warning: {marker} has no recorded owner; allowing. A marker "
              "written before owner stamping cannot be attributed to a session.",
              file=sys.stderr)
        return None

    mine = ancestry(pid)
    if owner in mine:
        return None  # my own gate

    if _stat(owner) is None:
        # NARROWED, not removed.
        #
        # "Owner dead therefore stale" is what let a fresh session stand the
        # guard down: exit a `lane3` session mid-gate, start another, and the
        # recorded pid reads dead while the gate it belonged to is, in every
        # sense that matters, still in progress — its scheduler lease is still
        # held, the four background loops are still disabled for it, and its
        # worktree is still parked on the commit under test.
        #
        # The anti-deadlock property is load-bearing and stays: a GENUINELY
        # abandoned gate must still release the worktree, because a previous
        # change that refused here "deadlocked every subsequent gate"
        # (`lane3-begin`'s own description). So the test is no longer "is the
        # pid alive" — which cannot tell abandoned from handed-over — but "is
        # this gate still holding the resource a live gate holds".
        #
        # WHICH witness proves "still running" is the consuming repo's own
        # answer -- its lease, its receipt, its process model -- so the
        # platform asks the declared adapter and owns only the question
        # (ADR-008 Exception 3).
        # Which worktree does this marker belong to? If that cannot be
        # established, the lease cannot be shown to be about THIS gate, and an
        # unproven claim must not produce a refusal — the whole hazard here is
        # inventing a new deadlock, so every ambiguity resolves toward allow.
        owning_worktree = _worktree_of(marker)
        if owning_worktree is None:
            return None  # cannot prove the lease is ours: allow
        result = adapter.call(
            "lease", "check_owner", cwd=owning_worktree,
            issue=None, target_sha=None, report_only=True, worktree=str(owning_worktree),
        )
        if result["status"] == adapter.BLOCKED:
            # ADR-008 AC4: an unavailable adapter step is reported explicitly and
            # never silently no-op'd. It is reported rather than REFUSED because
            # this guard's own rule (above) is that every ambiguity resolves
            # toward allow -- refusing here on a repo that declares no lease
            # would deadlock every gate in it, which is the exact failure the
            # anti-deadlock property exists to prevent. Loud, recorded, allowed.
            for line in result["evidence"]:
                print(f"BLOCKED: lease check unavailable, allowing: {line}", file=sys.stderr)
            return None
        if result["status"] != adapter.FAIL:
            return None  # no lease held: genuinely abandoned, release it
        holder = "; ".join(result["evidence"]) or "another session"
        return (
            f"refusing: the session that claimed this worktree (pid {owner}) is "
            f"gone, but its gate lease is still held by {holder}. That is "
            "a gate handed over mid-run, not an abandoned one -- the background "
            "loops are still disabled for it and the worktree is still parked "
            "on the commit under test. Release that lease, then retry."
        )

    return (
        f"refusing: this worktree is held by another live Lane 3 session "
        f"(pid {owner}, marker {marker}). That session may be idle between "
        "test runs — which is exactly the case a live-process check misses "
        ". Wait for that session to end its gate, or clear the "
        "marker by hand if you know that session is gone."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("marker", type=Path, nargs="?",
                        help="path to the LANE3_ACTIVE marker")
    parser.add_argument("--print-owner", action="store_true",
                        help="print this session's owner pid and exit — how "
                             "`lane3-begin` stamps the marker, so the writer "
                             "and the reader resolve ownership through the "
                             "same function rather than two implementations")
    args = parser.parse_args(argv)
    if args.print_owner:
        print(session_owner() or 0)
        return 0
    if args.marker is None:
        parser.error("a marker path is required unless --print-owner is given")
    message = refusal(args.marker)

    # A guard that FIRED is exactly as interesting after the
    # fact as one that did not, and a refusal never reaches git, so the reflog
    # will hold no trace of it. This is the only place it can be recorded.
    # Best-effort by construction: `record()` swallows everything, because a
    # logging failure must not be able to refuse (or permit) a gate.
    try:
        import lane3_audit  # noqa: PLC0415

        lane3_audit.record(
            "marker-check",
            "refused" if message else "allowed",
            worktree=args.marker.parent,
            detail=(message.split("\n")[0][:200] if message else None),
        )
    except Exception:
        pass

    if message is None:
        return 0
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
