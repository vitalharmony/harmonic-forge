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
    """Every path a first-column cell declares, losslessly (sticky-wicket on
    harmonic-forge#1231: both passes found a parser that threw declared text away).

    The tokens are the backticked spans PLUS the comma-split text left over once
    those spans are removed, each with a trailing `(note)` stripped. Nothing is
    dropped for containing a space or for sitting beside a backticked path: a
    junk token can only tighten the route, a dropped one can loosen it."""
    tokens = _BACKTICKED.findall(cell) + _BACKTICKED.sub(" ", cell).split(",")
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

    The comparison is `route_store.record_if_not_looser`'s, made against the
    newest EVALUATED route (an operator override is a floor that `current()`
    merges in, and comparing against it dropped real tightenings). Returns a
    message to print when the route was not recorded (a looser result is printed
    as a proposal, never stored). Raises on a store error; the caller prints it
    and never raises past the post.

    Known limit (preclose pass 1, operator ruling A plus B): the comparison and
    the append in `record_if_not_looser` are not atomic, so a second writer on
    the same issue that lands between them can be overwritten by a looser
    result. Today that needs two concurrent non-override writers on one issue,
    the route is shadow only, and an operator override is held by `current()` as
    a floor regardless. The atomic compare-and-append is the follow-up, and
    `record_if_not_looser` is the one place it goes."""
    key = f"{repo}#{issue}"
    event, evaluated = route_store.record_if_not_looser(key, route, decider="l1-post", checkpoint="handoff")
    if event is None:
        return (f"[route] handoff snapshot proposes path {route.path} ({route.gate_id}), looser than "
                f"path {evaluated} in force for {key}; not recorded")
    return None
