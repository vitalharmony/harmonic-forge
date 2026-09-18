# Advisory subagent findings — file-return convention (harmonic-forge#693)

Referenced by `preclose-inspection.md`, `pitch-inspection.md`,
`product-strategy.md`, and `sticky-wicket.md`. One shared convention, not
four independent copies — a second copy is how this kind of thing drifts.

## Why

A subagent's final chat message is summarized at the parent-session boundary
and cannot be recovered verbatim afterward, and the correct workaround —
relaying the findings through a shell string — is exactly the class of
corruption `tools/hooks/block_inline_prose.py` exists to deny (harmonic-forge#266:
silent command substitution, dropped apostrophes, `%` mangling). The fix is
on the producing side: write the findings to a file, return only the path.
The parent reads it with `Read` — no shell, no quoting, no hook interaction,
full fidelity.

## What to do, as the last action before ending your turn

1. Determine the target directory: use the scratchpad directory the
   invoking session's own environment names (the same field a top-level
   session sees in its own Environment block, e.g. "Scratchpad directory:
   /tmp/claude-.../scratchpad" — check your own context for it first). If
   none is present in your context, ask nothing and fall back to `/tmp`
   rather than blocking — a subagent that cannot find findings a working
   directory for is not a reason to fail the whole task.

2. Write your full findings (the same content and format your own agent
   definition already specifies) to a file in that directory, using a
   **quoted heredoc** so no shell substitution or quoting hazard applies:

   ```bash
   out="$(mktemp "<target-directory>/advisory-<your-agent-name>-XXXXXX.md")"
   cat <<'EOF' > "$out"
   <your findings, verbatim, exactly as your own Output section specifies>
   EOF
   ```

   `mktemp`'s `XXXXXX` suffix is what makes this collision-free under
   concurrency — two advisory subagents (even two of the same type) running
   at once each get their own file, no coordination needed. Substitute your
   own agent's name for `<your-agent-name>` (e.g. `preclose-inspection`,
   `pitch-inspection`) so the filename is self-describing.

2. **A run that finds nothing still writes a file** — one saying so
   explicitly (e.g. "No findings met the bar."). An absent file must never
   be the signal for "nothing found"; it must be reserved for "something
   went wrong," so the two are never ambiguous to whoever reads the
   scratchpad afterward.

3. Your final chat message — the thing that actually ends your turn — states
   only the file's absolute path and a short verdict line (your own agent
   definition's usual closing-line format still applies). Do not also repeat
   the full findings in that message; the file is the artifact, the message
   is a pointer to it.

## What this does not change

Every other instruction in your own agent definition — the bar for a
finding, what you must not do, tool restrictions, the closing verbatim line
— stays exactly as written. This convention only changes *how the findings
leave your session*, not what they contain or how they are produced.
