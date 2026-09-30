#!/usr/bin/env python3
"""Preclose passes per Tooling Exception issue (harmonic-forge#838 AC6).

Read-only. One row per preclose receipt in ``~/.claude/state/preclose``: how
many passes the issue took, whether the operator forced past the cap, how many
findings survived the last pass, and whether a post-verdict check ran. Run it
once before #838's review-the-handoff change lands and again after ten more
Tooling Exception closes, and compare the passes per issue.

    preclose_pass_report.py [--dir PATH] [--since YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preclose_passes  # noqa: E402


ARCHIVE = Path.home() / ".local/share/harmonic-forge/telemetry/archive"


def archived_heads(root: Path) -> dict[str, set[str]]:
    """Completed heads per issue from the #826 archive of overwritten receipts.
    Receipts written before #834 carry no pass history, so without this every
    such issue reads as one pass. Best-effort: an unreadable file is skipped."""
    heads: dict[str, set[str]] = {}
    for path in root.glob("**/preclose-receipts/*.jsonl.gz"):
        try:
            lines = gzip.open(path, "rt", encoding="utf-8").read().splitlines()
        except (OSError, EOFError):
            continue
        for line in lines:
            try:
                record = json.loads(line).get("record") or {}
            except (ValueError, AttributeError):
                continue
            head = preclose_passes.reviewed_head(record)
            if record.get("status") == "complete" and head:
                key = f"{record.get('repo', '?')}#{record.get('issue', '?')}"
                heads.setdefault(key, set()).add(head)
    return heads


def rows(directory: Path, since: datetime | None = None, archive: Path | None = None) -> list[dict]:
    history_heads = archived_heads(archive) if archive else {}
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
        })
    return out


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
    if not any(args.archive.glob("**/preclose-receipts/*.jsonl.gz")):
        # F838 preclose pass 1: without the archive, every pre-#834 issue reads
        # as one pass, which flatters the baseline -- say so, never silently.
        print(f"WARNING: no receipt archive under {args.archive}; issues closed before "
              "harmonic-forge#834 are counted as one pass each (an undercount).", file=sys.stderr)
    print(render(rows(args.dir, since, args.archive)))


if __name__ == "__main__":
    main()
