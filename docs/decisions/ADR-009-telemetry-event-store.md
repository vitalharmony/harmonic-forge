# ADR-009: the telemetry event store — append-only files + DuckDB, private repo, partitioned by account and org

**Date:** 2026-09-30
**Status:** Accepted
**Decider:** Marc Mangus (platform owner)
**Resolves:** harmonic-forge#828 (F637 T0).
**Governs:** harmonic-forge#637 and every telemetry child (T1–T6), and
harmonic-forge#635, which shares the store (F637 AC6).
**Binding source:** `~/Harmonic_Projects/reports/factory-design-2026-09-29/OPERATOR-RULINGS.md`
(2026-09-29). Where this ADR and that file disagree, the file wins.

## Context

F637's premise is "capture events, derive metrics". A question asked later
can be answered only if its events still exist. harmonic-forge#826 stopped
the losses with a gzip JSONL archive. This ADR decides where events live from
here on and in what shape, so that every extractor (T1, T2, T4, T5) writes one
schema and every question is a query (T3), never a new extractor.

`design.md` §2 compared four options. The operator ruled on them on 2026-09-29.

## Decision

1. **Storage: append-only files plus DuckDB views** (design.md §2, option A;
   OPERATOR-RULINGS D1/D2). There's no database server. DuckDB reads the files
   directly, and DevLake (hrse#986/#987) is fed CSV exports from this store,
   never used as a second collector.
2. **Location: a private repo, `vitalharmony/telemetry-store`,** checked out
   at `~/Harmonic_Projects/telemetry-store`. It's not in harmonic-forge, which
   is public, and not in HRSE2, a product repo whose boundary kenekted events
   would cross.
3. **Layout: partitioned by account and org, then source and month:**
   `events/<account>/<org>/<source>/<YYYY-MM>.jsonl`. A client or venture
   partition can then be excluded, exported or deleted as a unit. Events that
   can't be resolved go to `events/unresolved/unresolved/…` and are never
   dropped.
4. **Schema v1** (`tools/telemetry/schema_v1.json`): the `events` columns of
   design.md §3, **plus `org`**, which is required, as are `account` and
   `repo`, per the rulings. `event_id` is `sha256(source, subject_id,
   event_type, ts)`, so re-extraction is idempotent: `emit()` skips an
   `event_id` that already exists in its partition.
5. **Payloads are ids, kinds, timestamps and hashes. Never bodies.** Comment
   text, email text, quotes and secrets don't enter the store, so even a leak
   exposes only structure.
6. **Registry-driven, per-account credentials.** Every extractor walks
   `projects.toml` through `tools/onboard/manifest.py` and authenticates each
   repo through `apply_project_identity`. Adding a repo or account means
   onboarding it, and nothing else.
7. **The harmonic-forge#826 archive is ingested, not replaced.** Its v1
   envelopes are loaded into the store by `emit.ingest_archive()`. The
   envelope's `record_hash` is the dedupe key, and the archive stays in place
   as the loss-stopping buffer.
8. **Rendering stays in the existing static pipeline** on
   `board.cymagraph.ai`, behind Cloudflare Access (OPERATOR-RULINGS §
   Correction). Evidence.dev is considered only when a third chart type is
   needed (T6).

## Deliberately deferred

A data-privacy statement for dashboard access beyond the operator. The
operator ruled it out for now (the operator is the only user), and it must be
revisited before a second user or a client gets access.

## Consequences

- A new question means a new `.sql` view (T3). No extractor changes.
- A store leak exposes structure, not content.
- The store repo must stay private. Changing its visibility is an operator
  decision.
