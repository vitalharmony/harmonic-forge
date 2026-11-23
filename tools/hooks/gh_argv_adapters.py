"""One classification of a gh close or merge call, over `gh_argv` (harmonic-forge#1496).

`gh_argv` is the pflag-faithful parser behind the real-argv shim. Three more
readers (`batch_auth`, `block_data_migration_close`,
`block_missing_preclose_inspection`) still keep private target and flag
recognizers. This module is the shared wrapper they adopt later (F1497 to
F1499); it changes no consumer and makes no allow or deny decision of its own.

`classify` reports one of six outcomes, so each consumer can map them to its own
policy (BATCH's conservative ask, the text hooks' fail-open, the shim's
fail-closed) without re-deriving grammar:

    bound       a close or merge with a target the gate can name
    unbound     a close or merge whose target cannot be named (no argument, a
                branch name, `$N`, a second argument, a URL on another host)
    unparseable `gh_argv.parse` raised `ArgvError` for the call
    unprovable  a call that cannot be proved not to close or merge: a `gh api`
                call `gh_argv` refuses (GraphQL by node id, an unreadable
                `--input` body, a `{owner}/{repo}` placeholder), a flag before
                the command, an alias or extension, or a help flag on a close or
                merge path
    allowed     a gated root whose call does not close or merge
    not_gated   not `gh`, or a builtin root outside `gh_argv.GATED_ROOTS`

Inline `NAME=value` assignments before `gh` are the command's own environment and
override the one passed in (gh reads `GH_REPO`). `close_target`/`merge_target`
return the result for an unparseable or unprovable call too: None means the call
is provably not that kind.

`unprovable` and `unparseable` are kept apart from `unbound` on purpose: the old
text hooks let both through as "not a close", `gh_argv` refuses both, and an
adopter must choose per consumer rather than inherit the shim's answer.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import gh_argv
from shell_parse import command_segments, strip_invocation_prefix

BOUND = "bound"
UNBOUND = "unbound"
UNPARSEABLE = "unparseable"
UNPROVABLE = "unprovable"
ALLOWED = "allowed"
NOT_GATED = "not_gated"

#: `gh <path>` pairs whose refusal means "the target cannot be named"; any other
#: gated-root refusal is `gh api` (or another path) and is "cannot prove".
_CLI_TARGET_PATHS = (("issue", "close"), ("pr", "merge"))


@dataclass(frozen=True)
class Result:
    outcome: str
    kind: str | None = None  # "issue" or "pr" for bound; None otherwise
    repo: str | None = None
    number: str | None = None
    heads: tuple[str | None, ...] = (None,)
    reason: str = ""

    @property
    def gated(self) -> bool:
        """A close or merge call the gate can see: bound or unbound."""
        return self.outcome in (BOUND, UNBOUND)


#: The house account wrappers `shell_parse.strip_invocation_prefix` unwraps
#: (`gh-as <account> gh ...`, `gha <account> ...`); they set a token, never a repo.
_ACCOUNT_WRAPPERS = ("gh-as", "gha")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def _inline_env(tokens: list[str]) -> dict[str, str]:
    """`NAME=value` assignments before the program (after an optional `env`):
    the command's own environment, which gh reads (`GH_REPO=a/b gh issue close 12`
    closes a/b#12) and which overrides the hook process's."""
    found: dict[str, str] = {}
    for token in tokens:
        if token == "env":
            continue
        match = _ASSIGNMENT.match(token)
        if not match:
            break
        found[match.group(1)] = match.group(2)
    return found


def _plain_prefix(tokens: list[str]) -> bool:
    """True when everything before the first `gh` token is an optional bare
    `env` and `NAME=value` assignments: the one prefix whose environment this
    module models. Anything else (an `env` option, `sudo`, `xargs`, a
    redirection, a shell wrapper) can change what gh runs or where."""
    skip = False
    for token in tokens:
        if skip:  # the account argument of an account wrapper
            skip = False
            continue
        if Path(token).name == "gh":
            return True
        if Path(token).name in _ACCOUNT_WRAPPERS:
            skip = True
            continue
        if token != "env" and not _ASSIGNMENT.match(token):
            return False
    return True


_SHELLS = ("bash", "sh", "zsh", "dash")
_C_FLAG = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
_SEVERITY = (BOUND, UNBOUND, UNPARSEABLE, UNPROVABLE)


def _shell_body(tokens: list[str]) -> tuple[list[str], str] | None:
    """(the tokens before the shell, the `-c` script) for `[prefix] bash -c '...'`,
    else None."""
    for index, token in enumerate(tokens):
        if Path(token).name in _SHELLS:
            for flag in range(index + 1, len(tokens) - 1):
                if _C_FLAG.match(tokens[flag]):
                    return tokens[:index], tokens[flag + 1]
            return None
    return None


def _classify_shell_body(outer: list[str], body: str, env: dict) -> Result:
    """A `-c` script is classified segment by segment; the first close, merge or
    undecided segment is the answer. A bound target behind an outer prefix that
    is not plain (`nohup`, `env -u X`) is unprovable, since that prefix may
    change what the script's gh sees."""
    try:
        segments = command_segments(body)
    except ValueError:
        return Result(UNPROVABLE, reason="a shell -c script that cannot be tokenized")
    plain = all(token == "env" or _ASSIGNMENT.match(token) for token in outer)
    inner_env = dict(env)
    if plain:
        inner_env.update(_inline_env(outer))
    for segment in segments:
        result = classify(segment, inner_env)
        if result.outcome in _SEVERITY:
            if result.outcome == BOUND and not plain:
                return Result(UNPROVABLE, reason="gh inside a shell -c script behind a prefix this module does not model")
            return result
    return Result(NOT_GATED, reason="a shell -c script with no close or merge")


def _mentions_gh(tokens: list[str]) -> bool:
    """A `gh` word among the tokens (not prose that merely contains "gh")."""
    return any(Path(token).name == "gh" for token in tokens)


def classify(tokens: list[str], env: dict | None = None) -> Result:
    """Classify one command segment (the tokens a hook holds after shell splitting).
    `env` is the environment the command inherits (the hook process's when None);
    inline assignments in the segment override it.

    The safe answer is the default: a segment that mentions gh behind anything
    but a plain prefix is unprovable, and only a segment with no gh at all is
    not gated."""
    raw = list(tokens)
    env = dict(os.environ if env is None else env)
    wrapped = _shell_body(raw)
    if wrapped is not None:
        return _classify_shell_body(*wrapped, env)
    env.update(_inline_env(raw))
    tokens = strip_invocation_prefix(raw)
    if not tokens or Path(tokens[0]).name != "gh":
        if _mentions_gh(raw):
            return Result(UNPROVABLE, reason="gh behind a prefix this module does not model")
        return Result(NOT_GATED, reason="not a gh invocation")
    if _mentions_gh(raw) and not _plain_prefix(raw):
        return Result(UNPROVABLE, reason="gh behind a prefix this module does not model")
    argv = tokens[1:]
    if not argv:
        return Result(NOT_GATED, reason="bare gh")
    if argv[0] not in gh_argv.load_table()["builtins"]:
        # An alias or extension the gate does not expand, or a flag before the
        # command (cobra resolves `gh -R o/r issue close 5` to issue close).
        return Result(UNPROVABLE, reason=f"`gh {argv[0]}` is not a builtin command; cannot prove it is not a close")
    if argv[0] not in gh_argv.GATED_ROOTS:
        return Result(NOT_GATED, reason="a gh builtin root outside the gated roots")
    try:
        parsed = gh_argv.parse(argv)
    except gh_argv.ArgvError as exc:
        return Result(UNPARSEABLE, reason=str(exc))
    verdict = gh_argv.decide(argv, env)
    if verdict.action == "allow" and parsed.help and parsed.path in _CLI_TARGET_PATHS:
        # A help call on a close or merge path: a drifted flag table can read a
        # value as `--help`, so this cannot prove the call is not a close.
        return Result(UNPROVABLE, reason="a help flag on a close or merge path")
    if verdict.action == "gate" and verdict.target is not None:
        target = verdict.target
        if target.number:
            return Result(BOUND, kind=target.kind, repo=target.repo, number=target.number,
                          heads=target.heads, reason=verdict.reason)
        # `gh pr merge` with no argument yields a gate verdict with an empty number.
        return Result(UNBOUND, kind=target.kind, repo=target.repo, heads=target.heads,
                      reason="no number the gate can bind")
    if verdict.action == "allow":
        return Result(ALLOWED, reason=verdict.reason)
    if parsed.path in _CLI_TARGET_PATHS:
        return Result(UNBOUND, kind="issue" if parsed.path[0] == "issue" else "pr", reason=verdict.reason)
    return Result(UNPROVABLE, reason=verdict.reason)


def _undecided(result: Result) -> bool:
    return result.outcome in (UNPARSEABLE, UNPROVABLE)


def close_target(tokens: list[str], env: dict | None = None) -> Result | None:
    """The result for an issue close, or for a call that cannot be proved not to
    be one (unparseable or unprovable). None only when the segment is provably
    not an issue close."""
    result = classify(tokens, env)
    return result if (result.gated and result.kind == "issue") or _undecided(result) else None


def merge_target(tokens: list[str], env: dict | None = None) -> Result | None:
    """The result for a pull-request merge, or for a call that cannot be proved
    not to be one. None only when the segment is provably not a merge."""
    result = classify(tokens, env)
    return result if (result.gated and result.kind == "pr") or _undecided(result) else None
