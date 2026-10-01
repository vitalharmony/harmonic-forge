"""Codex Lane 2: no writes into the main checkout (harmonic-forge#840).

With no sandbox at any Codex lane (operator ruling 2026-09-30, "codex launches
with no sandbox, ever"), the sandbox is no longer what kept a Codex Lane 2
session out of the project's shared main checkout. Claude Lane 2 has always
been kept out by `block_lane1_status_claims.lane2_write_in_main_checkout`;
this applies that same predicate to Codex's `apply_patch` and `Bash` writes,
so the no-sandbox ruling holds without letting Lane 2 write where Lane 1 and
the operator work (operator ruling on #840's preclose, 2026-09-30). It also
applies `write_on_main_branch`, so a Lane 2 write into ANOTHER project's main
checkout (an HRSE2 session writing into ~/harmonic-forge) is refused as the
Claude side refuses it (harmonic-forge#458; operator ruling on the #840
sticky-wicket, Q1).

It parses nothing itself (#840 sticky-wicket): directory changes come from
the shared `directory_change()` (compound keywords, `pushd`), and paths are
resolved by the caller's `_resolve`, which returns None rather than raising.

Same posture as the Claude check: an accidental-mix-up guard, not a security
boundary. It fails OPEN on anything it cannot parse or resolve, because a
Lane 2 session must never be blocked from its own worktree by a parse gap.
Lane 1 is not guarded: Lane 1 works in the main checkout by design.

Called by `lane3_codex_write_guard.py`, which is already wired on `^Bash$` and
`^apply_patch$` in every repo's `.codex/hooks.json`, so no consuming repo
needs a wiring change.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from block_lane1_status_claims import (  # noqa: E402
    bash_write_targets,
    command_segments,
    directory_change,
    interpreter_write_pairs,
    lane2_write_in_main_checkout,
    write_on_main_branch,
)

LANE2_DENIAL = (
    "Codex Lane 2 may not write into the project's main checkout; work in "
    "your per-issue worktree under ~/Harmonic_Projects/.worktrees "
    "(harmonic-forge#840)."
)


def bash_targets(command: str, cwd: Path, resolve) -> list[str]:
    """Absolute write targets of a shell command, following a static
    directory change. A dynamic or bare one makes later relative targets
    unresolvable, and they are skipped (fail open), never guessed at; so is
    any target `resolve` cannot resolve."""
    try:
        segments = command_segments(command)
    except (AttributeError, TypeError, ValueError):
        return []
    effective: Path | None = cwd
    targets: list[str] = []
    for segment in segments:
        change = directory_change(segment)
        if change is not None:
            target, resolvable = change
            moved = resolve(target, effective) if resolvable and effective else None
            effective = Path(moved) if moved else None
            continue
        joined = " ".join(segment)
        raw = list(bash_write_targets(segment))
        raw += [operand for _construct, operand in interpreter_write_pairs(joined)]
        for target in raw:
            # After an unresolvable directory change only an absolute (or
            # `~`) target is known; a relative one would be a guess.
            if effective is None and not target.startswith(("/", "~")):
                continue
            resolved = resolve(target, effective or cwd)
            if resolved is not None:
                targets.append(resolved)
    return targets


def lane2_denial(tool_name: str, command: str, cwd: Path, patch_targets, resolve) -> str | None:
    """The denial reason when a Codex Lane 2 write lands in a main checkout,
    this project's or another's, else None. `patch_targets` and `resolve` are
    the guard's own `apply_patch_targets` and `_resolve`."""
    if tool_name == "apply_patch":
        targets = [resolve(t, cwd) for t in (patch_targets(command) or [])]
    elif tool_name == "Bash":
        targets = bash_targets(command, cwd, resolve)
    else:
        return None
    for target in targets:
        if target and (lane2_write_in_main_checkout(target, cwd)
                       or write_on_main_branch(target, cwd)):
            return LANE2_DENIAL
    return None
