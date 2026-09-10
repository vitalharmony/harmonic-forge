#!/usr/bin/env python3
"""SessionStart notice for a red `main` (harmonic-forge#566, follow-up to
#504's own AC6 question — "does a green PR check guarantee a green main
after merge?" -- answered no, and deliberately scoped out of #504).

## Why `main` can go red after a green PR

Two ordinary mechanisms, neither exotic:

1. **The base moves.** A PR's checks run against a merge preview -- head
   merged into base at that moment. Two branches individually green can be
   red together.
2. **A squash merge produces a commit no CI run has ever seen** -- every
   repo here squash-merges with `delete_branch_on_merge: true`, so the
   commit that lands on `main` is not the commit any PR check ran against.

The incident that filed this issue, 2026-09-07:

| Time | Event |
|---|---|
| 22:34 | last green `main` -- `1dd4d8b` |
| 00:12 | Lane 3 Gate Results -- harmonic-forge#494 -- PASS |
| 00:13 | `main` CI on the merge commit `0359854`: **failure** |
| 03:19 | `main` CI on `3be6c75`: **failure**, the same 3 tests |

Three hours, two merges, nobody noticed. #504 covers the 00:12 line (a PASS
may not outrun the PR's own CI). Nothing covered the 00:13 line until this
file -- `main`'s own CI going red with no one watching.

## Scope: notice and report, never gate/revert/auto-fix (AC4)

A merge cannot be blocked on a signal that only exists after it lands. This
module measures and reports; it does not touch `main`, does not revert, and
does not attempt a fix. That boundary is #504's own, held here too.

## Delivery mechanism (AC1): `SessionStart`

Chosen over a standalone daemon/cron because this platform has neither
today, and the deliverable is "reaches the session most likely to be able to
act" -- which IS the next session someone opens, not a notification queued
for nobody. `SessionStart` fires on every `startup`/`resume`/`clear`
(deliberately not `compact`, which is mid-session continuity, not a fresh
look at the world) and is already the established wake-up point for a
comparable cross-cutting notice (`belt_wakeup.py`). The tradeoff, stated
plainly: a red `main` between two session starts is not surfaced until the
next one opens -- acceptable because the alternative (a background poller)
does not exist in this platform yet, and the honest fallback for the gap
between sessions is unchanged from before this issue: the next PR against
that base still gets its own CI signal.

## Reuses `gate_ci.py`'s existing CI-health primitives (AC6)

`ci_conclusion()` and `required_checks()` are the same functions #504's gate
already uses -- no second reading of "is CI green" is written here. This
module is a second, later call site into the same primitives, not a second
implementation of what they compute.

## Dedup (AC3): one notification per distinct (repo, sha, state)

A state file keyed by repo records the last (sha, state) reported. An
unchanged red stays quiet; a NEW red -- a different sha, or a repo that had
gone green in between -- reports again. Best-effort: a corrupt or unreadable
state file is treated as empty (report once more, worse than silence) rather
than raising into a `SessionStart` hook, where an exception here must never
cost the session its start.

## Fails silent on an unreachable API (AC5)

`ci_conclusion()`'s own `"unknown"` state already covers "could not read
checks" -- this module reports only on `"red"`, so an unreachable GitHub
(which surfaces as `"unknown"`, never `"red"`) produces no report, matching
AC5's "an unavailable GitHub is not a red main" requirement exactly, with no
extra logic needed here to enforce it.

## An unprotected branch does not fall back to "every check is required"
(preclose-inspection finding)

`gate_ci.required_checks()` returns `None` when a branch has no protection
rule at all -- true today for 3 of the 4 `REPO_PREFIXES` repos (verified
live: only `vitalharmony/hrse` has one). `gate_ci`'s own docstring calls the
unscoped-scan fallback "the stricter direction," which is correct for a GATE
(refusing a PASS on ambiguous input is safe) and wrong for a REPORTER: an
unscoped scan grades every check on the commit, including ones nobody
intended to gate on. Live-reproduced: `vitalharmony/cymagraph-infra`'s
current `main` tip carries only a failed `Dependabot` run -- unscoped, that
reads as a red `main`; the repo's actual code CI never even ran on that
commit.

So this module never leaves `required` unscoped: `DEFAULT_REQUIRED_CHECKS =
{"verify"}` is used whenever `required_checks()` returns `None`. Empirically
the house's one canonical CI job name -- confirmed live present on `main`'s
recent history in `vitalharmony/hrse` (the one repo with real branch
protection, whose contexts list is exactly `["verify"]`) and
`vitalharmony/harmonic-forge`. Where a repo's current tip never ran it at
all (`openclaw-projects` has zero check runs on `main`; a Dependabot-only
commit on `cymagraph-infra` never runs it either), `ci_conclusion()` reports
`absent`, not `red` -- exactly the "not asked yet" distinction its own
docstring insists on, and not a false alarm either way.

## Reds recorded, not overwritten, by a non-terminal reading (preclose-inspection finding)

The dedup state is written for a repo only when this run resolves a
`"red"` or `"green"` reading -- never on `"pending"`/`"absent"`/`"unknown"`.
A transient API hiccup between two session starts (a rate limit, a
re-dispatching workflow mid-run) must not erase the memory that this exact
red was already reported: without this guard, the next `unknown`/`pending`
reading would overwrite the stored `"red"` entry, and the following run
would read the still-red commit as new and report it again -- exactly the
"a still-red main is not news twice" property AC3 names.

## Registered only in harmonic-forge's own checkout -- a disclosed gap, not
a silent one (preclose-inspection finding)

This hook is wired into harmonic-forge's own `.claude/settings.json` only.
A session started in a consuming repo -- HRSE2 above all, where most of
this platform's real work happens -- never runs it today. The same R-0353
propagation gap harmonic-forge#565 named for its own hook applies here:
adding the identical `SessionStart` line to `~/Harmonic_Projects/HRSE2/
.claude/settings.json` (matching `belt_wakeup.py`'s existing
`startup|resume|clear|fork` matcher there) is real follow-up work, not done
by this change -- HRSE2's own lane-protocol forbids editing its tracked
files from outside a Lane 2 worktree/PR, which this issue's Tooling
Exception scope (harmonic-forge only) does not authorize reaching into.

## One caveat, disclosed rather than silently accepted: single-session delivery

Dedup is keyed on `(repo, sha, state)`, not on which session receives the
notice. The first session to start after a new red is the one that sees it;
a second session started moments later reads the same, now-recorded, state
and stays silent. This is a real property of a stateless `SessionStart`
notice, not a bug to be fixed here -- a "mark read per session" design would
need per-session identity this hook has no reason to track otherwise, and
AC1 asks for "the session most likely to be able to act," not every session.
Named here so the tradeoff is a decision, not an unstated assumption.

## Repo list: the same one `batch_auth.py` already maintains

`REPO_PREFIXES` names every repo "under the platform's management" per its
own docstring (credential isolation, not merely a shorthand table) -- reused
here rather than a second, driftable list of the same four repos.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_auth import REPO_PREFIXES  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
import gate_ci  # noqa: E402

STATE_PATH = Path.home() / ".claude" / "state" / "main-ci-status.json"

#: Used whenever `gate_ci.required_checks()` returns `None` (no branch
#: protection configured) -- see the module docstring's "unprotected branch"
#: section for why an unscoped scan is the wrong fallback for a reporter.
DEFAULT_REQUIRED_CHECKS = {"verify"}


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, result.stdout if result.returncode == 0 else result.stderr


def _main_sha(repo: str, run=None) -> str | None:
    run = run or _run
    code, out = run(["gh", "api", f"repos/{repo}/commits/main", "--jq", ".sha"])
    if code != 0:
        return None
    sha = out.strip()
    return sha or None


def _load_state(path: Path | None = None) -> dict:
    path = path or STATE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict, path: Path | None = None) -> None:
    """Best-effort. A dedup write that fails must not cost the report --
    worse case is re-reporting an already-seen red, never a lost one."""
    path = path or STATE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def check_repo(repo: str, run=None, sha=None) -> tuple[str, str] | None:
    """`(sha, detail)` if this repo's `main` is newly red, else None.

    `sha`/`run` injected for tests -- no test may reach `gh api`.
    """
    run = run or _run
    head = sha if sha is not None else _main_sha(repo, run=run)
    if head is None:
        return None
    required = gate_ci.required_checks(repo, run=run) or DEFAULT_REQUIRED_CHECKS
    state, detail = gate_ci.ci_conclusion(repo, head, run=run, required=required)
    if state != "red":
        return None
    return head, detail


def check_all(repos: dict[str, str] | None = None, run=None,
              state_path: Path | None = None) -> list[dict]:
    """Every repo whose `main` is NEWLY red (AC1/AC3), as a list of
    `{"repo", "sha", "detail"}`. Updates the dedup state file as it goes so a
    partial failure among repos does not re-derive already-reported ones."""
    repos = repos if repos is not None else REPO_PREFIXES
    seen = _load_state(state_path)
    reports: list[dict] = []
    changed = False
    for repo in repos:
        try:
            head = _main_sha(repo, run=run)
        except Exception:
            continue
        if head is None:
            continue
        try:
            required = gate_ci.required_checks(repo, run=run) or DEFAULT_REQUIRED_CHECKS
            state, detail = gate_ci.ci_conclusion(repo, head, run=run, required=required)
        except Exception:
            continue
        prior = seen.get(repo) or {}
        if state == "red":
            if prior.get("sha") == head and prior.get("state") == "red":
                continue  # already reported this exact (repo, sha) (AC3)
            reports.append({"repo": repo, "sha": head, "detail": detail})
        # Only a red or green reading is recorded. A transient "pending" /
        # "absent" / "unknown" reading must never overwrite a stored "red" --
        # doing so would erase the memory that this exact commit was already
        # reported, and the next call would read the still-red commit as new
        # (preclose-inspection finding; AC3: "a still-red main is not news
        # twice").
        if state in ("red", "green"):
            seen[repo] = {"sha": head, "state": state}
            changed = True
    if changed:
        _save_state(seen, state_path)
    return reports


def _format(reports: list[dict]) -> str:
    lines = [
        "[MAIN-CI] `main` is red in the following repo(s) -- notice only, "
        "nothing here gates, reverts, or fixes it (harmonic-forge#566):",
    ]
    for report in reports:
        lines.append(
            f"  - {report['repo']} @ `{report['sha'][:8]}`: {report['detail']}"
        )
    lines.append(
        "This is the FIRST report for each commit above; an unchanged red "
        "will not repeat until it either changes or clears."
    )
    return "\n".join(lines)


def handle(state_path: Path | None = None) -> dict:
    try:
        reports = check_all(state_path=state_path)
    except Exception:
        # AC5's spirit extended to the whole handler, not just ci_conclusion's
        # own unknown/red split: a SessionStart hook must never fail a
        # session start over a reporting feature.
        return {}
    if not reports:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _format(reports),
        }
    }


def main() -> int:
    try:
        json.load(sys.stdin)  # payload unused; this check is repo-scoped, not session-scoped
    except (json.JSONDecodeError, ValueError):
        pass
    out = handle()
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
