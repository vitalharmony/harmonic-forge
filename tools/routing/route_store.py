"""harmonic-forge#1230 (semantic routing A1): route events in the telemetry store.

One event per routing checkpoint (filing, handoff, pre-merge, override). Only
the evaluator and the operator's `route set` write events. A write only appends;
it never reads first, so two writers racing decide nothing. The route in force
is computed when the events are read (see `current`): the newest evaluated
route, held no looser than the strictest operator override.

Fail closed: an unparseable event line raises rather than being skipped, since
a skipped line could be the tightening that holds the route in force.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telemetry"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import emit  # noqa: E402
from gates import Route  # noqa: E402

SOURCE = "route"
EVENT_TYPE = "route.event"
EXTRACTOR = "route-1"
#: the `decider` of an operator's `route set` event
OVERRIDE = "override"


def tighter(a: int, b: int) -> int:
    """The stricter of two paths (1 is strictest)."""
    return min(a, b)


def split_key(key: str) -> tuple[str, int]:
    repo, _, number = key.strip().rpartition("#")
    if not repo or "/" not in repo or not number.isdigit():
        raise ValueError(f"route key must be OWNER/REPO#N, got {key!r}")
    # GitHub names are case-insensitive: one issue, one history.
    return repo.lower(), int(number)


def _valid_path(path: Any) -> int:
    if isinstance(path, bool) or not isinstance(path, int) or path not in (1, 2, 3):
        raise ValueError(f"route path must be 1, 2 or 3, got {path!r}")
    return path


def load(key: str) -> list[dict[str, Any]]:
    """Every route event for `key`, oldest first. Raises on a corrupt line."""
    repo, issue = split_key(key)
    base = emit.store_root() / "events"
    out: list[dict[str, Any]] = []
    for part in sorted(base.glob(f"*/*/{SOURCE}/*.jsonl")):
        with part.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    raise RuntimeError(f"route store: unreadable event at {part}:{number}") from exc
                if str(event.get("repo", "")).lower() == repo and event.get("issue") == issue:
                    try:
                        _valid_path((event.get("attrs") or {}).get("path"))
                    except ValueError as exc:
                        raise RuntimeError(f"route store: {exc} at {part}:{number}") from exc
                    out.append(event)
    return sorted(out, key=lambda e: int((e.get("attrs") or {}).get("ns", 0)))


def current(key: str) -> Optional[dict[str, Any]]:
    """The route in force for `key`, or None when nothing is recorded.

    The newest evaluator event is the evaluated route (design 05: newest =
    current), so a later checkpoint may loosen an earlier one. Each operator
    override is a floor: the route in force is the stricter of the newest
    evaluated route and the strictest override, and only child D's digest line
    removes an override. When the floor holds a looser evaluated route back,
    that route is reported as `shadow_path`/`shadow_gate`."""
    evaluated: Optional[dict[str, Any]] = None
    floor: Optional[dict[str, Any]] = None
    for event in load(key):
        attrs = event["attrs"]
        if attrs.get("decider") == OVERRIDE:
            if floor is None or tighter(int(attrs["path"]), int(floor["attrs"]["path"])) == int(attrs["path"]):
                floor = event
        else:
            evaluated = event
    if evaluated is None and floor is None:
        return None
    # An override replaces the evaluation only when strictly tighter: on a tie
    # the evaluated route stays in force with its gate and Tier floor.
    if evaluated is None or (floor is not None and
                             int(floor["attrs"]["path"]) != int(evaluated["attrs"]["path"]) and
                             tighter(int(floor["attrs"]["path"]), int(evaluated["attrs"]["path"]))
                             == int(floor["attrs"]["path"])):
        in_force, shadow = floor, evaluated
    else:
        in_force, shadow = evaluated, None
    result = dict(in_force, attrs=dict(in_force["attrs"]))
    # harmonic-forge#1234: an override never carries a tier_floor (`route set`
    # records none), so a stricter override must not drop the evaluated route's.
    if shadow is not None and "tier_floor" not in result["attrs"] and "tier_floor" in shadow["attrs"]:
        result["attrs"]["tier_floor"] = shadow["attrs"]["tier_floor"]
    if shadow is not None and int(shadow["attrs"]["path"]) != int(in_force["attrs"]["path"]):
        result["attrs"]["shadow_path"] = int(shadow["attrs"]["path"])
        result["attrs"]["shadow_gate"] = shadow["attrs"]["gate_id"]
    return result


def record(key: str, route: Route, decider: str, checkpoint: str, *,
           tier: Optional[str] = None, actor: str = "lane1",
           reason: Optional[str] = None) -> dict[str, Any]:
    """Append one proposed route and return the event. Never reads first."""
    repo, issue = split_key(key)
    _valid_path(route.path)
    ns = time.time_ns()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    owner = repo.split("/")[0]
    attrs = {"path": route.path, "gate_id": route.gate_id,
             "reason": (reason or route.reason)[:200], "decider": decider,
             "checkpoint": checkpoint, "tier": tier, "tier_floor": route.tier_floor, "ns": ns}
    event = {"ts": now, "source": SOURCE, "repo": repo, "issue": issue, "account": owner, "org": owner,
             "actor": actor, "event_type": EVENT_TYPE, "subject_kind": "route",
             "subject_id": f"route:{key}:{ns}", "provenance": "manual", "validated": True,
             "extractor_version": EXTRACTOR,
             "attrs": {k: v for k, v in attrs.items() if v is not None}}
    counts = emit.emit([event])
    if counts["rejected"]:
        raise RuntimeError(f"route event refused: {counts['rejected'][0][2]}")
    if counts["written"] != 1:
        raise RuntimeError(f"route event not stored (duplicate {counts['duplicate']})")
    return event
