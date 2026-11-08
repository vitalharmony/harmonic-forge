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

## Failure store

`protocol-failures.jsonl` is the system of record for protocol failures
(`capture_failure.py`, harmonic-forge#520 and #1288): append-only, one slip per
record, each with a session and turn id and, where required, a failure key
(`--tool`, `--class`, `--probe`; the versioned class list is
`failure_classes.json`). An optional `--event <id>`
(`[a-z0-9][a-z0-9-]{0,31}`, secret-scanned, null when absent) groups the slips
of one bundled event: records sharing it within one session count once in
`fold_due.py` and D1, and every other record counts once. `failure_store.py` projects it into SQLite, a derived
store that is rebuilt, never migrated.

| what | where |
|---|---|
| the memory log | `~/Harmonic_Projects/operator-memory/protocol-failures.jsonl` |
| Lane 3's log | `~/Harmonic_Projects/testplan/protocol-failures.jsonl` |
| overlays (relabels) | `~/Harmonic_Projects/operator-memory/failure-overlays/*.jsonl` |
| the store | `~/Harmonic_Projects/operator-memory/failure-store.sqlite` |
| the schema | `tools/memory/failure_store.sql` (this repo) |

Schema: `meta` (the build's fingerprint and counts), `failures` (one row per
log line, keyed `(log, line)` with `line` the 0-based index), `overlays`, and
the `failure_keys` view, where an overlay's key wins over the record's own.
`failures` is indexed on `(tool, class, ts)`.

```bash
python3 ~/harmonic-forge/tools/memory/failure_store.py build
python3 ~/harmonic-forge/tools/memory/failure_store.py latest --tool l1_post --class cwd
```

`build` writes a temp file beside the store (`<db>.build-<pid>-<random>.tmp`)
and swaps it in. Builds of one store are serialized by an exclusive lock on
`<db>.lock`, so temp files left by earlier failed builds are provably abandoned
and are removed by that exact name pattern; no other file is ever deleted.
`latest` rebuilds first when a log or overlay changed (size and sha256 of each),
or the schema file or `STORE_VERSION` changed, since the last build, and skips the rebuild if another process finished one
while it waited for the lock. Neither opens a log for writing. `--db` must end
in `.sqlite`, `.sqlite3` or `.db` and must not be a log, an overlay or inside the
overlays folder. Exit codes: 0 found or built, 1 no stored failure for the key,
2 refused, 3 any other error (an unreadable or missing memory log, an I/O or
SQLite error, a record SQLite cannot bind, an error writing the result); only a
missing testplan log reads as empty. An overlay row is `{"log_line": N, "key_v1": "tool|class",
"probe"?: "...", "log"?: "memory"|"testplan"}`; files apply in name order, rows
in file order, the last match wins, and a row with an empty `key_v1` is skipped
and counted. Records are never rewritten.

**No failure data enters this repo, and no fixture copy of it either.**
harmonic-forge is public, and failure records hold internal issue text. No
record stores the command that failed: it is recovered from the session
transcript through `(session, turn)`, where `turn` is the capture turn (a slip
caught later passes its own `--turn`, or is found by searching back from it). `capture_failure.py --path` and `failure_store.py --db` refuse
a target inside a harmonic-forge checkout or worktree; `.gitignore` covers
`*.sqlite`, `*.sqlite3`, `protocol-failures*.jsonl` and `failure-overlays/`;
every test fixture is synthetic. Copying an overlay into
`operator-memory/failure-overlays/` is a local step, never a commit.

## Not here

The lesson-aging check (`first_seen:` / `instances:` / `promoted:` /
`allow_unpromoted.toml` and the 600-byte pointer rule) is **F500**, not this
issue. Zero files carry any of those fields today, so the check would gate on
nothing until F500's backfill populates them.
