"""Resolve and validate per-engagement sprint-plan configuration (F104)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

CONFIG = Path(".claude/sprint-plan.config.json")
LOCAL = Path(".claude/sprint-plan.local.json")
SCHEMA = Path(__file__).parent / "schema" / "sprint-plan.config.schema.json"
LANE_ROOT = re.compile(r"-lane[23]$")


class ConfigError(ValueError):
    pass


def _error(path: Path, message: str) -> ConfigError:
    return ConfigError(f"sprint-plan config: {path}: {message}")


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise _error(path, f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise _error(path, "$ must be an object")
    return value


def _type_matches(value: object, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)


def _schema_error(value: object, spec: dict, pointer: str = "$") -> str | None:
    expected = spec.get("type")
    if expected:
        choices = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(value, choice) for choice in choices):
            return f"{pointer} must be {' or '.join(choices)}"
    if isinstance(value, str):
        if len(value) < spec.get("minLength", 0):
            return f"{pointer} must be non-empty"
        if "pattern" in spec and not re.search(spec["pattern"], value):
            return f"{pointer} has invalid format"
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in spec:
        if value < spec["minimum"]:
            return f"{pointer} must be at least {spec['minimum']}"
    if isinstance(value, list):
        if len(value) < spec.get("minItems", 0):
            return f"{pointer} must contain at least {spec['minItems']} item(s)"
        for index, item in enumerate(value):
            if "items" in spec and (error := _schema_error(item, spec["items"], f"{pointer}[{index}]")):
                return error
    if isinstance(value, dict):
        required = set(spec.get("required", []))
        missing = sorted(required - value.keys())
        if missing:
            return "missing " + ", ".join(f"{pointer}.{key}" for key in missing)
        properties = spec.get("properties", {})
        if spec.get("additionalProperties") is False:
            extra = sorted(value.keys() - properties.keys())
            if extra:
                return "unknown " + ", ".join(f"{pointer}.{key}" for key in extra)
        for key, item in value.items():
            if key in properties and (error := _schema_error(item, properties[key], f"{pointer}.{key}")):
                return error
    return None


def validate(value: dict, path: Path) -> str:
    schema = _read(SCHEMA)
    title = "local config" if "home_checkout" in value else (
        "member config" if "home_repo" in value else "home config"
    )
    shape = next(item for item in schema["oneOf"] if item["title"] == title)
    if error := _schema_error(value, shape):
        raise _error(path, error)
    kind = title.partition(" ")[0]
    if kind == "home" and sum(repo["default"] is True for repo in value["repos"]) != 1:
        raise _error(path, "$.repos must contain exactly one default: true")
    return kind


def _guard(root: Path) -> None:
    if LANE_ROOT.search(root.name):
        raise ConfigError(f"sprint-plan config: refused lane worktree root {root}")


def resolve(cwd: Path | None = None, override: str | None = None) -> dict:
    cwd = (cwd or Path.cwd()).resolve()
    chosen = override or os.environ.get("SPRINT_PLAN_CONFIG")
    if chosen:
        path = Path(chosen)
        path = path if path.is_absolute() else cwd / path
        if not path.is_file():
            raise ConfigError(f"sprint-plan config: invalid override path {path}")
    else:
        path = next((parent / CONFIG for parent in (cwd, *cwd.parents) if (parent / CONFIG).is_file()), None)
        if path is None:
            raise ConfigError(f"sprint-plan config: no config found from cwd {cwd}")
    value = _read(path)
    kind = validate(value, path)
    root = path.parent.parent
    _guard(root)
    if kind != "member":
        return value
    local_path = root / LOCAL
    local = _read(local_path)
    validate(local, local_path)
    home = Path(local["home_checkout"])
    _guard(home)
    home_path = home / CONFIG
    if not home_path.is_file():
        raise ConfigError(f"sprint-plan config: missing member-local home checkout {home}")
    result = _read(home_path)
    if validate(result, home_path) != "home":
        raise _error(home_path, "member home must be a home config")
    if not any(repo["repo"] == value["home_repo"] for repo in result["repos"]):
        raise _error(home_path, "member home_repo is not listed in $.repos")
    return result
