"""Where sprint-plan's per-repo data lives (harmonic-forge#708).

The scripts are platform code; the documents they check, the drift baseline
and the board cache belong to the engagement's home checkout. Every path used
to be a `Path(__file__).parents[4]` climb that assumed the scripts sat inside
HRSE2 -- true until this move, silently wrong after it. They now resolve from
the checkout that holds the resolved `.claude/sprint-plan.config.json`.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config_loader  # noqa: E402

CONFIG = config_loader.CONFIG


@lru_cache(maxsize=1)
def _resolved() -> tuple[Path, dict]:
    root, config = config_loader.resolve_home()
    if config.get("repos"):
        return root, config
    raise config_loader.ConfigError(f"sprint-plan config: {root} declares no repos")


def home_root() -> Path:
    """The engagement's home checkout: docs, baseline and cache live under it."""
    return _resolved()[0]


def config() -> dict:
    return _resolved()[1]


def group() -> list[dict]:
    """The declared repos, enriched from projects.toml (see config_loader.group)."""
    return config_loader.group(config())


def docs_dir() -> Path:
    return home_root() / "docs"


def data_dir() -> Path:
    """Per-repo sprint-plan data: the drift baseline and the board cache."""
    return home_root() / ".claude" / "sprint-plan"


def repo_names() -> list[str]:
    return [r["repo"] for r in group()]


def home_repo() -> str:
    """The group's `default: true` repo -- the one whose docs this checks."""
    return next(r["repo"] for r in group() if r["default"])
