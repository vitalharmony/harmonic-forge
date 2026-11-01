"""harmonic-forge#1230 (semantic routing A1): route events in the telemetry store.

One event per routing checkpoint (filing, handoff, pre-merge, override). Only
the evaluator and the operator's `route set` write events. A write only appends
the proposed route; it never reads first. Tighten-only is applied when the
events are read: the route in force is the strictest event so far (on a tie,
the newest), and a looser later event is kept as the shadow proposal. So two
writers racing can never loosen a route, because neither one decides anything.

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


def tighter(a: int, b: int) -> int:
    """The stricter of two paths (1 is strictest)."""
    return min(a, b)


def split_key(key: str) -> tuple[str, int]:
    repo, _, number = key.rpartition("#")
    if not repo or "/" not in repo or not number.isdigit():
        raise ValueError(f"route key must be OWNER/REPO#N, got {key!r}")
    return repo, int(number)


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
                if event.get("repo") == repo and event.get("issue") == issue:
                    out.append(event)
    return sorted(out, key=lambda e: int((e.get("attrs") or {}).get("ns", 0)))


def current(key: str) -> Optional[dict[str, Any]]:
    """The route in force for `key`, with the newest looser event after it as
    `shadow_path`/`shadow_gate`, or None when nothing is recorded."""
    in_force: Optional[dict[str, Any]] = None
    shadow: Optional[dict[str, Any]] = None
    for event in load(key):
        path = int(event["attrs"]["path"])
        if in_force is None or tighter(path, int(in_force["attrs"]["path"])) == path:
            in_force, shadow = event, None
        else:
            shadow = event
    if in_force is None:
        return None
    result = dict(in_force, attrs=dict(in_force["attrs"]))
    if shadow is not None:
        result["attrs"]["shadow_path"] = int(shadow["attrs"]["path"])
        result["attrs"]["shadow_gate"] = shadow["attrs"]["gate_id"]
    return result


def record(key: str, route: Route, decider: str, checkpoint: str, *,
           tier: Optional[str] = None, actor: str = "lane1",
           reason: Optional[str] = None) -> dict[str, Any]:
    """Append one proposed route and return the event. Never reads first."""
    repo, issue = split_key(key)
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
