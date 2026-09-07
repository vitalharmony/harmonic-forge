---
name: memory-triage
description: Classify every feedback memory in the shared store against the rule registries and emit promotion dispositions — DUP / FOLD / FOLD+HOOK / STALE / LOCAL / STATE, with a target file and draft rule text for each FOLD. Use when the store has grown past what a session can afford to load, when preparing a promotion batch, or on the operator's explicit trigger. Read-only: it proposes, it never edits the store or files an issue.
---

# memory-triage

The memory store is loaded into every session. It reached 238 files before
anyone looked, and pruning it was a manual campaign that redid the same
classification from scratch each time (hrse#458, F385-F389). This makes the
mechanical half repeatable so the judgment half is the only part that costs
anything.

## Step 1 — run the classifier (always first, no subagent)

```
python3 ~/harmonic-forge/tools/memory/memory_triage.py
```

`--counts` for the class totals alone, `--json` for the rows, `--store` and
`--repo-root` to point it somewhere else. It is read-only on the store, on both
repos, and on GitHub.

Output is six classes, actionable ones first:

| class | what it means | your job |
|---|---|---|
| `FOLD+HOOK` | recurring lesson, no rule, trigger is a tool call | draft rule text **and** name the hook event |
| `FOLD` | recurring lesson, no rule, prose-enforced | draft rule text |
| `DUP` | already promoted to a rule that exists | shrink the file to a pointer |
| `STALE` | cites a path that exists in no repo and no store | verify, then delete |
| `LOCAL` | operator context, not a general lesson | leave it |
| `STATE` | ongoing work state | leave it; it ages out on its own |

## Step 2 — what the script decided, and what it deliberately did not

**Decided mechanically, trust it:** `LOCAL`, `STATE` and `DUP`. All three come
from the file's declared `metadata.type` or a `promoted:` marker resolving to a
real `R-` ID. There is no inference in any of them.

**Ranked, not decided — this is your job:** every `FOLD` row carries up to three
candidate rules with scores. **The score ranks; it does not separate.** Measured
over the live store, the top score and the median differ by about 0.1 with no
gap in between, because the corpus is one protocol and everything in it is about
the same subject. So:

- Treat the top candidate's **file** as a reliable signal — that is where the
  rule text most likely belongs.
- Treat the top candidate's **rule** as a question, not an answer. Open it. If
  it already says what the memory says, this is a `DUP` the script could not
  prove: reclassify it by hand and record the `R-` ID.
- A `FOLD` with no candidates is not necessarily novel. It may just use
  different words. Search the rules for the *behavior*, not the phrasing.

**Never add a similarity threshold to the script to skip this step.** That was
the first implementation. It called 103 of 209 memories duplicates, including a
scheduling-link memory matched against an AE-sweep rule. The reasoning is
recorded at `CANDIDATES` in the source; read it before proposing a cutoff.

## Step 3 — the filing bar applies, and the script marks it

Each `FOLD` row is marked `file` or `batch` from its `instances` count against
the bar's test 2 (`universal-agent.md` § THE FILING BAR):

- **`file`** — 2+ recorded recurrences. Write the ready-to-file body: problem,
  scope naming the exact target file and section, acceptance criteria, and the
  recurrences as evidence.
- **`batch`** — a single instance. **Do not file it.** Fold it into the next
  promotion issue. A true observation nobody will action is not a reason to
  file, and 37% of the open backlog was already work about the work when this
  was measured.

`instances` comes from the frontmatter `backfill_frontmatter.py` populates
(harmonic-forge#500). Loose recurrence language ("again", "repeatedly") is
scored 1 on purpose — scoring it would manufacture promotion obligations the
prose never recorded. If you believe a row has recurred more than its count
says, correct the frontmatter first and re-run; do not argue past the number.

## Step 4 — drafting rule text for a FOLD

Two lines maximum, in the target file's own voice, wrapped in the
`<!-- R-NNNN -->` markers the registry reads. Then:

```
python3 ~/harmonic-forge/tools/rules/check_rule_drift.py --next-id   # the registry is its own allocator
python3 ~/harmonic-forge/tools/rules/check_rule_drift.py             # after adding the registry entry
python3 ~/harmonic-forge/tools/rules/query_rules.py --duplicates
```

`--duplicates` is the check that matters here: it is the mechanical version of
the judgment Step 2 asked for, run against the text you actually wrote rather
than the memory you started from.

For `FOLD+HOOK`, name the hook event (`PreToolUse`, `SessionStart`, `Stop`) and
the concrete trigger. A rule with no mechanical trigger is a `FOLD`, not a
`FOLD+HOOK` — say so and move it rather than inventing a hook that would fire
on nothing.

## Step 5 — the shrink and the marker land together

A promoted file must carry `promoted: R-NNNN` **and** be under 600 bytes, and
`memory_lint` checks both. Setting markers in one commit and shrinking in a
follow-up turns the lint red on every intermediate commit. Same commit, every
time (harmonic-forge#500 AC5).

For a row that is genuinely un-promotable — a judgment no rule can express as a
predicate — add it to `tools/memory/allow_unpromoted.toml` **with a non-empty
reason**. An empty reason does not exempt.

## What this skill does not do

- It does not edit the store, delete a memory, or write frontmatter. Every
  disposition is a proposal.
- It does not create, close, or comment on a GitHub issue.
- It does not decide that a memory is a duplicate. It ranks; you decide.
