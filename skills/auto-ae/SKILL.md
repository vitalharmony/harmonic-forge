---
name: auto-ae
description: The operator's `/auto-ae on|off|status` surface for auto-AE (harmonic-forge#874, R-0378). This skill toggles nothing; a UserPromptSubmit hook acts on the operator's typed prompt, in a Lane 1 session only. Do NOT invoke this skill on your own initiative; only the operator types it.
---

# auto-ae

**This skill toggles nothing.** Invoking it, by the operator or by the model
through the Skill tool, changes no state and grants nothing. The toggle is
`tools/hooks/auto_ae_toggle.py`, which acts at `UserPromptSubmit` on what the
operator types; a model-invoked Skill call fires no `UserPromptSubmit`.

## What the operator types

The whole prompt, nothing else on the line, in an interactive Lane 1 session:

| Prompt | Effect |
|---|---|
| `/auto-ae on` | Turns auto-AE on, **only** if at least one `BATCH` lease is live; otherwise it refuses out loud and writes nothing. |
| `/auto-ae off` | Turns it off. |
| `/auto-ae status` or `/auto-ae` | Reports the state and the live leases; writes nothing. |

The hook's one-line message is the result. Anything else (prose around the
command, another lane, a non-interactive session) changes nothing.

## What auto-AE does while it is on (R-0378)

For an issue under a live `BATCH` lease whose Lane 3 spec is Tier R or Tier W
throughout, Lane 1 approves the spec and posts the AE and sweep itself, in the
same turn (belt rule 9, `spec-review`). The AE's **Authorized:** line cites
"auto-AE (R-0378)" and the spec comment. `l1_post` refuses it unless every
precondition holds. **Never Tier P:** a spec whose ceiling is P keeps the
operator's AE. An auto-AE never carries forward to a new SHA.

## What to do when this skill is invoked

Say, in one line, that the skill toggles nothing and that the state is what
the hook's last message said; for the current state the operator types
`/auto-ae status`. Do not write any file, post any comment, or treat the
invocation as an authorization. Never read or write the state file or the
probe log: the hook's guard denies it.
