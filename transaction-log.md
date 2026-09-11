# harmonic-forge Transaction Log

Auto-maintained by `mise run commit` (`scripts/git_commit.py` + `tools/transaction-log/`) — appends a delta summary in the same commit as the code change it describes (headline = verbatim commit message). Cleared on **push to main**, not a version bump — this repo has no running artifact to stamp, so push is its genuine "publish" event (see `mise.toml`'s header comment). Full history: `git log -p transaction-log.md`. Read this file at session start for recent context. Do not edit by hand.

<!-- TRANSACTION_LOG_START -->
## test(belt): make the mutual-exclusion test CI-safe (harmonic-forge#618)

The test shelled out to the real CLI, and `assert_identity` runs before
argument validation and calls `gh-as` -- which is on my PATH and not on CI's.
It passed locally and failed in CI with:

    FileNotFoundError: [Errno 2] No such file or directory: 'gh-as'

A test that only runs on one machine is not a test. Rewritten in-process,
patching `assert_identity` and asserting `SystemExit(2)` plus the message,
matching the two sibling tests added alongside it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- tools/gh/test_watch_lane_posts.py | 23 +++++++++++++++--------
- 1 file changed, 15 insertions(+), 8 deletions(-)

## feat(rules): R-0356/R-0357 -- report what the operator needs, not what you read (harmonic-forge#621)

Lanes report by pasting what they read: a subagent's return, a whole issue body,
a full comment thread. Operator: "It's useless. I want ONLY BLUF/outcomes/HITL
next actions. Only what I need to decide on or know to advance the issue."

And, clarifying the same day: "reporting incremental progress as they keep going
is fine. I don't want them to stop doing that." So the target is VOLUME AND
CONTENT, not frequency -- R-0356 says so explicitly, because a rule against
dumping is one careless reading away from a lane going quiet.

This already existed as operator-memory feedback four times over
(never_paste_subagent_reports, never_echo_gate_reports, expanded_summary_format,
bluf_discipline_recurring). Those reach ONE session. The lanes share no memory
store, so each rediscovers the correction and every correction gets repeated.
`universal-agent.md` is the surface every lane in every repo loads.

**The conflict had to be resolved, not ignored.** An existing enforced guard
requires generated artifacts to be pasted VERBATIM and forbids substituting a
tool-output pane. Written naively this new rule contradicts it and the older
failure returns. R-0357 states the test:

  INPUT    -- material you consumed to reach a conclusion. Never pasted.
  ARTIFACT -- the thing the operator asked to exist. Pasted verbatim.

Authorship and purpose, not size. A gate log read to decide PASS is an input; a
sprint summary the operator asked for is an artifact. Unsure: ask, rather than
defaulting to a dump.

Deliberately NOT hook-enforced. What counts as "only what the operator needs" is
a judgment a hook cannot make, and a bad automated guess would either block
honest reports or pass verbose ones. If it recurs, the next step is measuring
which lane and which trigger, not a regex.

Both registered: `rule registry: clean. 280 rule(s) annotated and in sync.`

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- rules/universal-agent.md  | 34 ++++++++++++++++++++++++++++++++++
- tools/rules/registry.toml | 16 ++++++++++++++++
- 2 files changed, 50 insertions(+)
## fix(belt): wire the plan guard, bind --sweep-for, correct the stale help (harmonic-forge#618)

Preclose returned six findings across the pair. The first is the worst kind.

**1. `reject_plan_as_discussion()` was defined and NEVER CALLED.** The whole
load-bearing AC was dead code: `mise run lane-comment` with a `## Plan` body
still posted `kind=discussion`, byte-identical to the four measured stalls. My
edit that added the call site aborted before writing and I did not re-check.
Wired into `validate_kind` now, with a test that drives the CLI path.

**2. The guard keyed on one heading nothing mandates.** `## Plan` matched;
`## L2S` -- the heading `l2_post.py` actually stamps -- did not. Keying on one
spelling moves the bypass rather than closing it. Both now match, and
`KIND_HEADING["plan"]` uses the same pattern so the cross-check agrees.

**3. The remediation named a command that does not exist.** `mise run l2-post`
is not in HRSE2's task table at all, and harmonic-forge's version has no
`--file` and hardcodes its own repo, so it cannot post to an hrse issue. A
Lane 2 session would have been blocked with no working alternative -- worse
than the drift. `plan` is now a real kind on `lane-comment`, which is the only
tool that can post there, and the message names it.

**4. `--queue-for`'s own --help still said `l1` is the unbounded sweep.** After
the split that is false in both halves, and it is the first thing an operator
reads. Someone reading it and then seeing `--queue-for l1` in the Lane 1 belt
command would conclude the belt arms the sweep -- i.e. read the shipped command
as #590's regression and "fix" it.

**5. `test_suspenders_still_carry_the_repo_wide_sweep` passed on prose.** The
new sentence "It is a *different flag* from `--queue-for l1`" satisfied its
substring check, so deleting the armed command left it green. Now asserts an
actual command line.

**6. Nothing bound `--sweep-for` to the sweep path.** Dropping `sweep=` at
`main()`'s call site routed the suspenders to the BOUNDED queue -- covering the
same narrow set as the belt while printing `sweep-for-l1` -- and the suite
stayed green. Two tests: one that `queue_cycle` respects the parameter, and one
that `main()` actually passes it. The second is the one that matters; the first
version of this fix had only the equivalent of the first, which is the same
untested-wiring shape as #616 and as finding 1 above.

All six mutations now fail:

    drop sweep= at the call site           -> 1 failure(s)
    QUEUE_POSTERS back to hardcoded l1     -> 1 failure(s)
    add discussion to l1 kinds             -> 2 failure(s)
    delete the armed sweep command         -> 2 failure(s)

2054 -> 2056 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- tools/gh/test_watch_lane_posts.py | 70 +++++++++++++++++++++++++++++++++++++--
- tools/gh/watch_lane_posts.py      | 12 ++++---
- 2 files changed, 74 insertions(+), 8 deletions(-)

## fix(belt): give Lane 1 a bounded inbound queue; split the sweep's flag (harmonic-forge#618)

Four Lane 2 plans -- hrse#1383, #1662, #1663, #1771 -- sat unactioned until
Lane 2 asked Lane 1 why it kept ignoring them. Three causes, and I only filed
the third.

**Measured before implementing**, which changed the fix:

    hrse#1383, comment before Lane 1's reply:
      ## Plan — H1383  ||  discussion / LANE2

1. **The plans were posted as `kind=discussion`.** `l2_post.py --kind plan`
   exists and stamps `## L2S` plus `kind=plan`; these went through
   `lane-comment`, which defaults to `discussion`. `post_lane_discussion.py`
   now REFUSES a body headed `## Plan` with `kind=discussion` and names the
   right emitter -- refuse, not restamp, because rewriting the kind changes
   what the thread records about who declared what. Same defect
   harmonic-forge#473 fixed for Lane 3's artifacts in this very script: "there
   was never a missing emitter, only a missing argument."

2. **`discussion` is deliberately un-queueable** -- removed from
   `QUEUE_KINDS["l2"]` on 63 measured issues, none actionable. So a plan
   wearing that kind is invisible to every bounded queue, correctly. AC1 as I
   filed it would not have worked.

3. **Lane 1's belt is worktrees-first and a Plan-First issue has no worktree**
   until Lane 1 approves the plan. `QUEUE_KINDS["l1"] = ("plan",)`, and Lane
   1's belt now arms both halves as Lane 2's has since #596.

**`QUEUE_KINDS["l1"]` alone changed nothing.** `discover_queue` hardcoded
`last_kind[0] == "l1"` -- correct for lanes 2 and 3, which receive work handed
DOWN, and structurally wrong for Lane 1, whose inbound is handed UP by Lane 2.
A `kind=plan` marker is `posted-by=LANE2`, so the check rejected it and the
queue stayed empty. Caught by running it, not by reading it. `QUEUE_POSTERS`
makes the direction explicit per lane.

**`--sweep-for l1` is split off `--queue-for l1`.** `--queue-for l1` used to
route to the unbounded repo-wide sweep, so it meant something categorically
different from `--queue-for l2` -- an inconsistency that was itself a trap, and
the reason Lane 1 could not simply copy Lane 2's fix. `--queue-for` now means
one thing for every lane; the sweep has its own name and stays in the
suspenders; arming both in one process is refused (#590).

Verified end to end:

    newest = kind=plan (LANE2)   -> {1383: 'plan'}
    plan then L1 answered        -> {}   (self-clearing)
    plan posted as discussion    -> {}   (why the four stalled)
    l2 handoff still works       -> {1383: 'handoff'}

Third instance of one property, now stated in SKILL.md rather than fixed a
third time per lane: A LANE'S INBOUND WORK HAS NO WORKTREE, because the
worktree is created in response to it.

2045 -> 2054 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- skills/belt-and-suspenders/test_skill_text.py |  7 ++-
- tools/gh/test_watch_lane_posts.py             | 91 ++++++++++++++++++++++++++-
- tools/gh/watch_lane_posts.py                  | 80 +++++++++++++++++++----
- 4 files changed, 182 insertions(+), 19 deletions(-)

## fix(gh): test the wiring, make the implication transitive (harmonic-forge#616)

Preclose returned two findings, and the first is this issue's own shape turned
back on the fix.

**1. The one line that reaches the helper had no coverage.** Reverting the CLI
call site to its pre-diff form -- leaving `normalise_labels` defined and unused
-- left all 67 tests green. Every new test called the helper directly; none
drove `main()` and inspected what reached `create_issue`. So the fix for "a
control exists and is not reached at the point of use" shipped with exactly
that defect in its own test suite. The file already had the harness pattern
(`TestThemeVentureCliWiring` does it for a different flag); I did not use it.

**2. The implication was single-pass, so AC4 was false as written.** The loop
iterated the caller's list, not the accumulator, so a label added by an
implication was never itself consulted: with a chained table
`{"a": "b", "b": "c"}`, `["a"]` yielded `["a", "b"]` and silently dropped `c`.
AC4 promises "a second implication is a data change" -- the next editor adds a
row, does the data-only change the table advertises, and gets a partially
applied result with nothing to surface it. Now a worklist, with a `seen` guard
so a cyclic table terminates rather than hanging the filing tool.

Both mutations now fail, along with emptying the table:

    revert the call site (the untested wiring)   -> 6 failure(s)
    back to single-pass (non-transitive)         -> 9 failure(s)
    empty the table                              -> 9 failure(s)

Also confirmed by the inspection, and worth recording because I did not check
it before filing: `main()` is the only issue-creation path -- both repos'
wrappers and the golden-path template all shell out to this script, nothing
imports `create_issue` as a library -- and a missing `tooling` label does NOT
fail the filing. The REST create endpoint auto-creates it, so the observable
effect in cymagraph-infra and openclaw-projects is a new grey undescribed label
rather than a broken file. That was the risk worth being wrong about.

2038 -> 2042 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- tools/gh/gh_issue.py      | 31 ++++++++++++++++++++++++-------
- tools/gh/test_gh_issue.py | 45 +++++++++++++++++++++++++++++++++++++++++++++
- 2 files changed, 69 insertions(+), 7 deletions(-)

## fix(gh): tooling-exception implies tooling at filing time (harmonic-forge#616)

`tooling-exception` says Lane 1 may execute an issue alone. `tooling` is what
keeps Lane 2 from picking it up. Two halves of one decision, applied by hand,
and they drifted.

Operator: "you need to make sure all of your issues are marked as tooling so L2
doesn't try to pick it up... this is crucial for avoiding overlap."

Eleven issues carried the exception without the label, every one filed by Lane 1
this session. `gh_issue.py` had ZERO knowledge of `tooling-exception` -- grep
returned nothing -- so the pairing lived entirely in whoever was filing
remembering it. The operator had already corrected this once earlier in the same
session, on a different pair of issues, and it did not stick because nothing
enforced it.

Why it is not cosmetic: two lanes on one issue means two writers in one
worktree, which `lane-protocol.md` forbids and nothing detects (tracked as
harmonic-forge#600 AC7-9, surfaced when Lane 2 halted on hrse#1774 believing
exactly that had happened).

Fourth instance this session of one shape -- a control that exists, is correct,
and is not reached at the point of use (#590 the sweep, #594 the roots, #605
the prefix map, now this). Three of the four were fixed by deriving rather than
restating; this one by implying rather than remembering.

- `normalise_labels()` applies `_IMPLIED_LABELS` at filing. Additive only: it
  never removes, rewrites or reorders what the caller asked for, and does not
  duplicate a label already present.
- One row in the table, deliberately. A general label-inference engine would be
  inventing a problem; a table with one row is honest about having one rule.
- Five tests. Verified live on this issue's own filing:
  `616 [bug,tooling,tooling-exception]` from `--labels "bug,tooling-exception"`.
- The eleven existing issues were backfilled by hand.

Not in scope, stated rather than assumed: nothing AUDITS existing issues for the
pairing. `repo_hygiene.py` is where that belongs if the backfill is ever needed
again.

2033 -> 2038 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- tools/gh/gh_issue.py      | 26 +++++++++++++++++++++++++-
- tools/gh/test_gh_issue.py | 30 ++++++++++++++++++++++++++++++
- 2 files changed, 55 insertions(+), 1 deletion(-)

## fix(belt): key dedup state per belt, prime per target, test the loop (harmonic-forge#599)

Preclose returned five findings. The first would have made the whole change
worse than not doing it.

**1. Every concurrently-armed belt shared one watermark file.** The key was
per-target but had no lane or process component, and SKILL.md prescribes Lane 1
(`--watch l2 --watch l3`) and Lane 2 (`--queue-for l2 --watch l1`) running at
once over the same `--all-worktrees` enumeration. So: Lane 1's belt dies at
10:00, Lane 2 keeps advancing the shared file every 90s, Lane 1 re-arms at
12:00 and reads 11:59 -- and every marker posted in those two hours is never
fetched, in that session or any later one. That is the exact loss AC2 exists to
close, reintroduced one axis over, and it is the argument `Watermarks`' own
docstring makes about accounts vs repos. State is now keyed by belt identity
(watch-set plus `--queue-for`); verified two belts no longer see each other's
mark.

**2. Every behavior AC1/AC3/AC4/AC6 name was untested.** Six mutations of the
loop -- including advance-on-failure, the exact regression AC6 names -- left the
suite green, because the loop lived inside `while True:` with no seam and the
tests were written against the extractable helpers while the class docstring
claimed the loop-level property. `comment_watch_cycle()` is extracted and
driven directly by 9 new tests. All six mutations now fail:

    M1 advance-on-failure    -> 2 failure(s)
    M2 no-overlap            -> 1 failure(s)
    M3 no-seen-check         -> 1 failure(s)
    M4 priming-always        -> 1 failure(s)
    M5 priming-never         -> 3 failure(s)
    M6 no-seen-add           -> 2 failure(s)

**3. A fetch failure during priming inverted priming.** `priming` was one
scalar cleared once per cycle, so a target whose first fetch failed never got a
priming pass -- and then replayed its entire overlap window as new, looking
exactly like a burst of real handoffs. Priming is now per target.

**4. Priming could silently swallow live work.** Adding the 15-minute overlap
made the priming window reach backwards into real posts, and the old message
was a bare count. It now NAMES every suppressed marker and states the recovery
path, because the seen-set entry is permanent and deleting the watermark does
not undo it.

**5. An unreadable watermark failed open silently.** Empty and unparseable both
returned `None`, which the caller turns into "read from now - K" -- collapsing
an hours-old window to minutes via the "I cannot decide" path, with no output.
Still `None`, since inventing a watermark would be worse; no longer silent.

1992 -> 2001 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- tools/gh/belt_mechanics.py        |  18 ++++
- tools/gh/test_watch_lane_posts.py | 115 +++++++++++++++++++++++++-
- tools/gh/watch_lane_posts.py      | 169 +++++++++++++++++++++++++-------------
- 3 files changed, 245 insertions(+), 57 deletions(-)

## fix(belt): implement the dedup mechanic SKILL.md declares mandatory (harmonic-forge#599)

`SKILL.md:172-182` states dedup as one mechanic with three parts -- watermark,
overlap, seen-set -- and ends "Do not simplify it back." The comment-watch path
had none of them. It imported four names from `belt_mechanics` and used
`Watermarks`, `SeenSet` and `query_since` nowhere.

What ran instead: one in-memory `since`, advanced unconditionally at the bottom
of every cycle, after a fetch that swallowed every exception to `[]`. So a rate
limit or a 502 on any issue produced "no comments", the loop advanced past that
window anyway, and those comments were never fetched again. The stderr line was
the only trace, and stderr is not what the Monitor reads. This is the failure
that lost hrse#1725's spec, in the file documenting the fix as mandatory.

Scope narrowed by the out-of-family review before implementing: the `--queue-for
l1` sweep ALREADY had a success-gated per-repo watermark (#579). This brings the
comment-watch path up to that, and adds what neither path had.

- **Watermark, per TARGET.** `_fetch_comments` returns `None` on failure
  (distinct from `[]`), and the watermark is held rather than advanced, so the
  next cycle re-reads the window. Per target rather than per repo because two
  issues in one repo fail independently -- the argument `Watermarks`' own
  docstring makes one level up about accounts vs repos.
- **Persistent**, in `~/.claude/state/belt/`, alongside `batch_auth`'s state.
  The in-memory `since` reset to `now` on every restart, silently skipping
  everything posted while the belt was down -- which is exactly when a handoff
  is most likely to be missed.
- **Overlap.** `query_since(watermark, K)` reads from `min(watermark, now - K)`,
  K=15m, a deliberate over-cover of every prescribed interval (60s/90s/300s/600s).
- **Seen-set**, which is what makes overlap affordable -- re-reading the seam
  re-delivers comments, and without it every one would be re-announced. Primed
  on a genuinely first arm so arming does not replay history as new, and only
  then: if state exists the belt has run before, and priming would suppress
  real posts made while it was down.

Verified live -- failure holds the mark, next query re-reads the seam:

    watermark after success: 2026-09-10 12:00:00+00:00
    fetch on failure    : None (None => hold the watermark)
    watermark unchanged : True
    next query starts at: 2026-09-10 11:50:00+00:00 (overlap K=15m)

AC5 (`TickLog` written by the belt) is deliberately not in this change -- the
issue marks it separable and the watermark half is the half losing work.

1976 -> 1985 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- tools/gh/test_watch_lane_posts.py | 70 +++++++++++++++++++++++++++++
- tools/gh/watch_lane_posts.py      | 94 +++++++++++++++++++++++++++++++++++++--
- 2 files changed, 160 insertions(+), 4 deletions(-)

## fix(belt): state what each mechanism actually does, verified by executing them (harmonic-forge#607)

Preclose returned five findings. The first two say my fix replaced a false
statement with a differently-false one, and they are right.

**"Newest wins, full stop -- no precedence table, no exclusion list" was TRUE
where it stood.** It annotates the `## Role: Lane 1` blockquote, which describes
`discover_l1_sweep` -- and that function applies neither filter. I deleted a
correct sentence and put `discover_queue`'s rules in its place, under a heading
about a different function. Executed:

    discover_l1_sweep  [handoff, L2 Finding] -> {1530: ('l2', '## L2 Finding ...')}
    discover_l1_sweep  [handoff]             -> {}

A `## L2 Finding` DOES put an issue in Lane 1's sweep. My table said it must
never change membership.

**And "newest queue-eligible marker wins" was wrong for the belts too.**
`discover_queue` requires the newest non-finding comment to ITSELF be Lane 1's
and of an eligible kind -- so a newer ineligible comment EVICTS:

    discover_queue l3  [ready-for-l3]        -> {1530: 'ready-for-l3'}
    discover_queue l3  [ready-for-l3, disc]  -> {}
    discover_queue l3  [ready-for-l3, find]  -> {1530: 'ready-for-l3'}

Eviction is not incidental, it is the mechanism: posting anything else clears
the queue, which is what makes it self-clearing with no "done" bookkeeping. My
phrasing would have made the `discussion` removal incoherent -- under
newest-eligible-wins, an older `handoff` would win forever and the 63 measured
noise issues would never leave Lane 2's belt.

Rewritten to state both mechanisms separately, with executed output for each,
and which one you are debugging when an answer surprises you.

**The guards were weak in the way the previous two were.** They substring-
searched the whole file, so two of three filter rows deleted green:

    delete `QUEUE_KINDS[lane]` row  -> 0 failures
    delete `_is_l2_finding` row     -> 0 failures

Both tokens also appear in the block's own prose. Now scoped to the block and
asserted as ROWS, and the guard imports `watch_lane_posts` so renaming or
deleting `_is_l2_finding` in the CODE fails it -- the AC's "while
`_is_l2_finding` exists" condition was never evaluated before. All three rows
now fail on deletion.

Also fixed: the `lane_state.py` citation was a bare relative path that resolves
only in HRSE2, while this skill is linked into four repos. Qualified and marked
HRSE2-local.

1974 -> 1976 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- skills/belt-and-suspenders/SKILL.md           | 61 ++++++++++++++--------
- skills/belt-and-suspenders/test_skill_text.py | 73 ++++++++++++++++++++-------
- 2 files changed, 95 insertions(+), 39 deletions(-)
## fix(belt): register L2P as retired and mark it where it still surfaces (harmonic-forge#609)

`L2P` stopped being emitted at #583 -- `l2_post._HEADINGS["plan"]` is `## L2S`.
It still reached an operator's screen, and nothing stopped it returning.

The source was not code. Historical issue comments carry it, and the belt
renders a comment's first line straight into the lane's task display, where it
reads as current because nothing said otherwise. Lane 1 showed it; the operator
corrected it by hand.

- `L2P` is registered in `retired_artifacts.py`, the machine-readable list
  `gh_issue.py` checks issue bodies against (#379). It was absent, which is why
  "make sure it does not appear anywhere" had nothing behind it. Enforcement is
  backtick-scoped, so a retirement notice in prose does not trip it.
- The belt MARKS rather than reproduces: `## L2P` renders as
  `## L2P [retired -> L2S or L2D]`. Marking, not rewriting -- the comment is an
  accurate record of what was posted and must not be falsified. Driven from the
  same registry, not a second list.
- `HRSE2/scripts/post_lane_discussion.py` still described the "L2P/L2D/L2B
  family" as current.
- A test asserts no `_HEADINGS` value contains `L2P`, so the original defect
  cannot silently return.

Deliberate mentions stay: `rules/lane-shorthand.md`, `3-lane-protocol.md` and
`l2_post.py` all name L2P in order to record that it is retired.

1974 -> 1979 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- tools/gh/retired_artifacts.py     |  4 ++++
- tools/gh/test_l2_post.py          | 14 +++++++++++++
- tools/gh/test_watch_lane_posts.py | 42 +++++++++++++++++++++++++++++++++++++++
- tools/gh/watch_lane_posts.py      | 41 +++++++++++++++++++++++++++++++++++++-
- 4 files changed, 100 insertions(+), 1 deletion(-)

## fix(manifest): derive the prefix map, spread the OpenClaw venture, clear board 1 (harmonic-forge#605)

Preclose returned four findings. My "the belt went 3 repos -> 4 with no code
change" claim was true for the repo-search half and wrong for the worktree half.

1. **The belt could not resolve an openclaw branch.** `_BRANCH_ISSUE_RE`'s
   prefix class read `[hHfFiI]`, so `l2/o12-fix` or `__gate__/o12-tc1` -- the
   exact branch shapes used live -- matched nothing and the belt never watched
   openclaw-projects#12. `_PREFIX_REPO` had no `o` either.

   Fixed by DERIVING both from projects.toml rather than adding a fourth `O` to
   a fourth hardcoded list. There were three independent copies of this map --
   `_PREFIX_REPO`, that regex class, and `batch_auth.REPO_PREFIXES` -- with no
   test tying any of them together. New `manifest.prefix_repos()` is the single
   source; all three now read it, and four tests assert they cannot disagree.
   `batch_auth` keeps a last-known fallback if the manifest is unreadable: it
   runs as a PreToolUse hook on every matching command, and denying every BATCH
   on a bad read would turn a gate into an outage.

2. **`OpenClaw` existed on board #4 only.** `gh_issue.py --venture` has no
   `choices` list and validates live against whichever board the repo maps to,
   so `--venture OpenClaw` on a forge or hrse issue created the issue, boarded
   it, wrote Status/Tier/Theme, then failed on Venture and exited 1 -- leaving a
   live issue with Venture unset, which `repo_hygiene.audit_unboarded` reports
   as drift. Not hypothetical: #605 itself sits on board 3 with Venture null.
   Added to boards 1 and 3.

3. **openclaw-projects#4 was stranded on board 1.** After the flip, both audits
   resolve openclaw to board 4 and fetch only that, so nothing would ever have
   seen it again -- and board 1 is CymaGraph's release view, which is the whole
   reason for the split. Found by paginating all 984 items; removed, 984 -> 983.

4. **The new CLAUDE.md asserted projects.toml is "the only place" the prefix is
   declared.** It is neither the only nor the canonical place: `lane-shorthand.md`
   is canonical and the manifest is CHECKED against it. Corrected.

Also corrected, comment-only: three modules still stated openclaw shares board
#1 (`repo_hygiene.py`, `gh_issue.py`, `manifest.py`), and HRSE2's
`docs/BOARD-FIELDS.md` still enumerated four ventures.

1968 -> 1972 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- tools/gh/watch_lane_posts.py      | 23 +++++++++++---------
- tools/hooks/batch_auth.py         | 45 +++++++++++++++++++++++++++++---------
- tools/onboard/manifest.py         | 30 +++++++++++++++++++++----
- 6 files changed, 129 insertions(+), 29 deletions(-)

## feat(manifest): onboard openclaw-projects onto its own board (harmonic-forge#605)

openclaw-projects had a live checkout, both lane worktrees, linked directives,
hooks and a reserved prefix -- but `onboarded = false`, so every manifest
consumer excluded it. Surfaced by #596: the belt derives its repo set from this
manifest and reported 3 non-archived repos, not 4, leaving all three armed
lanes blind to that repo. The manifest was working correctly; it was reporting
a repo nobody had onboarded.

It also needed its own board. It was pointed at board 1 (CymaGraph Backlog)
while carrying `milestones = false` -- the only repo with that combination, and
the mismatch that surfaced the real question, since board 1 is CymaGraph's
release view and openclaw ships in no CymaGraph release. Operator, 2026-09-10:
"openclaw 'feeds' the others because I prototype things there but it isn't part
of the cymagraph product nor will it be for kenekted or leasepal."

So openclaw is its own venture, and the board model is confirmed as PER VENTURE,
not per repo -- which is also why cymagraph-infra legitimately shares hrse's
board 1: those two repos ship one CymaGraph release together.

- New board vitalharmony #4 "openclaw Backlog", with Status/Tier/Theme/Venture/
  Sequence matching board 3's schema. Venture gains an OpenClaw option.
- `board_number` 1 -> 4, `onboarded` false -> true.
- The manifest header's board rule is corrected: it asserted openclaw
  "deliberately shares hrse's board #1", now the opposite of true.
- Two live-value guards updated. `test_gh_issue.TestRepoBoardMap` and
  `test_manifest.LiveManifestTests` pin the board map so a change cannot
  silently misroute issues (the failure #107 fixed once already). They fired
  correctly; the expectation moved with a recorded reason. A third test's
  docstring cited openclaw as the live `onboarded = false` example and now
  points at its own fixture.

forge-onboard: 6 projects, 0 failing checks. The belt went 3 repos -> 4 with no
code change, which is the point of deriving from the manifest rather than
listing.

`mise run check` exit 0, 1968 tests.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- projects.toml                  | 18 ++++++++++++++----
- tools/gh/test_gh_issue.py      |  7 ++++++-
- tools/onboard/test_manifest.py | 14 ++++++++++----
- 3 files changed, 30 insertions(+), 9 deletions(-)
## fix(belt): SKILL.md claimed no exclusion list while the belt has three (harmonic-forge#607)

`SKILL.md:279` read "Newest wins, full stop -- no precedence table, no exclusion
list." Three filters decide what can win:

- `QUEUE_KINDS[lane]` -- a precedence table. A marker addressed to another lane
  is not this lane's ball.
- `_is_l2_finding` -- an exclusion list. harmonic-forge#580 AC1: a finding
  posted after a `ready-for-l3` marker made `last_kind` become Lane 2's, which
  silently dropped a genuinely queued issue out of Lane 3's belt.
- `discussion` removed from `QUEUE_KINDS["l2"]` -- measured, 63 issues whose
  newest post-`l2.done` marker was a discussion, none actionable.

Flagged independently by the product-strategy design assessment and by the
out-of-family Codex review of it.

Text fix only; no behavior change. Each filter is now stated WITH the incident
that earned it, because the same out-of-family review caught a proposal to
delete them reasoning from the old sentence -- and deleting the finding
exclusion reintroduces #580's false retraction. The doctrine was wrong; the
filters are right. A filter with no recorded reason reads as accretion, and
this repo deletes accretion.

The cost of the old sentence was debugging time: when an issue is not where a
lane expects it, "newest wins, full stop" points at "something posted after my
marker" when the cause is usually that the newest marker's kind is not in this
lane's `QUEUE_KINDS`, or that a finding was correctly skipped. It also asserted
the absence of a state machine that `sprint-plan/scripts/lane_state.py` already
models.

Two doc-sync guards added, both mutation-checked: reverting to the old sentence
fails them.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- skills/belt-and-suspenders/SKILL.md           | 26 ++++++++++++++++++++++++--
- skills/belt-and-suspenders/test_skill_text.py | 22 ++++++++++++++++++++++
- 2 files changed, 46 insertions(+), 2 deletions(-)

## fix(belt): paginate the candidate search; make the #602 test actually bite

Preclose on PR #603 returned two findings. Both fixed.

**1. `_search_candidates` was the one fetch in this module that did not
paginate.** Unpaginated, `search/issues` returns at most 30 items regardless
of `total_count`, with no error and `incomplete_results=false`. Measured live:
`q=repo:vitalharmony/hrse state:closed lane` -> total_count 1193, items 30.
The l2 handoff search matches every open issue that ever received a Lane 1
handoff -- a set that only grows -- and stood at 22 on hrse when this was
found. Eight short. Past 30, a queued issue falls outside the first page, is
absent from `candidates`, absent from `queued`, and reported as SUCCESS --
which is precisely the false retraction this issue exists to remove, arriving
by a different route. Best-match ordering would have made it flap rather than
fail cleanly.

`--paginate -f per_page=100` with a `--jq` reduction to one number per line;
`--paginate` emits one JSON object per page, so the previous single
`json.loads` would have read only the first page anyway. `incomplete_results`
now fails closed rather than silently under-reporting, and an unparseable body
raises instead of returning `set()` -- "I do not know" is not "nothing is
queued".

**2. The AC3 "end to end" test passed with the entire fix reverted.** It
patched `discover_queue` -- the unit under change -- so it verified
`queue_cycle`'s carry-forward against a value the test itself supplied.
Rewritten to patch `_fetch_all_comments` one level lower and call
`queue_cycle` unmocked. Mutation-verified: reverting the fix now fails it.

Added alongside: a test that a genuinely-gone issue IS still retracted, so the
fix cannot buy safety by never retracting anything; and coverage of the
`SearchUnavailable` branch, which had none -- flipping it to `return {}, True`
previously left the whole suite green.

1963 -> 1968 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- tools/gh/test_watch_lane_posts.py | 81 ++++++++++++++++++++++++++++++++++++---
- tools/gh/watch_lane_posts.py      | 42 ++++++++++++++++++--
- 2 files changed, 113 insertions(+), 10 deletions(-)

## fix(belt): fail closed when a per-issue comment fetch fails (harmonic-forge#602)

`discover_queue` turned a failed per-issue comment fetch into an empty
iteration via `or ()` and still returned `fetch_ok=True`. `queue_cycle` then
counted the repo as having reported and emitted `left-queue-for-<lane>` on
stdout for that issue -- telling the lane the ball moved on because one comment
fetch hit a rate limit.

Found by an out-of-family review (Codex / gpt-5.6-sol via
`cross_family_call.sh --posture verify`) of #590/#594/#596. Four in-family
passes did not find it. Confirmed against source before acting.

The `or ()` was genuinely benign until #596: nothing diffed this function's
result against a previous cycle, and the note here said so. #596 added exactly
that diff, and added repo-level `fetch_ok` to stop this class of false
retraction -- but a repo-level guard cannot see an issue-level failure, so the
fix landed one level above the remaining hole. This is the other half of #596's
own preclose finding 2.

Conservative on purpose: one failed issue marks the whole repo unreliable for
that cycle. A stale queue carried one cycle is recoverable; a false retraction
is not, because the lane acts on it.

- `None` (failed) now returns `({}, False)`; `[]` (genuinely zero) still
  reports success, or the fix would trade a false retraction for a stuck queue.
- Three tests, including the end-to-end property: no `left-queue-for-*` line is
  emitted and the previously-queued issue survives.
- The "not a regression" note is corrected, being now the opposite of true.

1960 -> 1963 tests, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- tools/gh/test_watch_lane_posts.py | 37 +++++++++++++++++++++++++++++++++++++
- tools/gh/watch_lane_posts.py      | 30 +++++++++++++++++++++---------
- 2 files changed, 58 insertions(+), 9 deletions(-)

## fix(belt): restore Lane 2 queue discovery; derive the repo set from the manifest (harmonic-forge#596)

Preclose on PR #597 returned four findings, and the strategy review returned a
fifth that supersedes how I fixed the repo set. All addressed.

**1. Lane 2 lost inbound queue discovery — the belt's whole job for that lane.**
I deleted the `--queue-for l2` fallback while making Lane 2 worktrees-first.
But Lane 2's inbound handoff has NO worktree by construction: Lane 2 creates
`/tmp/<repo>-<issue>-impl` only AFTER picking an issue up, so every inbound
handoff is in the no-worktree state and a worktrees-only belt sees none of
them. `QUEUE_KINDS["l2"]` became reachable from no prescribed command. Lane 1's
belt can be worktrees-only because other lanes' worktrees ARE what Lane 1 needs
to see; that asymmetry is load-bearing and I carried it across without checking.
Lane 2 now arms both halves in one command.

**2. One repo's fetch failure retracted the other's queued issues.** A search
rate-limit trip returned `set()`, indistinguishable from "nothing queued", so
every issue that repo had queued printed `left-queue-for-l3` on stdout -- which
the lane reads as "the ball moved on". `discover_queue` now returns
`(queued, fetch_ok)` like `discover_l1_sweep`, a failed repo carries its
previous queue forward untouched, and only repos that actually reported can
produce a retraction. The first-poll line counts repos that REPORTED, not argv.

**3. Nothing tested the runtime.** Eight mutations of the queue loop -- including
`repos[:1]` and reverting the repo-qualified key -- left the suite green,
because no test calls `main()`. Extracted `queue_cycle()` so the behavior is
callable, and tested it.

**4. `--help` still prescribed the command this issue opened on**, and
documented a combination the parser now rejects.

**5. The repo set is derived from `projects.toml`, not listed and not from
`gh repo list`.** My four-repo list violated R-0122 outright, and a test pinned
it. `gh repo list` was the second attempt and still wrong: it needs a
convention to find checkouts, and the convention `<dir>/<repo name>` silently
dropped hrse, whose checkout is `HRSE2`. The manifest already carries repo,
path, worktree_dir and account -- it exists because "duplication is the only
source of drift, and drift here is silent" -- and onboarding (R-0340) is what
brings a repo under the belt. No API call, no second list, no convention.

The guards are inverted to match: they now assert DERIVATION and fail on any
hardcoded repo or path, per lane and for the suspenders' sweep.

Suite 1929 -> 1960, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- skills/belt-and-suspenders/test_skill_text.py |  16 +-
- tools/gh/test_watch_lane_posts.py             | 147 ++++++-------
- tools/gh/watch_lane_posts.py                  | 283 +++++++++++++++++++++-----
- 4 files changed, 347 insertions(+), 162 deletions(-)

## fix(belt): all four active repos, not two (harmonic-forge#596)

Operator caught this mid-flight: the belt spans hrse and harmonic-forge, and
there are FOUR active vitalharmony repos. `cymagraph-infra` and
`openclaw-projects` each have live `-lane2` and `-lane3` checkouts with this
skill linked and declared -- verified `[OK]` against their platform
declarations, all six. So the skill was loaded into those lanes and telling
them to watch repos that are not theirs.

Same defect as #594's half-belt and #596's hrse-only sweep, one scope wider.
Naming two repos instead of one fixed the instance and not the class.

- Every lane command and the suspenders' sweep now name all four.
- The guard asserts the repo SET, not merely "more than one" -- adding or
  retiring a repo now fails the suite until the commands are updated. That is
  the standing cost of #594's decision to name roots rather than infer them,
  and making the cost loud is the whole point: a visibly outdated list beats a
  silently narrowed belt.
- A separate guard covers the suspenders' sweep, which sits outside any lane
  bullet and which the per-lane guard therefore never reached -- the same
  "control not reached at the point of use" shape as the last three.
- `test_lane1_belt_names_two_distinct_repo_roots` asserted exactly two roots
  and is superseded; it now checks only distinctness, with arity owned by the
  four-repo guard. Two tests asserting the same property with different
  answers is how the next one goes stale.

Verified live from $HOME:

  root /home/mmangus/Harmonic_Projects/HRSE2: 17 worktree(s)
  root /home/mmangus/harmonic-forge: 11 worktree(s)
  root /home/mmangus/Harmonic_Projects/cymagraph-infra: 3 worktree(s)
  root /home/mmangus/Harmonic_Projects/openclaw-projects: 4 worktree(s)
  --all-worktrees enumerated 35 live worktree(s)

Suite 1960 -> 1961, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- skills/belt-and-suspenders/SKILL.md | 25 +++++++++++------
- tools/gh/test_watch_lane_posts.py   | 56 +++++++++++++++++++++++++++----------
- 2 files changed, 58 insertions(+), 23 deletions(-)

## fix(belt): finish the job — Lane 2 and Lane 3 belts, both repos, every lane (harmonic-forge#596)

#590 fixed Lane 1's pull source. #594 fixed Lane 1's cwd dependence. Both left
Lanes 2 and 3 carrying the same two defects, so the skill was correct for one
lane out of three. This finishes it.

- **Lane 2's belt is worktrees-first.** Naming the shared `HRSE2-lane2`
  checkout resolved 0/1 live: between issues it sits on a detached HEAD, and
  during an issue `lane-protocol.md` requires Lane 2 to work in
  `/tmp/<repo>-<issue>-impl` and forbids the shared checkout -- so the one path
  a static list could name was the one path Lane 2 may not work in. Now 7/28.

- **`--repo` is repeatable, and `--queue-for` scans every repo given.** Lane 3
  and the suspenders' Lane 1 sweep were hrse-only while 43 tooling-exception
  issues sit on harmonic-forge with live lane checkouts for it. The two-repo L1
  sweep immediately surfaced `harmonic-forge#62`, which the old command could
  not see.

- **The queue is keyed `(repo, issue)`, never a bare number.** hrse#570 and
  harmonic-forge#570 both exist; a shared int key let one evict the other.
  `l1_since` is per-repo for the same reason.

- **Poll first, sleep after.** The loop slept before its first poll, so a belt
  printed nothing for a whole interval -- ten minutes of silence for the
  documented 600s suspenders sweep, which is meant to be run and read. "A
  monitor that never printed a status line is not proof it is watching
  anything" is this protocol's own rule; making the operator wait an interval
  to learn otherwise is that same failure, deferred.

- **Guards are per lane now.** #594's preclose finding was a Lane 1 guard that
  passed on the command it forbids; Lanes 2 and 3 had no guard at all, which is
  how this shipped. Every lane's command is now asserted to span both repos, to
  be a single unwrapped copy-pasteable line, and to name no static worktree.

Also unblocks main, second time in two days: #569 (#592) added 3413 bytes to
`.claude/rules/lane-shorthand.md` without recording them, leaving the ratchet
failing and the repo un-committable. Recorded from here rather than left to
block whoever committed next.

Verified live, all three lanes, run verbatim from $HOME:
  Lane 1  27 worktrees / 2 roots, 8 closed-issue ghosts dropped
  Lane 2  0/1 -> 7/28 resolved
  Lane 3  queue-for-l3: 0 issue(s) queued now across 2 repo(s)
  L1 sweep 8 issue(s) queued across 2 repo(s), incl. harmonic-forge#62

Suite 1955 -> 1960, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- skills/belt-and-suspenders/test_skill_text.py | 18 +++++--
- tools/gh/test_watch_lane_posts.py             | 70 ++++++++++++++++++++++++
- tools/gh/watch_lane_posts.py                  | 78 ++++++++++++++++-----------
- 5 files changed, 174 insertions(+), 54 deletions(-)

## fix(belt): make the Lane 1 doc guard reject a flag where a repo root belongs (harmonic-forge#594)

Preclose inspection on PR #595 returned one finding, and it was the worst kind:
the guard test that exists to stop a prose edit re-arming a half-belt passed on
the exact command it forbids.

`re.search(r"--all-worktrees\\s+(\\S+)\\s+(\\S+)", cmd)` -- `\S+` matches flags.
Reverting SKILL.md to the pre-#594 CWD-dependent form bound group(1)="--watch"
and group(2)="l2", which are unequal, so the assertion held and all 96 tests
passed. Reproduced before fixing; the half-belt mutant (one root) passed too.

- The roots are now parsed as "every token up to the next flag", and asserted
  to be exactly two, distinct, path-shaped, and to name hrse and harmonic-forge
  specifically. Both mutants now fail; verified by re-applying each.

The finding also named the condition that let a vacuous assertion survive
review, and that is fixed here too:

- `skills/belt-and-suspenders/test_skill_text.py` -- the doc guard for this
  very protocol -- had been RED since #590 merged, asserting a Lane 1 command
  that #590 deliberately removed. Nobody noticed because nothing ran it:
  `tools/run_tests.py`'s TEST_DIRS could not reach outside `tools/`.
- TEST_DIRS now takes paths that do, and collects it. 1936 -> 1955 tests, 55 ->
  56 files.
- Its stale assertion is replaced by the property that is actually true after
  #590: Lane 1's belt is worktrees-first, and the repo-wide sweep survives in
  the suspenders.

Six guards across both suites now fail on the original #590 regression, where
before this commit the one that mattered passed on it.

This is the same root cause the last three of these have shared: a control
exists, is correct, and is not reached at the point of use.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- skills/belt-and-suspenders/test_skill_text.py | 16 +++++++++--
- tools/gh/test_watch_lane_posts.py             | 40 ++++++++++++++++++++++-----
- tools/run_tests.py                            | 10 ++++++-
- 3 files changed, 56 insertions(+), 10 deletions(-)

## fix(belt): name the repo roots, so /belt-and-suspenders arms correctly anywhere (harmonic-forge#594)

#590 made Lane 1's belt worktrees-first but left it only correct from one
directory: `--all-worktrees` enumerated the repo containing CWD, and the second
repo was seeded by overloading `--worktrees`. A skill cannot arm a command
whose correctness depends on where the session happens to be sitting -- and
`--worktrees` doing two jobs, distinguished only by prose, is the shape defects
grow in.

- `--all-worktrees` now takes the repo roots as its own arguments. With none,
  it keeps its #590 meaning (the repo containing CWD). Named roots do not also
  enumerate CWD, which is what makes the result cwd-independent.
- `--worktrees` has one job again: naming individual worktrees to watch.
- Repo identity resolves symlinks, so `~/harmonic-forge` and
  `~/Harmonic_Projects/harmonic-forge` collapse to one repo and the duplicate
  is reported rather than silently dropped.
- A NAMED root that is not a repo raises rather than warns. An asserted root
  contributing nothing is a typo, and arming a narrower belt than was asked for
  is the one thing this protocol exists to refuse. A bare CWD that is not a
  repo still only warns -- nobody asserted it was one -- and names the fix.
- A root that vanishes mid-session shouts and keeps the previous set. A dead
  belt is worse than a stale one, and the typo case is already caught at arm
  time by `parser.error`.
- SKILL.md's Lane 1 command is one line, armed verbatim, with no `cd` first and
  no caveat -- because there is nothing left to warn about.

Proof, run from `/tmp` rather than either checkout:

  [watch_lane_posts]   root /home/mmangus/Harmonic_Projects/HRSE2: 15 worktree(s)
  [watch_lane_posts]   root /home/mmangus/harmonic-forge: 11 worktree(s)
  [watch_lane_posts] worktree targets: 10/26 resolved

7 further tests (1929 -> 1936), including one asserting the doc carries no
"run it from the HRSE2 checkout" caveat and one covering symlinked roots.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- skills/belt-and-suspenders/SKILL.md |  14 ++--
- tools/gh/test_watch_lane_posts.py   | 109 +++++++++++++++++++++++++------
- tools/gh/watch_lane_posts.py        | 125 +++++++++++++++++++++++-------------
- 3 files changed, 176 insertions(+), 72 deletions(-)

## fix(belt): drop closed-issue worktrees, re-enumerate per cycle, report per root (harmonic-forge#590)

Preclose inspection on PR #591 returned four findings. All four fixed here.

1. **Closed issues were offered as live work.** Nothing prunes a
   `/tmp/<repo>-<issue>-impl` checkout, and its branch reads as ahead of
   origin/main forever once main takes the work as a squash merge. Making the
   belt worktrees-first therefore traded "every open issue in the repo" for
   "every stale checkout on the box" -- smaller, same permissive direction.
   `drop_closed_targets` demotes those to unresolved and names them; verified
   live, 8 ghosts dropped (hf#565/566/567/568, hrse#568/586/1675/1764), 11 real
   targets left. The verdict is cached, so this is one API call per ghost per
   session, not per cycle; a reopened issue comes back via the suspenders'
   sweep, which is what that backstop is for.

2. **The worktree set was frozen at arm time.** Enumeration sat in argument
   post-processing, before the poll loop, so a worktree Lane 2 creates during a
   session was invisible for the session's whole life -- and with the repo-wide
   sweep no longer on the belt, nothing else would have caught it. It now
   re-enumerates every cycle, as every other discovery step in that loop
   already did. The old docstring claimed arm-time reading "cannot go stale";
   true between sessions, false within one, and AC4's hazard is within one.

3. **A repo root contributing nothing was silent.** Running the prescribed
   command from `~/harmonic-forge` resolved both roots to the same repository
   and dropped all of HRSE2, while the aggregate count still read healthy.
   Roots are now keyed by `--git-common-dir` and reported individually; a
   duplicate root and a non-repo root are each named.

4. **SKILL.md's never-silent guarantee pointed at a deleted branch.** It
   attributed Lane 1's belt to `--queue-for`, which it no longer uses.

`report_resolution` is split so the poll cycle reports the same filtered list
it acts on, and #583's comment justifying an unconditional `branch_ahead_lines`
("Lane 1's belt has no --worktrees, so this is a no-op there") is corrected --
that premise is exactly what #590 inverted.

11 further tests; suite 1918 -> 1929, `mise run check` exit 0.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- skills/belt-and-suspenders/SKILL.md |  23 +++--
- tools/gh/test_watch_lane_posts.py   | 112 +++++++++++++++++++++++
- tools/gh/watch_lane_posts.py        | 172 ++++++++++++++++++++++++++++++------
- 3 files changed, 276 insertions(+), 31 deletions(-)

## fix(belt): Lane 1's belt watches the worktrees, not a repo-wide scan (harmonic-forge#590)

The original design puts the repo-wide newest-marker sweep in the SUSPENDERS,
as a backstop. #570's AC7 wording promoted it to the BELT, so Lane 1 started
pulling arbitrary open issues out of GitHub instead of the work actually in
flight -- and collapsed two deliberately independent mechanisms into one,
which is the thing this protocol is named for.

- `watch_lane_posts.py` gains `--all-worktrees`: `git worktree list` at arm
  time, unioned with any `--worktrees`. Ephemeral `/tmp/<repo>-<issue>-impl`
  checkouts appear and vanish per issue, so a hardcoded list narrows the belt
  silently; enumeration is the only spelling that cannot go stale.
- Enumeration runs once per named repo root, not just CWD. `git worktree list`
  sees one repository; Lane 1 spans hrse and harmonic-forge at the same time,
  and seeding from CWD alone would silently halve the belt -- the same failure
  class. Measured live: 19/26 targets resolved across both repos.
- `SKILL.md`'s Lane 1 command is worktrees-first. GitHub enriches an issue a
  worktree has already named; it is not where candidates come from.
- `discover_l1_sweep` is kept and demoted, not deleted, and is now armed as a
  literal command inside the suspenders' "what do I owe" check -- a control
  that exists but is not reached at the point of use is the recurring defect
  behind this whole class.
- 9 new tests, including two doc-sync assertions: the regression that produced
  #590 was a prose edit that read plausibly, so the property is now mechanical.

Also unblocks main: #588 added 5 bytes to `.claude/rules/lane-shorthand.md`
without recording them, leaving the context-budget ratchet failing and the
repo un-committable. The baseline entry is corrected to the measured net.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016PG84ERqwv39ouyC1EANJn
- skills/belt-and-suspenders/SKILL.md | 44 ++++++++++++++---
- tools/gh/test_watch_lane_posts.py   | 95 +++++++++++++++++++++++++++++++++++++
- tools/gh/watch_lane_posts.py        | 48 ++++++++++++++++++-
- 4 files changed, 186 insertions(+), 9 deletions(-)

## feat(belt): read the tick log — no data is not a clean report (harmonic-forge#519)

Two commands, per the ratified plan.

DEFAULT: read-only, zero API calls (AC1). Per-repo counts with every ref
validated against the now-documented '<repo>#<number>' format; an unparseable
entry is its own finding rather than a silent zero, which is the failure that
reads identically to a quiet repo. Zero-event flagging moves from a hardcoded
n>=3 to --window, printed as an UNVALIDATED DEFAULT because the log had zero
records when this shipped. The GraphQL-defect line now names the offending
tick. Every finding states what to do about it, and a clean run prints two
lines rather than none.

--audit: separate, explicit, REST, never scheduled (AC5 vs AC1 are in direct
conflict — a replay needs comment bodies the log does not store).

AC3 gains its missing input: TickLog's matched/emitted/owed_found entries
become {id, posted_at}, where posted_at is the MARKER's timestamp, not the
tick's. Without it only detection-to-action is derivable, and belt downtime is
invisible in its own telemetry. Missing timestamps record as null, never
backfilled from the tick — a fabricated posted_at reads as zero latency.
Bare-string appends still normalise to a well-formed entry.

The report refuses to print zeros against an empty file. 'No log exists',
'a log with zero ticks', and 'a healthy log with no findings' are three
different states and are reported as three different things — the
LANE3_ACTIVE write-only-marker defect is what happens when they collapse.

Found by running it live, not by review: --audit fetched the repo's OLDEST
100 comments (the endpoint defaults to ascending) and reported 'markers in
window: 0' against a repo full of them. Fixed with sort=created&direction=desc;
it now finds 71 markers on vitalharmony/hrse.

Verified: 21 new tests + F518's 25 (updated for the new entry shape, not
weakened) all pass; mutation-checked three ways — silencing malformed-ref
findings, printing zeros for an empty log, and dropping posted_at each fail
the tests that cover them. mise run check shows the same 4 pre-existing
failures as clean origin/main. Live: default run against the real absent log
reports NO DATA; a synthetic 3-tick log written by the real writer exercises
every finding type; --audit replays 100 real comments.
- tools/gh/belt_mechanics.py   |  66 +++++++-
- tools/gh/belt_report.py      | 388 ++++++++++++++++++++++++++++++++++++-------
- tools/gh/test_belt_report.py | 289 ++++++++++++++++++++++++++++++++
- 3 files changed, 679 insertions(+), 64 deletions(-)
## feat(rules): filing and its artifact are one rule, and a deferral names three things (harmonic-forge#516)

R-0039's branches 1 and 2 now read 'File it with its artifact.' The artifact
requirement is deliberately NOT a fourth test: the enumeration says stop at the
first that answers, so anything appended after branch 3 is unreachable for
every issue that ever gets filed, and a fourth branch inside the list is worse
— an issue answering at 1 or 2 terminates before reaching it, and the
contradiction (test 1 says File it, a fourth test says fold it) would then live
inside one span. Attaching the requirement to the two branches where filing
actually happens is unreachable-proof by construction.

Numbering, order and conditions are unchanged, which keeps
skills/memory-triage/SKILL.md:110's 'the bar's test 2' cross-reference alive —
it cites by number and would silently go stale under any renumbering.

Two clarifications fold in at the same span: only 'no one could write it' fails
the bar (lacking the authority does not — Lane 2/3 surface and stop, Lane 1
files), and the operator-scoped exception needs a literal instruction naming
the issue, because R-0232's self-declared Tooling-Exception eligibility would
otherwise let an agent scope its own filing out of the requirement.

New R-0352 states the deferral shape — trigger, owner (never 'someone'), and
record — plus the part that is easy to leave implied and wrong: NOTHING watches
for a fired trigger today. The rule says so rather than implying a sweep that
does not exist, following R-0151's posture on prose detectors.

Id verified at implementation time, not hardcoded: the plan allocated R-0351
and F524 independently took it before either merged. --next-id returns R-0352.

Verified: check_rule_drift.py clean at 275 rules, check_cross_registry.py clean
275+77 with no overlap, tools/run_tests.py at exactly 4 pre-existing failures.
- rules/universal-agent.md  | 54 +++++++++++++++++++++++++++++++++++++++++++----
- tools/rules/registry.toml | 12 +++++++++--
- 2 files changed, 60 insertions(+), 6 deletions(-)
## fix(rules): R-0350 said Lane 2 pushes; the hook denies it — correct the outlier

3-lane-protocol.md's R-0350 read "Lane 2 pushes its own branches and does
not narrate the push." That contradicted three things that agree with each
other: tools/hooks/block_lane2_status_claims.py (harmonic-forge#398), which
denies git push / gh pr create for LANE=2; R-0003 ("Push to the remote only
when the human operator explicitly asks"); and HRSE2's own
.claude/rules/hrse2-extended/lane-protocol.md.

Found live 2026-09-08: a Lane 2 session followed this file, attempted to
push a finished branch, and got the hook's denial.

R-0350 now states the prohibition, names the hook and its wrapper coverage,
and records the old wording so it is not restored as a fix. The registry row
gains the hooks entry it never had — it recorded this rule as unenforced
while its enforcing hook existed.

Verified: check_rule_drift.py and check_cross_registry.py clean (273 + 77
ids); test_rule_registry.py 33 passed, test_cross_registry.py 15 passed;
mutation-checked (reverting text_sha to the old value fails the drift gate
naming R-0350). mise run check shows the same 4 pre-existing failures on
pristine main as on this branch — none introduced here.
- 3-lane-protocol.md        | 23 +++++++++++++++++++++--
- tools/rules/registry.toml |  7 ++++---
- 2 files changed, 25 insertions(+), 5 deletions(-)

## feat(rules): stable rule IDs + registry for rules/*.md (harmonic-forge#447)

Assigns R-NNNN IDs to 140 enforceable rules across all 8 rules/*.md files
and builds the machine-readable registry the violation-counting work needs.
No rule text changed -- 280 insertions, 0 deletions, every added line a
paired marker, asserted mechanically not by reading.

Scheme: global monotonic R-NNNN, registry-as-allocator. Chosen against the
relocation-stability constraint (the corpus trim this unblocks moves rules
between files in bulk), which rules out per-file sequential; content-derived
was rejected because it changes when wording changes, defeating 'violations
of this rule since it was written'. Concurrent allocation can collide at
merge -- detected by the drift check, resolved as an ordinary merge
conflict, since preventing it needs a central allocator.

Classification unit is the OBLIGATION, not the bullet: one bullet may yield
several IDs (3-lane-protocol.md:133-153 carries two rules with different
enforcement layers plus narrative), and narrative attaches to the rule it
justifies rather than getting an ID.

Per the spec's three named changes:
- text_sha over the annotated span, whitespace-normalised per line, so a
  reflow is not drift but a word change is. statement is a paraphrase and
  cannot detect a rewording. file/anchor are descriptive only; the inline
  marker is the locator.
- hooks = [{script, events, wired_in}], not a scalar enforcement field.
  The live scan covers 7 settings.json locations plus agent frontmatter and
  finds 21 distinct hook scripts with visibly non-uniform wiring -- so 'is
  rule X mechanized' has no single answer and any scalar would be false
  somewhere.
- restates field carries AC4's duplicate relation; 10 rules restate another.

Two hooks a tools/hooks-scoped, single-settings-file scan misses entirely:
block_closing_keywords.py (user-global only, and outside tools/hooks/) and
deny_advisory_subagent_gh_writes.py (agent frontmatter only).

Scope: rules/*.md. 3-lane-protocol.md is the second annotation pass and is
where most hook-enforced rules actually live -- which is why AC4's
hook-backed duplicate count is 0 in this pass and will not stay that way.
20 tests; full forge suite 815 passing.
- tools/rules/query_rules.py        |  242 ++++++++
- tools/rules/registry.toml         | 1173 +++++++++++++++++++++++++++++++++++++
- tools/rules/test_rule_registry.py |  273 +++++++++
- 12 files changed, 2180 insertions(+)

## fix(hooks): preclose-inspection findings — python heredoc writes, /dev/null and quoted '>' false positives, harmonic-forge Edit|Write wiring (harmonic-forge#440)
- .claude/settings.json               |  5 +++
- tools/hooks/model_tier_gate.py      | 76 +++++++++++++++++++++++++++++++++++--
- tools/hooks/test_model_tier_gate.py | 42 +++++++++++++++++++-
- 3 files changed, 119 insertions(+), 4 deletions(-)

## fix(hooks): model_tier_gate covers Bash writes, exempts Lane 3 (harmonic-forge#440)
- .codex/hooks.json                   |   6 ++
- tools/hooks/model_tier_gate.py      | 114 +++++++++++++++++++++++-
- tools/hooks/test_model_tier_gate.py | 171 ++++++++++++++++++++++++++++++++++++
- 3 files changed, 290 insertions(+), 1 deletion(-)

## fix(hygiene): report open, mergeable, green PRs — the merge-forgotten state (harmonic-forge#438)

Nothing in the toolchain reported an open PR that is mergeable and passing
but not merged; `audit_repo()`'s own skip of open-PR branches (correct —
not stranded) meant that state was invisible everywhere, not mislabeled.
PRs #1451/#1501/#1513 sat unmerged, unnoticed, for up to a day.

- New `audit_open_prs()`: one detail fetch per currently-open PR (mergeable/
  mergeable_state only exist there, never on the list endpoint), split into
  `green_unmerged_prs` (mergeable and mergeable_state == 'clean' — fails the
  check) and `blocked_open_prs` (waiting on a human — report-only).
- Existing STRANDED/ORPHANED classification (audit_repo) untouched; the
  else-arm docstring stays as-is per the issue author's own correction.
- 9 new tests; all 765 tests pass.
- tools/gh/repo_hygiene.py      |  85 +++++++++++++++++++++++++++-
- tools/gh/test_repo_hygiene.py | 125 ++++++++++++++++++++++++++++++++++++++++++
- 2 files changed, 208 insertions(+), 2 deletions(-)

## feat(lane3): add dual fixed-root Gemini adapters
- tools/lane/lane3                                 | 18 -----
- tools/lane/policies/gemini-lane3.toml            | 68 +++++++++++++++++--
- tools/lane/test_lane3_context_mcp.py             | 10 +--
- 5 files changed, 166 insertions(+), 31 deletions(-)

## fix(lane3): choose Gemini target before launch
- tools/lane/lane3 | 18 ++++++++++++++++++
- 1 file changed, 18 insertions(+)

## feat(lane3): fetch named Gemini gate comments
- tools/gemini/lane3-context/lane3_context_mcp.py  | 28 ++++++++++++++++++++++++
- tools/lane/policies/gemini-lane3.toml            |  6 +++++
- tools/lane/test_lane3_context_mcp.py             | 13 +++++++++--
- 4 files changed, 46 insertions(+), 3 deletions(-)

## fix(lane3): route Gemini context by issue prefix
- tools/gemini/lane3-context/lane3_context_mcp.py | 63 +++++++++----------------
- tools/lane/test_lane3_context_mcp.py            | 49 ++++++-------------
- 2 files changed, 38 insertions(+), 74 deletions(-)

## feat(lane3): add bounded Gemini gate reporting
- tools/gemini/lane3-context/lane3_context_mcp.py  | 51 +++++++++++++++++++++---
- tools/lane/policies/gemini-lane3.toml            |  6 +++
- tools/lane/test_lane3_context_mcp.py             | 20 ++++++++--
- 4 files changed, 69 insertions(+), 10 deletions(-)

## fix(lane3): complete MCP policy catch-all
- tools/lane/policies/gemini-lane3.toml | 1 +
- 1 file changed, 1 insertion(+)

## fix(lane3): use scalar Gemini policy tool rules
- tools/lane/policies/gemini-lane3.toml | 58 ++++++++++++++++++++++++++++-------
- 1 file changed, 47 insertions(+), 11 deletions(-)

## fix(lane3): validate Gemini extension registration
- tools/lane/lane3                  | 15 ++++++++-------
- tools/lane/test_lane_launchers.py | 30 +++++++++++++-----------------
- 2 files changed, 21 insertions(+), 24 deletions(-)

## feat(lane3): add bounded Gemini context extension
- tools/lane/policies/gemini-lane3.toml              |  27 +++-
- tools/lane/test_lane3_context_mcp.py               | 114 ++++++++++++++++
- tools/lane/test_lane_launchers.py                  |  52 +++++++-
- 10 files changed, 428 insertions(+), 31 deletions(-)

## feat(lane): Gemini Lane 3 admin policy + pre-staged context (harmonic-forge#326)
- tools/lane/_agent_registry.sh         |  12 ++-
- tools/lane/lane3-stage-context        | 154 +++++++++++++++++++++++++++++++
- tools/lane/lane3_safety_additions.txt |  15 ++-
- tools/lane/policies/gemini-lane3.toml | 169 ++++++++++++++++++++++++++++++++++
- tools/lane/test_lane_launchers.py     |  85 +++++++++++++----
- 5 files changed, 410 insertions(+), 25 deletions(-)

## feat(canary): rebuild the deny-canary for Lane 3, fix #413's assertion defects (harmonic-forge#326)
- 3-lane-protocol.md                                 |  39 +-
- ...-agent-adapter-contract-and-capability-tiers.md |  20 +-
- tools/lane/policies/canary/run_canary.py           | 551 ++++++++++++++-------
- 3 files changed, 438 insertions(+), 172 deletions(-)

## fix(canary): kill the process group on timeout, per-check ceilings (harmonic-forge#326)
- tools/lane/policies/canary/run_canary.py | 277 +++++++++++++++++++++++--------
- 1 file changed, 207 insertions(+), 70 deletions(-)

## test(lane): capture pre-refactor launch tuples as the AC8 baseline (harmonic-forge#322)
- tools/lane/baseline_capture.py         | 314 ++++++++++++
- tools/lane/baseline_launch_tuples.json | 888 +++++++++++++++++++++++++++++++++
- tools/lane/lane3_safety_additions.txt  |  40 ++
- 3 files changed, 1242 insertions(+)

## feat(lane): closed agent registry, laneN --agent, Lane 3 check-only (harmonic-forge#322)
- tools/lane/_agent_registry.sh     | 259 +++++++++++++
- tools/lane/_cli_launch.sh         | 344 ++++++++++++-----
- tools/lane/_lane_args.sh          |  97 +++++
- tools/lane/baseline_capture.py    | 121 +++++-
- tools/lane/lane1                  |  24 +-
- tools/lane/lane2                  |  50 ++-
- tools/lane/lane3                  | 217 ++++++++---
- tools/lane/lane3-provision        | 101 +++++
- tools/lane/test_lane_launchers.py | 792 ++++++++++++++++++++++++++++++++++++++
- tools/run_tests.py                |   2 +-
- 10 files changed, 1812 insertions(+), 195 deletions(-)

## docs(lane): --agent is the documented interface; record #322's decisions (harmonic-forge#322)
- 3-lane-protocol.md                                 | 109 +++++++++++++++++----
- ...-agent-adapter-contract-and-capability-tiers.md |  90 ++++++++++++++---
- 2 files changed, 166 insertions(+), 33 deletions(-)

## fix(tooling): resolve model tier gate issue targets (harmonic-forge#367)
- ...-agent-adapter-contract-and-capability-tiers.md |  2 +-
- tools/hooks/model_tier_gate.py                     | 89 +++++++++++-----------
- tools/hooks/test_model_tier_gate.py                | 85 +++++++--------------
- 6 files changed, 90 insertions(+), 108 deletions(-)
## fix(tooling): harden permission hook command classification (harmonic-forge#369)
- tools/hooks/batch_auth.py             | 20 +++++++++++++++++++-
- tools/hooks/block_irreversible_ops.py | 16 ++++++++++++----
- tools/hooks/shell_parse.py            | 21 +++++++++++++++++++++
- 4 files changed, 60 insertions(+), 5 deletions(-)

## fix(tooling): transaction-log.md merge=union to end structural branch conflicts (harmonic-forge#376)
- .gitattributes | 15 +++++++++++++++
- 1 file changed, 15 insertions(+)

## feat(tooling): Lane 2 status-post integrity -- receipts, wrapper-only posting (harmonic-forge#371)

Reimplements harmonic-forge#371 after a prior Lane 2 (Codex) attempt got
to 'L2 blocked' with only receipt_runner.py/l2_post.py/the deny hook as
dense, likely-incomplete stubs, and that branch also carried the same
unrelated sprint-plan config_loader deletion seen on #366's Codex
attempt -- discarded, not merged from, same reasoning as that issue.

Adds tools/gh/receipt_runner.py (issue-scoped, content-digest command
receipts under .git/lane2-receipts/<issue>/, lock-on-nonzero-exit),
tools/gh/l2_post.py (post/snapshot/resolve-lock subcommands; posting
requires a mandatory post/fetch/diff self-check before reporting
success, and refuses ordinary status composition while an issue is
locked, except a legitimate --kind blocked), and
tools/hooks/block_lane2_status_claims.py (repo-agnostic raw-post deny
for LANE=2, reusing block_lane1_status_claims.py's is_direct_transport
rather than reimplementing it). Wired for Claude Code
(.claude/settings.json) and Codex (new .codex/hooks.json, pointed
directly at the canonical module -- harmonic-forge has no per-repo
gate_codex_tool.py the way HRSE2 does). Four new mise tasks
(l2-run/l2-post/l2-snapshot/l2-resolve-lock). ADR-007 Sec7 gains the new
guard-equivalence row.

27 new unit tests (490 total, up from 463), all passing. Live-verified
end to end against a disposable throwaway issue (harmonic-forge#373,
closed): real receipt-backed command -> real GitHub post with a
passing self-check -> a genuine failing command creating a real lock
-> a normal completion post correctly refused while locked -> a
legitimate blocked status correctly bypassing the lock -> resolve-lock
correctly refused against a fake comment id and correctly clearing
against the real one. The deny hook's decision() is exhaustively unit
tested (13 cases: LANE 2/1/3/unset, two different target repos, cd
prefixing, malformed payload) and its CLI entrypoint verified against
hand-built Claude- and Codex-shaped stdin payloads.
- tools/gh/test_receipt_runner.py                    |  69 +++++++++
- tools/hooks/block_lane2_status_claims.py           | 101 +++++++++++++
- tools/hooks/test_block_lane2_status_claims.py      | 110 +++++++++++++++
- 10 files changed, 752 insertions(+)

## fix(tooling): bake the {summary,findings} reply contract into cross_family_call.sh itself (harmonic-forge#366)

Fixes the defect Lane 1 found live while building #366's Lane 3 gate
sweep evidence: a probe brief with no explicit reply-shape instruction
produced a fully correct plain-prose answer from Codex that
emit_envelope's codex branch then classified invalid-report, because
nothing in invoke_codex() or the caller's brief told Codex to answer in
the shared JSON contract. Every correct Codex answer was failing,
silently, for every caller except Codex itself -- TC1's 2-of-3 vote,
and this issue's whole premise, depended on this working.

Fix applies to all three families uniformly, not just Codex: a fixed
REPORT_CONTRACT suffix is now appended to every brief inside
prompt_text(), so a caller gets a working contract even if it never
thinks to ask for one, rather than depending on brief-authoring
discipline the helper cannot enforce.

Reproduced live before and after: same bare brief (no JSON-shape
instruction), same seeded-defect scratch repo. Before: Codex target ->
status invalid-report despite a fully correct diagnosis in its native
output. After: Codex and Gemini targets both -> status ok with the
seeded defect correctly quoted as evidence. Re-verified TC2 (Gemini
read-only write denial) unaffected by the change, isolated from the
same-cwd cross-contamination a naive re-test would have introduced.
mise run check -- 463 tests pass.
- tools/lane/cross_family_call.sh | 26 +++++++++++++++++++++++---
- 1 file changed, 23 insertions(+), 3 deletions(-)

## feat(tooling): cross-family headless call helper -- ADR-007 amendment, admin-policy Gemini boundary (harmonic-forge#366)

Reimplements harmonic-forge#366 after Lane 1 sent the prior Lane 2 (Codex)
attempt back: the read-only posture's Gemini boundary used
--approval-mode plan, which live reproduction showed does not reliably
block a write (the model can still call write_file and narrate a false
success). Replaced with an admin-tier deny policy
(tools/lane/gemini-read-only-deny.toml, --admin-policy), the mechanism
harmonic-forge#326 already proved survives --yolo and removes denied
tools from the model's tool list entirely.

Adds tools/lane/cross_family_call.sh (closed caller/families/posture/
brief/cwd surface, locked caller-keyed family order, normalized
JSON-lines envelope per family), the pager/editor env fix to
_cli_launch.sh's gemini branch, and a bounded ADR-007 section
transcribing the family-order table, posture mapping, and consumer
table.

Also carries a new accepted-residual-gap note: live testing found
'codex exec --sandbox read-only' does not reliably block a file write
either (the file-edit tool completes even when shell redirection is
denied at the OS level) -- out of this issue's Gemini-scoped acceptance
criteria, flagged rather than silently fixed or ignored.
- tools/lane/_cli_launch.sh                          |   4 +
- tools/lane/cross_family_call.sh                    | 191 +++++++++++++++++++++
- tools/lane/gemini-read-only-deny.toml              |  36 ++++
- 4 files changed, 334 insertions(+)

## fix: validate sprint-plan schema contract (harmonic-forge#104)
- skills/sprint-plan/config_loader.py                |  89 ++++++-----
- .../schema/sprint-plan.config.schema.json          |  55 ++++++-
- skills/sprint-plan/test_config_loader.py           | 163 ++++++++++++++++-----
- 3 files changed, 234 insertions(+), 73 deletions(-)

## feat: add sprint-plan config loader (harmonic-forge#104)
- skills/sprint-plan/config_loader.py                | 107 +++++++++++++++++++++
- .../schema/sprint-plan.config.schema.json          |   1 +
- skills/sprint-plan/test_config_loader.py           |  48 +++++++++
- 3 files changed, 156 insertions(+)

## feat: add --theme and --venture flags to gh_issue.py (F308)

Generalizes _set_tier() into a shared _set_single_select() (Tier/Theme/Venture no longer three copies of the same match/edit/error shape), threads --theme/--venture through add_to_board() with the same hard-fail-if-requested-but-unwritten semantics Tier already has, reads option lists live from the board rather than hardcoding them (harmonic-forge#300), and prints the real available options on an unmatched value. HRSE2's gh-new-issue wrapper updated in the same change; mise run wrapper-parity passes. 13 new tests (48 total, up from 35), all passing. Live-verified against both boards (hrse #1, forge #3) including the invalid-value error path; three throwaway issues created and closed.
- tools/gh/README.md        |  12 +++-
- tools/gh/gh_issue.py      | 119 +++++++++++++++++++++++++++---------
- tools/gh/test_gh_issue.py | 150 ++++++++++++++++++++++++++++++++++++++++++++++
- 3 files changed, 250 insertions(+), 31 deletions(-)

## fix: resolve BASH_SOURCE through its symlink in lane1/2/3 (F305 regression)

~/.local/bin/lane{1,2,3} are symlinks into this directory; BASH_SOURCE[0] reflects the invoked path, not the symlink target, so dirname resolved to ~/.local/bin instead of here and the new source line 404'd -- broke all three launchers live. readlink -f before dirname fixes it; verified through the actual ~/.local/bin symlinks, not just the real files.
- tools/lane/lane1 | 2 +-
- tools/lane/lane2 | 2 +-
- tools/lane/lane3 | 2 +-
- 3 files changed, 3 insertions(+), 3 deletions(-)

## feat(gh): repo hygiene backstop — four remaining gaps (hrse#808)

Adds three local-checkout audits (checkout not on main, stale stashes,
merged commits missing a transaction-log.md entry) and fixes
_BOARD_ITEMS_QUERY's hardcoded `user(login:)` GraphQL owner type — it now
tries organization first, falls back to user, so a future org-owned
client-repo instance resolves correctly instead of silently 404ing.

The transaction-log check went through a live-verified redesign mid-
implementation: an initial pure headline-match version produced 524/588
false positives on hrse, because a squash-merged PR's own final subject
is the PR title, never any local commit's message, so it rarely matches
a documented headline even when `mise run commit` genuinely ran somewhere
in the PR. Fixed to also accept "this commit's own diff touched
transaction-log.md at all" as an independent, sufficient signal.

The owner-type fallback also needed a second live fix: `gh api graphql`
exits nonzero for `organization(login:)` against a real user-owned login
rather than returning a graceful null, so the fallback's first version
never actually reached the user() attempt — silently skipping the
migration/unboarded sweep on every repo, every run, until caught live.
- tools/gh/repo_hygiene.py      | 275 +++++++++++++++++++++++++++++++--
- tools/gh/test_repo_hygiene.py | 344 ++++++++++++++++++++++++++++++++++++++++++
- 2 files changed, 607 insertions(+), 12 deletions(-)

## tooling: scope gh CLI active-account state per-project via GH_CONFIG_DIR (F305)

lane1/2/3 now export GH_CONFIG_DIR based on the target project's git remote, so a global gh auth switch in one project can no longer lock out a concurrent lane session in another. Verified live against both existing account dirs (gh-vitalharmony, gh-harmonicarchitect) and the no-match fallthrough case.
- tools/lane/lane1             |  2 ++
- tools/lane/lane2             |  2 ++
- tools/lane/lane3             |  2 ++
- 4 files changed, 32 insertions(+)

## docs: document gh-as in foundation documents and directives (harmonic-forge#240)

gh-as was documented in tools/gh/README.md and the root README.md — enough
for someone who goes looking, not enough for a tool that governs credential
safety in multi-person and multi-agent work. The actors most likely to reach
for gh auth switch are the ones who never read tools/gh/README.md: agents
that load only universal-agent.md and 3-lane-protocol.md, and developers
following §6 onboarding.

- rules/universal-agent.md: the directive itself, in SECURITY, since this is
  the file agents actually load
- 3-lane-protocol.md: agent-readable standing rule for every lane, in the
  same register as GitHub Comment Formatting
- harmonic-forge.md §3: gh-as in the platform repo structure
- harmonic-forge.md §6: names gh-as as the mechanism enforcing the
  credential-isolation principle already stated there — gh auth switch
  cannot be part of that answer, since global identity mutation is exactly
  the cross-project bleed the principle exists to prevent
- harmonic-forge.md §6 onboarding: one-time gh-as --init setup step
- docs/onboarding-greg.md: the multi-account case, in the Security Model
  section he is told to read first — highest-risk case on the platform
- docs/onboarding-kyle.md: brief pointer; single-account today

Corpus swept: every remaining mention of gh auth switch is a warning against
it, none recommend it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
- [docs] Markdown-only commit — no code changes. Files: 3-lane-protocol.md, docs/onboarding-greg.md, docs/onboarding-kyle.md, harmonic-forge.md, rules/universal-agent.md

## tools/gh: add gh-as — per-process GitHub account scoping (harmonic-forge#235)

gh auth switch is global mutable state: switching accounts for one project
silently changes the active identity for every other concurrent session on
the machine, and there is no reliable way to restore it when two sessions
are running. Observed live 2026-08-11 — an account switch was reverted by a
different session mid-task, surfacing as a confusing error.

gh-as scopes gh to a named account per process via a per-account
GH_CONFIG_DIR under ~/.config/gh-accounts/<account>. Nothing to undo: the
scoping lives and dies with the process, so concurrent sessions cannot
collide. This is what harmonic-forge.md §6's credential-isolation principle
already required; there was no sanctioned mechanism for it until now.

Guards on every invocation: refuses an unconfigured slot, refuses an expired
or revoked token, and refuses when a slot's authenticated identity does not
match its name — so a command can never run against the wrong account
because a token was replaced out of band.

Documented in tools/gh/README.md and in README.md's credential-isolation
section as the sanctioned alternative to gh auth switch.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
- README.md          | 13 ++++++++
- tools/gh/README.md | 29 ++++++++++++++++--
- tools/gh/gh-as     | 89 ++++++++++++++++++++++++++++++++++++++++++++++++++++++
- 3 files changed, 128 insertions(+), 3 deletions(-)

## feat: deny Lane 3 from posting its own AE authorization comment (harmonic-forge#216)

New PreToolUse hook, sibling to remind_gate_readiness_sweep.py: denies a
LANE=3 session from posting a comment body matching the AE-authorization
heading shape, across both the wrapper posting tasks (lane-comment,
post-comment, post_lane_discussion.py) and raw gh issue comment --body/
--body-file -- the latter matters because raw gh is explicitly permitted
for Lane 2/3 and would otherwise be a silent, unmarked bypass.

Fenced-code-block stripping added after the test suite caught a real gap:
the heading-anchor regex alone denied a comment that only *quoted* the AE
format inside a markdown fence (this very issue's own body does exactly
that) -- fixed and re-verified before committing.
- tools/hooks/deny_lane3_ae_self_post.py      | 199 ++++++++++++++++++++++++++++
- tools/hooks/test_deny_lane3_ae_self_post.py | 192 +++++++++++++++++++++++++++
- 2 files changed, 391 insertions(+)

## docs: bound live verification of pre-existing services (forge#132)
- [docs] Markdown-only commit — no code changes. Files: 3-lane-protocol.md

## feat(agents): add read-only advisory profiles and sync them to .devin/agents
- agents/sticky-wicket/AGENT.md    |  69 ++++++++++++++++
- rules/universal-claude.md        | 165 ++++++++++++++++++++-------------------
- sync_rules.py                    |  73 ++++++++++++++++-
- 5 files changed, 362 insertions(+), 84 deletions(-)

## docs: codify gitignored-local-file pattern for passing sensitive real-world data between lanes
- [docs] Markdown-only commit — no code changes. Files: rules/universal-agent.md, templates/lane1-handoff.md

## docs: harmonic-forge.md's repo-layout tree was missing tools/gh/, .githooks/, mise.toml, transaction-log.md
- [docs] Markdown-only commit — no code changes. Files: harmonic-forge.md

<!-- TRANSACTION_LOG_END -->
