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
