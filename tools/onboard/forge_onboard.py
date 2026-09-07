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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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


def check_worktrees(project: Project) -> Check:
    if project.checkout is None:
        return Check("lane worktrees", SKIP, "no checkout")
    missing = [p for p in project.worktrees if not p.exists()]
    if missing:
        return Check("lane worktrees", FAIL,
                     "missing: " + ", ".join(p.name for p in missing))
    return Check("lane worktrees", OK, "lane2 + lane3 present")


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


def check_hooks(project: Project) -> Check:
    """Presence and shape only — reconciliation waits on harmonic-forge#324."""
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
    return Check("hooks", OK, f"{len(events)} event(s): {', '.join(events)}")


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


CHECKS = (check_checkout, check_worktrees, check_directives, check_entrypoint,
          check_hooks, check_memory, check_board)


def verify(project: Project, manifest: Path | None = None) -> list[Check]:
    checks = [check(project) for check in CHECKS]
    checks.insert(-1, check_prefix(project, manifest))
    return checks


def _run(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, (result.stderr or result.stdout).strip()


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
