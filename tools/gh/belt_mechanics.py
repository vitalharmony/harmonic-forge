#!/usr/bin/env python3
"""Shared mechanics for the belt-and-suspenders protocol (harmonic-forge#518).

WHY THIS IS A MODULE AND NOT PROSE
-------------------------------------
AC11 requires **one** dedup implementation with per-lane parameters. A mechanic
described in a skill's markdown gets re-derived by each role at read time —
three implementations wearing one description, which is precisely the defect
this issue exists to eliminate. In a module it is one implementation by
construction.

Four of the ACs (2, 10, 16, 17) say "a test asserts this." Prose cannot be
asserted. Everything a test needs to reach lives here.

THE DEDUP MECHANIC, AND WHY IT IS THREE THINGS
-------------------------------------------------
Each lane independently evolved a different answer to "how does the belt avoid
missing an event between ticks," and two of the three lost real work in a single
session:

* Lane 1's bare 5-minute window had no dedup and no overlap. It lost hrse#1725's
  test spec, which sat posted and approvable while the monitor stayed silent.
* Lane 3's bare watermark advanced unconditionally. It lost four issues on two
  separate occasions, both found only because the operator asked directly.
* Lane 2's 90-minute lookback plus primed seen-set had no observed failure.

The operator's ruling (AC11) is to compose all three rather than pick one, and
each covers precisely the others' failure mode:

    watermark  sets the floor, and advances ONLY when that repo's own call
               succeeded — so a failed call cannot skip a window
    overlap    covers the seam a watermark structurally cannot: a slow cycle,
               a transient error, clock skew
    seen-set   makes the redundancy free by suppressing what was already seen

WATERMARKS SHARD WITH REMOTE STATE; LOCKS FOLLOW THE SESSION
---------------------------------------------------------------
Settled thread decision. `issues/comments?since=` is a per-repo call, so its
failures are per-repo and the watermark is per-repo. The lock protects the
session's single worktree and its one thread of attention, so it is per-session
— a per-account lock would let a monitor-fired turn and a loop-fired turn run
concurrently against the same HEAD, each believing it held exclusive access.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = [
    "GhAsError",
    "IdentityMismatch",
    "CallCounter",
    "gh_as",
    "assert_identity",
    "list_accounts",
    "list_repos",
    "Watermarks",
    "SeenSet",
    "TickLog",
    "session_lock",
    "query_since",
]

_ISO = "%Y-%m-%dT%H:%M:%SZ"

#: `gh` subcommands that resolve through GraphQL. R-0019: a one-off GraphQL call
#: is a rounding error; the same call on a five-minute timer is a quota leak
#: that surfaces in another lane's session as a confusing failure. The quota is
#: 5,000/hour, complexity-priced, and shared across every concurrent lane.
#:
#: `api` is REST unless the path is `graphql`, which `_is_graphql` handles.
GRAPHQL_SUBCOMMANDS = frozenset({
    "search", "issue", "pr", "project", "repo", "release", "run", "workflow",
})


class GhAsError(RuntimeError):
    """A `gh-as` invocation failed. Never swallowed into an empty result."""


class IdentityMismatch(GhAsError):
    """The account slot authenticates as someone else.

    AC4's whole point. An empty result and a wrong-credential result are
    indistinguishable to a caller, and silence is the one signal this protocol
    cannot interpret — so this raises rather than returning nothing.
    """


def _is_graphql(argv: list[str]) -> bool:
    """True when this `gh` invocation will spend GraphQL quota."""
    if not argv:
        return False
    sub = argv[0]
    if sub == "api":
        # `gh api graphql` is the explicit form; every other path is REST.
        return any(a == "graphql" or a.endswith("/graphql") for a in argv[1:])
    return sub in GRAPHQL_SUBCOMMANDS


@dataclass
class CallCounter:
    """AC16, enforced continuously rather than by a one-time grep.

    A grep over the skill catches a literal `gh search` and misses a GraphQL
    call reached through a helper. Counting at the single wrapper every call
    passes through cannot be bypassed that way, and AC17 surfaces the count per
    tick, where a non-zero `calls_graphql` from a scheduled path is a defect
    rather than a statistic.
    """

    calls_rest: int = 0
    calls_graphql: int = 0

    def record(self, argv: list[str]) -> None:
        if _is_graphql(argv):
            self.calls_graphql += 1
        else:
            self.calls_rest += 1


def gh_as(
    account: str,
    argv: list[str],
    *,
    counter: Optional[CallCounter] = None,
    check: bool = True,
    timeout: int = 60,
) -> str:
    """Run one `gh` command scoped to `account`. The only call path.

    Scoping is via `gh-as`, never `gh auth switch` (R-0014) — `gh auth switch`
    mutates global state for every other session and agent on the machine, and
    `gh-as`'s own header states the guarantee this relies on: "There is no
    switch to undo: the scoping lives and dies with the process."
    """
    if counter is not None:
        counter.record(argv)
    proc = subprocess.run(
        ["gh-as", account, "gh", *argv],
        capture_output=True, text=True, timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GhAsError(
            f"gh-as {account} gh {' '.join(argv)} -> exit {proc.returncode}: "
            f"{proc.stderr.strip()[:400]}"
        )
    return proc.stdout


def list_accounts() -> dict[str, str]:
    """`{account: authenticated_login}` from `gh-as --list`.

    Runtime discovery (R-0122/AC3): adding an account is `gh-as --init` and
    nothing else — no edit to this module or to the skill.
    """
    proc = subprocess.run(["gh-as", "--list"], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise GhAsError(f"gh-as --list failed: {proc.stderr.strip()[:400]}")
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = " ".join(parts[1:])
    return out


def assert_identity(account: str, accounts: Optional[dict[str, str]] = None) -> None:
    """Refuse loudly when a slot authenticates as someone else (AC4, AC5).

    R-0015 makes this mandatory: a script needing a specific account verifies
    its identity and refuses if it is wrong. The failure being prevented is
    silent — a `vitalharmony`-scoped session polling a `harmonicarchitect` repo
    404s or returns empty, and an empty monitor result reads as "no new work."

    Live at the time of writing, `harmonicarchitect` authenticates as
    `vitalharmony`; that is a real fixture, not a hypothetical.
    """
    accounts = accounts if accounts is not None else list_accounts()
    who = accounts.get(account)
    if who is None:
        raise IdentityMismatch(
            f"account {account!r} is not configured (gh-as --init {account}). "
            "Refusing rather than returning an empty result."
        )
    if who == "(not authenticated)":
        raise IdentityMismatch(
            f"account {account!r} is not authenticated. Refusing rather than "
            "returning an empty result, which would read as 'no new work'."
        )
    if who != account:
        raise IdentityMismatch(
            f"account slot {account!r} authenticates as {who!r}. Polling it "
            f"would silently scan {who}'s repos and report nothing for "
            f"{account}. Refusing — re-authenticate with "
            f"`gh-as --init {account}` (operator action)."
        )


def list_repos(account: str, *, counter: Optional[CallCounter] = None) -> list[str]:
    """Non-archived repos for `account`, derived at runtime (AC3).

    The archived exclusion is not cosmetic: `conscious-architect-core` is
    archived and absent from every hardcoded list today **by luck**, not by
    filtering.
    """
    raw = gh_as(
        account,
        ["repo", "list", account, "--limit", "100", "--json", "name,isArchived",
         "--jq", ".[] | select(.isArchived | not) | .name"],
        counter=counter,
    )
    return [line.strip() for line in raw.splitlines() if line.strip()]


class Watermarks:
    """Per-repo, advancing only on that repo's own success (settled decision).

    One watermark per account would let a single repo's failure — a rate limit,
    a 5xx, a network blip — advance a shared marker past a window that repo
    never read. That window is then lost permanently and silently, which is the
    exact gap the watermark was introduced to close, reintroduced one level up.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, account: str, repo: str) -> Path:
        return self.root / f"{account}__{repo}.watermark"

    def get(self, account: str, repo: str) -> Optional[datetime]:
        path = self._path(account, repo)
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, _ISO).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def advance(self, account: str, repo: str, when: datetime) -> None:
        """Call ONLY after that repo's own call succeeded."""
        self._path(account, repo).write_text(
            when.astimezone(timezone.utc).strftime(_ISO), encoding="utf-8"
        )


def query_since(
    watermark: Optional[datetime], overlap_minutes: int, now: Optional[datetime] = None
) -> datetime:
    """`min(watermark, now - K)` — the floor, with the seam covered.

    Never later than the watermark, so a stalled repo is re-read rather than
    skipped; never earlier than necessary, so the response stays small.
    """
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(minutes=overlap_minutes)
    if watermark is None:
        return floor
    return min(watermark, floor)


class SeenSet:
    """Dedup state that can answer "was I actually told about this?" (AC12).

    Written `id<TAB>emitted|primed`, not bare ids. Priming and live polling both
    append, so a bare-id file cannot distinguish "reported to you" from
    "suppressed at arm" after the fact — and a monitor that cannot answer that
    cannot be debugged when it stays silent, which is the failure mode this
    whole protocol keeps hitting.
    """

    EMITTED = "emitted"
    PRIMED = "primed"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, str] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t")
                self._state[parts[0]] = parts[1] if len(parts) > 1 else self.EMITTED

    def __contains__(self, comment_id: str) -> bool:
        return str(comment_id) in self._state

    def status(self, comment_id: str) -> Optional[str]:
        return self._state.get(str(comment_id))

    def add(self, comment_id: str, how: str) -> None:
        if how not in (self.EMITTED, self.PRIMED):
            raise ValueError(f"how must be emitted|primed, got {how!r}")
        cid = str(comment_id)
        self._state[cid] = how
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"{cid}\t{how}\n")

    def prime(self, comment_ids: Iterable[str]) -> int:
        """Suppress history before the first poll, so arming does not replay it."""
        n = 0
        for cid in comment_ids:
            if str(cid) not in self._state:
                self.add(cid, self.PRIMED)
                n += 1
        return n


#: The element format for `matched`/`emitted`/`owed_found` in a tick record.
#: `ref` is `"<repo>#<number>"` — `"hrse#1725"`, `"harmonic-forge#518"` — and
#: the repo half is what the reader groups per-repo counts by.
#:
#: This convention was previously implicit: `belt_report.py` split on `"#"`
#: and nothing said so anywhere a writer would look, so a lane logging a bare
#: issue number would have produced per-repo counts of zero — indistinguishable
#: from a quiet repo, which is the exact "manufactures false confidence"
#: failure this telemetry exists to catch (harmonic-forge#519). Stated here,
#: validated by the reader, and a malformed ref is reported rather than dropped.
REF_FORMAT = "<repo>#<number>"


def _entry(ref: Any, posted_at: Optional[str] = None) -> dict[str, Any]:
    """Normalise one `matched`/`emitted`/`owed_found` element to its record shape.

    Accepts a bare ref string (legacy callers, and the shape a hand-written
    record may carry) or an already-built dict, and always returns
    `{"id": ..., "posted_at": ...}`. A missing timestamp is recorded as `None`
    rather than filled in with the tick's time — a fabricated `posted_at`
    would make detection-to-action read as zero, which is worse than absent.
    """
    if isinstance(ref, dict):
        out = {"id": str(ref.get("id", "")), "posted_at": ref.get("posted_at")}
        if posted_at is not None:
            out["posted_at"] = posted_at
        return out
    return {"id": str(ref), "posted_at": posted_at}


@dataclass
class TickLog:
    """One JSONL record per tick, written even on a quiet tick (AC17).

    A quiet tick that writes nothing is indistinguishable from a dead monitor,
    which is the core ambiguity. This does not conflict with AC8: that governs
    *chat* output, and a quiet tick still says nothing in chat.

    No API calls of its own — telemetry that costs quota reproduces the defect
    it exists to catch.
    """

    path: Path
    lane: str
    trigger: str
    counter: CallCounter = field(default_factory=CallCounter)
    repos_polled: list[dict[str, Any]] = field(default_factory=list)
    matched: list[Any] = field(default_factory=list)
    emitted: list[Any] = field(default_factory=list)
    suppressed_as_primed: list[str] = field(default_factory=list)
    owed_found: list[Any] = field(default_factory=list)
    lock: str = "acquired"
    actions_taken: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)

    def repo_result(self, account: str, repo: str, ok: bool, error: str = "") -> None:
        """17b: a repo polled and matching nothing is a different row from a
        repo never polled. Both are recorded so the reader can tell them apart."""
        row: dict[str, Any] = {"account": account, "repo": repo, "ok": ok}
        if error:
            row["error"] = error[:200]
        self.repos_polled.append(row)

    def record_match(self, ref: str, posted_at: Optional[str] = None) -> None:
        """Record a marker this tick matched, with the marker's OWN timestamp.

        `posted_at` is the comment's `created_at`, not the tick's `ts`
        (harmonic-forge#519). Without it the log can only yield
        detection-to-action; the interval between a marker being posted and a
        belt noticing it is exactly the outage the telemetry exists to expose,
        and a tick timestamp cannot see it.
        """
        self.matched.append(_entry(ref, posted_at))

    def record_emit(self, ref: str, posted_at: Optional[str] = None) -> None:
        """Record a marker this tick emitted. Same shape as `record_match`."""
        self.emitted.append(_entry(ref, posted_at))

    def record_owed(self, ref: str, posted_at: Optional[str] = None) -> None:
        """Record an unanswered item the own-output predicate found."""
        self.owed_found.append(_entry(ref, posted_at))

    def write(self) -> dict[str, Any]:
        record = {
            "ts": datetime.now(timezone.utc).strftime(_ISO),
            "lane": self.lane,
            "trigger": self.trigger,
            "duration_s": round(time.time() - self.started, 3),
            "repos_polled": self.repos_polled,
            "calls_rest": self.counter.calls_rest,
            "calls_graphql": self.counter.calls_graphql,
            # Normalised at write time, not at append time: a caller that does
            # `log.matched.append("hrse#1725")` still produces a well-formed
            # record rather than a silently different one. Under-recording
            # (`posted_at: null`) is visible to the reader; a bare string mixed
            # in among dicts is the kind of thing that reads as zero.
            "matched": [_entry(e) for e in self.matched],
            "emitted": [_entry(e) for e in self.emitted],
            "suppressed_as_primed": self.suppressed_as_primed,
            "owed_found": [_entry(e) for e in self.owed_found],
            "lock": self.lock,
            "actions_taken": self.actions_taken,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        return record


class session_lock:
    """POSIX-atomic `mkdir` mutex, one per session (settled decision).

    Lane 3's belt and loop both run `gate-checkout` in one shared worktree, so
    two ticks landing together corrupt a checkout. A lane whose belt writes
    nothing takes no lock at all — this is opt-in per role, not universal.

    `mkdir` is used rather than a lockfile because it is atomic on POSIX: the
    directory either exists or is created, with no check-then-act window.
    """

    def __init__(self, path: Path, stale_after_minutes: int = 10) -> None:
        self.path = Path(path)
        self.stale_after = timedelta(minutes=stale_after_minutes)
        self.state = "acquired"

    def _reclaim_if_stale(self) -> None:
        try:
            age = datetime.now(timezone.utc) - datetime.fromtimestamp(
                self.path.stat().st_mtime, tz=timezone.utc
            )
        except FileNotFoundError:
            return
        if age > self.stale_after:
            try:
                self.path.rmdir()
                self.state = "reclaimed"
            except OSError:
                pass

    def __enter__(self) -> "session_lock":
        try:
            self.path.mkdir(parents=True)
            return self
        except FileExistsError:
            pass
        self._reclaim_if_stale()
        try:
            self.path.mkdir(parents=True)
        except FileExistsError:
            self.state = "held"
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.state == "held":
            return
        try:
            self.path.rmdir()
        except OSError:
            pass

    @property
    def acquired(self) -> bool:
        return self.state != "held"
