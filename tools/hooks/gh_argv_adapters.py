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
    unprovable  `gh api` call `gh_argv` refuses only because it cannot prove the
                call is not a close or merge (GraphQL by node id, an unreadable
                `--input` body, a branch-moving endpoint)
    allowed     a gated root whose call does not close or merge
    not_gated   not `gh`, or a root outside `gh_argv.GATED_ROOTS`

`unprovable` and `unparseable` are kept apart from `unbound` on purpose: the old
text hooks let both through as "not a close", `gh_argv` refuses both, and an
adopter must choose per consumer rather than inherit the shim's answer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import gh_argv
from shell_parse import strip_invocation_prefix

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


def classify(tokens: list[str], env: dict | None = None) -> Result:
    """Classify one command segment (the tokens a hook holds after shell splitting)."""
    env = dict(os.environ) if env is None else env
    tokens = strip_invocation_prefix(list(tokens))
    if not tokens or Path(tokens[0]).name != "gh":
        return Result(NOT_GATED, reason="not a gh invocation")
    argv = tokens[1:]
    if not argv or argv[0] not in gh_argv.GATED_ROOTS:
        return Result(NOT_GATED, reason="a gh root outside the gated roots")
    try:
        parsed = gh_argv.parse(argv)
    except gh_argv.ArgvError as exc:
        return Result(UNPARSEABLE, reason=str(exc))
    verdict = gh_argv.decide(argv, env)
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


def close_target(tokens: list[str], env: dict | None = None) -> Result | None:
    """The result for an issue close, or None when the segment is not one."""
    result = classify(tokens, env)
    return result if result.gated and result.kind == "issue" else None


def merge_target(tokens: list[str], env: dict | None = None) -> Result | None:
    """The result for a pull-request merge, or None when the segment is not one."""
    result = classify(tokens, env)
    return result if result.gated and result.kind == "pr" else None
