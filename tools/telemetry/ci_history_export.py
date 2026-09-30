#!/usr/bin/env python3
"""Export CI pass/fail history before GitHub's retention deletes it (harmonic-forge#826).

From 2026-10-01 GitHub applies each repo's Actions retention to checks,
workflow runs and commit statuses, not only logs and artifacts. A public repo
caps at 90 days, and some org plans cap private repos there too, so CI history
older than the window stops existing. This walks the registry and, for every
repo whose `maximum_allowed_days` is under 400, archives the **metadata** of
every completed workflow run, its jobs, and every commit status. It never
exports log bodies ("events, not bodies", design.md).

Registry-driven through `tools/onboard/manifest.py`; every GitHub call runs
under the repo's own identity via `apply_project_identity` (harmonic-forge
#804) -- never `gh auth switch`, never `GH_TOKEN`. Idempotent: ids already
exported are recorded under `<archive root>/_state/ci-history/`, so a re-run
(the recurring timer) appends only what is new.

Usage:
    python3 tools/telemetry/ci_history_export.py            # export
    python3 tools/telemetry/ci_history_export.py --dry-run  # count only
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

HERE = Path(__file__).resolve().parent
ONBOARD = HERE.parent / "onboard"
for _p in (str(HERE), str(ONBOARD), str(HERE.parent.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import archive  # noqa: E402

FULL_RETENTION_DAYS = 400
SOURCE = "ci-history"

RUN_FIELDS = ("id", "name", "event", "status", "conclusion", "head_sha", "head_branch",
              "run_number", "run_attempt", "workflow_id", "created_at", "run_started_at",
              "updated_at", "html_url")
JOB_FIELDS = ("id", "run_id", "run_attempt", "name", "status", "conclusion", "head_sha",
              "started_at", "completed_at", "html_url")
# `description` is free text a status author chose; left out on purpose.
STATUS_FIELDS = ("id", "context", "state", "created_at", "updated_at", "target_url")

GhJson = Callable[[str], Iterable[Any]]


def gh_json_lines(path: str) -> list[Any]:
    """`gh api --paginate` with each page's items emitted as JSON lines."""
    result = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path}: {result.stderr.strip()[:200]}")
    items: list[Any] = []
    decoder = json.JSONDecoder()
    text, idx = result.stdout, 0
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        page, idx = decoder.raw_decode(text, idx)
        items.append(page)
    return items


def _state_path(repo: str) -> Path:
    # Outside the archive root (preclose pass 3): deleting an account's partition
    # must not leave dedupe state that hides its history from the next export.
    return archive.failure_log().parent / "ci-history-state" / (repo.replace("/", "__") + ".json")


def _load_state(repo: str) -> dict[str, set]:
    try:
        raw = json.loads(_state_path(repo).read_text(encoding="utf-8"))
        return {k: set(v) for k, v in raw.items()}
    except (OSError, ValueError):
        return {"run": set(), "job": set(), "status": set()}


def _save_state(repo: str, state: dict[str, set]) -> None:
    path = _state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: sorted(v) for k, v in state.items()}), encoding="utf-8")
    tmp.replace(path)


def _pick(obj: dict, fields: Iterable[str]) -> dict:
    return {f: obj.get(f) for f in fields}


def retention(repo: str, gh: GhJson) -> Optional[dict]:
    try:
        pages = list(gh(f"repos/{repo}/actions/permissions/artifact-and-log-retention"))
        return pages[0] if pages else None
    except RuntimeError:
        return None  # e.g. 404 without admin on a client org: unknown, so export


def export_repo(repo: str, origin: archive.Origin, gh: GhJson, *, dry_run: bool) -> dict[str, int]:
    """Archive what is new since the last run.

    **Incremental, oldest first.** Each run is archived with its jobs and
    recorded in the dedupe state before the next run's jobs are fetched, and
    runs go oldest-first -- the soonest to fall out of GitHub's retention
    window. A transient `gh` failure mid-pass therefore keeps everything
    already done, and the next pass resumes where it stopped. Statuses follow
    the same rule per commit, oldest commit first.

    **At-least-once.** A run is marked done only together with its jobs, so a
    failure can never strand a run's jobs. A crash between an append and the
    state save re-archives that one batch; every record carries `kind` + `id`,
    the key F637's store dedupes on (design.md: a deterministic `event_id`
    makes re-extraction idempotent)."""
    state = _load_state(repo)
    runs: list[dict] = []
    for page in gh(f"repos/{repo}/actions/runs?per_page=100"):
        runs.extend(r for r in page.get("workflow_runs", []) if r.get("status") == "completed")
    runs.sort(key=lambda r: (r.get("created_at") or "", r["id"]))
    new_runs = [r for r in runs if r["id"] not in state["run"]]

    commits: list[tuple[str, str]] = []
    for page in gh(f"repos/{repo}/commits?per_page=100"):
        for c in page:
            if isinstance(c, dict) and c.get("sha"):
                when = ((c.get("commit") or {}).get("committer") or {}).get("date") or ""
                commits.append((when, c["sha"]))
    seen = {sha for _, sha in commits}
    commits.extend(("", r["head_sha"]) for r in runs if r.get("head_sha") and r["head_sha"] not in seen)
    commits.sort()

    counts = {"runs": 0, "jobs": 0, "statuses": 0}
    for run in new_runs:
        jobs: list[dict] = []
        for page in gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100&filter=all"):
            jobs.extend({"kind": "job", **_pick(job, JOB_FIELDS)} for job in page.get("jobs", [])
                        if job.get("status") == "completed")
        batch = [{"kind": "workflow_run", **_pick(run, RUN_FIELDS)}, *jobs]
        counts["runs"] += 1
        counts["jobs"] += len(jobs)
        if dry_run:
            continue
        written = archive.archive(SOURCE, batch, origin=origin)
        if written != len(batch):
            raise RuntimeError(f"{repo}: archived {written} of {len(batch)} records for run {run['id']}")
        state["run"].add(run["id"])
        state["job"].update(j["id"] for j in jobs)
        _save_state(repo, state)

    for _when, sha in commits:
        batch = []
        for page in gh(f"repos/{repo}/commits/{sha}/statuses?per_page=100"):
            batch.extend({"kind": "status", "sha": sha, **_pick(st, STATUS_FIELDS)}
                         for st in page if st.get("id") not in state["status"])
        counts["statuses"] += len(batch)
        if dry_run or not batch:
            continue
        written = archive.archive(SOURCE, batch, origin=origin)
        if written != len(batch):
            raise RuntimeError(f"{repo}: archived {written} of {len(batch)} status records for {sha}")
        state["status"].update(r["id"] for r in batch)
        _save_state(repo, state)
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--manifest", type=Path, default=None, help="fixture registry (tests)")
    parser.add_argument("--repo", action="append", default=None,
                        help="limit to these registry repos (repeatable); default: all")
    args = parser.parse_args(argv)
    only = {r.lower() for r in args.repo} if args.repo else None

    import manifest  # noqa: PLC0415
    from manifest_identity import apply_project_identity  # noqa: PLC0415

    report: list[dict] = []
    failed = False
    for project in manifest.load(args.manifest):
        if not project.repo or (only is not None and project.repo not in only):
            continue
        row: dict[str, Any] = {"repo": project.repo, "account": project.account}
        print(f"[ci-history] {project.repo}: start", file=sys.stderr, flush=True)
        if project.path and not Path(project.path).expanduser().exists():
            row["note"] = "path not on disk (reported, not skipped)"
        try:
            apply_project_identity(project.repo)
            ret = retention(project.repo, gh_json_lines)
            row["retention"] = ret
            if ret and ret.get("days", 0) >= FULL_RETENTION_DAYS:
                # Keyed on the retention actually SET, not the plan maximum: a
                # repo that allows 400 but is set to 90 still loses history.
                row["action"] = "skip: retention already set to 400 days"
            else:
                origin = archive.Origin(project.account or "unresolved",
                                        project.repo.split("/", 1)[0], project.repo)
                row["action"] = "dry-run" if args.dry_run else "exported"
                row["counts"] = export_repo(project.repo, origin, gh_json_lines, dry_run=args.dry_run)
        except (RuntimeError, SystemExit) as exc:
            row["error"] = str(exc)[:200]
            failed = True
        report.append(row)
        print(f"[ci-history] {json.dumps(row)}", file=sys.stderr, flush=True)
    print(json.dumps(report, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
