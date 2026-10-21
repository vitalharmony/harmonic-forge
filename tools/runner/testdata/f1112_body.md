## Problem

Pilot run 4 (`run-20261021T002107Z-9124b530`, after harmonic-forge#1107) set every item aside, and no item could have come out green: each of `mise run check`, the kill checks and the preclose driver fails on unchanged `main` inside the boundary. Runs 1–4 each surfaced one defect at a time, because the reducer reports only the first failing clause and the run deletes its own evidence. This issue fixes every defect found by a stage-by-stage audit (below), and adds a preflight that fails the run before any agent spend when any stage cannot work.

## Evidence (reproduced outside the runner, 2026-10-20, base `d9716bc`)

| # | Stage | Defect | Reproduction |
|---|---|---|---|
| D1 | check (oracle) and the agent's own `mise run check` | mise writes `~/.local/state/mise/trusted-configs/<clone>` to trust the new clone's `mise.toml`; the boundary binds that dir read-only | `mise ERROR failed to ln -sf <clone> …/trusted-configs/… Read-only file system (os error 30)`, rc 1 before any task runs. `MISE_TRUSTED_CONFIG_PATHS=<clone>` alone still fails; adding `MISE_STATE_DIR=/tmp/mise-state` (tmpfs inside the boundary) lets the check run to completion |
| D2 | kill checks, and 51 check failures | `SAFE_PATH` (`PYTHONSAFEPATH=1`) drops the cwd/script dir from `sys.path`, so `python3 -m unittest tools/gh/x.py` and `python3 tools/gh/x.py` cannot import the repo | F1008's 5 kill checks: `baseline_rcs: [1]`, verdict `broken-test` (receipt `~/.claude/state/preclose/vitalharmony_harmonic-forge_1008.kill.json`). Same AC1 check in the oracle boundary: `ModuleNotFoundError: No module named 'tools'` with SAFE_PATH, `Ran 2 tests … OK` without |
| D3 | preclose | `preclose_check.py` plan refuses without a passing kill receipt, so D2 makes the driver exit 2 on every item | log `te-run preclose: driver exit 2` on F1015, F1008 (runs 3 and 4) |
| D4 | check | the `--shared` clone's `origin` is the local path, so manifest code fails `repo '/home/…/harmonic-forge' is not owner/name` | 9 failures in `test_l1_post_*`, `test_post_lane_discussion_*`, `test_belt_candidates_real_store_guard`; gone after `git remote set-url origin https://github.com/vitalharmony/harmonic-forge.git` |
| D5 | check | 40 tests depend on the host outside a user namespace: 36 (`test_lane_launchers`, `test_lane_refresh`, `test_lane_identity`) read the real `/usr/local/share/hrse-gate/claude-lane3.json`, which shows uid 65534 inside bwrap (`stat` in the boundary: `65534 nobody`); 3 `test_te_run.RealBoundary/RealPipeline` need nested bwrap; 1 `test_lane3_audit` expects `/nonexistent-root/…` unwritable, but the bwrap root is a tmpfs | with D1+D2+D4 fixed: `Ran 6204 tests … FAILED (failures=38, errors=4)`, all in those modules |
| D6 | agent | `te-runner.settings.json` + `dontAsk` refuses common commands | probe (`claude -p`, the runner's settings, 33 commands, $0.03): denied `FOO=1 python3 …`, `pytest`, `mkdir`, `rm`, `cp`, `bash -c`, `git checkout -b`, `git restore`, `ruff`, `mise tasks ls`, `cd /tmp`. Allowed: `git grep`, `git stash list`, `gh issue view`, `timeout …`, read-only pipes, `cd` inside the clone. `python3 -m pytest`: not installed. F1015's agent stopped on a denial without committing (`self_flag`: "Bash was denied in don't-ask mode, so the change is untested and uncommitted."); F1008 had 7 denials |
| D7 | agent prompt | the prompt says "follow the Lane 1 handoff exactly", but handoffs name steps the runner owns and the agent cannot reach (the kill bundle under `~/.local/share/te-runner/kill-bundles/`, `preclose-check`) and live-only ACs | F1008 `self_flag`: "not complete — the runner kill-check bundle and `preclose-check` were not run, the kill checks were not mutation-tested by hand, and the AC6 tick-log measurement needs a deployed belt." |
| D8 | evidence | `stream.jsonl`, its `.stderr`, transcripts, `revert.patch`, `checks.json` and the preclose `--out` dir live in `tempfile.TemporaryDirectory` and are deleted when the item ends; `mise run check` and `preclose_driver.py` stdout and stderr are never kept | the denied commands of F1015 and F1008 cannot be recovered; the kill receipt's `patch_path` names a deleted file |
| D9 | verdict | `verdict.reduce` returns the first failing clause only | run 4 reports `tool-refusal` for F1008 while D1, D2 and D3 also failed |
| D10 | reviewer PATH | the unit's PATH resolves `codex` to `/usr/local/bin/codex` 0.144.5; the lanes run `~/.npm-global/bin/codex` 0.162.1 | both answer a `codex exec` call inside the reviewer boundary; version parity only |
| D11 | preclose driver | `preclose_driver.acceptance_criteria` (`tools/runner/preclose_driver.py:107-115`) and `te_run.ac_titles` match only `^### Amendment 3: D12, D13 (stage rehearsal on F1073's head `87d90899`)

**D12. The report and its notification are never produced.** `report.py render` and `report.py notify` exist (harmonic-forge#1056) but nothing calls them: `git grep -n "report.py" -- tools ':!tools/runner/test_*' ':!tools/runner/report.py'` finds no caller, and neither unit runs them. Run 4 set all three items aside and ended `Result=success`, so it sent no notification of any kind; the operator learns of a finished run only by asking.

**D13. The cross-family call always checks nothing, so no item can reach green.** In the rehearsal the reviewer returned one assumption, verdict `uncheckable` ("Fixture writes were blocked by the read-only review; other errors included \"SystemExit: sprint-plan summary: not inside a git repository.\""). `cross_family_provenance.py --own-model claude-opus-5-5` labels that envelope:

```
Red-team provenance: in-family fallback (claude-opus-5-5) — cross-family call ran but checked nothing: all 1 verdict(s) uncheckable. The reviewer could not reach the evidence -- check that --cwd named the repository the artifact lives in, not an empty scratch directory
```

The reducer's preclose clause requires the triggered (cross-family) label (`verdict.py:66`, mirroring `preclose_check.PROVENANCE_TRIGGERED`), so every item that passed everything else would still be set aside. Causes: `export_head` (`preclose_driver.py:142-149`) unpacks a `git archive`, so the tree is not a repository; the verify posture is read-only, so the reviewer cannot run tests that write fixtures; and the brief asks one blanket assumption ("satisfies every acceptance criterion") with no evidence beyond the diff.

## Acceptance criteria\s*$`; F1073's body uses `## Acceptance`, so with its kill receipt passing and the plan recorded (`refuters: 1`, `tier: fast`) the driver still exits 2 before any refuter runs | read-only with `runner_env()`: `1073 ERR … has no '## Acceptance criteria' section`; `1008 AC ok`; `1015 AC ok`. `te_queue` eligibility accepted F1073 anyway |

**D2/D3 scope correction (amendment, 2026-10-21):** D2 breaks only kill checks that import the repo's own modules. F1008's five failed `broken-test`; F1073's three (`python3 kill_checks/test_f1073_kill.py …`) passed under `PYTHONSAFEPATH=1` (receipt `~/.claude/state/preclose/vitalharmony_harmonic-forge_1073.kill.json`). D3 applies to items whose kill checks fail; it is not universal.

Checked and **not** defects: runner push auth (`git push --dry-run` to the fork from a systemd context authenticates as `vitalharmony-runner`, token scopes include `read:project`); refuter call shape (`claude -p --json-schema FINDINGS_SCHEMA` returns `structured_output.findings`); codex inside the reviewer boundary.

Outside this issue: the Gemini CLI is rejected for every caller (`IneligibleTierError … UNSUPPORTED_CLIENT`). The runner never calls Gemini (`--caller claude --families 2` resolves to codex), but lane `--families 3` calls do.

## Acceptance criteria

1. Every `mise` run inside a boundary (oracle check, the agent, refuters) sets `MISE_TRUSTED_CONFIG_PATHS` to the clone or export and `MISE_STATE_DIR` to a directory under the boundary's private `/tmp`; `mise run check` on an unchanged clone of `main` gets past config loading.
2. No oracle process runs with `PYTHONSAFEPATH`; F1008's bundled AC1 check exits 0 on F1008's pushed head and non-zero on base, in the oracle boundary. The shadowing protection the safe path gave is kept by `deny_check.py diff`: a diff that adds a module or package whose top-level name is in `sys.stdlib_module_names` or is `pytest`, `unittest`, `_pytest` or `conftest`, at the repo root or in any directory `tools/run_tests.py` puts on `sys.path`, is a deny hit.
3. The agent clone's `origin` is `https://github.com/vitalharmony/harmonic-forge.git` (objects still borrowed from SOURCE through alternates).
4. The check fact is base-relative: the runner runs `mise run check` on the base in the same boundary once per base SHA (cached under `~/.local/share/te-runner/base-check/<sha>/`), and `check_rc` is non-zero only when the head fails a test ID or a mise task that passed on the base. The facts carry `check_new_failures` and `check_base_failures` (lists of test IDs and task names), and the report prints both.
5. The agent's allow list adds `mkdir`, `rm`, `cp`, `mv`, `touch`, `git checkout`, `git switch`, `git restore`, `git stash`, `git mv`, `git rm`, `git reset`, `git rev-parse`, `git branch`, `mise tasks` and `mise run`; `bash`, `sh`, `env`, `eval` and `xargs` stay denied (they would carry a denied command past the deny list).
6. The agent prompt states: the commands it may run; that a refusal is never a reason to stop (use Read/Grep/Glob/Edit or an allowed equivalent, and commit what exists); that the harness runs the kill checks, `mise run check`, the deny check and preclose, which the agent does not run and cannot see; that environment variables are set inside `python3` (`os.environ`), not as a command prefix; that `pytest` is not installed (`python3 -m unittest`, `python3 tools/run_tests.py`); and that an AC needing a deployed or live measurement is implemented and named in the final message without self-flagging for that alone.
7. Every per-item artifact is kept under `~/.local/share/te-runner/runs/<run_id>/<issue>/`: the stream log and its stderr, the transcripts, `revert.patch`, `checks.json`, the check's stdout and stderr, `kill_runner.py` and `kill_check.py` output, and the preclose driver's stdout, stderr and `--out` dir. The newest 10 runs are kept; older ones are pruned at run start.
8. `verdict.reduce` also returns `reasons`: every clause that failed, in precedence order (`reason` and `detail` unchanged). The report prints all of them.
9. `te_run.py preflight --queue FILE`, also run by `run` before the first item, aborts the run with exit 3 and lists **every** failure, never only the first, when any of these fails: (a) a permission probe (`claude -p`, the agent's settings and boundary, a cheap model, a budget of at most $0.10) running each queued item's verification commands plus a fixed probe list (the AC5 commands, `mise run check --help`), with any `permission_denials` entry a failure; (b) the base check (AC4) leaves a non-empty base-failure set where the cached base for the previous SHA was empty, which is reported, not fatal; (c) every queued item's kill-bundle checks, run on the base in the oracle boundary, each fail with a test failure, not an import or collection error (a `broken-test` baseline is fatal); (d) `codex --version` inside the reviewer boundary matches the operator's `~/.npm-global/bin/codex --version`; (e) one refuter call with `FINDINGS_SCHEMA` returns `structured_output`. Preflight writes `preflight.json` in the run dir.
10. The unit's PATH puts `%h/.npm-global/bin` ahead of `/usr/local/bin`.
11. `preclose_driver.acceptance_criteria` and `te_run.ac_titles` share one heading matcher that accepts `## Acceptance criteria` and `## Acceptance` (case-insensitive, trailing text allowed). Kill check: restore the exact match, and a test with an F1073-shaped body (`## Acceptance` plus checkbox items) fails.
12. AC9's preflight runs, for each queued item, every per-item precondition the pipeline checks before spending: `preclose_driver`'s read-only steps (out-dir, `passes_held`, the plan, `planned_lenses`, `acceptance_criteria`, `export_head`), the kill-bundle snapshot and pin check, and `te_queue`'s eligibility. It calls those same functions, not copies of them. Any refusal is a preflight failure, and all are listed together (exit 3). Kill check: add a new `DriverError` path to the driver's read-only steps, and the preflight reports it with no change to the preflight's code.
13. When a run ends, whatever its outcome, the unit renders the report and sends the one-line notification (`ExecStopPost=` running `report.py render`, then `report.py notify`); the alert unit stays for failures. Kill check: remove the `ExecStopPost=` line, and a unit test asserting a finished run produces `<run-id>.md` and one notify call fails.

14. The cross-family call can check the work:
1. `export_head` produces a git repository at the head (a `git clone --shared --no-checkout` of SOURCE plus a detached checkout of the head, with `origin` set as AC3), not an archive;
2. the brief carries one assumption per acceptance criterion, not one blanket assumption;
3. the brief's evidence includes the harness's own results for this head: the kill receipt (each AC's base and head rc), the base-relative check result (AC4) and the deny-check result, so each assumption can be confirmed from evidence without running a test.

Kill check: restore `git archive` and the single assumption, and a driver test whose fixture reviewer marks evidence it cannot reach as `uncheckable` yields the `in-family fallback` label instead of the cross-family one.

15. (extends AC9 and AC12) The preflight also:
1. rehearses one refuter call and one cross-family `verify` call on a fixed fixture with a known-true assumption, and fails unless the provenance label starts with `Red-team provenance: cross-family (`;
2. runs the per-item checks at `te_queue.py add` time as well, so an item that would fail them is refused when it is queued, not at the next run start.

## Decomposition

13 points, not split: the pilot needs all ten together (any one leaves every item set aside), and AC9 exercises AC1–AC6. Rationale recorded here per `planning.md`.




