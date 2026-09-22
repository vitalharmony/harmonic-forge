#!/usr/bin/env python3
"""Print a repo's verification gate as the exact calls to make (harmonic-forge#708).

The procedure is platform-owned; the commands are per-repo config in
`.claude/verification-gate.config.json`, validated against
`schema/verification-gate.config.schema.json`. Each command's `cwd` is
repo-relative and resolved against the checkout this runs from, so the same
config is correct in the main checkout and in every worktree -- the old
hardcoded absolute paths pointed every worktree's gate at the main checkout.

Usage (from the repo root or any directory under it):
    python3 ~/harmonic-forge/skills/verification-gate/gate_plan.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG = Path(".claude/verification-gate.config.json")
SCHEMA = Path(__file__).resolve().parent / "schema" / "verification-gate.config.schema.json"


class ConfigError(ValueError):
    pass


def _check(value: object, spec: dict, pointer: str) -> None:
    kind = spec.get("type")
    types = {"object": dict, "array": list, "string": str}
    if kind and not isinstance(value, types[kind]):
        raise ConfigError(f"{pointer} must be {kind}")
    if isinstance(value, str):
        if len(value) < spec.get("minLength", 0):
            raise ConfigError(f"{pointer} must be non-empty")
        if spec.get("pattern") == "^[^/]" and value.startswith("/"):
            raise ConfigError(f"{pointer} must be repo-relative, not absolute")
    if isinstance(value, list):
        if len(value) < spec.get("minItems", 0):
            raise ConfigError(f"{pointer} must contain at least {spec['minItems']} item(s)")
        for i, item in enumerate(value):
            _check(item, spec["items"], f"{pointer}[{i}]")
    if isinstance(value, dict):
        props = spec.get("properties", {})
        missing = sorted(set(spec.get("required", [])) - value.keys())
        if missing:
            raise ConfigError("missing " + ", ".join(f"{pointer}.{k}" for k in missing))
        if spec.get("additionalProperties") is False:
            extra = sorted(value.keys() - props.keys())
            if extra:
                raise ConfigError("unknown " + ", ".join(f"{pointer}.{k}" for k in extra))
        for key, sub in props.items():
            if key in value:
                _check(value[key], sub, f"{pointer}.{key}")


def find_root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / CONFIG).is_file():
            return p
    raise ConfigError(f"no {CONFIG} found from {start} upward")


def load(root: Path) -> dict:
    try:
        config = json.loads((root / CONFIG).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{root / CONFIG}: invalid JSON: {exc}") from exc
    _check(config, json.loads(SCHEMA.read_text()), "$")
    return config


def render(root: Path, config: dict) -> str:
    lines = []
    if "policy_doc" in config:
        lines += [f"Read first, in full: {root / config['policy_doc']}", ""]
    for n, cmd in enumerate(config["commands"], 1):
        when = cmd.get("when_changed")
        suffix = f" -- only if changed: {', '.join(when)}" if when else ""
        lines += [f"Command {n} ({cmd['name']}){suffix}:",
                  f"  working_dir: {(root / cmd['cwd']).resolve()}",
                  f"  command:     {cmd['command']}"]
        if cmd.get("note"):
            lines += [f"  # {line}" for line in cmd["note"].splitlines()]
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    try:
        root = find_root(Path.cwd().resolve())
        print(render(root, load(root)), end="")
    except ConfigError as exc:
        print(f"verification-gate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
