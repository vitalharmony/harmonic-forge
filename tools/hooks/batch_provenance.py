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
"""

from __future__ import annotations

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


def refusal_reason(prompt: str, batch_line_index: int) -> str | None:
    """Why this prompt may not mint a BATCH grant, or None if it may.

    `batch_line_index` is the 0-based line the `BATCH` token was found on;
    the disclaimer only counts when it appears ABOVE that line, so a message
    that authorizes and then discusses the mechanism afterwards still works.
    """
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
