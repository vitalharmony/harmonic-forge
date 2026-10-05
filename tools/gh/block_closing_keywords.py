#!/usr/bin/env python3
"""PreToolUse hook: block GitHub closing keywords in PR bodies/comments.

Extends harmonic-forge#85's gh-issue-close permission block to the
closing *intent*, not just that one command surface. #85 blocks
`gh issue close`/`gh issue reopen` directly; it does not and cannot
catch a `Closes #N`-style keyword written into a PR body or comment,
which delegates the same closing action to GitHub's own merge-triggered
automation. See harmonic-forge#93 for the incident record (6th instance
of the closed-without-authorization pattern, this one via a new
mechanism).

Only fires on `gh pr create`/`edit`/`merge`/`comment`, `gh issue
comment`/`edit`, and a `gh api` call that writes (an explicit
POST/PATCH/PUT method, or `-f`/`-F` fields, which make `gh api` POST), and
only on the text of the command itself. Within that text it denies the `#N`
and the issue-URL reference forms, and a colon after the keyword, which is
matched defensively rather than as documented GitHub behavior.
Non-closing references (Implements/Part of/Refs #N) are unaffected.

**Best-effort, fail-open on everything it cannot see** (harmonic-forge#911
preclose). It reads no file, so a body supplied by `--body-file`,
`-F body=@file` or `--input` passes; it sees no commit message, heredoc
body, `gh api graphql` mutation, web-UI edit, or anything run outside a
Bash tool call. Those routes are covered, if at all, by review and by the
close-time hooks, not here.

## No BATCH exception (harmonic-forge#911)

harmonic-forge#612 once allowed a `Closes #N` while a live BATCH grant
covered that issue's merge, so sequenced issues could close on merge. The
operator retired that exception (harmonic-forge#911, 2026-10-05): on the
surfaces above, a closing keyword is denied whatever the BATCH state. A batched issue
closes by an explicit close command after its merge, which keeps every close
a deliberate, separately visible action. BATCH itself grants merges only
(`batch_auth.py`'s `DEFAULT_ACTIONS`).
"""
import json
import re
import sys

CLOSING_KEYWORD = re.compile(
    r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r":?\s+(?P<repo>[\w.-]+/[\w.-]+)?#(?P<number>\d+)"
)

#: The URL form GitHub also honors. Mirrors
#: `tools/hooks/block_undetermined_phase_close.py`'s `CLOSING_KEYWORD_URL`;
#: kept separate so `CLOSING_KEYWORD` stays byte-identical to that file's copy.
CLOSING_KEYWORD_URL = re.compile(
    r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r":?\s+https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/issues/(?P<number>\d+)"
)

RELEVANT_COMMAND = re.compile(
    r"(?i)\bgh\s+(pr\s+(create|edit|merge|comment)|issue\s+(comment|edit)|"
    r"api\b(?=.*(?:-X\s*|--method[\s=]+)(?:POST|PATCH|PUT)\b|.*\s-[fF]\s))"
)

def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({}))
        return

    if data.get("tool_name") != "Bash":
        print(json.dumps({}))
        return

    command = (data.get("tool_input") or {}).get("command", "")
    if not RELEVANT_COMMAND.search(command):
        print(json.dumps({}))
        return

    matches = [*CLOSING_KEYWORD.finditer(command), *CLOSING_KEYWORD_URL.finditer(command)]
    if not matches:
        print(json.dumps({}))
        return

    unauthorized = [match.group(0) for match in matches]

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
        },
        "systemMessage": (
            f"Blocked: this command's body contains a GitHub closing "
            f"keyword ({unauthorized[0]!r}), which would auto-close the "
            f"referenced issue on merge without explicit human "
            f"authorization (harmonic-forge#84/#93). Use a non-closing "
            f"reference instead — \"Implements #N\" or \"Part of #N\" — "
            f"and close the issue explicitly, separately, only when told to. "
            f"BATCH does not change this (harmonic-forge#911)."
        ),
    }))


if __name__ == "__main__":
    main()
