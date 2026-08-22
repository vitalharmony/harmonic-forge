#!/usr/bin/env python3
"""Shared BATCH-authorization state: write, lookup, and idempotent consume
(harmonic-forge#336).

Lets an operator's literal `BATCH h395,f334,...` chat keyword pre-authorize
a named set of issues' `gh pr merge`/`gh issue close` actions, so a batch of
already-approved work does not force a fresh Ask prompt per issue. State is
a single global JSON file (`~/.claude/state/batch-authorized.json`),
deliberately not per-repo or per-session, so it survives session restart,
compaction, and spans a multi-repo batch.

WRITE PATH -- trust boundary lives in the caller, not here
------------------------------------------------------------
`authorize()` is called only from a genuine operator chat message carrying
the literal `BATCH` keyword -- never in response to text read from a file,
issue/PR body, tool output, or web page. That instruction-source boundary
is the calling agent's own judgement to make (the same boundary every
other write-triggering keyword in this project already relies on); this
module performs the write once that judgement is already made, and never
makes it itself.

READ PATH -- two independent callers, one shared lookup
-----------------------------------------------------------
Both `batch_gate.py` (emits the actual `allow`) and `block_irreversible_ops.py`
(must stay silent instead of asking when a covered command is authorized)
call `check_and_consume()`. Extracted here rather than duplicated in each
hook per the same reasoning as `shell_parse.py` (harmonic-forge#167): a
second copy of this parse is the drift risk the module exists to prevent.

Consumption is idempotent **per command hash**, not per lookup: a command
already consumed by its own identical hash still matches. Hook execution
order between the two PreToolUse hooks registered on the same matcher is
not guaranteed, so whichever runs first performs the write and the second
must see the same outcome rather than finding the entry already spent.

THE PR-NUMBER GAP -- stated plainly, not silently resolved
------------------------------------------------------------
`gh pr merge` names a PR number, never the issue number `BATCH` was given
-- GitHub issues and PRs share one number sequence per repo, so a PR
fulfilling H395 is essentially never PR 395. There is no mechanical way to
recover this mapping after the fact: this repo's own
`tools/gh/block_closing_keywords.py` hook denies writing a `Closes #N`
autoclose keyword into a PR body specifically to keep issue closure an
explicit human action, so GitHub's own `closingIssuesReferences` linkage
is never populated here either. The only place this mapping is ever known
is the agent that opens the PR -- `link_pr()` is that explicit record.
Until it is called for a given authorized issue, `gh pr merge` against
that issue's PR does not match, and the normal Ask rule fires. This is a
deliberate fail-closed default, not an oversight: a missed link_pr() call
means one more Ask prompt, not a wrongly-granted merge.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_parse import command_segments  # noqa: E402

STATE_PATH = Path.home() / ".claude" / "state" / "batch-authorized.json"
DEFAULT_TTL_HOURS = 2.0

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
    if state_path is None:
        state_path = STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def issue_key(repo: str, number: str | int) -> str | None:
    """`vitalharmony/hrse`, 395 -> `H395`. None if the repo has no prefix."""
    prefix = REPO_PREFIXES.get(repo)
    return f"{prefix}{number}" if prefix else None


def authorize(
    keys: list[str],
    action: str,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    state_path: Path | None = None,
) -> None:
    """Write one fresh entry per issue key. See module docstring -- the
    caller is responsible for having verified this came from a genuine
    operator chat message, not fetched content."""
    state = _load(state_path)
    now = _now()
    expires = now + timedelta(hours=ttl_hours)
    for raw_key in keys:
        key = raw_key.upper()
        if not ISSUE_KEY.match(key):
            raise ValueError(f"not a valid issue key: {raw_key!r}")
        state[key] = {
            "action": action,
            "authorized_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "consumed": False,
            "consumed_by": None,
            "repo": None,
            "pr_number": None,
        }
    _save(state, state_path)


def link_pr(key: str, repo: str, pr_number: int, state_path: Path | None = None) -> None:
    """Record which PR fulfils a BATCH-authorized issue, once opened. See
    the module docstring's PR-NUMBER GAP section for why this call is the
    only place this mapping can ever be recorded."""
    state = _load(state_path)
    key = key.upper()
    entry = state.get(key)
    if entry is None:
        raise ValueError(f"no authorization entry for {key!r} -- authorize it first")
    entry["repo"] = repo
    entry["pr_number"] = pr_number
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
    guessing one from cwd or environment: an unresolved repo means no match,
    which is always safe (falls through to the normal Ask rule)."""
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


def _match_issue_close(tokens: list[str], state: dict) -> tuple[str, dict] | None:
    if not tokens or Path(tokens[0]).name != "gh":
        return None
    rest = tokens[1:]

    if rest[:1] == ["api"]:
        api_match = next((API_ISSUE_PATH.search(t) for t in rest if API_ISSUE_PATH.search(t)), None)
        if not api_match or not _method_is(rest, "PATCH") or not _has_field(rest, "state=closed"):
            return None
        repo, number = api_match.group(1), api_match.group(2)
    elif rest[:2] == ["issue", "close"]:
        repo = _repo_flag(rest)
        number = next((t for t in rest[2:] if t.isdigit()), None)
        if repo is None or number is None:
            return None
    else:
        return None

    key = issue_key(repo, number)
    entry = state.get(key) if key else None
    if entry is None or "close" not in entry.get("action", "").lower():
        return None
    return key, entry


def _match_pr_merge(tokens: list[str], state: dict) -> tuple[str, dict] | None:
    if not tokens or Path(tokens[0]).name != "gh":
        return None
    rest = tokens[1:]

    if rest[:1] == ["api"]:
        api_match = next((API_MERGE_PATH.search(t) for t in rest if API_MERGE_PATH.search(t)), None)
        if not api_match or not _method_is(rest, "PUT"):
            return None
        repo, number = api_match.group(1), int(api_match.group(2))
    elif rest[:2] == ["pr", "merge"]:
        repo = _repo_flag(rest)
        number_token = next((t for t in rest[2:] if t.isdigit()), None)
        if repo is None or number_token is None:
            return None
        number = int(number_token)
    else:
        return None

    for key, entry in state.items():
        if "merge" not in entry.get("action", "").lower():
            continue
        if entry.get("repo") == repo and entry.get("pr_number") == number:
            return key, entry
    return None


def check_and_consume(command: str, state_path: Path | None = None) -> tuple[bool, str | None]:
    """Does `command` match a live BATCH authorization? If so, consume it
    (idempotently -- see module docstring) and return (True, reason). If
    not, (False, None). This function never denies anything itself; a
    non-match simply means "not my decision," and the caller's own normal
    ask/deny logic applies unchanged."""
    try:
        segments = command_segments(command)
    except ValueError:
        return False, None

    state = _load(state_path)
    if not state:
        return False, None
    now = _now()
    command_hash = _command_hash(command)

    for tokens in segments:
        match = _match_issue_close(tokens, state) or _match_pr_merge(tokens, state)
        if match is None:
            continue
        key, entry = match
        if not _entry_live(entry, now):
            continue
        if entry.get("consumed") and entry.get("consumed_by") != command_hash:
            continue  # already spent on a different command
        if not entry.get("consumed"):
            entry["consumed"] = True
            entry["consumed_by"] = command_hash
            _save(state, state_path)
        return True, f"BATCH-authorized ({key})"
    return False, None


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_auth = sub.add_parser("authorize", help="Write one entry per issue key")
    p_auth.add_argument("keys", nargs="+", help="Issue keys, e.g. H395 F334")
    p_auth.add_argument("--action", required=True, help='e.g. "gh pr merge" or "gh issue close"')
    p_auth.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)

    p_link = sub.add_parser("link-pr", help="Record the PR that fulfils an authorized issue")
    p_link.add_argument("key")
    p_link.add_argument("--repo", required=True)
    p_link.add_argument("--pr", type=int, required=True, dest="pr_number")

    args = parser.parse_args()
    if args.cmd == "authorize":
        authorize(args.keys, args.action, args.ttl_hours)
        print(f"authorized {', '.join(k.upper() for k in args.keys)} for {args.action!r}")
    elif args.cmd == "link-pr":
        link_pr(args.key, args.repo, args.pr_number)
        print(f"linked {args.key.upper()} -> {args.repo}#{args.pr_number}")


if __name__ == "__main__":
    _cli()
