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
    unprovable  a call that cannot be proved not to close or merge: gh in a shape
                `gh_segment` does not allow, a `gh api` call `gh_argv` refuses
                (GraphQL by node id, an unreadable `--input` body, a
                `{owner}/{repo}` placeholder), a flag before the command, an
                alias or extension, or a help flag on a close or merge path
    allowed     a gated root whose call does not close or merge
    not_gated   not `gh`, or a builtin root outside `gh_argv.GATED_ROOTS`

Whether a shell segment is a gh call at all is `gh_segment`'s strict allowlist
(shell grammar); this module reads only gh argv. Inline `NAME=value` assignments
before `gh` are the command's own environment and override the one passed in
(gh reads `GH_REPO`). `close_target`/`merge_target`
return the result for an unparseable or unprovable call too: None means the call
is provably not that kind.

`unprovable` and `unparseable` are kept apart from `unbound` on purpose: the old
text hooks let both through as "not a close", `gh_argv` refuses both, and an
adopter must choose per consumer rather than inherit the shim's answer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import gh_argv
import gh_segment

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
    exempt: bool = False  # gh_argv's not-planned/duplicate close exemption

    @property
    def gated(self) -> bool:
        """A close or merge call the gate can see: bound or unbound."""
        return self.outcome in (BOUND, UNBOUND)


def classify(tokens: list[str], env: dict | None = None) -> Result:
    """Classify one shell command segment. Whether the segment is a gh call at
    all, and with what environment, is `gh_segment`'s decision (shell grammar);
    this module reads only the gh argv it is handed. `env` is the environment the
    command inherits (the hook process's when None)."""
    segment = gh_segment.gh_segment(tokens)
    if segment.kind == gh_segment.NOT_GH:
        return Result(NOT_GATED, reason=segment.reason)
    if segment.kind == gh_segment.UNPROVABLE:
        return Result(UNPROVABLE, reason=segment.reason)
    merged = dict(os.environ if env is None else env)
    merged.update(segment.env)
    return classify_argv(list(segment.argv), merged)


def classify_argv(argv: list[str], env: dict) -> Result:
    """Classify one gh argv (the words after `gh`) against the pinned grammar."""
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
    if verdict.action == "allow" and parsed.help and (
            parsed.path in _CLI_TARGET_PATHS or tuple(parsed.path[:1]) == ("api",)):
        # A help call on a close or merge path, or on `gh api` (which can close
        # or merge): a drifted flag table can read a value as `--help`, so this
        # cannot prove the call is not a close (harmonic-forge#1496 reforge pass 1).
        return Result(UNPROVABLE, reason="a help flag on a close or merge path")
    if verdict.action == "gate" and verdict.target is not None:
        target = verdict.target
        if target.number:
            return Result(BOUND, kind=target.kind, repo=target.repo, number=target.number,
                          heads=target.heads, reason=verdict.reason, exempt=target.exempt)
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
