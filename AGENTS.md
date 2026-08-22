# AGENTS.md — harmonic-forge

Entrypoint for **Codex and any other agent reading AGENTS.md** working in this repo.

**Read [`docs/agent-foundation.md`](docs/agent-foundation.md) first.** It is
the vendor-neutral foundation for this repo — what it is, what to read, and
the conventions that differ from the other Vital Harmony repos (no release
milestones; board #3; `F<N>`/`H<N>` shorthand; rules edits propagate to
every project).

Then read [`3-lane-protocol.md`](3-lane-protocol.md), which is the
operative protocol document.

This repo ships no application code and has no build or test suite to run
before a change — verification here means the tooling's own tests
(`tools/hooks/test_*.py`, `tools/run_tests.py`) and live behavior, not a
compile step.

This file is deliberately thin and is **not** a symlink to another
entrypoint — each agent's file is real so tool-specific notes can diverge
without duplicating the foundation (harmonic-forge#321).
