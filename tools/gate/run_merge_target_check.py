#!/usr/bin/env python3
"""Run a consuming repo's declared merge-target validation query (ADR-008 AC4).

`merge_target_check` is the exception ADR-008 **rejected** as adapter-owned:
one query plus one shape assertion is three manifest values, not a repo-specific
procedure. So the platform owns the mechanism -- run the declared query against
the declared connection, compare the result against the declared expected
shape -- and the consuming repo owns only the values.

Reports in the standard gate-finding format:
`{"status": "pass"|"fail"|"blocked", "evidence": [...]}`.

- `blocked` when the manifest, the key or the connection environment variable
  is missing, or the driver cannot connect. A check that could not run must
  never read as one that ran and passed (ADR-008 AC4, `rules/testing-gate.md`
  rule 8).
- `fail` when the query returns no rows, lacks the declared field, or the
  value does not satisfy the declared comparison.
- `pass` only when the declared assertion actually held.

**Not wired to any call site** (harmonic-forge#721): no consuming repo declares
`merge_target_check` today. It exists so the capability is available, tested,
and does not have to be invented under time pressure by whoever needs it first.

    python3 tools/gate/run_merge_target_check.py [--repo <path>] [--id <elementId>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapter import BLOCKED, FAIL, PASS, declared, finding  # noqa: E402

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


def compare(value: Any, op: str, expected: Any) -> tuple[bool, str]:
    """(held, why). A type mismatch is a FAIL with its reason, never a crash."""
    try:
        return _OPS[op](value, expected), f"{value!r} {op} {expected!r}"
    except TypeError as exc:
        return False, f"cannot compare {value!r} {op} {expected!r}: {exc}"


def run(cwd: Path | None = None, params: dict[str, Any] | None = None,
        driver_factory: Any = None) -> dict[str, Any]:
    entry = declared("merge_target_check", cwd)
    if entry is None:
        return finding(BLOCKED, "merge_target_check: not declared in the adapter manifest")
    for key in ("query", "connection_env", "expected_shape"):
        if key not in entry:
            return finding(BLOCKED, f"merge_target_check.{key}: not declared")
    shape = entry["expected_shape"]
    uri = os.environ.get(entry["connection_env"])
    if not uri:
        return finding(BLOCKED, f"merge_target_check: {entry['connection_env']} is unset")

    if driver_factory is None:
        def driver_factory(uri: str) -> Any:  # noqa: ANN401 -- injected in tests
            from neo4j import GraphDatabase

            auth = (os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", ""))
            return GraphDatabase.driver(uri, auth=auth)

    try:
        driver = driver_factory(uri)
    except Exception as exc:  # noqa: BLE001 -- cannot connect is BLOCKED, never a pass
        return finding(BLOCKED, f"merge_target_check: cannot connect to {uri}: "
                                f"{type(exc).__name__}: {exc}")
    try:
        with driver.session() as session:
            rows = [dict(record) for record in session.run(entry["query"], **(params or {}))]
    except Exception as exc:  # noqa: BLE001
        return finding(BLOCKED, f"merge_target_check: query failed: {type(exc).__name__}: {exc}")
    finally:
        close = getattr(driver, "close", None)
        if callable(close):
            close()

    if not rows:
        return finding(FAIL, "merge_target_check: the query returned no rows")
    field = shape["field"]
    if field not in rows[0]:
        return finding(FAIL, f"merge_target_check: the query returned no {field!r} field "
                             f"(got {sorted(rows[0])})")
    held, why = compare(rows[0][field], shape["op"], shape["value"])
    return finding(PASS if held else FAIL, f"merge_target_check: {why}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=None,
                        help="the consuming repo to read the manifest from (default: cwd)")
    parser.add_argument("--id", default=None, help="bind as $id in the declared query")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    result = run(cwd=args.repo, params={"id": args.id} if args.id else None)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
