#!/usr/bin/env python3
"""Deny a mutating `gh`/wrapper-script command from the 4 advisory
subagents (`pitch-inspection`, `preclose-inspection`, `product-strategy`,
`sticky-wicket`) — harmonic-forge#237, `preclose-inspection` added by
vitalharmony/hrse#1208.

`preclose-inspection` matters most here: it reviews a finished diff on an
issue that is about to be closed, so it is the one advisory agent sitting
closest to a `gh issue close`. Its prose says it takes no closing action;
this is what makes that true.

Each of those agent files already states "never mutate" in prose, but
prose is read (in principle) by the model itself, with nothing blocking a
violation if the model doesn't follow it. This is the real technical
enforcement, wired via each agent's `hooks:` frontmatter (subagent
frontmatter hooks fire only while that specific subagent is active,
confirmed live against the installed Claude Code build — see this issue's
handoff for the full citation).

**Allowlist, not denylist** — deny by default for any `gh`/`gh-as`/`mise`/
`python3 …/tools/gh/*.py`-rooted command, permit only a named, narrow read
set. A denylist was tried first and found to have real, live-verified
bypasses (`gh issue close`/`gh pr merge` are non-`gh api` GraphQL
mutations a denylist scoped to `gh api` never sees; `--input` flips the
method to POST with no `-f`/`-F`/`-X` present; `-XPOST` evades a
two-token match; `-F query=@file` evades in-string `mutation` detection;
this repo's own documented wrapper scripts are invisible to any
`gh`-shaped check entirely). An allowlist fails safe against unenumerated
bypasses; a denylist fails open against them.

**Fails closed** on any parse/classification failure — this is a
security gate (block a plausible mutation on ambiguity), unlike
`deny_lane3_ae_self_post.py`'s courtesy-reminder context, where fail-open
on a stdin parse hiccup is the lower-cost mistake. That file's own
`main()`'s `print("{}")` -on-malformed-JSON behavior is deliberately NOT
reused here.

**Known residual gap, named explicitly, not silently assumed away**:
project-level subagent frontmatter hooks require workspace-trust
acceptance for the folder containing the agent file; the untrusted case
fails OPEN (the subagent still runs, hooks silently skipped). This is why
the 3 agents are installed as **user-level** symlinks
(`~/.claude/agents/*.md` → `harmonic-forge/agents/*.md`, see
`install_advisory_subagent_symlinks.sh`) rather than only the existing
project-level ones — `userSettings`-sourced subagent hooks are
unconditionally permitted regardless of a given project's trust state
(verified live against the installed build, cited in this issue's
handoff), making this hook trust-exempt by construction. The project-level
symlinks (e.g. `HRSE2/.claude/agents/*.md`) stay in place for discovery
but enforcement no longer depends on them.

**A graphql-endpoint reconciliation, not in the handoff's literal text**:
the Implementation Spec's rule (b) ("permit only if explicit `-X GET` or
no `-f`/`-F` present") and rule (c) ("for a `graphql` endpoint, every
`query`-keyed `-f`/`-F` value must be a literal `query`-leading string")
read as a strict AND if taken word-for-word — but a legitimate
`gh api graphql -f query='query{...}'` read call (explicitly listed as a
PERMIT case in this issue's own Test Cases) has a field present and never
carries `-X GET` (GraphQL always POSTs at the HTTP level regardless of
read/write; the query/mutation distinction lives in the document, not the
verb). Taking (b) and (c) as a strict AND would deny the handoff's own
worked permit example. Resolved by treating (c) as the graphql-endpoint's
own substitute for (b), not an additional layer on top of a failing (b):
for a `graphql` endpoint, `-X`/`--method` is ignored (it's meaningless
for that endpoint) and only (a) + (c) gate the call; for every other REST
endpoint, (a) + (b) gate it as literally stated, with (b) tightened to
also deny an *explicit* non-GET method even when no fields are present
(the handoff's own deny-case list includes bare `gh api -XPOST ...`,
which "no fields present" alone would otherwise wrongly permit).
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shell_parse import command_segments  # noqa: E402  (harmonic-forge#167)

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_SHELL_ESCAPE_COMMANDS = {"bash", "sh", "eval", "xargs"}

# Known in-repo mutation wrapper scripts/tasks — invisible to any
# `gh`-shaped check, since they're Python/mise entry points, not `gh`
# itself. Matched by basename (any invocation form: direct, `python3
# path/to/script.py`, etc.) or by exact `mise run <task>` shape.
_MUTATION_WRAPPER_BASENAMES = {"post_comment.py", "gh_issue.py"}
_MUTATION_MISE_TASKS = {"l1-comment", "post-comment", "gh-new-issue", "lane-comment"}

_READ_METHOD_TOKENS = {"-x", "--method"}

# Named read shapes permitted for the classic (non-`api`) gh subcommands.
# Keyed by (subcommand, sub-subcommand); a bare "*" sub-subcommand means
# any sub-subcommand under that top-level command is permitted (used only
# for `search`, which has no mutating form at all).
_PERMITTED_SUBCOMMANDS = {
    ("issue", "view"), ("issue", "list"),
    ("pr", "view"), ("pr", "list"), ("pr", "diff"), ("pr", "checks"),
    ("repo", "view"),
    ("label", "list"),
    ("release", "view"), ("release", "list"),
}


def _resolve_wrapper_prefix(tokens: list[str]) -> list[str]:
    """Strip leading VAR=value assignments, a leading bare `env`, and a
    leading `gh-as <account>` pair, looping until stable so a command like
    `FOO=bar gh-as work gh issue view 1` resolves fully."""
    changed = True
    while changed and tokens:
        changed = False
        while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
            tokens = tokens[1:]
            changed = True
        if tokens and tokens[0] == "env":
            tokens = tokens[1:]
            changed = True
        if tokens and tokens[0] == "gh-as":
            tokens = tokens[2:] if len(tokens) >= 2 else []
            changed = True
    return tokens


def _matches_mutation_wrapper(tokens: list[str]) -> bool:
    if len(tokens) >= 3 and tokens[0] == "mise" and tokens[1] == "run" and tokens[2] in _MUTATION_MISE_TASKS:
        return True
    for token in tokens:
        if Path(token).name in _MUTATION_WRAPPER_BASENAMES:
            return True
    return False


def _find_explicit_method(args: list[str]) -> str | None:
    for index, token in enumerate(args):
        lowered = token.lower()
        if lowered in _READ_METHOD_TOKENS:
            if index + 1 < len(args):
                return args[index + 1].upper()
            return None
        if lowered.startswith("--method="):
            return token.partition("=")[2].upper()
        if lowered.startswith("-x") and len(token) > 2:
            return token[2:].upper()
    return None


def _has_input_flag(args: list[str]) -> bool:
    return any(token == "--input" or token.startswith("--input=") for token in args)


def _has_field_flag(args: list[str]) -> bool:
    for token in args:
        if token in ("-f", "-F", "--field", "--raw-field"):
            return True
        if (token.startswith("-f") or token.startswith("-F")) and len(token) > 2 and "=" in token:
            return True
        if token.startswith("--field=") or token.startswith("--raw-field="):
            return True
    return False


def _extract_field_values(args: list[str], key_name: str) -> list[str]:
    """Every raw value string for `-f`/`-F`/`--field`/`--raw-field
    KEY=VALUE` pairs whose key matches key_name — spaced and concatenated
    forms both handled."""
    values: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        kv: str | None = None
        if token in ("-f", "-F", "--field", "--raw-field"):
            if index + 1 < len(args):
                kv = args[index + 1]
                index += 1
        elif (token.startswith("-f") or token.startswith("-F")) and len(token) > 2 and "=" in token:
            kv = token[2:]
        elif token.startswith("--field="):
            kv = token.partition("=")[2]
        elif token.startswith("--raw-field="):
            kv = token.partition("=")[2]
        if kv is not None and "=" in kv:
            key, _, value = kv.partition("=")
            if key == key_name:
                values.append(value)
        index += 1
    return values


def _gh_api_permitted(args: list[str]) -> bool:
    if _has_input_flag(args):
        return False

    endpoint = next((token for token in args if not token.startswith("-")), None)

    if endpoint == "graphql":
        # GraphQL always POSTs at the HTTP level regardless of read/write —
        # -X/--method is meaningless for it and ignored here. Only the
        # query-document content gates the call. See module docstring's
        # "graphql-endpoint reconciliation" note for why this doesn't also
        # require rule (b)'s explicit-GET-or-no-fields test.
        query_values = _extract_field_values(args, "query")
        if not query_values:
            return True  # no query document at all — nothing to inspect, vacuously safe
        for value in query_values:
            if value.startswith("@"):
                return False  # file/stdin-sourced document — cannot inspect contents
            if not value.strip().lower().startswith("query"):
                return False
        return True

    method = _find_explicit_method(args)
    if method is not None:
        return method == "GET"
    return not _has_field_flag(args)


def _gh_permitted(args: list[str]) -> bool:
    if len(args) >= 1 and args[0] == "api":
        return _gh_api_permitted(args[1:])
    if len(args) >= 1 and args[0] == "search":
        return True
    if len(args) >= 2 and (args[0], args[1]) in _PERMITTED_SUBCOMMANDS:
        return True
    return False


def _classify(tokens: list[str]) -> bool:
    """Return True if this resolved command segment is permitted."""
    resolved = _resolve_wrapper_prefix(tokens)
    if not resolved:
        return True  # empty segment (e.g. trailing pipe artifact) — nothing to deny

    head = Path(resolved[0]).name
    if head in _SHELL_ESCAPE_COMMANDS:
        return False
    if _matches_mutation_wrapper(resolved):
        return False
    if head != "gh":
        return True  # out of scope — this hook only governs gh-mutation risk
    return _gh_permitted(resolved[1:])


def denial(command_text: str) -> dict:
    message = (
        "Blocked: this advisory subagent may not run a command that could "
        f"mutate GitHub state (harmonic-forge#237): `{command_text}`. "
        "Advisory subagents (pitch-inspection/preclose-inspection/"
        "product-strategy/sticky-wicket) "
        "are read-only by design — hand any needed write back to the calling "
        "session rather than running it here."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        },
        "systemMessage": message,
    }


def decision(command: object) -> dict:
    if not isinstance(command, str):
        return denial("<non-string command payload>")
    try:
        segments = command_segments(command)
    except (AttributeError, TypeError, ValueError):
        return denial(command if isinstance(command, str) else "<unparseable>")
    for segment in segments:
        try:
            permitted = _classify(segment)
        except Exception:
            return denial(" ".join(segment) if segment else command)
        if not permitted:
            return denial(" ".join(segment))
    return {}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps(denial("<malformed hook payload>")))
        return
    if payload.get("tool_name") != "Bash":
        print("{}")
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    print(json.dumps(decision(command)))


if __name__ == "__main__":
    main()
