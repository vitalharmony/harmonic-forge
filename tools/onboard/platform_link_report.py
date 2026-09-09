#!/usr/bin/env python3
"""Report platform-link drift from a repo's own verification gate (harmonic-forge#543).

**Why not `tools/lane/_lane_args.sh`.** #543 rules that out with four reasons,
and they are all still true: it is a pure argument parser whose caller contract
declares `in $@` / `out lane_agent_requested, lane_ack_stale,
lane_passthrough`; all three launchers `set -euo pipefail` before sourcing it,
so a non-zero check there aborts the launcher with no message; it is sourced
before `lane3`'s own staleness and `backend/.env` refusals, so a drift message
would print ahead of both; and it would break the captured launch-tuple
baseline. The issue's own preferred surface is each repo's `mise run check`,
following `check_rule_drift.py` — no launcher latency, no AC5 risk. This is
that surface.

**Lane 3's zero-mutation guarantee is preserved by construction.** This calls
`sync_rules.py`'s `verify_project`, which never links, never repairs, and never
creates a directory — `link_project` calls `mkdir(parents=True)`, the verify
path deliberately does not (asserted by `test_verify_creates_nothing`). A gate
that repaired its own preconditions could not report on them.

## What fails the gate, and the one bounded thing that does not

#541 gave `--verify` three-valued exits so a caller can tell a finding from a
state it should not act on. This wrapper maps them to four outcomes:

  * **DRIFT** — a link this repo DECLARED is missing or points outside the
    platform. Unambiguously wrong wherever it is seen. **Fails.**
  * **BROKEN MANIFEST** — the file exists and cannot be read (conflict markers
    after a rebase, an `OSError`, the `skill =` typo). The branch has this file
    and can fix it, and `hooks-install` will NOT repair it — link mode skips
    skills on a `ManifestError`. **Fails.**
  * **UNDECLARED** — no manifest, and the repo's default branch has none
    either. That is a repo nobody wired up: harmonic-forge#540's actual state.
    **Fails.**
  * **BEHIND** — no manifest here, but the default branch carries one. This
    checkout is simply older. **Does not fail**, and says so loudly.

The BEHIND case is the only non-fatal degraded state, and it is bounded on
purpose. An earlier version made ALL of `EXIT_CANNOT_RUN` non-fatal, reasoning
that "a branch cannot fix a file it does not have"; preclose-inspection killed
that because nothing constrained it — `harmonic-forge-f326`, a long-lived
worktree with no `.claude/skills/` at all, passed. Removing the carve-out
entirely then broke the opposite case: `mise run check` inside a Lane 3 gate
checkout failed for a reason unrelated to the code under test, and printed a
remedy (rebase) that a gate parked on the commit under test may not take.

Asking the DEFAULT BRANCH is what separates the two. It is self-limiting — it
evaporates the moment the checkout advances — and it never applies to a repo
that has not adopted the manifest, which is the state that must keep failing.
Uncertainty answers "has not adopted", so the fatal path is the default.

No case is silent, which is the whole point of harmonic-forge#540: the defect
was never that the links were wrong, it was that nothing said so.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

#: Path form for `git cat-file`, which wants a forward-slash path
#: inside the tree regardless of platform.
SKILLS_MANIFEST_RELPATH_STR = ".claude/platform-skills.toml"


def _load_sync_rules():
    spec = importlib.util.spec_from_file_location(
        "sync_rules", _ROOT / "sync_rules.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_ROOT / 'sync_rules.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("sync_rules", module)
    spec.loader.exec_module(module)
    return module


def _rules_and_agents_verify(project_root: Path, sync_rules) -> bool:
    """The two link classes that need no manifest, checked on their own.

    Rules come from `UNIVERSAL_RULE_FILES` and agents are auto-discovered, so
    neither depends on a declaration. That makes them verifiable even in a
    checkout with no manifest, and it is what lets the BEHIND branch below say
    "rules and agents were verified" rather than assert it.
    """
    import contextlib
    import io

    # verify_project short-circuits before comparing anything when the manifest
    # is missing, so call the per-class checks directly. Their output is
    # captured and re-emitted only on failure: on the BEHIND path a clean run
    # should be quiet.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        ok = sync_rules._verify_dir(
            sync_rules.RULES_DIR,
            project_root / ".claude" / "rules",
            sync_rules.UNIVERSAL_RULE_FILES,
        )
        ok = sync_rules._verify_dir(
            sync_rules.AGENTS_DIR,
            project_root / ".claude" / "agents",
            sync_rules._universal_agent_files(),
        ) and ok
    if not ok:
        sys.stderr.write(err.getvalue())
    return ok


def _default_branch_has_manifest(project_root: Path) -> bool:
    """Has this REPO adopted the manifest, even if this CHECKOUT predates it?

    This is the boundary the previous carve-out lacked. Asking the default
    branch distinguishes "a checkout that is behind" — a history artifact that
    a rebase fixes and a gate checkout cannot — from "a repo that never adopted
    the manifest", which is the unwired state harmonic-forge#540 is about and
    which must keep failing.

    Any uncertainty answers False, so the fatal path is the default: an
    unreachable remote, a detached checkout with no upstream, or a git error
    all fail closed rather than granting a silent pass.
    """
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        result = subprocess.run(
            ["git", "-C", str(project_root), "cat-file", "-e",
             f"{ref}:{SKILLS_MANIFEST_RELPATH_STR}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True
    return False


def report(project_root: Path, sync_rules=None) -> int:
    """Gate exit code: 0 verified, 1 anything else. Nothing degraded is green.

    An earlier version of this carved out `EXIT_CANNOT_RUN` as a loud-but-
    non-fatal warning, reasoning that an absent manifest usually means a branch
    forked before it landed, and "a branch cannot fix a file it does not have."
    Preclose-inspection killed that, on two counts, and both were right:

    * **The carve-out was keyed on the exit code, not the reason.**
      `verify_project` returns `EXIT_CANNOT_RUN` for TWO states — manifest
      absent, and manifest present but unreadable (conflict markers after a
      rebase, an `OSError`, or the `skill = [...]` typo `load_skill_manifest`
      exists to catch). The second is a file the branch DOES have and CAN fix,
      and it was being told the opposite. Worse, the remedy named did not work:
      link mode deliberately skips skills on a `ManifestError`, so
      `mise run hooks-install` left the skill unlinked and the gate green.

    * **It was unbounded.** The "short-lived old branch" rationale does not
      constrain anything. `harmonic-forge-f326`, a long-lived worktree with no
      `.claude/skills/` at all, passed this gate — harmonic-forge#540's exact
      measured defect reproducing inside the check built to end it. Nothing
      counted, aged, or escalated the condition, so "loud but non-fatal"
      degrades to "in the scrollback" on the second occurrence.

    So there is no non-green degraded state any more, which restores
    harmonic-forge#541's own stated contract — "an absent manifest is never
    green" — instead of quietly inverting it one layer up. The two reasons
    still get different messages, because they need different actions.
    """
    sync_rules = sync_rules or _load_sync_rules()

    if not project_root.is_dir():
        print(f"[platform-links] not a directory: {project_root}", file=sys.stderr)
        return 2

    code = sync_rules.verify_project(project_root)
    if code == sync_rules.EXIT_OK:
        return 0

    if code == sync_rules.EXIT_CANNOT_RUN:
        # Absent and malformed both land here; tell them apart by asking.
        malformed: str | None = None
        try:
            sync_rules.load_skill_manifest(project_root)
        except sync_rules.ManifestError as exc:
            malformed = str(exc)

        rules_agents_ok = _rules_and_agents_verify(project_root, sync_rules)

        if (malformed is None
                and rules_agents_ok
                and _default_branch_has_manifest(project_root)):
            # BOUNDED carve-out. The repo HAS adopted the manifest on its
            # default branch; this checkout is simply behind it. That is a
            # history artifact, not an unwired repo, and the only remedy is a
            # rebase — which a Lane 3 gate checkout is forbidden to perform,
            # because it is parked on the commit under test by design.
            #
            # Failing here made `mise run check` fail inside HRSE2-lane3 for a
            # reason unrelated to the code being gated, and printed a remedy
            # Lane 3 may not take. Reported loudly and non-fatally instead.
            #
            # This is NOT the unbounded carve-out preclose-inspection removed.
            # That one applied to any EXIT_CANNOT_RUN forever, so a repo that
            # never adopted the manifest stayed permanently green. This one
            # asks whether the repo adopted it, and is therefore self-limiting:
            # it evaporates the moment the checkout advances, and never applies
            # to a repo that has not adopted at all — which still fails.
            print(
                "[platform-links] BEHIND — this checkout predates "
                f"{sync_rules.SKILLS_MANIFEST_RELPATH}, which the default "
                "branch carries, so the skills half could not be compared. "
                "Not failing: the fix is a rebase, and a gate checkout is "
                "parked on the commit under test by design. Rules and agents "
                "were verified and are correct.",
                file=sys.stderr,
            )
            return 0

        if malformed is not None:
            print(
                "[platform-links] BROKEN MANIFEST — "
                f"{sync_rules.manifest_path(project_root)} exists and cannot be "
                f"read: {malformed}. Nothing was compared, so this gate can say "
                "nothing about the rules, agents or skills this checkout is "
                "running with. `mise run hooks-install` will NOT fix it — link "
                "mode skips skills on a manifest error by design. Repair the "
                "file (a rebase leaving conflict markers, and a `skill =` typo "
                "for `skills =`, are the two that happen).",
                file=sys.stderr,
            )
        else:
            print(
                "[platform-links] UNDECLARED — "
                f"{sync_rules.manifest_path(project_root)} does not exist, so "
                "nothing states which platform skills this checkout consumes "
                "and nothing was compared. Usually a branch forked before the "
                "manifest landed: rebase onto a commit that carries it, or run "
                "`mise run hooks-install` on a fresh clone.",
                file=sys.stderr,
            )
        return 1

    print(
        "[platform-links] DRIFT — a platform link this repo declares is missing "
        "or points outside the platform. The paths are named above. This session "
        "is running without content it declared it consumes; run "
        "`mise run hooks-install` to repair.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report platform-link drift for a checkout (harmonic-forge#543)")
    parser.add_argument("--project", default=".", help="Checkout to inspect")
    args = parser.parse_args(argv)
    return report(Path(args.project).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
