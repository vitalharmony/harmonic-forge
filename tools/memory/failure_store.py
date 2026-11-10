#!/usr/bin/env python3
"""The failure store: a derived SQLite projection of the protocol-failure logs
(harmonic-forge#1288).

`protocol-failures.jsonl` stays the system of record. This store is rebuilt
from it, so a lost or stale database costs one rebuild and nothing else.

  build   rebuild the database from both logs (the memory log and Lane 3's
          testplan log) plus every `failure-overlays/*.jsonl`, through a
          uniquely named temp file (`<db>.build-<pid>-<random>.tmp`) in the
          database's folder that is swapped in. Builds of one database are
          serialized by an exclusive lock on `<db>.lock`, so temp files left
          by earlier failed builds are provably abandoned and are removed by
          that exact name pattern; no other file is ever deleted.
  latest  print the newest failure with an effective key `--tool T --class C`
          and its probe, as one JSON line (the precedent-citation lookup).
          Rebuilds first when a log, an overlay, the schema file or
          `STORE_VERSION` changed since the last build.

Logs are only ever read. Overlays relabel without rewriting: a row
`{"log_line": N, "key_v1": "tool|class", "probe"?: "...", "log"?: "memory"|
"testplan"}` re-keys line N (0-based) of that log in the `failure_keys` view.
Overlay files apply in name order and rows in file order; the last match wins.
A row with an empty `key_v1` re-keys nothing and is counted as skipped; an
absent, null or false `key_v1` is malformed.

The database, the logs and the overlays live in
~/Harmonic_Projects/operator-memory/. A `--db` inside a harmonic-forge
checkout or worktree is refused: harmonic-forge is public and failure data
never enters it, and so is a `--db` that is a log or overlay path or lacks a
database suffix (`.sqlite`, `.sqlite3`, `.db`). Exit codes: 0 found or built,
4 no stored failure for that key, 2 refused, 3 any other error (I/O, SQLite,
an unreadable or missing memory log, a record SQLite cannot bind). A missing
testplan log reads as empty.
Stdlib `sqlite3` only, no network.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
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

#: The store's code version. Part of the staleness fingerprint with the schema
#: file's bytes, so a store built by older code or an older schema is rebuilt,
#: never served. Bump it when the projection changes without a schema change.
STORE_VERSION = 3

COLUMNS = ("log", "line", "ts", "session", "turn", "lane", "repo", "issue", "caught_by",
           "rule_existed", "rule_ref", "tool", "class", "probe", "class_list_version")


class Refused(Exception):
    """A refusal: exit 2, nothing written."""


def _guard(path: Path, flag: str) -> None:
    refusal = capture_failure.forge_refusal(path, flag)
    if refusal:
        raise Refused(refusal)


def _read_bytes(path: Path) -> bytes | None:
    """The file's bytes, or None when it does not exist. Only a missing file
    reads as an empty log; any other OSError (permissions, a directory, an I/O
    error) propagates, so an unreadable log is a build error (exit 3) and never
    reads as "no precedent"."""
    try:
        with path.open("rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def overlay_files(overlays: Path) -> list[Path]:
    return sorted(overlays.glob("*.jsonl")) if overlays.is_dir() else []


def fingerprint(logs: dict[str, Path], overlays: Path) -> str:
    """`STORE_VERSION`, the schema file's sha256, and the size and sha256 of
    every log and overlay file, as one JSON string."""
    parts: list = [["store", STORE_VERSION, hashlib.sha256(SCHEMA.read_bytes()).hexdigest()]]
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


#: The log that must exist. A missing memory log is an unreadable source (exit
#: 3), never "no precedent"; only Lane 3's testplan log may be absent.
REQUIRED_LOG = "memory"


def failure_rows(name: str, path: Path) -> tuple[list[tuple], int]:
    """`(rows, malformed)` for one log; `line` is the 0-based line index."""
    if name == REQUIRED_LOG and not Path(path).exists():
        raise FileNotFoundError(f"the {name} log {path} does not exist")
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
                continue  # fail-direction: a blank line is no row; a non-blank bad line counts malformed below
            try:
                row = json.loads(line)
            except ValueError:
                row = None  # refuses: counted malformed below, and any malformed row refuses the score
            # refuses: `bool` is an `int` subclass, so `false` would alias line 0
            # (lines count from 0, `failure_rows`), and a negative line is no
            # line (harmonic-forge#1289 epoch 4).
            if not isinstance(row, dict) or type(row.get("log_line")) is not int or row["log_line"] < 0:
                malformed += 1
                continue
            # Only the empty string is a deliberate skip; an absent, null or
            # false key_v1 falls through as malformed (harmonic-forge#1289 epoch 4).
            key = row.get("key_v1")
            if key == "":
                skipped += 1
                continue
            tool, sep, klass = str(key).partition("|")
            if not sep or not tool or not klass:
                malformed += 1
                continue
            rows.append((path.name, str(row.get("log") or "memory"), row["log_line"],
                         tool, klass, _text(row.get("probe"))))
    return rows, skipped, malformed


#: The database suffixes `--db` accepts. Anything else is refused, so the store
#: can never be pointed at a log, an overlay or any other non-database file.
DB_SUFFIXES = (".sqlite", ".sqlite3", ".db")

#: Exit code for a build that crashed (an I/O or SQLite error), distinct from
#: a refusal's 2 and `latest`'s EXIT_NOT_FOUND.
EXIT_ERROR = 3
#: `latest` found no stored failure for the key (operator ruling, harmonic-forge#1288):
#: never 1, which is also what any uncaught Python exception returns.
EXIT_NOT_FOUND = 4


def _guard_db(db: Path, logs: dict[str, Path], overlays: Path) -> None:
    """Refuse a `--db` that could overwrite a source: one without a database
    suffix, one that resolves to a log or an overlay file, or one inside the
    overlays folder."""
    _guard(db, "--db")
    if db.suffix.lower() not in DB_SUFFIXES:
        raise Refused(f"--db {db} must end in one of {', '.join(DB_SUFFIXES)}.")
    target = db.resolve()
    sources = {Path(p).expanduser().resolve() for p in logs.values()}
    sources |= {p.resolve() for p in overlay_files(Path(overlays).expanduser())}
    if target in sources or target.parent == Path(overlays).expanduser().resolve():
        raise Refused(f"--db {db} is a log or overlay path, or inside the overlays "
                      "folder; the store never writes over its own sources.")


def _temp_pattern(db: Path) -> re.Pattern[str]:
    """A build's temp file: `<db>.build-<pid>-<random>.tmp`, plus SQLite's
    `-journal`/`-wal`/`-shm` siblings. Nothing else matches."""
    return re.compile(re.escape(db.name) + r"\.build-\d+-[A-Za-z0-9_]+\.tmp(?:-journal|-wal|-shm)?")


@contextlib.contextmanager
def _build_lock(db: Path):
    """Serialize builds of one database with an exclusive `flock` on
    `<db>.lock`. The lock file is kept; the kernel drops the lock when the
    holder exits, so a crashed build never wedges the next one."""
    fd = os.open(db.parent / (db.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def build(db: Path, logs: dict[str, Path], overlays: Path, *, if_stale: bool = False) -> dict | None:
    """Rebuild `db` from the logs and overlays; return the row counts.

    Builds of one database are serialized by `_build_lock`, so while it is
    held no other build is running and every temp file left in the folder is
    provably abandoned: only those, by their exact name pattern, are removed.
    With `if_stale`, a build that finds the store already current once it holds
    the lock (another process rebuilt it meanwhile) does nothing and returns
    None."""
    db = Path(db).expanduser()
    _guard_db(db, logs, overlays)
    db.parent.mkdir(parents=True, exist_ok=True)
    with _build_lock(db):
        pattern = _temp_pattern(db)
        for stale in db.parent.iterdir():
            if pattern.fullmatch(stale.name) and stale.is_file():
                stale.unlink(missing_ok=True)
        stamp = fingerprint(logs, overlays)
        if if_stale and stored_fingerprint(db) == stamp:
            return None
        counts: dict = {"failures": {}, "malformed": 0}
        fd, tmp_name = tempfile.mkstemp(dir=db.parent, prefix=f"{db.name}.build-{os.getpid()}-",
                                        suffix=".tmp")
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
    _guard_db(db, logs, overlays)
    if stored_fingerprint(db) != fingerprint(logs, overlays):
        build(db, logs, overlays, if_stale=True)
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
            sys.stdout.flush()
            return 0
        row = latest(args.db, logs, args.overlays, args.tool, args.klass)
        if row is None:
            print(f"failure-store: no stored failure with key {args.tool}|{args.klass}", file=sys.stderr)
            return EXIT_NOT_FOUND
        # Inside the handler, and flushed here: an I/O error writing the result
        # exits 3, never 1.
        print(json.dumps(row, separators=(",", ":")))
        sys.stdout.flush()
        return 0
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - every non-refusal error is exit 3, never 1
        print(f"failure-store: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        # The failed write stays in stdout's buffer, and the interpreter flushes
        # it again at shutdown, which turns this exit 3 into 120. Point stdout's
        # descriptor at devnull so that last flush succeeds (the Python docs'
        # "Note on SIGPIPE" pattern).
        with contextlib.suppress(OSError, ValueError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                os.close(devnull)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
