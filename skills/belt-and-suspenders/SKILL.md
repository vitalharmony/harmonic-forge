---
name: belt-and-suspenders
description: Arm a lane's proactive work-discovery protocol — a persistent Monitor (the belt) plus a self-paced loop (the suspenders), parameterized by the LANE env var. Use when a lane session should spend idle time advancing work instead of waiting. Do NOT use in a session with no LANE set.
---

# Belt and suspenders

Two independent ways to discover work, so a handoff cannot be missed because one
of them failed. `LANE` selects the role; everything else here is shared.

The mechanics live in `~/harmonic-forge/tools/gh/belt_mechanics.py`, not in
this file. That is deliberate: a mechanic described in prose gets re-derived
by each role at read time — three implementations wearing one description,
which is the defect this skill exists to eliminate. This file itself lives at
`harmonic-forge/skills/belt-and-suspenders/SKILL.md`, two directories below
the repo root, so a bare relative `tools/gh` path reads as skill-relative here
and resolves to nothing — every path below is written absolute, rooted at
`~/harmonic-forge/`, for exactly that reason (harmonic-forge#570).

## The belt is `watch_lane_posts.py` — copy the command, don't rebuild it

**`watch_lane_posts.py` already is the belt.** It re-derives `(repo, issue)`
from a worktree's live branch every cycle, has a `--queue-for` mode for a lane
with no issue in hand, and reports what it resolved and did not (never
silently — see the last bullet below). A fresh session that reads
`belt_mechanics.py`, understands it, and then hand-writes a fourth poller has
not failed to understand the mechanics — it failed to be told that reading is
the whole job. That happened three times in one session, once per lane
(harmonic-forge#570): a session that wrote its own belt has re-derived
something this skill already names, and **that re-derivation is the defect**,
the same one `belt_mechanics.py`'s existence already eliminated one level
down.

**Arming is one mechanism per half, never four.** The belt is a `Monitor` on
the command below; the suspenders are `/loop` (further down). `CronCreate` and
a hand-written poller script are neither — arming either of them alongside a
`Monitor` is not extra safety, it is an unrequested second live protocol with
its own bugs to debug later.

**The literal command, per lane** — a lane between issues sits on a detached
HEAD with no issue number as its *normal* resting state (confirmed live,
cross-lane evidence on harmonic-forge#570), so a command that only works
mid-issue is not the command to arm:

- **Lane 1** — **worktrees first.** Lane 1 has no worktree *on an issue branch*,
  but it can see every worktree there is, and that set is what is actually in
  flight. Enumerate them rather than listing them: `/tmp/<repo>-<issue>-impl`
  checkouts appear and vanish per issue, so a hardcoded list narrows the belt
  silently (harmonic-forge#590).

  ```
  python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py --all-worktrees ~/Harmonic_Projects/HRSE2 ~/harmonic-forge --watch l2 --watch l3 --interval 300
  ```

  **Arm it verbatim, from wherever the session already is** — no `cd` first.
  `git worktree list` only ever sees one repository, so the roots the belt
  should span are named to `--all-worktrees` rather than inferred from the
  working directory (harmonic-forge#594). A named root that is not a repo is a
  hard error, not a warning: a root you asserted and that contributes nothing
  would arm a narrower belt than you asked for.

  **GitHub enriches; it does not discover.** The repo-wide sweep
  (`discover_l1_sweep`) is the *suspenders'* backstop, not the belt's pull
  source — see "Role: Lane 1" below. Arming it as the belt is
  harmonic-forge#590's regression: it surfaces every open issue in the repo
  rather than the work in hand, and it collapses two deliberately independent
  mechanisms into one, which is what this protocol's name is about.

- **Lane 2** — self-discovers from its own worktree when it is genuinely on an
  issue's branch:

  ```
  python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py \
      --worktrees ~/Harmonic_Projects/HRSE2-lane2 \
      --watch l1 --interval 90
  ```

  and falls back to `--queue-for l2` when it is between issues on a detached
  HEAD, rather than guessing an issue number:

  ```
  python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py \
      --queue-for l2 --repo vitalharmony/hrse --watch l1 --interval 90
  ```

- **Lane 3** — no worktree of its own either; queue-discover repo-wide:

  ```
  python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py \
      --queue-for l3 --repo vitalharmony/hrse --watch l1 --interval 60
  ```

**A monitor that never printed a status line is not proof it is watching
anything.** For `--worktrees` — which now includes **Lane 1's belt**, since it
is worktrees-first — the script reports its resolved target set at startup and
on every change: how many resolved, which did not and why, and explicitly,
never silently, when zero resolved. `--all-worktrees` adds a line per repo
root, because a root that contributes nothing (not a repo, or the *same* repo
as another root) is otherwise invisible inside an aggregate count, and a belt
covering one repo instead of two looks identical to one covering both
(harmonic-forge#590). For `--queue-for` — the Lane 3 command above, and the
suspenders' Lane 1 sweep below — it reports the queued count once at the first
poll, even when that count is zero. A genuinely quiet repo and a dead process
must never look the same on the log.

**A worktree is evidence work was started, not that it is live.** Abandoned
`/tmp/<repo>-<issue>-impl` checkouts are never pruned, and their branches read
as ahead of `origin/main` forever once main takes the work as a squash merge.
Targets whose issue is closed are therefore dropped and named as dropped —
without that, worktrees-first fails permissively in its own smaller way.

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
2. **What do I owe** — repo-wide, ignoring recency. This is where the
   repo-wide sweep belongs, and the only place it belongs (harmonic-forge#590).
   Lane 1 runs it here, per repo, as a one-shot backstop to the belt — never as
   the belt itself:

   ```
   python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py --queue-for l1 --repo vitalharmony/hrse --interval 600
   ```

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

**Belt pull source: the live worktrees** (`--all-worktrees`). That set bounds
discovery to work that exists. GitHub is then read to enrich an issue a worktree
has already named — it is not the place candidates come from.

**Suspenders backstop: a repo-wide newest-marker sweep** (`discover_l1_sweep`).
This runs on the *pull loop*, not the monitor, and exists for exactly what the
belt structurally cannot see: handoffs that predate it, anything its filter
misses, and issues whose state changed with no new comment. Promoting it to the
belt was harmonic-forge#590.

> For each open issue, find the newest comment carrying a lane-post marker. If
> its `posted-by` is **not** Lane 1, the ball is with Lane 1. If the newest
> marker is Lane 1's own, it has already acted.

Newest wins, full stop — no precedence table, no exclusion list. "Already acted"
needs no state because acting *is* posting, which makes Lane 1's marker newest.

**This rule is only total if Lane 1 posts a `kind=discussion` when it decides an
issue needs nothing.** Otherwise that issue is re-offered every tick, and the
`AskUserQuestion` becomes a nag.

**After a Lane 3 PASS that Lane 1 has independently re-verified live: merge and
close directly** (R-0351, the explicit carve-out this line implements — Lane 1
only, PASS only, independent re-verification required). Do not present it as a
decision needing approval; narrate it after the fact. One carve-out — an issue
labeled `data-migration` cannot close without the migration actually running or
an explicit `migration-abandoned` decision, and `block_data_migration_close.py`
enforces that regardless of PASS.

**An agreed decision is not "pending."** It is completed-but-unexecuted, and it
does not wait behind a newly-opened item. "1 agreed" sat unexecuted for multiple
turns while item 2 was investigated.

Parameters: overlap `K` to be set from the first week's tick log · no session
lock (Lane 1's belt does not mutate a shared worktree).

## Role: Lane 2

Fires on `handoff`, `rework`. Silent on `ready-for-l3`, `ae`,
`sweep`, `ae-and-sweep`, `spec`, `gate-result` — that is the Lane 1 ↔ Lane 3
channel. (`discussion` was removed from `QUEUE_KINDS["l2"]` in
harmonic-forge#570 — R-0337/`lane-shorthand.md` measured 63 issues on
`vitalharmony/hrse` whose newest marker after `l2.done` was a `discussion`,
none of them actionable — so it fires on neither belt now.)

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
