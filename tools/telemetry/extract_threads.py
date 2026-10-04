#!/usr/bin/env python3
"""Tranche 1 thread-event extractor (harmonic-forge#829, F637 T1).

For every `projects.toml` repo, through that repo's own identity
(`apply_project_identity`), lists issues (one paginated list, as the #828
census does), and for each issue with comments fetches its comments and
timeline over REST, turns lane markers and timeline facts into schema-v1
events (`eras.py`), and `emit()`s them to the `gh-thread` and `gh-timeline`
partitions. `emit()` dedupes on `event_id`, so a re-run over the same window
writes nothing and leaves every file byte-identical (AC1).

- **REST only** (AC4): every call goes through `rest_get`, which refuses a
  GraphQL path. No board scans.
- **ETag-cached**: each page's ETag and body live under
  `$XDG_CACHE_HOME/harmonic-forge/telemetry-etag/` (mode 700, files 600), so
  an unchanged page is a 304, which GitHub does not count against the quota.
  **The cache holds raw REST pages, comment bodies included** -- the
  "never bodies" rule binds the event store, not this local cache. Entries
  unused for 30 days and orphaned `.tmp` files are pruned on each run.
- **A full last page is always followed.** GitHub sends no `rel="next"` on a
  page that is the last one *when fetched*; once cached, a 304 would stop
  there forever after comment 101 lands. So a page of `per_page` items with
  no next link is followed by an explicit `page=N+1` probe.
- **Quota recorded** (AC4): core `remaining` is read per account before and
  after, and the cost per 100 issues is printed in the run summary. A
  negative difference means the rate-limit window reset mid-run: the cost is
  then recorded as unmeasurable (`null` plus `quota_note`), never negative.

Usage:
    python3 tools/telemetry/extract_threads.py [--repo owner/name] [--issue N]
                                               [--since ISO] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parent / "onboard")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import emit  # noqa: E402
import eras  # noqa: E402

#: path -> list of decoded JSON pages. Injected in tests.
RestGet = Callable[[str], list[Any]]
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_PAGE = re.compile(r"[?&]page=(\d+)")
PER_PAGE = 100
_CACHE_MAX_AGE_S = 30 * 86400
_TMP_MAX_AGE_S = 3600
_pruned: set[str] = set()


class GraphQLRefused(RuntimeError):
    pass


def _cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = base / "harmonic-forge" / "telemetry-etag"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)  # mkdir's mode is ignored when the directory exists
    if str(path) not in _pruned:
        _pruned.add(str(path))
        _prune(path)
    return path


def _prune(path: Path) -> None:
    """Drop entries unused for 30 days and orphaned temp files."""
    now = time.time()
    for entry in path.iterdir():
        try:
            age = now - entry.stat().st_mtime
            if age > (_TMP_MAX_AGE_S if entry.suffix == ".tmp" else _CACHE_MAX_AGE_S):
                entry.unlink()
        except OSError:
            continue


def _with_page(url: str, number: int) -> str:
    base, _, query = url.partition("?")
    params = [p for p in query.split("&") if p and not p.startswith("page=")]
    return f"{base}?{'&'.join(params + [f'page={number}'])}"


def _split_response(text: str) -> tuple[int, dict[str, str], str]:
    head, _, body = text.partition("\r\n\r\n") if "\r\n\r\n" in text else text.partition("\n\n")
    lines = head.splitlines()
    status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else 0
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return status, headers, body


def rest_get(path: str) -> list[Any]:
    """Every page of a REST GET, ETag-cached per page. Raises on GraphQL."""
    if "graphql" in path.lower():
        raise GraphQLRefused(path)
    pages, url = [], path
    scope = os.environ.get("GH_CONFIG_DIR", "")
    while url:
        key = hashlib.sha256(f"{scope}\x1f{url}".encode()).hexdigest()
        cached = _cache_dir() / f"{key}.json"
        try:
            entry = json.loads(cached.read_text()) if cached.exists() else None
        except (ValueError, OSError):
            # A torn or corrupt entry is a cache miss, never a permanent failure.
            print(f"[extract] discarding corrupt cache entry {cached}", file=sys.stderr)
            cached.unlink(missing_ok=True)
            entry = None
        cmd = ["gh", "api", "-i", url]
        if entry and entry.get("etag"):
            cmd[3:3] = ["-H", f"If-None-Match: {entry['etag']}"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        status, headers, body = _split_response(result.stdout)
        if status == 304 and entry:
            page, link = entry["page"], headers.get("link") or entry.get("link", "")
            os.utime(cached)  # still in use: keep it out of the 30-day prune
        elif result.returncode == 0 and status == 200:
            page, link = json.loads(body), headers.get("link", "")
            fd, tmp = tempfile.mkstemp(dir=cached.parent, prefix=cached.stem, suffix=".tmp")
            with os.fdopen(fd, "w") as handle:  # mkstemp: per-process, mode 600
                handle.write(json.dumps({"etag": headers.get("etag"), "page": page, "link": link}))
            os.replace(tmp, cached)
        else:
            raise RuntimeError(f"gh api {url}: HTTP {status} {result.stderr.strip()[:200]}")
        pages.append(page)
        nxt = _NEXT.search(link or "")
        number = int(m.group(1)) if (m := _PAGE.search(url)) else 1
        if nxt:
            url = nxt.group(1).split("api.github.com/", 1)[-1]
        elif isinstance(page, list) and len(page) >= PER_PAGE:
            url = _with_page(url, number + 1)  # full last page: probe for more
        else:
            url = ""
    return pages


def core_remaining() -> Optional[int]:
    result = subprocess.run(["gh", "api", "rate_limit", "--jq", ".resources.core.remaining"],
                            capture_output=True, text=True)
    return int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip().isdigit() else None


def issues_with_comments(repo: str, get: RestGet, since: Optional[str]) -> list[int]:
    path = f"repos/{repo}/issues?state=all&per_page=100&sort=created&direction=asc"
    if since:
        path += f"&since={since}"
    return [item["number"] for page in get(path) for item in page
            if "pull_request" not in item and item.get("comments", 0) > 0]


def _own(comment: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """`(footer, kind)` of a comment's own l1-post footer."""
    footer = eras.own_footer(eras.clean(comment.get("body") or ""))
    kind = eras.lane_state._FOOTER_KIND.search(footer or "")
    return footer, (kind.group("kind").lower() if kind else None)


def pair_gates_with_specs(comments: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
    """harmonic-forge#893: give each classified gate event its per-class fails.

    For each measured gate (whatever its kind), the newest EARLIER `kind=spec`
    comment is its spec, and a spec without `classes=` carries nothing. When
    their case ids match, the gate's event gains `paired: true`, the spec's
    class counts (`case_ac`, `case_existing`, `case_live`) and
    `fail_*`/`blocked_*` per class; otherwise `paired: false` and the gate's
    measurement becomes `unpaired`. Scalars only (schema v1)."""
    by_id = {str(e.get("attrs", {}).get("comment_id")): e for e in events
             if (e.get("attrs") or {}).get("measurement") == "measured"}
    latest_spec: Optional[dict[str, str]] = None
    spec_id = "none"
    for comment in comments:
        footer, kind = _own(comment)
        # The shared recognizer (#893 reforge pass 1): a spec posted as
        # `discussion` carries, and resets, the classes exactly as a
        # `--kind spec` post does.
        if eras.lane3_artifact_of(eras.clean(comment.get("body") or ""), kind, footer) == "spec":
            # Every spec resets what is carried, classified or not: a newer
            # unclassified spec must never pair a gate with an older round's
            # classes (#893 preclose pass 2).
            latest_spec = eras.parse_case_map(footer, "classes") or None
            # The spec's edit instant is part of the key too: reclassifying a
            # spec in place keeps its id (#893 post-verdict check).
            spec_id = (f"{comment.get('id') or 'none'}@{comment.get('updated_at') or comment.get('created_at') or ''}"
                       if latest_spec else "none")
            continue
        event = by_id.get(str(comment.get("id") or ""))
        if event is None:
            continue
        results = eras.parse_case_map(footer, "results")
        attrs = event["attrs"]
        # The pairing input is part of the reading's identity (#893 e1
        # sticky-wicket #8): the attrs depend on a sibling comment, so a
        # re-run after that spec changes or is deleted must be a new event,
        # not a duplicate the store drops. latest_readings then keeps the
        # newer extraction.
        paired = latest_spec is not None and set(latest_spec) == set(results)
        event["subject_id"] += f"/pair:{spec_id if paired else 'unpaired'}"
        if not paired:
            attrs["paired"] = False
            attrs["measurement"] = "unpaired"
            continue
        attrs["paired"] = True
        for cls in ("ac", "existing", "live"):
            ids = [k for k, v in latest_spec.items() if v == cls]
            attrs[f"case_{cls}"] = len(ids)
            attrs[f"fail_{cls}"] = sum(1 for k in ids if results.get(k) == "fail")
            attrs[f"blocked_{cls}"] = sum(1 for k in ids if results.get(k) == "blocked")


def issue_events(repo: str, number: int, get: RestGet, *, account: Optional[str],
                 org: Optional[str]) -> list[dict[str, Any]]:
    where = {"account": account, "org": org, "repo": repo, "issue": number}
    events: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    for page in get(f"repos/{repo}/issues/{number}/comments?per_page=100"):
        for comment in page:
            comments.append(comment)
            events.extend(eras.comment_events(comment, **where))
    pair_gates_with_specs(comments, events)
    for page in get(f"repos/{repo}/issues/{number}/timeline?per_page=100"):
        for item in page:
            events.extend(eras.timeline_events(item, **where))
    return events


def extract_repo(repo: str, account: Optional[str], get: RestGet, *, since: Optional[str] = None,
                 only: Optional[int] = None, dry_run: bool = False) -> dict[str, Any]:
    org = repo.split("/", 1)[0] if "/" in repo else None
    numbers = [only] if only else issues_with_comments(repo, get, since)
    summary: dict[str, Any] = {"repo": repo, "account": account, "issues": len(numbers),
                               "events": 0, "written": 0, "duplicate": 0, "rejected": 0}
    for number in numbers:
        events = issue_events(repo, number, get, account=account, org=org)
        summary["events"] += len(events)
        if dry_run:
            continue
        counts = emit.emit(events)
        summary["written"] += counts["written"]
        summary["duplicate"] += counts["duplicate"]
        summary["rejected"] += len(counts["rejected"])
        for index, event_type, reason in counts["rejected"]:
            print(f"[extract] {repo}#{number} rejected {event_type}: {reason}", file=sys.stderr)
    return summary


def run(projects: Iterable[Any], get: RestGet, identity: Callable[[str], Any],
        quota: Callable[[], Optional[int]], **opts: Any) -> list[dict[str, Any]]:
    summaries = []
    only_repo = opts.pop("repo", None)
    projects = list(projects)
    if only_repo and not any(p.repo == only_repo for p in projects):
        # An unmatched filter must not look like an idempotent no-op re-run.
        return [{"repo": only_repo, "error": "not in the registry (projects.toml)"}]
    for project in projects:
        if not project.repo or (only_repo and project.repo != only_repo):
            if not project.repo:
                summaries.append({"repo": f"unresolved ({project.name})", "error": "no repo in registry"})
            continue
        try:
            identity(project.repo)
            before = quota()
            summary = extract_repo(project.repo, project.account, get, **opts)
            after = quota()
            cost = before - after if before is not None and after is not None else None
            if cost is not None and cost < 0:
                summary["quota_note"] = "rate-limit window reset during the run; cost not measurable"
                cost = None
            summary["quota_cost"] = cost
            summary["quota_per_100_issues"] = (round(cost * 100 / summary["issues"], 1)
                                               if cost is not None and summary["issues"] else None)
        except (Exception, SystemExit) as exc:  # one repo never loses the others
            summary = {"repo": project.repo, "account": project.account,
                       "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        print(json.dumps(summary, sort_keys=True), file=sys.stderr)
        summaries.append(summary)
    return summaries


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo")
    parser.add_argument("--issue", type=int, dest="only")
    parser.add_argument("--since")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    import manifest  # noqa: PLC0415
    from manifest_identity import apply_project_identity  # noqa: PLC0415

    summaries = run(manifest.load(args.manifest), rest_get, apply_project_identity, core_remaining,
                    repo=args.repo, since=args.since, only=args.only, dry_run=args.dry_run)
    print(json.dumps(summaries, indent=2, sort_keys=True))
    return 1 if any("error" in s for s in summaries) else 0


if __name__ == "__main__":
    sys.exit(main())
