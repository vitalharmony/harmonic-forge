#!/usr/bin/env python3
"""Record PR, link, issue, CI and fetch observations as events (harmonic-forge#1166, F637 T2a).

Fetch only: every event is an observation whose `event_id` is built from
identity that never changes, written through `emit()`. No event carries a
join decision -- `issue` is null on `pr.merged` and `pr.link.observed`, and a
link's target lives only in `attrs`. harmonic-forge#1167 (T2b) derives the
join, so a later observation revises it instead of conflicting with a stamp.

  pr.merged         gh-pr        <repo>#<pr>                       merged_at
  pr.link.observed  gh-pr        <repo>#<pr>:git-subject:<sha>      merged_at
                    gh-pr        <repo>#<pr>:pr-link-marker:<cid>   comment created_at
                    gh-timeline  <repo>#<pr>:timeline:...           event created_at
  issue.opened/closed gh-thread  <repo>#<issue>                     created_at / closed_at
  ci.conclusion     gate-run     <repo>@<sha>:<check run id>        completed_at
  fetch.status      gh-pr        <repo>:fetch:<run start>[:<pr>:<stage>]   run start

Unknown is recorded, never collapsed: a failed call emits `fetch.status`
`failed` with its stage and the run moves on; a PR whose steps all succeed
gets a per-PR `ok`, so a crash mid-PR reads as unresolved downstream. A
`connected` timeline event is recorded with a null target. Identity is
`apply_project_identity`, once per repo; attribution is `account_for`, with
an unregistered repo attributed `unresolved`.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "onboard"))
sys.path.insert(0, str(HERE.parent / "gh"))

import emit  # noqa: E402
import manifest  # noqa: E402
import manifest_identity  # noqa: E402
from lane_transition_report import _parse_marker_fields  # noqa: E402
from manifest_protocol import normalize_repo  # noqa: E402

UNRESOLVED = emit.UNRESOLVED
SUBJECT_RE = re.compile(r"\((?P<slug>[\w.-]+(?:/[\w.-]+)?)#(?P<issue>\d+)\)")

#: `gh_json(path) -> list[dict]`: every item of a paginated list endpoint, or
#: the single object for a non-list one. Injected so tests never call `gh`.
GhJson = Callable[[str], Any]


class FetchError(Exception):
    """A call failed; the caller records it as `fetch.status` `failed`."""


def gh_json(path: str) -> Any:
    """`gh api --paginate`, one JSON value per line (list items flattened)."""
    result = subprocess.run(["gh", "api", "--paginate", path, "--jq", "if type == \"array\" then .[] else . end"],
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise FetchError((result.stderr or f"gh exited {result.returncode}").strip().splitlines()[0][:200])
    items = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return items


def check_runs(gh: GhJson, repo: str, sha: str) -> list[dict]:
    pages = gh(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    runs: list[dict] = []
    for page in pages if isinstance(pages, list) else [pages]:
        runs.extend(page.get("check_runs", []) if isinstance(page, dict) and "check_runs" in page else [page])
    return runs


def attribute(repo: str, manifest_file: Path | None) -> dict[str, str]:
    """`account`/`org`/`repo`; an unregistered repo is `unresolved`, never raised."""
    key = normalize_repo(repo)
    try:
        account = manifest_identity.account_for(key, manifest_file)
    except manifest.ManifestError:
        account = UNRESOLVED
    return {"account": account, "org": key.split("/", 1)[0], "repo": key}


def target_repo_for(slug: str, repo: str, registry: Iterable[str]) -> str | None:
    """`owner/name` for a subject's `(slug#N)`: an explicit owner wins, then a
    registry repo of that name in this repo's org, then any registry repo of
    that name. None when nothing matches -- recorded, never guessed."""
    if "/" in slug:
        return normalize_repo(slug)
    name = slug.lower()
    owner = repo.split("/", 1)[0]
    matches = [r for r in registry if r.split("/", 1)[1] == name]
    same_org = [r for r in matches if r.split("/", 1)[0] == owner]
    if same_org:
        return same_org[0]
    return matches[0] if len(matches) == 1 else None


class Recorder:
    """Builds events for one repo and emits them per PR, so a crash loses
    nothing already observed."""

    def __init__(self, repo: str, started: str, where: dict[str, str]) -> None:
        self.repo, self.started, self.where = repo, started, where
        self.pending: list[dict] = []
        self.totals = {"written": 0, "duplicate": 0, "rejected": 0}

    def add(self, **event: Any) -> None:
        self.pending.append({**self.where, **event})

    def status(self, status: str, stage: str, *, pr: int | None = None, key: str | None = None, **attrs: Any) -> None:
        """`key` tells apart repo-level statuses that can repeat within a run
        (one per marker comment or foreign PR); event_id ignores attrs."""
        suffix = f":{pr}:{stage}" if pr is not None else (f"::{stage}" if status != "ok" else "")
        if key is not None:
            suffix += f":{key}"
        self.add(event_type="fetch.status", source="gh-pr", subject_id=f"{self.repo}:fetch:{self.started}{suffix}",
                 ts=self.started, attrs={"status": status, "stage": stage, "pr": pr, **attrs})

    def flush(self) -> int:
        """Emit what is pending; returns how many events emit() rejected."""
        if not self.pending:
            return 0
        counts = emit.emit(self.pending)
        self.pending = []
        self.totals["written"] += counts["written"]
        self.totals["duplicate"] += counts["duplicate"]
        self.totals["rejected"] += len(counts["rejected"])
        return len(counts["rejected"])


def _error_kind(exc: BaseException) -> str:
    return type(exc).__name__


def _int_or_none(value: str | None) -> int | None:
    """An ASCII decimal marker field as int, else None: `str.isdigit()` accepts
    characters such as '²' that `int()` rejects, and a value past 18 digits is
    no issue or PR number (and past 4,300 `int()` raises)."""
    return int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value) else None


def _repo_or_none(value: str | None) -> str | None:
    """`owner/name`, or None for marker text that is not one: untrusted
    input is recorded, never raised (normalize_repo raises ManifestError)."""
    if not value:
        return None
    try:
        return normalize_repo(value)
    except manifest.ManifestError:
        return None


def observe_pr(rec: Recorder, gh: GhJson, pr: dict, markers: dict[int, list[dict]], registry: list[str]) -> str | None:
    """Every observation for one merged PR; each step fails on its own.
    Returns the PR's CI summary when every step succeeded, else None."""
    repo, number = rec.repo, pr["number"]
    sha = pr.get("merge_commit_sha") or ""
    rec.add(event_type="pr.merged", source="gh-pr", subject_id=f"{repo}#{number}", ts=pr["merged_at"],
            pr=number, sha=sha or None)
    targets: set[int] = set()
    failed = False

    match = SUBJECT_RE.search(pr.get("title") or "")
    if match:
        target = target_repo_for(match.group("slug"), repo, registry)
        rec.add(event_type="pr.link.observed", source="gh-pr", provenance="git-subject",
                subject_id=f"{repo}#{number}:git-subject:{sha}", ts=pr["merged_at"], pr=number,
                attrs={"link_kind": "subject", "target_repo": target, "target_ref": match.group("slug"),
                       "target_issue": int(match.group("issue"))})
        if target == repo:
            targets.add(int(match.group("issue")))

    for comment in markers.get((repo, number), []):
        target, issue = record_marker(rec, repo, number, comment)
        if target == repo and issue is not None:
            targets.add(issue)

    try:
        timeline = gh(f"repos/{repo}/issues/{number}/timeline?per_page=100")
        for event in timeline:
            kind = event.get("event")
            if kind == "cross-referenced":
                source = (event.get("source") or {}).get("issue") or {}
                src_repo = normalize_repo(((source.get("repository") or {}).get("full_name")) or "") or None
                src_num = source.get("number")
                if src_num is None or source.get("pull_request"):
                    continue
                rec.add(event_type="pr.link.observed", source="gh-timeline", provenance="timeline",
                        subject_id=f"{repo}#{number}:timeline:cross-referenced:{src_repo}#{src_num}:{event['created_at']}",
                        ts=event["created_at"], pr=number,
                        attrs={"link_kind": "cross-referenced", "target_repo": src_repo, "target_issue": src_num})
                if src_repo == repo:
                    targets.add(int(src_num))
            elif kind == "connected":
                rec.add(event_type="pr.link.observed", source="gh-timeline", provenance="timeline",
                        subject_id=f"{repo}#{number}:timeline:connected:{event['created_at']}",
                        ts=event["created_at"], pr=number,
                        attrs={"link_kind": "connected", "target_repo": None, "target_issue": None})
    except Exception as exc:  # noqa: BLE001 -- recorded, never collapsed
        rec.status("failed", "timeline", pr=number, error_kind=_error_kind(exc))
        failed = True

    for issue in sorted(targets):
        try:
            data = gh(f"repos/{repo}/issues/{issue}")
            data = data[0] if isinstance(data, list) else data
            rec.add(event_type="issue.opened", source="gh-thread", subject_id=f"{repo}#{issue}",
                    ts=data["created_at"], issue=issue)
            if data.get("closed_at"):
                rec.add(event_type="issue.closed", source="gh-thread", subject_id=f"{repo}#{issue}",
                        ts=data["closed_at"], issue=issue)
        except Exception as exc:  # noqa: BLE001
            rec.status("failed", f"issue:{issue}", pr=number, error_kind=_error_kind(exc))
            failed = True

    ci = "none-found"
    if sha:
        try:
            runs = check_runs(gh, repo, sha)
            if any(r.get("status") != "completed" for r in runs):
                ci = "in-progress"
            for run in runs:
                if run.get("status") == "completed" and run.get("completed_at"):
                    ci = "found" if ci == "none-found" else ci
                    rec.add(event_type="ci.conclusion", source="gate-run", subject_id=f"{repo}@{sha}:{run['id']}",
                            ts=run["completed_at"], sha=sha, pr=number,
                            attrs={"conclusion": run.get("conclusion"), "run_id": run["id"], "check": run.get("name")})
        except Exception as exc:  # noqa: BLE001
            rec.status("failed", "ci", pr=number, error_kind=_error_kind(exc))
            failed = True

    return None if failed else ci


def marker_comments(gh: GhJson, repo: str) -> tuple[dict[tuple[str, int], list[dict]], list[object]]:
    """`({(pr_repo, pr): [marker comment]}, [unreadable comment ids])` from this
    repo's issue comments: Lane 1's `lane-pr-link v1` marker is written on the
    issue and names `pr-repo`/`pr`. A marker that cannot be read is returned by
    comment id, for a per-comment failure, never a failed listing."""
    found: dict[tuple[str, int], list[dict]] = {}
    unreadable: list[object] = []
    for comment in gh(f"repos/{repo}/issues/comments?per_page=100"):
        try:
            fields = _parse_marker_fields(comment.get("body") or "")
            if not fields:
                continue
            pr_repo = _repo_or_none(fields.get("pr-repo")) if fields.get("pr-repo") else repo
            if pr_repo is None or _int_or_none(fields.get("pr")) is None:
                unreadable.append(comment.get("id"))
                continue
            found.setdefault((pr_repo, int(fields["pr"])), []).append(
                {"id": comment["id"], "created_at": comment["created_at"], "fields": fields})
        except Exception:  # noqa: BLE001 -- one comment never fails the listing
            unreadable.append(comment.get("id") if isinstance(comment, dict) else None)
    return found, unreadable


def record_marker(rec: Recorder, pr_repo: str, number: int, comment: dict) -> tuple[str | None, int | None]:
    """One `pr-link-marker` observation about `pr_repo#number`; returns its target."""
    fields = comment["fields"]
    target = _repo_or_none(fields.get("issue-repo"))
    issue = _int_or_none(fields.get("issue"))
    # An unreadable issue-repo is recorded as a null target only: marker text
    # is never stored (ADR-009: ids, kinds, timestamps and hashes, no bodies).
    attrs = {"link_kind": "marker", "target_repo": target, "target_issue": issue}
    rec.add(event_type="pr.link.observed", source="gh-pr", provenance="pr-link-marker",
            subject_id=f"{pr_repo}#{number}:pr-link-marker:{comment['id']}", ts=comment["created_at"], pr=number,
            attrs=attrs)
    return target, issue


def fetch_repo(repo: str, gh: GhJson, *, limit: int | None, started: str, registry: list[str],
               manifest_file: Path | None, identity: Callable[[str], Any]) -> dict[str, int]:
    rec = Recorder(repo, started, attribute(repo, manifest_file))
    try:
        identity(repo)
    except BaseException as exc:  # SystemExit included (manifest_identity.py:206)
        if isinstance(exc, KeyboardInterrupt):
            raise
        rec.status("failed", "identity", error_kind=_error_kind(exc))
        rec.flush()
        return rec.totals
    try:
        merged = [p for p in gh(f"repos/{repo}/pulls?state=closed&per_page=100") if p.get("merged_at")]
        markers, unreadable = marker_comments(gh, repo)
    except Exception as exc:  # noqa: BLE001
        rec.status("failed", "list", error_kind=_error_kind(exc))
        rec.flush()
        return rec.totals
    # A marker on this repo's issue that names a PR in another repo is an
    # observation about that PR: record it here, attributed to the PR's repo,
    # since that repo's pass never reads this repo's comments.
    for comment_id in unreadable:
        rec.status("failed", "marker", key=f"comment-{comment_id}", error_kind="unreadable", comment_id=comment_id)
    for (pr_repo, number), comments in sorted(markers.items()):
        if pr_repo == repo:
            continue
        try:
            other = Recorder(pr_repo, started, attribute(pr_repo, manifest_file))
            for comment in comments:
                record_marker(other, pr_repo, number, comment)
            if other.flush():
                rec.status("failed", "emit", key=f"{pr_repo}#{number}", error_kind="rejected",
                           marker_pr=f"{pr_repo}#{number}")
        except Exception as exc:  # noqa: BLE001 -- one foreign PR never ends the run
            rec.status("failed", "marker", key=f"{pr_repo}#{number}", error_kind=_error_kind(exc),
                       marker_pr=f"{pr_repo}#{number}")
    merged.sort(key=lambda p: p["merged_at"], reverse=True)
    if limit is not None and len(merged) > limit:
        merged = merged[:limit]
        rec.status("capped", "list", merged_seen=len(merged),
                   oldest_merged_at=merged[-1]["merged_at"] if merged else None)
    for pr in merged:
        ci = None
        try:
            ci = observe_pr(rec, gh, pr, markers, registry)
        except Exception as exc:  # noqa: BLE001 -- one PR never ends the backfill
            rec.status("failed", "pr", pr=pr.get("number"), error_kind=_error_kind(exc))
        rejected = rec.flush()
        # The per-PR ok follows the flush, so an observation emit() refused is
        # a failure, never a fully observed PR.
        if rejected:
            rec.status("failed", "emit", pr=pr.get("number"), error_kind="rejected", rejected_count=rejected)
        elif ci is not None:
            rec.status("ok", "pr", pr=pr.get("number"), ci=ci)
        rec.flush()
    rec.status("ok", "repo", merged_seen=len(merged))
    rec.flush()
    return rec.totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", action="append", help="limit to these registry repos (default: all)")
    parser.add_argument("--limit", type=int, help="merged PRs per repo, newest first")
    parser.add_argument("--manifest", type=Path, help="projects.toml (default: the registry)")
    args = parser.parse_args(argv)
    registry = sorted(manifest.by_repo(args.manifest))
    repos = [normalize_repo(r) for r in args.repo] if args.repo else registry
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for repo in repos:
        totals = fetch_repo(repo, gh_json, limit=args.limit, started=started, registry=registry,
                            manifest_file=args.manifest,
                            identity=lambda r: manifest_identity.apply_project_identity(r, args.manifest))
        print(f"[pr-observations] {repo}: {json.dumps(totals)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
