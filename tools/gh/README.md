# tools/gh/ — repo-agnostic GitHub issue/comment helpers

Two `gh`-CLI wrappers, extracted from HRSE2's originals (harmonic-forge#53)
after a hardcoded repo default caused a real mis-post: `mise run
post-comment` was run for an `harmonic-forge` issue out of HRSE2 habit, and —
because the script defaulted to `REPO = "vitalharmony/hrse"` — silently
posted the comment to an unrelated HRSE2 issue instead. Caught and fixed
immediately, but it's the same failure shape as ADR-004/005 already named
for a different mechanism: **make the compliant path the only easy one to
invoke.** A hardcoded default that can silently target the wrong repo is
the opposite of that — so both tools require `--repo` explicitly, with no
default, and print a banner naming the resolved target before acting.

## Files

- **`post_comment.py`** — post a GitHub issue comment via `--body-file`,
  then refetch and byte-diff it against the source (ADR-004/005's mandatory
  self-check). No project-specific state beyond the repo slug.
  ```bash
  python3 post_comment.py --repo vitalharmony/hrse --issue 251 --file /path/to/body.md
  ```
- **`gh_issue.py`** — `gh issue create`, then optionally `gh project
  item-add` + set `Status=Todo` if a project board is configured. Board
  owner/number come from `--project-owner`/`--project-number` or the
  `GH_PROJECT_OWNER`/`GH_PROJECT_NUMBER` env vars (a project's own
  `mise.toml` supplies these via `[env]`). **Board-add is skipped, not
  treated as an error, when neither is set** — not every project has a
  project board yet, and this tool shouldn't force one into existing.
  ```bash
  GH_PROJECT_OWNER=vitalharmony GH_PROJECT_NUMBER=3 \
    python3 gh_issue.py --repo vitalharmony/harmonic-forge --title "..." --labels "tech-debt"
  ```

- **`fetch_lane1_context.py`** — for Lane 3 test-spec derivation
  (harmonic-forge#253/#254): fetches the issue body plus only Lane-1-authored
  comments, never the full thread, so Lane 3 structurally cannot see Lane
  2's completion comment while deriving its spec — a rule that held as
  prose and still got violated twice on the same issue (vitalharmony/hrse#793)
  before this script closed the gap mechanically. Named explicitly in
  `3-lane-protocol.md`'s Lane 3 step; a manual `gh` read of the full thread
  is not a substitute.
  ```bash
  python3 fetch_lane1_context.py --repo vitalharmony/hrse --issue 848
  ```

- **`gh-as`** — run a command with `gh` scoped to a specific GitHub account,
  **without touching global auth state**. Each account gets its own `gh`
  config directory under `~/.config/gh-accounts/<account>`, applied
  per-process via `GH_CONFIG_DIR`, so concurrent sessions working on
  different repos cannot collide.
  ```bash
  gh-as --init harmonicarchitect                       # one-time, per account
  gh-as harmonicarchitect gh issue list -R owner/repo
  gh-as harmonicarchitect python3 some_script.py       # children inherit scoping
  gh-as --list                                         # slots, and who each resolves to
  ```
  **Use this instead of `gh auth switch`.** `gh auth switch` is global mutable
  state: it changes the active identity for every other session on the
  machine, and there is no reliable way to restore it when two sessions are
  running. `gh-as` has nothing to undo — the scoping lives and dies with the
  process.

  Guards on every invocation: refuses an unconfigured slot; refuses an expired
  or revoked token; and **refuses if a slot's authenticated identity does not
  match its name**, so a command can never run against the wrong account
  because a token was replaced out of band.

`post_comment.py` and `gh_issue.py` are importable (`from post_comment import
post_comment`, `from gh_issue import create_issue, add_to_board`) as well as
CLI-invocable, same convention as `tools/transaction-log/`. `gh-as` is a shell
wrapper — put it on `PATH` (symlink into `~/.local/bin/`) or invoke it by path.

## Wiring into a project's `mise.toml`

Set the repo (and, if applicable, board) slug once in `[env]`, then have
your tasks call these with `$GH_REPO` etc. — the value lives in one visible
config line per project instead of being hand-copied into a script:

```toml
[env]
GH_REPO = "vitalharmony/hrse"
GH_PROJECT_OWNER = "vitalharmony"
GH_PROJECT_NUMBER = "1"

[tasks.gh-new-issue]
run = 'python3 ~/harmonic-forge/tools/gh/gh_issue.py --repo "$GH_REPO" --title "$usage_title" --body "$usage_body" --labels "$usage_labels"'

[tasks.post-comment]
run = 'python3 ~/harmonic-forge/tools/gh/post_comment.py --repo "$GH_REPO" --issue "$usage_issue" --file "$usage_file"'
```

A project without a board simply omits `GH_PROJECT_OWNER`/
`GH_PROJECT_NUMBER` — `gh_issue.py` prints a notice and skips that step
rather than failing.

## GraphQL vs REST quota (harmonic-forge#203)

`gh`'s API access splits across **two separate 5000/hr quotas**. Draining
one does not touch the other. **`gh api rate_limit`'s JSON body is
confirmed live-unreliable for the `core` bucket** (harmonic-forge#222/#231
— disagreed with header ground truth by a wide margin on a real request);
read the bucket-appropriate source below instead of trusting that
endpoint's body for either number.

- **REST (`.resources.core`)**: issue/comment reads and writes
  (`gh api repos/{owner}/{repo}/issues ...`). Per-request cost, cheap and
  predictable. Check remaining headroom via the `X-Ratelimit-*` response
  headers on any real REST call (`gh api --include ...`), not
  `/rate_limit`'s `core` field.
- **GraphQL (`.resources.graphql`)**: `gh project *` — Status/Priority/
  Sequence/Estimate field reads and writes. **Cost-based, not
  per-request** — GitHub bills by query complexity/node count, confirmed
  live: a single `gh project item-list --limit 1000` fetching hundreds of
  items with nested fields can burn hundreds-to-thousands of points in one
  call, enough to fully drain the quota from a handful of calls. Projects
  v2 has **no REST equivalent** — every board touch is GraphQL, there is
  no escape hatch for board operations themselves. Prefer GraphQL's own
  purpose-built `rateLimit` field over `/rate_limit`'s `graphql` value
  (`gh api graphql -f query='query { rateLimit { remaining resetAt } }'`,
  harmonic-forge#222) — both agree with header ground truth for this
  bucket specifically (unlike `core`, see above), but `rateLimit`
  doesn't cost a separate REST call and is confirmed live not to consume
  real quota itself.
- **`gh issue create` (the CLI subcommand) also uses GraphQL internally**,
  even though issue creation is REST-capable — found live when it failed
  with a GraphQL rate-limit error while the REST quota sat untouched.
  `create_issue()` in `gh_issue.py` calls `gh api -X POST` directly
  instead, for exactly this reason.

If you hit `GraphQL: API rate limit already exceeded`: REST reads (issue
bodies, comments) are almost certainly still available — check
`.resources.core` before assuming you're fully blocked. If you must keep
writing to a board, throttle (short sleep between `item-edit` calls) rather
than retry-looping, and reuse one `item-list` fetch across multiple field
writes rather than re-fetching per field (see `_fetch_project_context()` in
`gh_issue.py` and the on-disk cache in `../hooks/model_tier_gate.py`).

**Tradeoff:** `model_tier_gate.py`'s 120s cache means a mid-lane
re-estimation crossing its threshold can be missed for up to that TTL —
accepted as bounded and strictly better than the unbounded gap fail-open
already tolerates (e.g. `gh` unavailable entirely).

## What this does not cover

- Which repo/board a project uses — that's the one thing every project
  supplies itself, by design (see the incident above for why it's not a
  default).
- Migrating a project already running its own local copy of these scripts
  (e.g. HRSE2's `scripts/post_comment.py`/`scripts/gh_issue.py`) — that's
  a separate, per-project follow-up issue, not part of this extraction.
