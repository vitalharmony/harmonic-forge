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
   working tree.

2. **Plan the panel**, run from the repo under review (its `.claude/cache/preclose/`
   receipt anchors to `git rev-parse --show-toplevel`, not to where the script
   itself lives — harmonic-forge#704):

   ```
   python3 "${HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}/tools/gh/preclose_check.py" \
     --repo <owner/repo> --issue <N> --base origin/main --head HEAD [--tier fast|standard|deep]
   ```

   Or, from a repo carrying the `preclose-check` mise task (harmonic-forge
   itself, or any consumer that ported it per AC2): `mise run preclose-check --
   --repo <owner/repo> --issue <N> --base origin/main --head HEAD`.

   `--repo` is required and never defaulted — a missing value is an argument
   error, not a silent fallback to any one consuming repo (ADR-008 decision 2).

   It computes the panel from blast radius (primary) and `Tier` (secondary),
   prints one lens per refuter, and writes a receipt enforcing one pass.
   Blast radius leads because every incident in this class so far was a small
   diff — a hook that locked out Bash, `l1_post.py`'s worktree-overlap check,
   a stale `harmonic-forge` checkout. Sizing by diff size would have
   under-reviewed all three.

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
   finding the panel returned to a JSON list of `{anchor, scenario}` objects,
   survivors and dismissed alike. Give each one you dismissed a
   `"dismissed": "<reason>"` field, so it does not count as a survivor.
   Then run:

   ```
   python3 "${HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}/tools/gh/preclose_check.py" \
       --repo <owner/repo> --issue <N> --gate --findings <file>
   ```

   The script applies the step-4 filter itself and decides. **Silence triggers
   the branch; findings do not.** That is not a bug to correct: a unanimous
   no-defect verdict from one model family cannot be told apart from a blind
   spot that family shares with the implementer, while a panel that found real
   defects has already given you work, and the clean re-run after the fix is
   where the gate fires. A high-blast diff (the same patterns as step 2), or
   the operator asking (`--cross-family`), also triggers it. Tier never does.

   When it triggers, take the branch exactly as
   `~/harmonic-forge/rules/cross-family-review.md` states. That file is the
   whole mechanism, and this skill deliberately does not restate it. The
   branch is part of this **one** pass, not a second round. Record the pass
   with `--envelope <envelope path>` when the branch ran, or `--not-triggered`
   when it did not. The script runs `cross_family_provenance.py` itself: there
   is no flag to type a label. It refuses a label that contradicts the gate,
   and it refuses a second `--complete` on the same diff. A call that could not
   run records its `in-family fallback` label, never relabelled.

   ```
   python3 "${HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}/tools/gh/preclose_check.py" \
       --repo <owner/repo> --issue <N> --complete \
       --findings <file> (--envelope <envelope path> | --not-triggered)
   ```

6. **Post to the issue, verbatim** — via whichever wrapper this repo declares
   for Lane 1 comment posting (HRSE2/cymagraph-infra's `mise run lane-comment`;
   harmonic-forge's own `mise run post-comment`), never a raw `gh` call — see
   your repo's own Lane-1-posting rule for why. Include:
   - every surviving finding, in the refuter's own words,
   - **every dismissed finding, with your reason for dismissing it.**

   The dismissals are not optional. Without them the check is invisible and
   unfalsifiable — the operator sees another Lane 1 self-report rather than
   an auditable record. Publishing what you overruled is the whole
   difference.

7. **Act on the survivors**, then hand back to the operator. State plainly
   that the check ran, what it found, and that closure is theirs to call.

## One pass, then escalate

If you disagree with a finding after one revision, **escalate to the
operator** rather than re-running. The script refuses a second pass on the
same issue for this reason: a second panel on the same diff is Lane 1
arguing with itself at the operator's cost. `--force` exists for an operator
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
lane launchers, CI, or live data escalates to the full panel regardless of
Tier. If you are unsure whether a change qualifies, run the planner — it is
cheap and only the refuters cost anything.
