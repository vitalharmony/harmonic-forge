#!/usr/bin/env python3
"""Close the other two routes a Lane 3 PASS can reach an issue uncovered
(harmonic-forge#565, follow-up to #504).

#504 made a Lane 3 PASS unpostable while the PR's own CI is red, pending, or
absent -- but the refusal lives in HRSE2's `scripts/post_lane_discussion.py`,
which calls `gate_ci.check_gate_result()` itself. That is one of at least
three sanctioned routes a gate result can reach an issue:

| Route | Guarded before this issue? |
|---|---|
| `post_lane_discussion.py` (any kind) | yes -- #504, in-script |
| `gh issue comment --body`/`--body-file` | **no** |
| `tools/gh/post_comment.py` / `mise run post-comment` | **no** |

And Lane 3's freedom to use either of the last two is deliberate, not an
oversight -- `block_lane2_status_claims.py`: "Lane 3 has no raw-post
restriction by design"; `deny_lane3_ae_self_post.py`: "raw `gh issue comment`
remains legitimate for LANE=2/3 sessions." So this hook adds a CONTENT check
at the two open routes, never a route restriction (AC4) -- it denies a body
that reads as an unready PASS, the same as #504 already does for the third
route, and does nothing else.

`post_lane_discussion.py` itself is deliberately NOT re-matched here --
#504 already guards it in-script, and re-checking it here would just be a
second network round-trip for the same answer. Body recognition (AC2) means
that exclusion is about avoiding duplicate work, not about which tool gets a
free pass: any OTHER transport carrying the same body is still checked.

## Body extraction, mirroring `deny_lane3_ae_self_post.py`'s established shape

That hook is the working precedent named in #565's own "Likely shape"
section for reading a `--body`/`--body-file` payload at PreToolUse time.
Covered here: `gh issue comment ... --body`/`--body-file` (inline or file),
`tools/gh/post_comment.py --file`/`--body` (bare, `python3 ...`, or via
`mise run post-comment`/`mise r post-comment`). Not covered: `gh api
repos/.../issues/.../comments` raw POST/PATCH calls carrying `-f
body=.../-F body=@file` -- same accepted gap `deny_lane3_ae_self_post.py`
documents, for the same reason: no established parsing convention for that
shape, and the threat model here is catching an accidental unready PASS, not
a deliberately adversarial session using an unusual transport.

**Wrapper forms, not just the bare command (preclose-inspection finding).**
The first draft matched only a bare `gh`/`mise run`, and preclose inspection
live-reproduced two bypasses of the exact class `block_lane2_status_claims.py`
was already written to close for a different guard:

- `gh-as <account> gh issue comment ...` -- `gh-as` (`rules/universal-agent.md`)
  is the MANDATED spelling in this house, not an alternate one; a check
  matching only bare `gh` missed the primary real-world shape entirely.
  Stripped and recursed on, exactly as `block_lane2_status_claims.py`'s
  `_is_push_or_pr_create` already does for its own guard.
- `mise r post-comment ...` -- `r` is mise's own documented alias for `run`;
  `block_lane2_status_claims.py`'s own docstring records this exact bypass
  being live-reproduced once already (`mise restart --push` / `mise r
  restart --push`), for a different check. `_mise_task_and_rest()` here is
  the same fix, reused rather than re-derived.
- `post_comment.py --body "<text>"` (inline, not just `--file`) -- the
  script's own argparse (`tools/gh/post_comment.py`) exposes `--file` and
  `--body` as a required mutually-exclusive pair; the first draft here read
  only the former, so half of this route's real interface was silently
  unreachable.

## Repo resolution

`gh issue comment`/`post_comment.py` both accept an explicit `--repo`; when
it's present, use it -- never guess a repo from cwd for a credential-
isolation-sensitive check (batch_auth.py's own `_repo_flag` makes the
identical choice, for the identical reason). When `--repo` is absent, this
hook does NOT invent one: an unresolved repo means this check cannot run, so
the segment is skipped (fails toward "allow", matching #504's own posture
that an unreadable input degrades the check rather than blocking the world
-- `gate_ci.py`'s `required_checks()`/`ci_conclusion()` return `None`/
`"unknown"` rather than raising, for the same reason).

## Never denies FAIL or BLOCKED (AC3)

Delegated entirely to `gate_ci.check_gate_result()`, which already refuses
only a PASS. This hook adds no verdict logic of its own -- it is a second
enforcement POINT for the same, single, existing check (AC2: reuse
`gate_ci.looks_like_a_gate_report()`/`check_gate_result()`; do not write a
second definition of what a gate report is or what makes CI green).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_parse import command_segments  # noqa: E402  (harmonic-forge#167)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
import gate_ci  # noqa: E402


def find_repo_flag(args: list[str]) -> str | None:
    for index, token in enumerate(args):
        if token == "--repo" and index + 1 < len(args):
            return args[index + 1]
        if token.startswith("--repo="):
            return token.partition("=")[2]
    return None


def _read_file(file_arg: str, cwd: Path) -> str | None:
    """Resolved against cwd if relative. None (fail-open) if unreadable."""
    path = Path(file_arg).expanduser()
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _mise_task_and_rest(args: list[str]) -> tuple[str | None, list[str]]:
    """`(task_name, remaining_args)` for a `mise` invocation, or `(None, [])`
    if `args` isn't one. `mise <task> [args]`, `mise run <task> [args]`, and
    `mise r <task> [args]` ("r" is mise's own documented alias for "run")
    all equally reach the task -- mirrors `block_lane2_status_claims.py`'s
    `_mise_task_and_rest`, which exists precisely because a check that only
    matched the `run` spelling was preclose-found bypassable via `mise r`."""
    if len(args) < 2 or args[0] != "mise":
        return None, []
    if args[1] in ("run", "r") and len(args) >= 3:
        return args[2], args[3:]
    return args[1], args[2:]


def _body_from_flags(args: list[str], cwd: Path) -> str | None:
    """`--body`/`--body=`/`--body-file`/`--body-file=`/`--file`/`--file=`
    from anywhere in `args`. Every route this hook covers accepts one of
    these spellings for the outgoing comment text."""
    for index, token in enumerate(args):
        if token in ("--body", "--file") and index + 1 < len(args):
            value = args[index + 1]
            return value if token == "--body" else _read_file(value, cwd)
        if token.startswith("--body="):
            return token.partition("=")[2]
        if token.startswith("--file="):
            return _read_file(token.partition("=")[2], cwd)
        if token in ("--body-file",) and index + 1 < len(args):
            return _read_file(args[index + 1], cwd)
        if token.startswith("--body-file="):
            return _read_file(token.partition("=")[2], cwd)
    return None


def _find_body_and_repo_inner(args: list[str], cwd: Path) -> tuple[str, str] | None:
    """`(body, repo)` for one command segment that posts a comment body via
    one of the two open routes, or None if this segment isn't one of them,
    or the body/repo can't be resolved. Assumes any `gh-as`/mise wrapper has
    already been stripped by the caller.

    `post_lane_discussion.py` is deliberately excluded -- #504 already
    guards it in-script; see module docstring.
    """
    is_post_comment_py = bool(args) and Path(args[0]).name == "post_comment.py"
    is_python_post_comment_py = (
        len(args) >= 2 and Path(args[0]).name.startswith("python")
        and Path(args[1]).name == "post_comment.py"
    )
    if is_post_comment_py or is_python_post_comment_py:
        rest = args[1:] if is_python_post_comment_py else args
        repo = find_repo_flag(rest)
        body = _body_from_flags(rest, cwd)
        return (body, repo) if body is not None and repo else None

    if len(args) >= 3 and args[0] == "gh" and args[1] == "issue" and args[2] == "comment":
        repo = find_repo_flag(args)
        body = _body_from_flags(args, cwd)
        return (body, repo) if body is not None and repo else None

    return None


def find_body_and_repo(args: list[str], cwd: Path) -> tuple[str, str] | None:
    """Wraps `_find_body_and_repo_inner` with the two wrapper forms this
    house's `gh`/mise invocations actually use:

    - `gh-as <account> <command...>` (`rules/universal-agent.md`) scopes
      `gh` to a named account for one command -- strip it and recurse,
      mirroring `block_lane2_status_claims.py`'s own established handling
      of the identical wrapper for a different guard (preclose-inspection
      finding on this issue: the bare-`gh`-only check missed the account-
      scoped spelling this repo's own rules mandate).
    - `mise (run|r) post-comment [--] [args]` -- both mise spellings, via
      `_mise_task_and_rest`, matching the same preclose precedent.
    """
    if args and Path(args[0]).name == "gh-as" and len(args) >= 3:
        return find_body_and_repo(args[2:], cwd)

    task, rest = _mise_task_and_rest(args)
    if task == "post-comment":
        rest = [token for token in rest if token != "--"]
        repo = find_repo_flag(rest)
        body = _body_from_flags(rest, cwd)
        return (body, repo) if body is not None and repo else None

    return _find_body_and_repo_inner(args, cwd)


def denial(message: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        },
        "systemMessage": message,
    }


def decision(command: object, cwd: Path, check=None) -> dict:
    """`check` is injected for tests -- avoids a live `gh api` call from
    ever reaching a unit test, mirroring `gate_ci.py`'s own `run=` pattern.
    """
    check = check or gate_ci.check_gate_result
    if not isinstance(command, str):
        return denial(
            "Blocked: malformed Bash hook payload; refusing to bypass the "
            "gate-CI-coverage guard (harmonic-forge#565)."
        )
    try:
        segments = command_segments(command)
    except (AttributeError, TypeError, ValueError):
        return denial(
            "Blocked: malformed shell command; refusing to bypass the "
            "gate-CI-coverage guard (harmonic-forge#565)."
        )
    for segment in segments:
        found = find_body_and_repo(segment, cwd)
        if found is None:
            continue
        body, repo = found
        if not gate_ci.looks_like_a_gate_report(body):
            continue
        ok, message = check(repo, body)
        if not ok:
            return denial(
                f"{message}\n\n(harmonic-forge#565: this check now applies "
                "to every route a gate report can reach an issue by, not "
                "only `post_lane_discussion.py`.)"
            )
    return {}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("{}")
        return
    if payload.get("tool_name") != "Bash":
        print("{}")
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    cwd = Path(payload.get("cwd") or Path.cwd())
    print(json.dumps(decision(command, cwd)))


if __name__ == "__main__":
    main()
