#!/usr/bin/env python3
"""Bring a repo under the 3-lane protocol, idempotently (harmonic-forge#498).

Onboarding used to be a hand procedure spread across five places, each keeping
its own copy of the same facts: `3-lane-protocol.md` § Per-Lane Working
Directories, `sync_rules.py --project`, a copied `.claude/settings.json` hooks
block, a prefix letter in `rules/lane-shorthand.md`, a board mapping in
`gh_issue.py`, and an `autoMemoryDirectory` setting. Four repos are covered
today and two more are coming, and every one of those steps could be half-done
without anything noticing.

**Verify is the default; `--apply` writes.** A tool whose default action
mutates six things across a repo is one nobody runs speculatively, and the
whole value here is being able to ask "is this repo actually set up?" cheaply
and often. `mise run hygiene` runs the verify mode across the manifest.

**Exit codes are three-valued on purpose.** 0 = every check passed, 1 = a check
failed, 2 = the run could not happen (bad manifest, missing project). A tool
that returned non-zero for both "found a problem" and "could not look" makes a
CI gate unable to tell a real finding from its own misconfiguration --
`memory_lint`'s missing-store case is the local precedent.

**What this does NOT do yet.** The hooks half installs a template and reports
drift; it does not reconcile. That waits on harmonic-forge#324's declarative
agent-surface synchronizer, and pretending otherwise here would mean writing a
second, competing reconciler that #324 then has to delete.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lane_tasks  # noqa: E402
from manifest import (  # noqa: E402
    ManifestError, Project, check_prefix_agreement, load, prefixes,
)

#: The checkout this module happens to live in. Fine for reading; NEVER the
#: source to point another repo's symlinks at.
_THIS_CHECKOUT = Path(__file__).resolve().parents[2]


def platform_source() -> Path:
    """The CANONICAL platform checkout that directives are linked from.

    Not `_THIS_CHECKOUT`. `sync_rules.py` symlinks `<project>/.claude/rules/*`
    at `<the root it was run from>/rules`, so running this tool out of an
    implementation worktree points every onboarded repo's rules and agents at
    that worktree -- and they all dangle the moment it is deleted.

    That is not hypothetical: it happened during this issue's own AC5 backfill.
    32 symlinks across four repos were repointed at `/tmp/forge-498-impl`,
    silently, because `.claude/rules/.gitignore` hides them from `git status`
    and `check_directives` SKIPs the platform repo so it would never have
    reported its own.

    Resolved from the manifest's `harmonic-forge` entry, which is the one place
    that records where the real checkout is. Falls back to this checkout only
    when the manifest has no such entry, and says so rather than guessing.
    """
    for project in load():
        if project.checkout is not None and _is_platform(project.checkout):
            return project.checkout
    return _THIS_CHECKOUT

#: Status glyphs. `SKIP` exists so "not applicable to this project" never reads
#: as either a pass or a failure -- a projected repo with no checkout has no
#: worktrees to be missing.
OK, FAIL, SKIP = "ok", "FAIL", "skip"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    def line(self) -> str:
        mark = {OK: "  ok  ", FAIL: " FAIL ", SKIP: " skip "}[self.status]
        return f"[{mark}] {self.name}" + (f" — {self.detail}" if self.detail else "")


def _settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


def _is_platform(checkout: Path) -> bool:
    """Is this checkout the platform repo itself?

    Detected by STRUCTURE, not by comparing against `PLATFORM_ROOT`. This
    module runs from whatever worktree it was checked out into, so a path
    comparison says "not the platform" whenever the tool is run from a
    worktree -- which is every time it is developed or gated.
    """
    return (checkout / "3-lane-protocol.md").is_file() and (checkout / "sync_rules.py").is_file()


def check_checkout(project: Project) -> Check:
    if project.checkout is None:
        return Check("checkout", SKIP, "no path declared (projected repo)")
    if not (project.checkout / ".git").exists():
        return Check("checkout", FAIL, f"{project.checkout} is not a git checkout")
    return Check("checkout", OK, str(project.checkout))


#: harmonic-forge#746 AC2. Commits behind origin/main a lane worktree may
#: trail before being flagged stale. Chosen small rather than zero: a
#: worktree naturally lags by a handful of commits between one merge and the
#: next lane launch, and zero-tolerance would flag that constantly instead of
#: the actual failure mode -- drift nobody notices because nothing ever
#: reports it. Ten is enough headroom for "just merged, not yet relaunched"
#: while catching real drift (this issue's own live case was 35-90) long
#: before it compounds into stale hooks/posting guards silently in effect.
STALE_WORKTREE_BEHIND_THRESHOLD = 10


@dataclass
class WorktreeCurrency:
    state: str
    detail: str
    failing: bool


def worktree_commit_currency(path: Path) -> WorktreeCurrency:
    """One worktree's commit currency against `origin/main`
    (harmonic-forge#746 AC1/AC3/AC4/AC5).

    Caller fetches first (`check_worktrees` does) -- this never fetches
    itself, so repeated verify runs stay read-only and cheap, matching
    `_worktree_is_safe_to_advance`'s own read-only contract. This function
    never writes to the worktree either way; "detected" and "advanced" are
    two different capabilities on purpose (AC3/AC4 -- see the issue).

    `.failing` is computed here, independently of which descriptive `.state`
    label applies, specifically because staleness and dirtiness are NOT
    mutually exclusive (preclose finding: a first version returned `"dirty"`
    as a terminal state, so a worktree 90 behind with one stray edit read
    `ok` -- the dirty state silently swallowed the stale one). Both are
    reported in `.detail` when both apply; `.failing` is true if either is.

    `state` is one of:
    - `"current"` -- behind the threshold, nothing to report.
    - `"stale"` -- behind `origin/main` past `STALE_WORKTREE_BEHIND_THRESHOLD`.
      Applies on a branch too, if that branch's tip has already merged (or
      never diverged) -- "on a branch" alone does not exempt a worktree
      whose branch carries no unmerged commits (preclose finding: the
      original branch short-circuit exempted every branch worktree from
      measurement, not just genuinely unmerged ones).
    - `"branch"` -- on a branch that IS ahead of `origin/main` (genuinely
      unmerged/in-flight). AC5: behind-main is the wrong predicate for that
      worktree, so it is never scored as stale -- but the behind-count is
      still reported in `.detail`, per AC2's "every lane worktree" wording.
    - `"detached-orphan"` -- detached HEAD carrying commits no branch (local
      or remote) contains. AC3's live test case.
    - `"dirty"` -- uncommitted changes, and otherwise current. Reported,
      never silently folded into a verdict that would suggest it is safe to
      touch.
    - `"no-git"` -- `path` carries no `.git` at all, i.e. it is not a git
      worktree. Never failing: this is what many of this suite's own
      synthetic fixtures look like, and "not a worktree" is `check_worktrees`'
      existence check's job to catch, not this function's.
    - `"unreadable"` -- `path` IS a git worktree (`.git` present) but a git
      command still failed -- a genuinely broken worktree, and failing
      (preclose finding: an earlier version fail-opened on every
      "could not measure" case, indistinguishable from `"no-git"`, which
      would have hidden a real corrupt worktree in production).
    """
    if not (path / ".git").exists():
        return WorktreeCurrency("no-git", "not a git worktree", False)

    status = _run(["git", "-C", str(path), "status", "--porcelain"])
    if status[0] != 0:
        return WorktreeCurrency("unreadable", "could not read status", True)
    dirty = bool(status[1].strip())

    head_ref = _run(["git", "-C", str(path), "symbolic-ref", "-q", "HEAD"])
    on_branch = head_ref[0] == 0
    branch = head_ref[1].strip().removeprefix("refs/heads/") if on_branch else None

    behind = _run(["git", "-C", str(path), "rev-list", "--count", "HEAD..origin/main"])
    ahead = _run(["git", "-C", str(path), "rev-list", "--count", "origin/main..HEAD"])
    if behind[0] != 0 or ahead[0] != 0:
        return WorktreeCurrency("unreadable",
                                "could not compute ahead/behind origin/main", True)
    behind_n = int(behind[1].strip() or 0)
    ahead_n = int(ahead[1].strip() or 0)
    dirty_suffix = ", uncommitted changes" if dirty else ""
    where = f"on {branch}, " if branch else ""

    if on_branch and ahead_n:
        detail = f"{where}{ahead_n} ahead, {behind_n} behind (unmerged, not scored){dirty_suffix}"
        return WorktreeCurrency("branch", detail, False)

    if not on_branch and ahead_n:
        # `for-each-ref`, not `branch -a --contains`: the latter prints a
        # `* (no branch)` pseudo-line for the detached HEAD itself on every
        # detached worktree, which made `contains[1].strip()` non-empty
        # unconditionally and this branch never fired (caught by this
        # issue's own test).
        contains = _run(["git", "-C", str(path), "for-each-ref", "--contains", "HEAD",
                         "refs/heads", "refs/remotes"])
        if contains[0] == 0 and not contains[1].strip():
            detail = f"{ahead_n} ahead, {behind_n} behind, no branch contains HEAD{dirty_suffix}"
            return WorktreeCurrency("detached-orphan", detail, True)

    stale = behind_n > STALE_WORKTREE_BEHIND_THRESHOLD
    if dirty and stale:
        return WorktreeCurrency(
            "stale", f"{where}{behind_n} behind origin/main{dirty_suffix}", True)
    if dirty:
        return WorktreeCurrency(
            "dirty", f"{where}{behind_n} behind origin/main{dirty_suffix}", False)
    if stale:
        return WorktreeCurrency("stale", f"{where}{behind_n} behind origin/main", True)
    return WorktreeCurrency(
        "current", f"{where}{behind_n} behind origin/main" if behind_n else f"{where}current",
        False)


def check_worktrees(project: Project) -> Check:
    """harmonic-forge#746 AC6: `<repo>-release` (and any other non-lane
    worktree) is excluded from this check by construction, not by omission
    -- `Project.worktrees` only ever names `<repo>-lane{2,3}` (see
    `manifest.Project.worktrees`/`worktree_names`), the same scope
    `check_hooks`/`advance_stale_worktrees` already use. A `-release`
    worktree is not a lane worktree the protocol launches sessions into, so
    it is out of scope for the same reason the main checkout itself is."""
    if project.checkout is None:
        return Check("lane worktrees", SKIP, "no checkout")
    missing = [p for p in project.worktrees if not p.exists()]
    if missing:
        return Check("lane worktrees", FAIL,
                     "missing: " + ", ".join(p.name for p in missing))
    lanes = " + ".join(path.name.rsplit("-", 1)[-1] for path in project.worktrees)
    functional = _run(["git", "-C", str(project.checkout), "rev-parse", "--git-dir"])
    if functional[0] != 0:
        # Not a functional git checkout -- `.git` existing as a bare
        # directory is not enough proof (this suite's own non-git-backed
        # synthetic fixtures use exactly that shape: `mkdir`, no `git
        # init`). Existence is all that can be verified here, same as the
        # predecessor check.
        return Check("lane worktrees", OK, f"{lanes} present")
    # harmonic-forge#746 AC1: measure commit currency alongside existence --
    # a worktree present but 90 commits behind used to read identically to
    # one that is current. Fetch once, up front, so every worktree's
    # ahead/behind below is computed against the same origin/main snapshot.
    # The fetch's own exit code is checked (preclose finding: a discarded
    # fetch failure meant every worktree was silently compared against
    # whatever origin/main ref happened to already be on disk -- a stale
    # ref made 90-behind worktrees compute "0 behind" and read green).
    fetched = _run(["git", "-C", str(project.checkout), "fetch", "-q", "origin", "main"])
    if fetched[0] != 0:
        return Check("lane worktrees", FAIL,
                     f"{lanes} present; could not fetch origin/main: {fetched[1]}")
    findings = []
    failing = False
    for path in project.worktrees:
        currency = worktree_commit_currency(path)
        findings.append(f"{path.name}: {currency.detail}")
        failing = failing or currency.failing
    detail = f"{lanes} present; " + "; ".join(findings)
    return Check("lane worktrees", FAIL if failing else OK, detail)


def check_protocol(project: Project) -> Check:
    """Every declared project carries the inputs portable lane tooling reads."""
    if project.protocol is None:
        return Check("protocol", FAIL, "missing [project.protocol] in projects.toml")
    state = "Lane 3 enabled" if project.protocol.runs_lane3 else "Lane 3 disabled"
    return Check("protocol", OK, state)


def check_lane_tasks(project: Project) -> Check:
    """Do the lane tasks this repo DECLARES actually exist in its mise.toml?

    `check_protocol` above confirms a `[project.protocol]` block is present. It
    never confirmed the task names inside it resolve, so `projects.toml` could
    claim -- and did claim, for two repos -- `onboarded = true` /
    `runs_lane3 = true` while almost none of the lane task layer existed
    (harmonic-forge#730). The only signal was a Lane 1 session running
    `mise run l1-post` and getting "task not found".

    **Resolves the DECLARED name, never a hardcoded one.** A repo is free to
    call its lane-comment task something else; reading `protocol.<x>_task` is
    what makes this a check of the repo's own claim rather than of a convention.

    **Verifies existence, never body.** hrse's `lane3-begin` stamps an owner
    pid, runs a port preflight and takes a scheduler lease; harmonic-forge's
    touches a file. Both are correct. A body-match check would fail hrse on the
    day it shipped, which is why the generated block is documented as a floor.
    """
    if project.protocol is None:
        return Check("lane tasks", SKIP, "no [project.protocol]")
    if project.checkout is None:
        return Check("lane tasks", SKIP, "no checkout")
    mise = project.checkout / "mise.toml"
    if not mise.is_file():
        return Check("lane tasks", FAIL, f"no mise.toml in {project.checkout}")

    declared = {
        "l1_post_task": project.protocol.l1_post_task,
        "lane_comment_task": project.protocol.lane_comment_task,
        "gate_checkout_task": project.protocol.gate_checkout_task,
        "lane3_begin_task": project.protocol.lane3_begin_task,
        "lane3_end_task": project.protocol.lane3_end_task,
    }
    present = lane_tasks.task_names(mise.read_text(encoding="utf-8"))
    missing = {field: name for field, name in declared.items() if name not in present}
    if missing:
        detail = ", ".join(f"{name} ({field})" for field, name in sorted(missing.items()))
        return Check("lane tasks", FAIL, f"declared but absent from mise.toml: {detail}")
    return Check("lane tasks", OK, f"{len(declared)} declared task(s) resolve")


def check_gate_adapter(project: Project) -> Check:
    """A Lane 3 repo either has an adapter manifest or says it needs none.

    ADR-008's capabilities (`residue_sweep`, `lease`, `merge_target_check`,
    `tier_w_message`) are graph/database/live-service concepts, and a repo that
    owns none of those genuinely needs no manifest -- cymagraph-infra
    provisions infrastructure, openclaw-projects is a prototyping ground.

    What is outlawed is SILENCE (harmonic-forge#730 DJC 2). An absent file with
    no declaration is indistinguishable from an oversight, which is this
    issue's own complaint one level up. So a `runs_lane3 = true` repo must
    carry either the manifest or an explicit `needs_gate_adapter = false`, and
    a repo with neither fails. An empty manifest would be worse than either --
    it asserts adapters were considered while declaring nothing.
    """
    if project.protocol is None or not project.protocol.runs_lane3:
        return Check("gate adapter", SKIP, "no Lane 3")
    if project.checkout is None:
        return Check("gate adapter", SKIP, "no checkout")
    manifest_file = project.checkout / ".claude" / "gate-adapter.json"
    if manifest_file.is_file():
        return Check("gate adapter", OK, "declared in .claude/gate-adapter.json")
    if project.protocol.needs_gate_adapter is False:
        return Check("gate adapter", OK, "declared unnecessary for this repo")
    return Check("gate adapter", FAIL,
                 "runs_lane3 = true but no .claude/gate-adapter.json and no "
                 "needs_gate_adapter = false declaration")


def check_directives(project: Project) -> Check:
    if project.checkout is None:
        return Check("directives", SKIP, "no checkout")
    # The platform repo is the SOURCE of the directives every other repo links
    # to. It carries `rules/`, not `.claude/rules/`, and reporting that as a
    # failure told the operator to sync harmonic-forge against itself.
    if _is_platform(project.checkout):
        # It IS the source, so it has no `.claude/rules/` of platform rules to
        # link -- but it does carry `.claude/agents/` links back into itself,
        # and a blanket SKIP here is why 8 of its own symlinks pointed at a
        # `/tmp` worktree with nothing reporting it.
        agents = project.checkout / ".claude" / "agents"
        strays = [p.name for p in agents.iterdir()
                  if p.is_symlink() and not p.resolve().exists()] if agents.is_dir() else []
        if strays:
            return Check("directives", FAIL,
                         "platform repo, dangling agent link(s): " + ", ".join(sorted(strays)))
        return Check("directives", SKIP, "platform repo — it is the source")
    rules = project.checkout / ".claude" / "rules"
    if not rules.is_dir():
        return Check("directives", FAIL, "no .claude/rules/")
    links = [p for p in rules.iterdir() if p.is_symlink()]
    if not links:
        return Check("directives", FAIL,
                     ".claude/rules/ has no symlinks — sync_rules.py never ran")
    dangling = [p.name for p in links if not p.resolve().exists()]
    if dangling:
        return Check("directives", FAIL, "dangling: " + ", ".join(sorted(dangling)))
    return Check("directives", OK, f"{len(links)} linked rule file(s)")


def check_entrypoint(project: Project) -> Check:
    if project.checkout is None:
        return Check("entrypoint", SKIP, "no checkout")
    present = [n for n in ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
               if (project.checkout / n).is_file()]
    if "CLAUDE.md" not in present:
        return Check("entrypoint", FAIL, "no CLAUDE.md")
    return Check("entrypoint", OK, ", ".join(present))


def _hook_commands(hooks: dict) -> list[str]:
    """Every `command` string in a settings.json hooks block, any event."""
    found: list[str] = []
    for matchers in hooks.values():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            for entry in matcher.get("hooks") or []:
                if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                    found.append(entry["command"])
    return found


_PLATFORM_HOOK_PATH = re.compile(r'["\']?(\$\{HOME\}|~|/[^"\'\s]*?)/(harmonic-forge/[^"\'\s]+\.py)')


def unresolvable_hook_targets(hooks: dict) -> list[str]:
    """Platform hook scripts a settings.json names that do not exist on disk.

    harmonic-forge#547's preclose-inspection: this check read only the event
    KEY NAMES and never a `command` string, so renaming or moving a platform
    hook script made every session in every consuming repo run a command that
    exits 2 and produces no output. For `SessionStart` that does not block the
    session — it just silently contributes nothing, which is indistinguishable
    from the pre-#547 state the issue was filed about, and this tool reported
    green throughout.

    That is the reconciliation half the skills manifest has and hooks lacked.
    The answer is NOT a `hooks` key on the skills manifest (that manifest
    exists because gitignored symlinks cannot travel with a clone, which is not
    hooks' problem) — it is asserting here that what settings.json names
    actually resolves.
    """
    missing: list[str] = []
    for command in _hook_commands(hooks):
        match = _PLATFORM_HOOK_PATH.search(command)
        if not match:
            continue
        candidate = Path(os.path.expandvars(os.path.expanduser(
            f"{match.group(1)}/{match.group(2)}")))
        if not candidate.is_file():
            missing.append(str(candidate))
    return sorted(set(missing))


#: `SessionStart` sources a wake-up hook must match to actually reach a lane
#: session (harmonic-forge#560). `compact` is deliberately NOT here: it has its
#: own entry running `compaction_marker.py`, whose `build_context()` already
#: carries a wake-up line, so matching it in both injects twice.
WAKEUP_SOURCES = ("startup", "resume", "clear", "fork")


def _matcher_covers(matcher: str | None, source: str) -> bool:
    """Does this matcher match this source?

    A hook `matcher` is a REGEX, not a pipe-delimited list — `startup|resume`
    only looks like one. Splitting on "|" and testing membership got the common
    case right and every other case wrong: `".*"`, `"clear|fork"` combined with
    a second block, or an OMITTED matcher (which matches everything) all read
    as covering nothing. An omitted or empty matcher is the match-all case.
    """
    if matcher in (None, "", "*"):
        return True
    try:
        # UNANCHORED, because that is what Claude Code does: dispatch is
        # `new RegExp(matcher).test(source)` (verified in the installed binary,
        # 2.1.267), with an invalid pattern logged and treated as no match.
        # `re.fullmatch` here would be stricter than the runtime and report a
        # gap for a matcher that actually fires — a check that disagrees with
        # the thing it checks is worse than no check.
        return re.search(matcher, source) is not None
    except re.error:
        # An unparseable matcher matches nothing, which is itself the finding.
        return False


def sessionstart_source_gaps(hooks: dict) -> list[str]:
    """Sources no `belt_wakeup.py` entry covers.

    harmonic-forge#560. `belt_wakeup.py` shipped wired as `startup|resume`, and
    `clear` is a DISTINCT source rather than a variant of `startup` — so the
    hook was silent for `lane<N> /clear`, which is how the operator actually
    starts lane sessions across all three lanes. Nothing failed; it simply
    never fired, and a fresh Lane 1 session opened by asserting it had no lane
    while `LANE=1` sat in its environment.

    Coverage is accumulated across EVERY block naming the hook, not read off
    the first one. Returning on the first match reported a settings file that
    splits the hook over two blocks as covering only what the first block
    listed — and this same change establishes the two-block shape, by adding a
    second `SessionStart` block for `compaction_marker.py` to two repos.

    Checked in the per-repo onboarding report rather than only in
    harmonic-forge's own unit tests: a repo's tests can only speak for that
    repo, and this is precisely the partial-distribution class #540 exists for.
    An empty list is full coverage; a repo that does not wire the hook at all
    returns an empty list too, because absent is a different finding from
    mis-matched.
    """
    matchers: list[str | None] = []
    for block in hooks.get("SessionStart") or []:
        if not isinstance(block, dict):
            continue
        commands = " ".join(h.get("command", "") for h in block.get("hooks") or []
                            if isinstance(h, dict))
        if "belt_wakeup.py" in commands:
            matchers.append(block.get("matcher"))
    if not matchers:
        return []
    return [s for s in WAKEUP_SOURCES
            if not any(_matcher_covers(m, s) for m in matchers)]


def stale_worktree_hook_gaps(project: Project) -> list[str]:
    """Worktrees of this project whose OWN settings.json misses a source.

    The finding that nearly shipped this issue as a no-op (preclose, #560).
    `.claude/settings.json` is a TRACKED file, so every worktree carries its
    own copy of it — and the lane launchers do not run in the main checkout.
    `tools/lane/lane2` cd's into `HRSE2-lane2`, which is exactly where #560 was
    reproduced, and which sits on a DETACHED HEAD that no merge to `main` ever
    fast-forwards.

    Measured at the time of writing: seven lane worktrees across four repos,
    every one of them still `startup|resume`. Merging the fix would have left
    `lane2 /clear` — the launch the issue is entirely about — exactly as broken
    as before, while this same report printed `ok` for the one checkout nobody
    launches from.
    """
    if project.checkout is None:
        return []
    result = subprocess.run(
        ["git", "-C", str(project.checkout), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return []
    declared = {path.resolve() for path in project.worktrees}
    gaps = []
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line.split(" ", 1)[1])
        if path.resolve() == project.checkout or path.resolve() not in declared:
            continue
        settings = path / ".claude" / "settings.json"
        if not settings.is_file():
            continue
        try:
            hooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks")
        except (OSError, ValueError):
            continue
        if isinstance(hooks, dict) and sessionstart_source_gaps(hooks):
            gaps.append(path.name)
    return sorted(gaps)


def check_hooks(project: Project) -> Check:
    """Presence, shape, AND that every platform script it names resolves.

    Full reconciliation still waits on harmonic-forge#324; this closes the one
    gap that made the check actively misleading rather than merely incomplete.
    """
    if project.checkout is None:
        return Check("hooks", SKIP, "no checkout")
    settings = project.checkout / ".claude" / "settings.json"
    if not settings.is_file():
        return Check("hooks", FAIL, "no .claude/settings.json")
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Check("hooks", FAIL, f"unparseable settings.json: {exc}")
    hooks = data.get("hooks")
    if not hooks:
        return Check("hooks", FAIL, "settings.json declares no hooks block")

    events = sorted(hooks) if isinstance(hooks, dict) else []
    missing = unresolvable_hook_targets(hooks) if isinstance(hooks, dict) else []
    if missing:
        return Check("hooks", FAIL,
                     f"{len(events)} event(s) declared, but "
                     f"{len(missing)} named script(s) do not exist: "
                     + ", ".join(missing))

    # Declaring ONE event is not the same as being guarded. Naming the count of
    # PreToolUse entries stops a repo with a single SessionStart line from
    # reading identically to one carrying the full guard set — the specific
    # regression #547 introduced in cymagraph-infra, which had no PreToolUse
    # hooks at all and flipped from FAIL to OK the moment a settings.json
    # existed.
    # A wake-up hook that never fires for the launch path in daily use is
    # worse than none: the report reads green and the session is unguarded.
    gaps = sessionstart_source_gaps(hooks) if isinstance(hooks, dict) else []
    if gaps:
        return Check("hooks", FAIL,
                     "belt_wakeup.py matches no SessionStart source for: "
                     + ", ".join(gaps)
                     + " -- a session started that way is never told its lane "
                       "(harmonic-forge#560)")

    # The main checkout being right is not the same as the launcher's checkout
    # being right, and the launchers do not run here.
    stale = stale_worktree_hook_gaps(project)
    if stale:
        return Check("hooks", FAIL,
                     "this checkout is correct but these worktrees are not: "
                     + ", ".join(stale)
                     + " -- `.claude/settings.json` is tracked, so a detached "
                       "worktree keeps its own stale copy through any merge, "
                       "and the lane launchers run THERE (harmonic-forge#560)")

    guards = len(hooks.get("PreToolUse") or []) if isinstance(hooks, dict) else 0
    detail = f"{len(events)} event(s): {', '.join(events)}; {guards} PreToolUse matcher(s)"
    return Check("hooks", OK, detail)


def check_memory(_project: Project) -> Check:
    """User-scope, so it is the same answer for every project.

    Checked per project anyway: onboarding is not complete without it, and a
    report that omitted it would read as green while the store was unset.
    """
    settings = _settings_path()
    if not settings.is_file():
        return Check("shared memory", FAIL, f"no {settings}")
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Check("shared memory", FAIL, f"unparseable settings.json: {exc}")
    configured = data.get("autoMemoryDirectory")
    if not configured:
        return Check("shared memory", FAIL, "autoMemoryDirectory is not set")
    store = Path(configured).expanduser()
    if not store.is_dir():
        return Check("shared memory", FAIL, f"{store} does not exist")
    if not (store / ".git").exists():
        return Check("shared memory", FAIL, f"{store} is not git-tracked")
    return Check("shared memory", OK, str(store))


def check_prefix(project: Project, manifest: Path | None = None) -> Check:
    """The manifest and `rules/lane-shorthand.md` must claim the same letters.

    A prefix in one and not the other is how `L2B F496` reaches the wrong
    repo — a correct-looking action against the wrong issue.

    Two defects fixed here, both of which made this report a false green:

    1. It called `check_prefix_agreement()` with no argument, so `--manifest`
       was ignored and an alternate manifest was validated against the SHIPPED
       one. A manifest claiming prefix `Q` reported `ok` at exit 0.
    2. It filtered findings to this project's own letter, which silently
       discarded every finding of the form "in lane-shorthand.md but in no
       manifest entry" — those match no project's prefix, so all six projects
       dropped them and `mise run hygiene` saw nothing.
    """
    try:
        findings = check_prefix_agreement(manifest)
    except OSError as exc:
        return Check("prefix", FAIL, f"cannot read lane-shorthand.md: {exc}")
    mine = [f for f in findings if f.startswith(f"{project.prefix}:")]
    if mine:
        return Check("prefix", FAIL, mine[0])
    # A letter claimed by the doc and by no manifest entry belongs to nobody,
    # so it is reported by the FIRST project rather than by none.
    orphans = [f for f in findings
               if not any(f.startswith(f"{p}:") for p in prefixes(manifest))]
    if orphans and project.prefix == min(prefixes(manifest)):
        return Check("prefix", FAIL, "unclaimed in projects.toml — " + "; ".join(orphans))
    return Check("prefix", OK, f"`{project.prefix}` agrees with lane-shorthand.md")


def check_board(project: Project) -> Check:
    if not project.repo:
        return Check("board", SKIP, "projected repo")
    if project.board is None:
        return Check("board", SKIP, "no board by design")
    owner, number = project.board
    return Check("board", OK, f"{owner} #{number}")


CHECKS = (check_protocol, check_lane_tasks, check_gate_adapter, check_checkout, check_worktrees,
          check_directives, check_entrypoint, check_hooks, check_memory, check_board)


def verify(project: Project, manifest: Path | None = None) -> list[Check]:
    checks = [check(project) for check in CHECKS]
    checks.insert(-1, check_prefix(project, manifest))
    return checks


def _run(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, (result.stderr or result.stdout).strip()


def _worktree_is_safe_to_advance(path: Path) -> tuple[bool, str]:
    """Clean, detached, and not holding a live Lane 3 gate.

    Every one of these three is a lesson rather than a precaution:

      * **clean** — a worktree with uncommitted work is somebody's work in
        progress, and `git checkout` carries uncommitted tracked changes ACROSS
        the switch rather than isolating them.
      * **detached** — a worktree on a branch is a branch owner; advancing it
        would move that ref out from under whoever holds it.
      * **no live gate** — hrse#1757. A checkout landing on a worktree mid-gate
        is exactly the incident that issue was filed for, and the Lane 1 session
        that caused it was doing precisely this: advancing lane worktrees after
        a merge. A dead owner is a stale marker and does not block; a LIVE one
        does, absolutely.
    """
    status = _run(["git", "-C", str(path), "status", "--porcelain"])
    if status[0] != 0:
        return False, "could not read status"
    if status[1].strip():
        return False, "uncommitted changes"
    head = _run(["git", "-C", str(path), "symbolic-ref", "-q", "HEAD"])
    if head[0] == 0:
        return False, f"on a branch ({head[1].strip()}), not detached"
    git_dir = _run(["git", "-C", str(path), "rev-parse", "--absolute-git-dir"])
    if git_dir[0] == 0:
        marker = Path(git_dir[1].strip()) / "LANE3_ACTIVE"
        if marker.is_file():
            owner = None
            for line in marker.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("owner_pid="):
                    owner = line.split("=", 1)[1].strip()
            if owner and owner.isdigit() and Path(f"/proc/{owner}").exists():
                return False, f"LANE3_ACTIVE held by live pid {owner}"
    return True, "clean, detached, no live gate"


def advance_stale_worktrees(project: Project,
                            dry_run: bool = False) -> list[Check]:
    """Fast-forward worktrees whose own settings.json is stale.

    harmonic-forge#560 preclose. `.claude/settings.json` is TRACKED, so every
    worktree carries its own copy, and the lane launchers run in the worktrees
    rather than the main checkout. `apply()` creates a missing worktree and
    NOTHING in this platform ever advanced an existing one — so a hook fix
    merged to `main` reached the one checkout nobody launches from, and the
    nine lane worktrees kept the broken matcher indefinitely.

    That is not a #560-specific problem. Any tracked configuration a merge
    changes has the same shape; #560 is just where it became visible.
    """
    done: list[Check] = []
    declared = {path.name: path for path in project.worktrees}
    for name in stale_worktree_hook_gaps(project):
        path = declared[name]
        safe, why = _worktree_is_safe_to_advance(path)
        if not safe:
            done.append(Check(f"worktree {name}", FAIL, f"stale, NOT advanced: {why}"))
            continue
        if dry_run:
            done.append(Check(f"worktree {name}", OK, "would advance to origin/main"))
            continue
        code, err = _run(["git", "-C", str(path), "checkout", "-q", "--detach",
                          "origin/main"])
        done.append(Check(f"worktree {name}", OK if code == 0 else FAIL,
                          "advanced to origin/main" if code == 0 else err))
    return done


def apply_lane_tasks(project: Project, dry_run: bool = False) -> list[Check]:
    """Append the generated lane-task block to a repo that lacks the tasks.

    Idempotent in the way that matters: the *generated block marker* is not the
    test -- a repo's own already-present task is. A repo that hand-wrote its
    `gate-checkout` before this existed (cymagraph-infra did) gets only the
    tasks it is actually missing, and a second run adds nothing because by then
    nothing is missing. Keying off the marker alone would re-append the whole
    block to any repo that had removed it, and keying off nothing would append
    on every run.
    """
    if project.protocol is None or project.checkout is None:
        return []
    mise = project.checkout / "mise.toml"
    if not mise.is_file():
        return [Check("lane tasks", FAIL, f"no mise.toml in {project.checkout}")]

    text = mise.read_text(encoding="utf-8")
    present = lane_tasks.task_names(text)
    declared = {
        "l1-post": project.protocol.l1_post_task,
        "lane-comment": project.protocol.lane_comment_task,
        "gate-checkout": project.protocol.gate_checkout_task,
        "lane3-begin": project.protocol.lane3_begin_task,
        "lane3-end": project.protocol.lane3_end_task,
    }
    missing = {canon: name for canon, name in declared.items() if name not in present}
    if not missing:
        return [Check("lane tasks", OK, "already present")]
    if dry_run:
        return [Check("lane tasks", OK,
                      f"would add {', '.join(sorted(missing.values()))} to {mise}")]

    block = lane_tasks.render_subset(missing)
    mise.write_text(text.rstrip("\n") + "\n\n" + block, encoding="utf-8")
    return [Check("lane tasks", OK, f"added {', '.join(sorted(missing.values()))}")]


def apply(project: Project, dry_run: bool = False) -> list[Check]:
    """Create what is missing. Idempotent: a second run changes nothing."""
    done: list[Check] = []
    if project.checkout is None or not (project.checkout / ".git").exists():
        return [Check("apply", SKIP, "no checkout to act on")]

    for path in project.worktrees:
        if path.exists():
            done.append(Check(f"worktree {path.name}", OK, "already present"))
            continue
        if dry_run:
            done.append(Check(f"worktree {path.name}", OK, f"would create {path}"))
            continue
        # Detached, from `origin/main`: a lane worktree is a session's working
        # directory, not a branch owner. Attaching a branch here is what makes
        # two lanes fight over one ref.
        code, err = _run(["git", "-C", str(project.checkout), "worktree", "add",
                          "--detach", str(path), "origin/main"])
        done.append(Check(f"worktree {path.name}", OK if code == 0 else FAIL,
                          "created" if code == 0 else err))

    # A worktree that EXISTS but carries stale tracked config is invisible to
    # the loop above, which only ever creates missing ones.
    done.extend(advance_stale_worktrees(project, dry_run=dry_run))

    done.extend(apply_lane_tasks(project, dry_run=dry_run))

    source = platform_source()
    if source.resolve() != _THIS_CHECKOUT.resolve():
        done.append(Check("directive source", OK, f"linking from {source}"))
    sync = source / "sync_rules.py"
    if dry_run:
        done.append(Check("directives", OK, f"would run sync_rules.py --project {project.checkout}"))
    else:
        code, err = _run([sys.executable, str(sync), "--project", str(project.checkout)])
        done.append(Check("directives", OK if code == 0 else FAIL,
                          "synced" if code == 0 else err))
    return done


def report(projects: list[Project], mode: str, dry_run: bool,
           manifest: Path | None = None) -> tuple[str, int]:
    lines: list[str] = []
    failed = 0
    for project in projects:
        checks = apply(project, dry_run) if mode == "apply" else verify(project, manifest)
        if mode == "apply":
            checks += verify(project, manifest)
        bad = sum(1 for c in checks if c.status == FAIL)
        failed += bad
        head = f"{project.name}" + (f"  ({project.repo})" if project.repo else "  (projected)")
        lines.append(f"\n== {head} — {'FAIL' if bad else 'green'}")
        lines += [f"  {c.line()}" for c in checks]
    lines.append(f"\n{len(projects)} project(s), {failed} failing check(s).")
    return "\n".join(lines), (1 if failed else 0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Onboard a repo to the 3-lane protocol.")
    p.add_argument("--project", help="Manifest `name` to act on (default: all).")
    p.add_argument("--apply", action="store_true",
                   help="Create what is missing. Without it the run only verifies.")
    p.add_argument("--dry-run", action="store_true",
                   help="With --apply: print what would change, change nothing.")
    p.add_argument("--manifest", type=Path, default=None)
    args = p.parse_args(argv)

    try:
        projects = load(args.manifest)
    except ManifestError as exc:
        print(f"forge-onboard: {exc}", file=sys.stderr)
        return 2

    if args.project:
        projects = [p for p in projects if p.name == args.project]
        if not projects:
            print(f"forge-onboard: no manifest entry named {args.project!r}",
                  file=sys.stderr)
            return 2

    text, code = report(projects, "apply" if args.apply else "verify", args.dry_run,
                        args.manifest)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
