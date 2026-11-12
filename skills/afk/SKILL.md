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
| `/afk on` | Turns afk on (interactive session, any lane or none): writes `~/.claude/state/afk/state.json`. |
| `/afk off` | Turns it off, from any session, and prints every command queued for this workspace as one ready-to-run block, then clears those lines. Says **STILL ON** if the state cannot be removed. |
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
