#!/usr/bin/env python3
"""The never-refuse posts sample: lane posts that were never later corrected,
derived from the issues the preclose receipts name (harmonic-forge#1450).

  derive  list the issues named by the preclose receipts
          (`~/.claude/state/preclose/<owner>_<repo>_<N>.json`, nothing else),
          read each issue's comments with one `gh api` call (per_page=100,
          no paging), keep the marked lane posts no later post corrected, and
          write exactly `SIZE` (200) rows plus their manifest, manifest first.

A post is a candidate when the last lane-tool footer in its body names a
candidate kind and a lane. It is superseded, and excluded, when a later
candidate by the same lane on the same issue, within `WINDOW` (7 days), is
either the same kind or a `rework` (every rework, whatever its `rework=`).
A post whose window has not closed yet is immature and excluded; so is every
post on a thread with a next page (a superseder could be on it), an empty
body once the footer is removed, and a duplicate body. At most `PER_ISSUE`
posts come from one issue.

The loop stops at `SIZE` accepted posts, at `MAX_CALLS` reads, before a read
would take the REST core budget below `FLOOR + MARGIN`, and on an unknown
budget. A fetched page is cached under the output's folder, so a run stopped
by the budget resumes without reading an issue twice.

The sample holds private-repo text, so it lives in operator-memory and never
in harmonic-forge: an `--out` inside a forge checkout is refused. Nothing here
prints a body; only counts.

Exit codes: 0 written; 2 refused, nothing written (an `--out` inside forge, a
wrong name, an existing sample, an orphan, a leftover temp file, fewer than
`SIZE` accepted posts, an unknown or exhausted budget); 3 any other error.
Never 1.

Stdlib only; the network is reached only through the `gh` subprocess.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS / "memory"))
sys.path.insert(0, str(TOOLS / "gh"))
sys.path.insert(0, str(HERE))

import bounded_scan  # noqa: E402
import capture_failure  # noqa: E402
import gh_scan_patterns  # noqa: E402
import never_refuse_sample  # noqa: E402

SIZE = 200
#: At most this many posts from one issue (Lane 1 ruling 4), so a few long
#: threads cannot dominate the sample.
PER_ISSUE = 3
WINDOW = dt.timedelta(days=7)
MAX_CALLS = 300
FLOOR = bounded_scan.FLOOR
MARGIN = 25
PER_PAGE = 100
DRY_RUN_ISSUES = 20

DEFAULT_RECEIPTS = Path.home() / ".claude" / "state" / "preclose"
DEFAULT_OUT = capture_failure.MEMORY_STORE / "never-refuse" / "never-refuse-posts-v1.jsonl"
NAME_PATTERN = "never-refuse-posts*.jsonl"
TEMP_PREFIX = "never-refuse-posts"
TEMP_PATTERN = "never-refuse-posts*.tmp"
CACHE_DIR = "posts-cache"
MANIFEST_SUFFIX = never_refuse_sample.MANIFEST_SUFFIX
manifest_path = never_refuse_sample.manifest_path
orphan_refusal = never_refuse_sample.orphan_refusal

EXIT_REFUSED = 2
EXIT_ERROR = 3

#: A preclose receipt, and only a receipt: `.lock`, `.kill.json`,
#: `.enrollment.jsonl` and `.json.pre-*-bak` never match. The owner and repo
#: hold no underscore, so a hyphenated repo name splits correctly.
RECEIPT = re.compile(r"^([A-Za-z0-9-]+)_([A-Za-z0-9.-]+)_([0-9]+)\.json$")

#: The footer every lane tool appends (l1_post.py, l2_post.py,
#: post_lane_discussion.py). Fields are read inside the LAST marker only
#: (harmonic-forge#269 scoping): the tools append it at the end, so an earlier
#: one is quoted text.
MARKER = re.compile(r"<!--\s*l1-post\s+v1;.*?-->", re.DOTALL)
_KIND = re.compile(r"(?:^|[;\s])kind=([\w-]+)")
_POSTED_BY = re.compile(r"(?:^|[;\s])posted-by=([\w-]+)")
#: l1_post.py's `--kind` choices: its footer carries no `posted-by=`.
L1_KINDS = frozenset({"handoff", "ready-for-l3", "sweep", "ae", "ae-and-sweep", "rework", "carry-forward"})
#: Posts the sample may hold. `blocked` and `finding` are left out (Lane 1
#: ruling 2); `rework` is a candidate, so it also supersedes.
CANDIDATE_KINDS = L1_KINDS | {"completion", "plan", "spec", "gate-result", "discussion"}


class Refused(Exception):
    """Exit 2."""


class Failed(Exception):
    """Exit 3."""


# --------------------------------------------------------------------------
# The issue set.

def issue_set(folder: Path) -> list[tuple[str, int]]:
    """`(owner/repo, N)` for every receipt file, newest issue first per repo."""
    found = set()
    for name in os.listdir(folder):
        match = RECEIPT.match(name)
        if match:
            found.add((f"{match.group(1)}/{match.group(2)}", int(match.group(3))))
    return sorted(found, key=lambda item: (item[0], -item[1]))


# --------------------------------------------------------------------------
# Classification.

def marker_fields(body: str) -> tuple[str | None, str | None]:
    """`(kind, posted_by)` read inside the last footer marker, or Nones."""
    markers = list(MARKER.finditer(body or ""))
    if not markers:
        return None, None
    text = markers[-1].group(0)
    kind, posted = _KIND.search(text), _POSTED_BY.search(text)
    return (kind.group(1) if kind else None), (posted.group(1) if posted else None)


def lane_of(kind: str | None, posted_by: str | None) -> str | None:
    if kind is None:
        return None
    if posted_by is not None:
        return "LANE1" if posted_by == "LANE-unset" else posted_by
    return "LANE1" if kind in L1_KINDS else None


def classify(comment: dict) -> dict | None:
    """`{kind, lane, t, body}` for a candidate-or-rework comment, else None."""
    body = comment.get("body")
    if not isinstance(body, str):
        return None
    kind, posted_by = marker_fields(body)
    lane = lane_of(kind, posted_by)
    if lane is None or kind not in CANDIDATE_KINDS:
        return None
    try:
        t = dt.datetime.fromisoformat(str(comment["created_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return {"kind": kind, "lane": lane, "t": t, "body": body}


def superseded(post: dict, others: list[dict]) -> bool:
    """A later same-lane post inside the window: the same kind, or any rework."""
    return any(c is not post and c["lane"] == post["lane"]
               and post["t"] < c["t"] <= post["t"] + WINDOW
               and (c["kind"] == post["kind"] or c["kind"] == "rework")
               for c in others)


def stripped(body: str) -> str:
    return MARKER.sub("", body).strip()


def select(items: list, truncated: bool, now: dt.datetime, counts: dict) -> list[dict]:
    """The issue's accepted posts in time order, uncapped; counts updated."""
    posts = []
    for comment in items:
        post = classify(comment) if isinstance(comment, dict) else None
        if post is None:
            counts["unmarked"] += 1
        else:
            posts.append(post)
    counts["candidates"] += len(posts)
    if truncated:
        counts["truncated"] += len(posts)
        return []
    accepted = []
    for post in sorted(posts, key=lambda p: p["t"]):
        if post["t"] > now - WINDOW:
            counts["immature"] += 1
        elif superseded(post, posts):
            counts["superseded"] += 1
        elif not stripped(post["body"]):
            counts["empty"] += 1
        else:
            accepted.append(post)
    return accepted


# --------------------------------------------------------------------------
# Fetching.

def comments_path(repo: str, n: int) -> str:
    return f"repos/{repo}/issues/{n}/comments?per_page={PER_PAGE}"


def gh_fetch(repo: str, n: int, runner=subprocess.run) -> tuple[int, dict, list | None]:
    """`(status, {"link", "remaining"}, items)` from one `gh api -i` read; 0
    status when the call could not run or its output cannot be parsed."""
    argv = ["gh", "api", "-i", comments_path(repo, n)]
    try:
        done = runner(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 0, {"link": "", "remaining": None}, None
    out = done.stdout or ""
    status = bounded_scan._STATUS.search(out)
    if status is None:
        return 0, {"link": "", "remaining": None}, None
    head, _, body = out.partition("\r\n\r\n") if "\r\n\r\n" in out else out.partition("\n\n")
    headers = {"link": (m.group(1) if (m := bounded_scan._LINK.search(head)) else ""),
               "remaining": (int(m.group(1)) if (m := bounded_scan._REMAINING.search(head)) else None)}
    code = int(status.group(1))
    if code != 200:
        return code, headers, None
    try:
        items = json.loads(body)
    except ValueError:
        return 0, headers, None
    return code, headers, items if isinstance(items, list) else None


def rest_budget() -> int | None:
    budget = gh_scan_patterns.budget()
    return budget[0] if budget else None


def cache_file(cache: Path, repo: str, n: int) -> Path:
    return cache / f"{repo.replace('/', '_')}_{n}.json"


# --------------------------------------------------------------------------
# Writing.

def out_refusal(out: Path) -> str | None:
    refusal = capture_failure.forge_refusal(out, "--out")
    if refusal:
        return refusal
    if not fnmatch.fnmatch(out.name, NAME_PATTERN):
        return f"--out {out} must be named {NAME_PATTERN}"
    manifest = manifest_path(out)
    if out.exists() and manifest.exists():
        return f"already derived: {out} and its manifest exist; the sample is frozen"
    if out.exists():
        return orphan_refusal(out)
    if manifest.exists():
        return f"orphan manifest: delete {manifest} and re-run"
    if out.parent.is_dir():
        leftovers = sorted(p.name for p in out.parent.iterdir() if fnmatch.fnmatch(p.name, TEMP_PATTERN))
        if leftovers:
            return (f"leftover temp from an interrupted derive: delete {', '.join(leftovers)} "
                    f"in {out.parent} and re-run")
    return None


def _write_temp(folder: Path, data: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".tmp", dir=folder)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
        raise
    finally:
        if fd != -1:
            os.close(fd)
    return Path(name)


def serialized(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def _publish(out: Path, data: bytes, manifest: dict) -> None:
    """Temp files, then `os.link`, which refuses an existing name, so a racing
    derive never overwrites a frozen sample (never_refuse_sample.py's sequence)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest_tmp = sample_tmp = None
    try:
        manifest_tmp = _write_temp(out.parent, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
        sample_tmp = _write_temp(out.parent, data)
        try:
            os.link(sample_tmp, out)
        except FileExistsError:
            raise Refused(f"already derived: {out} appeared while this derive ran; the sample is frozen") from None
        try:
            os.link(manifest_tmp, manifest_path(out))
        except FileExistsError:
            out.unlink()
            raise Refused(f"orphan manifest appeared while this derive ran: delete {manifest_path(out)} "
                          "and re-run") from None
    finally:
        for tmp in (sample_tmp, manifest_tmp):
            if tmp is not None:
                with contextlib.suppress(FileNotFoundError):
                    tmp.unlink()


# --------------------------------------------------------------------------

COUNT_KEYS = ("issues_read", "calls", "cached", "unreadable", "candidates", "superseded", "immature",
              "truncated", "unmarked", "empty", "duplicate", "over_issue_cap")


def derive(receipts: Path, out: Path, now: dt.datetime | None = None,
           fetch: Callable = gh_fetch, budget: Callable[[], int | None] = rest_budget,
           dry_run: bool = False) -> dict:
    out = Path(out).expanduser()
    refusal = out_refusal(out)
    if refusal:
        raise Refused(refusal)
    now = now or dt.datetime.now(dt.timezone.utc)
    issues = issue_set(Path(receipts).expanduser())
    if dry_run:
        issues = issues[:DRY_RUN_ISSUES]
    cache = out.parent / CACHE_DIR
    counts = dict.fromkeys(COUNT_KEYS, 0)
    remaining = budget()
    stop = None
    rows: list[dict] = []
    digests: set[str] = set()
    for repo, n in issues:
        if len(rows) >= SIZE:
            stop = "size"
            break
        cached = cache_file(cache, repo, n)
        page = None
        if cached.is_file():
            with contextlib.suppress(OSError, ValueError):
                page = json.loads(cached.read_text(encoding="utf-8"))
        if isinstance(page, dict) and isinstance(page.get("items"), list):
            counts["cached"] += 1
        else:
            if counts["calls"] >= MAX_CALLS:
                stop = "max-calls"
                break
            if remaining is None:
                stop = "budget-unknown"
                break
            if remaining - 1 < FLOOR + MARGIN:
                stop = "budget-floor"
                break
            status, headers, items = fetch(repo, n)
            counts["calls"] += 1
            remaining = headers.get("remaining")
            if status in (404, 410):
                counts["unreadable"] += 1
                continue
            if status != 200 or items is None:
                raise Failed(f"reading {repo}#{n} returned status {status}")
            page = {"items": items, "truncated": bounded_scan._has_next(headers.get("link", ""))}
            if not dry_run:
                cache.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
        counts["issues_read"] += 1
        taken = 0
        for post in select(page["items"], bool(page.get("truncated")), now, counts):
            digest = hashlib.sha256(post["body"].encode("utf-8")).hexdigest()
            if digest in digests:
                counts["duplicate"] += 1
                continue
            if taken >= PER_ISSUE or len(rows) >= SIZE:
                counts["over_issue_cap"] += 1
                continue
            digests.add(digest)
            taken += 1
            rows.append({"repo": repo, "issue": n, "kind": post["kind"], "body_digest": digest,
                         "body": post["body"]})
    if stop is None and len(rows) >= SIZE:
        stop = "size"
    counts["rest_after"] = remaining
    counts["stop"] = stop or "issues-exhausted"
    counts["accepted"] = len(rows)
    if dry_run:
        return counts
    if len(rows) < SIZE:
        raise Refused(f"only {len(rows)} posts accepted, fewer than {SIZE}; nothing written ({_counts(counts)})")
    data = "".join(serialized(r) + "\n" for r in rows).encode("utf-8")
    manifest = {"version": 1, "size": SIZE, "derived_at": now.isoformat(),
                "sha256": hashlib.sha256(data).hexdigest(), **counts}
    refusal = out_refusal(out)
    if refusal:
        raise Refused(refusal)
    _publish(out, data, manifest)
    return manifest


def _counts(counts: dict) -> str:
    return ", ".join(f"{k} {counts[k]}" for k in (*COUNT_KEYS, "accepted", "rest_after", "stop") if k in counts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("derive", help="Derive and freeze the posts sample.")
    d.add_argument("--receipts", type=Path, default=DEFAULT_RECEIPTS)
    d.add_argument("--out", type=Path, default=DEFAULT_OUT)
    d.add_argument("--dry-run", action="store_true",
                   help=f"Read the first {DRY_RUN_ISSUES} issues, print counts, write nothing.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = derive(args.receipts, args.out, dry_run=args.dry_run)
        label = "dry run" if args.dry_run else f"wrote {SIZE} rows to {Path(args.out).expanduser()}"
        print(f"never-refuse-posts: {label} ({_counts(result)})")
        sys.stdout.flush()
        return 0
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Failed as exc:
        print(f"never-refuse-posts: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - every other error is exit 3, never 1
        print(f"never-refuse-posts: error: {type(exc).__name__}", file=sys.stderr)
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.flush()
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
