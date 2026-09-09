#!/usr/bin/env python3
"""BATCH-authorization: the sole gate for `gh issue close`/`gh pr merge`
(harmonic-forge#336, reforged after Lane 3's live gate FAIL; multi-target
state shape added in harmonic-forge#356 gap 2).

## Why this reforge exists

The original design layered `batch_gate.py`'s `allow` decision *under* a
static `permissions.ask` rule (`Bash(gh issue close *)` / `Bash(gh pr merge
*)` in `~/.claude/settings.json`), on the assumption a `PreToolUse` hook's
`allow` could suppress that static rule. It cannot: per Claude Code's own
documented permission precedence (`deny > ask > allow`, no hook exception --
https://code.claude.com/docs/en/permissions.md), the static `ask` rule
always wins regardless of what any hook returns. Lane 3's live gate proved
this exactly: `decide()`'s predecessor matched and flipped the state file to
`consumed: true`, and the operator was still prompted. 56 passing unit tests
were all correct and all beside the point -- they tested state-file logic in
isolation, which was never the broken part.

## The only supported shape

`permissions.ask` no longer carries `gh issue close *` / `gh pr merge *` at
all. `decide()` (via `batch_gate.py`) is now the **sole, full-time decision**
for those two command classes, on every invocation -- not a supplementary
bypass layer for BATCH-covered commands only:

- Live, matching authorization -> `"allow"`.
- No live, matching authorization -> `"ask"`, explicitly. Silence would now
  mean "nothing objects," which the harness's normal fallback resolves
  toward `allow` -- the opposite of the previous safe default (always
  prompt unless a static rule explicitly permitted it). Preserving that
  safe default is now this module's own responsibility, not inherited from
  a static rule sitting above it.
- Not one of these two command classes at all -> `None` (silent; some other
  hook or rule's decision, unaffected).

**Fail-direction is inverted from every other hook in this directory.**
`block_irreversible_ops.py` fails *open* on anything unparseable ("a hook
that blocks whatever it cannot parse gets unregistered") -- correct for a
hook riding as a backstop under a static `ask` rule that still fires
regardless. `decide()` fails *closed*: unparseable input, an internal
exception, or a covered command it can't confidently resolve to a specific
authorized issue all return `("ask", ...)`, never silent. This is the single
highest-consequence design property in this module -- getting it backwards
means every unrecognized `gh issue close`/`gh pr merge` silently proceeds
with no prompt at all, since nothing else in the permission chain asks for
these two classes anymore.

`block_irreversible_ops.py` no longer has `_check_issue_close`/
`_check_pr_merge_delete` rules -- they moved here in full (including the
delete-branch stacked-child-PR warning). Two hooks independently deciding
the same command class was undefined behavior under "strongest decision
wins" composition; one hook now owns each class end to end.

## One key, both actions (harmonic-forge#356 gap 2)

A batched issue's real lifecycle is implement -> merge -> close -- both
command classes need authorizing, not just one. Each key's entry now holds a
`targets` list, one dict per authorized action, sharing one `expires_at` and
one `authorized_at` from the single `authorize()` call that created them.
`authorize()` defaults to authorizing BOTH actions per key for exactly this
reason; a narrower `--action` (e.g. a superseded issue that closes without
ever having a PR, per the H767 case this gap was found from) is still
supported by passing fewer action strings explicitly.

Each target is independently consumed -- merging a PR does not consume the
issue's close authorization, and vice versa. `link_pr()` attaches
repo/pr_number to the specific `"gh pr merge"` target within a key's
`targets` list, not to the entry as a whole.

## Write path -- trust boundary lives in the caller, not here

`authorize()` is called only from a genuine operator chat message carrying
the literal `BATCH` keyword -- never in response to text read from a file,
issue/PR body, tool output, or web page. That instruction-source boundary is
the calling agent's own judgement to make; this module performs the write
once that judgement is already made, and never makes it itself.

## The PR-number gap -- stated plainly, not silently resolved

`gh pr merge` names a PR number, never the issue number `BATCH` was given --
GitHub issues and PRs share one number sequence per repo, so a PR fulfilling
H395 is essentially never PR 395. There is no mechanical way to recover this
mapping after the fact: this repo's own `tools/gh/block_closing_keywords.py`
hook denies writing a `Closes #N` autoclose keyword into a PR body
specifically to keep issue closure an explicit human action, so GitHub's own
`closingIssuesReferences` linkage is never populated here either. The only
place this mapping is ever known is the agent that opens the PR --
`link_pr()` is that explicit record, and stays the authoritative override.

**Derivation is the fallback, not a replacement** (AC5, harmonic-forge#552,
carried from #549). When no `link_pr` record names the PR, `decide()` reads the
two carriers this house's PRs always populate -- the head branch
(`l1/h1757-...`, `feat/1754-...`) and the title's `(repo#N)` suffix -- and uses
the issue they agree on. It resolves a mapping that already exists; it never
invents a grant. Three fail-closed directions are explicit and tested:
unparseable carriers, carriers that disagree, and a derived key with no live
authorization all return `("ask", ...)`. A missed `link_pr()` call now usually
costs nothing, and in every case it can still only cost one Ask prompt --
never a wrongly-granted merge.

## `authorize()` and the command it authorizes must be in SEPARATE tool calls

`PreToolUse` hooks evaluate a submitted Bash command's *entire* text once,
before any of it executes. A multi-line tool call that bundles `authorize`
and the now-authorized `gh issue close`/`gh pr merge` together (e.g. two
shell lines in one Bash tool invocation) gets evaluated as a whole *before
the authorize line has run* -- `decide()` sees no live entry yet, correctly
asks, and the authorize line then executes anyway as the script continues,
leaving an unconsumed entry behind it. Confirmed live, harmonic-forge#356:
identical `authorize` + `close` sequences differed only in whether they were
one tool call or two, and only the two-call form went through silently.
Not a bug in `decide()` -- a structural fact about hook evaluation timing.
Always issue `authorize` (and `link_pr()`) as their own tool call, with the
authorized command as a separate, later one.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

STATE_PATH = Path.home() / ".claude" / "state" / "batch-authorized.json"
#: BATCH exists for UNATTENDED batches, and the default was 2.0 -- a
#: supervised-run TTL on a feature whose whole reason for existing is running
#: while nobody is watching. The operator stepped away "a few hours" and came
#: back to a stalled lane; even a correctly-issued authorization would have
#: expired mid-run and produced the identical symptom (harmonic-forge#502).
#:
#: 12 hours: long enough that no plausible batch outlives it, short enough that
#: a forgotten grant does not sit live for days. It is not derived from batch
#: size on purpose -- a size-derived TTL would be a second thing to get wrong,
#: and the failure mode of "too short" is the one that actually bit.
DEFAULT_TTL_HOURS = 12.0
DEFAULT_ACTIONS = ("gh pr merge", "gh issue close")

#: AC1 disposition (harmonic-forge#567): a GRACE WINDOW past expiry, not the
#: house's usual count-based cap (`lane3_audit.py`'s `MAX_RECORDS`,
#: `gate_ci.py`'s `CARRIER_CACHE_MAX`). A count-based cap is denominated in
#: CALLS to authorize()/top_up() -- under a busy batch day a revoked or
#: expired entry could be evicted within hours of being marked, which is
#: exactly the "not an audit property" failure AC6 names. A time-based window
#: keeps the EXPIRED diagnostic (and a revoke's marker, AC4/AC6) readable for
#: a fixed, predictable period regardless of how many OTHER keys get
#: authorized in the meantime -- the two are decoupled on purpose.
#:
#: 7 days: comfortably spans the normal "what happened to that grant" review
#: window (the incident that filed this issue was noticed at 18 days of
#: totally unbounded growth -- a week is a small fraction of that and still
#: bounds the file), while ensuring the file no longer grows without limit.
PRUNE_GRACE_HOURS = 24.0 * 7

# harmonic-forge#369: the read -> live-entry-check -> consume/write sequence
# in decide()/authorize()/link_pr() was an unlocked read-modify-write --
# concurrent consumption could lose a flag and make a one-shot grant
# reusable. Sub-second and non-blocking by design: this hook must never
# wedge a lane over its own lock contention (the same fail-*closed* posture
# as the rest of this module -- a lock that can't be acquired promptly means
# `ask`, not a hang and not a silent skip of the guard).
_LOCK_TIMEOUT_SECONDS = 0.4
_LOCK_POLL_SECONDS = 0.02


class StateLockTimeout(Exception):
    """Raised when the state-file lock can't be acquired within the budget.
    Callers must treat this the same as any other decide()-time failure:
    fail toward `ask`, never toward a silent allow or a hang."""


@contextlib.contextmanager
def _locked_state(state_path: Path):
    """Exclusive advisory lock on `state_path`'s own `.lock` sibling, held
    only across the read -> check -> write sequence -- never across a
    subprocess call or any I/O beyond the state file itself (module
    docstring). Non-blocking with a short poll/timeout rather than a
    blocking `flock()`: a stuck holder must produce a fast `ask`, not a
    hung hook."""
    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise StateLockTimeout(
                        f"could not acquire lock on {lock_path} within "
                        f"{_LOCK_TIMEOUT_SECONDS}s"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

# vitalharmony/hrse -> "H", etc. -- rules/lane-shorthand.md is the canonical
# table; K/P point at other accounts entirely and are deliberately excluded
# here (credential isolation across engagements is a standing rule -- BATCH
# authorization never crosses an account boundary).
REPO_PREFIXES = {
    "vitalharmony/hrse": "H",
    "vitalharmony/harmonic-forge": "F",
    "vitalharmony/cymagraph-infra": "I",
    "vitalharmony/openclaw-projects": "O",
}

ISSUE_KEY = re.compile(r"^([A-Za-z])(\d+)$")
API_ISSUE_PATH = re.compile(r"repos/([^/\s]+/[^/\s]+)/issues/(\d+)")
API_MERGE_PATH = re.compile(r"repos/([^/\s]+/[^/\s]+)/pulls/(\d+)/merge")

ASK_ISSUE_CLOSE = (
    "Closing an issue. The protocol requires an explicit human close -- "
    "Lane 3's gate or the operator's instruction, never an agent's own "
    "judgement -- unless a live BATCH authorization covers this exact issue."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(state_path: Path | None = None) -> dict:
    """`state_path` defaults dynamically to the current `STATE_PATH` module
    global, resolved at call time rather than bound at def time -- a `Path
    = STATE_PATH` default would freeze the value the moment this module is
    first imported, so a test (or any caller) patching `batch_auth.STATE_PATH`
    afterward would silently have no effect."""
    if state_path is None:
        state_path = STATE_PATH
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(state: dict, state_path: Path | None = None) -> None:
    """Temp-file + atomic replace (harmonic-forge#369) -- a reader (this
    module's own `_load`, or an operator `cat`) can never observe a partial
    write, and a crash mid-write leaves the prior state intact rather than a
    truncated/corrupt file."""
    if state_path is None:
        state_path = STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(state_path.parent), prefix=state_path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp_name, state_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def issue_key(repo: str, number: str | int) -> str | None:
    """`vitalharmony/hrse`, 395 -> `H395`. None if the repo has no prefix."""
    prefix = REPO_PREFIXES.get(repo)
    return f"{prefix}{number}" if prefix else None


def _new_target(action: str) -> dict:
    return {"action": action, "consumed": False, "consumed_by": None, "repo": None, "pr_number": None}


def _prune(state: dict, now: datetime) -> dict:
    """Drop entries expired more than `PRUNE_GRACE_HOURS` ago (AC1).

    Called only from `authorize()`/`top_up()`, inside the `_locked_state` lock
    they already hold -- never from `_save()`, never as a separate sweep
    (AC2). A live entry is never a candidate: liveness is checked first and
    unconditionally, so AC3 holds regardless of how the grace window is
    tuned. An entry whose `expires_at` cannot be parsed is kept rather than
    dropped -- this function fails toward retention, never toward deletion,
    the same fail-closed posture as the rest of this module.
    """
    cutoff = now - timedelta(hours=PRUNE_GRACE_HOURS)
    kept: dict = {}
    for key, entry in state.items():
        if _entry_live(entry, now):
            kept[key] = entry
            continue
        try:
            expires = datetime.fromisoformat(entry["expires_at"])
            stale = expires < cutoff
        except (KeyError, TypeError, ValueError):
            # Unparseable, OR a timezone-naive value that parses but cannot
            # be compared against the (aware) cutoff -- both fail toward
            # retention, matching `_entry_live`'s posture just above.
            kept[key] = entry
            continue
        if not stale:
            kept[key] = entry
    return kept


def top_up(
    keys: list[str],
    actions: list[str] | tuple[str, ...] = DEFAULT_ACTIONS,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    state_path: Path | None = None,
) -> list[str]:
    """Extend a live grant instead of replacing it; authorize fresh keys.

    **`authorize()` REPLACES an entry outright**, resetting consumption and
    wiping every recorded `link_pr` mapping. That was safe while its only
    caller was a deliberate CLI invocation. It is not safe now that a chat
    message creates grants: a mid-batch "keep going on the BATCH F495, F497
    work" is encouragement, not a new grant, and replacing on it silently
    made an already-spent single-use CLOSE re-usable and sent every linked PR
    back to Ask on its next merge -- the prompt-storm this issue removes.

    So: a key with a LIVE entry has its expiry extended and its targets left
    exactly as they are. A key with no entry, or an expired one, is authorized
    normally. Returns the keys that were newly authorized, so the caller can
    say which is which rather than claiming success for both.
    """
    actual_path = STATE_PATH if state_path is None else state_path
    fresh: list[str] = []
    with _locked_state(actual_path):
        state = _load(state_path)
        now = _now()
        state = _prune(state, now)
        expires = (now + timedelta(hours=ttl_hours)).isoformat()
        for key in keys:
            key = key.upper()
            entry = state.get(key)
            if entry is not None and _entry_live(entry, now):
                entry["expires_at"] = expires
                continue
            fresh.append(key)
        _save(state, state_path)
    if fresh:
        authorize(fresh, actions=actions, ttl_hours=ttl_hours,
                  state_path=state_path)
    return fresh


def authorize(
    keys: list[str],
    actions: list[str] | tuple[str, ...] = DEFAULT_ACTIONS,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    state_path: Path | None = None,
) -> None:
    """Write one fresh entry per issue key, one target per action (default:
    both merge and close -- harmonic-forge#356 gap 2). See module docstring
    -- the caller is responsible for having verified this came from a
    genuine operator chat message, not fetched content.

    A repeat call for an already-authorized key REPLACES its entry outright
    (fresh targets, any prior consumption reset) -- a new BATCH grant is a
    new grant, not a merge with whatever was there before.
    """
    if isinstance(actions, str):
        # A bare string is technically iterable -- silently producing one
        # single-character garbage target per letter is far worse than a
        # loud, immediate TypeError. Caught live while fixing this exact
        # module's own test suite for harmonic-forge#356.
        raise TypeError(f"actions must be a list of strings, not a bare string: {actions!r}")
    if not actions:
        raise ValueError("authorize() requires at least one action")
    actual_path = STATE_PATH if state_path is None else state_path
    with _locked_state(actual_path):
        state = _load(state_path)
        now = _now()
        state = _prune(state, now)
        expires = now + timedelta(hours=ttl_hours)
        for raw_key in keys:
            key = raw_key.upper()
            if not ISSUE_KEY.match(key):
                raise ValueError(f"not a valid issue key: {raw_key!r}")
            state[key] = {
                "authorized_at": now.isoformat(),
                "expires_at": expires.isoformat(),
                "targets": [_new_target(action) for action in actions],
            }
        _save(state, state_path)


def link_pr(key: str, repo: str, pr_number: int, state_path: Path | None = None) -> None:
    """Record which PR fulfils a BATCH-authorized issue's merge target, once
    opened. See the module docstring's PR-number-gap section for why this
    call is the only place this mapping can ever be recorded."""
    actual_path = STATE_PATH if state_path is None else state_path
    with _locked_state(actual_path):
        state = _load(state_path)
        key = key.upper()
        entry = state.get(key)
        if entry is None:
            raise ValueError(f"no authorization entry for {key!r} -- authorize it first")
        merges = [t for t in entry.get("targets", [])
                  if "merge" in t.get("action", "").lower()]
        if not merges:
            raise ValueError(f"{key!r} was not authorized for a merge action -- authorize it with --action 'gh pr merge' first")

        # Already linked to THIS pr on a slot that can still be USED?
        # Idempotent, so a re-run is free.
        #
        # AC4 (harmonic-forge#552): keyed on (repo, pr_number), REGARDLESS of
        # `consumed`. At most one merge target per distinct pair, ever.
        #
        # The `not target.get("consumed")` clause that stood here was added by
        # harmonic-forge#549 to route around dead slots — slots marked consumed
        # by a merge that never ran. Now that consumption is truthful (it
        # happens in batch_consume.py, only after the action is confirmed to
        # have landed), `consumed` means the merge HAPPENED, and skipping such
        # a slot would make re-linking an already-merged PR allocate a
        # duplicate. #552 requires this clause removed in the same change that
        # fixes consumption — not before, not after.
        #
        # Allocation itself stays: #502 AC8's cross-repo reason is sound, and a
        # NEW (repo, pr_number) pair still allocates. What was wrong was the
        # trigger, not the mechanism — with untruthful consumption every dead
        # slot looked like "no slot available", which is how F544 reached four
        # targets for one PR. Two live targets for one pair is the tell.
        for target in merges:
            if (target.get("repo") == repo
                    and target.get("pr_number") == pr_number):
                return

        # An unlinked, unconsumed slot takes it.
        target = next((t for t in merges
                       if not t.get("consumed") and t.get("pr_number") is None), None)

        # harmonic-forge#502 AC8: otherwise ALLOCATE one. `authorize` grants a
        # single merge target per key, but a CROSS-REPO issue needs one per
        # repo -- harmonic-forge#497 needed two (the forge tool and the hrse
        # mise wiring), the first consumed the only slot, and the second merge
        # correctly fell closed to Ask. Of the five issues in that batch, two
        # were two-repo; it is the standard shape for a forge tool called from
        # hrse's mise.toml, so a one-merge grant is wrong for roughly half the
        # tooling backlog.
        #
        # Allocating here rather than at `authorize` time is deliberate: the
        # number of repos an issue touches is not knowable when the operator
        # types BATCH, and it IS knowable the moment a PR is opened. The close
        # target stays single-use -- a close is irreversible and happens once.
        if target is None:
            template = merges[0]
            target = {"action": template.get("action", "gh pr merge"),
                      "consumed": False, "consumed_by": None,
                      "pr_number": None, "repo": None}
            entry.setdefault("targets", []).append(target)

        target["repo"] = repo
        target["pr_number"] = pr_number
        _save(state, state_path)


def revoke(keys: list[str], state_path: Path | None = None) -> list[str]:
    """Stand down one or more keys without a hand-written file edit (AC4).

    Marks every unconsumed target `consumed: True`,
    `consumed_by: "revoked-<ISO timestamp>"` -- it never deletes the entry or
    any target, so the state never reads as though a merge or close actually
    happened. `decide()`'s existing "already CONSUMED" branch then fires on
    any later attempt against a revoked target, same as a real consumption.

    A no-op, not an error, on a key that does not exist (never creates one)
    or whose targets are all already consumed (AC5) -- standing down a batch
    that mostly landed is the normal case, not an exceptional one.

    Returns the keys that were actually changed.
    """
    actual_path = STATE_PATH if state_path is None else state_path
    changed: list[str] = []
    with _locked_state(actual_path):
        state = _load(state_path)
        now = _now()
        marker = f"revoked-{now.isoformat()}"
        for raw_key in keys:
            key = raw_key.upper()
            entry = state.get(key)
            if entry is None:
                continue
            did_change = False
            for target in entry.get("targets", []):
                if not target.get("consumed"):
                    target["consumed"] = True
                    target["consumed_by"] = marker
                    did_change = True
            if did_change:
                changed.append(key)
        if changed:
            _save(state, state_path)
    return changed


def _entry_live(entry: dict, now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(entry["expires_at"])
        return now < expires
    except (KeyError, TypeError, ValueError):
        # harmonic-forge#567 preclose finding: a timezone-NAIVE `expires_at`
        # (a hand-written entry, or one of the ten zero-target entries the
        # issue's own measurement found in the live file) parses fine but
        # raises TypeError on comparison against `now` (aware). That used to
        # escape uncaught from `_prune()` inside `authorize()`/`top_up()`'s
        # lock -- no other caller of this function ever reaches an
        # uncaught exception on this shape (`decide()` wraps it in its own
        # outer try/except; `batch_context.live_batch_keys` and
        # `block_batch_stop.py` each put the comparison inside their own
        # try). Moving the comparison inside this try makes `_entry_live`
        # itself safe for every caller, present and future, rather than
        # requiring each new one to remember to guard it separately.
        return False


def _repo_flag(tokens: list[str]) -> str | None:
    """Explicit `--repo`/`--repo=` only. REST paths embed the repo directly
    and are matched separately -- a bare CLI-form command with no --repo
    flag has no repo to resolve here, and deliberately does not fall back to
    guessing one from cwd or environment: an unresolved repo can't be
    matched to an authorization, so `decide()` asks."""
    for index, token in enumerate(tokens):
        if token == "--repo" and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("--repo="):
            return token.partition("=")[2]
    return None


def _method_is(tokens: list[str], want: str) -> bool:
    want = want.upper()
    for index, token in enumerate(tokens):
        upper = token.upper()
        if upper in (f"-X{want}", f"--METHOD={want}", f"-X={want}"):
            return True
        if token in ("-X", "--method") and index + 1 < len(tokens):
            if tokens[index + 1].upper() == want:
                return True
    return False


def _has_field(tokens: list[str], assignment: str) -> bool:
    return any(token.replace(" ", "") == assignment for token in tokens)


PROTECTED_GRAPHQL_MUTATIONS = re.compile(
    r"\b(?:closeIssue|mergePullRequest|updateIssue|updatePullRequest|"
    r"enablePullRequestAutoMerge|enqueuePullRequest|closePullRequest)\b"
)


def _protected_graphql(tokens: list[str]) -> bool:
    tokens = strip_invocation_prefix(tokens)
    if not tokens or Path(tokens[0]).name != "gh" or "api" not in tokens or "graphql" not in tokens:
        return False
    query = next((token.partition("=")[2] for token in tokens if token.startswith("query=")), None)
    return query is None or query.startswith("@") or "$" in query or bool(PROTECTED_GRAPHQL_MUTATIONS.search(query))


def classify_issue_close(tokens: list[str]) -> tuple[str | None, str | None] | None:
    """Is this segment ANY form of `gh issue close`? Returns (repo, number)
    -- either may be None if unresolvable from this command alone -- or
    None if this segment is not an issue-close invocation at all."""
    tokens = strip_invocation_prefix(tokens)
    if not tokens or Path(tokens[0]).name != "gh":
        return None
    rest = tokens[1:]

    if rest[:1] == ["api"]:
        api_match = next((API_ISSUE_PATH.search(t) for t in rest if API_ISSUE_PATH.search(t)), None)
        if not api_match or not _method_is(rest, "PATCH") or not _has_field(rest, "state=closed"):
            return None
        return api_match.group(1), api_match.group(2)

    if rest[:2] == ["issue", "close"]:
        repo = _repo_flag(rest)
        number = next((t for t in rest[2:] if t.isdigit()), None)
        return repo, number

    return None


def classify_pr_merge(tokens: list[str]) -> tuple[str | None, int | None] | None:
    """Is this segment ANY form of `gh pr merge`? Returns (repo, number) --
    either may be None if unresolvable -- or None if not a pr-merge
    invocation at all."""
    tokens = strip_invocation_prefix(tokens)
    if not tokens or Path(tokens[0]).name != "gh":
        return None
    rest = tokens[1:]

    if rest[:1] == ["api"]:
        api_match = next((API_MERGE_PATH.search(t) for t in rest if API_MERGE_PATH.search(t)), None)
        if not api_match or not _method_is(rest, "PUT"):
            return None
        return api_match.group(1), int(api_match.group(2))

    if rest[:2] == ["pr", "merge"]:
        repo = _repo_flag(rest)
        number_token = next((t for t in rest[2:] if t.isdigit()), None)
        return repo, (int(number_token) if number_token else None)

    return None


def _ask_pr_merge_reason(tokens: list[str]) -> str:
    rest = tokens[1:]
    delete_branch = _has_field(rest, "delete_branch=true") or any(
        t in ("--delete-branch", "-d") for t in rest[2:]
    )
    reason = (
        "Merging a pull request requires the operator's explicit instruction "
        "every time -- unless a live BATCH authorization covers this exact issue."
    )
    if delete_branch:
        reason += (
            " Deleting the branch on merge also silently CLOSES any stacked "
            "child PR whose base is that branch -- retarget the child first."
        )
    return reason


def _match_issue_close(tokens: list[str], state: dict) -> tuple[str, dict, dict] | None:
    target_info = classify_issue_close(tokens)
    if target_info is None:
        return None
    repo, number = target_info
    if repo is None or number is None:
        return None
    key = issue_key(repo, number)
    entry = state.get(key) if key else None
    if entry is None:
        return None
    target = next((t for t in entry.get("targets", []) if "close" in t.get("action", "").lower()), None)
    if target is None:
        return None
    return key, entry, target


#: A branch segment carrying an issue number: an optional shorthand prefix
#: letter, then the number. `l1/h1757-gate-attribution` -> ("h", "1757");
#: `feat/1754-prompt-cache-parity` -> ("", "1754"). Two digits minimum, so the
#: lane segment in `l1/...` is not itself read as issue 1.
_BRANCH_ISSUE = re.compile(r"(?:^|[/_-])([A-Za-z]?)(\d{2,})(?=$|[/_-])")

#: A title reference, and ONLY in its documented trailing-parenthetical shape:
#: `(harmonic-forge#552)`, `(hrse#1754)`, `(#552)`, `(harmonic-forge#516 AC6)`.
#:
#: Unanchored `#N` anywhere in the title is NOT accepted, because a title that
#: merely cites an issue does not fulfil it. `fix(hooks): supersedes #549` on a
#: branch carrying no number derived F549 and merged an unauthorized PR against
#: F549's grant with no prompt — two wrong outcomes from one input, since F549's
#: real PR then correctly-but-wrongly asked "already CONSUMED".
_TITLE_ISSUE = re.compile(r"\((?:([A-Za-z][A-Za-z0-9-]*)#|#)(\d+)[^)]*\)")

_SLUG_TO_REPO = {repo.split("/")[-1]: repo for repo in REPO_PREFIXES}
_PREFIX_TO_REPO = {prefix.upper(): repo for repo, prefix in REPO_PREFIXES.items()}


def _carrier_key(prefix: str, number: str, pr_repo: str) -> str | None:
    """One carrier's `(prefix, number)` -> a BATCH key, or None if unresolvable.

    An unrecognised prefix or slug yields None rather than falling back to the
    PR's own repo. Guessing here would be the one way derivation could widen
    authorization, which AC5 forbids outright.
    """
    if not prefix:
        return issue_key(pr_repo, number)
    repo = _PREFIX_TO_REPO.get(prefix.upper()) or _SLUG_TO_REPO.get(prefix.lower())
    return issue_key(repo, number) if repo else None


#: Derived carriers are cached on disk, not in memory: every hook invocation is
#: a fresh process, so an in-process cache would never see a second hit. A PR's
#: head branch and title do not change in the window between opening it and
#: merging it, and the failure mode of a stale entry is bounded — it can only
#: name an issue, and a wrong issue still has to hold a live grant whose merge
#: target the operator authorized.
#: Resolved at CALL time, never bound at import. A module-level
#: `STATE_PATH.parent / ...` constant would freeze the value the moment this
#: module is first imported, so a test patching `batch_auth.STATE_PATH` would
#: silently keep writing the real cache — the same trap `_load`'s docstring
#: names, in a second place.
CARRIER_CACHE_NAME = "pr-carriers.json"
#: Ten minutes, not hours. The cache exists to absorb a retry burst — the
#: operator merging, being prompted, and merging again — which happens inside
#: one minute. A long TTL buys nothing more and costs correctness: the head
#: branch cannot change, but the TITLE can, and editing it is the only remedy
#: available to an author who sees their PR derive to the wrong issue. A 6h
#: window meant a corrected title was ignored for 6h.
CARRIER_CACHE_TTL_SECONDS = 600
#: Bounded so the file cannot grow without limit; the interesting entries are
#: always the recent ones.
CARRIER_CACHE_MAX = 200


def _carrier_cache_read(key: str, path: Path) -> tuple[str, str] | None:
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        entry = cache[key]
        if time.time() - float(entry["at"]) > CARRIER_CACHE_TTL_SECONDS:
            return None
        return entry["branch"], entry["title"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _carrier_cache_write(key: str, value: tuple[str, str], path: Path) -> None:
    """Best-effort. A cache that can fail a merge is worse than no cache."""
    try:
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                cache = {}
        except (OSError, ValueError):
            cache = {}
        cache[key] = {"at": time.time(), "branch": value[0], "title": value[1]}
        if len(cache) > CARRIER_CACHE_MAX:
            newest = sorted(cache.items(),
                            key=lambda kv: kv[1].get("at", 0), reverse=True)
            cache = dict(newest[:CARRIER_CACHE_MAX])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        return


def _pr_carriers(repo: str, pr_number: int,
                 cache_path: Path | None = None) -> tuple[str, str] | None:
    """`(head branch, title)` for a PR, or None if it cannot be read.

    REST, not GraphQL — `gh api repos/{owner}/{repo}/pulls/{n}` is the REST
    endpoint the house policy prefers (R-0083); the GraphQL-backed `gh pr view`
    would answer the same question and is deliberately not used.

    Cached, because this runs inside a `PreToolUse` hook: without it, an
    operator who retries a merge three times pays three round trips before
    three prompts. A miss is one REST call on a path whose alternative is
    stopping for a human.
    """
    path = cache_path or (STATE_PATH.parent / CARRIER_CACHE_NAME)
    key = f"{repo}#{pr_number}"
    cached = _carrier_cache_read(key, path)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}",
             # `.head.ref`, NOT `.headRefName`. The latter is the GraphQL /
             # `gh pr view --json` spelling; against this REST endpoint it
             # resolves to null, which returned ("", title) rather than an
             # error. Derivation was silently TITLE-ONLY, and the
             # carriers-disagree guard below could never fire. Caught by
             # preclose inspection on this very diff; the test that "covered"
             # it asserted only the first three argv entries and stubbed the
             # rest, so the wrong field name was invisible to 90 green tests.
             "--jq", ".head.ref, .title"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if len(lines) < 2:
        return None
    value = (lines[0].strip(), lines[1].strip())
    _carrier_cache_write(key, value, path)
    return value


def derive_issue_key(pr_repo: str, pr_number: int, state: dict) -> str | None:
    """Which BATCH key does this PR fulfil, when no `link_pr` record says?

    AC5 (harmonic-forge#552, carried from #549's AC1/AC2). The PR number is not
    knowable when the operator types BATCH, so `link_pr` is the explicit
    mapping — but forgetting it costs a prompt on an authorization that
    genuinely exists. Both of this house's PR carriers name the issue: the head
    branch (`l1/h1757-...`, `feat/1754-...`) and the title's `(repo#N)` suffix.

    **Fail-closed in three named ways, each of which returns None and therefore
    asks.** Derivation may only stop discarding a mapping already present; it
    may never widen authorization:

      * neither carrier yields a parseable, resolvable issue number;
      * the two carriers yield DIFFERENT keys — disagreement is not a tie to be
        broken, it is evidence that at least one reading is wrong;
      * the derived key has no authorization entry at all (checked by the
        caller, which then falls through to `_diagnose`).

    The PR's own number is discarded as a candidate: `gh` appends `(#1752)` to
    a squashed title, and a PR is not an authorization for itself.

    One `gh api` call, on the path that would otherwise have stopped for a
    human anyway — the latency objection that keeps `action_landed` out of
    `decide()` does not apply to a branch whose alternative is a prompt.
    """
    carriers = _pr_carriers(pr_repo, pr_number)
    if carriers is None:
        return None
    branch, title = carriers

    keys: set[str] = set()
    for prefix, number in _BRANCH_ISSUE.findall(branch or ""):
        key = _carrier_key(prefix, number, pr_repo)
        if key:
            keys.add(key)
    for slug, number in _TITLE_ISSUE.findall(title or ""):
        key = _carrier_key(slug, number, pr_repo)
        if key:
            keys.add(key)

    keys.discard(issue_key(pr_repo, pr_number) or "")
    return keys.pop() if len(keys) == 1 else None


def _derived_merge_match(repo: str, number: int,
                         state: dict) -> tuple[str, dict, dict] | None:
    """A live, unconsumed merge target on the key this PR derives to."""
    # REPO_PREFIXES is the account boundary, not merely a shorthand table:
    # credential isolation across engagements is a standing rule, and the close
    # path already refuses an unmapped repo (`_match_issue_close` returns None
    # when `issue_key` does). Without this, a PR in an unmapped repo on another
    # account (branch `l2/h395-port`) derived H395 and merged with no prompt.
    if repo not in REPO_PREFIXES:
        return None
    key = derive_issue_key(repo, number, state)
    if key is None:
        return None
    entry = state.get(key)
    if entry is None:
        return None
    for target in entry.get("targets", []):
        if "merge" not in target.get("action", "").lower():
            continue
        if target.get("consumed"):
            continue
        if target.get("pr_number") not in (None, number):
            continue
        return key, entry, target
    return None


def _match_pr_merge(tokens: list[str], state: dict) -> tuple[str, dict, dict] | None:
    target_info = classify_pr_merge(tokens)
    if target_info is None:
        return None
    repo, number = target_info
    if repo is None or number is None:
        return None
    # Prefer an UNCONSUMED slot; fall back to a consumed one only so
    # `_diagnose` can still say "already spent" when that is the whole truth.
    #
    # Returning the first match regardless of `consumed` was a defect
    # (harmonic-forge#549): a key holding two merge grants, the first spent and
    # the second live and linked to this very PR, resolved to the spent one and
    # asked. `decide()` marks a target consumed the moment it matches, whether
    # or not the command ran -- so a merge denied by a later guard, or
    # interrupted, permanently poisoned the first slot and every retry matched
    # the corpse instead of the live grant beside it. This is the same
    # ignoring-of-`consumed` fixed in `link_pr` above; the two together made a
    # linked, authorized, unexpired merge unreachable.
    fallback: tuple[str, dict, dict] | None = None
    for key, entry in state.items():
        for target in entry.get("targets", []):
            if "merge" not in target.get("action", "").lower():
                continue
            if target.get("repo") == repo and target.get("pr_number") == number:
                if not target.get("consumed"):
                    return key, entry, target
                if fallback is None:
                    fallback = (key, entry, target)
    if fallback is not None:
        return fallback
    # AC5: no `link_pr` record names this PR. Derive the mapping from the head
    # branch and title rather than discarding an authorization that exists.
    return _derived_merge_match(repo, number, state)


def _diagnose(tokens: list[str], state: dict, is_close: bool,
              now: datetime) -> str:
    """Why no authorization matched — the four states, told apart.

    `decide()` returning a bare "this requires explicit instruction" leaves the
    operator unable to distinguish "I never issued BATCH for this", "it
    expired", "it is already spent" and "the PR was never linked". Those need
    four different actions.
    """
    target_info = (classify_issue_close(tokens) if is_close
                   else classify_pr_merge(tokens))
    repo, number = target_info if target_info else (None, None)
    if repo is None or number is None:
        return ("[BATCH] Could not resolve this command to a repo and number, "
                "so no authorization could match it. Pass an explicit --repo.")

    if is_close:
        key = issue_key(repo, number)
        if key is None:
            # An unmapped repo has no shorthand, so no BATCH key can ever name
            # it. Saying "no authorization exists for None -- issue `BATCH
            # None`" sent the operator to type a literal impossibility.
            return (f"[BATCH] {repo} has no shorthand prefix, so no BATCH key "
                    "can refer to it. Add it to harmonic-forge's projects.toml "
                    "and rules/lane-shorthand.md, or close by explicit "
                    "instruction.")
        entry = state.get(key)
        if entry is None:
            return (f"[BATCH] No authorization exists for {key}. Issue one with "
                    f"a chat message containing `BATCH {key}`.")
        if not _entry_live(entry, now):
            return (f"[BATCH] {key} EXPIRED at {entry.get('expires_at', '?')}. "
                    f"Re-issue `BATCH {key}`.")
        return (f"[BATCH] {key} is live but was not authorized for a close "
                "action. Re-issue it, or close by explicit instruction.")

    # A merge carries a PR number, never an issue number, so the only way it
    # reaches an authorization is a recorded `link-pr`. That call has no
    # automatic caller either, which is why this is the common case.
    live = [k for k, e in state.items() if _entry_live(e, now)
            and any("merge" in t.get("action", "").lower()
                    for t in e.get("targets", []))]
    if not live:
        return ("[BATCH] No live authorization has a merge target at all. "
                "Issue one with a chat message containing `BATCH <KEY>`.")
    linked = [f"{t.get('repo')}#{t.get('pr_number')}"
              for k in live for t in state[k].get("targets", [])
              if t.get("pr_number") is not None]
    return (f"[BATCH] {repo}#{number} is not linked to any authorization. "
            f"`gh pr merge <PR#>` carries no issue number, so the mapping only "
            f"exists if `link-pr` recorded it. Live keys: {', '.join(sorted(live))}. "
            f"Linked PRs: {', '.join(linked) or 'none'}. Run:\n"
            f"  python3 tools/hooks/batch_auth.py link-pr <KEY> --repo {repo} "
            f"--pr {number}")


def decide(command: str, state_path: Path | None = None) -> tuple[str, str] | None:
    """The sole decision for `gh issue close`/`gh pr merge`, any form. See
    module docstring. Returns `("allow", reason)`, `("ask", reason)`, or
    `None` (not one of these two command classes -- silent).

    Fails toward `("ask", ...)` on anything unparseable, any internal
    exception, and any covered segment that isn't confidently resolvable to
    a live authorization -- never silent, never `allow` by default.
    """
    try:
        segments = command_segments(command)
    except ValueError:
        return "ask", (
            "Could not safely parse this command -- failing closed rather "
            "than silently allowing a possible issue-close or PR-merge."
        )

    # NO LOCK. AC1 made `decide()` read-only, and `_save` is a temp-file +
    # atomic `os.replace`, so a lockless read can never observe a partial
    # write — it sees the state either before or after, never during.
    #
    # Holding it was actively harmful once AC5 landed: derivation shells out to
    # `gh`, `_locked_state`'s own contract forbids holding the lock across a
    # subprocess, and the lock budget is 0.4s against a measured ~0.7s API
    # call. A concurrent session's fully-authorized merge got
    # "could not acquire the lock -- failing closed" — an unexplained operator
    # prompt on a valid grant, which is the exact symptom #552 exists to end.
    try:
        state = _load(state_path)
        now = _now()
        command_hash = _command_hash(command)
        covered = False
        allow_reason: str | None = None

        for tokens in segments:
            if _protected_graphql(tokens):
                return "ask", "GraphQL mutation is protected; use the reviewed CLI or REST authorization path."
            is_close = classify_issue_close(tokens) is not None
            is_merge = (not is_close) and classify_pr_merge(tokens) is not None
            if not is_close and not is_merge:
                continue
            covered = True

            match = _match_issue_close(tokens, state) if is_close else _match_pr_merge(tokens, state)
            reason = ASK_ISSUE_CLOSE if is_close else _ask_pr_merge_reason(tokens)
            # harmonic-forge#502 AC4: a prompt that does not say WHY is
            # indistinguishable from any other permission prompt. The
            # operator's own words on the incident that filed this issue:
            # "waiting for my ok to merge/close OR SOMETHING I COULDN'T
            # TELL WHAT." These four states need four different actions,
            # and only the diagnostic makes that self-service.
            if match is None:
                return "ask", reason + "\n\n" + _diagnose(tokens, state, is_close, now)
            key, entry, target = match
            if not _entry_live(entry, now):
                return "ask", reason + (
                    f"\n\n[BATCH] {key} WAS authorized but EXPIRED at "
                    f"{entry.get('expires_at', '?')}. Re-issue BATCH {key}.")
            if target.get("consumed") and target.get("consumed_by") != command_hash:
                return "ask", reason + (
                    f"\n\n[BATCH] {key}'s "
                    f"{'close' if is_close else 'merge'} target is already "
                    "CONSUMED by a different command. A close is single-use "
                    "by design; a merge allocates a new target on the next "
                    "`link-pr`, so run that first if this is a second repo.")
            # AC1 (harmonic-forge#552): decide() is READ-ONLY. It used to
            # set consumed/consumed_by and _save() here, which is the root
            # cause of the incident that filed #552. A PreToolUse hook
            # cannot know whether the command will run: Claude Code
            # composes PreToolUse hooks under strongest-decision-wins, so
            # another hook's `deny` lands AFTER this write, and an operator
            # decline or a failed merge never reaches execution either.
            # Three live paths consumed a slot with no action taken, and
            # F544 reached FOUR consumed merge targets all pointing at the
            # still-unmerged PR 1753 — four matches, zero executions.
            #
            # Consumption now happens in batch_consume.py on PostToolUse,
            # which fires only after the tool ran, and only once the merge
            # or close is confirmed to have actually landed (AC3).
            allow_reason = f"BATCH-authorized ({key})"

        if not covered:
            return None
        return "allow", allow_reason
    except Exception:
        return "ask", (
            "Internal error classifying this command -- failing closed "
            "rather than silently allowing a possible issue-close or PR-merge."
        )


def action_landed(kind: str, repo: str, number: str | int) -> bool | None:
    """Did the merge/close this command was authorized for ACTUALLY happen?

    AC3 (harmonic-forge#552). `PostToolUse` fires on failure too — a merge that
    exits "not mergeable: the base branch policy prohibits the merge" reaches
    this code path exactly as a successful one does. Consuming unconditionally
    there would just move the over-count from PreToolUse to PostToolUse rather
    than fixing it. Two of the three no-execution paths (`deny` from a sibling
    hook, operator decline) never reach PostToolUse at all; this check is what
    covers the third.

    Cheap here, and only here: one API call after the fact, where the same call
    inside the PreToolUse path would have put network latency in front of every
    merge and close the operator issues. REST (`gh api repos/.../pulls/N`), per
    R-0083 — `gh pr view --json merged` is GraphQL-backed.

    **Deliberately NOT cached**, unlike `_pr_carriers`. That one reads two
    fields that do not change between opening a PR and merging it; this one
    asks the single question whose answer flips at exactly the moment being
    tested. A cached "not merged yet" would leave a landed merge unconsumed,
    and a cached "merged" is the wrong-consumption defect this issue exists to
    remove.

    Returns None when the answer cannot be established, and the caller then
    declines to consume — an unconsumed live grant costs one extra prompt,
    a wrongly-consumed one costs a stuck batch.
    """
    endpoint = (f"repos/{repo}/pulls/{number}" if kind == "merge"
                else f"repos/{repo}/issues/{number}")
    field = ".merged" if kind == "merge" else '.state == "closed"'
    result = subprocess.run(
        ["gh", "api", endpoint, "--jq", field],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return None
    answer = result.stdout.strip().lower()
    if answer in ("true", "false"):
        return answer == "true"
    return None


def _relocate_target(entry: dict, is_close: bool, repo: str,
                     number) -> dict | None:
    """Re-find, in a freshly-loaded entry, the slot a pre-lock match resolved.

    The match is made OUTSIDE the lock (it can shell out to `gh`), so the dict
    it returns belongs to a state snapshot that may be stale by the time the
    write lock is held. Writing through that stale dict would silently drop a
    concurrent session's change. This finds the slot again in the current
    state, and only ever returns an UNCONSUMED one — so a slot another process
    consumed in between is never overwritten.
    """
    want = "close" if is_close else "merge"
    slots = [s for s in entry.get("targets", [])
             if want in s.get("action", "").lower()]
    if not is_close:
        for slot in slots:
            if (slot.get("repo") == repo and slot.get("pr_number") == number
                    and not slot.get("consumed")):
                return slot
    return next((s for s in slots if not s.get("consumed")), None)


def consume(command: str, state_path: Path | None = None,
            landed=None) -> list[str]:
    """Mark the targets `command` was authorized against as consumed, after it ran.

    AC2 (harmonic-forge#552). This is the write half that `decide()` used to
    perform at PreToolUse time. It runs from `batch_consume.py` on
    `PostToolUse`, which fires only once the tool has actually executed.

    `consumed_by` keeps its existing meaning — the same `_command_hash` of the
    same command text — so `decide()`'s deliberate re-allow of an identical
    retry is unaffected.

    Returns the list of keys consumed, which is **not** always at most one.
    `gh pr merge A && gh pr merge B` is the normal shape of a BATCH run, and
    the incident itself involved a bundled call: an earlier draft of this
    function returned on the first success, so the second merge landed while
    its slot still said the merge had never happened — leaving a live grant any
    later command could spend, and (because `block_batch_stop.py` keys on the
    close target) wedging every turn-end in the repo for the rest of the TTL.

    Never raises into the hook: a failure to record consumption must not break
    the session, and the cost of missing one is a single extra prompt.
    """
    landed = landed or action_landed
    actual_path = STATE_PATH if state_path is None else state_path
    try:
        segments = command_segments(command)
    except ValueError:
        return []

    # Classify BEFORE touching the state file or the lock. This runs on EVERY
    # Bash tool call in the session, and virtually none of them are a merge or
    # a close; taking an exclusive lock on all of them made every unrelated
    # command a contender for a 0.4s budget shared with the real ones.
    work: list[tuple[list[str], bool, tuple]] = []
    for raw in segments:
        tokens = strip_invocation_prefix(raw)
        close_info = classify_issue_close(tokens)
        if close_info is not None:
            work.append((tokens, True, close_info))
            continue
        merge_info = classify_pr_merge(tokens)
        if merge_info is not None:
            work.append((tokens, False, merge_info))
    if not work:
        return []

    command_hash = _command_hash(command)
    consumed: list[str] = []

    for tokens, is_close, info in work:
        repo, number = info
        if not repo or not number:
            continue
        try:
            # Match, derive and confirm-landed all happen OUTSIDE the lock.
            # `_locked_state`'s own contract forbids holding it across a
            # subprocess, and both AC5 derivation and `action_landed` shell out
            # to `gh` — measured ~0.7s against a 0.4s lock budget, which turned
            # one session's network call into another session's spurious prompt
            # on a valid grant.
            state = _load(state_path)
            now = _now()
            match = (_match_issue_close(tokens, state) if is_close
                     else _match_pr_merge(tokens, state))
            if match is None:
                continue
            key, entry, target = match
            if not _entry_live(entry, now) or target.get("consumed"):
                continue

            # Confirm the PR/issue the COMMAND names, not the one the slot
            # records. They agree whenever `link_pr` ran; on the AC5 derivation
            # path the slot carries no `pr_number` yet, and reading it from the
            # slot would silently skip consumption for exactly the merges
            # derivation just made possible.
            if landed("close" if is_close else "merge", repo, number) is not True:
                # Ran and did not land, or could not be established. Leave the
                # grant live — this is the whole point of the issue.
                continue

            # Only now, and only for the write.
            with _locked_state(actual_path):
                fresh = _load(state_path)
                fresh_entry = fresh.get(key)
                if fresh_entry is None or not _entry_live(fresh_entry, _now()):
                    continue
                slot = _relocate_target(fresh_entry, is_close, repo, number)
                if slot is None:
                    continue
                slot["consumed"] = True
                slot["consumed_by"] = command_hash
                if not is_close and slot.get("pr_number") is None:
                    # Persist what derivation worked out, so the mapping is on
                    # the record the operator reads rather than only in a
                    # decision that has already been made.
                    slot["repo"], slot["pr_number"] = repo, number
                _save(fresh, state_path)
                consumed.append(key)
        except Exception:
            continue
    return consumed


GATE_HOOK = "batch_gate.py"
CONSUME_HOOK = "batch_consume.py"


def _registered_hooks(settings: dict, event: str, needle: str) -> bool:
    for block in (settings.get("hooks") or {}).get(event) or []:
        for hook in block.get("hooks") or []:
            if needle in (hook.get("command") or ""):
                return True
    return False


def verify_registration(settings_path: Path | None = None) -> tuple[bool, str]:
    """Are the gate and its consumer registered at the SAME scope?

    Found by preclose inspection on harmonic-forge#552, and the sharpest
    finding of the three panels. `batch_gate.py` is registered in the USER
    settings, so `decide()` runs in every project. The consumer was registered
    in two projects' settings. In `cymagraph-infra` and `openclaw-projects` —
    both in `REPO_PREFIXES`, both BATCH-eligible — the gate therefore allowed
    and nothing ever consumed: a single-use close target silently became
    multi-use for the full 12h TTL, and `block_batch_stop.py` (which keys on
    that target) would wedge every turn-end until it expired.

    Before AC1 this could not happen, because the write lived in the
    globally-registered half. Splitting Pre from Post split the SCOPE too, and
    a prose note saying "remember to register both" is precisely the thing that
    drifts — hence a check rather than a sentence.
    """
    path = settings_path or (Path.home() / ".claude" / "settings.json")
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, f"could not read {path}"
    gate = _registered_hooks(settings, "PreToolUse", GATE_HOOK)
    consume_hook = _registered_hooks(settings, "PostToolUse", CONSUME_HOOK)
    if gate and consume_hook:
        return True, f"{path}: gate and consumer both registered"
    if not gate and not consume_hook:
        return True, f"{path}: neither registered (consistent)"
    missing = CONSUME_HOOK if gate else GATE_HOOK
    return False, (
        f"{path}: {GATE_HOOK if gate else CONSUME_HOOK} is registered here but "
        f"{missing} is NOT. The gate and its consumer must share a scope — a "
        f"gate without a consumer allows merges that are never marked spent.")


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_auth = sub.add_parser("authorize", help="Write one entry per issue key")
    p_auth.add_argument("keys", nargs="+", help="Issue keys, e.g. H395 F334")
    p_auth.add_argument(
        "--action", dest="actions", action="append",
        help='e.g. "gh pr merge" or "gh issue close" -- repeatable. '
             f"Default (if omitted): both {DEFAULT_ACTIONS!r}.",
    )
    p_auth.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)

    p_top = sub.add_parser(
        "top-up",
        help="ADD TO OR EXTEND a running batch — the safe default. Extends a "
             "live key's expiry and leaves its targets untouched; authorizes "
             "a new or expired key normally. Use this, not `authorize`, when "
             "the operator adds an issue to a batch already in flight.",
    )
    p_top.add_argument("keys", nargs="+", help="Issue keys, e.g. H395 F334")
    p_top.add_argument(
        "--action", dest="actions", action="append",
        help="Applies only to keys that are newly authorized; a live key's "
             "targets are never rewritten. Repeatable. "
             f"Default (if omitted): both {DEFAULT_ACTIONS!r}.",
    )
    p_top.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    p_consume = sub.add_parser(
        "consume", help="Mark the target a command authorized as consumed, after it ran")
    p_consume.add_argument("--command", required=True)

    p_link = sub.add_parser("link-pr", help="Record the PR that fulfils an authorized issue's merge target")
    p_link.add_argument("key")
    p_link.add_argument("--repo", required=True)
    p_link.add_argument("--pr", type=int, required=True, dest="pr_number")

    p_revoke = sub.add_parser(
        "revoke",
        help="Stand down one or more keys -- marks unconsumed targets "
             "consumed rather than deleting the entry (harmonic-forge#567 "
             "AC4). A no-op on an unknown or already-consumed key.",
    )
    p_revoke.add_argument("keys", nargs="+", help="Issue keys, e.g. H395 F334")

    p_check = sub.add_parser(
        "check-hooks",
        help="Verify the gate and its consumer are registered at the same scope")
    p_check.add_argument("--settings", default=None)

    args = parser.parse_args()
    if args.cmd == "authorize":
        actions = args.actions if args.actions else list(DEFAULT_ACTIONS)
        authorize(args.keys, actions, args.ttl_hours)
        print(f"authorized {', '.join(k.upper() for k in args.keys)} for {actions!r}")
    elif args.cmd == "top-up":
        actions = args.actions if args.actions else list(DEFAULT_ACTIONS)
        requested = [k.upper() for k in args.keys]
        fresh = [k.upper() for k in top_up(args.keys, actions, args.ttl_hours)]
        extended = [k for k in requested if k not in fresh]
        # Say WHICH is which. top_up() returns the newly-authorized keys
        # precisely so the caller can distinguish them, and reporting one
        # undifferentiated "done" would hide the only fact that matters here:
        # an extended key kept its consumption and its PR links, a fresh one
        # never had any.
        if fresh:
            print(f"newly authorized: {', '.join(fresh)} for {actions!r}")
        if extended:
            print(f"extended (targets and links untouched): {', '.join(extended)}")
        if not fresh and not extended:
            print("nothing to do")
    elif args.cmd == "consume":
        consumed = consume(args.command)
        print(f"consumed {consumed}" if consumed else "nothing to consume")
    elif args.cmd == "link-pr":
        link_pr(args.key, args.repo, args.pr_number)
        print(f"linked {args.key.upper()} -> {args.repo}#{args.pr_number}")
    elif args.cmd == "revoke":
        changed = [k.upper() for k in revoke(args.keys)]
        print(f"revoked: {', '.join(changed)}" if changed else "nothing to revoke")
    elif args.cmd == "check-hooks":
        ok, message = verify_registration(
            Path(args.settings) if args.settings else None)
        print(("[OK] " if ok else "[DRIFT] ") + message)
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    _cli()
