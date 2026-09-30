#!/usr/bin/env python3
"""Registry census of issues (harmonic-forge#828, F637 T0).

For every `projects.toml` repo, through its own identity
(`apply_project_identity`), one paginated `issues?state=all` list -- no
comment fetches -- recording state, comment count and closed_at per issue.
The output tells T1 which issues are worth a comment fetch at all.

A repo's lane-marker presence is answered by T1 (which reads comments); the
census answers the prerequisite: how many closed issues have >=1 comment vs
0, per repo. Rows carry account, org and repo (OPERATOR-RULINGS).

Usage:
    python3 tools/telemetry/census.py --out census.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parent / "onboard")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

GhPages = Callable[[str], Iterable[Any]]


def gh_pages(path: str) -> list[Any]:
    result = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path}: {result.stderr.strip()[:200]}")
    decoder, text, idx, pages = json.JSONDecoder(), result.stdout, 0, []
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        page, idx = decoder.raw_decode(text, idx)
        pages.append(page)
    return pages


def census_repo(repo: str, gh: GhPages) -> dict[str, int]:
    counts = {"issues": 0, "open": 0, "closed": 0, "closed_with_comments": 0, "closed_zero_comments": 0}
    for page in gh(f"repos/{repo}/issues?state=all&per_page=100"):
        for item in page:
            if "pull_request" in item:  # the issues endpoint also returns PRs
                continue
            counts["issues"] += 1
            if item.get("state") == "closed":
                counts["closed"] += 1
                key = "closed_with_comments" if item.get("comments", 0) > 0 else "closed_zero_comments"
                counts[key] += 1
            else:
                counts["open"] += 1
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)

    import manifest  # noqa: PLC0415
    from manifest_identity import apply_project_identity  # noqa: PLC0415

    fields = ["account", "org", "repo", "note", "issues", "open", "closed",
              "closed_with_comments", "closed_zero_comments", "error"]
    rows, failed = [], False
    for project in manifest.load(args.manifest):
        if not project.repo:
            continue
        row: dict[str, Any] = {"account": project.account, "org": project.repo.split("/", 1)[0],
                               "repo": project.repo, "note": ""}
        if project.path and not Path(project.path).expanduser().exists():
            row["note"] = "path not on disk (reported, not skipped)"
        try:
            apply_project_identity(project.repo)
            row.update(census_repo(project.repo, gh_pages))
        except (RuntimeError, SystemExit) as exc:
            row["error"] = str(exc)[:200]
            failed = True
        rows.append(row)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(args.out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
