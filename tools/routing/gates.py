"""harmonic-forge#1230 (semantic routing A1): the ordered hard-gate evaluator.

A route's path is 1 (full 3-lane), 2 (Tooling Exception) or 3 (the autonomous
runner). Path 1 is the strictest. Gates are evaluated in a fixed order and the
first match wins (map ticket 04, operator-approved 2026-10-28):

1. client or cross-account repo (prefixes P, K, Y, D) -> 1
2. hrse product code under backend/ or frontend/ -> 1
3. an R-0371 authorization, declared by a hostile-author threat model -> 2, Tier
   deep, never 3
4. lane machinery (preclose_check.HIGH_BLAST_PATTERNS, imported) -> 2 at most
5. runner-eligible, as passed by the caller -> 3
6. the tooling-exception label (R-0226's operator scoping) -> 2
otherwise -> 1

A cross-repo change is gated per half and the strictest half wins. An empty
scope, or any half with no files, is path 1. A hostile-author threat model is
never path 3.

Every edge fails closed: inputs are normalized once (repo names lowercased,
paths stripped of `./` and normalized, the threat model's case and separators
folded), the issue's own repo is gated as well as each half, and a threat model
that is set but not recognized is treated as hostile-author.
Runner eligibility is an input and is never computed here: it needs an existing
issue and a sandboxed preflight (te_queue.item_failures).
"""
from __future__ import annotations

import posixpath
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gh"))
from preclose_check import HIGH_BLAST_PATTERNS  # noqa: E402

#: rules/lane-shorthand.md, "Repo prefixes": the repos that belong to another
#: account or to a client (K, Y, D, P), by full name.
CROSS_ACCOUNT_REPOS = frozenset({
    "kenekted/kenekted-platform", "kenekted/kenekted-ai", "kenekted/kenekted-docs",
    "leasepal-ml/leasepal-app-prototype",
})
HRSE_REPO = "vitalharmony/hrse"
PRODUCT_PREFIXES = ("backend/", "frontend/")
TE_LABEL = "tooling-exception"
HOSTILE = "hostile-author"
GOOD_FAITH = "good-faith"


@dataclass(frozen=True)
class Route:
    path: int
    gate_id: str
    reason: str
    tier_floor: str | None = None


@dataclass
class Scope:
    #: the issue's own repo, full name
    repo: str
    #: repo full name -> repo-relative paths touched in that repo
    halves: dict[str, list[str]] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    threat_model: str | None = None
    runner_eligible: bool = False


def _lane_machinery(path: str) -> str | None:
    for pattern, why in HIGH_BLAST_PATTERNS:
        if pattern.search(path):
            return why
    return None


def _repo(name: str) -> str:
    return name.strip().lower()


def _path(path: str) -> str:
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return posixpath.normpath(path) if path else path


def _threat(model: str | None) -> str | None:
    """None stays None; good-faith in any spelling is good-faith; anything
    else that is set reads as hostile-author."""
    if model is None:
        return None
    folded = model.strip().lower().replace("_", "-").replace(" ", "-")
    return GOOD_FAITH if folded == GOOD_FAITH else HOSTILE


def _gate_half(repo: str, paths: list[str], scope: Scope, threat: str | None) -> Route:
    if repo in CROSS_ACCOUNT_REPOS:
        return Route(1, "cross-account", f"{repo} belongs to another account or a client")
    if repo == HRSE_REPO:
        product = [p for p in paths if p.startswith(PRODUCT_PREFIXES)]
        if product:
            return Route(1, "hrse-product", f"hrse product code: {product[0]}")
    if threat == HOSTILE:
        return Route(2, "authorization", "hostile-author threat model: an R-0371 authorization",
                     tier_floor="deep")
    for path in paths:
        why = _lane_machinery(path)
        if why:
            return Route(2, "lane-machinery", f"{path}: {why}")
    if scope.runner_eligible:
        return Route(3, "runner", "runner-queue admission passed")
    if TE_LABEL in scope.labels:
        return Route(2, "te-label", "tooling-exception label (R-0226 operator scoping)")
    return Route(1, "default", "no gate matched")


def evaluate(scope: Scope) -> Route:
    """The route for `scope`: each half gated alone, the strictest half wins."""
    issue_repo = _repo(scope.repo)
    if issue_repo in CROSS_ACCOUNT_REPOS:
        return Route(1, "cross-account", f"{issue_repo} belongs to another account or a client")
    if not scope.halves:
        return Route(1, "no-scope", "no declared file scope")
    halves: dict[str, list[str]] = {}
    for repo, paths in scope.halves.items():
        halves.setdefault(_repo(repo), []).extend(_path(p) for p in paths if p.strip())
    if not any(halves.values()):
        return Route(1, "no-scope", "no declared file scope")
    empty = sorted(repo for repo, paths in halves.items() if not paths)
    if empty:
        return Route(1, "empty-half", f"{empty[0]} is named with no files")
    threat = _threat(scope.threat_model)
    routes = [_gate_half(repo, paths, scope, threat) for repo, paths in sorted(halves.items())]
    # On a tie, report the gate that fired over the fall-through default.
    return min(routes, key=lambda r: (r.path, r.gate_id == "default"))
