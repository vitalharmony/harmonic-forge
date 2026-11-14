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
`gh run watch`, `preclose_check.py --complete`, `pytest`, and an `l1-post` /
`l1_post.py` dry run (harmonic-forge#1362).
Write: `mise run commit`, `git commit`, `git push`, `gh pr merge`,
`mise run restart` without `--no-git`, and a real `l1-post` / `l1_post.py` post.

An `l1-post` dry run gates every listed write, not only its own post:
`l1-post … --dry-run | tail -2 && l1-post …` let a failed dry run through,
because the pipe returns `tail`'s exit status (harmonic-forge#1362). "Dry run"
is any word that `l1_post.py`'s parser accepts as `--dry-run`, abbreviations
included (`--dr`, `--dry`, `--dry-r`).

`preclose_check.py --complete && gh pr merge` is denied on purpose: preclose is
a gate and its result is read before the merge.

Order matters: the deny fires only when a gate runs BEFORE a write in execution
order. `git push && gh pr checks 12 --watch` (push, then watch CI) is allowed.
The string argument of `bash -c` / `sh -c` / `zsh -c` and of `eval`, anywhere in
a command (after `env`, `uv run`, ...), is parsed again like a `$(...)` body.

Classification is by word presence, not position (sticky-wicket pass-2 REFORGE):
wrappers (`env`, `exec`, `sudo`, `uv run`, interpreters and their flags,
assignments, leading numbers) are skipped only to tell whether the program is
`git`, `gh` or `mise`; everything else is matched by which words it holds. A
quoted argument stays one word, so a commit message or comment body is never a
gate or write.

Known residual (operator ruling 2026-11-12, option A): a here-doc body fed to a
shell (`bash <<X ... X`) is NOT parsed, so a gate and a write inside one are not
seen.

Known residual (sticky-wicket PATCH, issuecomment-6841112555): wrapper value flags
are a fixed list (`_VALUE_FLAGS`). An unlisted value-taking wrapper flag makes its
value read as the program, so that command's gate is not seen (fails open).

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


#: Words that wrap the real program (sticky-wicket pass-2 REFORGE): they, any
#: `NAME=value` assignment, any flag and any bare number before the program are
#: skipped to find which program runs. Interpreters are wrappers too, so
#: `python3 -I -m pytest` is seen through.
_WRAPPERS = frozenset({"env", "exec", "sudo", "command", "nohup", "nice", "time",
                       "timeout", "uv", "run", "--", "python", "python3"})


#: Wrapper flags whose next word is their value, never the program
#: (`python3 -X dev`, `uv run --with x`, `env -C /x`, `mise -C x`).
_VALUE_FLAGS = frozenset({"-X", "-W", "--with", "--from", "--python", "-C", "--cd",
                          "-u", "--chdir",
                          # uv run's value flags (sticky-wicket PATCH, issuecomment-6841112555)
                          "--project", "--directory", "--package", "--env-file", "--extra",
                          "--group", "--index", "--default-index", "-p",
                          "--with-requirements", "--with-editable"})


def _words(argv: list[str]) -> list[str]:
    return [_base(w) for w in argv]


#: `l1_post.py` builds its parser with argparse's default `allow_abbrev`, so any
#: prefix of `--dry-run` at least this long is the dry-run flag (harmonic-forge#1362).
_DRY_RUN = "--dry-run"
_DRY_RUN_MIN = len("--dr")


def _has_dry_run(argv: list[str]) -> bool:
    """Over the RAW argv words, never their basenames: `--file /tmp/--dry-run`
    is a path, not the flag (preclose pass 1, survivor 3)."""
    return any(len(w) >= _DRY_RUN_MIN and _DRY_RUN.startswith(w) for w in argv)


def _is_l1_post(argv: list[str], program: Optional[str]) -> bool:
    """`mise run l1-post …` or `[python3] …/l1_post.py …`."""
    if program == "mise":
        return _mise_task(argv) == "l1-post"
    return program == "l1_post.py"


def _program(argv: list[str]) -> Optional[str]:
    """The program a simple command runs, past wrappers, assignments, flags and numbers.
    Only `git`, `gh` and `mise` change how the words are read, so a wrong guess for
    any other program changes nothing: those are matched by word presence."""
    skip = False
    for word in _words(argv):
        if skip:
            skip = False
            continue
        if word in _VALUE_FLAGS:
            skip = True
            continue
        if (word in _WRAPPERS or word.startswith("python") or word.startswith("-")
                or word.isdigit() or ("=" in word and not word.startswith("-"))):
            continue
        return word
    return None


def gate_of(argv: list[str]) -> Optional[str]:
    """The gate this simple command runs, as display text, or None. Classified by which
    words the command holds, not where they sit (sticky-wicket pass-2 REFORGE): a quoted
    argument is one word, so `git commit -m "make pytest pass"` holds no `pytest` word."""
    words = set(_words(argv))
    program = _program(argv)
    if program == "git":
        return None
    if program == "gh":
        path = _gh_path(argv)
        if path == ["pr", "checks"]:
            return "gh pr checks"
        if path in (["run", "view"], ["run", "watch"]):
            return "gh run " + path[1]
        return None
    if program == "mise":
        rest = _after_mise_exec(argv)
        if rest is not None:
            return gate_of(rest)
        task = _mise_task(argv)
        if task in ("check-full", "check"):
            return f"mise run {task}"
        if task == "l1-post" and _has_dry_run(argv):
            return "mise run l1-post --dry-run"
        if task == "preclose-check" and "--complete" in words:
            return "mise run preclose-check --complete"
        return None
    # The gate is the PROGRAM, never a data word (sticky-wicket PATCH,
    # issuecomment-6840890733): `echo pytest` and `pip show pytest` are not gates.
    if program in ("pytest", "py.test"):
        return "pytest"
    if program == "preclose_check.py" and "--complete" in words:
        return "preclose_check.py --complete"
    if program == "l1_post.py" and _has_dry_run(argv):
        return "l1_post.py --dry-run"
    return None


#: Global flags that take a value, skipped when reading a program's subcommand path.
_GH_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})
_GIT_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                              "--config-env", "--super-prefix"})


def _subcommand(argv: list[str], program: str, value_flags: frozenset[str],
                count: int) -> list[str]:
    """The first `count` non-flag words after `program`, skipping each value flag's
    value. A data word later in argv (`git log --grep push`) is never the subcommand
    (sticky-wicket PATCHes, issuecomment-6840890733 and issuecomment-6841112555)."""
    words = _words(argv)
    if program not in words:
        return []
    path: list[str] = []
    skip = False
    for word in words[words.index(program) + 1:]:
        if skip:
            skip = False
            continue
        if word in value_flags:
            skip = True
            continue
        if word.startswith("-"):
            continue
        path.append(word)
        if len(path) == count:
            break
    return path


def _gh_path(argv: list[str]) -> list[str]:
    """`gh pr view 12 --json checks` is `["pr", "view"]`."""
    return _subcommand(argv, "gh", _GH_VALUE_FLAGS, 2)


def _mise_task(argv: list[str]) -> Optional[str]:
    """The task `mise run <task>` / `mise r <task>` runs, or None."""
    path = _subcommand(argv, "mise", _VALUE_FLAGS, 2)
    return path[1] if len(path) == 2 and path[0] in ("run", "r") else None


def _after_mise_exec(argv: list[str]) -> Optional[list[str]]:
    """The command `mise exec`/`mise x` runs (the words after `--`, else after the
    verb), or None when this is not a mise exec."""
    words = _words(argv)
    if "mise" not in words:
        return None
    skip = False
    for index in range(words.index("mise") + 1, len(words)):
        word = words[index]
        if skip:
            skip = False
            continue
        if word in _VALUE_FLAGS:
            skip = True
            continue
        if word.startswith("-"):
            continue
        if word not in ("exec", "x"):
            return None
        rest = argv[index + 1:]
        return rest[rest.index("--") + 1:] if "--" in rest else rest
    return None


def write_of(argv: list[str]) -> Optional[str]:
    """The gated write this simple command runs, as display text, or None. Classified
    by word presence after the program (any git or mise global flag in between)."""
    words = set(_words(argv))
    program = _program(argv)
    if program == "git":
        verb = _subcommand(argv, "git", _GIT_VALUE_FLAGS, 1)
        return f"git {verb[0]}" if verb and verb[0] in ("commit", "push") else None
    if program == "gh":
        return "gh pr merge" if _gh_path(argv) == ["pr", "merge"] else None
    if program == "mise":
        task = _mise_task(argv)
        if task == "commit":
            return "mise run commit"
        if task == "restart" and "--no-git" not in words:
            return "mise run restart"
    if _is_l1_post(argv, program) and not _has_dry_run(argv):
        return "mise run l1-post" if program == "mise" else "l1_post.py"
    return None


#: Shells whose `-c` string argument is a command line.
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})


def _script_arg(argv: list[str]) -> Optional[str]:
    """The command string a shell `-c` (or combined `-lc`, `-ec`…) or `eval` runs."""
    for start, word in enumerate(argv):
        head = _base(word)
        if head == "eval":
            return " ".join(argv[start + 1:]) or None
        if head in _SHELLS:
            for index in range(start + 1, len(argv)):
                flag = argv[index]
                if flag.startswith("-") and not flag.startswith("--") and "c" in flag[1:]:
                    return argv[index + 1] if index + 1 < len(argv) else None
            return None
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
        yield shell_parse.strip_invocation_prefix(item.argv)


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
