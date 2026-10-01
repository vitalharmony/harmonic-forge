"""Codex Lane 2: no writes into the main checkout (harmonic-forge#840).

With no sandbox at any Codex lane (operator ruling 2026-09-30, "codex launches
with no sandbox, ever"), the sandbox is no longer what kept a Codex Lane 2
session out of the project's shared main checkout. Claude Lane 2 has always
been kept out by `block_lane1_status_claims.lane2_write_in_main_checkout`;
this applies that same predicate to Codex's `apply_patch` and `Bash` writes,
so the no-sandbox ruling holds without letting Lane 2 write where Lane 1 and
the operator work (operator ruling on #840's preclose, 2026-09-30).

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
    interpreter_write_pairs,
    lane2_write_in_main_checkout,
)

LANE2_DENIAL = (
    "Codex Lane 2 may not write into the project's main checkout; work in "
    "your per-issue worktree under ~/Harmonic_Projects/.worktrees "
    "(harmonic-forge#840)."
)


def _resolve(target: str, cwd: Path) -> str:
    raw = Path(target).expanduser()
    return str(raw if raw.is_absolute() else cwd / raw)


def bash_targets(command: str, cwd: Path) -> list[str]:
    """Absolute write targets of a shell command, following a static `cd`.
    A dynamic or bare `cd` makes later relative targets unresolvable, and
    they are skipped (fail open), never guessed at."""
    try:
        segments = command_segments(command)
    except (AttributeError, TypeError, ValueError):
        return []
    effective, resolvable = cwd, True
    targets: list[str] = []
    for segment in segments:
        if segment and segment[0] == "cd":
            operands = [t for t in segment[1:] if not t.startswith("-")]
            if not operands or any(c in operands[0] for c in "$`"):
                resolvable = False
                continue
            moved = Path(operands[0]).expanduser()
            effective = moved if moved.is_absolute() else effective / moved
            resolvable = True
            continue
        joined = " ".join(segment)
        raw = list(bash_write_targets(segment))
        raw += [operand for _construct, operand in interpreter_write_pairs(joined)]
        for target in raw:
            if not resolvable and not Path(target).expanduser().is_absolute():
                continue
            targets.append(_resolve(target, effective))
    return targets


def lane2_denial(tool_name: str, command: str, cwd: Path, patch_targets) -> str | None:
    """The denial reason when a Codex Lane 2 write lands in the main checkout,
    else None. `patch_targets` is the guard's own `apply_patch_targets`."""
    if tool_name == "apply_patch":
        targets = [_resolve(t, cwd) for t in (patch_targets(command) or [])]
    elif tool_name == "Bash":
        targets = bash_targets(command, cwd)
    else:
        return None
    for target in targets:
        if lane2_write_in_main_checkout(target, cwd):
            return LANE2_DENIAL
    return None
