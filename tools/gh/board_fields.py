#!/usr/bin/env python3
"""Set Tier, Theme or Venture on an issue already on its board (harmonic-forge#989).

`gh_issue.py` writes these fields only at filing, and its `--title` is
required, so it cannot set one on an existing issue. `l1_post.py` refuses a
handoff missing any of the three and names this command as the fix.

    board_fields.py set --repo R --issue N [--tier T] [--theme X] [--venture Y]

Writes only the fields it is given, never Status: it deliberately does not
call `gh_issue.add_to_board`, which resets Status to Todo (`gh_issue.py:486`)
and would move an in-progress card. Every written field is re-read fresh and
compared, so a write never fails silently (harmonic-forge#263).

Exit codes: 0 every field written and verified; 1 a query, write or re-read
failed or disagreed; 2 a refusal (bad option, not on the board, cross-owner
board, nothing to set).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gh_issue  # noqa: E402
import item_list_cache  # noqa: E402

#: CLI flag -> board field name, in the order they are written and reported.
FIELD_FLAGS = (("tier", "Tier"), ("theme", "Theme"), ("venture", "Venture"))

#: The item lookup, matching owner login plus board number. `_find_existing_item`
#: is not reused: it matches the number but not the owner, and returns None on a
#: query error, which would read a quota blip as "not on board".
_ITEM_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      projectItems(first: 20) {
        nodes {
          id
          project { number owner { ... on User { login } ... on Organization { login } } }
        }
      }
    }
  }
}
"""


class ItemQueryError(Exception):
    """The item lookup itself failed. Never the same as "not on the board"."""


def find_item(repo: str, issue: int, board_owner: str, board_number: str) -> str | None:
    """The issue's item id on board `board_owner`#`board_number`, or None if it is not there.

    Only valid for a board owned by the repo's owner: `issue.projectItems` is
    blind to a user-owned board on an organization repo (harmonic-forge#820),
    which is why `main` refuses cross-owner repos before reaching here.
    """
    repo_owner, repo_name = repo.split("/", 1)
    result = gh_issue._run(
        ["gh", "api", "graphql", "-f", f"query={_ITEM_QUERY}",
         "-F", f"owner={repo_owner}", "-F", f"repo={repo_name}", "-F", f"number={int(issue)}"],
        check=False,
    )
    if result.returncode != 0:
        raise ItemQueryError((result.stderr or "").strip() or "gh api graphql failed")
    try:
        payload = json.loads(result.stdout)
        if payload.get("errors"):
            raise ItemQueryError(json.dumps(payload["errors"]))
        issue_node = payload["data"]["repository"]["issue"]
        if issue_node is None:
            raise ItemQueryError(f"{repo}#{issue} does not exist")
        nodes = issue_node["projectItems"]["nodes"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ItemQueryError(f"unexpected response shape: {exc}") from exc
    for node in nodes or []:
        project = (node or {}).get("project") or {}
        owner = str((project.get("owner") or {}).get("login", ""))
        if str(project.get("number")) == str(board_number) and owner.lower() == board_owner.lower():
            return node.get("id")
    return None


def _option_error(fields: list[dict], name: str, value: str) -> str | None:
    """Why `value` cannot be written to field `name`, or None. Read from the
    live field list, never a hardcoded enum (harmonic-forge#300)."""
    field = next((f for f in fields if f.get("name") == name), None)
    if field is None:
        return f"the board defines no {name!r} field"
    options = [str(o.get("name", "")) for o in (field.get("options") or [])]
    if value.strip().lower() not in {o.strip().lower() for o in options}:
        return f"{name} has no option {value!r}. Valid options: {', '.join(sorted(options)) or '(none)'}"
    return None


def cmd_set(args: argparse.Namespace) -> int:
    wanted = [(name, getattr(args, flag)) for flag, name in FIELD_FLAGS
              if getattr(args, flag) is not None]
    if not wanted:
        print("[board-fields] nothing to set: pass at least one of --tier, --theme, --venture",
              file=sys.stderr)
        return 2
    repo = args.repo.strip()
    owner, number = gh_issue.resolve_board_for_repo(repo)
    if owner.lower() != repo.split("/", 1)[0].lower():
        # Operator ruling, 2026-10-13: "refuse kenekted". Resolving an item on a
        # user-owned board from an org repo needs the #820 cross-owner resolver.
        print(f"[board-fields] {repo}'s board is {owner}#{number}, owned by a different account "
              "than the repo. Cross-owner boards are not supported yet (harmonic-forge#989); "
              "set the fields in the board UI.", file=sys.stderr)
        return 2
    try:
        item_id = find_item(repo, args.issue, owner, number)
    except ItemQueryError as exc:
        print(f"[board-fields] cannot look up {repo}#{args.issue}'s board item: {exc}",
              file=sys.stderr)
        return 1
    if item_id is None:
        print(f"[board-fields] {repo}#{args.issue} is not on board {owner}#{number}. Add it first:\n"
              f"  gh project item-add {number} --owner {owner} "
              f"--url https://github.com/{repo}/issues/{args.issue}", file=sys.stderr)
        return 2
    ctx = gh_issue._fetch_project_context(owner, number)
    if ctx is None:
        print(f"[board-fields] cannot read project {owner}#{number}", file=sys.stderr)
        return 1
    # Every option is checked before anything is written, so a typo in one
    # flag never leaves the item half-edited.
    errors = [e for e in (_option_error(ctx["fields"], name, value) for name, value in wanted) if e]
    if errors:
        for error in errors:
            print(f"[board-fields] {error}", file=sys.stderr)
        return 2
    ok = True
    written = []
    for name, value in wanted:
        if gh_issue._set_single_select(item_id, ctx["project_id"], ctx["fields"], name, value):
            written.append((name, value))
        else:
            ok = False
    for name, value in written:
        try:
            actual = item_list_cache.fetch_issue_field(
                repo, args.issue, number, field=name, run=lambda cmd: gh_issue._run(cmd, check=False),
                ttl=0, owner=owner)
        except item_list_cache.GhItemListError as exc:
            print(f"[board-fields] cannot re-read {name}: {exc}", file=sys.stderr)
            ok = False
            continue
        if not _matches(actual, value):
            print(f"[board-fields] {name} re-read as {actual!r}, expected {value!r}",
                  file=sys.stderr)
            ok = False
        else:
            print(f"[board-fields] verified {name} = {actual}")
    return 0 if ok else 1


def _matches(actual: str | None, expected: str) -> bool:
    return isinstance(actual, str) and actual.strip().lower() == expected.strip().lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    set_parser = sub.add_parser("set", help="set board fields on an existing issue")
    set_parser.add_argument("--repo", required=True, help="owner/name")
    set_parser.add_argument("--issue", required=True, type=int, help="issue number")
    set_parser.add_argument("--tier", help="Tier: fast | standard | deep")
    set_parser.add_argument("--theme", help="Theme; options are read live from the board")
    set_parser.add_argument("--venture", help="Venture; options are read live from the board")
    args = parser.parse_args(argv)
    return cmd_set(args)


if __name__ == "__main__":
    sys.exit(main())
