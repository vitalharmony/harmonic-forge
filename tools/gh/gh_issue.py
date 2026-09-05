#!/usr/bin/env python3
"""Create a GitHub issue and, if a project board is configured, add it there.

Generalized from HRSE2's original `scripts/gh_issue.py` (harmonic-forge#53),
which hardcoded `GH_REPO`/`PROJECT_OWNER`/`PROJECT_NUMBER` for
vitalharmony/hrse. `--repo` is now required with no default (same fix as
`post_comment.py`, same incident class as #50). Board owner/number are
optional — supplied via `--project-owner`/`--project-number` or the
the repo->board map (harmonic-forge#107); the env vars a project's own `mise.toml`
sets — and board-add is skipped entirely (not treated as an error) when
neither is configured, since not every project has a project board.

GraphQL-quota efficiency (harmonic-forge#203): issue creation goes through
`gh api -X POST` (pure REST) rather than `gh issue create`, which was found
live to route through GraphQL for a REST-capable operation. Board field
writes (`Status`, `Tier`) share one `project view` + `field-list` fetch
per `add_to_board()` call instead of each independently re-fetching —
GitHub's GraphQL limiting is cost-based (query complexity/node count, not
per-request), so halving the fetch count is a real, not cosmetic, saving.
"""

import json
import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retired_artifacts import RETIRED_ARTIFACTS  # noqa: E402

_BACKTICK_SPAN = re.compile(r"`([^`\n]+)`")


def find_retired_citations(body: str) -> list[tuple[str, str]]:
    """(name, replacement-note) for each backtick-quoted retired artifact.

    harmonic-forge#379. Whole-string match only, scoped to backtick-quoted
    spans -- never a bare word. That scope is deliberate, not an
    afterthought: the backlog-premise audit this check is drawn from found
    bare-word matching responsible for its worst false positives (flagging
    "Estimate" in every issue that correctly states one per
    `.claude/rules/planning.md`, flagging "Devin" in the issue whose whole
    point was scrubbing Devin references). A normal issue's free-text
    "Estimate: N points" line is never backtick-quoted, so it is excluded
    by construction, not by a special case.
    """
    seen: list[tuple[str, str]] = []
    for match in _BACKTICK_SPAN.findall(body):
        note = RETIRED_ARTIFACTS.get(match)
        if note is not None and (match, note) not in seen:
            seen.append((match, note))
    return seen
from item_list_cache import BOARD_ITEM_SCAN_LIMIT  # noqa: E402

STATUS_OPTION_NAME = "Todo"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def fetch_milestones(repo: str) -> dict[str, int]:
    """{title: number} for every open milestone on `repo`, or {} if it uses none.

    harmonic-forge#283 NC1: a failed query fails LOUD (SystemExit), never
    silently as an empty dict. An auth/network error must not be
    indistinguishable from "this repo genuinely has no milestones" — that
    would quietly make --milestone optional exactly when the check matters.
    """
    result = _run(
        ["gh", "api", f"repos/{repo}/milestones", "-X", "GET", "-f", "state=open",
         "--jq", ".[] | [.title, .number] | @tsv"],
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"[GH] cannot read milestones for {repo}: {result.stderr.strip()}\n"
            "[GH] refusing to file — a milestone query failure must not be "
            "mistaken for a repo that has none (harmonic-forge#283)."
        )
    milestones: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        title, number = line.rsplit("\t", 1)
        milestones[title.strip()] = int(number)
    return milestones


def create_issue(repo: str, title: str, body: str, labels: list[str],
                 milestone_number: int | None = None) -> str | None:
    create_cmd = [
        "gh", "api", f"repos/{repo}/issues", "-X", "POST",
        "-f", f"title={title}",
        "-f", f"body={body or '*Created via mise gh-new-issue*'}",
    ]
    for label in labels:
        create_cmd += ["-f", f"labels[]={label}"]
    # harmonic-forge#283 NC3: REST's issue-create takes the milestone NUMBER,
    # not its title ("null or string or integer — the number of the
    # milestone"), so the caller resolves title -> number before this point.
    if milestone_number is not None:
        create_cmd += ["-f", f"milestone={milestone_number}"]
    create_cmd += ["--jq", ".html_url"]

    result = _run(create_cmd, check=False)
    if result.returncode != 0:
        print(f"[GH] Failed to create issue:\n{result.stderr}", file=sys.stderr)
        return None

    issue_url = result.stdout.strip()
    print(f"[GH] Created issue: {issue_url}")
    return issue_url


# harmonic-forge#107: the board a repo's issues belong on is a property of the
# REPO, not of whichever shell happened to invoke this. Resolving it from
# $GH_PROJECT_OWNER/$GH_PROJECT_NUMBER meant a session working out of HRSE2
# filed harmonic-forge issues straight onto board #1 -- silently, because the
# add succeeded, just onto the wrong board.
#
# Exhaustive by design, and unmapped is a hard failure rather than a fallback.
# A default would reintroduce exactly the silent-misroute this fixes: a new
# repo would quietly inherit some other project's board. Adding a repo here is
# a one-line change and a deliberate act.
#
# cymagraph-infra has no board of its own -- its items live on board #1
# alongside hrse's (hrse#979). openclaw-projects likewise.
REPO_BOARDS: dict[str, tuple[str, str]] = {
    "vitalharmony/hrse": ("vitalharmony", "1"),
    "vitalharmony/harmonic-forge": ("vitalharmony", "3"),
    "vitalharmony/cymagraph-infra": ("vitalharmony", "1"),
    "vitalharmony/openclaw-projects": ("vitalharmony", "1"),
}


def resolve_board_for_repo(repo: str) -> tuple[str, str]:
    """Board (owner, number) for a repo. Fails loudly if unmapped.

    Deliberately raises rather than returning None: every caller that reaches
    here has already decided a board is wanted, so a soft failure would just
    reproduce the silent no-board case this issue exists to remove.
    """
    try:
        return REPO_BOARDS[repo]
    except KeyError:
        raise SystemExit(
            f"[GH] No project board mapped for {repo!r}.\n"
            f"[GH] Known repos: {', '.join(sorted(REPO_BOARDS))}.\n"
            f"[GH] Add it to REPO_BOARDS in tools/gh/gh_issue.py, or pass "
            f"--project-owner/--project-number explicitly to override."
        ) from None


def _fetch_project_context(project_owner: str, project_number: str) -> dict | None:
    """One `project view` + one `field-list` call, shared by every field write
    in a single `add_to_board()` invocation instead of each field fetching
    independently (harmonic-forge#203)."""
    view_result = _run(
        ["gh", "project", "view", project_number, "--owner", project_owner, "--format", "json"],
        check=False,
    )
    if view_result.returncode != 0:
        print(f"[GH] project view failed:\n{view_result.stderr}", file=sys.stderr)
        return None
    try:
        project_id = json.loads(view_result.stdout)["id"]
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"[GH] Could not parse project id: {exc}", file=sys.stderr)
        return None

    fields_result = _run(
        ["gh", "project", "field-list", project_number, "--owner", project_owner, "--format", "json"],
        check=False,
    )
    if fields_result.returncode != 0:
        print(f"[GH] field-list failed:\n{fields_result.stderr}", file=sys.stderr)
        return None
    try:
        fields = json.loads(fields_result.stdout)["fields"]
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"[GH] Could not parse field list: {exc}", file=sys.stderr)
        return None

    return {"project_id": project_id, "fields": fields}


def _set_single_select(item_id: str, project_id: str, fields: list[dict],
                        field_name: str, value: str) -> bool:
    """Set a project item's single-select field by name, matching the
    option case-insensitively (harmonic-forge#308). Shared by Status/Tier/
    Theme/Venture (harmonic-forge#329 folded the once-separate
    `_set_status_todo` in here -- same shape as `_set_tier`'s, just with a
    literal field name and value instead of both).

    Option lists are read live from `fields` (see `_fetch_project_context`),
    never hardcoded: harmonic-forge#300 established that editing a
    single-select's options wipes every existing assignment, so a
    hardcoded enum here would drift from the real board and a "fix" to
    realign it would be destructive.
    """
    value_norm = value.strip().lower()
    field = next((f for f in fields if f.get("name") == field_name), None)
    if field is None:
        print(f"[GH] No {field_name!r} field on the project", file=sys.stderr)
        return False
    option = next(
        (o for o in (field.get("options") or [])
         if str(o.get("name", "")).strip().lower() == value_norm),
        None,
    )
    if option is None:
        available = ", ".join(sorted(o.get("name", "") for o in (field.get("options") or [])))
        print(
            f"[GH] {field_name} field has no option {value!r}. "
            f"Available: {available or '(none)'}",
            file=sys.stderr,
        )
        return False
    cmd = [
        "gh", "project", "item-edit",
        "--project-id", project_id,
        "--id", item_id,
        "--field-id", field["id"],
        "--single-select-option-id", option["id"],
    ]
    edit_result = _run(cmd, check=False)
    if edit_result.returncode != 0:
        # harmonic-forge#263 AC3: name the exact repair command. Every id
        # is already resolved at this point, so the failing invocation is
        # itself the fix -- the common cause is a transient GraphQL quota
        # blip, where re-running it succeeds immediately.
        print(
            f"[GH] Setting {field_name} failed:\n{edit_result.stderr}\n"
            f"[GH] Repair by re-running:\n  {shlex.join(cmd)}",
            file=sys.stderr,
        )
        return False
    print(f"[GH] Set {field_name} = {value.strip()}")
    return True


def _set_tier(item_id: str, project_id: str, fields: list[dict], tier: str) -> bool:
    """Set the project item's Tier field (harmonic-forge#257).

    The legacy numeric `Estimate` fallback is gone. It existed so the rename
    could be non-atomic across two repos and two boards; both boards are
    migrated and the field itself was deleted in hrse#966, so the fallback
    could only ever have written to a field that no longer exists.
    """
    return _set_single_select(item_id, project_id, fields, "Tier", tier)


def set_tier(item_id: str, project_owner: str, project_number: str, tier: str) -> bool:
    """Standalone convenience wrapper for ad-hoc/backfill use (fetches its
    own project context) — `add_to_board()` uses the shared-context path
    above instead, since it may also be setting Status in the same call."""
    ctx = _fetch_project_context(project_owner, project_number)
    if ctx is None:
        return False
    return _set_tier(item_id, ctx["project_id"], ctx["fields"], tier)


#: harmonic-forge#468: the targeted lookup that replaced a 5000-item scan.
#:
#: Roughly one GraphQL complexity point — it can only ever return this one
#: issue's project items — against hundreds for the board scan this used to do.
_ITEM_ID_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      projectItems(first: 10) {
        nodes { id project { number } }
      }
    }
  }
}
"""


def _find_existing_item(issue_url: str, project_owner: str, project_number: str) -> str | None:
    """Project item id for an issue already on the board, or None.

    Only called after `item-add` failed, so this is the rare path — but it used
    to pull **every item on the board** (`--limit 5000`) to find one issue by
    URL, which is precisely the "what is X for issue N" shape harmonic-forge#468
    exists to make cheap. The board scan is gone; this asks the issue directly.

    Derives owner/repo/number from the issue URL rather than taking them as
    parameters, so no caller has to change.
    """
    match = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", issue_url)
    if not match:
        return None
    repo_owner, repo_name, issue_number = match.groups()

    result = _run(
        ["gh", "api", "graphql",
         "-f", f"query={_ITEM_ID_QUERY}",
         "-F", f"owner={repo_owner}", "-F", f"repo={repo_name}",
         "-F", f"number={int(issue_number)}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
        issue = (payload.get("data") or {}).get("repository", {}).get("issue")
        nodes = ((issue or {}).get("projectItems") or {}).get("nodes") or []
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None
    for node in nodes:
        if str((node.get("project") or {}).get("number")) == str(project_number):
            return node.get("id")
    return None


def add_to_board(issue_url: str, project_owner: str, project_number: str, tier: str | None,
                  theme: str | None = None, venture: str | None = None) -> bool:
    # --format json returns the item id directly, no need to scan item-list
    # (which paginates at 30 by default and a freshly created item sorts
    # last on a large board).
    add_cmd = [
        "gh", "project", "item-add", project_number,
        "--owner", project_owner,
        "--url", issue_url,
        "--format", "json",
    ]
    add_result = _run(add_cmd, check=False)
    if add_result.returncode != 0:
        # hrse#883: a repo-level auto-add can win the race and put the issue
        # on the board before this call runs, and GitHub answers the second
        # add with "Content already exists". Bailing here left the item on
        # the board with no Tier, which is exactly the silent-unset outcome
        # harmonic-forge#263 exists to prevent — the field a model-routing
        # gate reads, absent, with the run reporting only a warning. The
        # item existing is the state this call wanted; recover its id and
        # carry on to the field writes.
        item_id = _find_existing_item(issue_url, project_owner, project_number)
        if item_id is None:
            print(f"[GH] Warning: created issue but failed to add to board:\n{add_result.stderr}", file=sys.stderr)
            return False
        print(f"[GH] Already on board #{project_number} (added by another writer); setting fields on the existing item")
    else:
        try:
            item_id = json.loads(add_result.stdout)["id"]
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"[GH] Warning: added to board but could not parse item id: {exc}", file=sys.stderr)
            return False

        print(f"[GH] Added issue to board #{project_number} (owner: {project_owner})")

    ctx = _fetch_project_context(project_owner, project_number)
    if ctx is None:
        print("[GH] Warning: added to board but could not fetch project context — needs manual triage", file=sys.stderr)
        return False

    if not _set_single_select(item_id, ctx["project_id"], ctx["fields"], "Status", STATUS_OPTION_NAME):
        print("[GH] Warning: could not set Status=Todo — item was added but needs manual triage", file=sys.stderr)
        return False

    if tier is not None:
        if not _set_tier(item_id, ctx["project_id"], ctx["fields"], tier):
            # Not a warning: this exits non-zero. Calling it one invited the
            # reader to scroll past, which is how harmonic-forge#263 was
            # observed live -- two issues filed --tier fast, boarded with Tier
            # unset after a quota blip, the message lost in the scrollback.
            print(
                f"[GH] ERROR: Tier={tier} was requested but not written. The "
                f"model-tier gate reads an unset Tier as 'does not escalate', "
                f"so this must not pass silently. Repair command above.",
                file=sys.stderr,
            )
            return False

    # harmonic-forge#308: same hard-fail shape as Tier above -- Theme/Venture
    # are the required reporting-slice fields hrse#966 established, and an
    # unset value is silent drift (exactly how the retired `Priority` field
    # rotted), not a soft failure to warn about and move on from.
    if theme is not None:
        if not _set_single_select(item_id, ctx["project_id"], ctx["fields"], "Theme", theme):
            print(
                f"[GH] ERROR: Theme={theme} was requested but not written. "
                f"Theme is a required reporting-slice field (hrse#966), so "
                f"this must not pass silently. Repair command above.",
                file=sys.stderr,
            )
            return False

    if venture is not None:
        if not _set_single_select(item_id, ctx["project_id"], ctx["fields"], "Venture", venture):
            print(
                f"[GH] ERROR: Venture={venture} was requested but not written. "
                f"Venture is a required reporting-slice field (hrse#966), so "
                f"this must not pass silently. Repair command above.",
                file=sys.stderr,
            )
            return False

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a GitHub issue and optionally add it to a project board")
    parser.add_argument("--repo", required=True, help="Target repo, e.g. vitalharmony/hrse (no default — must be explicit)")
    parser.add_argument("--title", required=True, help="Issue title")
    parser.add_argument("--body", default="", help="Issue body (prefer --body-file)")
    parser.add_argument(
        "--body-file", default=None, metavar="PATH",
        help="Read the issue body from a file. PREFERRED over --body "
             "(harmonic-forge#266): prose passed as a shell argument gets "
             "corrupted by backtick command substitution, printf %% specifiers, "
             "and dropped apostrophes in single-quoted strings. Use '-' for stdin.",
    )
    parser.add_argument("--labels", default="feature", help="Comma-separated labels")
    parser.add_argument(
        "--project-owner", default=None,
        help="Override the board owner derived from --repo (harmonic-forge#107). "
             "Rarely needed; the repo->board map is authoritative.",
    )
    parser.add_argument(
        "--project-number", default=None,
        help="Override the board number derived from --repo (harmonic-forge#107). "
             "Rarely needed; the repo->board map is authoritative.",
    )
    parser.add_argument(
        "--tier", choices=("fast", "standard", "deep"), default=None,
        help="Complexity tier for the board's Tier field (harmonic-forge#257) — "
             "the model-routing signal, not a forecast. no-op without a board.",
    )
    parser.add_argument(
        "--theme", default=None,
        help="Board Theme field (hrse#966) — what capability this issue is "
             "about. No fixed choice list here: options differ per board "
             "(harmonic-forge#300) and are read live; an unmatched value "
             "errors with the real option list from the board.",
    )
    parser.add_argument(
        "--venture", default=None,
        help="Board Venture field (hrse#966) — whose work this is. Same "
             "live-validated shape as --theme, not hardcoded here.",
    )
    parser.add_argument(
        "--milestone", default=None, metavar="TITLE",
        help="Milestone title, e.g. '2.7' — release membership (harmonic-forge#283). "
             "REQUIRED for a repo that uses milestones; ignored where none exist. "
             "Use 'Later' for real work not yet placed in a numbered release. "
             "Stricter than --tier, which is optional: an unset milestone reads "
             "identically to a deliberate 'in no release'.",
    )
    args = parser.parse_args()

    # harmonic-forge#266: --body-file is the safe path for prose. --body still
    # works for short bodies, but anything with backticks, apostrophes or
    # percent signs should come from a file rather than survive a trip through
    # shell quoting.
    body = args.body
    if args.body_file is not None:
        if args.body and args.body_file:
            parser.error("pass --body or --body-file, not both")
        try:
            if args.body_file == "-":
                body = sys.stdin.read()
            else:
                with open(args.body_file) as handle:
                    body = handle.read()
        except OSError as exc:
            parser.error(f"cannot read --body-file {args.body_file}: {exc}")

    tier = args.tier

    # harmonic-forge#283: requiredness is derived from live state — does this
    # repo use milestones at all? — rather than a hardcoded per-repo list.
    # A repo with none (harmonic-forge, openclaw-projects) is never gated;
    # nothing invents a per-issue milestone concept for a repo that decided
    # against having one.
    milestones = fetch_milestones(args.repo)
    milestone_number: int | None = None
    if milestones:
        if args.milestone is None:
            parser.error(
                f"--milestone is required for {args.repo} (harmonic-forge#283). "
                f"Open milestones: {', '.join(sorted(milestones))}. "
                "Use 'Later' for real work not yet placed in a numbered release."
            )
        if args.milestone not in milestones:
            parser.error(
                f"--milestone {args.milestone!r} is not an open milestone on "
                f"{args.repo}. Valid: {', '.join(sorted(milestones))}."
            )
        milestone_number = milestones[args.milestone]
    elif args.milestone is not None:
        print(f"[GH] {args.repo} has no milestones — ignoring --milestone "
              f"{args.milestone!r}.", file=sys.stderr)

    # harmonic-forge#329: a half-override is a mistake, not a partial
    # instruction -- and unlike an unmapped repo (below, deliberately still
    # post-create_issue() per #263 AC2: a filed issue with a missing field
    # beats a lost issue), a half-override is knowable from argv alone,
    # before any GitHub call. parser.error() calls sys.exit(), so checking
    # it only AFTER create_issue() left a live, un-boarded issue behind a
    # confusing error instead of a clean refusal -- fixed by moving just
    # this check early, not the whole resolution block.
    if bool(args.project_owner) != bool(args.project_number):
        parser.error(
            "--project-owner and --project-number must be given together; "
            "pass both to override the repo->board map, or neither to use it."
        )

    # harmonic-forge#379: warn, never block -- a cleanup issue legitimately
    # names the thing it's cleaning up (e.g. harmonic-forge#319, "Devin
    # scrub, remaining corpus"), so this must never stop the filing.
    for name, note in find_retired_citations(body):
        print(
            f"[GH] WARNING: body backtick-cites `{name}`, which is retired "
            f"-- {note}. Filing anyway; fix the citation if this wasn't "
            f"deliberate.",
            file=sys.stderr,
        )

    print(f"[GH] Creating issue in {args.repo}")

    labels = [lbl.strip() for lbl in args.labels.split(",") if lbl.strip()]
    issue_url = create_issue(args.repo, args.title, body, labels, milestone_number)
    if issue_url is None:
        return 1

    # harmonic-forge#107: derive from --repo. An explicit override still wins.
    if args.project_owner and args.project_number:
        project_owner, project_number = args.project_owner, args.project_number
        print(f"[GH] Board overridden explicitly: {project_owner}/#{project_number}")
    else:
        project_owner, project_number = resolve_board_for_repo(args.repo)

    if not project_owner or not project_number:
        print("[GH] No project board configured — skipping board-add.")
        # harmonic-forge#263, same defect in the branch nobody looked at: a
        # board field was explicitly requested and cannot be written because
        # there is no board. Returning 0 here reports success for a routing/
        # reporting signal that was silently dropped. The issue itself is
        # kept -- AC2, a filed issue with a missing field beats a lost issue.
        requested = [f"--{name} {value}" for name, value in
                     (("tier", tier), ("theme", args.theme), ("venture", args.venture))
                     if value is not None]
        if requested:
            print(
                f"[GH] ERROR: {', '.join(requested)} was requested but "
                f"these are board fields and no board is configured, so "
                f"nothing was written. Issue created: {issue_url}\n"
                f"[GH] Repair by re-running with "
                f"--project-owner/--project-number, or set "
                f"$GH_PROJECT_OWNER/$GH_PROJECT_NUMBER.",
                file=sys.stderr,
            )
            return 1
        return 0

    return 0 if add_to_board(issue_url, project_owner, project_number, tier,
                              args.theme, args.venture) else 1


if __name__ == "__main__":
    sys.exit(main())
