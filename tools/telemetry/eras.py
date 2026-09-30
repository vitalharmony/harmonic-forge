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

_GH = Path(__file__).resolve().parents[1] / "gh"
if str(_GH) not in sys.path:
    sys.path.insert(0, str(_GH))

import gate_ci  # noqa: E402

EXTRACTOR_VERSION = "threads-1"
_POSTED_BY = re.compile(r"posted-by=LANE(\d)", re.I)
#: `posted-by` is read only from inside an l1-post footer, never from prose or
#: a blockquote that quotes another comment's footer as evidence.
_FOOTER = re.compile(r"<!--\s*l1-post\b[^>]*-->", re.I)
_GATE_KEYS = {lane_state.KEY_GATE_PASS, lane_state.KEY_GATE_FAIL, lane_state.KEY_UNKNOWN}
_VERDICT_KEY = {"PASS": lane_state.KEY_GATE_PASS, "FAIL": lane_state.KEY_GATE_FAIL,
                "BLOCKED": lane_state.KEY_BLOCKED_LANE}
_TOKEN_LANE = re.compile(r"^token:L(\d)")
#: Footers `l1_post.py` stamps without `posted-by` are Lane 1's by construction.
_L1_KINDS = {"handoff", "ae", "sweep", "rework", "ready-for-l3", "discussion"}


def _actor(provenance: str, body: str) -> str:
    posted = next((m for f in _FOOTER.findall(body) for m in [_POSTED_BY.search(f)] if m), None)
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
    updated = str(comment.get("updated_at") or comment.get("created_at") or "")

    def add(key: str, provenance: str, validated: bool, at: str, cid: str, extra: dict) -> None:
        era, _, detail = provenance.partition(":")
        # `lane_state`'s token regex is anchored to a `## L<N><C>` heading, so
        # a token transition IS heading-era evidence (the pre-2026-09-09 L2
        # shape AC3 names); `marker` keeps the token spelling.
        if era == "token":
            era = "heading"
        events.append({
            "ts": at, "source": "gh-thread", "account": account, "org": org, "repo": repo,
            "issue": issue, "actor": _actor(provenance, body), "event_type": key,
            "subject_kind": "comment", "subject_id": f"comment:{cid}",
            # updated_at orders two readings of one edited comment: the
            # latest per subject_id is the current one (the store is append-only).
            "attrs": {"marker": detail, "edited_at": updated, **extra},
            "provenance": era, "validated": validated, "extractor_version": EXTRACTOR_VERSION,
        })

    is_gate = gate_ci.looks_like_a_gate_report(body)
    for t in lane_state.parse_timeline([comment]):
        if t.key in _GATE_KEYS and t.provenance.endswith("gate-result") and is_gate:
            continue  # scored below, by gate_ci's anchored reader
        add(t.key, t.provenance, t.validated, t.at, t.comment_id, {})
    # Gate verdicts come from gate_ci.verdict_of(): heading or lead block only
    # (never a per-TC line), any heading level, and CONFLICT when the two
    # disagree -- recorded as unknown, never resolved to either side.
    if is_gate:
        verdict = gate_ci.verdict_of(body)
        attested = bool(lane_state._FOOTER_KIND.search(body)) and \
            lane_state._FOOTER_KIND.search(body).group("kind").lower() == "gate-result"
        provenance = "footer:gate-result" if attested else "heading:gate-result"
        validated = attested and bool(lane_state._FOOTER_BODY_SHA.search(body))
        add(_VERDICT_KEY.get(verdict or "", lane_state.KEY_UNKNOWN), provenance, validated,
            str(comment.get("created_at") or comment.get("createdAt") or ""),
            str(comment.get("id") or ""), {"verdict": verdict or "none"})
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
