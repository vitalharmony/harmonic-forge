# transaction-log view, rendered from git at read time

A short, recent-history context primer for agents. It is not a changelog
and not a replacement for `git log`. **There is no `transaction-log.md`.**
The view is derived from git each time it is read (harmonic-forge#883).

## Why this exists

A session-bound agent starting fresh has no memory of what other sessions
committed recently unless it re-derives it from `git log`, which is easy
to skip. The view hands it that summary at session start without a query.

## How it is delivered

- **Session start:** `tools/hooks/transaction_log_context.py` is a
  SessionStart hook. It emits the view as
  `hookSpecificOutput.additionalContext`, capped at 40 entries / 6000
  characters by default, and exits 0 with no output on any failure, so it
  never blocks a session.
- **On demand:** a project's `mise run transaction-log [--all] [--out <path>]`
  task calls `transaction_log.py`. `--all` drops the caps; `--out` writes a
  file (for example, a scratch file for release-notes synthesis).

A repo opts in by registering the hook in its own `.claude/settings.json`
and adding the task to its own `mise.toml`, each with its own boundary.

## The renderer

`transaction_log.py` exposes `render(repo, boundary, max_entries=None,
max_chars=None) -> str` and a CLI. It makes one
`git log --first-parent --shortstat` pass. Each entry is `## <subject>`
plus the commit's shortstat line. Bodies and trailers (`Co-Authored-By`,
`Claude-Session`) are dropped. Entries are newest first. When a cap
truncates, a final line names how many older entries were omitted.

Boundaries:

| Boundary | Starts at | Used by |
|---|---|---|
| `recent:<N>` | the last N first-parent commits on HEAD | harmonic-forge (no version to bump) |
| `version-minor:<relpath>` | the commit that introduced `"version": "X.Y.0"` in `<relpath>`, where X.Y is read from **HEAD's committed copy** | HRSE2 (`frontend/package.json`) |

`version-minor` details, each one found by review rather than chosen for
taste:
- It reads the version from `HEAD:<relpath>`, never the working tree. A
  bump pass writes the next version before any commit exists, and a
  working-tree read would ask for an X.Y.0 that no commit has introduced.
- `git log -S` also matches the commit that later *removed* the string. The
  renderer takes the oldest hit and asserts that the commit's version is
  X.Y.0, raising `ValueError` otherwise.
- Patch bumps never move the boundary. HRSE2's earlier `find_boundary`
  anchored on the *current* patch version, so its view showed only the 0–2
  commits since the last patch bump.
- An unknown or unresolvable boundary raises. It never falls back to the
  full history.

The summary stays deterministic, with no LLM call. That was evaluated and
rejected earlier: a local 2B model failed about 25% of the time, and each
failure fell back to the diffstat anyway.

## Why the committed file was dropped

Every committed variant of this view failed in a new way, because a
summary of git history committed back into git is stale as soon as the next
commit lands, and two branches that write it collide:

| Occurrence | Symptom |
|---|---|
| hrse#242 | an append written after the commit left the tree permanently dirty |
| harmonic-forge#376 | every pair of concurrent branches conflicted at the fixed anchor line, papered over with a `.gitattributes` union merge driver |
| hrse#1511 | the append missed every PR-squash merge (zero entries for a whole release's final push) |
| hrse#1529, hrse#1853 | a regenerated, committed view needed its own regen-only commits |
| hrse#1764 | sessions trusted a stale committed copy, which needed a staleness detector |

Rendering at read time removes the whole class: nothing is tracked, so
there is nothing to conflict, clear, regenerate or detect as stale.
`transaction-log.md` is in `.gitignore` so a stray local copy is never
committed again.
