#!/usr/bin/env python3
"""Create a GitHub issue and, if a project board is configured, add it there.

Generalized from HRSE2's original `scripts/gh_issue.py` (harmonic-forge#53),
which hardcoded `GH_REPO`/`PROJECT_OWNER`/`PROJECT_NUMBER` for
vitalharmony/hrse. `--repo` is now required with no default (same fix as
`post_comment.py`, same incident class as #50). Board owner/number are
optional — supplied via `--project-owner`/`--project-number` or the
`GH_PROJECT_OWNER`/`GH_PROJECT_NUMBER` env vars a project's own `mise.toml`
sets — and board-add is skipped entirely (not treated as an error) when
neither is configured, since not every project has a project board.

GraphQL-quota efficiency (harmonic-forge#203): issue creation goes through
`gh api -X POST` (pure REST) rather than `gh issue create`, which was found
live to route through GraphQL for a REST-capable operation. Board field
writes (`Status`, `Estimate`) share one `project view` + `field-list` fetch
per `add_to_board()` call instead of each independently re-fetching —
GitHub's GraphQL limiting is cost-based (query complexity/node count, not
per-request), so halving the fetch count is a real, not cosmetic, saving.
"""

import json
import argparse
import os
import subprocess
import sys

STATUS_OPTION_NAME = "Todo"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def create_issue(repo: str, title: str, body: str, labels: list[str]) -> str | None:
    create_cmd = [
        "gh", "api", f"repos/{repo}/issues", "-X", "POST",
        "-f", f"title={title}",
        "-f", f"body={body or '*Created via mise gh-new-issue*'}",
    ]
    for label in labels:
        create_cmd += ["-f", f"labels[]={label}"]
    create_cmd += ["--jq", ".html_url"]

    result = _run(create_cmd, check=False)
    if result.returncode != 0:
        print(f"[GH] Failed to create issue:\n{result.stderr}", file=sys.stderr)
        return None

    issue_url = result.stdout.strip()
    print(f"[GH] Created issue: {issue_url}")
    return issue_url


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


def _set_status_todo(item_id: str, project_id: str, fields: list[dict]) -> bool:
    """Set the project item's Status field to Todo. Returns True on success.

    Takes an already-fetched project id/field list (see
    `_fetch_project_context`) rather than fetching its own — this used to
    independently re-fetch both, doubling the GraphQL cost of every
    `add_to_board()` call (harmonic-forge#203).
    """
    status_field = next((f for f in fields if f.get("name") == "Status"), None)
    if status_field is None:
        print("[GH] No 'Status' field found on the project", file=sys.stderr)
        return False
    todo_option = next((o for o in status_field.get("options", []) if o.get("name") == STATUS_OPTION_NAME), None)
    if todo_option is None:
        print(f"[GH] No '{STATUS_OPTION_NAME}' option found on the Status field", file=sys.stderr)
        return False

    edit_result = _run(
        [
            "gh", "project", "item-edit",
            "--project-id", project_id,
            "--id", item_id,
            "--field-id", status_field["id"],
            "--single-select-option-id", todo_option["id"],
        ],
        check=False,
    )
    if edit_result.returncode != 0:
        print(f"[GH] item-edit failed:\n{edit_result.stderr}", file=sys.stderr)
        return False
    return True


def _set_tier(item_id: str, project_id: str, fields: list[dict], tier: str) -> bool:
    """Set the project item's Tier field (harmonic-forge#257).

    Falls back to the legacy numeric Estimate when a board has not been migrated
    yet, so this works against either board in either order — the rename spans
    two repos and two boards and cannot be atomic.
    """
    tier = tier.strip().lower()
    tier_field = next((f for f in fields if f.get("name") == "Tier"), None)
    if tier_field is not None:
        option = next(
            (o for o in (tier_field.get("options") or [])
             if str(o.get("name", "")).strip().lower() == tier),
            None,
        )
        if option is None:
            print(f"[GH] Tier field has no option {tier!r}", file=sys.stderr)
            return False
        edit_result = _run(
            [
                "gh", "project", "item-edit",
                "--project-id", project_id,
                "--id", item_id,
                "--field-id", tier_field["id"],
                "--single-select-option-id", option["id"],
            ],
            check=False,
        )
        if edit_result.returncode != 0:
            print(f"[GH] Setting Tier failed:\n{edit_result.stderr}", file=sys.stderr)
            return False
        print(f"[GH] Set Tier = {tier}")
        return True

    # Board not migrated: write the legacy numeric field instead.
    legacy = {"fast": 3, "standard": 5, "deep": 8}.get(tier)
    estimate_field = next((f for f in fields if f.get("name") == "Estimate"), None)
    if legacy is None or estimate_field is None:
        print("[GH] Neither 'Tier' nor 'Estimate' field found on the project", file=sys.stderr)
        return False
    edit_result = _run(
        [
            "gh", "project", "item-edit",
            "--project-id", project_id,
            "--id", item_id,
            "--field-id", estimate_field["id"],
            "--number", str(legacy),
        ],
        check=False,
    )
    if edit_result.returncode != 0:
        print(f"[GH] Setting Estimate failed:\n{edit_result.stderr}", file=sys.stderr)
        return False
    print(f"[GH] Set Estimate = {legacy} (board not yet migrated to Tier)")
    return True


def set_tier(item_id: str, project_owner: str, project_number: str, tier: str) -> bool:
    """Standalone convenience wrapper for ad-hoc/backfill use (fetches its
    own project context) — `add_to_board()` uses the shared-context path
    above instead, since it may also be setting Status in the same call."""
    ctx = _fetch_project_context(project_owner, project_number)
    if ctx is None:
        return False
    return _set_tier(item_id, ctx["project_id"], ctx["fields"], tier)


def add_to_board(issue_url: str, project_owner: str, project_number: str, tier: str | None) -> bool:
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
        print(f"[GH] Warning: created issue but failed to add to board:\n{add_result.stderr}", file=sys.stderr)
        return False

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

    if not _set_status_todo(item_id, ctx["project_id"], ctx["fields"]):
        print("[GH] Warning: could not set Status=Todo — item was added but needs manual triage", file=sys.stderr)
        return False

    print(f"[GH] Set Status = {STATUS_OPTION_NAME}")

    if tier is not None:
        if not _set_tier(item_id, ctx["project_id"], ctx["fields"], tier):
            print("[GH] Warning: could not set Tier — item was added but needs manual triage", file=sys.stderr)
            return False

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a GitHub issue and optionally add it to a project board")
    parser.add_argument("--repo", required=True, help="Target repo, e.g. vitalharmony/hrse (no default — must be explicit)")
    parser.add_argument("--title", required=True, help="Issue title")
    parser.add_argument("--body", default="", help="Issue body")
    parser.add_argument("--labels", default="feature", help="Comma-separated labels")
    parser.add_argument(
        "--project-owner", default=os.environ.get("GH_PROJECT_OWNER"),
        help="Project board owner (default: $GH_PROJECT_OWNER). Omit both to skip board-add entirely.",
    )
    parser.add_argument(
        "--project-number", default=os.environ.get("GH_PROJECT_NUMBER"),
        help="Project board number (default: $GH_PROJECT_NUMBER). Omit both to skip board-add entirely.",
    )
    parser.add_argument(
        "--tier", choices=("fast", "standard", "deep"), default=None,
        help="Complexity tier for the board's Tier field (harmonic-forge#257) — "
             "the model-routing signal, not a forecast. no-op without a board.",
    )
    parser.add_argument(
        "--estimate", type=float, default=None,
        help="DEPRECATED (harmonic-forge#257): legacy story-point estimate. "
             "Mapped to a tier: >=8 deep, >=5 standard, else fast. Use --tier.",
    )
    args = parser.parse_args()

    tier = args.tier
    if tier is None and args.estimate is not None:
        # Same boundary as the retired THRESHOLD=8: 8 maps to deep, so callers
        # still passing --estimate keep their existing escalation behaviour.
        tier = "deep" if args.estimate >= 8 else "standard" if args.estimate >= 5 else "fast"
        print(f"[GH] --estimate is deprecated; mapped {args.estimate} -> Tier '{tier}'",
              file=sys.stderr)

    print(f"[GH] Creating issue in {args.repo}")

    labels = [lbl.strip() for lbl in args.labels.split(",") if lbl.strip()]
    issue_url = create_issue(args.repo, args.title, args.body, labels)
    if issue_url is None:
        return 1

    if not args.project_owner or not args.project_number:
        print("[GH] No project board configured (--project-owner/--project-number or "
              "$GH_PROJECT_OWNER/$GH_PROJECT_NUMBER not set) — skipping board-add.")
        return 0

    return 0 if add_to_board(issue_url, args.project_owner, args.project_number, tier) else 1


if __name__ == "__main__":
    sys.exit(main())
