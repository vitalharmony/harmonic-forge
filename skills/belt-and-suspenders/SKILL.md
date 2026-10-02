---
name: belt-and-suspenders
description: Arm a lane's proactive work-discovery protocol — a persistent Monitor (the belt) plus a self-paced loop (the suspenders), parameterized by the LANE env var. Use when a lane session should spend idle time advancing work instead of waiting. Do NOT use in a session with no LANE set.
---

# Belt and suspenders

1. Run `python3 ~/harmonic-forge/tools/lane/belt_plan.py`. If it exits non-zero,
   report its message and stop.
2. Make exactly the `Monitor` call (`monitor`) and the `Skill` call (`loop`) it
   prints, character for character. Do not rewrite, extend or summarize either.
3. Report the monitor task id and the loop job id, as its `report` says.
4. The belt never stops and never asks the operator whether to stop. When the
   Monitor expires, re-arm the same call unchanged. A hook denies any other
   arming call: no `CronCreate` of your own, no hand-written prompt, no
   repo-wide sweep.
5. Pacing back-off between ticks uses `ScheduleWakeup`, never a new `/loop` or
   `CronCreate`.

## Tick output — silence is the default

Every Monitor event and every loop tick ends in exactly one of two ways.

6. **Nothing is owed to this lane → no text at all.** Do not report, acknowledge, summarize, or say "nothing to do". Call `ScheduleWakeup` with `noop: true` (loop tick) or simply end the turn (Monitor event — re-arming on expiry per step 4 is not text and still happens). The global BLUF rule does not apply: a no-op tick is not a response to the operator, so there are no sections to print, empty or otherwise.
7. **Another lane's event is not this lane's news.** A post by another lane — a ready-for-l3, an AE, a gate result, a handoff for someone else — is owed to that lane. Stay silent unless it changes what *this* lane must do next (e.g. Lane 2 receiving a `rework` or a FAIL on its own branch — a FAIL reaches Lane 2 as `queued-for-l2 kind=gate-result owes=fix`). Never relay, restate, or announce another lane's post to the operator. The belt already drops the kinds another lane owes (harmonic-forge#851), so anything it still prints that is not in rule 9 is a kind it has no owner for: read it, and stay silent unless it is yours.
8. **Speak only when this lane acted, or needs the operator.** Then the BLUF rule applies in full, and it covers only this lane's action and the operator's next step — including the literal trigger phrase when another lane must be woken.
9. **A `queued-for-<lane> … owes=<action>` event is the trigger** (harmonic-forge#851). Do the step below for your own lane. Do not ask first, and do not relay it to the operator. The preconditions each step names are checks: when one fails, report that lane's `B` token naming it, and never turn it into a question. Lane 3's `owes=gate` authority is R-0208 (the posted AE is Lane 3's trigger). Its `owes=spec` authority is R-0220 (equivalent to `Spec H<N>`). This is Claude Code only: a Codex Lane 3 session has no belt and keeps the relay it names.

   | Lane | owes | Next step |
   |---|---|---|
   | l3 | `spec` | `python3 ~/harmonic-forge/tools/gh/fetch_lane1_context.py --repo <r> --issue <n>`, then derive and post the `## Lane 3 Test Spec` with the repo's `lane-comment --kind spec`, then stop for review. |
   | l3 | `gate` | The repo's `lane3-begin --issue <n>`, then the `check_lane3_ready` readiness check, then only the AE's **Authorized:** cases. |
   | l3 | `sweep-missing` | Post `L3B` naming the missing sweep (R-0208: AE and sweep are one action). |
   | l2 | `implement` | Re-read the full issue thread, then implement from the handoff on its feature branch. |
   | l2 | `plan` | Re-read the full issue thread, then post the plan with `l2_post.py --kind spec` (`L2S`) and stop. |
   | l2 | `fix` | A `rework` or a FAIL `gate-result`: re-read the issue thread, then do the work on the branch. |
   | l1 | `plan-review` | Review Lane 2's plan and post the verdict and Implementation Spec. |
   | l1 | `spec-review` | Review Lane 3's spec. Post the AE and sweep together once the operator's AE arrives, or under the auto-AE rule when it applies. |
