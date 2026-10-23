#!/usr/bin/env python3
"""Weekly improvement-detector report (harmonic-forge#1159, D1; D2 adds a view).

Registers the input tables in an in-memory DuckDB, runs every
`views/d*_*.sql` in name order, and writes one Markdown report with one
section per view. A new detector is a new `.sql` file; this runner does not
change. Read-only and LLM-free: it files nothing and edits nothing (AC3).

Tables:
  misses_raw  the rows of both protocol-failures.jsonl logs (a missing log is
              empty), each tagged with `account`/`org`/`repo` partitions (AC4).
              The record's own `account` key is free-text narrative, so it is
              kept as `account_note`; `org`/`repo` come from the record's
              `repo` when it names one, and are `unresolved` otherwise. The
              account is never guessed (operator ruling on #1159, "1 A").
  rule_hooks  `rule_id`, `hook_count`, `scripts`, `protected`, from
              tools/rules/registry.toml via query_rules.load_rules.

A protected candidate (the te-runner's permissions, its deny list, the
preclose gates) is counted per view and never named: the runner re-runs each
view with `protected` cleared and reports the difference.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "tools" / "rules"))
sys.path.insert(0, str(ROOT / "tools" / "memory"))
sys.path.insert(0, str(ROOT / "tools" / "runner"))

import query_rules  # noqa: E402
from capture_failure import default_log_path  # noqa: E402
from te_run import BASE_CHECK_ROOT  # noqa: E402

INSTALL_HINT = "pip install --user duckdb==1.5.6"
UNRESOLVED = "unresolved"
RULE_ID_RE = re.compile(r"R-\d{4}")
IN_FORCE = ("rule", "scoped_out")
MISS_COLUMNS = ("ts", "caught_by", "rule_existed", "rule_ref", "lane", "issue",
                "account", "org", "repo", "account_note")

DEFAULT_LOGS = [default_log_path(None), default_log_path("3")]
DEFAULT_REGISTRY = ROOT / "tools" / "rules" / "registry.toml"
DEFAULT_VIEWS = HERE / "views"
DEFAULT_OUT = Path.home() / "Harmonic_Projects" / "reports" / "detectors"


def hook_basename(hook: dict | str) -> str:
    """A registry hook is a `{script = ...}` table or a bare string, either a
    full path or a basename."""
    script = hook.get("script", "") if isinstance(hook, dict) else hook
    return Path(script).name


def is_protected(rule: dict, scripts: list[str], targets: dict) -> bool:
    if rule["id"] in targets.get("rule_ids", []):
        return True
    regex = targets.get("statement_regex")
    if regex and re.search(regex, rule.get("statement", "")):
        return True
    return bool(set(scripts) & set(targets.get("hook_basenames", [])))


def rule_hook_rows(rules: list[dict], targets: dict) -> list[tuple]:
    rows = []
    for rule in rules:
        scripts = [hook_basename(h) for h in rule.get("hooks") or []]
        rows.append((rule["id"], len(scripts), ", ".join(scripts), is_protected(rule, scripts, targets)))
    return rows


def with_unregistered(hooks: list[tuple], misses: list[tuple]) -> list[tuple]:
    """A cited rule id the registry does not hold cannot be checked against
    protected_targets.json, so it gets a protected row: counted, never named."""
    known = {h[0] for h in hooks}
    cited = {m.group(0) for r in misses if (m := RULE_ID_RE.search(r[3]))}
    return hooks + [(rule_id, 0, "", True) for rule_id in sorted(cited - known)]


TEST_ID_RE = re.compile(r"^\S+ \((test_\w+)\.\w+(?:\.\w+)*\)$")


def read_base_checks(root: Path) -> list[tuple]:
    """`(sha, complete, failures, mtime)` per cached `<sha>/result.json`, in
    te_run.previous_base's order source (file mtime). A missing cache is empty."""
    rows = []
    for path in sorted(root.glob("*/result.json")) if root.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            rows.append((str(data.get("sha") or path.parent.name), bool(data.get("complete")),
                         [str(f) for f in data.get("failures") or []], mtime))
    return rows


def tracked_modules(repo_root: Path) -> dict[str, int]:
    """`{module basename: tracked file count}` for every tools/**/test_*.py."""
    listed = subprocess.run(["git", "-C", str(repo_root), "ls-files", "tools/**/test_*.py"],
                            capture_output=True, text=True, check=False).stdout.split()
    counts: dict[str, int] = {}
    for path in set(listed):  # an unmerged index lists a path once per stage
        counts[Path(path).stem] = counts.get(Path(path).stem, 0) + 1
    return counts


def base_check_protected_rows(checks: list[tuple], targets: dict, modules: dict[str, int]) -> list[tuple]:
    """Protected when the module is a protected target, or the id cannot be
    resolved to exactly one tracked module (unparseable, zero or several)."""
    protected_modules = set(targets.get("test_modules", []))
    tests = sorted({test for check in checks for test in check[2]})
    rows = []
    for test in tests:
        match = TEST_ID_RE.match(test)
        module = match.group(1) if match else None
        rows.append((test, module is None or module in protected_modules or modules.get(module, 0) != 1))
    return rows


def partition(record: dict) -> tuple[str, str, str]:
    """`(account, org, repo)`; never guessed beyond what the record names."""
    repo = record.get("repo")
    if isinstance(repo, str) and "/" in repo:
        org, name = repo.split("/", 1)
        return UNRESOLVED, org, name
    return UNRESOLVED, UNRESOLVED, UNRESOLVED


def read_misses(logs: list[Path]) -> tuple[list[dict], int]:
    """`(records, malformed)`. A torn append must not stop every later
    report, so a line that is not a JSON object is skipped and counted."""
    rows, malformed = [], 0
    for log in logs:
        if not log.is_file():
            continue
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if isinstance(record, dict):
                rows.append(record)
            else:
                malformed += 1
    return rows, malformed


def miss_rows(records: list[dict], account: str | None, org: str | None, repo: str | None) -> list[tuple]:
    """Partition filters keep a matching row and an `unresolved` one: an
    unresolved row is never dropped by a guess about where it belongs."""
    rows = []
    for record in records:
        acct, rec_org, rec_repo = partition(record)
        if any(want and have not in (want, UNRESOLVED)
               for want, have in ((account, acct), (org, rec_org), (repo, rec_repo))):
            continue
        rows.append((record.get("ts"), record.get("caught_by"), record.get("rule_existed"),
                     record.get("rule_ref") or "", record.get("lane"), str(record.get("issue") or ""),
                     acct, rec_org, rec_repo, record.get("account")))
    return rows


def unresolved_rule_refs(rows: list[tuple]) -> int:
    return sum(1 for r in rows if r[2] in IN_FORCE and not RULE_ID_RE.search(r[3]))


TABLE_DDL = {
    "misses_raw": "ts VARCHAR, caught_by VARCHAR, rule_existed VARCHAR, rule_ref VARCHAR, lane VARCHAR, "
                  "issue VARCHAR, account VARCHAR, org VARCHAR, repo VARCHAR, account_note VARCHAR",
    "rule_hooks": "rule_id VARCHAR, hook_count INTEGER, scripts VARCHAR, protected BOOLEAN",
    "base_checks": "sha VARCHAR, complete BOOLEAN, failures VARCHAR[], mtime DOUBLE",
    "base_check_protected": "test VARCHAR, protected BOOLEAN",
}
# Tables whose last column is `protected`: cleared to count suppression.
PROTECTED_TABLES = ("rule_hooks", "base_check_protected")


def _register(con, tables: dict[str, list[tuple]]) -> None:
    for name, ddl in TABLE_DDL.items():
        con.execute(f"CREATE OR REPLACE TABLE {name} ({ddl})")
        rows = tables.get(name) or []
        if rows:
            con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * len(rows[0]))})", rows)


def run_views(tables: dict[str, list[tuple]], views_dir: Path) -> list[dict]:
    """One result per `d*_*.sql`, in name order: columns, rows, suppressed."""
    import duckdb

    cleared = {name: [r[:-1] + (False,) for r in rows] if name in PROTECTED_TABLES else rows
               for name, rows in tables.items()}
    results = []
    for sql_file in sorted(views_dir.glob("d*_*.sql")):
        name = sql_file.stem
        sql = sql_file.read_text(encoding="utf-8")
        counts = []
        for variant in (tables, cleared):
            con = duckdb.connect(":memory:")
            _register(con, variant)
            con.execute(sql)
            cursor = con.execute(f"SELECT * FROM {name}")
            if not counts:
                columns = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
                counts.append(len(rows))
            else:
                counts.append(len(cursor.fetchall()))
            con.close()
        results.append({"name": name, "columns": columns, "rows": rows,
                        "suppressed": max(counts[1] - counts[0], 0)})
    return results


def render(results: list[dict], unresolved: int, malformed: int, day: str) -> str:
    out = [f"# Detector report — {day}", "",
           "Read-only (harmonic-forge#1159). Lane 1 decides what becomes a Tooling Exception; "
           "this report files nothing.", "",
           f"{unresolved} in-force miss(es) with an unresolved rule_ref.", "",
           f"{malformed} malformed miss-log line(s) skipped.", ""]
    for result in results:
        out += [f"## {result['name']}", ""]
        if result["rows"]:
            out.append("| " + " | ".join(result["columns"]) + " |")
            out.append("|" + "---|" * len(result["columns"]))
            for row in result["rows"]:
                out.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
        else:
            out.append("No rows.")
        out += ["", f"{result['suppressed']} candidate(s) suppressed (protected target).", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--miss-log", action="append", type=Path,
                        help="protocol-failures.jsonl to read; repeatable (default: both lanes' logs)")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--views", type=Path, default=DEFAULT_VIEWS)
    parser.add_argument("--protected", type=Path, default=None,
                        help="protected_targets.json (default: <views>/protected_targets.json)")
    parser.add_argument("--base-checks", type=Path, default=BASE_CHECK_ROOT,
                        help="te-runner base-check cache (default: te_run.BASE_CHECK_ROOT)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    # A date, never a path: the report name must stay inside --out-dir.
    parser.add_argument("--date", type=lambda value: date.fromisoformat(value).isoformat(),
                        default=date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--account", help="keep rows of this account (and unresolved ones)")
    parser.add_argument("--org", help="keep rows of this org (and unresolved ones)")
    parser.add_argument("--repo", help="keep rows of this repo name (and unresolved ones)")
    args = parser.parse_args(argv)

    try:
        import duckdb  # noqa: F401
    except ImportError:
        print(f"detector-report: duckdb not installed; run: {INSTALL_HINT}", file=sys.stderr)
        return 2

    targets = json.loads((args.protected or args.views / "protected_targets.json").read_text(encoding="utf-8"))
    records, malformed = read_misses(args.miss_log or DEFAULT_LOGS)
    misses = miss_rows(records, args.account, args.org, args.repo)
    hooks = with_unregistered(rule_hook_rows(query_rules.load_rules(args.registry), targets), misses)
    checks = read_base_checks(args.base_checks)
    tables = {"misses_raw": misses, "rule_hooks": hooks, "base_checks": checks,
              "base_check_protected": base_check_protected_rows(checks, targets, tracked_modules(ROOT))}
    report = render(run_views(tables, args.views), unresolved_rule_refs(misses), malformed, args.date)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    target = args.out_dir / f"{args.date}.md"
    target.write_text(report, encoding="utf-8")
    print(f"detector-report: wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
