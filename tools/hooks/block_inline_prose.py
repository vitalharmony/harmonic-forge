#!/usr/bin/env python3
"""PreToolUse hook: deny multi-line prose passed through a bash string literal
(harmonic-forge#266).

Prose belongs in a file. Every layer between an agent and a commit message or
PR body is a chance to corrupt it, and the corruption is usually SILENT:

1. Command substitution. `git commit -m "... `mise run hygiene` ..."` executes
   the backticked command and substitutes its output into the message. Observed
   live 2026-08-12: 36 lines of branch listing ended up inside a commit message,
   and would have been permanent in the git record if unnoticed.
2. Dropped apostrophes. Bodies built with `printf '...'` cannot contain a single
   quote, so they get removed -- "the doc's own rule" silently becomes "the doc
   own rule". Nothing errors; the writing is just worse.
3. `%` mangling. printf treats `%` as a format specifier, so every literal
   percent needs doubling. Percentages are constant in measurement writeups.
4. Heredoc collisions with the delimiter or with the auto-close-keyword guard.

This is deliberately a hook rather than a rule. The correct pattern
(`--body-file`, `-F`) is already known and already used most of the time; it
fails exactly when the shortcut is taken, which is what a rule cannot prevent
and a mechanical gate can. Same reasoning the sprint-plan skill records for its
own narrative check: "prose reminders have repeatedly failed to hold in
practice -- this is a mechanical, scriptable check instead."

Scope is intentionally narrow. Single-line, metacharacter-free `-m` messages
stay allowed, because making ordinary commits annoying is how a guard gets
disabled.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shell_parse import command_segments, mask_heredoc_bodies  # noqa: E402

# Backtick or $( ) inside a double-quoted argument is the dangerous case: bash
# evaluates it. Single-quoted arguments are literal, so they are only a prose
# QUALITY problem (dropped apostrophes), not an execution one -- length is what
# catches those.
SUBSTITUTION = re.compile(r"[`]|\$\(")


def denial(message: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        },
        "systemMessage": message,
    }


def _flag_value(segment: list[str], *names: str) -> str | None:
    """Return the value of the first matching --flag / --flag=value present."""
    for index, token in enumerate(segment):
        if token in names and index + 1 < len(segment):
            return segment[index + 1]
        for name in names:
            if token.startswith(f"{name}="):
                return token.partition("=")[2]
    return None


def _has_flag(segment: list[str], *names: str) -> bool:
    return any(t == n or t.startswith(f"{n}=") for t in segment for n in names)


def inline_prose_problem(segment: list[str]) -> str | None:
    """Return a reason string if this segment inlines prose that belongs in a file."""
    if not segment:
        return None

    # git commit -m
    if segment[0] == "git" and "commit" in segment[:3]:
        message = _flag_value(segment, "-m", "--message")
        if message is not None:
            if SUBSTITUTION.search(message):
                return (
                    "this `git commit -m` message contains a backtick or $( ), which bash "
                    "evaluates as a command and substitutes into the message. Write the "
                    "message to a file and use `git commit -F <file>`, or a quoted heredoc "
                    "(`git commit -F - <<'EOF'`)."
                )
            if "\n" in message:
                return (
                    "this `git commit -m` message is multi-line. Use `git commit -F <file>` "
                    "or a quoted heredoc (`git commit -F - <<'EOF'`) so quoting cannot "
                    "corrupt it."
                )
        return None

    # gh pr create/edit --body
    if segment[:2] == ["gh", "pr"] and len(segment) > 2 and segment[2] in {"create", "edit"}:
        if _has_flag(segment, "--body", "-b") and not _has_flag(segment, "--body-file", "-F"):
            body = _flag_value(segment, "--body", "-b") or ""
            if "\n" in body or SUBSTITUTION.search(body) or len(body) > 300:
                return (
                    f"`gh pr {segment[2]} --body` is carrying a long or metacharacter-bearing "
                    "body. Write it to a file and use `--body-file <path>` -- backticks are "
                    "executed, `%` is a printf specifier, and single-quoted strings silently "
                    "drop apostrophes."
                )
    return None


def decision(command: object) -> dict:
    if not isinstance(command, str) or not command.strip():
        return {}
    # An escape hatch, deliberately explicit and greppable in transcripts.
    if os.environ.get("ALLOW_INLINE_PROSE") == "1":
        return {}
    try:
        segments = command_segments(mask_heredoc_bodies(command))
    except Exception:
        # Fail OPEN on a parse failure. This guard protects prose quality, not
        # safety -- wedging every Bash call over an unparseable command would be
        # a far worse failure than letting one ugly commit message through.
        return {}
    for segment in segments:
        reason = inline_prose_problem(segment)
        if reason:
            return denial(
                f"Blocked (harmonic-forge#266): {reason}\n\n"
                "Set ALLOW_INLINE_PROSE=1 to override if this is genuinely a one-liner."
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
    print(json.dumps(decision(command)))


if __name__ == "__main__":
    main()
