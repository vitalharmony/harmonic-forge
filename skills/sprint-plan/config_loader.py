"""Resolve and validate per-engagement sprint-plan configuration (F104)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

CONFIG = Path(".claude/sprint-plan.config.json")
LOCAL = Path(".claude/sprint-plan.local.json")
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


def _keys(value: dict, required: set[str], path: Path) -> None:
    missing = required - value.keys()
    extra = value.keys() - required
    if missing:
        raise _error(path, "missing " + ", ".join(f"$.{key}" for key in sorted(missing)))
    if extra:
        raise _error(path, "unknown " + ", ".join(f"$.{key}" for key in sorted(extra)))


def validate(value: dict, path: Path) -> str:
    if {"engagement", "home_repo"} <= value.keys():
        _keys(value, {"engagement", "home_repo"}, path)
        if not all(isinstance(value[key], str) and value[key] for key in value):
            raise _error(path, "member fields must be non-empty strings")
        return "member"
    if "home_checkout" in value:
        _keys(value, {"home_checkout"}, path)
        if not isinstance(value["home_checkout"], str) or not Path(value["home_checkout"]).is_absolute():
            raise _error(path, "$.home_checkout must be an absolute path")
        return "local"
    _keys(value, {"engagement", "doc_paths", "board_owner", "board_fields", "repos"}, path)
    if not isinstance(value["doc_paths"], list) or not value["doc_paths"]:
        raise _error(path, "$.doc_paths must be a non-empty list")
    fields = value["board_fields"]
    if not isinstance(fields, dict) or set(fields) != {"priority", "sequence", "tier"}:
        raise _error(path, "$.board_fields must contain priority, sequence, tier")
    repos = value["repos"]
    if not isinstance(repos, list) or not repos:
        raise _error(path, "$.repos must be a non-empty list")
    for index, repo in enumerate(repos):
        if not isinstance(repo, dict) or set(repo) != {"prefix", "repo", "short", "board", "default"}:
            raise _error(path, f"$.repos[{index}] has invalid fields")
        if not isinstance(repo["board"], (int, type(None))) or isinstance(repo["board"], bool):
            raise _error(path, f"$.repos[{index}].board must be integer or null")
    if sum(repo["default"] is True for repo in repos) != 1:
        raise _error(path, "$.repos must contain exactly one default: true")
    return "home"


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
