---
name: auto-ae
description: The operator's `/auto-ae on|off|status` surface for auto-AE (harmonic-forge#874). This skill toggles nothing; a UserPromptSubmit hook acts on the operator's typed prompt, in a Lane 1 session only. Today only a probe exists and nothing can be turned on. Do NOT invoke this skill on your own initiative; only the operator types it.
---

# auto-ae

**This skill toggles nothing.** Invoking it, by the operator or by the model
through the Skill tool, changes no state and grants nothing.

**Auto-AE is not implemented.** Only a probe exists (harmonic-forge#874,
implementation step 1). There is no `on` state to reach, no state file, and
no AE is ever posted on auto-AE's authority. Every Lane 3 spec still needs the
operator's own HITL approval and AE, exactly as before.

## What happens when the operator types `/auto-ae …`

1. `tools/hooks/auto_ae_toggle.py` runs on `UserPromptSubmit`. That hook, not
   this skill, is where any toggle will live: the hook sees only prompts the
   operator submits, and a model-invoked Skill call fires no
   `UserPromptSubmit`.
2. In probe-only mode, when `LANE=1` and the prompt contains `auto-ae`, the hook
   appends one line to `~/.local/state/auto-ae/probe.jsonl`: the timestamp, the
   prompt text, `CLAUDE_CODE_ENTRYPOINT`, `LANE`, and the payload's top-level
   key names (never the `session_id` or `transcript_path` values). It decides
   nothing, writes no state, and prints `auto-ae probe recorded; nothing
   toggled`.
3. The probe answers two design questions before any toggle logic is written:
   whether a typed `/auto-ae` reaches `UserPromptSubmit` at all, and in what
   envelope.

## What to do when this skill is invoked

Say, in one line, that auto-AE is not implemented beyond a probe and that
nothing was toggled. Do not write any file, post any comment, or treat this as
an authorization of any kind. Do not read or summarize the probe log unless
the operator asks.

The design, the operator's four rulings, and the remaining steps are on
harmonic-forge#874.
