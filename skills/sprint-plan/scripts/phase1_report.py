"""hrse#917 phase 1 — measurement, not selection.

Runs all six candidates (`phase1_candidates.py`) against every mention
(`phase1_mentions.py`) in both coverage modes and emits two artifacts:

- `adjudication.csv` — the deduplicated union of mentions any candidate
  flagged under `whole-h2` coverage, for Lane 1 to mark
  genuine-drift/honest-history/ambiguous.
- `matrix.csv` — every (candidate, coverage, mention) row, scoring data only.

Never edits `docs/PRIORITIES.md`; never calls `main()` or
`branches_ahead_of_main()` (which mutates git refs). `stale_closed_mentions()`
itself is untouched — this module is additive and exploratory.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Optional

from phase1_candidates import CANDIDATE_IDS, CandidateResult, evaluate_all
from phase1_mentions import Mention, _BLOCK_START, context, extract_mentions

# "legacy" (today's first-bullet-to-EOF slice) is diagnostic only — it can
# never be the selected coverage mode, only evidence that today's blind spot
# is real. Only "whole-h2" (offset 0..EOF) is eligible for selection, and
# `build_adjudication_rows` below enforces that by construction.
COVERAGE_MODES = ("legacy", "whole-h2")

_HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = _HERE / "phase1_output"


def legacy_scan_start(text: str) -> Optional[int]:
    """Offset of the first `_BLOCK_START` match — what today's `_blocks()`
    treats as the start of scannable text. `None` if the doc has no bullets
    at all (nothing is in scope under `legacy`)."""
    m = _BLOCK_START.search(text)
    return m.start() if m else None


def coverage_summary(text: str, mode: str) -> dict:
    total = len(text)
    if mode == "whole-h2":
        return {"mode": mode, "total_chars": total, "scanned_chars": total, "unscanned_ranges": []}
    start = legacy_scan_start(text)
    if start is None:
        return {"mode": mode, "total_chars": total, "scanned_chars": 0, "unscanned_ranges": [(0, total)]}
    return {
        "mode": mode,
        "total_chars": total,
        "scanned_chars": total - start,
        "unscanned_ranges": [(0, start)],
    }


def _in_scope(mention: Mention, mode: str, legacy_start: Optional[int]) -> bool:
    if mode == "whole-h2":
        return True
    return legacy_start is not None and mention.start >= legacy_start


def live_state_of(mention: Mention, closed_by_repo: dict[str, set[int]]) -> str:
    return "closed" if mention.issue in closed_by_repo.get(mention.repo, set()) else "open"


def _line_col(mention: Mention) -> str:
    return f"{mention.line}:{mention.col}"


def build_rows(
    text: str, mentions: list[Mention], closed_by_repo: dict[str, set[int]]
) -> tuple[list[dict], dict[str, dict]]:
    """Returns (matrix_rows, coverage_summaries). Candidate evaluation is
    computed once (evidence-scoping doesn't depend on coverage mode); only
    which rows are reported as EXCLUDE(unscanned) varies by mode."""
    by_candidate = evaluate_all(mentions, text)
    legacy_start = legacy_scan_start(text)
    coverages = {mode: coverage_summary(text, mode) for mode in COVERAGE_MODES}

    rows: list[dict] = []
    for mode in COVERAGE_MODES:
        for mention in mentions:
            live_state = "n/a" if mention.is_board_link else live_state_of(mention, closed_by_repo)
            base = {
                "coverage": mode,
                "mention_id": mention.mention_id,
                "repo": mention.repo,
                "issue": mention.issue,
                "live_state": live_state,
                "line_col": _line_col(mention),
                "heading": mention.heading,
                "unit_kind": mention.unit_kind,
                "role": mention.role,
                "context": context(text, mention.start, mention.end),
            }
            if not _in_scope(mention, mode, legacy_start):
                for cid in CANDIDATE_IDS:
                    rows.append({**base, "rule": cid, **_excluded("unscanned")})
                continue
            if mention.is_board_link:
                for cid in CANDIDATE_IDS:
                    rows.append({**base, "rule": cid, **_excluded("board-link")})
                continue
            if live_state != "closed":
                for cid in CANDIDATE_IDS:
                    rows.append({**base, "rule": cid, **_excluded("open-issue")})
                continue
            for cid in CANDIDATE_IDS:
                result = by_candidate[cid][mention.mention_id]
                rows.append({**base, "rule": cid, **_from_result(result)})
    return rows, coverages


def _excluded(reason: str) -> dict:
    return {
        "outcome": "EXCLUDE",
        "reason_code": reason,
        "evidence_token": None,
        "evidence_direction": None,
        "evidence_span": None,
    }


def _from_result(result: CandidateResult) -> dict:
    return {
        "outcome": result.outcome,
        "reason_code": result.reason_code,
        "evidence_token": result.evidence_token,
        "evidence_direction": result.evidence_direction,
        "evidence_span": result.evidence_span,
    }


def build_adjudication_rows(matrix_rows: list[dict]) -> list[dict]:
    """Union of distinct mentions flagged by any candidate under `whole-h2`
    (the only selectable coverage), deduplicated by `mention_id`."""
    seen: dict[str, dict] = {}
    for row in matrix_rows:
        if row["coverage"] != "whole-h2" or row["outcome"] != "FLAG":
            continue
        if row["mention_id"] in seen:
            continue
        seen[row["mention_id"]] = {
            "mention_id": row["mention_id"],
            "repo": row["repo"],
            "issue": row["issue"],
            "live_state": row["live_state"],
            "line_col": row["line_col"],
            "heading": row["heading"],
            "unit_kind": row["unit_kind"],
            "role": row["role"],
            "context": row["context"],
            "lane1_adjudication": "",
        }
    return sorted(seen.values(), key=lambda r: (r["repo"], r["issue"], r["mention_id"]))


MATRIX_FIELDS = [
    "rule", "coverage", "mention_id", "repo", "issue", "live_state", "line_col",
    "heading", "unit_kind", "role", "context", "evidence_token", "evidence_direction",
    "evidence_span", "outcome", "reason_code",
]
ADJUDICATION_FIELDS = [
    "mention_id", "repo", "issue", "live_state", "line_col", "heading",
    "unit_kind", "role", "context", "lane1_adjudication",
]


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Elimination gates (see module docstring in phase1_candidates.py). Gate (v)
# — bullet-insertion variance — needs multiple documents (base + variants)
# and lives in test_phase1_report.py, which reuses `mention_signature` below.
# ---------------------------------------------------------------------------


def mention_signature(row: dict) -> tuple:
    """Offset-independent identity for a (mention, candidate) outcome —
    what bullet-insertion invariance (gate v) compares."""
    return (
        row["repo"], row["issue"], row["mention_id"].rsplit(":", 1)[-1],
        row["heading"], row["unit_kind"], row["role"], row["outcome"], row["reason_code"],
    )


def elimination_report(
    matrix_rows: list[dict],
    must_flag_ids: set[str],
    must_clear_ids: set[str],
    coverage_summaries: dict[str, dict],
) -> dict[str, dict]:
    """Per-candidate PASS/FAIL against gates (i)-(iv). Gate (v) is not
    included — see the module docstring."""
    by_candidate: dict[str, dict[str, str]] = {cid: {} for cid in CANDIDATE_IDS}
    for row in matrix_rows:
        if row["coverage"] != "whole-h2":
            continue
        by_candidate[row["rule"]][row["mention_id"]] = row["outcome"]

    whole_h2_complete = coverage_summaries["whole-h2"]["scanned_chars"] == coverage_summaries["whole-h2"]["total_chars"]

    report: dict[str, dict] = {}
    for cid in CANDIDATE_IDS:
        outcomes = by_candidate[cid]
        failures = []
        cleared_canary = [mid for mid in must_flag_ids if outcomes.get(mid) == "CLEAR"]
        if cleared_canary:
            failures.append(f"gate i/ii: canary(s) incorrectly cleared: {sorted(cleared_canary)}")
        flagged_bound = [mid for mid in must_clear_ids if outcomes.get(mid) == "FLAG"]
        if flagged_bound:
            failures.append(f"gate iii: bound claim(s) incorrectly flagged: {sorted(flagged_bound)}")
        if not whole_h2_complete:
            failures.append("gate iv: whole-h2 coverage left characters unscanned")
        report[cid] = {"passed": not failures, "failures": failures}
    return report


def run_phase1_report(
    doc_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    closed_by_repo: Optional[dict[str, set[int]]] = None,
) -> int:
    """The `--phase1-report` CLI body. Read-only against `doc_path`; never
    calls `main()`."""
    from drift_check import DOC_PATH, REPOS, closed_issues

    doc_path = doc_path or DOC_PATH
    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    if closed_by_repo is None:
        closed_by_repo = {repo: closed_issues(repo) for repo in REPOS}

    text = doc_path.read_text(encoding="utf-8")
    mentions = extract_mentions(text)
    matrix_rows, coverages = build_rows(text, mentions, closed_by_repo)
    adjudication_rows = build_adjudication_rows(matrix_rows)

    hrse_860 = {m.mention_id for m in mentions if m.repo == "vitalharmony/hrse" and m.issue == 860}
    hrse_848_canonical = {
        m.mention_id for m in mentions
        if m.repo == "vitalharmony/hrse" and m.issue == 848 and m.occurrence == 1
    }
    elimination = elimination_report(matrix_rows, hrse_860, hrse_848_canonical, coverages)

    write_csv(output_dir / "matrix.csv", MATRIX_FIELDS, matrix_rows)
    write_csv(output_dir / "adjudication.csv", ADJUDICATION_FIELDS, adjudication_rows)

    print(f"phase1: {len(mentions)} mentions extracted, {len(matrix_rows)} matrix rows")
    print(f"phase1: {len(adjudication_rows)} distinct mentions need adjudication (whole-h2)")
    for mode in COVERAGE_MODES:
        c = coverages[mode]
        print(f"phase1: coverage[{mode}] = {c['scanned_chars']}/{c['total_chars']} chars, "
              f"unscanned={c['unscanned_ranges']}")
    for cid, result in elimination.items():
        status = "PASS" if result["passed"] else "FAIL"
        print(f"phase1: elimination[{cid}] = {status}" + (f" — {result['failures']}" if result["failures"] else ""))
    # NC2: no survivor is a valid, reportable result — never invent a
    # seventh candidate or relax a gate to manufacture one.
    if not any(r["passed"] for r in elimination.values()):
        print("phase1: NO CANDIDATE survived every gate — this is a valid "
              "phase 1 result, not a failure to escalate around.")
    print(f"phase1: artifacts written to {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(run_phase1_report())
