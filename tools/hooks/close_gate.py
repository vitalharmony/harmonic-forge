"""The close and merge gates as one decision, shared by the PreToolUse hooks
and the gh shim (harmonic-forge#1447).

The hooks read a Bash command's text; the shim reads the real argv `gh`
receives, so no shell spelling (an alias, `xargs`, `$G`, a function, a
comment hiding a reserved word) can hide a close or merge from it. Both call
the same decisions here: `block_missing_preclose_inspection.decide_targets`
(the preclose label and receipt) and
`block_undetermined_phase_close.segment_verdict` (the shipped-evidence
phase rule). The hooks stay fail-open; the shim runs them in strict mode,
where any read the decision needs and cannot make refuses (`GateReadError`).
"""
from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


@dataclass
class _State:
    strict: bool = False
    gh_bin: str | None = None


STATE = _State()


class GateReadError(Exception):
    """A read the gate needs could not be made (strict mode only)."""


def unread(args, why: str) -> None:
    """What a hook's `_gh` returns for a failed read: None (fail open) on the
    hook path, a raised `GateReadError` on the shim path."""
    if STATE.strict:
        raise GateReadError(f"could not read `gh {' '.join(map(str, args))}` ({why})")
    return None


@contextlib.contextmanager
def strict(gh_bin: str | None) -> Iterator[None]:
    saved = (STATE.strict, STATE.gh_bin)
    STATE.strict, STATE.gh_bin = True, gh_bin
    try:
        yield
    finally:
        STATE.strict, STATE.gh_bin = saved


PREFIX = "gh-shim: refused (close/merge gate, harmonic-forge#1447): "


def shim_verdict(argv: list[str], *, cwd: str, real_gh: str) -> str | None:
    """None to let the shim run `gh argv`; otherwise the refusal it prints.
    Any exception is the caller's to refuse on (fail closed).

    Deny by default (harmonic-forge#1447 reforge): `gh_argv.decide` allows
    only the calls it can prove cannot close or merge, binds `issue close`,
    `pr merge` and every other issue/PR REST mutation to one target, and
    refuses everything it cannot parse."""
    import gh_argv  # noqa: PLC0415
    BUILTINS = frozenset(gh_argv.load_table()["builtins"])  # noqa: N806
    argv = list(argv)
    if not argv:
        return None
    if argv[0] not in BUILTINS:
        # Sticky-wicket epoch 2: the gate expands no alias. Every root outside the
        # static, checked-in builtin set (a config alias like `co`, a `!` alias, an
        # extension) refuses, so what is validated is what gh runs.
        return PREFIX + (f"`gh {argv[0]}` is not a gh builtin (gh {gh_argv.load_table()['gh_version']}); "
                         "aliases and extensions are refused through the shim, so spell the builtin "
                         "command (for example `gh pr checkout`, not `gh co`)")
    if argv[0] not in gh_argv.GATED_ROOTS:
        return None
    verdict = gh_argv.decide(argv, dict(os.environ))
    if verdict.action == "allow":
        return None
    if verdict.action == "refuse":
        return PREFIX + verdict.reason
    target = verdict.target
    import block_missing_preclose_inspection as preclose  # noqa: PLC0415
    import block_undetermined_phase_close as phase  # noqa: PLC0415
    tokens = list(target.tokens)
    try:
        with strict(real_gh):
            decided = preclose.decide_targets(
                [(target.repo, target.number, target.kind, target.exempt)], cwd,
                lambda _number: list(target.heads), strict_repo=True)
            if decided:
                return PREFIX + decided[0]
            decided = phase.segment_verdict(tokens, cwd, {}, strict_repo=True)
            if decided:
                return PREFIX + decided[0]
    except GateReadError as exc:
        return (PREFIX + f"{exc}; a close or merge fails closed when its gate cannot be "
                "evaluated. Retry once GitHub is reachable.")
    return None
