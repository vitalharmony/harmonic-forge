---
name: _stub
description: harmonic-forge#169's proof-of-mechanism skill — not a real skill. Exists only to demonstrate that sync_rules.py can symlink a whole skill directory (including a nested subdirectory) into a project's .claude/skills/. Never distributed by default (see UNIVERSAL_SKILL_DIRS's own docstring) — opt in explicitly via --skill _stub for testing the mechanism.
---

# _stub

This skill exists only to prove the distribution mechanism harmonic-forge#169
built. If you're seeing this in a real project's skill listing outside a
scratch verification run, something opted it in that shouldn't have —
`UNIVERSAL_SKILL_DIRS` is empty by default, so this requires an explicit
`--skill _stub` flag to `sync_rules.py`.

Mirrors `frontend/src/verticals/_stub/`'s own "not a product X" framing in
HRSE2 (hrse#678) — the smallest thing that exercises the mechanism
end-to-end, nothing more.

A second file lives at `agents/openai.yaml`, alongside this one — proving the
directory-level symlink carries every file under it, not just `SKILL.md`.
