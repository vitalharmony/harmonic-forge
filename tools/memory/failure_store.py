#!/usr/bin/env python3
"""The failure store: a derived SQLite projection of the protocol-failure logs
(harmonic-forge#1288).

`protocol-failures.jsonl` stays the system of record. This store is rebuilt
from it, so a lost or stale database costs one rebuild and nothing else.

  build   rebuild the database from both logs (the memory log and Lane 3's
          testplan log) plus every `failure-overlays/*.jsonl`, through a
          uniquely named temp file in the database's folder that is swapped
          in; stale temp files from earlier failed builds are removed.
  latest  print the newest failure with an effective key `--tool T --class C`
          and its probe, as one JSON line (the precedent-citation lookup).
          Rebuilds first when a log or overlay changed since the last build.

Logs are only ever read. Overlays relabel without rewriting: a row
`{"log_line": N, "key_v1": "tool|class", "probe"?: "...", "log"?: "memory"|
"testplan"}` re-keys line N (0-based) of that log in the `failure_keys` view.
Overlay files apply in name order and rows in file order; the last match wins.
A row with an empty `key_v1` re-keys nothing and is counted as skipped.

The database, the logs and the overlays live in
~/Harmonic_Projects/operator-memory/. A `--db` inside a harmonic-forge
checkout or worktree is refused: harmonic-forge is public and failure data
never enters it. Stdlib `sqlite3` only, no network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import capture_failure  # noqa: E402

SCHEMA = HERE / "failure_store.sql"
DEFAULT_DB = capture_failure.MEMORY_STORE / "failure-store.sqlite"
DEFAULT_OVERLAYS = capture_failure.MEMORY_STORE / "failure-overlays"
DEFAULT_LOGS = {
    "memory": capture_failure.default_log_path(None),
    "testplan": capture_failure.default_log_path("3"),
}

COLUMNS = ("log", "line", "ts", "session", "turn", "lane", "repo", "issue", "caught_by",
           "rule_existed", "rule_ref", "tool", "class", "probe", "command", "class_list_version")


class Refused(Exception):
    """A refusal: exit 2, nothing written."""


def _guard(path: Path, flag: str) -> None:
    refusal = capture_failure.forge_refusal(path, flag)
    if refusal:
        raise Refused(refusal)


def _read_bytes(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            return handle.read()
    except OSError:
        return None


def overlay_files(overlays: Path) -> list[Path]:
    return sorted(overlays.glob("*.jsonl")) if overlays.is_dir() else []


def fingerprint(logs: dict[str, Path], overlays: Path) -> str:
    """Size and sha256 of every log and overlay file, as one JSON string."""
    parts = []
    for name, path in sorted(logs.items()):
        data = _read_bytes(path)
        parts.append([f"log:{name}", None if data is None else len(data),
                      None if data is None else hashlib.sha256(data).hexdigest()])
    for path in overlay_files(overlays):
        data = _read_bytes(path) or b""
        parts.append([f"overlay:{path.name}", len(data), hashlib.sha256(data).hexdigest()])
    return json.dumps(parts, separators=(",", ":"))


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _lines(path: Path) -> list[str]:
    data = _read_bytes(path)
    return [] if data is None else data.decode("utf-8", errors="replace").splitlines()


def failure_rows(name: str, path: Path) -> tuple[list[tuple], int]:
    """`(rows, malformed)` for one log; `line` is the 0-based line index."""
    rows, malformed = [], 0
    for index, line in enumerate(_lines(path)):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        if not isinstance(record, dict):
            malformed += 1
            continue
        rows.append((name, index, *(_text(record.get(c)) for c in COLUMNS[2:-1]),
                     _int(record.get("class_list_version"))))
    return rows, malformed


def overlay_rows(overlays: Path) -> tuple[list[tuple], int, int]:
    """`(rows, skipped, malformed)`, in application order."""
    rows, skipped, malformed = [], 0, 0
    for path in overlay_files(overlays):
        for line in _lines(path):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            if not isinstance(row, dict) or not isinstance(row.get("log_line"), int):
                malformed += 1
                continue
            key = row.get("key_v1") or ""
            if not key:
                skipped += 1
                continue
            tool, sep, klass = str(key).partition("|")
            if not sep or not tool or not klass:
                malformed += 1
                continue
            rows.append((path.name, str(row.get("log") or "memory"), row["log_line"],
                         tool, klass, _text(row.get("probe"))))
    return rows, skipped, malformed


def build(db: Path, logs: dict[str, Path], overlays: Path) -> dict:
    """Rebuild `db` from the logs and overlays; return the row counts."""
    db = Path(db).expanduser()
    _guard(db, "--db")
    db.parent.mkdir(parents=True, exist_ok=True)
    stamp = fingerprint(logs, overlays)
    counts: dict = {"failures": {}, "malformed": 0}
    fd, tmp_name = tempfile.mkstemp(dir=db.parent, prefix=db.name + ".")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        _guard(tmp, "the build's temp file")
        con = sqlite3.connect(tmp)
        try:
            con.executescript(SCHEMA.read_text(encoding="utf-8"))
            for name, path in logs.items():
                rows, malformed = failure_rows(name, path)
                con.executemany(f"INSERT INTO failures VALUES ({', '.join('?' * len(COLUMNS))})", rows)
                counts["failures"][name] = len(rows)
                counts["malformed"] += malformed
            rows, skipped, bad = overlay_rows(overlays)
            con.executemany("INSERT INTO overlays VALUES (?, ?, ?, ?, ?, ?)", rows)
            counts.update(overlays=len(rows), skipped=skipped, overlay_malformed=bad)
            con.executemany("INSERT INTO meta VALUES (?, ?)",
                            [("fingerprint", stamp), ("counts", json.dumps(counts))])
            con.commit()
        finally:
            con.close()
        tmp.replace(db)
    finally:
        tmp.unlink(missing_ok=True)
    for stale in db.parent.glob(db.name + ".*"):
        stale.unlink(missing_ok=True)
    return counts


def stored_fingerprint(db: Path) -> str | None:
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'fingerprint'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def latest(db: Path, logs: dict[str, Path], overlays: Path, tool: str, klass: str) -> dict | None:
    """The newest failure whose effective key is `tool|klass`, rebuilding first
    when the logs or overlays changed since the last build."""
    db = Path(db).expanduser()
    _guard(db, "--db")
    if stored_fingerprint(db) != fingerprint(logs, overlays):
        build(db, logs, overlays)
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM failure_keys WHERE tool = ? AND class = ? "
            "ORDER BY ts DESC, log DESC, line DESC LIMIT 1", (tool, klass)).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def _summary(db: Path, counts: dict) -> str:
    per_log = ", ".join(f"{name} {n}" for name, n in counts["failures"].items())
    return (f"failure-store: built {db}: {sum(counts['failures'].values())} failures ({per_log}), "
            f"{counts['malformed']} malformed line(s), {counts['overlays']} overlay row(s), "
            f"{counts['skipped']} skipped (empty key_v1), "
            f"{counts['overlay_malformed']} malformed overlay row(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--memory-log", type=Path, default=DEFAULT_LOGS["memory"])
    parser.add_argument("--testplan-log", type=Path, default=DEFAULT_LOGS["testplan"])
    parser.add_argument("--overlays", type=Path, default=DEFAULT_OVERLAYS,
                        help="Folder of overlay *.jsonl files.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="Rebuild the store.")
    look = sub.add_parser("latest", help="The newest failure with an effective key.")
    look.add_argument("--tool", required=True)
    look.add_argument("--class", dest="klass", required=True)
    args = parser.parse_args(argv)
    logs = {"memory": args.memory_log, "testplan": args.testplan_log}
    try:
        if args.command == "build":
            print(_summary(args.db, build(args.db, logs, args.overlays)))
            return 0
        row = latest(args.db, logs, args.overlays, args.tool, args.klass)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if row is None:
        print(f"failure-store: no stored failure with key {args.tool}|{args.klass}", file=sys.stderr)
        return 1
    print(json.dumps(row, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
