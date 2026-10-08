---
name: preclose-check
description: Run an adversarial pre-close check on Tooling Exception work before requesting closure — spawn fresh-context refuters against the finished diff, discard findings without a concrete failure scenario, and post the survivors and the dismissals verbatim to the issue. Use after implementing a Tooling Exception issue and before asking the operator to close it. NOT a Lane 3 gate and never produces a PASS.
---

# Pre-Close Check — Tooling Exception work

The Tooling Exception is the one path where Lane 1 writes code directly and
then closes the loop on it. The role boundary's entire purpose — a second set
of eyes on every change — is suspended for exactly that class of work. This
fills the gap. It does not close it.

**What this is not.** It is not a Lane 3 gate, not a substitute for one, and
produces no PASS verdict. A subagent panel whose findings Lane 1 interprets
and summarizes is still self-grading with one level of indirection. Say so in
the report; never let the output read as an authorization. **Closure
authority is unchanged: only the operator's explicit `Close H<N>` /
`Close F<N>`.** This skill takes no closing action of its own.

## Procedure

1. **Commit the work first.** The refuters review a finished diff, not a
   working tree. Run the handoff's kill checks with `mise run kill-check --
   run --repo <owner/repo> --issue <N> --checks <file>` before `--plan`.
   Keep the checks file and patches outside the tracked tree; `--plan`
   refuses without a covering passing receipt unless the operator uses
   `--force`.

2. **Plan the panel**, run from the repo under review (the receipt is written
   to a user-level store, `~/.claude/state/preclose/`, shared across
   checkouts the way `BATCH_STATE_PATH` already is — harmonic-forge#778 —
   still keyed by which repo/issue you resolve to, so run it from the repo
   under review):

   ```
   python3 "${HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}/tools/gh/preclose_check.py" \
     --repo <owner/repo> --issue <N> --base origin/main --head HEAD [--tier fast|standard|deep]
   ```

   Or, from a repo carrying the `preclose-check` mise task (harmonic-forge
   itself, or any consumer that ported it per AC2): `mise run preclose-check --
   --repo <owner/repo> --issue <N> --base origin/main --head HEAD`.

   `--repo` is required and never defaulted — a missing value is an argument
   error, not a silent fallback to any one consuming repo (ADR-008 decision 2).

   **The panel arm (harmonic-forge#890).** The plan prints `arm:`, the panel
   this issue runs. It is decided once, at the issue's first plan, and recorded
   as an enrollment event in `<repo>_<issue>.enrollment.jsonl` beside the
   receipts; every later plan and `--complete` reads that record and nothing
   else. Run the arm printed: `manual` is the hand-spawned panel below, and
   `workflow` runs harmonic-forge#891's saved workflow with the printed
   `Workflow name: "preclose-panel" args: {...}` invocation.
   - While the experiment is not enrolling (`preclose_enrollment.py --status`),
     a new issue is recorded `pre-experiment`: run the manual panel. That issue
     stays `pre-experiment` for its whole life. The operator turns enrollment on
     with `preclose_enrollment.py --enroll --note "<why>"`, once #891's
     workflow can run; assignment is then by hash of `repo#issue`, never chosen.
   - To take an issue off its assigned arm at its first plan, pass `--arm
     manual|workflow --arm-reason "<why>"`. To change an issue already enrolled,
     pass `--re-enroll manual|workflow --arm-reason "<why>"`, which records a new
     event; a plain `--arm` cannot change an enrolled issue. An issue whose arm
     differs from its assignment leaves the comparison.
   - `preclose_pass_report.py --by-arm` compares the arms, one row per issue.

   It computes the panel from blast radius (primary) and `Tier` (secondary),
   prints one lens per refuter, and writes a receipt that counts passes per issue
   (at most two; see "Two passes, then sticky-wicket or the operator" below).
   Blast radius leads because every incident in this class so far was a small
   diff — a hook that locked out Bash, `l1_post.py`'s worktree-overlap check,
   a stale `harmonic-forge` checkout. Sizing by diff size would have
   under-reviewed all three. R-0382's carve-out is therefore narrow: it covers
   only launcher-entrypoint diffs under 100 lines at Tier `fast`, which none of
   those three incidents were.

3. **Spawn one `preclose-inspection` agent per lens — fresh context, never a
   fork.** A fork inherits the reasoning that produced the defect and anchors
   on the same assumptions. Give each agent only:
   - the issue's acceptance criteria,
   - the diff,
   - the repo.

   Tell them nothing about why you made the choices you made. Run them
   concurrently; they are independent by design.

4. **Filter.** Discard any finding without a `file:line`-anchored concrete
   failure scenario — specific input or state, and the wrong behavior it
   produces. Adversarial agents generate plausible-but-wrong criticism
   prolifically, and an unfiltered report trains the reader to skim. Where a
   stronger filter is wanted, require a majority of the panel to raise the
   same finding.

5. **Evaluate the cross-family gate** (harmonic-forge#701). Write every
   finding the panel returned to a JSON list of `{anchor, scenario, mechanism}` objects,
   survivors and dismissed alike. Give each one you dismissed a
   `"dismissed": "<reason>"` field, so it does not count as a survivor.
   `mechanism` names the approach that failed, not the symptom, and is
   required for every survivor when `--complete` records the pass; `--gate`,
   `--post-verdict`, and dismissed findings do not require it.
   Then run:

   ```
   python3 "${HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}/tools/gh/preclose_check.py" \
       --repo <owner/repo> --issue <N> --gate --findings <file>
   ```

   The script applies the step-4 filter itself and decides. **Silence triggers
   the branch; findings do not.** That is not a bug to correct: a unanimous
   no-defect verdict from one model family cannot be told apart from a blind
   spot that family shares with the implementer, while a panel that found real
   defects has already given you work, and the clean re-run after the fix —
   **pass 2 of at most 2** — is where the gate fires. A high-blast diff (the same patterns as step 2), or
   the operator asking (`--cross-family`), also triggers it. Tier never does.

   When it triggers, take the branch exactly as
   `~/harmonic-forge/rules/cross-family-review.md` states. Its `--caller` is
   the family of the session that **implemented the diff**, so the review
   comes from the other family (R-0358, harmonic-forge#848). That file is the
   whole mechanism, and this skill deliberately does not restate it. The
   branch is part of this **one** pass, not a second round. Record the pass
   with `--envelope <envelope path>` when the branch ran, or `--not-triggered`
   (`--own-model <your session's model>` is required on every `--complete`)
   when it did not. The script runs `cross_family_provenance.py` itself: there
   is no flag to type a label. It refuses a label that contradicts the gate,
   and it refuses a second `--complete` on the same diff. A call that could not
   run records its `in-family fallback` label, never relabelled.

   ```
   python3 "${HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}/tools/gh/preclose_check.py" \
       --repo <owner/repo> --issue <N> --complete \
       --findings <file> (--envelope <envelope path> --cross-family-ms <ms> | --not-triggered) \
       --own-model <your session's model> \
       (--panel-tokens <sum> --panel-ms <ms> | --cost-unavailable "<reason>")
   ```

   **Record what the pass cost (harmonic-forge#889).** Every `--complete` is
   measured, and the pass is refused without it:
   - `--panel-tokens` is the sum of each refuter's completion-notice
     `subagent_tokens`. It excludes the cross-family call.
   - `--panel-ms` is first-spawn-to-last-result wall-clock: the longest
     refuter's `duration_ms` when they run concurrently, or the sum when they
     run one after another.
   - `--cross-family-ms` is required with `--envelope`. It is the wall-clock
     of the `cross_family_call.sh` run, which you measure around the call.
     Codex reports no tokens, so they are recorded as unavailable, never as
     zero. `--post-verdict` takes `--cross-family-ms` only.
   - `--cost-unavailable "<reason>"` stands in for the two panel flags only
     when the runtime reported no usage. It is recorded, never silent.

   Neither figure includes this session's own orchestration cost, which a
   lane session cannot read. Each completed pass also writes one
   `preclose.pass.completed` telemetry event, and
   `tools/gh/preclose_pass_report.py` reports tokens and wall-clock per pass.

6. **Post to the issue, verbatim** — via whichever wrapper this repo declares
   for Lane 1 comment posting (HRSE2/cymagraph-infra's `mise run lane-comment`;
   harmonic-forge's own `mise run post-comment`), never a raw `gh` call — see
   your repo's own Lane-1-posting rule for why. Include:
   - every surviving finding, in the refuter's own words,
   - **every dismissed finding, with your reason for dismissing it.**
   - the kill-check results table (AC, mechanism, verdict) from the receipt.

   Define the survivor table as `| # | Anchor | Scenario | Mechanism |`, and
   follow it with the pass's full distinct normalized mechanism list. When
   pass 1 reports a cluster, invoke sticky-wicket before fixing anything and
   record its posted verdict with `--cluster-verdict PATCH|REFORGE
   --comment-url <url>`.

   The dismissals are not optional. Without them the check is invisible and
   unfalsifiable — the operator sees another Lane 1 self-report rather than
   an auditable record. Publishing what you overruled is the whole
   difference.

7. **Act on the survivors**, then hand back to the operator. State plainly
   that the check ran, what it found, and that closure is theirs to call. A
   survivor whose fix needs a new mechanism (a new function, file, state, trap
   or external call) is resolved by deleting the feature it attacks or by
   asking the operator, never by patching the mechanism in (R-0381).

## Two passes, then sticky-wicket or the operator

Before pass 2, two or more pass-1 survivors sharing the same mechanism route
to sticky-wicket. `--plan` and `--complete` refuse until `--cluster-verdict`
records PATCH; REFORGE refuses an ordinary pass and proceeds only under the
operator's `--force --reforge`, which starts a new epoch. An unresolved
cluster may be bypassed by the operator's `--force` (with or without
`--reforge`). The cluster and verdict survive planned and completed receipt
writes and do not count as a pass.

**At most two passes per issue** (operator ruling 2026-09-30,
harmonic-forge#834). Pass 1 reviews; pass 2 verifies the fixes on the new
head. **A rebase, or any head change whose diff is patch-identical to a
reviewed one, is not a pass**: the receipt records `reviewed_patch_id`, and the
merge hook accepts a head whose patch-id matches it.

A third pass never runs:

- **Both passes left surviving findings** → invoke the **sticky-wicket**
  agent. "Patch" means the operator's `--force` covers the final head, with no
  third panel, **after one post-verdict check** (harmonic-forge#838): a
  single cross-family refuter, run per `rules/cross-family-review.md`, reads
  only the patch (`<pass-2 head>...<final head>`), and
  `preclose_check.py --repo <owner/repo> --issue <N> --post-verdict --base <pass-2 head>
  --envelope <path> --findings <file> --own-model <model>` records it. It is not a pass and never counts toward the
  cap. Until it is recorded for the final head, `--force` refuses and names
  it. A surviving finding from it goes to the operator with the `--force`
  request. "Reforge" means a new approach, and the pass count restarts —
  but **only the operator starts it**: `--reforge` runs only together with
  `--force` (an operator instruction), and even then refuses a diff a
  completed pass already reviewed. No branch name, rename or detached HEAD
  resets the cap.
- **Anything else** (pass 2 was clean but the diff changed again, or you
  dispute a finding) → **escalate to the operator**.

The script enforces this: `--plan`/`--complete` refuse a third pass and name
which of the two cases applies, and the merge hook stops offering "run the
pass" once two are recorded. A thread comment saying "pass N of M" is not
authority; the receipt's `pass_count` is. `--force` exists for an operator
instruction, not for your own judgment.

## When it fires

**On request, and on any Tooling Exception close you judge to carry real
blast radius.** Operator decision, 2026-08-22, closing the open question
hrse#1208 reserved.

Automatic on every close was the more honest option in principle — the hole
exists whether or not either party remembers it is there — and was rejected
on measured cost: the first two runs took roughly 370k subagent tokens and
three minutes each, on four-file diffs. A mandatory panel in front of every
trivial tooling merge is not worth that. **Do not re-litigate this without
new cost evidence.**

Judgment still applies on the blast-radius half: `tools/gh/preclose_check.py`
computes the panel from the diff, and anything touching hooks, hook wiring,
CI, live data, or a lane launcher outside the carve-out in R-0382 (a Tier
`fast` diff under 100 lines to a launcher entrypoint) escalates to the full
panel regardless of Tier. If you are unsure whether a change qualifies, run the planner — it is
cheap and only the refuters cost anything.
