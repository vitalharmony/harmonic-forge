#!/usr/bin/env python3
"""The five universal lane mise tasks, written once (harmonic-forge#730).

`projects.toml` claimed `onboarded = true` / `runs_lane3 = true` for two repos
that had almost none of the lane task layer those flags imply. Fixing the two
instances was never the point -- harmonic-forge#208's own decomposition asked
for "a standard lane task set any repo adopts", so the *next* repo does not
need another hand-porting pass.

## Why a generator and not a mise include

`[task_config] includes` cannot express this. Measured on mise 2026.9.11: an
included file is parsed as ONE task definition, not a collection -- a file
containing `[tasks.foo]` fails with "unknown field `foo`, expected one of
`description`, `alias`, ...", and a file written as a bare task instead makes
`mise tasks` list `description` and `run` as the task NAMES. The directory form
behaves identically. No include shape yields five named tasks, so the option
was eliminated on evidence rather than preference (harmonic-forge#730 Q1).

The generator also reuses a path that already exists: `forge_onboard.py
--apply` already writes into consumer repos (worktrees, then `sync_rules.py`),
so these tasks becoming generated content there adds no second distribution
mechanism -- the trap `forge_onboard.py`'s own docstring names about
harmonic-forge#324's declarative reconciler.

## What is universal and what is not

These five are required of every onboarded repo regardless of shape.
`gate-restart`, `gate-e2e` and `check-worktree-busy` are NOT here: they restart
and exercise a live running service, and a repo without one does not need them
(harmonic-forge's own `lane3-end` says it plainly -- "unlike hrse, this repo
has no backend and no lease"). A repo that has a live service declares its own
equivalents; a repo that does not declares that it does not.

## The generated text is a FLOOR, never a ceiling

A repo may carry a richer version of any of these. hrse's `lane3-begin` stamps
an owner pid, runs a port preflight and acquires a scheduler lease; its
`gate-checkout` propagates `LANE`. `check_lane_tasks` therefore verifies that a
declared task RESOLVES, never that its body matches what this module renders --
a body-match check would fail hrse on the day it shipped.
"""
from __future__ import annotations

#: Marks the generated span so a second `--apply` can find it and leave it
#: alone. A repo that has edited the block keeps its edits: the markers say
#: where the generated text went, not that it is still verbatim.
BEGIN_MARKER = "# >>> harmonic-forge lane tasks (generated, harmonic-forge#730) >>>"
END_MARKER = "# <<< harmonic-forge lane tasks <<<"

#: How every generated task reaches the platform. Never a repo-relative path:
#: harmonic-forge's own `gate-checkout` used `tools/worktree/...` and resolved
#: only because harmonic-forge IS the platform -- the same text in any other
#: repo silently names a file that is not there.
PLATFORM = '"${HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}"'

#: name -> TOML body. The names are the DEFAULTS a repo's `[project.protocol]`
#: block declares; a repo free to rename them is why the check reads the
#: declared name rather than assuming these.
LANE_TASKS: dict[str, str] = {
    "l1-post": f'''[tasks.l1-post]
description = "Post a Lane 1 protocol artifact through the platform-owned transport. The attested --sha/--branch resolve against the invoking checkout, independently of the target --repo; omit --repo to resolve the invoking checkout through projects.toml. The script owns the flag surface, so pass its arguments after `--`."
usage = \'\'\'
arg "args" var=#true help="tools/gh/l1_post.py arguments verbatim, e.g. --issue 123 --kind handoff --sha <sha> --branch main --file body.md --plan-first false"
\'\'\'
run = """
# mise single-quotes each multi-word `var=#true` value inside $usage_args, and
# an UNQUOTED expansion re-splits it on whitespace leaving the literal quotes
# behind. `eval` re-joins and re-parses, restoring mise's own quoting
# (harmonic-forge#568, live-verified).
eval python3 {PLATFORM}/tools/gh/l1_post.py $usage_args
"""
''',
    "lane-comment": f'''[tasks.lane-comment]
description = "Post an attested lane discussion/spec/gate-result through the platform transport; omit --repo to resolve the invoking checkout through projects.toml."
usage = \'\'\'
flag "--issue <issue>" help="Issue number" required=#true
flag "--file <file>" help="Path to the comment body" required=#true
flag "--repo <repo>" help="Target GitHub repository; defaults from the invoking checkout via projects.toml"
flag "--kind <kind>" help="discussion | spec | gate-result" default="discussion"
\'\'\'
run = \'\'\'
set -- --issue "$usage_issue" --file "$usage_file" --kind "$usage_kind"
[ -n "${{usage_repo:-}}" ] && set -- "$@" --repo "$usage_repo"
python3 {PLATFORM}/tools/gh/post_lane_discussion.py "$@"
\'\'\'
''',
    "gate-checkout": f'''[tasks.gate-checkout]
description = "Lane 3: switch to a branch without discarding uncommitted work. Refuses pathspec forms and dirty trees. Refuses if a live process (dev server, running test) has its cwd inside this worktree — a second actor's checkout would otherwise silently yank state out from under it, no lock or error (harmonic-forge#137)."
usage = \'\'\'
arg "<branch>" help="Branch name to check out"
\'\'\'
run = \'\'\'
set -e
branch="$usage_branch"

if [ -z "$branch" ]; then
  echo "gate-checkout: branch name is required" >&2
  exit 1
fi

case "$branch" in
  -*|.|..|/*|*[[:space:]]*)
    echo "gate-checkout: invalid branch name '$branch'" >&2
    exit 1
    ;;
esac

if [ -n "$(git status --porcelain)" ]; then
  echo "gate-checkout: refusing to switch branches with a dirty working tree" >&2
  exit 1
fi

python3 {PLATFORM}/tools/worktree/check_worktree_busy.py "$(pwd)" || exit 1

git fetch origin "$branch" --quiet || {{
  echo "gate-checkout: could not fetch '$branch' from origin" >&2
  exit 1
}}

# A branch already checked out in a sibling worktree cannot be attached here;
# a detached checkout of the same commit gates identically (harmonic-forge#29/#30).
if git checkout "$branch" --quiet 2>/dev/null; then
  git merge --ff-only "origin/$branch" --quiet || true
else
  git checkout --detach "origin/$branch" --quiet
  echo "gate-checkout: '$branch' is checked out elsewhere; using a detached checkout of origin/$branch"
fi
git --no-pager log -1 --format="gate-checkout: now at %h %s"
\'\'\'
''',
    "lane3-begin": '''[tasks.lane3-begin]
description = "Lane 3: mark this worktree as actively filling Lane 3, unlocking gate-checkout for the next 12 hours (harmonic-forge#138). Run once per Lane 3 session, from the worktree you're gating in, before using it."
run = \'\'\'
git_dir="$(git rev-parse --absolute-git-dir)"
touch "$git_dir/LANE3_ACTIVE"
echo "lane3-begin: $(pwd) marked Lane 3-active"
\'\'\'
''',
    "lane3-end": '''[tasks.lane3-end]
description = "Lane 3: clear the LANE3_ACTIVE marker set by lane3-begin (harmonic-forge#433). Without it a finished worktree reads as Lane 3-active forever and has to be cleared by hand."
run = \'\'\'
set -e
git_dir="$(git rev-parse --absolute-git-dir)"
rm -f "$git_dir/LANE3_ACTIVE"
echo "lane3-end: $(pwd) cleared"
\'\'\'
''',
}


def render(task_names: dict[str, str] | None = None) -> str:
    """The generated block, marker to marker.

    `task_names` maps a canonical name to the name a repo actually declares, so
    a repo that calls `lane-comment` something else still gets a task under the
    name its `[project.protocol]` block names -- the check reads the declared
    name, and a generator that ignored it would write a task the check then
    reports as missing.
    """
    names = task_names or {}
    parts = [BEGIN_MARKER,
             "# Generated by tools/onboard/lane_tasks.py via `forge_onboard.py --apply`.",
             "# Edit the generator, not this block -- but a repo MAY replace a task with a",
             "# richer version of its own (hrse does): the check verifies a declared task",
             "# resolves, never that its body matches this text.",
             ""]
    for canonical, body in LANE_TASKS.items():
        declared = names.get(canonical, canonical)
        parts.append(body.replace(f"[tasks.{canonical}]", f"[tasks.{declared}]", 1))
    parts.append(END_MARKER)
    return "\n".join(parts) + "\n"


def render_subset(wanted: dict[str, str]) -> str:
    """Render only the named tasks — `{canonical name: declared name}`.

    A repo that already hand-wrote one of the five keeps it. Writing the whole
    block regardless would either duplicate a `[tasks.x]` key (mise then fails
    to parse the file at all) or silently replace a repo's own richer version.
    """
    parts = [BEGIN_MARKER,
             "# Generated by tools/onboard/lane_tasks.py via `forge_onboard.py --apply`.",
             "# Edit the generator, not this block -- but a repo MAY replace a task with a",
             "# richer version of its own (hrse does): the check verifies a declared task",
             "# resolves, never that its body matches this text.",
             ""]
    for canonical, declared in wanted.items():
        body = LANE_TASKS[canonical]
        parts.append(body.replace(f"[tasks.{canonical}]", f"[tasks.{declared}]", 1))
    parts.append(END_MARKER)
    return "\n".join(parts) + "\n"


def has_block(mise_toml: str) -> bool:
    return BEGIN_MARKER in mise_toml and END_MARKER in mise_toml


def task_names(mise_toml: str) -> set[str]:
    """Every `[tasks.<name>]` declared in a mise.toml, generated or not.

    Text-scanned rather than TOML-parsed on purpose: mise's own schema rejects
    the file's task bodies as unknown fields when read as plain TOML in some
    shapes, and this only ever needs the names.
    """
    import re  # noqa: PLC0415

    return set(re.findall(r"(?m)^\[tasks\.([A-Za-z0-9_-]+)\]", mise_toml))
