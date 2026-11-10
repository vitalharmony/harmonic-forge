"""harmonic-forge#1230 (semantic routing A1): route events in the telemetry store.

One event per routing checkpoint (filing, handoff, pre-merge, override). Only
the evaluator and the operator's `route set` write events. A write appends under
a per-issue lock and reads only the issue's newest timestamp, to keep events in
append order; it never decides a route, so two writers racing decide nothing.
The route in force is computed when the events are read (see `current`): the
newest evaluated route, held no looser than the strictest operator override.

Fail closed: an unparseable event line raises rather than being skipped, since
a skipped line could be the tightening that holds the route in force.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
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

    def _unreadable(err: OSError) -> None:
        # `Path.glob` skips a directory it cannot read, so an unreadable store
        # would load as "no route" (harmonic-forge#1243 pass 2).
        raise RuntimeError(f"route store: cannot read {err.filename} ({err.strerror})") from err

    if base.exists():
        # followlinks: `Path.glob` traverses a symlinked directory, so the walk must too.
        for root, dirs, files in os.walk(base, onerror=_unreadable, followlinks=True):
            # `os.walk` classes a symlink whose target it cannot stat as a file
            # and never calls `onerror`, so stat each link here. Only a denied
            # read raises; any other unresolvable link (broken, a loop, a path
            # through a file) is still ignored, as before (harmonic-forge#1255).
            for name in dirs + files:
                path = os.path.join(root, name)
                if os.path.islink(path):
                    try:
                        os.stat(path)
                    except PermissionError as err:
                        _unreadable(err)
                    except OSError:
                        continue
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
    that route is reported as `shadow_path`/`shadow_gate`.

    A Tier floor is independent of the path (harmonic-forge#1267): once any
    evaluated event carries one, the result reports it and `floor_gate`, the
    gate that set it, whatever later events say."""
    evaluated: Optional[dict[str, Any]] = None
    floor: Optional[dict[str, Any]] = None
    floored: Optional[dict[str, Any]] = None
    for event in load(key):
        attrs = event["attrs"]
        if attrs.get("decider") == OVERRIDE:
            if floor is None or tighter(int(attrs["path"]), int(floor["attrs"]["path"])) == int(attrs["path"]):
                floor = event
        else:
            evaluated = event
            if attrs.get("tier_floor") == "deep" and floored is None:
                floored = event
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
    # records none), so a stricter override must not drop the evaluated route's;
    # harmonic-forge#1267: nor may a later evaluated event. An event written
    # before #1267 has no `floor_gate`, and its own gate set the floor.
    if floored is not None:
        result["attrs"]["tier_floor"] = floored["attrs"]["tier_floor"]
        result["attrs"]["floor_gate"] = floored["attrs"].get("floor_gate") or floored["attrs"]["gate_id"]
    if shadow is not None and int(shadow["attrs"]["path"]) != int(in_force["attrs"]["path"]):
        result["attrs"]["shadow_path"] = int(shadow["attrs"]["path"])
        result["attrs"]["shadow_gate"] = shadow["attrs"]["gate_id"]
    return result


def evaluated_path(key: str) -> Optional[int]:
    """The path of the newest evaluated (non-override) event for `key`, or None.

    This is the baseline a tighten-only writer compares against: `current()`
    merges an operator override's floor in, and a comparison against that floor
    drops a real tightening of the evaluated route (harmonic-forge#1231)."""
    newest = None
    for event in load(key):
        if event["attrs"].get("decider") != OVERRIDE:
            newest = event
    return None if newest is None else int(newest["attrs"]["path"])


@contextlib.contextmanager
def _route_lock(key: str):
    """An exclusive per-issue lock for a compare-then-append (harmonic-forge#1238).

    The path comes from `split_key(key)`, which strips the key and lowercases the
    repo, never from the raw key, so `Owner/Repo#5` and `owner/repo#5` share one
    file. It is a separate file from `emit`'s partition lock: `emit.emit` takes
    that lock itself for the append, and a second `flock` on another open file
    description of the same file blocks even in one process, so holding it across
    `record` would deadlock. The descriptor is closed on exit, so an exception or
    a killed holder releases the lock."""
    repo, number = split_key(key)
    directory = emit.store_root() / "route-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{repo.replace('/', '__')}#{number}.lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_if_not_looser(key: str, route: Route, decider: str, checkpoint: str, **kwargs
                         ) -> tuple[Optional[dict[str, Any]], Optional[int]]:
    """Record `route` unless it is looser than the newest evaluated route.

    Returns (the recorded event or None, the evaluated path compared against).
    The read of the newest evaluated route and the append happen under one
    per-issue lock (`_route_lock`), so a concurrent stricter writer cannot be
    overwritten by this looser one. It calls the lock-free `_append` because the
    lock is not reentrant; `record` takes the same lock for the writers that do
    not compare, such as the operator's `route set`. Any future writer that
    compares before it appends must hold the lock across both steps, as this
    does.

    A looser route carrying a Tier floor still returns None, and records the
    floor on a copy of the newest evaluated route, checkpoint included, so the
    path in force and the merge recheck's baseline are unchanged
    (harmonic-forge#1267 AC2)."""
    with _route_lock(key):
        baseline = evaluated_path(key)
        if baseline is not None and route.path > baseline:
            in_force = current(key)
            if route.tier_floor and not (in_force and in_force["attrs"].get("tier_floor")):
                newest = [e for e in load(key) if e["attrs"].get("decider") != OVERRIDE][-1]["attrs"]
                held = Route(int(newest["path"]), newest["gate_id"], newest.get("reason", ""),
                             tier_floor=route.tier_floor, floor_gate=route.floor_gate or route.gate_id)
                _append(key, held, decider, newest["checkpoint"],
                        **{k: v for k, v in kwargs.items() if k != "reason"})
            return None, baseline
        return _append(key, route, decider, checkpoint, **kwargs), baseline


def record(key: str, route: Route, decider: str, checkpoint: str, **kwargs) -> dict[str, Any]:
    """Append one proposed route under the per-issue lock and return the event.

    The lock lets `_append` keep `ns` strictly increasing for the issue
    (harmonic-forge#1240). `ns` is an order key, not a wall time: a forward clock
    spike pins that issue's later values above the spike."""
    with _route_lock(key):
        return _append(key, route, decider, checkpoint, **kwargs)


def _append(key: str, route: Route, decider: str, checkpoint: str, *,
            tier: Optional[str] = None, actor: str = "lane1",
            reason: Optional[str] = None) -> dict[str, Any]:
    """Append one event; the caller holds `_route_lock(key)`.

    `ns` is the wall clock or one above the issue's newest stored `ns`,
    whichever is larger, so the sort order `load` applies equals the append order
    even if the system clock steps backward. Reading the newest `ns` means a
    corrupt route store raises here, as it already does for `current()`: the
    filing checkpoint's event is then dropped (gh_issue.py catches it and the
    issue is still filed)."""
    repo, issue = split_key(key)
    _valid_path(route.path)
    newest = max((int(e["attrs"].get("ns", 0)) for e in load(key)), default=0)
    ns = max(time.time_ns(), newest + 1)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    owner = repo.split("/")[0]
    attrs = {"path": route.path, "gate_id": route.gate_id,
             "reason": (reason or route.reason)[:200], "decider": decider,
             "checkpoint": checkpoint, "tier": tier, "tier_floor": route.tier_floor,
             "floor_gate": route.floor_gate, "ns": ns}
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
