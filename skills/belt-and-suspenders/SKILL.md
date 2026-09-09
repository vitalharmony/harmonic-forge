---
name: belt-and-suspenders
description: Arm a lane's proactive work-discovery protocol — a persistent Monitor (the belt) plus a self-paced loop (the suspenders), parameterized by the LANE env var. Use when a lane session should spend idle time advancing work instead of waiting. Do NOT use in a session with no LANE set.
---

# Belt and suspenders

Two independent ways to discover work, so a handoff cannot be missed because one
of them failed. `LANE` selects the role; everything else here is shared.

The mechanics live in `tools/gh/belt_mechanics.py`, not in this file. That is
deliberate: a mechanic described in prose gets re-derived by each role at read
time — three implementations wearing one description, which is the defect this
skill exists to eliminate.

## Refuse before arming anything

**No lane means no protocols, neither one.** If `LANE` is unset, unrecognized,
or disagrees with the worktree this is running in: report which lane was
detected and which was expected, and **stop**. Do not fall back to Lane 1, do
not offer a read-only variant, do not ask.

The hazard is specific: a no-lane session — the kind that files issues and
writes handoffs — arming Lane 1's belt and beginning to move work it has no
authority to move.

Then assert the account identity before the first poll:

```python
from belt_mechanics import assert_identity
assert_identity(account)          # raises IdentityMismatch, loudly
```

A slot authenticating as someone else must refuse rather than return empty.
**Empty and wrong-credentials are indistinguishable to a caller, and silence is
the one signal this protocol cannot interpret.** Live proof at the time of
writing: the `harmonicarchitect` slot authenticates as `vitalharmony`.

## The belt — a push monitor

Poll every 5 minutes. Derive accounts from `gh-as --list` and repos from
`list_repos(account)` at runtime, never hardcoded (R-0122): adding an account
costs `gh-as --init <account>` and nothing else, and an archived repo drops out
on its own. `conscious-architect-core` is archived and absent from every
hardcoded list today **by luck**.

**Dedup is one mechanic with three parts, and each covers the others' failure:**

```
watermark   per repo, advancing ONLY when that repo's own call succeeded
overlap     query from min(watermark, now - K) — covers the seam
seen-set    id<TAB>emitted|primed, primed before the first poll
```

Two of the three lanes lost real work in one session by using only part of this.
Lane 1's bare window lost hrse#1725's test spec. Lane 3's bare watermark lost
four issues twice. Do not simplify it back.

**Verify the filter against a real comment body before trusting it** — every
time it is rewritten, not just at first setup:

```
gh-as <account> gh api repos/OWNER/REPO/issues/comments/ID --jq '.body' \
  | jq -Rs --arg re 'PATTERN' 'test($re; "im")'
```

must print `true`. Lane 1 built a filter from the *prompt's own shorthand*
(`L3S`, `L3P`) instead of from the bodies Lane 3 actually posts (`## Lane 3 Test
Spec`, never abbreviated), and coverage narrowed silently. **Silence is not
proof that a filter works.**

**When replacing a monitor**, stop the old one by its actual task id, then carry
forward every match condition it had unless a drop is stated explicitly.
"Replace" is not "write a new one from the description."

**An empty `TaskList` is not proof the monitor is dead.** Lane 1 armed a
duplicate because `TaskList` said "No tasks found" while an older, more capable
monitor had been running the whole session and later fired from a task id Lane 1
had never seen. Confirm through a real signal before re-arming.

## The suspenders — a pull loop

`/loop 10m proactively find work to do`. The wakeup delay must match the cadence
set here literally — not a generic idle-tick default.

Each tick runs three checks. **Checks never gate each other**: a cheap
idempotent check runs every tick regardless of what any other check found. Lane
3 lost the same four issues twice — once by reading "not execute-ready" as
"nothing owed," then again by letting that check decide whether the other ran.

1. **What changed** since the last tick.
2. **What do I owe** — repo-wide, ignoring recency.
3. **What have I never answered** — count *my own* posted markers per issue.

**Check 3 is the only one orthogonal to the other two.** Checks 1 and 2 both
read other lanes' output, so they share a predicate; neither asks whether my own
conclusion reached the issue. hrse#1715 carried a live handoff, was assigned by
the operator, was analysed to a correct conclusion — and nothing was ever
posted. Two hours, a one-line thread, while the lane carried it forward as done.

## Standing rules, all roles

- **EOQ.** Any trigger queues behind work in flight and never interrupts it
  (R-0116). Finish the current task to its normal finish line first.
- **Quiet ticks produce no chat output** — but **always write a tick record**
  (`TickLog`), including on a quiet tick. A quiet tick that writes nothing is
  indistinguishable from a dead monitor.
- **Anything on a timer is REST-only** (R-0019). A one-off GraphQL call is a
  rounding error; the same call every ten minutes is a quota leak that surfaces
  in another lane's session as a confusing failure. The counter in
  `belt_mechanics` enforces this continuously — a non-zero `calls_graphql` from
  a scheduled path is a defect, not a statistic.
- **Name the question a tool actually answers** before treating its result as
  dispositive. Every one of Lane 3's expensive misses was a correctly-functioning
  signal answering an adjacent question.
- **Re-fetch before naming any next action.** Lane 2 told the operator to push
  four branches already on the remote at its own SHAs. In a protocol whose
  premise is concurrent lanes, an hour-old fact is not a current fact.
- **Staleness presents as a healthy checkout.** Before quoting source as
  current: `git rev-list --count HEAD..origin/main`. A worktree 114 commits
  behind has a clean `git status`, no divergence, and nothing to rebase.
- **Lead with what the operator must do.** If that is nothing, say so and stop.

## Role: Lane 1

**Pull source: a repo-wide newest-marker sweep.** Lane 1 has no single worktree
to self-discover from, so a manually maintained list is the failure mode.

> For each open issue, find the newest comment carrying a lane-post marker. If
> its `posted-by` is **not** Lane 1, the ball is with Lane 1. If the newest
> marker is Lane 1's own, it has already acted.

Newest wins, full stop — no precedence table, no exclusion list. "Already acted"
needs no state because acting *is* posting, which makes Lane 1's marker newest.

**This rule is only total if Lane 1 posts a `kind=discussion` when it decides an
issue needs nothing.** Otherwise that issue is re-offered every tick, and the
`AskUserQuestion` becomes a nag.

**After a Lane 3 PASS that Lane 1 has independently re-verified live: merge and
close directly.** Do not present it as a decision needing approval; narrate it
after the fact. One carve-out — an issue labeled `data-migration` cannot close
without the migration actually running or an explicit `migration-abandoned`
decision, and `block_data_migration_close.py` enforces that regardless of PASS.

**An agreed decision is not "pending."** It is completed-but-unexecuted, and it
does not wait behind a newly-opened item. "1 agreed" sat unexecuted for multiple
turns while item 2 was investigated.

Parameters: overlap `K` to be set from the first week's tick log · no session
lock (Lane 1's belt does not mutate a shared worktree).

## Role: Lane 2

Fires on `handoff`, `rework`, `discussion`. Silent on `ready-for-l3`, `ae`,
`sweep`, `ae-and-sweep`, `spec`, `gate-result` — that is the Lane 1 ↔ Lane 3
channel.

**A `plan-first=true` handoff must render as PLAN-FIRST in the event line.**
Implementing one is a protocol violation and the event line is the last place to
catch it.

**Run diagnostics from a freshly provisioned per-issue worktree off
`origin/main`**, never the long-lived lane worktree.

**When an issue is deleted or dropped, purge its open items explicitly**, and
quote issue *numbers* rather than topics when carrying an item across. A deleted
plan contaminated a comment on a different issue fifteen minutes later — adjacent
numbers, similar titles.

Lane 2 does not push, open PRs, merge, close, file issues, or execute write-tier
paths (R-0157, R-0350).

Parameters: `K` = 90 minutes (measured, no observed failures) · no session lock.

## Role: Lane 3

**Two-stage readiness, both stages every tick, unconditionally:**

- **Check A — spec owed.** Newest `kind=ready-for-l3` with no spec of mine after
  it on the thread means a spec is owed *now*. Do not wait for a sweep or an AE;
  neither exists until the spec does.
- **Check B — execute ready.** `mise run gate-checkout <branch>` **first**, never
  assumed still-current from a prior tick.

B runs regardless of what A found: a spec posted on an earlier tick can have its
AE and sweep land on any later tick, and only B sees that.

**Lane 3 takes the session lock** — its belt and loop both run `gate-checkout` in
one shared worktree, so two ticks landing together corrupt a checkout. One lock
per session, not per account: work for two accounts still runs in one worktree.

**Flag an AE carried forward across a sticky-wicket reforge** (R-0209). A routine
fix-and-repush after a FAIL is the legitimate carry-forward and needs no flag.

**Lane 3 can only write to `~/Harmonic_Projects/testplan/`.** Anything this skill
tells Lane 3 to write elsewhere lands there silently — which is what happened to
its own prompt dump. The tick log path is a parameter for exactly this reason.

Parameters: `K` to be set from the first week's tick log · session lock at
`~/Harmonic_Projects/testplan/poll.lock` · tick log under the same directory.

## Report when armed

Name the monitor's task id, the loop's job id, the account(s) and repos derived,
and the tick log path. Then stop.
