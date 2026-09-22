---
description: "The verification gate (lint, typecheck/build, mypy, restart -- whatever the repo declares) that must all pass before any committing restart. Invoke before running a committing restart or version bump."
---

# Verification gate

The procedure is platform-owned (harmonic-forge#708); the commands are the
repo's own, in `.claude/verification-gate.config.json`, validated against
`schema/verification-gate.config.schema.json`.

1. From the repo root (main checkout or worktree), print the gate:

   ```
   python3 ~/harmonic-forge/skills/verification-gate/gate_plan.py
   ```

   A non-zero exit means the config is missing or invalid. Report its message
   and stop; do not reconstruct the commands from memory.
2. If it names a policy doc, read it in full before running anything.
3. Run each command **as a separate tool call**, in the printed
   `working_dir`, exactly as printed. Never chain commands with `&&`, `||`,
   `;` or pipes. The `working_dir` is load-bearing: some tools only find
   their config from the right cwd, and the printed notes say which.
4. A command marked `only if changed` runs only when one of the named paths
   changed on this branch; otherwise skip it and say so.
5. If any command fails, fix it and re-run that command before the next. Do
   not skip steps, combine them, or commit broken code.
