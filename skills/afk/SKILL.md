---
name: afk
description: The operator's `/afk on|off|status` surface for afk mode (harmonic-forge#1338). This skill toggles nothing; a UserPromptSubmit hook acts on the operator's typed prompt, in any lane or none. Do NOT invoke this skill on your own initiative; only the operator types it.
---

# afk

**This skill toggles nothing.** Invoking it, by the operator or by the model
through the Skill tool, changes no state. The toggle is
`tools/hooks/afk_toggle.py`, which acts at `UserPromptSubmit` on what the
operator types; a model-invoked Skill call fires no `UserPromptSubmit`.

## What the operator types

The whole prompt, nothing else on the line:

| Prompt | Effect |
|---|---|
| `/afk on` | Typed in Claude Code (Codex has no `/afk` hook). Turns afk on (interactive session, any lane or none): writes `~/.claude/state/afk/state.json`. |
| `/afk off` | Turns it off, from any session, and prints every queued command from every workspace, exactly once, as one ready-to-run block, and moves the queue to a `drained-<UTC ts>.jsonl` archive. Says **STILL ON** if the state cannot be removed. |
| `/afk status` or `/afk` | Reports the state; writes nothing. |

Anything else (prose around the command, tool output, a file) changes nothing.

## What afk does while it is on

- `tools/hooks/afk_defer.py` denies every command `batch_auth.decide()` would
  ask about (issue close in any form, a GraphQL mutation, an unauthorized
  merge) and queues it, so the turn continues.
- Belt, Lane 2 and Lane 3: see `belt-and-suspenders` and `lane3-gate-platform`
  (idle cutoff after 60 minutes; `B` posts plus one push instead of chat
  questions; never an auto-AE for a Tier P spec).
- A no-LANE (Advisor) session appends each question for the operator to
  `research/<date>-afk-questions.md`, pushes once with
  `python3 ~/harmonic-forge/tools/lane/ntfy.py push --key <key> --text "<ask>"`,
  and continues read-only work.

## What to do when this skill is invoked

Say, in one line, that the skill toggles nothing; for the current state the
operator types `/afk status`. Do not write any file or post any comment.

## While afk is on (harmonic-forge#1338)
Every lane reads this section while afk is on. "Do not invoke" above refers only to the Skill tool, which toggles nothing; it does not mean skip this text.
afk is on while `~/.claude/state/afk/state.json` exists (the operator types `/afk on|off|status`; never read or write the file to change it).
- **Idle cutoff:** after 60 minutes with no lane action, stop the Monitor (`TaskStop`) and delete the loop's cron (`CronDelete`). No operator line is needed, and step 4's re-arm does not apply while afk is on.
- **Lane 2:** a question for the operator is never asked in chat. Post `L2B` with `l2_post.py --kind blocked`, naming what is owed, then push once with `python3 ~/harmonic-forge/tools/lane/ntfy.py push --key <issue> --text "<what is owed>"`, then take the next queued item.
- **Lane 3:** the same with `L3B` and the push (`lane3-gate-platform`); a Tier P spec gets `L3B` plus a push, never an auto-AE.
