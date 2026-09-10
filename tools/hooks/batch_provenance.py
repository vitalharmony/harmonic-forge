#!/usr/bin/env python3
"""Whether a prompt may mint a BATCH grant at all (harmonic-forge#589).

`batch_auth.py`'s docstring already states the boundary: a grant is created
from an operator's chat message, "never in response to text read from a file,
issue/PR body, tool output, or web page." Nothing enforced it. On 2026-09-10 a
`general-purpose` subagent quoted a `sprint-plan`/`milestone_summary.py` audit
verbatim in its report; the report reached the parent session as a
`<task-notification>`, `expand_lane_shorthand.py`'s `UserPromptSubmit` hook
parsed `BATCH F74,F326,F569,F582` out of it, and four real 12-hour merge+close
grants appeared in the live store. The operator typed nothing.

WHY THE HOOK CANNOT SIMPLY ASK WHO SENT THE PROMPT
--------------------------------------------------
The harness knows. Every user record it writes carries `promptSource` and
`origin` -- measured across 10,719 live records in this machine's transcripts:

    promptSource         origin.kind         count   what it is
    typed                human                6589   the operator typing
    suggestion_accepted  human                 467   the operator accepting
    queued               human                 299   the operator, mid-turn
    system               task-notification    2180   a subagent/background report
    system               (none)                424   a /loop re-fire or schedule
    system               peer                    6   another Claude session
    system               auto-continuation       1   usage-limit auto-resume
    sdk                  (none)                749   a headless `claude -p` run
    sdk                  task-notification       3   a report, in an SDK session

`origin.kind == "human"` is exactly the operator. **It is not reachable from a
hook.** Measured live (`claude -p` against a probe hook, 2026-09-10), the
`UserPromptSubmit` payload is only:

    cwd, hook_event_name, permission_mode, prompt, prompt_id, session_id,
    transcript_path

-- no `promptSource`, no `origin`. Nor can the hook recover them from the
transcript: the same probe showed the current prompt's record **has not been
written yet** when the hook runs (mid-session, `transcript_path` existed and
its newest user record was the *previous* prompt; on a session's first prompt
the file did not exist at all). Correlating on `prompt_id` returns nothing,
every time, by construction.

WHAT IS LEFT, AND WHY IT HOLDS
-------------------------------
The harness wraps injected text in its own envelope before the hook sees it --
verified live, not assumed: a probe hook capturing the raw `prompt` for a
background-task completion received it beginning `<task-notification>\\n
<task-id>...`. Content inside an envelope can *add* text but can never delete
the wrapper the harness put around it, so the test fails CLOSED under
adversarial input: a hostile report that forges `</task-notification>` and a
fake operator message after it still carries the opening tag, and is still
refused.

That is the direction that matters. A false refusal costs the operator one
retyped message; a false grant is standing authority to merge and close.

DO NOT REPLACE THIS WITH A CLEVERER READ OF THE TEXT
-----------------------------------------------------
`expand_lane_shorthand.batch_keys()` already skipped fenced and blockquoted
lines, and the emitter that produced the incident's line already fenced it.
Both were true and the grant was still minted: an unbalanced ``` earlier in
the report (line 8 of 13,890 characters) inverted the fence parity, so the
emitter's *opening* fence at line 86 read as a closing one and the `BATCH`
line at 87 scanned as ordinary prose. Fence parity is a property of text this
hook does not control and cannot trust. Provenance is the boundary; quoting
heuristics are a courtesy on top of it.

THE ENVELOPE TEST WAS STILL A DENYLIST -- AN ALLOWLIST CLOSES THE REST
------------------------------------------------------------------------
Found by `preclose-inspection` on this issue's own PR, live-reproduced against
this exact module: `injected_envelope()` returns `None` -- "no objection" --
for ANY prompt carrying none of the listed markers, including one with no
harness envelope at all. `tools/lane/cross_family_call.sh --posture read-only`
runs a headless `claude -p "$(cat brief)"` with no `--cwd`, and a cold brief
that quotes a fetched issue/PR body verbatim (the pattern this whole family of
tooling uses on purpose) carries a `BATCH` line straight into that session's
`UserPromptSubmit` hook -- no envelope, no disclaimer, and (until this fix) no
refusal. Reproduced live against `fix/589-batch-provenance-gate` @ `714ed1b`:
a bare prompt with a `BATCH` line and zero markers minted a real grant.

Re-measured from the module's own transcript sweep: of 10,779 records,
**1,188 are non-`('typed'|'suggestion_accepted', 'human')` and carry no
envelope marker** -- 754 `('sdk', None)` (a headless `claude -p`/SDK run,
exactly the reproduced vector) plus 434 `('system', None)` (a `/loop` re-fire
or scheduled wakeup *within an already-interactive session*).

`CLAUDE_CODE_ENTRYPOINT` closes the first class and is measured, not assumed:
a probe hook run under `claude -p` (2026-09-10) recorded `sdk-cli`; this
session's own live environment recorded `cli`. Grepping the CLI binary's own
string table for every value it sets enumerates the full vocabulary:

    cli, claude-desktop, claude-vscode, claude-in-teams   -- launched BY a human
    remote                                                 -- ambiguous, excluded
    sdk-cli, sdk-py, sdk-ts, local-agent, bench, unknown   -- never a human

The allowlist below is deliberately the first row only -- `remote`'s exact
meaning (a browser session vs. a headless remote/cloud run) was not resolved
live and a wrong inclusion re-opens exactly this gap, so it is treated the
same as an unrecognised value: refused. **This closes the reproduced `sdk`
vector (754 of 1,188) by construction, every session in this vocabulary,
present or future, that Claude Code has not yet added a name for included.**

It does NOT close the `('system', None)` class (434 records): `/loop`'s own
re-fire and a `ScheduleWakeup` delivery both run inside the operator's own
already-interactive session, so `CLAUDE_CODE_ENTRYPOINT` is `cli` for those
too -- an env var is session-scoped, not per-prompt, and cannot tell "the
operator just typed this" apart from "the scheduler just delivered this into
the same session" the way it can tell a whole separate headless process apart.
No hook-visible signal for that distinction was found this session (the
harness's own `promptSource`/`origin` fields would answer it and are the
unreachable ones documented above). **Stated as an open gap, not silently
narrowed to look closed:** a hostile `BATCH` line reaching an interactive
session only through a scheduled/looped delivery -- e.g. quoted inside content
a `/loop` iteration itself fetches and echoes -- still mints a grant after
this fix. Not filed as a follow-up by this module -- Lane 2 does not file
issues (R-0157) -- named here and in this PR's own completion report instead,
for whoever confirms or refutes a per-prompt signal exists.
"""

from __future__ import annotations

import os
import re

#: Envelopes the harness itself wraps around text that did not come from the
#: operator's keyboard. Each is a literal the *harness* writes, not something
#: the wrapped content chooses, which is what makes presence dispositive.
#: Evidence for each is a live transcript record or the probe run above; a
#: marker with no such evidence does not belong here, because every entry is
#: also a way to refuse a message the operator really did type.
INJECTED_ENVELOPES: tuple[tuple[str, str], ...] = (
    ("<task-notification>", "a background-task or subagent completion report"),
    ("<cross-session-message", "a message from another Claude session"),
    ("Another Claude session sent a message:",
     "a message from another Claude session"),
    ("<command-message>", "a slash-command invocation"),
    ("<command-name>", "a slash-command invocation"),
    ("<local-command-stdout>", "slash-command output"),
    ("Your claude.ai usage limit has reset.",
     "an automatic continuation after a usage limit"),
)

#: `milestone_summary.py` and its siblings label their own proposal lines. The
#: emitter saying "this is not an authorization" is a fact about the text, and
#: honoring it costs nothing. This catches the case the envelope test cannot:
#: an operator PASTING a tool's output into chat, where `promptSource` really
#: is `typed` and no envelope exists. Deliberately matched on the stable half
#: of the sentence rather than its em dash, which varies by emitter.
PROPOSAL_DISCLAIMER_RE = re.compile(r"not an authorization", re.IGNORECASE)

#: The `CLAUDE_CODE_ENTRYPOINT` values a human could plausibly be sitting in
#: front of -- enumerated by grepping the CLI binary's own string table
#: (2026-09-10, version 2.1.267), not guessed. `remote` is EXCLUDED
#: deliberately: its exact meaning (a browser session vs. a headless
#: remote/cloud run) was not resolved live, and this list's whole job is to
#: fail closed on anything not confirmed -- see the module docstring's
#: "THE ENVELOPE TEST WAS STILL A DENYLIST" section for the full vocabulary
#: (`sdk-cli`/`sdk-py`/`sdk-ts`/`local-agent`/`bench`/`unknown`) and why each
#: of those is excluded.
_INTERACTIVE_ENTRYPOINTS = frozenset({"cli", "claude-desktop", "claude-vscode", "claude-in-teams"})


def injected_envelope(prompt: str) -> str | None:
    """What this prompt arrived wrapped in, or None if it is a bare message.

    Substring, not prefix: a report that prepends its own preamble to the
    harness envelope must still be caught, and the envelope's *position* is
    not something the wrapped content is prevented from changing.
    """
    for marker, description in INJECTED_ENVELOPES:
        if marker in prompt:
            return description
    return None


def non_interactive_entrypoint() -> str | None:
    """The session's own `CLAUDE_CODE_ENTRYPOINT`, if it is not one a human
    could be sitting in front of -- or None when it is (`_INTERACTIVE_
    ENTRYPOINTS`) or genuinely absent (an older harness build; fails open on
    ABSENCE deliberately, since refusing every prompt on a missing env var
    would be indistinguishable from the guard being broken, not working).

    This is the allowlist half of the fix (module docstring, "THE ENVELOPE
    TEST WAS STILL A DENYLIST") -- closes the reproduced `claude -p`/SDK
    vector; does not close a scheduled delivery inside an already-interactive
    session, which shares that session's `cli` value. See the docstring for
    the measured split (754 vs. 434 of the 1,188-record gap).
    """
    value = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
    if value is None or value in _INTERACTIVE_ENTRYPOINTS:
        return None
    return value


def refusal_reason(prompt: str, batch_line_index: int) -> str | None:
    """Why this prompt may not mint a BATCH grant, or None if it may.

    `batch_line_index` is the 0-based line the `BATCH` token was found on;
    the disclaimer only counts when it appears ABOVE that line, so a message
    that authorizes and then discusses the mechanism afterwards still works.
    """
    entrypoint = non_interactive_entrypoint()
    if entrypoint is not None:
        return (f"this session's CLAUDE_CODE_ENTRYPOINT is {entrypoint!r}, not "
                "an interactive one a human could be typing into. A BATCH "
                "grant is created only from the operator's own interactive "
                "session (harmonic-forge#589)")
    envelope = injected_envelope(prompt)
    if envelope is not None:
        return (f"it arrived as {envelope}, not as an operator chat message. "
                "A BATCH grant is created only from something the operator "
                "typed (harmonic-forge#589)")
    above = prompt.splitlines()[:batch_line_index]
    if any(PROPOSAL_DISCLAIMER_RE.search(line) for line in above):
        return ("the line is introduced by its emitter's own \"not an "
                "authorization\" disclaimer, so it is a proposal quoted into "
                "this message, not an instruction (harmonic-forge#589)")
    return None
