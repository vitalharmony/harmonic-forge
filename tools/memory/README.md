# `tools/memory/` — memory-store lint

Lints the shared operator memory store that `autoMemoryDirectory` points every
repo at (harmonic-forge#493 Phase 0). Read-only: no run modifies any file under
the store.

## Running it

```bash
python3 ~/harmonic-forge/tools/memory/memory_lint.py            # report-only, exits 0
python3 ~/harmonic-forge/tools/memory/memory_lint.py --gate     # exit 1 on gating findings
python3 ~/harmonic-forge/tools/memory/memory_lint.py --store PATH
```

The first output line is always the resolved store path.

## Why `--gate` never runs against the live store

The store is grown by the harness, not by any branch. A live-pinned exit-1
check is one that **no commit can turn green** — `MEMORY.md` rises on its own as
sessions write memories, so it would eventually fail `mise run check` with no
code change capable of fixing it.

So the wiring is split:

| runner | store | flag |
|---|---|---|
| `mise run check` (harmonic-forge) | `testdata/clean/` | `--gate` |
| `mise run hygiene` (hrse) | live store | none (report-only) |
| SessionStart hook | live store | none (report-only) |

`testdata/conventions/` is a synthetic fixture the tests use to pin exact
counts. It is **not** what the gate runs against — it carries one deliberate
orphan.

**A frozen copy of the real store is NOT in this repo, and must not be.**
harmonic-forge#494 originally specified one. `harmonic-forge` is PUBLIC and the
operator memory store holds a file documenting a live Keycloak test credential
alongside investor context, resume canon, customer-naming rules and tenant
priorities, so committing it would be a first publication that cannot be undone.
Lane 1 amended the acceptance criteria accordingly on 2026-09-06.

`testdata/conventions/` reproduces the structural conditions that fixture existed
to pin — one orphan, both naming conventions resolving, one issue reference —
with no operator content, and cannot drift when the live store is next edited.

## SessionStart hook — operator install

`~/.claude/settings.json` is untracked and operator-owned, so this entry cannot
arrive by merge. Merge it by hand, following `tools/hooks/user_prompt_tasklist.json`'s
precedent:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${HOME}/harmonic-forge/tools/memory/session_start_summary.py\""
          }
        ]
      }
    ]
  }
}
```

`"matcher": "startup"` deliberately excludes `compact` — a compaction already
injects `compaction_marker.py`'s payload, and a second memory line there is
noise at the moment context is scarcest.

The script always exits 0 and always emits. That is not defensive habit: on a
non-zero exit the harness **discards the output**, which would leave a hook that
is wired, runs, and says nothing while looking healthy.

## Checks

| # | Check | Gating under `--gate` |
|---|---|---|
| 1 | Orphan files (not linked from `MEMORY.md`) | yes |
| 2 | Dead `MEMORY.md` links | yes |
| 3 | Stale project memories (past dates near deadline words) | no |
| 4 | Broken `[[slug]]` refs, plus the slug-collision guard | yes |
| 4b | `[[repo#123]]` issue references | no — report-only |
| 5 | Missing frontmatter | yes |
| 6 | `MEMORY.md` load cap (200 lines / 25 KB, warn at 80%) | yes |
| 7 | Staleness: dead `mise run` tasks, script paths, `R-NNNN`, retired board fields | no — report-only |

### Check 4 and the two naming conventions

Filenames are `feedback_foo_bar.md`; the harness instructs sessions to write
`name: <short-kebab-case-slug>`, so `name:` values are `feedback-foo-bar` — and
sometimes a *shortened* variant of the stem. Before this was handled, Check 4
reported 119 findings across 62 targets, all false.

The lint normalizes (`-` → `_`, lowercased) and accepts a link that matches the
filename stem, its normalized form, or the normalized `name:`. This is safe
because **Check 4 is an existence test against a set union, not a resolution to
a unique file** — it never selects a file, so it cannot select the wrong one.
Because the shortened-`name:` convention makes a genuine collision reachable,
two files normalizing to the same key is itself a gating finding.

## Not here

The lesson-aging check (`first_seen:` / `instances:` / `promoted:` /
`allow_unpromoted.toml` and the 600-byte pointer rule) is **F500**, not this
issue. Zero files carry any of those fields today, so the check would gate on
nothing until F500's backfill populates them.
