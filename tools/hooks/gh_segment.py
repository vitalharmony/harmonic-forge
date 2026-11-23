"""Is one shell command segment exactly a gh call? The one shell gate (harmonic-forge#1496).

A hook holds a command as shell tokens. Before any gh grammar applies, something
must decide whether the segment *is* a gh call, and with what environment. That
decision is shell grammar, not gh grammar, so it lives here and nowhere else;
`gh_argv_adapters` reads only the gh argv this module hands it.

It is a strict allowlist. A segment is a gh call only in these shapes:

    [env] [NAME=value ...] gh ARGS...
    [env] [NAME=value ...] gh-as ACCOUNT gh ARGS...
    [env] [NAME=value ...] gha ACCOUNT ARGS...

Any other appearance of `gh`, `gh-as` or `gha` makes the segment `unprovable`: as
a token behind another prefix (`sudo`, `xargs`, `nohup`, `env -u X`, a redirection,
`command`, `exec`, `timeout`), inside a script handed to a shell's `-c` or to
`eval`, quoted, or inside a command substitution. A command word that is itself an
expansion, substitution or glob is `unprovable` too. Only a segment with no gh word
at all, and a literal command word, is `not_gh`. Nothing here
parses a script: modelling a shell inside a shell is the defect class this module
exists to avoid. The account wrappers set a token, never a repo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

GH = "gh"
UNPROVABLE = "unprovable"
NOT_GH = "not_gh"

_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_GH_NAMES = ("gh", "gh-as", "gha")
#: A gh word inside a script or quoted string: bounded by start, end, whitespace,
#: shell punctuation or a quote, optionally path-qualified.
_GH_WORD = re.compile(r"(?:^|[\s;&|(){}`$'\"<>])(?:[^\s;&|(){}`$'\"<>]*/)?(?:gh|gh-as|gha)(?=$|[\s;&|(){}`'\"<>])")
_SHELLS = ("bash", "sh", "zsh", "dash", "ksh")
_C_FLAG = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")


@dataclass(frozen=True)
class Segment:
    kind: str  # GH, UNPROVABLE or NOT_GH
    argv: tuple[str, ...] = ()  # the gh argv (after `gh`) when kind is GH
    env: dict = field(default_factory=dict)  # the segment's own NAME=value assignments
    reason: str = ""


def _script_tokens(tokens: list[str]) -> list[str]:
    """Tokens a shell will run as a script: the argument of a shell's `-c`, and
    everything after `eval`."""
    found: list[str] = []
    for index, token in enumerate(tokens):
        name = Path(token).name
        if name == "eval":
            found.extend(tokens[index + 1:])
        if name in _SHELLS:
            for flag in range(index + 1, len(tokens) - 1):
                if _C_FLAG.match(tokens[flag]):
                    found.append(tokens[flag + 1])
                    break
    return found


#: A command substitution whose body starts with an expansion: its command word is
#: dynamic, so what it runs cannot be read (harmonic-forge#1496 reforge pass 1).
_DYNAMIC_SUBSTITUTION = re.compile(r"(?:\$\(|`)\s*[$`]")
#: A command word whose text the shell rewrites before running it: an expansion,
#: a substitution, a glob or a quote splice.
_DYNAMIC_WORD = re.compile(r"[$`*?\[\]'\"\\]")


def _substitution_runs_gh(token: str) -> bool:
    """A `$(...)` or backtick inside `token` (quoted or not) whose body names gh,
    or whose own command word is dynamic. A plain `$VAR` runs nothing and is not
    one (harmonic-forge#1496 reforge pass 1)."""
    if "$(" not in token and "`" not in token:
        return False
    return bool(_GH_WORD.search(token) or _DYNAMIC_SUBSTITUTION.search(token))


def _mentions_gh(tokens: list[str]) -> bool:
    if any(Path(token).name in _GH_NAMES for token in tokens):
        return True
    if any(_substitution_runs_gh(token) for token in tokens):
        return True
    return any(_GH_WORD.search(script) for script in _script_tokens(tokens))


def gh_segment(tokens: list[str]) -> Segment:
    """Classify one command segment as a gh call, unprovable, or not gh."""
    tokens = list(tokens)
    index, env = 0, {}
    if index < len(tokens) and tokens[index] == "env":
        index += 1
    while index < len(tokens) and (match := _ASSIGNMENT.match(tokens[index])):
        env[match.group(1)] = match.group(2)
        index += 1
    if index < len(tokens) and _DYNAMIC_WORD.search(tokens[index]):
        # Fail closed (harmonic-forge#1496 reforge pass 1): `$GH`, `$(echo gh)`,
        # a backtick, `g${x}h` or a glob in command position can run gh.
        return Segment(UNPROVABLE, env=env, reason="the command word is an expansion, substitution or glob")
    if index < len(tokens):
        name = Path(tokens[index]).name
        rest = tokens[index + 1:]
        if name == "gh" and not _mentions_gh(rest):
            return Segment(GH, tuple(rest), env)
        if name == "gh-as" and len(rest) >= 2 and Path(rest[1]).name == "gh" and not _mentions_gh(rest[2:]):
            return Segment(GH, tuple(rest[2:]), env)
        if name == "gha" and len(rest) >= 1 and not _mentions_gh(rest[1:]):
            return Segment(GH, tuple(rest[1:]), env)
    if _mentions_gh(tokens):
        return Segment(UNPROVABLE, env=env, reason="gh behind a prefix, in a script, or in a shape this gate does not allow")
    return Segment(NOT_GH, env=env, reason="no gh word in the segment")
