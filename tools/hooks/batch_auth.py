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
`link_pr()` is that explicit record. Until it is called for a given
authorized issue, `gh pr merge` against that issue's PR does not match, and
`decide()` returns `("ask", ...)`. Deliberate fail-closed default: a missed
`link_pr()` call means one more Ask prompt, never a wrongly-granted merge.

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
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

STATE_PATH = Path.home() / ".claude" / "state" / "batch-authorized.json"
DEFAULT_TTL_HOURS = 2.0
DEFAULT_ACTIONS = ("gh pr merge", "gh issue close")

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
        target = next((t for t in entry.get("targets", []) if "merge" in t.get("action", "").lower()), None)
        if target is None:
            raise ValueError(f"{key!r} was not authorized for a merge action -- authorize it with --action 'gh pr merge' first")
        target["repo"] = repo
        target["pr_number"] = pr_number
        _save(state, state_path)


def _entry_live(entry: dict, now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(entry["expires_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return now < expires


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


def _match_pr_merge(tokens: list[str], state: dict) -> tuple[str, dict, dict] | None:
    target_info = classify_pr_merge(tokens)
    if target_info is None:
        return None
    repo, number = target_info
    if repo is None or number is None:
        return None
    for key, entry in state.items():
        for target in entry.get("targets", []):
            if "merge" not in target.get("action", "").lower():
                continue
            if target.get("repo") == repo and target.get("pr_number") == number:
                return key, entry, target
    return None


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

    actual_path = STATE_PATH if state_path is None else state_path
    try:
        with _locked_state(actual_path):
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
                if match is None:
                    return "ask", reason
                key, entry, target = match
                if not _entry_live(entry, now):
                    return "ask", reason
                if target.get("consumed") and target.get("consumed_by") != command_hash:
                    return "ask", reason  # already spent on a different command
                if not target.get("consumed"):
                    target["consumed"] = True
                    target["consumed_by"] = command_hash
                    _save(state, state_path)
                allow_reason = f"BATCH-authorized ({key})"

            if not covered:
                return None
            return "allow", allow_reason
    except StateLockTimeout:
        return "ask", (
            "Could not acquire the authorization state lock promptly -- "
            "failing closed rather than risking a lost concurrent-consumption race."
        )
    except Exception:
        return "ask", (
            "Internal error classifying this command -- failing closed "
            "rather than silently allowing a possible issue-close or PR-merge."
        )


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

    p_link = sub.add_parser("link-pr", help="Record the PR that fulfils an authorized issue's merge target")
    p_link.add_argument("key")
    p_link.add_argument("--repo", required=True)
    p_link.add_argument("--pr", type=int, required=True, dest="pr_number")

    args = parser.parse_args()
    if args.cmd == "authorize":
        actions = args.actions if args.actions else list(DEFAULT_ACTIONS)
        authorize(args.keys, actions, args.ttl_hours)
        print(f"authorized {', '.join(k.upper() for k in args.keys)} for {actions!r}")
    elif args.cmd == "link-pr":
        link_pr(args.key, args.repo, args.pr_number)
        print(f"linked {args.key.upper()} -> {args.repo}#{args.pr_number}")


if __name__ == "__main__":
    _cli()
