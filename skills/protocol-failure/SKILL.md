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
  [--tool <id> --class <class> --probe "the check that would have caught it"] \
  [--event <id>] \
  --expected "what the protocol required" \
  --happened "what actually happened" \
  --account "the narrative"
```

`--session` defaults to `$CLAUDE_CODE_SESSION_ID` and `--turn` to the newest
`promptId` in that session's transcript. Either one unresolved refuses the
capture (exit 2, nothing written). **A Codex caller passes `--session` and
`--turn` itself, and so does a background subagent**: it shares the parent's
session id, so the transcript's newest turn is the parent's, not its own.
`turn` is the turn the capture ran in, not necessarily the slip's: a slip
caught in a later turn passes `--turn <the slip's promptId>`, or its command is
found by searching the transcript back from the capture turn.

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

## The failure key: `--tool`, `--class`, `--probe`

The key names the tool, the missing-precondition class, and the probe that
would have caught the slip. Every gate, lint or provenance refusal cites the
latest stored failure with the same key, and repeating keys are what the
improvement loop proposes registry changes from (harmonic-forge#1279).

- **Required** when `--rule-ref` names a precheck rule (`R-0419`), or when
  `--caught-by` is `hook` or `gate`. Otherwise optional.
- **All three or none.** A partial key is refused under every flag combination.
- **One slip per record.** Each key flag may be given once; a repeat refuses.
  A bundled event is one call per slip, each with the same `--event <id>`
  (`[a-z0-9][a-z0-9-]{0,31}`, given once, secret-scanned; the record's
  `event` is null when absent). Records sharing an `--event` within one
  session count once in the recurrence trigger and the D1 detector; every
  other record counts once. A turn is not an event: one operator prompt can
  span unrelated slips.
- **`--tool`** is on the interim closed list in
  `tools/memory/failure_classes.json` (`interim_tools`), or one of the two
  pseudo-tools `bash-string` and `operator-commands-block`. There is no
  `other:` escape for tools. `--tool none` is accepted only with
  `--class other:reasoning`: a reasoning error no tool would have caught, whose
  `--probe` names the check that would have (for example "re-read the issue's
  AC before claiming"). For a gate-caught code defect, `--tool` is the gate's
  tool (`mise-check`, `pytest`, `run_tests`) and `--probe` a one-line
  description of the failing check.
- **`--class`** is from the current class-list version, or `other:<name>`
  (`[a-z][a-z0-9-]*`, not an existing class). The improvement loop reviews
  `other:` classes. A published version is never edited; a change is a new
  version, and every record carries `class_list_version`.

Class-list version 1:

| class | the precondition the missing probe would have checked |
|---|---|
| `args` | the arguments have the shape the tool takes |
| `cwd` | the command runs from the right directory or repo |
| `freshness` | the input was read recently enough to still be true |
| `receipt` | a required receipt or preflight exists before the step |
| `target-state` | the target is in the state assumed (open, on origin, unlocked, not in use by another worktree) |
| `exists` | the thing named exists |
| `env` | the environment is as assumed (variables, account, CI green) |
| `local-check` | a fast local test or lint passes before the full check |
| `body` | the post or file body has the required shape and fields |
| `shell` | the shell string is safe (quoting, path variables, timeouts, one step per call) |
| `provenance` | a claim in a post was verified before it was posted |
| `owed-work` | owed work was done or reported before stopping |
| `policy` | the action is allowed (operator-only steps, raw writes, scan refusals) |

**The command that failed is not stored.** It is recovered from the session
transcript through the record's `(session, turn)`, which every record carries.
The turn is the capture turn: for a slip caught later, pass its own `--turn`, or
search the transcript back from the capture turn.

`--probe` is a short description, not a command: one line, at most 200
characters. It passes a blunt local scan (`tools/memory/secret_scan.py`) with
no shell awareness and no exemptions. Any of these refuses the capture (exit 2,
nothing written; the refusal names the kind, never the text): a credential
keyword anywhere, case-insensitive (`token`, `secret`, `password`, `passwd`,
`pwd`, `api_key`/`apikey`, `auth_key`, `private_key`, `authorization`), any `@`,
any `://`, a vendor token shape, a JWT header, a PEM private-key header, or a
32+ character mixed-case alphanumeric run. Rephrase the probe without it ("the
gh call ran under the wrong account", not the command). A secret with none of
those marks and under 32 characters is out of scope. A blank or
whitespace-only `--tool`, `--class` or `--probe` counts as absent.

## Where it writes

A parameter with a per-lane default. Lane 3 writes to
`~/Harmonic_Projects/testplan/protocol-failures.jsonl`, honoring its
write restriction; every other lane writes to
`~/Harmonic_Projects/operator-memory/protocol-failures.jsonl`, the store shared
across all repos and lanes. Override with `--path`. A path inside a
harmonic-forge checkout or worktree is refused: harmonic-forge is public, and
failure data never enters it.

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
