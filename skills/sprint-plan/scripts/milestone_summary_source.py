"""Sources, release-anchor handling, and GitHub collection for sprint summary."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

# harmonic-forge#708: which repos carry release milestones, and each repo's
# prefix, are projects.toml facts (its `milestones` and `prefix` fields), not
# constants to repeat here. Read in manifest order so the output is stable.
import sys as _sys  # noqa: E402

_sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools" / "onboard"))
from manifest import load as _load_manifest  # noqa: E402

_sys.path.insert(0, str(Path(__file__).resolve().parent))
import _home  # noqa: E402

_PROJECTS = [p for p in _load_manifest() if p.onboarded and p.repo]


def _venture_board() -> tuple[str | None, str | None]:
    """The home repo's board: a board is per venture (projects.toml), so the
    milestone repos are the ones sharing it. No config -> no filter."""
    try:
        home = _home.home_repo()
    except _home.config_loader.ConfigError:
        return None, None
    project = next(p for p in _PROJECTS if p.repo == home)
    return project.board_owner, project.board_number


_BOARD = _venture_board()
MILESTONE_REPOS = tuple(p.repo for p in _PROJECTS if p.milestones and
                        (_BOARD == (None, None) or (p.board_owner, p.board_number) == _BOARD))
NO_MILESTONE_REPO = next(p.repo for p in _PROJECTS if p.name == "harmonic-forge")
ALL_REPOS = (*MILESTONE_REPOS, NO_MILESTONE_REPO)
NOT_RELEASES = ("later", "platform")
ANCHOR = re.compile(r"^## Current release\s*[—-]\s*([^,\n]+)", re.M)
REPO_PREFIX = {p.repo.split("/")[-1]: p.prefix for p in _PROJECTS if p.repo in ALL_REPOS}
FETCH_LIMIT = 500


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def repo_root() -> Path:
    result = run("git", "rev-parse", "--show-toplevel")
    if result.returncode:
        raise SystemExit("sprint-plan summary: not inside a git repository.")
    return Path(result.stdout.strip())


def cache_dir() -> Path:
    return repo_root() / ".claude" / "cache" / "sprint-plan"


def gh_json(*args: str):
    result = run("gh", *args)
    if result.returncode:
        raise SystemExit(
            f"sprint-plan summary: `gh {' '.join(args)}` failed: {result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def read_anchor(priorities: Path) -> str:
    match = ANCHOR.search(priorities.read_text())
    if not match:
        raise SystemExit(
            f"sprint-plan summary: no '## Current release — X, ...' heading in "
            f"{priorities}. The anchor is the only source for which milestone is "
            "current; refusing to guess.")
    return match.group(1).strip()


def rewrite_anchor(priorities: Path, milestone: str) -> None:
    text = priorities.read_text()
    match = ANCHOR.search(text)
    if not match:
        raise SystemExit(f"sprint-plan summary: no anchor heading in {priorities} to rewrite.")
    line_end = text.find("\n", match.start())
    if line_end == -1:
        line_end = len(text)
    body = text[:match.start()] + f"## Current release — {milestone}" + text[line_end:]
    handle = tempfile.NamedTemporaryFile("w", dir=priorities.parent, delete=False, suffix=".tmp")
    try:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, priorities)


def working_tree_dirty(path: Path) -> bool:
    return bool(run("git", "status", "--porcelain", "--", str(path)).stdout.strip())


def fetch_issues(repo: str, milestone: str | None) -> list[dict]:
    args = ["issue", "list", "--repo", repo, "--state", "open", "--limit",
            str(FETCH_LIMIT), "--json",
            "number,title,body,labels,updatedAt,milestone,comments"]
    if milestone:
        args += ["--milestone", milestone]
    issues = gh_json(*args)
    if len(issues) >= FETCH_LIMIT:
        raise SystemExit(
            f"sprint-plan summary: {repo} returned {len(issues)} open issues, at "
            f"the --limit {FETCH_LIMIT} ceiling. The list is truncated oldest-first "
            "and the missing issues would silently skew milestone resolution. Raise "
            "FETCH_LIMIT before trusting a run.")
    return issues


def milestone_counts(repos: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for repo in repos:
        for issue in fetch_issues(repo, None):
            title = (issue.get("milestone") or {}).get("title")
            if title:
                counts[title] = counts.get(title, 0) + 1
    return counts


def known_milestones(repos: tuple[str, ...]) -> list[str]:
    titles = []
    for repo in repos:
        for milestone in gh_json("api", f"repos/{repo}/milestones?state=all&per_page=100"):
            if milestone["title"] not in titles:
                titles.append(milestone["title"])
    return titles


def resolve_milestone(anchor: str, counts: dict[str, int],
                      known: list[str]) -> tuple[str, str | None]:
    if anchor.lower() in NOT_RELEASES:
        raise SystemExit(f"sprint-plan summary: the anchor names {anchor!r}, which is not a release. Fix the anchor.")
    if counts.get(anchor, 0) > 0:
        return anchor, None
    if anchor not in known:
        raise SystemExit(f"sprint-plan summary: the anchor names milestone {anchor!r}, which does not exist. Known milestones: {', '.join(known) or '(none)'}. The anchor is stale; refusing to guess.")
    candidates = sorted((name for name, count in counts.items() if count > 0
                         and name.lower() not in NOT_RELEASES and re.match(r"^\d", name)),
                        key=lambda name: [int(part) for part in re.findall(r"\d+", name)])
    if not candidates:
        raise SystemExit(f"sprint-plan summary: milestone {anchor!r} has 0 open issues and no numbered milestone has any either. The anchor is stale and nothing resolves it; refusing to emit an empty summary.")
    return candidates[0], (f"anchor named {anchor} (0 open); reconciled to {candidates[0]} "
                           f"({counts[candidates[0]]} open) — lowest-numbered release with open work")


def board_fields(owner: str, number: str) -> dict[tuple[str, int], dict[str, str]]:
    query = """query($owner:String!,$number:Int!,$cursor:String){user(login:$owner){projectV2(number:$number){items(first:100,after:$cursor){pageInfo{hasNextPage endCursor}nodes{content{... on Issue{number repository{nameWithOwner}}}fieldValues(first:20){nodes{... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2SingleSelectField{name}}}... on ProjectV2ItemFieldNumberValue{number field{... on ProjectV2FieldCommon{name}}}... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2FieldCommon{name}}}}}}}}}}"""
    fields, cursor = {}, None
    while True:
        args = ["api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"number={number}"]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        page = gh_json(*args)
        items = (((page.get("data") or {}).get("user") or {}).get("projectV2") or {}).get("items") or {}
        for node in items.get("nodes") or []:
            content = node.get("content") or {}
            if not content.get("number"):
                continue
            values = {}
            for value in (node.get("fieldValues") or {}).get("nodes") or []:
                field = (value.get("field") or {}).get("name")
                raw = value.get("name") or value.get("text") or value.get("number")
                if field and raw is not None:
                    values[field] = str(raw)
            fields[(content["repository"]["nameWithOwner"], content["number"])] = values
        info = items.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return fields
        cursor = info["endCursor"]
