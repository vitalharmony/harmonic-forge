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

6. **Nothing is owed to this lane → no text at all.** Do not report, acknowledge, summarize, or say "nothing to do". Call `ScheduleWakeup` with `noop: true` (loop tick) or simply end the turn (Monitor event). The global BLUF rule does not apply: a no-op tick is not a response to the operator, so there are no sections to print, empty or otherwise.
7. **Another lane's event is not this lane's news.** A post by another lane — a ready-for-l3, an AE, a gate result, a handoff for someone else — is owed to that lane. Stay silent unless it changes what *this* lane must do next (e.g. Lane 2 receiving a `rework` or a FAIL on its own branch). Never relay, restate, or announce another lane's post to the operator.
8. **Speak only when this lane acted, or needs the operator.** Then the BLUF rule applies in full, and it covers only this lane's action and the operator's next step — including the literal trigger phrase when another lane must be woken.
