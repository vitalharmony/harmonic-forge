#!/usr/bin/env python3
"""PreToolUse hook: intercept a wrong-cwd `mypy` invocation (harmonic-forge#167).

CLAUDE.md's own documented gotcha: `backend/mypy.ini`'s `ignore_missing_imports`
overrides (fitz/google_auth_oauthlib/googleapiclient) are only discovered when
mypy is invoked with cwd=`backend/` — running from elsewhere with an absolute
`backend/app` path skips the config and produces phantom errors.

Not just a config-discovery quirk: `backend/mypy.ini` also sets
`explicit_package_bases = True` with "package root is backend/", so even
`mypy --config-file backend/mypy.ini backend/app` from repo root resolves
different module names than the correct `cd backend && mypy app` form. The
`--config-file`/`MYPY_CONFIG_FILE` allowance below is a deliberate operator
escape hatch, not a claim of equivalence.

Fail-open by design (the opposite default of block_lane1_status_claims.py's
posting-control guard) — a hook crash here would block the verification gate
this project's own testing depends on, which is a worse failure mode than
occasionally missing a wrong-cwd invocation. Every error path returns `{}`.

Scoped to repos that actually have the HRSE2-shaped trap (a reachable
`backend/mypy.ini`) — a Lane 1 session running `mypy` inside `harmonic-forge`
itself (no mypy anywhere in that repo) must never be denied.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shell_parse import command_segments  # noqa: E402  (harmonic-forge#167)

# python[3]?, uv run, poetry run prefixes before the real command word --
# the "improvised invocation" shapes this hook exists to catch, alongside
# the direct-binary form every in-repo scripted invocation already uses.
_RUNNER_PREFIXES = (
    ["python3", "-m"], ["python", "-m"], ["uv", "run"], ["poetry", "run"],
)


def _is_mypy_invocation(segment: list[str]) -> bool:
    """Command-word anchoring, skipping leading FOO=bar env assignments and
    a known runner prefix (python -m / uv run / poetry run). Rejects
    `grep mypy`, `cat mise.toml` (mypy only appears as an argument/file
    content, never as the resolved command word)."""
    tokens = segment
    i = 0
    while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith(("-", "/")):
        i += 1
    remaining = tokens[i:]
    if not remaining:
        return False  # env-assignment-only segment (e.g. `MYPY="..."`) — nothing to match
    for prefix in _RUNNER_PREFIXES:
        if remaining[: len(prefix)] == prefix:
            remaining = remaining[len(prefix):]
            break
    if not remaining:
        return False
    word = remaining[0]
    return word == "mypy" or word.endswith("/mypy")


def _has_config_file_override(segment: list[str]) -> bool:
    for token in segment:
        if token == "--config-file" or token.startswith("--config-file="):
            return True
        if token.startswith("MYPY_CONFIG_FILE="):
            return True
    return bool(os.environ.get("MYPY_CONFIG_FILE"))


def _git_root(cwd: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        return None
    return Path(result.stdout.strip())


def _repo_has_mypy_trap(cwd: Path) -> bool:
    """Is the enclosing repo HRSE2-shaped (has backend/mypy.ini)? A repo
    with no such file — e.g. harmonic-forge itself — has no cwd trap to
    guard, and a wrong-cwd mypy invocation there is simply not this
    hook's business."""
    root = _git_root(cwd)
    if root is None:
        return False
    return (root / "backend" / "mypy.ini").exists()


def denial() -> dict:
    message = (
        "Blocked: mypy invoked with cwd != backend/ and no --config-file "
        "override. backend/mypy.ini's ignore_missing_imports overrides are "
        "only discovered from cwd=backend/ — running elsewhere with an "
        "absolute path produces phantom errors (CLAUDE.md). Run "
        "`cd backend && .venv/bin/mypy app`, or cd into backend/ first, or "
        "pass --config-file backend/mypy.ini (note: even with an explicit "
        "config file, module-name resolution differs from the correct "
        "cd-first form — explicit_package_bases in mypy.ini)."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        },
        "systemMessage": message,
    }


def decision(command: object, payload_cwd: str | None) -> dict:
    if not isinstance(command, str):
        return {}
    try:
        segments = command_segments(command)
    except (AttributeError, TypeError, ValueError):
        return {}

    effective_cwd = Path(payload_cwd) if payload_cwd else Path.cwd()
    for segment in segments:
        if len(segment) == 2 and segment[0] == "cd":
            target = Path(segment[1]).expanduser()
            effective_cwd = target if target.is_absolute() else effective_cwd / target
            continue
        if not _is_mypy_invocation(segment) or _has_config_file_override(segment):
            continue
        if effective_cwd.name == "backend" or (effective_cwd / "mypy.ini").exists():
            continue  # correct invocation
        if not _repo_has_mypy_trap(effective_cwd):
            continue  # this repo has no cwd trap to guard (e.g. harmonic-forge itself)
        return denial()
    return {}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({}))
        return
    if payload.get("tool_name") != "Bash":
        print(json.dumps({}))
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    payload_cwd = payload.get("cwd")
    try:
        print(json.dumps(decision(command, payload_cwd)))
    except Exception:
        print(json.dumps({}))  # fail open on any unexpected error


if __name__ == "__main__":
    main()
