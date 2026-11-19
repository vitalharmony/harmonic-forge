"""harmonic-forge#1076: a lane tool's first real run in a session needs its
preflight form first.

Lane 1 kept learning each tool's requirements from a refusal, and a written
note ("run it yourself minus the operator-only flag") did not bind. So each
guarded tool records a receipt when its preflight form runs (`--help`, or for
`preclose_check.py --force` the same command without `--force`) and refuses its
real run in a session that has no receipt for it. `l1_post.py`'s preflight form
is a clean `--dry-run` of the same post, keyed by `digest_key` to that post's
arguments (harmonic-forge#1115); `--help` no longer counts for it.

harmonic-forge#1215: every preflight is a read-only probe of the run's own
requirements, bound to that run's argument digest.

harmonic-forge#1291: the receipt is now `tools/precheck/gate_receipt.py`'s
state-bound receipt (tool, precheck mode, canonical argv and the registry's
`binds`, resolved; 1800 s ceiling), the same one the PreToolUse precheck gate
reads. `record` and `require` delegate to it; `require` stays in each tool as
the backstop the operator kept on 2026-11-18, since only the tool sees
argparse's real parse.

The inventory below is generated from tools/registry/effects.toml by
`python3 tools/registry/effects.py inventory` (harmonic-forge#1290): every
shared-state-or-above mode of a tools/gh or tools/lane script, and every
outside-command family, with its precheck, guard or gap. Edit the registry and
regenerate; tools/registry/test_effects.py fails when the two differ.

| Tool | Mode | Class | Precheck, guard or gap |
|---|---|---|---|
| `board_fields.py` | set | shared-state | gap: writes board fields with no dry run |
| `check_lane3_ready.py` | --mark-active | shared-state | precheck: `default` |
| `force_grant.py` | record | external-publish | guard (operator grant): FORCE line: refuses with no operator-typed grant (tools/gh/force_grant.py:170-172) |
| `gh-as` | --init <account> | shared-state | gap: interactive operator login into the account's config dir |
| `gh_issue.py` | create | external-publish | gap: no dry run |
| `gh_shim` | wrapper | shared-state | gap: spends the operator's one-shot scan override on the next call |
| `imc_waiver.py` | record | external-publish | guard (operator grant): IMC on a BATCH line: record refuses with no hook-written waiver record (tools/gh/imc_waiver.py:303-306) |
| `imc_waiver.py` | end | external-publish | gap: posts the end comment and archives the receipt with no dry run |
| `imc_waiver.py` | retire --all-closed | shared-state | gap: archives every closed issue's waiver receipt |
| `imc_waiver.py` | retire --key | shared-state | gap: archives one waiver receipt |
| `install_gh_shim.sh` | install | shared-state | gap: the operator's own action: links ~/.local/bin/gh to the shim |
| `kill_check.py` | run | shared-state | precheck: `run --validate` |
| `kill_check.py` | waive | shared-state | gap: writes a waived kill-check receipt |
| `l1_post.py` | post (every --kind) | external-publish | precheck: `--dry-run` |
| `l2_post.py` | snapshot | shared-state | gap: writes a snapshot receipt a later post reads |
| `l2_post.py` | resolve-lock | shared-state | gap: clears the issue's failure lock |
| `l2_post.py` | post | external-publish | gap: no dry run |
| `pitch_receipt.py` | record | shared-state | gap: writes the pitch receipt l1_post reads |
| `post_comment.py` | post | external-publish | gap: role-scoped hooks only |
| `post_lane1_issue.py` | fill or amend | external-publish | gap: validates before the PATCH, no dry run |
| `post_lane_discussion.py` | post (every --kind) | external-publish | gap: no dry run |
| `preclose_check.py` | --post-verdict | shared-state | gap: writes the pass receipt |
| `preclose_check.py` | --cluster-verdict --force | shared-state | guard (operator grant): FORCE <key> cluster line (force_grant.require) |
| `preclose_check.py` | --cluster-verdict | shared-state | gap: writes the cluster verdict into the receipt |
| `preclose_check.py` | --complete --force | shared-state | guard (operator grant): FORCE line (force_grant.require) |
| `preclose_check.py` | --complete | shared-state | gap: records the pass |
| `preclose_check.py` | plan --force [--reforge] | shared-state | guard (operator grant): FORCE line, plain or reforge (force_grant.require) |
| `preclose_check.py` | plan | shared-state | gap: writes the planned receipt |
| `preclose_enrollment.py` | --enroll | shared-state | gap: writes the experiment file every preclose plan reads |
| `preclose_enrollment.py` | --stop | shared-state | gap: writes the experiment file every preclose plan reads |
| `receipt_runner.py` | --issue N -- <command> | shared-state | gap: writes the command receipt and, on failure, the issue lock l2_post reads |
| `repo_hygiene.py` | --prune-worktrees | irreversible | precheck: `--prune-worktrees --dry-run` |
| `sticky_receipt.py` | record | shared-state | gap: writes the sticky-wicket receipt |
| `bounded_scan.py` | scan | shared-state | guard (operator grant): SCAN line: refuses with no live grant for this session, repo and scope (tools/gh/bounded_scan.py:152) |
| `ui_grant.py` | record | external-publish | guard (operator grant): UI line: record refuses with no hook-written pending grant (tools/gh/ui_grant.py:356) |
| `watch_lane_posts.py` | belt run | shared-state | gap: writes the belt's seen, offer and tick state under ~/.claude/state/belt |
| `baseline_capture.py` | --out | shared-state | gap: writes the committed comparison fixture |
| `ci_plan.py` | plan | shared-state | gap: writes the receipt require_ci_plan.py acts on |
| `cli_update.py` | run | shared-state | gap: no dry run; runs each CLI's update command and appends the shared log; forge-cli-update.timer runs it |
| `heavy_slot.py` | status | shared-state | gap: removes holder, lock and ticket files under the machine-wide lock |
| `heavy_slot.py` | run | shared-state | gap: takes a machine-wide admission slot, then runs the command |
| `install_lane_hooks.py` | --repo | shared-state | gap: writes hook wrappers and sets core.hooksPath |
| `lane-queue-run` | run | shared-state | gap: saves the queue state file and drives a Codex session |
| `lane1` | launch | shared-state | gap: refreshes the lane checkout's branch |
| `lane1_rebase.py` | run | external-publish | precheck: `--check` |
| `lane2` | launch | shared-state | gap: refreshes the lane worktree |
| `lane3` | launch | shared-state | gap: refreshes the lane worktree |
| `lane3-provision` | repair | shared-state | gap: moves the Lane 3 worktree's HEAD |
| `lane3_audit.py` | record | shared-state | gap: appends the shared git-common-dir audit log |
| `lane3_marker.py` | <marker> | shared-state | gap: appends the same shared audit log |
| `lanes-claude` | --stop-lanes | shared-state | gap: stops every lane session process |
| `lanes-claude` | --stop-advisor | shared-state | gap: stops the advisor session and kills its tmux session |
| `lanes-claude` | --record-lanes | shared-state | gap: writes the session-id state file teardown reads |
| `lanes-claude` | launch | shared-state | precheck: `--dry-run` |
| `lanes_reap.py` | reap-belt-locks DIR | shared-state | gap: removes stale belt locks |
| `lanes_reap.py` | reap-worktree-servers | shared-state | gap: stops orphaned worktree dev servers |
| `ntfy.py` | push --key --text | external-publish | gap: no hook or grant: any session can push to the operator's phone, bounded only by the per-lane-plus-key hourly limit |
| `git push` | outside command | external-publish | guard (hook): block_unchecked_push.py:118 (check_receipt.push_refusal), and check_receipt.py verify at pre-push (:203) |
| `gh pr merge` | outside command | irreversible | guard (hook): batch_gate.py:50 (batch_auth.decide: allow on a live BATCH grant, else ask) |
| `gh issue close` | outside command | external-publish | guard (hook): batch_gate.py:50 (batch_auth.decide asks on every close, batch_auth.py:1344-1353) |
| `gh` | outside command | external-publish | gap: no guard on a gh write outside the named families |
| `curl, wget` | outside command | external-publish | gap: no guard on an HTTP write |
| `podman, docker` | outside command | shared-state | gap: no guard on a container write |

The session is `CLAUDE_CODE_SESSION_ID`. An empty value (a non-Claude caller,
CI) is not enforced: an unidentified caller is never blocked. The id is hashed
into the path, so no value of it can name a directory outside the receipt store.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "precheck"))
import gate_receipt  # noqa: E402


def session() -> str:
    return gate_receipt.session()


def receipt_dir() -> Path:
    return gate_receipt.store_dir()


def record(tool_path: str, argv: list[str], cwd: str | Path | None = None,
           extra: str | None = None) -> None:
    """Write this session's state-bound receipt for the precheck call `argv`.
    Called only at the tool's clean point. Never raises: the precheck form is
    never gated, so a store or registry problem is a warning, and the real
    run then refuses."""
    try:
        gate_receipt.record(tool_path, list(argv), cwd, extra=extra)
    except (gate_receipt.ReceiptRefused, gate_receipt.Deadline, OSError) as exc:
        print(f"[preflight] receipt not written ({exc}); the real run will refuse", file=sys.stderr)


def require(tool_path: str, argv: list[str], preflight_cmd: str | None = None,
            cwd: str | Path | None = None, extra: str | None = None) -> str | None:
    """None when this real run may proceed, else the refusal naming the
    precheck command (`preflight_cmd` when given)."""
    try:
        return gate_receipt.consume(tool_path, list(argv), preflight_cmd, cwd, extra=extra)
    except gate_receipt.Deadline as exc:
        return f"the precheck receipt could not be checked ({exc}); retry"
