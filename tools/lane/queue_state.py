"""Durable, single-writer state for ``lane-queue-run`` (F774)."""
from __future__ import annotations

import json
import os
from pathlib import Path

STATUSES = {"pending", "in_progress", "done", "blocked"}


def load(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read queue: {exc}") from exc
    items = state.get("items") if isinstance(state, dict) else None
    if not isinstance(items, list):
        raise ValueError("queue must contain an items list")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("every item needs a string id")
        if item.get("status") not in STATUSES:
            raise ValueError(f"item {item['id']} has invalid status")
        item.setdefault("kind", "work")
        item.setdefault("note", "")
    return state


def save(path: Path, state: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def recover(state: dict) -> bool:
    """Turn crash-ambiguous work into an operator-visible blocker."""
    changed = False
    for item in state["items"]:
        if item["status"] == "in_progress":
            item["status"] = "blocked"
            item["note"] = "recovery: prior driver stopped while this item was in progress"
            changed = True
    return changed


def next_item(state: dict) -> dict | None:
    return next((item for item in state["items"] if item["status"] == "pending"), None)


def is_blocked(state: dict) -> bool:
    return any(item["status"] == "blocked" for item in state["items"])
