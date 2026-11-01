"""The route snapshot a handoff carries (harmonic-forge#1231, semantic routing A3).

A handoff is where an issue's file scope becomes concrete (its Affected Files
table). `snapshot_line` evaluates that scope with A1's `gates.evaluate` and
returns the one visible line `l1_post.py` appends before the footer digest;
`record_snapshot` writes the route event after the comment has posted.

Both fail open: a routing bug must never block Lane 1's posts. Shadow only --
nothing reads the route to grant or refuse anything yet.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "routing"))
import gates  # noqa: E402
import route_store  # noqa: E402

_HEADING = re.compile(r"(?m)^###[ \t]+Affected Files[ \t]*$")
_NEXT_HEADING = re.compile(r"(?m)^###?[ \t]")
_THREAT = re.compile(r"(?im)^[ \t]*\**[ \t]*Threat model[ \t]*\**[ \t]*:[ \t]*\**[ \t]*(?P<text>\S.*?)[ \t]*\**[ \t]*$")
_BACKTICKED = re.compile(r"`([^`]+)`")
_ANNOTATION = re.compile(r"\s*\([^)]*\)\s*$")
_QUALIFIED = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+):(?P<path>.+)$")


def affected_files(body: str, issue_repo: str) -> dict[str, list[str]]:
    """Repo -> paths from the first column of the Affected Files table.

    A cell is `path` (the issue's repo) or `owner/repo:path` (another repo).
    Rows without a path-like first cell (the header, the divider, `new`) add
    nothing."""
    start = _HEADING.search(body)
    if not start:
        return {}
    rest = body[start.end():]
    stop = _NEXT_HEADING.search(rest)
    section = rest[:stop.start()] if stop else rest
    halves: dict[str, list[str]] = {}
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cell = line.strip("|").split("|", 1)[0].strip()
        if not cell or cell.strip("`").strip().lower() == "file" or set(cell) <= set("-: "):
            continue
        for token in _cell_paths(cell):
            qualified = _QUALIFIED.match(token)
            repo, path = (qualified["repo"], qualified["path"].strip()) if qualified else (issue_repo, token)
            halves.setdefault(repo, []).append(path)
    return halves


def _cell_paths(cell: str) -> list[str]:
    """Every path a first-column cell declares (harmonic-forge#1231 pass 1).

    Backticked tokens when there are any (so `a.py`, `b.py` and
    `my file.py` (new) each keep their paths, spaces included); otherwise the
    comma-separated pieces of the plain cell. A trailing `(note)` is dropped.
    A cell is never skipped for containing a space: a dropped row would loosen
    the route the handoff declared."""
    tokens = _BACKTICKED.findall(cell) or cell.split(",")
    return [path for path in (_ANNOTATION.sub("", token.strip()).strip() for token in tokens) if path]


def threat_model(body: str) -> Optional[str]:
    """The `Threat model:` lead value, or None when the handoff has none."""
    lead = re.split(r"(?m)^###[ \t]", body, maxsplit=1)[0]
    match = _THREAT.search(lead)
    return match["text"].strip().strip("*").strip() if match else None


def snapshot(body: str, issue_repo: str) -> tuple[str, Optional[gates.Route]]:
    """(the line to append, the evaluated route or None when unavailable)."""
    try:
        scope = gates.Scope(repo=issue_repo, halves=affected_files(body, issue_repo),
                            threat_model=threat_model(body), runner_eligible=False)
        route = gates.evaluate(scope)
        return f"Route (snapshot): path {route.path} — gate {route.gate_id} — {route.reason}", route
    except Exception as exc:  # fail open: a routing bug never blocks a post
        return f"Route (snapshot): unavailable — {type(exc).__name__}", None


def record_snapshot(repo: str, issue: int, route: gates.Route) -> Optional[str]:
    """Record `route` at the handoff checkpoint, tighten-only.

    Returns a message to print when the route was not recorded in force (a
    looser result is printed as a proposal, never stored: the store treats any
    non-override event as the evaluated route in force). Raises on a store
    error; the caller prints it and never raises past the post.

    Known limit (preclose pass 1, operator ruling A plus B): the comparison
    reads `current()` and then appends, with no lock between the two. A second
    writer on the same issue that lands in that gap is overwritten by a looser
    result. Today that needs two concurrent non-override writers on one issue
    (a handoff post and a filing checkpoint), the route is shadow only, and an
    operator override is held by `current()` as a floor regardless. An atomic
    compare-and-append in `route_store` is the follow-up."""
    key = f"{repo}#{issue}"
    in_force = route_store.current(key)
    if in_force is not None:
        held = int(in_force["attrs"]["path"])
        if route_store.tighter(route.path, held) != route.path and route.path != held:
            return (f"[route] handoff snapshot proposes path {route.path} ({route.gate_id}), looser than "
                    f"path {held} in force for {key}; not recorded")
    route_store.record(key, route, decider="l1-post", checkpoint="handoff")
    return None
