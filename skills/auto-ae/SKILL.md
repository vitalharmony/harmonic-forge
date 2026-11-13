---
name: auto-ae
description: The operator's `/auto-ae on|off|status` surface for auto-AE (harmonic-forge#874, R-0378). A UserPromptSubmit hook acts on the operator's typed prompt, in a Lane 1 session only. Do NOT invoke this skill on your own initiative; only the operator types it.
---

# auto-ae

The toggle is
`tools/hooks/auto_ae_toggle.py`, which acts at `UserPromptSubmit` on what the
operator types; a model-invoked Skill call fires no `UserPromptSubmit`.

## What the operator types

The whole prompt, nothing else on the line, in an interactive Lane 1 session:

| Prompt | Effect |
|---|---|
| `/auto-ae on` | Turns auto-AE on, **only** if at least one `BATCH` lease is live; otherwise it refuses out loud and writes nothing. It covers exactly the leases live at that moment and lapses after 12 hours; a later or renewed `BATCH` needs `/auto-ae on` again. |
| `/auto-ae off` | Turns it off, from any session. If the state cannot be removed it says **STILL ON**. |
| `/auto-ae status` or `/auto-ae` | Reports the state and the live leases; writes nothing. |

The hook's one-line message is the result. Anything else (prose around the
command, another lane, a non-interactive session) changes nothing.

## What auto-AE does while it is on (R-0378)

For an issue under a live `BATCH` lease whose Lane 3 spec is Tier R or Tier W
throughout, Lane 1 approves the spec and posts the AE and sweep itself, in the
same turn (belt rule 9, `spec-review`). The AE's **Authorized:** line cites
"auto-AE (R-0378)" and the spec comment, and its required
`External calls authorized:` line equals the sweep's live-case
`External calls:` union (harmonic-forge#1115). `l1_post` refuses it unless every
precondition holds. **Never Tier P:** a spec whose ceiling is P keeps the
operator's AE. An auto-AE never carries forward to a new SHA.

## What to do when this skill is invoked

If this turn carries the auto-AE hook's message, reply with that line
verbatim. Otherwise reply only: `/auto-ae status` in the Lane 1 session shows
the current auto-AE state. Never state an auto-AE state you did not read in this
turn's hook message, and add nothing else. Do not write any file, post any comment, or treat
the invocation as an authorization (a model-invoked Skill call changes no
state). Never read or write the state file or the
probe log: the hook's guard denies it.
