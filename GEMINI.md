# GEMINI.md — harmonic-forge

Entrypoint for **Gemini CLI** working in this repo.

**Read [`docs/agent-foundation.md`](docs/agent-foundation.md) first.** It is
the vendor-neutral foundation for this repo — what it is, what to read, and
the conventions that differ from the other Vital Harmony repos (no release
milestones; board #3; `F<N>`/`H<N>` shorthand; rules edits propagate to
every project).

Then read [`3-lane-protocol.md`](3-lane-protocol.md), which is the
operative protocol document.

Launch through the lane scripts (`LANE_CLI=gemini lane1`), not a bare
`gemini` invocation — the launcher supplies `GOOGLE_CLOUD_PROJECT` and unsets
`GOOGLE_API_KEY`/`GEMINI_API_KEY` so the session runs on the OAuth path. A
bare invocation from this repo fails with `ProjectIdRequiredError`, and one
with an API key in the environment silently runs on the wrong identity. See
`3-lane-protocol.md` § Per-CLI launch wiring.

This file is deliberately thin and is **not** a symlink to another
entrypoint — each agent's file is real so tool-specific notes can diverge
without duplicating the foundation (harmonic-forge#321).
