"""harmonic-forge#1230 (semantic routing A1): route events in the telemetry store.

One event per routing checkpoint (filing, handoff, pre-merge, override). The
newest event for an issue is its current route. Only the evaluator and the
operator's `route set` write events. A re-check only tightens on its own: a
looser result is kept as a shadow proposal, never as the route in force.
"""
from __future__ import annotations

import json
import sys
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


def load(key: str, root: Optional[Path] = None) -> list[dict[str, Any]]:
    """Every route event for `key`, oldest first."""
    repo, issue = split_key(key)
    base = (root or emit.store_root()) / "events"
    out: list[dict[str, Any]] = []
    for part in sorted(base.glob(f"*/*/{SOURCE}/*.jsonl")):
        with part.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("repo") == repo and event.get("issue") == issue:
                    out.append(event)
    return sorted(out, key=lambda e: int((e.get("attrs") or {}).get("seq", 0)))


def current(key: str, root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    events = load(key, root)
    return events[-1] if events else None


def record(key: str, route: Route, decider: str, checkpoint: str, *,
           shadow: Optional[Route] = None, tier: Optional[str] = None,
           actor: str = "lane1", reason: Optional[str] = None,
           root: Optional[Path] = None) -> dict[str, Any]:
    """Write one route event and return it. Tighten-only: a route looser than
    the one in force is recorded as the shadow proposal, and the route in force
    is kept."""
    repo, issue = split_key(key)
    events = load(key, root)
    prior = events[-1] if events else None
    in_force = route
    if prior is not None:
        prior_attrs = prior["attrs"]
        if route.path > int(prior_attrs["path"]):
            shadow = shadow or route
            in_force = Route(int(prior_attrs["path"]), prior_attrs["gate_id"], prior_attrs["reason"],
                             prior_attrs.get("tier_floor"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    owner = repo.split("/")[0]
    seq = len(events)
    attrs = {"path": in_force.path, "gate_id": in_force.gate_id,
             "reason": (reason or in_force.reason)[:200], "decider": decider,
             "checkpoint": checkpoint, "tier": tier, "tier_floor": in_force.tier_floor,
             "shadow_path": shadow.path if shadow else None,
             "shadow_gate": shadow.gate_id if shadow else None, "seq": seq}
    event = {"ts": now, "source": SOURCE, "repo": repo, "issue": issue, "account": owner, "org": owner,
             "actor": actor, "event_type": EVENT_TYPE, "subject_kind": "route",
             "subject_id": f"route:{key}:{seq}", "provenance": "manual", "validated": True,
             "extractor_version": EXTRACTOR,
             "attrs": {k: v for k, v in attrs.items() if v is not None}}
    counts = emit.emit([event])
    if counts["rejected"]:
        raise RuntimeError(f"route event refused: {counts['rejected'][0][2]}")
    return event
