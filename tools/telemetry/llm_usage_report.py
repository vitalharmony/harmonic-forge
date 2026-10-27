#!/usr/bin/env python3
"""Reports over `source=llm-usage` and `source=operator-attention` events
(harmonic-forge#1188 AC9, AC10, AC11, AC12). Read-only over the store.

  report               tokens (uncached, cached read, cache write, output kept
                       separate), estimated cost and validated attention
                       minutes per issue and per lane; unassigned is its own
                       row; the unassigned share is token-weighted (AC9).
  audit --sample N     N attributed calls beside the evidence an auditor checks
                       them against: the sidecar line joined and the session's
                       other sidecar lines (AC10). The operator marks each.
  attention-compare --timings CSV
                       formula minutes per day against the operator's own
                       timing (CSV `date,minutes`), AC11. Attention events stay
                       validated=false; `report` never reads them until then.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import attrib  # noqa: E402
import emit  # noqa: E402

TOKEN_FIELDS = ("input_uncached", "cached_read", "cache_write", "output")
UNASSIGNED = "unassigned"


def load(source: str, root: Optional[Path] = None) -> list[dict]:
    root = root or emit.store_root()
    events = []
    for part in sorted((root / "events").glob(f"*/*/{source}/*.jsonl")):
        with part.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    return events


def _issue_key(event: dict) -> str:
    if event.get("issue") is None:
        return UNASSIGNED
    return f"{event.get('repo')}#{event['issue']}"


def _tokens(event: dict) -> int:
    attrs = event.get("attrs") or {}
    return sum(int(attrs.get(f) or 0) for f in TOKEN_FIELDS)


def summarize(usage: Iterable[dict], attention: Iterable[dict]) -> dict:
    """{"issue": {key: row}, "lane": {key: row}, "unassigned_share": float}."""
    def row() -> dict:
        return {**{f: 0 for f in TOKEN_FIELDS}, "calls": 0, "est_cost_usd": 0.0,
                "unpriced_calls": 0, "attention_min": 0.0}
    out = {"issue": defaultdict(row), "lane": defaultdict(row)}
    total = unassigned = 0
    for e in usage:
        attrs = e.get("attrs") or {}
        keys = (("issue", _issue_key(e)), ("lane", str(attrs.get("lane") or UNASSIGNED)))
        tokens = _tokens(e)
        total += tokens
        if e.get("issue") is None:
            unassigned += tokens
        for dim, key in keys:
            r = out[dim][key]
            r["calls"] += 1
            for f in TOKEN_FIELDS:
                r[f] += int(attrs.get(f) or 0)
            cost = attrs.get("est_cost_usd")
            if isinstance(cost, (int, float)) and cost >= 0:
                r["est_cost_usd"] = round(r["est_cost_usd"] + cost, 6)
            else:
                r["unpriced_calls"] += 1
    for e in attention:
        if e.get("validated") is not True:  # AC11: unvalidated minutes are never reported
            continue
        attrs = e.get("attrs") or {}
        minutes = float(attrs.get("minutes") or 0)
        out["issue"][_issue_key(e)]["attention_min"] += minutes
        out["lane"][str(attrs.get("lane") or UNASSIGNED)]["attention_min"] += minutes
    return {"issue": dict(out["issue"]), "lane": dict(out["lane"]),
            "unassigned_share": round(unassigned / total, 4) if total else 0.0}


def render(summary: dict) -> str:
    cols = ("calls",) + TOKEN_FIELDS + ("est_cost_usd", "unpriced_calls", "attention_min")
    lines = []
    for dim in ("issue", "lane"):
        lines.append(f"## per {dim}  (est_cost_usd is a list-price estimate)")
        lines.append("\t".join((dim,) + cols))
        for key in sorted(summary[dim], key=lambda k: (k == UNASSIGNED, k)):
            r = summary[dim][key]
            lines.append("\t".join([key] + [str(r[c]) for c in cols]))
        lines.append("")
    lines.append(f"unassigned share of tokens: {summary['unassigned_share']:.2%}")
    lines.append("attention minutes: validated events only (none until the AC11 comparison is posted)")
    return "\n".join(lines)


def audit_sample(usage: list[dict], n: int, seed: Optional[int] = None) -> list[dict]:
    """N explicitly attributed calls, each with its evidence (AC10)."""
    attributed = [e for e in usage if (e.get("attrs") or {}).get("attribution") == "explicit"]
    picked = random.Random(seed).sample(attributed, min(n, len(attributed)))
    rows = []
    for e in picked:
        attrs = e.get("attrs") or {}
        session = attrs.get("session_id") or ""
        rows.append({
            "ts": e.get("ts"), "provider": attrs.get("provider"), "session_id": session,
            "joined_issue": _issue_key(e), "lane": attrs.get("lane"),
            "evidence_trigger": attrs.get("attrib_trigger"), "evidence_ts": attrs.get("attrib_ts"),
            # Evidence outside the sidecar's own claim: the posted comment's
            # URL and the transcript holding the trigger prompt.
            "session_sidecar": [{k: line.get(k) for k in ("ts", "issue", "lane", "trigger",
                                                          "comment_url", "transcript_path")
                                 if line.get(k) is not None}
                                for line in attrib.read(session)],
            "verdict": "",  # the auditor marks right/wrong
        })
    return rows


def attention_compare(attention: list[dict], timings: Path) -> list[dict]:
    formula: dict[str, float] = defaultdict(float)
    for e in attention:
        formula[str(e.get("ts", ""))[:10]] += float((e.get("attrs") or {}).get("minutes") or 0)
    rows = []
    with timings.open(encoding="utf-8") as handle:
        for rec in csv.DictReader(handle):
            day, measured = rec.get("date", ""), float(rec.get("minutes") or 0)
            est = round(formula.get(day, 0.0), 2)
            rows.append({"date": day, "measured_min": measured, "formula_min": est,
                         "ratio": round(est / measured, 3) if measured else None})
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report")
    a = sub.add_parser("audit")
    a.add_argument("--sample", type=int, default=50)
    a.add_argument("--seed", type=int)
    c = sub.add_parser("attention-compare")
    c.add_argument("--timings", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "report":
        print(render(summarize(load("llm-usage"), load("operator-attention"))))
    elif args.cmd == "audit":
        for row in audit_sample(load("llm-usage"), args.sample, args.seed):
            print(json.dumps(row, sort_keys=True))
    else:
        for row in attention_compare(load("operator-attention"), args.timings):
            print(json.dumps(row, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
