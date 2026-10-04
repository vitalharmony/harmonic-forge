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
invisible, which is the one thing AC2 asks to see. A comment is scored as a
gate only when its OWN footer says `kind=gate-result`, or, with no footer at
all, when `lane_state` already placed it as a gate. A `### Lane 3 Gate
Results` recap inside a Lane 1 discussion is therefore never a gate.

**Quoted markers never count** (`lane-shorthand.md`: "a marker quoted as
evidence never counts as a transition"). Fenced blocks AND blockquoted lines
are stripped before any marker is read, by this module and by the
`lane_state` pass it feeds, and `posted-by`/`kind` come only from the
comment's own footer: the LAST footer standing on a line of its own, which is
where `l1_post.py` appends it.

**One row per reading.** The store is append-only and `emit()` dedupes on
`event_id`, which hashes `subject_id`. So `subject_id` names the reading --
`comment:<id>@<updated_at>/<extractor_version>` -- and `attrs.comment_id`
names the comment. An edited comment, or a parser fix, yields new rows rather
than being discarded as a duplicate; the current reading of a comment is the
rows with its highest `extractor_version`, then its latest `attrs.edited_at`.
Known gap, stated rather than implied closed: an edit that REMOVES every
marker, or a deleted comment, leaves the earlier rows standing -- there is no
tombstone event.

Payloads carry ids, kinds and timestamps. The comment body is read to find
markers and is never copied into an event.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
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

EXTRACTOR_VERSION = "threads-4"
#: `post_lane_discussion.py` stamps `posted-by=LANE-unset` when LANE is unset.
_POSTED_BY = re.compile(r"posted-by=LANE(\d|-unset)\b", re.I)
#: A comment's own footer stands on a line of its own; the last one is l1_post's.
_OWN_FOOTER = re.compile(r"^[ \t]*(<!--\s*l1-post\b[^>]*-->)[ \t]*$", re.I | re.M)
_QUOTED = re.compile(r"^[ \t]*>.*$", re.M)
_GATE_KEYS = {lane_state.KEY_GATE_PASS, lane_state.KEY_GATE_FAIL, lane_state.KEY_UNKNOWN}
_VERDICT_KEY = {"PASS": lane_state.KEY_GATE_PASS, "FAIL": lane_state.KEY_GATE_FAIL,
                "BLOCKED": lane_state.KEY_BLOCKED_LANE}
_TOKEN_LANE = re.compile(r"^token:L(\d)")
#: Footers `l1_post.py` stamps without `posted-by` are Lane 1's by construction.
_L1_KINDS = {"handoff", "ae", "sweep", "rework", "ready-for-l3", "discussion"}


def _now() -> str:
    """The extraction instant, second precision (a seam for tests)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def clean(raw: str) -> str:
    """The body with fenced blocks and blockquoted lines removed."""
    return _QUOTED.sub("", lane_state._FENCE.sub("", raw))


def own_footer(body: str) -> Optional[str]:
    found = _OWN_FOOTER.findall(body)
    return found[-1] if found else None


#: harmonic-forge#893: `classes=1:ac,2:live` on a spec footer, `results=1:pass,…`
#: on a gate-result footer.
_CASE_FIELD = r"\b{key}=([\w:,-]*)"


#: harmonic-forge#893 reforge (R4): every gate event carries exactly one of these,
#: stamped here and by the pairing step, never inferred later from which attrs
#: happen to be present.
MEASUREMENTS = ("measured", "unpaired", "no-map", "no-cases", "pre-893", "bypass-route")


def case_field_state(footer: Optional[str], key: str) -> str:
    """`missing` (no such field: an older post or another route), `absent`
    (the post went out without a usable map), `empty` (a map with no cases) or
    `present`."""
    found = re.findall(_CASE_FIELD.format(key=re.escape(key)), footer or "")
    if not found:
        return "missing"
    value = found[-1]
    return "absent" if value == "absent" else "empty" if not value else "present"


def parse_case_map(footer: Optional[str], key: str) -> dict[str, str]:
    """`{case id: value}` from a footer's `key=` field, or `{}` when absent."""
    # The LAST match: the case fields are appended after every other key, and
    # an earlier free-text value (an ack reason) must not shadow them.
    found = re.findall(_CASE_FIELD.format(key=re.escape(key)), footer or "")
    if not found:
        return {}
    if found[-1] == "absent":
        return {}
    pairs = (item.partition(":") for item in found[-1].split(",") if item)
    return {k: v for k, _, v in pairs if k and v}


def case_counts(footer: Optional[str], kind: Optional[str]) -> dict[str, Any]:
    """Scalar attrs only (schema v1 forbids nesting): class counts for a spec,
    verdict counts and gate time for a gate result. `{}` for a footer without
    the fields, so an old-format post is unchanged."""
    if kind == "spec":
        classes = parse_case_map(footer, "classes")
        if not classes:
            return {}
        values = list(classes.values())
        return {f"tc_{c}": values.count(c) for c in ("ac", "existing", "live")}
    if kind == "gate-result":
        results = parse_case_map(footer, "results")
        if not results:
            return {}
        values = list(results.values())
        return {"tc_count": len(values), "fail_count": values.count("fail"),
                "blocked_count": values.count("blocked")}
    return {}


def gate_measurement(footer: Optional[str], verdict: Optional[str]) -> str:
    """One stamped state per gate (R4). `unpaired` is set later, by pairing."""
    if footer is None:
        return "bypass-route"
    state = case_field_state(footer, "results")
    if state == "missing":
        return "pre-893"
    if state == "absent":
        return "no-map"
    if state == "empty":
        # An explicitly supplied empty map, whatever the verdict line reads
        # (#893 e1 sticky-wicket #3): a CONFLICT verdict must not turn it into
        # "no map". The report's verdict column carries the verdict.
        return "no-cases"
    return "measured"


def map_agrees(counts: dict[str, Any], verdict: Optional[str], body: str) -> bool:
    """A soft data-quality check, never a refusal (R1): does the stamped map
    agree with the report's own verdict? Stamped data only. The prose is never
    read for case ids (#893 e1 sticky-wicket #2: a prose count re-admitted the
    epoch-0 root category and flagged correct FAIL reports)."""
    tc, fails, blocked = counts.get("tc_count", 0), counts.get("fail_count", 0), counts.get("blocked_count", 0)
    if verdict == "PASS" and (fails or blocked):
        return False
    if verdict == "FAIL" and not fails:
        return False
    if verdict == "BLOCKED" and not blocked:
        return False
    return True


#: A post by any known poster other than Lane 3 relays or recaps a gate (#893
#: e1 sticky-wicket #6): Lane 1, Lane 2 and LANE-unset.
_POSTED_BY_L1_L2 = re.compile(r"posted-by=LANE(?!3\b)[\w-]+", re.I)
_POSTED_BY_ANY = re.compile(r"posted-by=(LANE[\w-]+)", re.I)


def lane3_artifact_of(body: str, own_kind: Optional[str], footer: Optional[str]) -> Optional[str]:
    """`gate_ci.lane3_artifact` over a stored comment: its footer kind (when
    it names an artifact) and its poster."""
    poster = _POSTED_BY_ANY.search(footer or "")
    return gate_ci.lane3_artifact(body, own_kind if own_kind in ("gate-result", "spec") else None,
                                  poster.group(1) if poster else None)


def _actor(provenance: str, footer: Optional[str]) -> str:
    posted = _POSTED_BY.search(footer or "")
    if posted:
        return "unset" if posted.group(1) == "-unset" else f"lane{posted.group(1)}"
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
    body = clean(comment.get("body") or "")
    footer = own_footer(body)
    kind = lane_state._FOOTER_KIND.search(footer or "")
    own_kind = kind.group("kind").lower() if kind else None
    events: list[dict[str, Any]] = []
    updated = str(comment.get("updated_at") or comment.get("created_at") or "")
    # When this reading was taken: the tiebreak that lets a re-extraction
    # displace an older reading of the same comment and version (#893 e1 #8).
    extracted_at = _now()

    def add(key: str, provenance: str, validated: bool, at: str, cid: str, extra: dict) -> None:
        era, _, detail = provenance.partition(":")
        # `lane_state`'s token regex is anchored to a `## L<N><C>` heading, so
        # a token transition IS heading-era evidence (the pre-2026-09-09 L2
        # shape AC3 names); `marker` keeps the token spelling.
        if era == "token":
            era = "heading"
        events.append({
            "ts": at, "source": "gh-thread", "account": account, "org": org, "repo": repo,
            "issue": issue, "actor": _actor(provenance, footer), "event_type": key,
            "subject_kind": "comment",
            "subject_id": f"comment:{cid}@{updated}/{EXTRACTOR_VERSION}",
            "attrs": {"marker": detail, "comment_id": cid, "edited_at": updated,
                      "extracted_at": extracted_at, **extra},
            "provenance": era, "validated": validated, "extractor_version": EXTRACTOR_VERSION,
        })

    timeline = lane_state.parse_timeline([{**comment, "body": body}])
    lane_gates = [t for t in timeline if t.key in _GATE_KEYS]
    # The shared recognizer (#893 reforge pass 1), the same test the posting
    # side applies, so the two can never disagree on what a post is.
    artifact = lane3_artifact_of(body, own_kind, footer)
    relay = bool(_POSTED_BY_L1_L2.search(footer or ""))
    # The shared recognizer is the sole authority (#893 e1 sticky-wicket #5):
    # a second, stricter heading test here let an indented or fenced heading
    # be stamped by the poster and then dropped by the extractor.
    is_gate = artifact == "gate" or (own_kind is None and footer is None and bool(lane_gates))
    for t in timeline:
        if (is_gate or relay) and t.key in _GATE_KEYS:
            # One gate event per comment, scored below; a Lane 1/2 relay of a
            # gate is never a gate reading at all.
            continue
        extra = (case_counts(footer, "spec")
                 if artifact == "spec" and t.key == lane_state.KEY_SPEC_POSTED else {})
        add(t.key, t.provenance, t.validated, t.at, t.comment_id, extra)
    # Gate verdicts come from gate_ci.verdict_of(): heading or lead block only
    # (never a per-TC line), any heading level, and CONFLICT when the two
    # disagree -- recorded as unknown, never resolved to either side. With no
    # verdict line (a token-era `## L3P` gate), lane_state's reading stands.
    if is_gate:
        verdict = gate_ci.verdict_of(body)
        attested = own_kind == "gate-result"
        provenance = "footer:gate-result" if attested else "heading:gate-result"
        validated = attested and bool(lane_state._FOOTER_BODY_SHA.search(footer or ""))
        key = _VERDICT_KEY.get(verdict or "")
        definite = [t for t in lane_gates if t.key != lane_state.KEY_UNKNOWN]
        if key is None and verdict != "CONFLICT" and definite:
            key, provenance, validated = definite[0].key, definite[0].provenance, definite[0].validated
        counts = case_counts(footer, "gate-result")
        extra: dict[str, Any] = {"verdict": verdict or "none",
                                 "measurement": gate_measurement(footer, verdict), **counts}
        if counts:
            extra["map_agrees"] = map_agrees(counts, verdict, body)
        add(key or lane_state.KEY_UNKNOWN, provenance, validated,
            str(comment.get("created_at") or comment.get("createdAt") or ""),
            str(comment.get("id") or ""), extra)
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
