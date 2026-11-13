#!/usr/bin/env python3
"""PreToolUse(Bash) hook: a gate and the write it gates never share one command (R-0419,
harmonic-forge#1337).

When a check and its write run in one Bash call, the check's result prints but
nothing reads it before the write runs: PR 1322 merged red that way
(protocol-failures.jsonl 281), and a commit landed on a failed check (287).
This hook denies any one command that holds both a gate segment and a write
segment, and names both, so the session runs the gate alone, reads it, then
writes.

Matching is on argv words after `shell_parse.strip_invocation_prefix`, never a
text search, so a gate or write word inside a quoted argument (a commit
message, a comment body, a `--grep` pattern) is not a segment. Every `$(...)`
and backtick body in a word is parsed again, so `x="$(pytest -q)"; git push` is
seen. A command that does not parse is allowed: this hook adds a deny and
never fails closed (good-faith threat model; it mints, relaxes or bypasses no
R-0371 authorization).

Gate: `mise run check` / `check-full`, `gh pr checks`, `gh run view` /
`gh run watch`, `preclose_check.py --complete`, `pytest`.
Write: `mise run commit`, `git commit`, `git push`, `gh pr merge`,
`mise run restart` without `--no-git`.

`preclose_check.py --complete && gh pr merge` is denied on purpose: preclose is
a gate and its result is read before the merge.

Order matters: the deny fires only when a gate runs BEFORE a write in execution
order. `git push && gh pr checks 12 --watch` (push, then watch CI) is allowed.
The string argument of `bash -c` / `sh -c` / `zsh -c` and of `eval` is parsed
again like a `$(...)` body; `uv run <cmd>` and mise's `r` alias and global
flags (`-C`, `--cd`, `-E`, `--env`, `-q`, `-v`, `-y`) are seen through.

Known residual (operator ruling 2026-11-12, option A): a here-doc body fed to a
shell (`bash <<X ... X`) is NOT parsed, so a gate and a write inside one are not
seen.

The decision goes to stderr (harmonic-forge#1291's decision log is not on main).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import shell_parse  # noqa: E402

#: How deep `$(...)` bodies are re-parsed; deeper nesting is not read.
MAX_DEPTH = 4


def _base(word: str) -> str:
    return os.path.basename(word)


#: mise global flags that take a value, and that do not.
_MISE_VALUE_FLAGS = frozenset({"-C", "--cd", "-E", "--env"})
_MISE_BARE_FLAGS = frozenset({"-q", "--quiet", "-v", "--verbose", "-y", "--yes"})


def _mise_task(argv: list[str]) -> Optional[str]:
    """The task of `mise [global flags] run|r <task>`, or None."""
    if not argv or _base(argv[0]) != "mise":
        return None
    index = 1
    while index < len(argv):
        word = argv[index]
        if word in _MISE_VALUE_FLAGS:
            index += 2
        elif word in _MISE_BARE_FLAGS or word.startswith(("--cd=", "--env=")):
            index += 1
        else:
            break
    if index + 1 < len(argv) and argv[index] in ("run", "r"):
        return argv[index + 1]
    return None


def _unwrap_runner(argv: list[str]) -> list[str]:
    """`uv run [--] <cmd>` is a prefix: return `<cmd>`."""
    if len(argv) >= 3 and _base(argv[0]) == "uv" and argv[1] == "run":
        rest = argv[2:]
        return rest[1:] if rest and rest[0] == "--" else rest
    return argv


def _gh_sub(argv: list[str]) -> tuple[str, str] | None:
    """`gh <group> <verb>`, skipping value flags before or between them."""
    if not argv or _base(argv[0]) != "gh":
        return None
    words: list[str] = []
    skip = False
    for token in argv[1:]:
        if skip:
            skip = False
            continue
        if token in ("-R", "--repo", "--hostname"):
            skip = True
            continue
        if token.startswith("-"):
            continue
        words.append(token)
        if len(words) == 2:
            return words[0], words[1]
    return None


def gate_of(argv: list[str]) -> Optional[str]:
    """The gate this simple command runs, as display text, or None."""
    if not argv:
        return None
    task = _mise_task(argv)
    if task in ("check", "check-full"):
        return f"mise run {task}"
    sub = _gh_sub(argv)
    if sub == ("pr", "checks"):
        return "gh pr checks"
    if sub in (("run", "view"), ("run", "watch")):
        return f"gh run {sub[1]}"
    head = _base(argv[0])
    if head == "pytest" or (head.startswith("python") and argv[1:3] == ["-m", "pytest"]):
        return "pytest"
    if any(_base(w) == "preclose_check.py" for w in argv[:2]) and "--complete" in argv:
        return "preclose_check.py --complete"
    return None


def write_of(argv: list[str]) -> Optional[str]:
    """The gated write this simple command runs, as display text, or None."""
    if not argv:
        return None
    task = _mise_task(argv)
    if task == "commit":
        return "mise run commit"
    if task == "restart" and "--no-git" not in argv:
        return "mise run restart"
    if _base(argv[0]) == "git":
        # `git -C dir commit`: -C/-c take a value.
        rest = argv[1:]
        index = 0
        while index < len(rest) and rest[index].startswith("-"):
            index += 2 if rest[index] in ("-C", "-c") else 1
        verb = rest[index] if index < len(rest) else None
        if verb in ("commit", "push"):
            return f"git {verb}"
    if _gh_sub(argv) == ("pr", "merge"):
        return "gh pr merge"
    return None


#: Shells whose `-c` string argument is a command line.
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})


def _script_arg(argv: list[str]) -> Optional[str]:
    """The command string a shell `-c` (or combined `-lc`, `-ec`…) or `eval` runs."""
    if not argv:
        return None
    head = _base(argv[0])
    if head == "eval":
        return " ".join(argv[1:]) or None
    if head in _SHELLS:
        for index, word in enumerate(argv[1:], start=1):
            if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
                return argv[index + 1] if index + 1 < len(argv) else None
    return None


def _argvs(command: str, depth: int = 0) -> Iterator[list[str]]:
    """Every simple command of `command`, in execution order: a word's `$(...)` bodies
    run before the command holding them, and a `-c`/`eval` string runs as that command."""
    for item in shell_parse.simple_commands(command):
        if not isinstance(item, shell_parse.SimpleCommand):
            continue
        if depth < MAX_DEPTH:
            for word in item.argv:
                for body in shell_parse.substitution_bodies(word):
                    yield from _argvs(body, depth + 1)
            script = _script_arg(item.argv)
            if script is not None:
                yield from _argvs(script, depth + 1)
                continue
        yield _unwrap_runner(shell_parse.strip_invocation_prefix(item.argv))


def find(command: str) -> tuple[Optional[str], Optional[str]]:
    """(gate, write): the first gate, and the first write that runs AFTER a gate, in
    execution order. A write that runs before every gate is not this slip. Raises on a
    parse failure."""
    gate = write = None
    for argv in _argvs(command):
        if gate and not write:
            write = write_of(argv)
        gate = gate or gate_of(argv)
    return gate, write


def decide(command: str) -> Optional[str]:
    """The deny reason, or None to allow."""
    try:
        gate, write = find(command)
    except (ValueError, shell_parse.NotOrdinary):
        return None
    if gate and write:
        return (f"Blocked (R-0419, harmonic-forge#1337): this one command runs the gate "
                f"`{gate}` and the write `{write}` it gates, so the write would run before "
                f"anyone reads the gate's result. Run `{gate}` alone, read its result, "
                f"then run `{write}` in a separate call.")
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({}))
        return
    if data.get("tool_name") != "Bash":
        print(json.dumps({}))
        return
    command = (data.get("tool_input") or {}).get("command", "") or ""
    reason = decide(command)
    if reason is None:
        print(json.dumps({}))
        return
    print(f"block_gate_and_write_same_command: deny: {reason}", file=sys.stderr)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
    }))


if __name__ == "__main__":
    main()
