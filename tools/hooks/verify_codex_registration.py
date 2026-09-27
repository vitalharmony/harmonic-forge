#!/usr/bin/env python3
"""Fail loud when a repo's `.codex/hooks.json` is missing one of the three
merge/close gates (harmonic-forge#778 AC2).

harmonic-forge#774 merged and auto-closed with no pre-close pass because
Codex's `.codex/hooks.json` never wired `block_missing_preclose_inspection.py`,
`batch_gate.py` (the actual hook file; `batch_auth.py` is the library it
imports -- see its own module docstring) or `block_closing_keywords.py` at
all -- three gates that already existed and were already wired for Claude
Code. A prose note saying "wire these three in Codex too" is precisely the
kind of drift `batch_auth.verify_registration` (harmonic-forge#552) already
exists to catch for the gate/consumer split; this is the same check, one
level up, for gate/repo coverage instead of gate/consumer scope.

Run directly for a human-readable report, or import `verify()` for a test
or a wiring CI check.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

#: The three gates harmonic-forge#778 requires wired for Codex, alongside
#: whatever a repo's `.codex/hooks.json` already wires (`model_tier_gate.py`,
#: `lane3_codex_write_guard.py`, etc. -- untouched by this check).
REQUIRED_HOOKS = (
    "block_missing_preclose_inspection.py",
    "batch_gate.py",
    "block_closing_keywords.py",
)


def _registered_hooks(config: dict, needle: str) -> bool:
    for block in (config.get("hooks") or {}).get("PreToolUse") or []:
        for hook in block.get("hooks") or []:
            command = hook.get("command") or ""
            try:
                tokens = shlex.split(command)
            except ValueError:
                continue
            for index, token in enumerate(tokens):
                if Path(token).name == needle and index and Path(tokens[index - 1]).name in {"python", "python3"}:
                    return True
    return False


def verify(hooks_json_path: Path) -> tuple[bool, list[str]]:
    """`(ok, missing)` for one `.codex/hooks.json`. `ok` is `False`, and
    `missing` non-empty, when any of `REQUIRED_HOOKS` isn't registered under
    `PreToolUse` anywhere in the file. A missing or unparseable file counts
    as every hook missing -- fail loud, not "nothing to check."""
    try:
        config = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, list(REQUIRED_HOOKS)
    missing = [name for name in REQUIRED_HOOKS if not _registered_hooks(config, name)]
    return not missing, missing


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", action="append", dest="paths")
    args = parser.parse_args()
    targets = [Path(p) for p in args.paths] if args.paths else [
        Path.home() / "harmonic-forge" / ".codex" / "hooks.json",
        Path.home() / "Harmonic_Projects" / "HRSE2" / ".codex" / "hooks.json",
    ]
    ok_all = True
    for path in targets:
        ok, missing = verify(path)
        if ok:
            print(f"[OK] {path}: all {len(REQUIRED_HOOKS)} required hooks registered.")
        else:
            ok_all = False
            print(f"[MISSING] {path}: {', '.join(missing)} not registered under "
                  f"PreToolUse (harmonic-forge#778).", file=sys.stderr)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
