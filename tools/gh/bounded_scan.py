#!/usr/bin/env python3
"""The one sanctioned gh scan route (harmonic-forge#1363).

    bounded_scan.py --repo <letter> --scope <scope> [--fields a,b] [--max-pages N] [--dry-run]

A scan needs the operator's `SCAN <letter> <scope>: <reason>` line for this
session (`tools/hooks/scan_grant.py`). Before any list call it reads the REST
core and GraphQL budgets separately and refuses if the estimated pages would
take REST core below the shim's floor (1000). An unknown REST budget refuses
too: a bounded scan is never worth an unbounded guess.

Snapshots live under `~/.cache/harmonic-forge/scan/<owner>_<repo>/<scope digest>.json`.
One younger than `STALE_SECONDS` is served with no call. An older one is
refreshed page by page with `If-None-Match`: a 304 page (which spends no REST
budget, probed live) is reused, a 200 page replaces it. Paging follows
`Link: rel="next"`; cached pages past the new last page are dropped.
`open-issues` drops pull requests (`/issues` returns both). A run cut short by
`--max-pages` records `truncated: true`.

`gh` exits 1 on a 304 and prints `gh: HTTP 304`, so the status is read from
the `-i` status line, never from the exit code (probed live).

Every run, refused or not, appends one receipt line to
`~/.claude/state/scan/receipts.jsonl`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))
import gh_scan_patterns  # noqa: E402
import scan_grant  # noqa: E402

FLOOR = 1000
STALE_SECONDS = 15 * 60
DEFAULT_MAX_PAGES = 10
PER_PAGE = 100
CALLER = "bounded_scan"
SNAPSHOT_ROOT_ENV = "BOUNDED_SCAN_SNAPSHOT_ROOT"
RECEIPTS_ENV = "BOUNDED_SCAN_RECEIPTS"

_STATUS = re.compile(r"^HTTP/\S+\s+(\d{3})", re.M)
_ETAG = re.compile(r"(?im)^etag:\s*(.+?)\s*$")
_LINK = re.compile(r"(?im)^link:\s*(.+?)\s*$")
_REMAINING = re.compile(r"(?im)^x-ratelimit-remaining:\s*(\d+)\s*$")


def snapshot_root() -> Path:
    return Path(os.environ.get(SNAPSHOT_ROOT_ENV)
                or Path.home() / ".cache" / "harmonic-forge" / "scan")


def receipts_path() -> Path:
    return Path(os.environ.get(RECEIPTS_ENV)
                or Path.home() / ".claude" / "state" / "scan" / "receipts.jsonl")


def parse_fields(raw: str | None) -> list[str] | None:
    """`*` is the no-fields key, so it is never a field name (preclose pass 2)."""
    return sorted({f for f in (raw or "").split(",") if f and f != "*"}) or None


def snapshot_path(repo: str, scope: str, fields: list[str] | None = None) -> Path:
    """Keyed on the scope AND the fields (preclose pass 1, survivor 3): items are
    projected before they are cached, so a run with other fields must never be
    served this projection, from a fresh snapshot or from a 304 page."""
    key = scope + "|" + (",".join(fields) if fields else "*")
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return snapshot_root() / repo.replace("/", "_") / f"{digest}.json"


def list_path(repo: str, scope: str, page: int) -> str:
    """The REST list path for one page. Exactly the shape the shim admits for
    `bounded_scan`: `repos/<owner>/<repo>/(issues|pulls)?<query>`."""
    kind = "pulls" if scope == "open-prs" else "issues"
    query = f"state=open&per_page={PER_PAGE}&page={page}"
    if scope.startswith("milestone="):
        query += f"&milestone={scope.split('=', 1)[1]}"
    elif scope.startswith("label="):
        query += f"&labels={scope.split('=', 1)[1]}"
    return f"repos/{repo}/{kind}?{query}"


def fetch(path: str, etag: str | None, runner=subprocess.run) -> tuple[int, dict, list | None]:
    """`(status, headers, items)`. Runs `gh` (the shim) with the caller marker;
    0 status when the call could not run or the output cannot be parsed."""
    argv = ["gh", "api", "-i"]
    if etag:
        argv += ["-H", f"If-None-Match: {etag}"]
    argv.append(path)
    env = {**os.environ, "GH_SHIM_CALLER": CALLER}
    try:
        done = runner(argv, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError):
        return 0, {}, None
    out = done.stdout or ""
    status = _STATUS.search(out)
    if status is None:
        return 0, {}, None
    head, _, body = out.partition("\r\n\r\n") if "\r\n\r\n" in out else out.partition("\n\n")
    headers = {"etag": (m.group(1) if (m := _ETAG.search(head)) else None),
               "link": (m.group(1) if (m := _LINK.search(head)) else ""),
               "remaining": (int(m.group(1)) if (m := _REMAINING.search(head)) else None)}
    code = int(status.group(1))
    if code != 200:
        return code, headers, None
    try:
        items = json.loads(body)
    except ValueError:
        return 0, headers, None
    return code, headers, items if isinstance(items, list) else None


def _has_next(link: str) -> bool:
    return 'rel="next"' in (link or "")


def _receipt(record: dict) -> None:
    path = receipts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"bounded-scan: receipt not written ({exc})", file=sys.stderr)


def _project(items: list, fields: list[str] | None, scope: str) -> list:
    if scope == "open-issues":
        items = [i for i in items if "pull_request" not in i]
    if fields:
        items = [{k: i.get(k) for k in fields} for i in items]
    return items


def run(args: argparse.Namespace, runner=subprocess.run, now: float | None = None) -> int:
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    letter, scope = args.repo.upper(), args.scope
    repo = scan_grant.REPOS.get(letter)
    record = {"time": time.time() if now is None else now, "session": session, "repo": repo,
              "letter": letter, "scope": scope, "pages_fetched": 0, "pages_304": 0,
              "truncated": False}
    line = f"SCAN {letter} {scope}: <reason>"

    def refuse(reason: str) -> int:
        record.update(outcome="refused", detail=reason, rest_after=record.get("rest_before"),
                      graphql_after=record.get("graphql_before"))
        _receipt(record)
        print(f"bounded-scan: refused: {reason}", file=sys.stderr)
        return 2

    if repo is None:
        return refuse(f"no repo prefix matches {letter!r}")
    grant = scan_grant.live(repo, scope, session)
    if grant is None:
        return refuse(f"no live SCAN grant for {repo} {scope} in this session; "
                      f"the operator types, as the prompt's first line: {line}")
    record["reason"] = grant.get("reason")
    rest, graphql = gh_scan_patterns.budget(), gh_scan_patterns.graphql_budget()
    record["rest_before"] = rest[0] if rest else None
    record["graphql_before"] = graphql[0] if graphql else None
    fields = parse_fields(args.fields)
    snap_path = snapshot_path(repo, scope, fields)
    try:
        snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        snapshot = None
    moment = time.time() if now is None else now
    # Preclose pass 2: a fresh snapshot is served only when this request would
    # have produced it: complete, or truncated at exactly this run's max-pages.
    fresh = (snapshot and moment - snapshot.get("fetched_at", 0) < STALE_SECONDS
             and (not snapshot.get("truncated") or snapshot.get("max_pages") == args.max_pages))
    if fresh and not args.dry_run:
        record["truncated"] = bool(snapshot.get("truncated"))
        record.update(outcome="served-from-snapshot", rest_after=record["rest_before"],
                      graphql_after=record["graphql_before"])
        _receipt(record)
        print(str(snap_path))
        return 0
    pages = (snapshot or {}).get("pages", [])
    estimate = len(pages) or args.max_pages
    if rest is None:
        return refuse("REST core budget unknown; a bounded scan does not run on a guess")
    if rest[0] - estimate < FLOOR:
        gql = graphql[0] if graphql else "unknown"
        return refuse(f"REST core {rest[0]} − {estimate} page(s) estimated would cross the "
                      f"floor {FLOOR} (GraphQL {gql})")
    if args.dry_run:
        print(f"bounded-scan plan: {repo} {scope}: up to {estimate} page(s); REST core "
              f"{rest[0]}, GraphQL {graphql[0] if graphql else 'unknown'}; snapshot "
              f"{'present' if snapshot else 'none'} at {snap_path}")
        record.update(outcome="dry-run", rest_after=record["rest_before"],
                      graphql_after=record["graphql_before"])
        _receipt(record)
        return 0
    new_pages, page, remaining = [], 1, None
    while True:
        if page > args.max_pages:
            record["truncated"] = True
            break
        cached = pages[page - 1] if page - 1 < len(pages) else None
        code, headers, items = fetch(list_path(repo, scope, page),
                                     cached.get("etag") if cached else None, runner)
        if headers.get("remaining") is not None:
            remaining = headers["remaining"]
        if code == 304 and cached:
            record["pages_304"] += 1
            new_pages.append(cached)
            link = cached.get("link", "")
        elif code == 200 and items is not None:
            record["pages_fetched"] += 1
            link = headers.get("link", "")
            new_pages.append({"etag": headers.get("etag"), "link": link,
                              "items": _project(items, fields, scope)})
        else:
            record.update(outcome="error", detail=f"page {page}: HTTP {code or 'unreadable'}",
                          rest_after=remaining, graphql_after=None)
            _receipt(record)
            print(f"bounded-scan: page {page} failed (HTTP {code or 'unreadable'}); "
                  "snapshot left as it was", file=sys.stderr)
            return 1
        if not _has_next(link):
            break
        page += 1
    snap = {"repo": repo, "scope": scope, "fetched_at": moment, "truncated": record["truncated"],
            "max_pages": args.max_pages,
            "pages": new_pages}
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    # A temporary name unique to this process (preclose pass 1, survivor 4): two
    # concurrent runs each install a complete snapshot, last rename wins, and
    # neither can consume the other's temporary file.
    tmp = snap_path.with_name(f"{snap_path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_text(json.dumps(snap), encoding="utf-8")
    tmp.replace(snap_path)
    # Preclose pass 2: the after-reading is the last response's own
    # x-ratelimit-remaining, never the 60 s budget cache.
    record.update(outcome="ok", rest_after=remaining,
                  graphql_after=(gql_after[0] if (gql_after := gh_scan_patterns.graphql_budget())
                                 else None),
                  items=sum(len(p["items"]) for p in new_pages))
    _receipt(record)
    print(str(snap_path))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="repo letter (H, F, I, O)")
    parser.add_argument("--scope", required=True,
                        help="open-issues | open-prs | milestone=<number> | label=<name>")
    parser.add_argument("--fields", help="comma-separated item fields to keep")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--dry-run", action="store_true",
                        help="check the grant and the budgets, print the plan, fetch nothing")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
