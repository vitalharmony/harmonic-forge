#!/usr/bin/env python3
"""Era-aware lane-marker parsing for the thread extractor (harmonic-forge#829).

The markers themselves are `lane_state.py`'s, imported rather than copied
(design.md §6 T1): `parse_timeline()` already reads footer -> heading ->
token, in that authority order, with `provenance` and `validated` on every
transition. This module turns those transitions into schema-v1 events and
adds the one thing `lane_state` cannot do:

**Gate verdicts in the `**Verdict:**` line.** Since harmonic-forge#473 a
Lane 3 gate result carries its verdict in a `**Verdict:** PASS` lead line,
not in the heading, so `lane_state` renders every modern gate as `unknown`
(measured live: both gates on hrse#1866). A FAIL->PASS round is then
invisible, which is the one thing AC2 asks to see. The verdict line is read
only for a comment `lane_state` already placed as a gate result, so it can
never invent a gate.

Payloads carry ids, kinds and timestamps. The comment body is read to find
markers and is never copied into an event.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Optional

_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "sprint-plan" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import lane_state  # noqa: E402

EXTRACTOR_VERSION = "threads-1"
#: Both live shapes: `**Verdict:** PASS` and `**Verdict: PASS**`.
_VERDICT = re.compile(r"(?im)^\s*\**\s*Verdict\s*:?\s*\**\s*:?\s*\**\s*(PASS|FAIL|BLOCKED)\b")
_POSTED_BY = re.compile(r"posted-by=LANE(\d)", re.I)
_TOKEN_LANE = re.compile(r"^token:L(\d)")
#: Footers `l1_post.py` stamps without `posted-by` are Lane 1's by construction.
_L1_KINDS = {"handoff", "ae", "sweep", "rework", "ready-for-l3", "discussion"}


def _actor(provenance: str, body: str) -> str:
    posted = _POSTED_BY.search(body)
    if posted:
        return f"lane{posted.group(1)}"
    token = _TOKEN_LANE.match(provenance)
    if token:
        return f"lane{token.group(1)}"
    kind = provenance.split(":", 1)[1] if ":" in provenance else ""
    if provenance.startswith("footer:") and kind in _L1_KINDS:
        return "lane1"
    if kind == "gate-result" or kind == "spec":
        return "lane3"
    return "unset"


def comment_events(comment: dict[str, Any], *, account: Optional[str], org: Optional[str],
                   repo: str, issue: int) -> list[dict[str, Any]]:
    """Schema-v1 events for one REST comment. Never includes its body."""
    raw = comment.get("body") or ""
    body = lane_state._FENCE.sub("", raw)
    events = []
    for t in lane_state.parse_timeline([comment]):
        key, validated = t.key, t.validated
        if key == lane_state.KEY_UNKNOWN and t.provenance.endswith("gate-result"):
            verdict = _VERDICT.search(body)
            if verdict and verdict.group(1) != "BLOCKED":
                key = lane_state.KEY_GATE_PASS if verdict.group(1) == "PASS" else lane_state.KEY_GATE_FAIL
            elif verdict:
                key = lane_state.KEY_BLOCKED_LANE
        era, _, detail = t.provenance.partition(":")
        # `lane_state`'s token regex is anchored to a `## L<N><C>` heading, so
        # a token transition IS heading-era evidence (the pre-2026-09-09 L2
        # shape AC3 names); `marker` keeps the token spelling.
        if era == "token":
            era = "heading"
        events.append({
            "ts": t.at, "source": "gh-thread", "account": account, "org": org, "repo": repo,
            "issue": issue, "actor": _actor(t.provenance, body), "event_type": key,
            "subject_kind": "comment", "subject_id": f"comment:{t.comment_id}",
            "attrs": {"marker": detail}, "provenance": era, "validated": validated,
            "extractor_version": EXTRACTOR_VERSION,
        })
    return events


#: REST timeline event -> event_type. Anything else is not a lane-flow fact.
_TIMELINE_TYPES = {"closed": "issue.closed", "reopened": "issue.reopened",
                   "labeled": "label.added", "unlabeled": "label.removed",
                   "milestoned": "milestone.set", "demilestoned": "milestone.removed",
                   "cross-referenced": "cross-referenced", "connected": "pr.linked"}


def timeline_events(item: dict[str, Any], *, account: Optional[str], org: Optional[str],
                    repo: str, issue: int) -> list[dict[str, Any]]:
    event_type = _TIMELINE_TYPES.get(item.get("event") or "")
    if event_type is None:
        return []
    attrs: dict[str, Any] = {}
    if item.get("label"):
        attrs["label"] = str(item["label"].get("name", ""))[:100]
    if item.get("milestone"):
        attrs["milestone"] = str(item["milestone"].get("title", ""))[:100]
    if item.get("commit_id"):
        attrs["sha"] = item["commit_id"]
    subject = f"timeline:{item['id']}" if item.get("id") else ""
    source = (item.get("source") or {}).get("issue") or {}
    if source:
        src_repo = (source.get("repository") or {}).get("full_name", "")
        attrs["source_repo"] = src_repo
        attrs["source_number"] = source.get("number")
        attrs["source_is_pr"] = bool(source.get("pull_request"))
        subject = subject or f"xref:{src_repo}#{source.get('number')}"
    # Every lane and the operator act through one login per account, so the
    # login cannot say who acted; guessing "operator" would be a fabrication.
    return [{
        "ts": item.get("created_at"), "source": "gh-timeline", "account": account, "org": org,
        "repo": repo, "issue": issue, "actor": "unset",
        "event_type": event_type, "subject_kind": "timeline",
        "subject_id": subject or f"timeline:{event_type}:{item.get('created_at')}",
        "attrs": attrs, "provenance": "timeline", "validated": True,
        "extractor_version": EXTRACTOR_VERSION,
    }]
