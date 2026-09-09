---
name: protocol-failure
description: Capture one protocol failure as a structured, append-only record — what was expected, what happened, how it was caught, and whether a rule already existed. Use the moment a failure is noticed, in any lane, before the context is gone. Do NOT use for a bug in the code being worked on; this is about the protocol, not the product.
---

# Protocol failure capture

One invocation, no network, no GitHub call. Cheap enough to run mid-task,
because a capture that derails the task is a capture that gets skipped — and a
skipped capture is indistinguishable from a clean session.

```bash
python3 ~/harmonic-forge/tools/memory/capture_failure.py \
  --caught-by <how> --rule-existed <what> [--rule-ref <id>] \
  --expected "what the protocol required" \
  --happened "what actually happened" \
  --account "the narrative"
```

Run it **when you notice**, not at the end. The two required fields are
answerable in the moment and much harder an hour later, which is the whole
reason they have never been recorded.

## A failure whose rule already existed is an ENFORCEMENT finding

This is the point of the `--rule-existed` field, and getting it wrong is how
the corpus grows while failures do not fall.

- **`none`** — nothing was written anywhere. An **authoring** problem. Writing
  a rule is the right remedy.
- **`rule`** or **`scoped_out`** — a promoted `R-` rule was in force and the
  failure happened anyway. An **enforcement** problem. **Writing another rule
  is useless and actively harmful here**: it adds bytes to a corpus every
  session must load, in exchange for a rule that has already been demonstrated
  not to bind. The remedy is a hook, a refusal, or a mechanical gate.
- **`unpromoted`** — a memory file exists and was never promoted. The
  promotion rule (2 instances or 14 days, harmonic-forge#493) has not been
  applied.
- **`prose`** — written in a doc or a handoff but never as a rule. Closer to
  `none` than to `rule`: nothing was ever going to fire.

The proof case is `feedback_git_er_done_bias`: **7 instances, already promoted
to R-0344, still recurring.** A rule was in force the entire time. Nothing in
the current setup surfaces that, and the promotion rule's blind spot is that it
treats promotion as the end state. Recurrence *after* promotion is the signal
that says words will not fix it.

## `self` and `self_late` are different, and the line is bright

| value | meaning |
|---|---|
| `hook` | a PreToolUse/PostToolUse hook denied or flagged it |
| `gate` | a Lane 3 gate, a checker, or a test suite caught it |
| `another_lane` | a different lane read the work and found it |
| `operator` | the human noticed and said so — **a detection gap** |
| `self` | the acting session caught it **before any artifact left the session** |
| `self_late` | the acting session caught it **after** an artifact left |

An artifact leaving means a posted comment, a commit, a pushed branch —
anything another party could already have read. Before that line, the process
worked. After it, the process did not work and the session happened to notice
before anyone else did. Collapsing the two loses exactly the
generous-self-reporting signal this distinction exists to preserve, so when it
is genuinely ambiguous, it is `self_late`.

`operator` is not a neutral value. Every significant failure in the three lane
belt dumps was caught by the operator asking a question and not one by a
mechanism. Each `operator` record is a claim that the machinery had its chance
and missed.

## Where it writes

A parameter with a per-lane default. Lane 3 writes to
`~/Harmonic_Projects/testplan/protocol-failures.jsonl`, honoring its
write restriction; every other lane writes to
`~/Harmonic_Projects/operator-memory/protocol-failures.jsonl`, the store shared
across all repos and lanes. Override with `--path`.

Append-only. There is no rewrite path in the writer, and no write-time dedup:
deciding two records are the same event is an analysis-time judgment, and
making it at write time would silently drop a real recurrence — the exact
signal the log exists to preserve.

## This does not replace a memory file

A failure record is the **raw event**; a memory is the **distilled lesson**.
Write the record now. Whether it becomes a memory, and whether that memory
becomes a rule, is a separate human decision — this skill records and never
promotes.

## Absence is a finding

A session with zero failure records is **not clean, it is unaudited.** A log
written only when someone remembers is subject to the same failure mode it
exists to record, and the mitigation is to say so plainly rather than to design
around it. Any analysis over this log must report a green zero as an absence of
data, never as an absence of failures.
