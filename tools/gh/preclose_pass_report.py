#!/usr/bin/env python3
"""Preclose passes per Tooling Exception issue (harmonic-forge#838 AC6).

Read-only. One row per preclose receipt in ``~/.claude/state/preclose``: how
many passes the issue took, whether the operator forced past the cap, how many
findings survived the last pass, and whether a post-verdict check ran. Run it
once before #838's review-the-handoff change lands and again after ten more
Tooling Exception closes, and compare the passes per issue.

A second table lists every pass that recorded its cost (harmonic-forge#889):
panel tokens, panel wall-clock, and the Codex cross-family check's wall-clock.
Neither figure includes the Lane 1 session's own orchestration cost, which a
lane session cannot read.

    preclose_pass_report.py [--dir PATH] [--since YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preclose_passes  # noqa: E402


ARCHIVE = Path.home() / ".local/share/harmonic-forge/telemetry/archive"


def archived_heads(root: Path, skipped: list[str] | None = None) -> dict[str, set[str]]:
    """Completed heads per issue from the #826 archive of overwritten receipts.
    Receipts written before #834 carry no pass history, so without this every
    such issue reads as one pass. Best-effort: an unreadable file is skipped."""
    heads: dict[str, set[str]] = {}
    for path in root.glob("**/preclose-receipts/*.jsonl.gz"):
        try:
            lines = gzip.open(path, "rt", encoding="utf-8").read().splitlines()
        except (OSError, EOFError, UnicodeDecodeError):
            if skipped is not None:
                skipped.append(str(path))
            continue
        for line in lines:
            try:
                record = json.loads(line).get("record") or {}
            except (ValueError, AttributeError):
                # A line that will not decode is a record this count misses:
                # say so, never skip silently (F838 post-verdict check).
                if skipped is not None:
                    skipped.append(f"{path}: undecodable line")
                continue
            head = preclose_passes.reviewed_head(record)
            if record.get("status") == "complete" and head:
                key = f"{record.get('repo', '?')}#{record.get('issue', '?')}"
                heads.setdefault(key, set()).add(head)
    return heads


def rows(directory: Path, since: datetime | None = None, archive: Path | None = None,
         skipped: list[str] | None = None) -> list[dict]:
    history_heads = archived_heads(archive, skipped) if archive else {}
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(receipt, dict):
            continue
        modified = datetime.fromtimestamp(os.stat(path).st_mtime, tz=timezone.utc)
        if since and modified < since:
            continue
        passes = preclose_passes.history(receipt)
        current = preclose_passes.current(passes)
        issue = f"{receipt.get('repo', '?')}#{receipt.get('issue', '?')}"
        seen = history_heads.get(issue, set()) | {p.get("sha") for p in passes if p.get("sha")}
        out.append({
            "issue": issue,
            "passes": max(len(current), len(seen)),
            "epochs": len({int(p.get("epoch") or 0) for p in passes}) or 0,
            "forced": len(current) > preclose_passes.MAX_PASSES,
            "surviving_last": int(current[-1].get("surviving") or 0) if current else 0,
            "post_verdict_check": "post_verdict_check" in receipt,
            "modified": modified.date().isoformat(),
            "costs": [_cost_row(issue, number, p) for number, p in enumerate(current, 1)],
        })
    return out


def _cost_row(issue: str, number: int, entry: dict) -> dict:
    return {"issue": issue, "pass": number, "sha": str(entry.get("sha") or "")[:12],
            "tokens": entry.get("panel_tokens"), "ms": entry.get("panel_ms"),
            "unavailable": entry.get("cost_unavailable"),
            "codex_ms": entry.get("cross_family_ms") if entry.get("cross_family_ran") else None,
            "codex_ran": bool(entry.get("cross_family_ran"))}


def _number(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) else ""


def render_costs(table: list[dict]) -> str:
    """Per-pass cost (harmonic-forge#889), with a median/total footer."""
    passes = [cost for r in table for cost in r.get("costs", [])]
    lines = ["| Issue | Pass | Head | Panel tokens | Panel ms | Codex check ms |",
             "|---|---|---|---|---|---|"]
    for c in passes:
        # n/a: cost unavailable, or a pass recorded before harmonic-forge#889.
        measured = isinstance(c["tokens"], int)
        lines.append(f"| {c['issue']} | {c['pass']} | {c['sha']} | "
                     f"{_number(c['tokens']) if measured else 'n/a'} | "
                     f"{_number(c['ms']) if measured else 'n/a'} | {_number(c['codex_ms'])} |")
    counted = [c["tokens"] for c in passes if isinstance(c["tokens"], int)]
    median = f"{statistics.median(counted):,.0f}" if counted else "n/a"
    share = f"{sum(c['codex_ran'] for c in passes)}/{len(passes)}" if passes else "0/0"
    lines += ["", f"{len(passes)} pass(es); panel tokens median {median}, "
                  f"total {sum(counted):,} over {len(counted)} measured; Codex check ran on {share}. "
                  "Excludes the Lane 1 session's own orchestration cost, which it cannot read."]
    return "\n".join(lines)


def render(table: list[dict]) -> str:
    lines = ["| Issue | Passes (completed heads) | Epochs | Forced past cap | Surviving at last pass | Post-verdict check | Receipt date |",
             "|---|---|---|---|---|---|---|"]
    for r in table:
        lines.append(f"| {r['issue']} | {r['passes']} | {r['epochs']} | {'yes' if r['forced'] else 'no'} | "
                     f"{r['surviving_last']} | {'yes' if r['post_verdict_check'] else 'no'} | {r['modified']} |")
    counted = [r["passes"] for r in table]
    mean = sum(counted) / len(counted) if counted else 0.0
    lines += ["", f"{len(table)} issue(s); mean passes per issue {mean:.2f}; "
                  f"{sum(1 for r in table if r['passes'] >= 2)} took two or more; "
                  f"{sum(1 for r in table if r['forced'])} forced past the cap."]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=Path.home() / ".claude" / "state" / "preclose")
    parser.add_argument("--archive", type=Path, default=ARCHIVE,
                        help="The #826 receipt archive, for pass counts before #834's history.")
    parser.add_argument("--since", help="Only receipts written on or after this date (YYYY-MM-DD).")
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else None
    print(report(args.dir, since, args.archive))


def report(directory: Path, since: datetime | None, archive: Path) -> str:
    """The table plus any undercount warning, all on stdout, so a report
    redirected to a file carries its own caveat (F838 sticky-wicket PATCH)."""
    skipped: list[str] = []
    found = rows(directory, since, archive, skipped)
    table = render(found) + "\n\n" + render_costs(found)
    warnings = []
    if not any(archive.glob("**/preclose-receipts/*.jsonl.gz")):
        warnings.append(f"WARNING: no receipt archive under {archive}; issues closed before "
                        "harmonic-forge#834 are counted as one pass each (an undercount).")
    if skipped:
        warnings.append(f"WARNING: {len(skipped)} archive file(s) or line(s) could not be read; "
                        "pass counts for issues they held may be undercounted.")
    return "\n".join(warnings + ([""] if warnings else []) + [table])


if __name__ == "__main__":
    main()
