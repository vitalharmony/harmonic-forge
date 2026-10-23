#!/usr/bin/env python3
"""PR <-> issue join, coverage and F739 medians (harmonic-forge#1167, F637 T2b).

Derive only: reads harmonic-forge#1166's observation events from the store,
registers them as one `events` table in an in-memory DuckDB, and runs
`views/t2_pr_issue_join.sql`. It writes nothing to the store. Each merged PR
is `joined`, `unjoined` or `unresolved`; coverage is joined / (joined +
unjoined) per repo and month with unresolved reported beside it, and the
medians are reported only at 20 or more pairs. Nothing is stamped, so a later
observation changes the answer on the next run.

Until harmonic-forge#831 owns the store's reader, this runner's registration
is the one reader; when it lands, the registration moves and the SQL does not.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import emit  # noqa: E402

INSTALL_HINT = "pip install --user duckdb==1.5.6"
VIEW = HERE / "views" / "t2_pr_issue_join.sql"
UNRESOLVED = emit.UNRESOLVED
EVENT_TYPES = ("pr.merged", "pr.link.observed", "issue.opened", "issue.closed", "fetch.status")
ATTRS = ("link_kind", "target_repo", "target_issue", "status", "stage", "pr", "oldest_merged_at")
COLUMNS = ("event_type", "source", "provenance", "account", "org", "repo", "pr", "issue", "ts",
           *(f"a_{a}" for a in ATTRS))
DDL = ("event_type VARCHAR, source VARCHAR, provenance VARCHAR, account VARCHAR, org VARCHAR, repo VARCHAR, "
       "pr BIGINT, issue BIGINT, ts VARCHAR, a_link_kind VARCHAR, a_target_repo VARCHAR, a_target_issue BIGINT, "
       "a_status VARCHAR, a_stage VARCHAR, a_pr BIGINT, a_oldest_merged_at VARCHAR")


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_events(store: Path, account: str | None = None, org: str | None = None) -> tuple[list[tuple], int]:
    """`(rows, malformed)` from `<store>/events/**/*.jsonl`. A partition filter
    keeps rows whose partition matches or is `unresolved` (the partition
    value, not a join state); a line that is not a JSON object is counted."""
    rows, malformed = [], 0
    root = store / "events"
    for path in sorted(root.rglob("*.jsonl")) if root.is_dir() else []:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if not isinstance(event, dict):
                malformed += 1
                continue
            if event.get("event_type") not in EVENT_TYPES:
                continue
            if any(want and event.get(col) not in (want, UNRESOLVED) for want, col in ((account, "account"), (org, "org"))):
                continue
            attrs = event.get("attrs") if isinstance(event.get("attrs"), dict) else {}
            rows.append((event.get("event_type"), event.get("source"), event.get("provenance"),
                         event.get("account"), event.get("org"), event.get("repo"),
                         _int(event.get("pr")), _int(event.get("issue")), event.get("ts"),
                         attrs.get("link_kind"), attrs.get("target_repo"), _int(attrs.get("target_issue")),
                         attrs.get("status"), attrs.get("stage"), _int(attrs.get("pr")),
                         attrs.get("oldest_merged_at")))
    return rows, malformed


def derive(rows: list[tuple]) -> dict[str, tuple[list[str], list[tuple]]]:
    """`{view: (columns, rows)}` for the join, coverage and medians views."""
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE events ({DDL})")
    if rows:
        con.executemany(f"INSERT INTO events VALUES ({', '.join('?' * len(COLUMNS))})", rows)
    con.execute(VIEW.read_text(encoding="utf-8"))
    out = {}
    for name in ("pr_join_coverage", "pr_join_medians", "pr_issue_join"):
        cursor = con.execute(f"SELECT * FROM {name}")
        out[name] = ([d[0] for d in cursor.description], cursor.fetchall())
    con.close()
    return out


def _table(columns: list[str], rows: list[tuple]) -> list[str]:
    if not rows:
        return ["No rows."]
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in row) + " |" for row in rows]
    return lines


def render(results: dict[str, tuple[list[str], list[tuple]]], malformed: int) -> str:
    out = ["# PR <-> issue join (harmonic-forge#1167)", "",
           "Derived from observation events; unresolved PRs are excluded from the coverage denominator.", "",
           f"{malformed} malformed store line(s) skipped.", "", "## Coverage by repo and month", ""]
    out += _table(*results["pr_join_coverage"])
    out += ["", "## F739 AC1 medians (hours from issue opened to first joined merge; shown at n >= 20)", ""]
    out += _table(*results["pr_join_medians"])
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", type=Path, default=None, help="telemetry store (default: emit.store_root())")
    parser.add_argument("--account", help="keep rows of this account (and unresolved ones)")
    parser.add_argument("--org", help="keep rows of this org (and unresolved ones)")
    args = parser.parse_args(argv)
    try:
        import duckdb  # noqa: F401
    except ImportError:
        print(f"pr-join-report: duckdb not installed; run: {INSTALL_HINT}", file=sys.stderr)
        return 2
    rows, malformed = read_events(args.store or emit.store_root(), args.account, args.org)
    print(render(derive(rows), malformed), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
