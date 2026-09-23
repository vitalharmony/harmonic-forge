#!/usr/bin/env python3
"""Codex Lane 3 write guard (harmonic-forge#644).

Forge-owned PreToolUse hook for `^apply_patch$` and `^Bash$`, wired by
absolute path into every repo's `.codex/hooks.json` that runs a Codex
Lane 3. Denies any write outside `~/Harmonic_Projects/testplan/` using the
same predicate Claude Lane 3 already uses (`lane3_write_outside_testplan`).

Repo-agnostic: reads no repo-specific config, so one copy in
`~/harmonic-forge/tools/hooks/` covers every repo without duplication.

Never imports or calls `protected_write_denial` — that predicate carries
`write_on_main_branch` and the Lane 2 checks, which would change Codex
Lane 1 and Lane 2 behavior too.

This guard DOES read `LANE` itself (harmonic-forge#644 rework), despite
`lane3_write_outside_testplan` already gating on it: several of this
file's own branches deny BEFORE ever calling that predicate (an
unparseable command, an unresolvable `cd` plus a relative write, an
unrecognized `apply_patch` shape) — those are fail-closed decisions this
file makes on its own, and they ran unconditionally at every `LANE`
value, blocking ordinary Codex Lane 1/Lane 2 writes. So `main()` checks
`LANE == "3"` first and allows outright otherwise, before any parsing;
every fail-closed branch below still applies, unchanged, once `LANE`
actually is `"3"`.

apply_patch `tool_input` shape (harmonic-forge#644 Step 0, cited per the
Implementation Spec — never guessed): `{"command": "<patch text>"}`,
confirmed live against the codex-cli 0.154.0 source,
`codex-rs/core/src/tools/handlers/apply_patch.rs`
(`pre_tool_use_payload`, tag `rust-v0.154.0`) — the same
`tool_input.command` string convention Bash already uses. The patch text
itself uses the literal markers `codex-apply-patch/src/parser.rs` defines
at that tag: `*** Add File: `, `*** Update File: `, `*** Delete File: `,
`*** Move to: `.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from block_lane1_status_claims import (  # noqa: E402
    bash_write_targets,
    command_segments,
    interpreter_write_pairs,
    lane3_write_outside_testplan,
)

DENIAL_MESSAGE = (
    "Codex Lane 3 may write only inside ~/Harmonic_Projects/testplan/ "
    "(harmonic-forge#644)."
)

_PATCH_TARGET_MARKERS = (
    "*** Add File: ",
    "*** Update File: ",
    "*** Delete File: ",
    "*** Move to: ",
)

_CD_DYNAMIC = re.compile(r"[$`]")


def _deny(reason: str = DENIAL_MESSAGE) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allow(host: str) -> dict | None:
    """Explicit allow for hosts that support it; silence everywhere else."""
    if host != "claude":
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


def _emit(decision: dict | None) -> None:
    if decision is not None:
        print(json.dumps(decision))


def _host(argv: list[str]) -> str:
    """Return an explicitly named host without letting bad args skip denials."""
    try:
        return argv[argv.index("--host") + 1]
    except (ValueError, IndexError):
        return "unknown"


def _resolve(target: str, cwd: Path) -> str | None:
    try:
        raw = Path(target).expanduser()
        if not raw.is_absolute():
            raw = cwd / raw
        return str(raw)
    except (OSError, ValueError, RuntimeError):
        return None


def _cd_target(segment: list[str]) -> tuple[str | None, bool] | None:
    """`(target, resolvable)` for a `cd <path>` segment, else None.

    Thin, single-purpose reimplementation (not a reuse of
    `block_lane1_status_claims.directory_change`, which is not on this
    guard's approved import list) — covers exactly what TC3 requires: a
    later relative write judged against a `cd`'s destination, and a bare
    or dynamic `cd` treated as unresolvable so a subsequent relative
    target is denied rather than guessed at.
    """
    if not segment or segment[0] != "cd":
        return None
    operands = [token for token in segment[1:] if not token.startswith("-")]
    if not operands:
        return (None, False)
    target = operands[0]
    if _CD_DYNAMIC.search(target):
        return (None, False)
    return (target, True)


def apply_patch_targets(command: str) -> list[str] | None:
    """Every add/update/delete/move target in an `apply_patch` body.

    None means the payload does not parse as a patch at all — the
    caller's fail-closed case (Step 0 / spec 4.3): every unparseable
    `apply_patch` is denied until this recognizes the payload's shape.
    """
    if "*** Begin Patch" not in command:
        return None
    targets: list[str] = []
    for line in command.splitlines():
        for marker in _PATCH_TARGET_MARKERS:
            if line.startswith(marker):
                targets.append(line[len(marker):].strip())
                break
    if not targets:
        return None
    return targets


def bash_decision(command: str, cwd: Path, host: str) -> dict | None:
    try:
        segments = command_segments(command)
    except (AttributeError, TypeError, ValueError):
        return _deny(
            "Codex Lane 3 write guard: malformed shell command, refusing "
            "to allow an unverifiable write (harmonic-forge#644)."
        )

    effective_cwd = cwd
    cwd_unresolved = False
    for segment in segments:
        change = _cd_target(segment)
        if change is not None:
            target, resolvable = change
            if not resolvable or target is None:
                cwd_unresolved = True
                continue
            cwd_unresolved = False
            expanded = Path(target).expanduser()
            effective_cwd = (
                expanded if expanded.is_absolute() else effective_cwd / expanded
            )
            continue

        joined = " ".join(segment)
        raw_targets = list(bash_write_targets(segment))
        raw_targets += [operand for _construct, operand in interpreter_write_pairs(joined)]
        for raw_target in raw_targets:
            if cwd_unresolved and not Path(raw_target).expanduser().is_absolute():
                return _deny(
                    "Codex Lane 3 write guard: this command changes to a "
                    "directory this guard cannot resolve statically and "
                    f"then writes to the relative path {raw_target!r} "
                    "(harmonic-forge#644). Use an absolute path."
                )
            resolved = _resolve(raw_target, effective_cwd)
            if resolved is None or lane3_write_outside_testplan(resolved):
                return _deny()
    return _allow(host)


def main() -> int:
    host = _host(sys.argv[1:])

    if os.environ.get("LANE") != "3":
        # harmonic-forge#644 rework: this file's own fail-closed branches
        # below (unparseable command, unresolvable cd + relative write,
        # unrecognized apply_patch) run before lane3_write_outside_testplan
        # ever gets called, so they cannot rely on that predicate's own
        # LANE gate. Allow outright here, before any parsing, so a Codex
        # Lane 1/Lane 2 session is never denied by this guard.
        _emit(_allow(host))
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _emit(_deny(
            "Codex Lane 3 write guard: unparseable PreToolUse payload "
            "(harmonic-forge#644)."
        ))
        return 0

    if not isinstance(payload, dict):
        _emit(_deny(
            "Codex Lane 3 write guard: PreToolUse payload must be a JSON "
            "object (harmonic-forge#644)."
        ))
        return 0

    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    cwd_str = payload.get("cwd")
    if not isinstance(tool_input, dict) or not isinstance(cwd_str, str) or not cwd_str:
        _emit(_deny(
            "Codex Lane 3 write guard: PreToolUse payload missing cwd or "
            "tool_input (harmonic-forge#644)."
        ))
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        _emit(_deny(
            "Codex Lane 3 write guard: PreToolUse payload missing a "
            "command string (harmonic-forge#644)."
        ))
        return 0
    cwd = Path(cwd_str)

    if tool_name == "apply_patch":
        targets = apply_patch_targets(command)
        if not targets:
            _emit(_deny(
                "Codex Lane 3 write guard: unrecognized apply_patch "
                "payload shape, denying until parseable (harmonic-forge#644)."
            ))
            return 0
        for target in targets:
            resolved = _resolve(target, cwd)
            if resolved is None or lane3_write_outside_testplan(resolved):
                _emit(_deny())
                return 0
        _emit(_allow(host))
        return 0

    if tool_name == "Bash":
        _emit(bash_decision(command, cwd, host))
        return 0

    _emit(_allow(host))
    return 0


if __name__ == "__main__":
    sys.exit(main())
