#!/usr/bin/env python3
"""Lane 3 read-only policy for cloud CLIs (`kubectl`, `doctl`) — hrse#327.

`.devin/agents/lane3-gate/AGENT.md` carries a narrow `Exec(kubectl get)` /
`Exec(doctl … list)` allow-list, but that is a **Devin-runtime** mechanism.
This repo's Lane 3 gates actually run under Claude Code or Codex, and a live
gate on hrse#327 proved the point: `kubectl delete pod <nonexistent>` reached
the real Kubernetes API and came back `NotFound` — refused by the API for
lack of a target, not by any permission layer. `3-lane-protocol.md:154-158`
already says a non-Devin Lane 3 tool needs "its own equivalent mechanism";
this module is that equivalent, shared by both CLIs so one list cannot drift
into two.

**Deny-on-non-match, not allow-on-match.** Every hook here is
deny-on-match/default-fall-through — `allow()` is a bare `return 0`, i.e. *no
opinion*, never a grant. A hook that only "allowed" `kubectl get` would
restrict nothing, because `kubectl delete` would still sail past on the
fall-through. So: under Lane 3, any `kubectl`/`doctl` invocation whose argv
prefix is not on the safe list is denied.

**Fail-closed.** A command this module cannot parse is denied, matching
`block_lane1_status_claims.py`'s safety-hook convention rather than
`block_inline_prose.py`'s deliberate fail-open (that one is a quality guard;
this is a safety guard, and the failure directions are opposite).

Known and deliberate limits, stated so nobody "fixes" them into a hole:
  * `kubectl --context=foo get nodes` is DENIED. A flag before the subcommand
    breaks the argv prefix, and the Devin list — which this mirrors exactly
    per AC1 — rejects it too. Mirroring beats convenience here.
  * Wrapper resolution covers the set in `_COMMAND_WRAPPERS` below. A wrapper
    outside that set (say a user shell function) is not resolved through.
    Adding one is a one-line change; leaving it silently open is not.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_parse import command_segments  # noqa: E402

# Loaded once at import, not per call. `gate_codex_tool.py` reuses THIS
# instance rather than exec'ing a second copy, so there is exactly one
# canonical module object across both call sites -- which also means a test
# patching it sees the same object the probe actually calls.
_CANONICAL_PATH = Path(__file__).resolve().parent / "block_lane1_status_claims.py"
_canonical_spec = importlib.util.spec_from_file_location("block_lane1_status_claims", _CANONICAL_PATH)
assert _canonical_spec and _canonical_spec.loader
_canonical = importlib.util.module_from_spec(_canonical_spec)
_canonical_spec.loader.exec_module(_canonical)

# Mirrors .devin/agents/lane3-gate/AGENT.md:67-76 exactly — read-only listing
# and describe subcommands only, never a bare kubectl/doctl prefix that would
# also match delete/create/apply.
SAFE_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("kubectl", "get"),
    ("kubectl", "describe"),
    ("doctl", "kubernetes", "cluster", "list"),
    ("doctl", "kubernetes", "cluster", "get"),
    ("doctl", "compute", "domain", "list"),
    ("doctl", "compute", "domain", "records", "list"),
    ("doctl", "compute", "droplet", "list"),
    ("doctl", "vpcs", "list"),
    ("doctl", "projects", "list"),
    ("doctl", "projects", "resources", "list"),
)

GUARDED_PROGRAMS = frozenset({"kubectl", "doctl"})

# hrse#327 NC2: every one of these parses with the WRAPPER as segment[0], so a
# basename check on segment[0] alone lets the real program through unexamined.
# All four confirmed live against the real parser:
#   env KUBECONFIG=x kubectl delete …  ->  ['env', 'KUBECONFIG=x', 'kubectl', …]
#   sudo kubectl delete …              ->  ['sudo', 'kubectl', …]
#   timeout 30 kubectl delete …        ->  ['timeout', '30', 'kubectl', …]
#   xargs kubectl delete …             ->  ['xargs', 'kubectl', …]
_COMMAND_WRAPPERS = frozenset({
    "env", "sudo", "timeout", "nice", "xargs", "nohup", "stdbuf", "command",
})

_MALFORMED_DENIAL = (
    "this command could not be parsed, and Lane 3's cloud-CLI policy fails "
    "closed rather than guessing (hrse#327). Re-issue it as a simple, "
    "unquoted command if it is a legitimate read-only `kubectl get` / "
    "`doctl … list`."
)


def _resolve_program(segment: list[str]) -> tuple[str, list[str]] | None:
    """Strip leading `VAR=value` assignments and known wrappers, returning
    (program_basename, effective_argv) — or None if nothing is left.

    `Path(...).name` handles an absolute path (`/usr/bin/kubectl delete`),
    which the Devin file's own string-prefix matching would miss. Stricter
    than the thing it mirrors, deliberately.
    """
    argv = list(segment)
    while argv:
        token = argv[0]
        if "=" in token and not token.startswith("=") and "/" not in token.split("=", 1)[0]:
            argv = argv[1:]           # VAR=value assignment prefix
            continue
        name = Path(token).name
        if name in _COMMAND_WRAPPERS:
            argv = argv[1:]
            # `timeout 30 kubectl …` / `nice -n 5 kubectl …`: drop the
            # wrapper's own operand(s) until something command-shaped appears.
            while argv and (argv[0].startswith("-") or argv[0].isdigit()):
                argv = argv[1:]
            continue
        return name, argv
    return None


def _is_safe(program: str, argv: list[str]) -> bool:
    candidate = tuple([program] + argv[1:])
    return any(candidate[: len(prefix)] == prefix for prefix in SAFE_PREFIXES)


def denial_reason(command: str) -> str | None:
    """None = no opinion. A string = deny, with that reason.

    Lane-agnostic on purpose: Claude Code and Codex each decide "is this a
    Lane 3 session?" through their own entry point, and keeping that decision
    out of here is what makes this function testable with no environment at
    all.
    """
    try:
        segments = command_segments(command)
    except (AttributeError, TypeError, ValueError):
        # hrse#327 NC1: fail closed. Same convention as
        # block_lane1_status_claims.py's own parse guard.
        return _MALFORMED_DENIAL

    for segment in segments:
        if not segment:
            continue
        resolved = _resolve_program(segment)
        if resolved is None:
            continue
        program, argv = resolved
        if program not in GUARDED_PROGRAMS:
            continue
        if not _is_safe(program, argv):
            return (
                f"`{' '.join(argv[:4])}` is not on Lane 3's read-only cloud-CLI "
                f"allow-list (hrse#327). A Lane 3 gate may run only read-only "
                f"{program} commands: "
                + ", ".join("`" + " ".join(p) + "`" for p in SAFE_PREFIXES if p[0] == program)
                + ". Lane 3 never mutates infrastructure — report the finding instead."
            )
    return None


def is_lane3_session(cwd: Path) -> bool:
    """Three-case LANE precedence (harmonic-forge#151), in the shared layer.

    hrse#327 NC3: this deliberately lives here rather than in HRSE2's
    `gate_codex_tool.py`. A cross-repo shared module importing an HRSE2-local
    helper inverts the dependency direction and cycles the moment
    `gate_codex_tool.py` imports this module — so the probe moves down here
    and `gate_codex_tool.is_lane3_session()` delegates to it, keeping exactly
    one copy of the logic.

    LANE == "3" is positive evidence; LANE set to anything else is positive
    evidence it is NOT Lane 3; LANE unset falls back to the fresh
    LANE3_ACTIVE marker, reusing the canonical helper's own encoding of that
    same three-case answer.
    """
    probe_task = next(iter(_canonical.LANE3_ONLY_TASKS))
    return _canonical.lane3_task_without_marker(["mise", "run", probe_task], cwd) is None


def main() -> int:
    """Claude Code PreToolUse entry point."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload.get("tool_input", {}).get("command", "")
    # hrse#327 NC4: the payload's cwd, never Path.cwd(). A hook runs as a
    # subprocess with no guarantee its own cwd matches the session's, and the
    # LANE-unset branch of the probe resolves LANE3_ACTIVE via `git rev-parse`
    # from exactly this path. Same pattern as gate_codex_tool.py:222.
    cwd = Path(payload.get("cwd") or Path.cwd())
    if not is_lane3_session(cwd):
        return 0
    reason = denial_reason(command)
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
