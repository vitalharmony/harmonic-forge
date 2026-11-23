"""Is one shell command segment exactly a gh call? The one shell gate (harmonic-forge#1496).

A hook holds a command as shell tokens. Before any gh grammar applies, something
must decide whether the segment *is* a gh call, and with what environment. That
decision is shell grammar, not gh grammar, so it lives here and nowhere else;
`gh_argv_adapters` reads only the gh argv this module hands it.

It is a strict allowlist. A segment is a gh call only in these shapes:

    [env] [NAME=value ...] gh ARGS...
    [env] [NAME=value ...] gh-as ACCOUNT gh ARGS...
    [env] [NAME=value ...] gha ACCOUNT ARGS...

Any other segment with a bounded `gh`, `gh-as` or `gha` word anywhere (any token,
assignment values and quoted strings included), a substitution whose command word
is an expansion, or a command word that is itself an expansion, substitution or
glob (`[` and `[[` excepted) is `unprovable`. This is containment, not
recognition (harmonic-forge#1496 re-scope): no runner is modelled. `not_gh` means
only that no gh word is visible; a gh call hidden from every token (an alias, a
function, a wrapper script, an encoded string) is out of scope, and the F1447
real-argv shim is the backstop. The account wrappers set a token, never a repo.
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
#: A gh word anywhere in a token: bounded by start, end, whitespace, shell
#: punctuation or a quote, optionally path-qualified. Containment, not
#: recognition (harmonic-forge#1496 re-scope): no runner is modelled.
_GH_WORD = re.compile(r"(?:^|[\s;&|(){}`$'\"<>=])(?:[^\s;&|(){}`$'\"<>=]*/)?(?:gh|gh-as|gha)(?=$|[\s;&|(){}`'\"<>=])")
#: A command word whose text the shell rewrites before running it: an expansion,
#: a substitution, a glob or a quote splice.
_DYNAMIC_WORD = re.compile(r"[$`*?\[\]'\"\\]")
#: The test builtins: a literal `[` or `[[` command word is not a glob.
_TEST_BUILTINS = ("[", "[[")
#: A plain account name for the gh-as and gha wrappers.
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class Segment:
    kind: str  # GH, UNPROVABLE or NOT_GH
    argv: tuple[str, ...] = ()  # the gh argv (after `gh`) when kind is GH
    env: dict = field(default_factory=dict)  # the segment's own NAME=value assignments
    reason: str = ""


#: A command substitution whose own command word is an expansion: what it runs
#: cannot be read, so it may run gh. Names no runner.
_DYNAMIC_SUBSTITUTION = re.compile(r"(?:\$\(|`)\s*[$`]")


def _mentions_gh(tokens: list[str]) -> bool:
    """A bounded gh, gh-as or gha word anywhere in any token, assignment values
    and quoted strings included, or a substitution whose command word is an
    expansion."""
    return any(Path(token).name in _GH_NAMES or _GH_WORD.search(token)
               or _DYNAMIC_SUBSTITUTION.search(token) for token in tokens)


def gh_segment(tokens: list[str]) -> Segment:
    """Classify one command segment as a gh call, unprovable, or not gh."""
    tokens = list(tokens)
    index, env = 0, {}
    if index < len(tokens) and tokens[index] == "env":
        index += 1
    while index < len(tokens) and (match := _ASSIGNMENT.match(tokens[index])):
        env[match.group(1)] = match.group(2)
        index += 1
    if index < len(tokens) and tokens[index] not in _TEST_BUILTINS and _DYNAMIC_WORD.search(tokens[index]):
        # Fail closed (harmonic-forge#1496 reforge pass 1): `$GH`, `$(echo gh)`,
        # a backtick, `g${x}h` or a glob in command position can run gh.
        return Segment(UNPROVABLE, env=env, reason="the command word is an expansion, substitution or glob")
    if index < len(tokens):
        name = Path(tokens[index]).name
        rest = tokens[index + 1:]
        assigned = tokens[:index]
        if _mentions_gh(assigned):
            return Segment(UNPROVABLE, env=env, reason="gh inside an inline assignment")
        if name == "gh" and not _mentions_gh(rest):
            return Segment(GH, tuple(rest), env)
        if name in ("gh-as", "gha") and rest and not _ACCOUNT.fullmatch(rest[0]):
            # The account slot must be a plain literal: a substitution or a gh
            # word there runs before the wrapper (harmonic-forge#1496 re-scope).
            return Segment(UNPROVABLE, env=env, reason="the account slot is not a plain literal")
        if name == "gh-as" and len(rest) >= 2 and Path(rest[1]).name == "gh" and not _mentions_gh(rest[2:]):
            return Segment(GH, tuple(rest[2:]), env)
        if name == "gha" and len(rest) >= 1 and not _mentions_gh(rest[1:]):
            return Segment(GH, tuple(rest[1:]), env)
    if _mentions_gh(tokens):
        return Segment(UNPROVABLE, env=env, reason="a gh word outside the allowed shapes (containment; the F1447 shim is the backstop)")
    return Segment(NOT_GH, env=env, reason="no gh word is visible (best-effort, not a proof)")
