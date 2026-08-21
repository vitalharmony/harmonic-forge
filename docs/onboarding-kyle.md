# Onboarding Addendum — Kyle (CymaGraph / HRSE2)

Read `../3-lane-protocol.md` and `../harmonic-forge.md` first. This file covers
only what's different for HRSE2 specifically, or still pending from your
earlier onboarding.

## Still Pending From Original Onboarding

Per prior notes: NDA/GitHub/email access is done. Still outstanding —
`.env` + Neo4j DB dump share, and Google Cloud setup. Ping Marc directly for
these; they're out-of-band (the DB dump is gitignored and shared separately
from the repo per `setup/first_time_setup.md`).

**When you do the `.env` setup (Section 4a of `first_time_setup.md`): as of
#175, `SESSION_SECRET_KEY` has no insecure fallback anymore — the backend
won't boot without it, and it's per-machine (generate your own, don't copy
Marc's). The exact command is in that section.**

## HRSE2-Specific Overrides You Need to Know

These are real deviations from the generic platform description — don't be
surprised by them:

1. **Service lifecycle is `mise` tasks, full stop.** `hrse_manager.py` and
   its five `manager_*.py` modules are **retired** (epic #224 closed
   2026-07-13, final cutover in hrse#237) — the files no longer exist in
   the repo. Never run `uvicorn`, `npm run dev`, or `git commit`/`push`
   manually on this repo, even to save time. If a `mise` task itself is
   broken, the fix is to fix the task, not route around it.
   ```bash
   mise run restart              # normal restart, bumps + commits
   mise run restart --no-bump --no-git   # tooling/test-only
   mise run check                # verification gate (backend mypy + frontend build)
   mise run bump a|b|c           # version bump only
   mise run gh-new-issue --title "..." --labels "bug"
   mise run containers-up/down/status [--service X]
   mise run dashboard
   mise run db-heal
   mise run post-comment --issue N --file <path>   # GitHub comments — never gh issue comment directly
   ```
   You are single-account on `vitalharmony`, so this mostly won't bite you —
   but if you ever work across two GitHub accounts, **never `gh auth switch`**
   (it is global and will change the active identity for every other session
   on your machine). Use `harmonic-forge/tools/gh/gh-as` instead; see
   `harmonic-forge.md` §6.
   Full task list: `mise tasks` from the repo root, or read `mise.toml`
   directly. `mise run restart` returns control immediately (it daemonizes
   the stack under `process-compose`, same as the old tool's behavior) —
   if it appears to hang, something is genuinely wrong, don't assume it's
   just slow. A local git hook (`.githooks/pre-commit`) rejects any commit
   made while on `main` — work on a branch, always (see hrse#243).
2. **Neo4j/Cypher rules are HRSE2-local, not platform-universal.**
   `.claude/rules/cypher.md` stays as an HRSE2-specific file — it is not
   symlinked from `harmonic-forge` because most other Vital Harmony projects
   don't use Neo4j. Parameterized-Cypher discipline still applies, it's just
   documented locally instead of centrally.
3. **The 300-line cap and LLM-gateway pattern are real and enforced** —
   `app/services/llm_gateway.py` is the only place that talks to the model
   provider; `src/api/client.ts` (`apiClient`) is the only place the
   frontend talks to the backend. These match the platform's universal
   rules exactly, so nothing extra to learn there.
4. **Bug fixes/features go through the 3-lane loop as normal** — you are
   Lane 2 (Implementer) taking Lane 1 (Claude Code, or another tool per
   harmonic-forge#317's multi-agent parity effort) handoffs, gated by Lane 3
   before merge.
5. **Don't just pull whatever `tech-debt`-labeled issue looks interesting —
   check `docs/PRIORITIES.md` first** (HRSE2 repo root). It's the canonical
   cross-repo (hrse + harmonic-forge) sequencing doc — what's actually next,
   with the reasoning, and what's deliberately parked and why. **The doc
   does not drive the boards, and never did in the way this section used
   to describe** (hrse#839): `board_sync.py` and the `Priority`/`Sequence`
   board fields it kept in sync are all **deleted**, not merely out of sync
   — `Status` (Todo → In Progress → Done) is the one board field a human
   actually maintains, and it carries the Kanban. The doc holds the
   reasoning a board's fields structurally can't; it is not a mirror of
   board state and was never meant to be re-derived from it. If something
   you're about to pick up isn't mentioned in the doc, flag it to Marc
   rather than assuming it's fine to start — it may be real drift, or a
   deliberate cut you're about to undo.

## Setup Steps

1. `git clone https://github.com/vitalharmony/harmonic-forge.git ~/harmonic-forge/`
2. Clone/pull the HRSE2 repo. `.claude/rules/backend-python.md` and
   `frontend-typescript.md` are now symlinks (issue #6 migration landed) —
   **but right after your clone, they'll point at Marc's machine**
   (`/home/mmangus/harmonic-forge/...`), because that's the absolute path baked
   into the symlink when it was committed. This is expected, not broken.
3. Immediately run, from the HRSE2 repo root:
   `python3 ~/harmonic-forge/sync_rules.py --project .`
   This detects the symlinks point somewhere other than your own
   `~/harmonic-forge` checkout and relinks them to your machine. Confirm with:
   `readlink .claude/rules/backend-python.md` — it should print a path under
   *your* home directory, not `/home/mmangus/...`.
4. Follow `setup/first_time_setup.md` in the HRSE2 repo for environment
   setup (backend `.venv`, frontend `npm install`, `.env`).
5. Read `README.md` and `hrse.md` in the HRSE2 repo for the domain/graph
   ontology before touching any ingestion or graph code.

## If You Ever See `.claude/rules/backend-hrse2.md` / `frontend-hrse2.md`

These are real files, not symlinks — they hold HRSE2-only additions layered
on top of the platform's universal rules. Don't delete or symlink these;
they're intentionally local-only and not part of the platform sync.

`frontend-hrse2.md` currently holds three real, enforced rules — not just
the "no React Router" one:

1. **No React Router** — `App.tsx` owns a single `activeTab` state.
2. **Data fetching — migrate opportunistically.** TanStack Query is the
   adopted caching/refetch layer (see `ADR-012-TanStack-Query-Data-Fetching.md`
   in the HRSE2 repo's `docs/architecture/decisions/`). If you're editing a
   component for any reason and it has a hand-rolled `useEffect` +
   `useState(loading)` fetch, convert it to `useQuery`/`useMutation` as part
   of that same change — don't leave it, and don't go looking for other
   components to convert either. Same "fix it when you're already there"
   discipline as the 300-line cap.
3. **Component layer — shadcn/ui, same policy.** See
   `ADR-013-shadcn-ui-Component-Layer.md`. If you touch a component with a
   hand-rolled modal, native `<select>`, dropdown, or toast, convert it to
   the matching shadcn/ui primitive in the same change. Same opportunistic
   rule as #2 — no dedicated sweep.

**Read `docs/architecture/decisions/` in the HRSE2 repo before assuming a
rule you don't understand is arbitrary** — every ADR there explains the
reasoning (what was measured, what alternatives were rejected, and why) for
exactly this kind of "convert when touched" rule. If a rule in
`frontend-hrse2.md` or `backend-hrse2.md` references an ADR by name, read
that ADR before pushing back on the rule or asking Marc about it.

## Brand Assets — Use Whenever a UI Issue Touches Branding/Theming

CymaGraph has a locked brand mark and app-wide theme token set. Any UI
issue that touches the favicon, app icon, color tokens (`index.css`
`:root`/`.dark`), or the mark itself should pull from here — don't
improvise a color or regenerate the mark by hand.

**Google Drive:** https://drive.google.com/drive/folders/1jXlR2Je3KFtHtRkrHMQyeMtF3twA-qH7
(already shared with you directly, plus a link — should already be in
your Drive/inbox)

**Local path on Marc's machine** (Drive-synced — this exact absolute path
won't exist on yours):
```
/home/mmangus/Google Drive/Vital Harmony/Harmonic Architect/CymaGraph (HRSE)/Brand Assets/
```

**Start with `brand-guide.md`** in that folder — a plain-text export of
the full locked guide (construction geometry, color tokens with exact
`oklch` values, typography, lockup rules, file inventory, App/Play Store
submission rules). It exists specifically so you don't need live Google
Docs access to read it — the original is a Google Doc
(`CymaGraph — Brand and Identity Guide (v2, locked).gddoc` in the same
folder is just a shortcut to it, not the content); the `.md` export is
the one to actually read.

Key things to know before touching anything brand-related:
- **The mark is parametric, never hand-edited.** If a mark asset needs
  regenerating, use `cymagraph-asset-generator.py` in that folder — never
  edit an SVG's paths directly.
- **Ignore `Archive — superseded (2026-07-26)/`** — abandoned earlier
  design, kept for history only.
- **§ 7 of the guide has the exact app theme tokens** (`--primary`,
  `--accent`, etc., both light and dark) already in copy-pasteable CSS
  form — this is what hrse#400 applies to `frontend/src/index.css`.
- If a handoff references brand assets and doesn't already name the
  exact file(s) you need, check the guide's § 8 file inventory before
  asking Marc — it's usually already answered there.

## New Since 2026-07-09 — Protocol Mechanisms You'll See on Real Issues

Landed 2026-07-12 through 2026-07-13, driven directly by real incidents
(HRSE2 #233 burned ~10 review rounds and a full day's Devin credit budget
before the pattern was named; #236 later showed the first Plan-First fix
wasn't enough on its own — see item 3). Read `docs/decisions/ADR-002`
through `ADR-005` in this repo for the full incident records — summary of
what changes your day-to-day work as Lane 2:

1. **Three new Fable subagents exist, invoked by Lane 1, not by you** —
   `agents/product-strategy.md` (HRSE2-local, architecture/strategy
   judgment calls), `agents/sticky-wicket.md` (reactive: fires after **2**
   consecutive FAIL/declined-completion rounds on the same issue — lowered
   from an original 3 — reads the whole thread fresh and asks whether the
   *approach* is wrong, not just the latest bug), `agents/pitch-inspection.md`
   (proactive: reviews Lane 1's handoff *before* it's posted to you, and —
   new as of ADR-004 — reviews *your* implementation plan too, see below).
   You'll see their verdicts show up as comments on issues; nothing for
   you to invoke yourself.
2. **`templates/lane1-handoff.md` now has three mandatory fields** you'll
   see on every handoff: *Design Alternatives Considered*, *Load-Bearing
   Assumptions*, *Delegated Judgment Calls*. Read all three before
   implementing — "Delegated Judgment Calls" specifically means Lane 1 is
   deliberately leaving a design decision to you.
3. **Plan-First Implementation (ADR-004, enforcement fixed by ADR-005 —
   read this one carefully, it changed after a real failure on your own
   work).** If a handoff's Delegated Judgment Calls field is non-"none,"
   or your implementation itself mutates git state or live data,
   Plan-First applies. Original incident (#233): you posted a plan
   unprompted, got a solo Lane 1 read that approved a design which then
   generated ~10 rounds of recurring bugs — ADR-004 fixed the *review*
   (fresh `pitch-inspection` context instead of a solo Lane 1 read).
   **Second incident (#236): even with an explicit, bolded, three-times-
   repeated written instruction to stop and plan first, you implemented
   809 lines straight to `main` before any plan was posted or reviewed.**
   ADR-005's fix removes the choice instead of asking you to remember it
   correctly: for a Plan-First issue, **the handoff you receive has no
   Implementation Spec section** — it's withheld on purpose, so there's
   nothing to implement from yet. The trigger is **"Plan #N"**, not
   "Implement #N" — post your plan as a comment and stop. After a
   `pitch-inspection` verdict (PROCEED / PROCEED WITH NAMED CHANGES /
   REFORGE), Lane 1 posts a **follow-up comment with the actual
   Implementation Spec**, and only then does "Implement #N" arrive — wait
   for both messages, not just the first. Implementing before that
   follow-up spec exists is a filed protocol violation, not a shortcut
   (see `rules/universal-agent.md` § STANDING-RULE VIOLATIONS GET FILED;
   the #236 incident is harmonic-forge#50).
4. **Tooling Exception:** dev/test tooling issues (never application code,
   never shipped, labeled `infrastructure`/`tech-debt`) can skip the full
   3-lane loop — single implementer, one human-reviewed pass, no per-round
   Lane 3 gate. Only applies when the issue is explicitly scoped that way;
   don't assume it for anything touching application source.
5. **GitHub comment formatting — this one's about you directly.** Your
   completion comments arrived mangled/swallowed three times (#234, #235,
   #236 — heredoc/nested-backtick escaping collisions, once bad enough to
   inject stray `gh --help` output into the comment). Three recurrences of
   the same rule despite it being written down twice is proof a written
   rule isn't sufficient here. **Never invoke `gh issue comment` directly
   on this repo — use `mise run post-comment --issue N --file <path>`
   instead.** It writes the body to a file, posts via `--body-file`,
   refetches the exact comment it just posted, and fails loudly if the
   content doesn't match byte-for-byte — the self-check is mechanical now,
   not something to remember at the moment of posting.
