#!/usr/bin/env python3
"""Stop hook: report a lane artifact posted on a `deep` issue from a model
that is not high-tier (harmonic-forge#656 AC5).

The backstop behind `tier_model_trigger_check.py`. That hook blocks at the
moment work is assigned; this one catches what got through anyway -- a
trigger phrased in a way the parser missed, a check skipped by `LANE_MODEL`,
or a post on an issue the prompt never named. It fires after the work is
done, so it cannot prevent anything; it guarantees the operator sees it.

Reads the current turn from `transcript_path` (back to the last real prompt,
bounded like every other transcript read in this directory) and collects the
Bash tool calls that post to an issue thread and did not error:
`l1_post.py`, `l2_post.py post`, `post_comment.py`, `mise run lane-comment`,
`gh issue comment`, and `gh api .../issues/N/comments` with a POST. For each
posted `(repo, issue)` it reads the board Tier fresh (`ttl=0`) and,
for `deep`, compares the model that made the call (that assistant entry's
`message.model`, else `session_model.current_model`).

The transcript read is bounded (`_SCAN_MAX_BYTES`). When the bound is hit
before the start of the turn, the report says so ("backstop scan truncated"),
because a post made early in a long turn was otherwise silently unchecked.

**Never blocks.** A Stop `block` forces Claude to continue rather than handing
control to the operator, which is the opposite of what a report is for. Output
is a `systemMessage` only. Lane 3 is skipped (AC7). Deliberately names no
override.

Claude Code only. Codex and Gemini lanes do not run this hook and are not
covered.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))
import model_tier_gate  # noqa: E402
import session_model  # noqa: E402
from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

_MAX_TIER_READS = 6
_READ_TIMEOUT_SECONDS = 3
_POSTER_SCRIPTS = frozenset({"l1_post.py", "l2_post.py", "post_comment.py"})
# Scripts whose --repo defaults to hrse when omitted (HRSE2's l1_post.py and
# its `lane-comment` mise task); everything else falls back to the cwd repo.
_HRSE_DEFAULT = "vitalharmony/hrse"
_API_COMMENTS_RE = re.compile(r"(?:^|/)repos/([^/\s]+/[^/\s]+)/issues/(\d+)/comments/?$")
_ISSUE_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")
_GH_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
# The transcript tail read's bound, passed to `session_model._tail_lines`.
_SCAN_MAX_BYTES = 4 << 20
_SCAN_CHUNK_BYTES = 65536
_TRUNCATED_NOTE = "backstop scan truncated; earlier posts in this turn were not checked"


def _flag(tokens: list[str], *names: str) -> str | None:
    for index, token in enumerate(tokens):
        for name in names:
            if token == name and index + 1 < len(tokens):
                return tokens[index + 1]
            if name.startswith("--") and token.startswith(name + "="):
                return token.split("=", 1)[1]
    return None


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    match = _ISSUE_URL_RE.search(value)
    if match:
        return int(match.group(2))
    return int(value) if value.isdigit() else None


def posted_targets(command: str, cwd_repo) -> list[tuple[str, int]]:
    """`(repo, issue)` pairs a Bash command posts a comment to.

    `cwd_repo` is a zero-argument callable, so the cwd's `git`/`mise.toml`
    read happens only for a post that actually needs it.
    """
    try:
        segments = command_segments(command)
    except ValueError:
        return []
    targets: list[tuple[str, int]] = []
    for raw in segments:
        tokens = strip_invocation_prefix(raw)
        if not tokens:
            continue
        names = [os.path.basename(t) for t in tokens]
        repo: str | None = None
        issue: int | None = None
        script = next((i for i, n in enumerate(names) if n in _POSTER_SCRIPTS), None)
        if script is not None:
            rest = tokens[script + 1:]
            if names[script] == "l2_post.py" and "post" not in rest:
                continue  # snapshot / resolve-lock post nothing
            issue = _as_int(_flag(rest, "--issue"))
            repo = _flag(rest, "--repo") or (
                _HRSE_DEFAULT if names[script] == "l1_post.py" else None)
        elif "lane-comment" in tokens and names[0] == "mise":
            rest = tokens[tokens.index("lane-comment") + 1:]
            issue = _as_int(_flag(rest, "--issue"))
            repo = _flag(rest, "--repo") or _HRSE_DEFAULT
        elif names[0] == "gh" and tokens[1:3] == ["issue", "comment"] and len(tokens) > 3:
            issue = _as_int(tokens[3])
            url = _ISSUE_URL_RE.search(tokens[3])
            repo = _flag(tokens[4:], "--repo", "-R") or (url.group(1) if url else None)
        elif names[0] == "gh" and len(tokens) > 1 and tokens[1] == "api":
            path = next((t for t in tokens[2:] if _API_COMMENTS_RE.search(t)), None)
            if path is None:
                continue
            method = (_flag(tokens, "-X", "--method") or "").upper()
            has_fields = any(t in _GH_FIELD_FLAGS or t.startswith("--field=")
                             or t.startswith("--raw-field=") for t in tokens)
            if method != "POST" and not (has_fields and not method):
                continue
            match = _API_COMMENTS_RE.search(path)
            repo = match.group(1)
            issue = int(match.group(2))
            if "{owner}" in repo or "{repo}" in repo:
                repo = None
        else:
            continue
        if issue is None:
            continue
        repo = repo or cwd_repo()
        if repo and (repo, issue) not in targets:
            targets.append((repo, issue))
    return targets


def _is_turn_start(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def turn_posts(transcript_path: str) -> list[tuple[str, str | None, str]]:
    """`(command, model_of_that_call, tool_use_id)` for this turn's successful
    Bash calls, oldest first."""
    return scan_turn(transcript_path)[0]


def _bytes_scanned(size: int) -> int:
    """How much of a `size`-byte file `_tail_lines` reads: whole chunks until
    `_SCAN_MAX_BYTES` is reached, never more than the file."""
    chunks = -(-_SCAN_MAX_BYTES // _SCAN_CHUNK_BYTES)
    return min(size, chunks * _SCAN_CHUNK_BYTES)


def scan_turn(transcript_path: str) -> tuple[list[tuple[str, str | None, str]], bool]:
    """`(turn_posts, truncated)`. `truncated` is True when the bounded tail
    read ran out before reaching the turn's first entry (harmonic-forge#656
    preclose fix 6), so earlier calls in this turn were never seen."""
    errored: set[str] = set()
    calls: list[tuple[str, str | None, str]] = []
    reached_turn_start = False
    try:
        size = os.path.getsize(transcript_path)
        for line in session_model._tail_lines(transcript_path, chunk_size=_SCAN_CHUNK_BYTES,
                                              max_bytes=_SCAN_MAX_BYTES):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if _is_turn_start(entry):
                reached_turn_start = True
                break
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, list) or entry.get("isSidechain"):
                continue
            if entry.get("type") == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result" \
                            and block.get("is_error"):
                        errored.add(block.get("tool_use_id"))
            elif entry.get("type") == "assistant":
                model = (entry.get("message") or {}).get("model")
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use" \
                            and block.get("name") == "Bash":
                        command = (block.get("input") or {}).get("command")
                        if isinstance(command, str):
                            calls.append((command, model, block.get("id") or ""))
    except OSError:
        return [], False
    truncated = not reached_turn_start and size > _bytes_scanned(size)
    calls.reverse()
    return [call for call in calls if call[2] not in errored], truncated


def _timed_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return model_tier_gate.timed_run(cmd, timeout=_READ_TIMEOUT_SECONDS)


def run(payload: dict, env: dict | None = None, lookup=None, fallback_model=None) -> dict | None:
    env = os.environ if env is None else env
    if env.get("LANE") == "3":
        return None
    transcript_path = payload.get("transcript_path") or ""
    cwd = payload.get("cwd") or os.getcwd()
    cache: dict[str, str | None] = {}

    def cwd_repo():
        if "repo" not in cache:
            cache["repo"] = model_tier_gate.resolve_repo(cwd)
        return cache["repo"]

    posted: list[tuple[str, int, str | None]] = []
    calls, truncated = scan_turn(transcript_path)
    for command, model, _tool_id in calls:
        for repo, issue in posted_targets(command, cwd_repo):
            repo = repo.lower()
            if not any(p[:2] == (repo, issue) for p in posted):
                posted.append((repo, issue, model))
    messages: list[str] = [_TRUNCATED_NOTE + "."] if truncated else []
    if not posted:
        return _report(messages)

    if lookup is None:
        from tier_model_trigger_check import _boards  # noqa: PLC0415

        boards = _boards()

        def lookup(repo, number):
            board = boards.get(repo)
            if board is None:
                return None, None
            # ttl=0 (preclose fix 7): a report on a Tier raised mid-turn must
            # not be hidden by the edit gate's 120 s cache.
            return model_tier_gate.read_tier(repo, number, board, run=_timed_run, ttl=0)

    for repo, issue, call_model in posted[:_MAX_TIER_READS]:
        model = call_model or fallback_model or session_model.current_model(
            transcript_path, cwd, payload.get("session_id"))
        if model_tier_gate.claude_model_is_high(model):
            continue  # no board read needed: a high model satisfies any Tier
        label = model or "an unresolved model"
        tier, error = lookup(repo, issue)
        if tier is model_tier_gate.LOOKUP_FAILED:
            messages.append(
                f"Posted on {repo}#{issue} from {label}, but its Tier could not be read "
                f"({(error or 'unknown error')[:200]}); check whether it is deep."
            )
        elif tier in model_tier_gate.ESCALATING_TIERS:
            messages.append(
                f"Posted on {repo}#{issue} (Tier {tier}) from {label}; this needs "
                f"redoing on a high-tier model."
            )
    return _report(messages)


def _report(messages: list[str]) -> dict | None:
    if not messages:
        return None
    return {"systemMessage": "\n".join(messages) + " (harmonic-forge#656)"}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return
        result = run(payload)
    except Exception as exc:  # noqa: BLE001 -- a report must never break a turn
        print(f"tier_model_stop_backstop: internal error: {exc!r}", file=sys.stderr)
        return
    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
