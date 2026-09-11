# Cross-family advisory review — the one mechanism

<!-- harmonic-forge#448 built it for `pitch-inspection`; harmonic-forge#598
     made it shared and gave it a second consumer. -->

**Read this file before taking a cross-family branch.** It is the whole
mechanism. An advisory agent's own file carries only its *trigger* — when its
particular job warrants a second family — and never a second copy of what is
here. Two copies is precisely how the mechanism broke once already: the deny
hook's permitted invocation and `cross_family_call.sh`'s own argument contract
each stayed green in their own test suite while, together, permitting only a
shape that could not run (harmonic-forge#598).

## Why a second family at all

Fresh context removes one bias — the maker cannot grade their own design
(ADR-002). It does not remove **shared priors**: the same model family brings
the same training, the same blind spots, the same taste in what counts as an
obvious design. Measured, not theoretical: harmonic-forge#590, #594 and #596
were three consecutive rounds of in-family review on one artifact, each
finding an implementation defect, none asking the design question the operator
asked in one line.

So the second family is not a second opinion. It is a check on the claims the
first family had no reason to doubt.

## The call — one shape, and it is the only one permitted

<!-- R-0358 -->
```
~/harmonic-forge/tools/lane/cross_family_call.sh --caller claude --families 2 \
    --posture verify --brief <brief path> --cwd <scratch dir>
```

`deny_advisory_subagent_gh_writes.py` permits exactly this argument sequence
and nothing else — a different posture, a third family, a reordering, or one
extra token is denied. Do not attempt to work around a denial; report it.
<!-- /R-0358 -->

**`--cwd` is the repository checkout the artifact lives in.** Not a scratch
directory — that instruction was wrong when first written here and is the
defect this paragraph exists to prevent (harmonic-forge#598 preclose finding
1). It is true that the choice grants no *reach*: `verify` runs under
`--sandbox read-only`, so the reviewer can read what it could read from
anywhere. It is false that the choice does not matter. `codex exec -C <cwd>`
starts the reviewer there, so from an empty directory `git log/show/diff` —
which the reviewer's own contract instructs it to run — all fail, `gh issue
view` has no repo context, and every relative path in the brief resolves to
nothing. Every verdict comes back `uncheckable`, the call exits 0 with
`status: ok`, and the one pass is spent having checked nothing.

## The brief — build it, do not write it

```
python3 ~/harmonic-forge/tools/lane/build_cross_family_brief.py \
    --artifact <path> [--artifact <path> …] \
    --intent "<what the artifact is trying to achieve>" \
    --question "<the specific question>" \
    --assumption "<an asserted, unverified claim>" [--assumption …] \
    --evidence "<a path or command that checks one>" [--evidence …] \
    --out <brief path>
```

It refuses to write a brief missing any of the five, naming all the gaps at
once. That refusal is the point: `verify` returns one verdict per asserted
assumption and nothing else, so a brief with no assumptions spends the call
and produces nothing — and there is no second call.

`--evidence` is the half that is easy to skip and expensive to skip. Embedding
the artifact makes the brief *readable* cold; it does not make the assumptions
*checkable*. The paths resolve from the `--cwd` above, which is why the two
must agree.

**What the brief must not carry:** your reasoning for the assumptions, or your
opinion of the artifact. Either one hands the second family the prior it exists
to escape. The builder cannot detect an opinion written into `--intent`; that
part is yours to hold.

## Reading the result

Each assumption returns `confirmed`, `refuted` or `uncheckable` with the
evidence actually executed. This is **evidence you must still evaluate, never a
verdict**:

- A `confirmed`/`refuted` verdict with no executed evidence is downgraded to
  `uncheckable` by the helper, so anything still marked `confirmed` showed its
  work — the work can still be wrong, and you should read it.
- `uncheckable` is a real and expected answer, not a failed call. The reviewer
  runs with `--ignore-user-config` and so has no Gmail, Drive, Docs, Sheets or
  Slides access at all; any assumption resting on those is structurally
  uncheckable from there. Do not re-run to improve it.
- The reviewer is *instructed* to be read-only, and that instruction is the
  whole of its GitHub-write boundary — Codex hooks do not fire under
  `--ignore-user-config` (ADR-007, accepted residual gap, operator decision
  2026-09-03). An envelope showing the reviewer having mutated anything is a
  real incident to report, not a curiosity: nothing downstream would catch it.
- A `refuted` verdict does not decide your verdict. The second family is
  fallible in its own uncorrelated ways, which is exactly why its output is
  evidence rather than an oracle.

## Provenance — state which model produced which half

<!-- R-0359 -->
Every report that took this branch says, in the report itself, which family
produced the cross-family half and which produced yours. A red-team whose
provenance is unstated is indistinguishable from an in-family one, and the
whole value of the branch is that a reader can tell them apart.
<!-- /R-0359 -->

**Do not compose the label yourself — compute it and paste it:**

```
python3 ~/harmonic-forge/tools/lane/cross_family_provenance.py --envelope <envelope path>
# or, when the branch did not trigger:
python3 ~/harmonic-forge/tools/lane/cross_family_provenance.py --envelope /dev/null --not-triggered
```

A label derived by reading prose is a label that will sometimes be derived
wrong, and the wrong one has a specific shape worth naming: the call runs,
exits 0, returns `status: ok`, and every verdict is `uncheckable` because the
reviewer could not reach the evidence. Nothing about that envelope looks like
a failure, and reading it as a success produces a report labelled
`cross-family` that contains no cross-family checking. The classifier gives
that state its own label. `uncheckable` beside a `confirmed` is the mechanism
working; ALL-`uncheckable` is the mechanism having produced nothing.

## When the call does not run — loud, non-fatal, never relabelled

The call can fail: the CLI is missing, the account's token budget is
exhausted, the envelope comes back `process-error` or `invalid-report`. None of
these is fatal to your pass.

<!-- R-0360 -->
1. **Complete the review anyway**, on your own in-family read.
2. **Say plainly that the cross-family red-team did not run**, and why — the
   `status` and the `stderr` the envelope carries, quoted, not paraphrased.
3. **Label it `in-family fallback`**, never `cross-family`. Producing an
   in-family review under a cross-family label is worse than not having the
   mechanism: it converts a known gap into a false assurance.
4. **Do not retry.** The call does not get its own retry budget and does not
   extend your ONE pass.
<!-- /R-0360 -->

The fallback model is the one you are already running as. There is no second
process to launch; the fallback is your own read, correctly labelled.

## What the branch never becomes

Advisory only. No cross-family result authorizes a closure, a merge, or a
PASS. You post nothing yourself — the calling session posts one comment
carrying your verdict and the cross-family evidence together, and there is no
channel by which anyone requests a re-run.
