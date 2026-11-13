#!/usr/bin/env python3
"""The UI grant: the operator's typed approval of a change to a project's
rendered UI, its versioned brand asset (harmonic-forge#1274).

A project opts in by checking in `frontend/ui-version.toml` (`UI_VERSION_PATH`)
on `main`: a `version`, one `digest` over the corpus its `corpus` globs name,
the `brand` globs, and the `font` package. The project's own tool (HRSE2
`scripts/ui_version.py`) computes the digest, and its default-branch
`ui-approval` workflow writes the required `ui-approval` commit status on every
PR head: `success` when the PR leaves the corpus alone or carries the
operator's recorded approval of the head's digest, `failure` otherwise. That
status is the gate (reforge, comment 6699562919); nothing here is spent.

This module owns what the operator types and its public record:

- `UI <KEY>: <reason>`, the prompt's first line in a LANE=1 session, writes a
  pending **plan** entry. `record` posts `## UI plan grant — <KEY>` on issue
  KEY. It lets a handoff declare a UI change (`l1_post.py`, AC6/AC7).
- `UI <KEY> <digest>: <reason>` writes a pending **merge** entry bound to that
  12-64 hex prefix, typed after the operator has seen `ui_version.py diff`.
  `record` posts `## UI grant — <KEY>` with the full digest on the single open
  PR, in an opted-in repo under the key's owner, that names KEY (title
  `(<slug>#N)` suffix, else its branch number in the key's own repo) and whose
  head digest starts with the prefix. Zero or several such PRs refuse, writing
  nothing and posting nothing. The comment fires the PR's `issue_comment`
  event, and the workflow recomputes the digest and turns the status green.

The head digest is read from that PR head's `ui-approval` status, written by
the workflow (creator `github-actions[bot]`), which computes it with
`scope`'s rule at the head; the workflow re-checks it against its own
computation, so a wrong one here only produces a record that never matches.

`expand_lane_shorthand.py` writes the pending entry with R-0117 provenance,
as it does FORCE (`force_grant.py`, whose shape this copies). Local state is
only that pending entry, one per mode, so each mode is independent; `record`
posts it, refetches it, and moves it to `recorded`. An expired pending entry
is dropped with a note and never blocks the other mode.

Like FORCE, this trusts the operator-typed line and the session. The grant
file and the public comment are an audit trail, not proof against a session
that writes this directory itself.

Run:
    python3 tools/gh/ui_grant.py record --key H2615
    python3 tools/gh/ui_grant.py status --key H2615
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import imc_waiver  # noqa: E402  (key, time and atomic-write helpers, one copy)

#: `None` means `~/.claude/state/ui-grant`, resolved at call time, so a test
#: that patches this global or `HOME` never reaches the live store.
UI_STATE_DIR: Path | None = None
STATE_RELPATH = Path(".claude") / "state" / "ui-grant"
TTL_DAYS = 7
HOOK_VERSION = "2"
MODES = ("plan", "merge")
#: Where a project declares its corpus; absent on `main` means not opted in.
UI_VERSION_PATH = "frontend/ui-version.toml"
DIGEST_PREFIX_RE = re.compile(r"^[0-9a-f]{12,64}$")
#: The status context the project's workflow writes, and who may write it.
STATUS_CONTEXT = "ui-approval"
STATUS_CREATOR = "github-actions[bot]"
_STATUS_DIGEST = re.compile(r"\bdigest ([0-9a-f]{64})\b")
#: The account `record` posts as, and the only author a record is read from.
APPROVER = "vitalharmony"
#: The handoff lead line AC7 requires, in `l1_post.LEAD_LABEL`'s form.
UI_CHANGE_RE = re.compile(
    r"(?im)^[ \t]*\**[ \t]*UI change[ \t]*\**[ \t]*:[ \t]*\**[ \t]*(?P<text>\S.*)$")
#: A PR's issue: the `(<slug>#N)` title suffix (block_route_tightened_merge.TITLE_ISSUE),
#: else the branch number (block_missing_preclose_inspection.BRANCH_ISSUE).
_TITLE_ISSUE = re.compile(r"\((?P<slug>[A-Za-z0-9_.-]+)#(?P<number>\d+)\)\s*$")
_BRANCH_ISSUE = re.compile(r"^[a-z]+/(\d+)[-/]")


class RecordRefused(Exception):
    """`record` will not post this entry; the message says why."""


def state_dir() -> Path:
    return UI_STATE_DIR if UI_STATE_DIR is not None else Path.home() / STATE_RELPATH


def grant_path(key: str) -> Path:
    return state_dir() / f"{key.upper()}.json"


@contextlib.contextmanager
def grant_lock(key: str):
    """Serialize every read-modify-write of one key's grant file. Never held
    across the GitHub post."""
    path = state_dir() / f"{key.upper()}.json.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ui_change_line(bodies) -> str | None:
    """The newest `**UI change:**` line among comment bodies (oldest first)."""
    for body in reversed(list(bodies or [])):
        match = UI_CHANGE_RE.search(body or "")
        if match:
            return match.group("text").strip().strip("*").strip()
    return None


def _gh_lines(args: list[str], runner, caller: str | None = None) -> list[dict]:
    """A paginated `gh api ... --jq '... | tojson'` read as a list of dicts;
    raises RecordRefused on any failure. `caller` marks this one call as a
    sanctioned gh-shim scan (harmonic-forge#964, #1356); every other call runs
    with the caller's environment unchanged."""
    extra = {"env": {**os.environ, "GH_SHIM_CALLER": caller}} if caller else {}
    result = runner(["gh", "api", "--paginate", *args], capture_output=True, text=True, check=False,
                    **extra)
    if result.returncode:
        raise RecordRefused(f"`gh api {args[0]}` failed: {(result.stderr or '').strip()[:200]}")
    try:
        return [json.loads(line) for line in (result.stdout or "").splitlines() if line.strip()]
    except ValueError as exc:
        raise RecordRefused(f"`gh api {args[0]}` returned unreadable output ({exc})") from exc


def issue_comments(repo: str, issue: int, runner=subprocess.run) -> list[dict]:
    """Every comment on an issue or PR, all pages: `{login, body, html_url}`."""
    return _gh_lines([f"repos/{repo}/issues/{issue}/comments?per_page=100", "--jq",
                      ".[] | {login: .user.login, body: .body, html_url: .html_url} | tojson"], runner)


def fetch_ui_change(repo: str, issue: int, runner=subprocess.run) -> str | None:
    """The issue's newest `**UI change:**` line, or None (best effort)."""
    try:
        return ui_change_line(c.get("body") for c in issue_comments(repo, issue, runner))
    except Exception:  # noqa: BLE001
        return None


# --- the public record --------------------------------------------------------

def heading(key: str, mode: str = "merge") -> str:
    return f"## UI grant — {key.upper()}" if mode == "merge" else f"## UI plan grant — {key.upper()}"


def marker(key: str, mode: str, granted_at: str, digest: str = "") -> str:
    """The record's machine line. HRSE2's `scripts/ui_approval.py` reads a merge
    record's by this exact shape; change both together."""
    return f"<!-- ui-grant key={key.upper()} mode={mode} granted_at={granted_at} digest={digest} -->"


def find_record(comments, key: str, mode: str) -> dict | None:
    """The newest comment that is the posted `mode` record for `key`: author
    `APPROVER`, first line exactly the heading, the marker on its own line."""
    want_heading, key = heading(key, mode), key.upper()
    shape = re.compile(rf"(?m)^<!-- ui-grant key={re.escape(key)} mode={mode} granted_at=\S* "
                       r"digest=[0-9a-f]* -->[ \t]*$")
    for comment in reversed(list(comments or [])):
        body = (comment.get("body") or "").replace("\r\n", "\n")
        if comment.get("login") != APPROVER or body.split("\n", 1)[0] != want_heading:
            continue
        if shape.search(body):
            return comment
    return None


_AUTHORIZES = {
    "plan": "a handoff that declares a UI change (`l1_post.py`, harmonic-forge#1274 AC6/AC7); "
            "it approves no merge",
    "merge": "this PR's merge while its head's UI corpus digest is exactly this one: the required "
             "`ui-approval` status passes on it. A new head digest needs a new line",
}


def comment_body(grant: dict, mode: str, ui_change: str | None, *, digest: str = "",
                 pr: str | None = None, head_sha: str | None = None) -> str:
    entry = grant[mode]
    bound = (f" at digest `{digest}` (typed `{entry['digest']}`), PR head `{head_sha}`"
             if mode == "merge" else "")
    lines = [
        heading(grant["key"], mode),
        "",
        f"**UI change:** {ui_change or '(no `**UI change:**` line on the issue yet)'}",
        "",
        f"- **Who:** the operator, through a typed prompt that passed the BATCH provenance "
        f"check (R-0117), in session `{entry.get('session_id') or 'unknown'}`.",
        f"- **When:** `{entry.get('granted_at')}`.",
        f"- **Mode:** {mode}{bound}.",
        f"- **What it approves:** {_AUTHORIZES[mode]}, for {grant['repo']}#{grant['issue']}"
        f"{f' ({pr})' if pr else ''}. BATCH, R-0351, R-0415 and `close` approve none of this.",
        f"- **Reason:** {entry['reason']}",
        "",
        marker(grant["key"], mode, str(entry.get("granted_at")), digest if mode == "merge" else ""),
    ]
    return "\n".join(lines)


# --- the grant store ----------------------------------------------------------

def read_grant(key: str) -> dict | None:
    return imc_waiver.read_json(grant_path(key))


def _expired(entry: dict, now: datetime) -> bool:
    expiry = imc_waiver._parse_time(entry.get("expires_at"))
    return expiry is None or now >= expiry


def write_pending(key: str, repo: str, issue: int, mode: str, digest: str | None, reason: str,
                  session_id: str | None, line: str,
                  now: datetime | None = None) -> tuple[dict, dict | None]:
    """The hook's write: a pending entry of `mode`, replacing only that mode's
    earlier pending entry. Returns `(entry, replaced entry or None)`."""
    if mode not in MODES:
        raise ValueError(f"unknown UI grant mode {mode!r}")
    moment = now or imc_waiver._now()
    entry = {
        "mode": mode, "digest": digest if mode == "merge" else None, "reason": reason,
        "granted_at": imc_waiver._iso(moment),
        "expires_at": imc_waiver._iso(moment + timedelta(days=TTL_DAYS)),
        "session_id": session_id or "", "line": line, "status": "pending",
        "hook_version": HOOK_VERSION,
    }
    with grant_lock(key):
        current = read_grant(key) or {}
        if current.get("repo", repo).lower() != repo.lower() or int(current.get("issue") or issue) != issue:
            current = {}
        replaced = current.get(mode)
        grant = {"key": key.upper(), "repo": repo, "issue": issue, "plan": current.get("plan"),
                 "merge": current.get("merge"), "recorded": current.get("recorded") or [],
                 mode: entry}
        imc_waiver.write_json_atomic(grant_path(key), grant)
    return entry, replaced


# --- which PR a merge record goes on (reforge step 5) ---------------------------

def opted_in_repos(owner: str, runner=subprocess.run) -> list[str]:
    """Every registered repo under `owner` whose `main` carries the version file.
    A 404 is "not opted in"; any other read failure refuses."""
    from batch_auth import REPO_PREFIXES  # noqa: PLC0415
    out = []
    for repo in sorted(r for r in REPO_PREFIXES if r.split("/")[0].lower() == owner.lower()):
        result = runner(["gh", "api", f"repos/{repo}/contents/{UI_VERSION_PATH}?ref=main",
                         "--jq", ".sha"], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            out.append(repo)
            continue
        detail = f"{result.stderr} {result.stdout}"
        if "HTTP 404" not in detail and "Not Found" not in detail:
            raise RecordRefused(f"cannot read {UI_VERSION_PATH} on {repo}'s main: {detail.strip()[:160]}")
    return out


def pr_issue(pr_repo: str, title: str, head_ref: str) -> tuple[str, int] | None:
    """The issue a PR is for: its title's `(<slug>#N)` suffix, else its branch
    number in its own repo. A title and branch that disagree raise."""
    title_match = _TITLE_ISSUE.search(title or "")
    branch_match = _BRANCH_ISSUE.match(head_ref or "")
    if title_match:
        number = int(title_match.group("number"))
        if branch_match and int(branch_match.group(1)) != number:
            raise RecordRefused(f"{pr_repo} PR titled {title!r} names #{number} but its branch "
                                f"{head_ref!r} names #{branch_match.group(1)}; fix the title or branch")
        return f"{pr_repo.split('/')[0]}/{title_match.group('slug')}".lower(), number
    return (pr_repo.lower(), int(branch_match.group(1))) if branch_match else None


def head_digest(repo: str, sha: str, runner=subprocess.run) -> str | None:
    """The digest the newest Actions-written `ui-approval` status on `sha`
    names, or None (no status yet, or out of scope)."""
    statuses = _gh_lines([f"repos/{repo}/commits/{sha}/statuses?per_page=100", "--jq",
                          f'.[] | select(.context == "{STATUS_CONTEXT}") | '
                          "{login: .creator.login, description: .description} | tojson"], runner)
    for status in statuses:  # newest first
        if status.get("login") == STATUS_CREATOR:
            match = _STATUS_DIGEST.search(status.get("description") or "")
            return match.group(1) if match else None
    return None


def find_merge_pr(key: str, key_repo: str, number: int, prefix: str,
                  runner=subprocess.run) -> tuple[str, int, str, str]:
    """`(repo, PR number, full digest, head sha)` of the one open PR that names
    the key and whose head digest starts with `prefix`; RecordRefused otherwise."""
    candidates, seen = [], []
    want = (key_repo.lower(), number)
    for repo in opted_in_repos(key_repo.split("/")[0], runner):
        prs = _gh_lines([f"repos/{repo}/pulls?state=open&per_page=100", "--jq",
                         ".[] | {number, title, ref: .head.ref, sha: .head.sha} | tojson"], runner,
                        caller="ui_grant")
        for pr in prs:
            title, ref = pr.get("title") or "", pr.get("ref") or ""
            title_match, branch_match = _TITLE_ISSUE.search(title), _BRANCH_ISSUE.match(ref)
            named = {
                (f"{repo.split('/')[0]}/{title_match.group('slug')}".lower(),
                 int(title_match.group("number"))) if title_match else None,
                (repo.lower(), int(branch_match.group(1))) if branch_match else None,
            }
            if want not in named:
                continue
            # Survivor 12's guard: a PR naming the key whose title and branch
            # disagree cannot be told apart, so it refuses.
            if pr_issue(repo, title, ref) != want:
                continue
            digest = head_digest(repo, pr["sha"], runner)
            seen.append(f"{repo}#{pr['number']} at {pr['sha'][:12]} (digest "
                        f"{digest[:16] if digest else 'not reported'})")
            if digest and digest.startswith(prefix):
                candidates.append((repo, int(pr["number"]), digest, pr["sha"]))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RecordRefused(f"no open PR names {key} with a head digest starting {prefix[:16]}; "
                            f"PRs naming {key}: {', '.join(seen) or 'none'}. The digest is the one "
                            "`ui_version.py diff` printed for the PR head")
    raise RecordRefused(f"{len(candidates)} open PRs name {key} at digest {prefix[:16]}: "
                        + ", ".join(f"{r}#{n}" for r, n, _, _ in candidates))


# --- record ---------------------------------------------------------------------

def _finish(key: str, mode: str, entry: dict, moment: datetime, url: str, extra: dict) -> None:
    with grant_lock(key):
        current = read_grant(key) or {}
        if (current.get(mode) or {}).get("granted_at") != entry.get("granted_at"):
            raise SystemExit(f"ui-grant: the {mode} UI grant for {key} was replaced during the "
                             "post; record the newer grant instead.")
        current[mode] = None
        current["recorded"] = [*(current.get("recorded") or []),
                               {**entry, "status": "recorded", "comment_url": url,
                                "recorded_at": imc_waiver._iso(moment), **extra}]
        imc_waiver.write_json_atomic(grant_path(key), current)


def record(key: str, *, now: datetime | None = None, runner=subprocess.run) -> list[str]:
    """Post each pending entry for the key and refetch it; returns one line per
    entry (recorded, dropped as expired, or refused). Raises SystemExit only
    for a session that may not record at all."""
    key = key.strip().upper()
    lane = os.environ.get("LANE", "")
    if lane != "1":
        raise SystemExit(f"ui-grant: LANE is {lane!r}; a UI grant is recorded only in a "
                         "LANE=1 session.")
    grant = read_grant(key)
    if grant is None or not any(grant.get(m) for m in MODES):
        raise SystemExit(f"ui-grant: no pending UI grant for {key}. Only an operator-typed "
                         f"`UI {key}: <reason>` or `UI {key} <digest>: <reason>` line creates one.")
    moment = now or imc_waiver._now()
    repo, issue = grant["repo"], int(grant["issue"])
    lines = []
    ui_change = None
    for mode in MODES:
        entry = grant.get(mode)
        if not entry:
            continue
        if _expired(entry, moment):
            with grant_lock(key):
                current = read_grant(key) or {}
                if (current.get(mode) or {}).get("granted_at") == entry.get("granted_at"):
                    current[mode] = None
                    imc_waiver.write_json_atomic(grant_path(key), current)
            lines.append(f"ui-grant: {key} {mode}: DROPPED, expired at {entry.get('expires_at')}; "
                         "the operator types a new UI line")
            continue
        ui_change = ui_change if ui_change is not None else (fetch_ui_change(repo, issue, runner) or "")
        if mode == "plan":
            url = imc_waiver.post_and_confirm(repo, issue, comment_body(grant, mode, ui_change), runner)
            _finish(key, mode, entry, moment, url, {})
            lines.append(f"ui-grant: {key} plan: recorded on {repo}#{issue} -- {url}")
            continue
        try:
            pr_repo, pr, digest, sha = find_merge_pr(key, repo, issue, str(entry["digest"]), runner)
        except RecordRefused as exc:
            lines.append(f"ui-grant: {key} merge: REFUSED, nothing posted: {exc}")
            continue
        body = comment_body(grant, mode, ui_change, digest=digest, pr=f"{pr_repo}#{pr}", head_sha=sha)
        url = imc_waiver.post_and_confirm(pr_repo, pr, body, runner)
        _finish(key, mode, entry, moment, url, {"pr": f"{pr_repo}#{pr}", "full_digest": digest,
                                                "head_sha": sha})
        lines.append(f"ui-grant: {key} merge: recorded on {pr_repo}#{pr} for digest {digest} -- {url}")
    return lines


def _status_line(key: str) -> str:
    grant = read_grant(key)
    if grant is None:
        return f"ui-grant: {key}: none"
    parts = []
    for mode in MODES:
        entry = grant.get(mode)
        if entry:
            bound = f" at {str(entry.get('digest'))[:12]}" if mode == "merge" else ""
            parts.append(f"{mode} pending{bound}, expires {entry.get('expires_at')}")
    for done in grant.get("recorded") or []:
        parts.append(f"{done.get('mode')} recorded {done.get('comment_url')}")
    return f"ui-grant: {key} ({grant.get('repo')}#{grant.get('issue')}): " + ("; ".join(parts) or "none")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="UI grants: the operator's approval of a rendered-UI change (harmonic-forge#1274).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "status"):
        sub.add_parser(name).add_argument("--key", required=True)
    args = parser.parse_args(argv)
    key = args.key.strip().upper()
    if args.command == "status":
        print(_status_line(key))
        return 0
    lines = record(key, runner=subprocess.run)
    for line in lines:
        print(line)
    return 1 if any("REFUSED" in line or "DROPPED" in line for line in lines) else 0


if __name__ == "__main__":
    raise SystemExit(main())
