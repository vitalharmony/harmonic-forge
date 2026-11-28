# Recovering from a hook lockout

Every PreToolUse handler forge registers declares `"onFailure": "block"`
(harmonic-forge#1608). The files are `.claude/settings.json`,
`tools/lane/lane_hooks.json` and `tools/lane/lane_hooks.operator.json`, and
`tools/lane/lane_hookset.py` refuses a lane set with a PreToolUse handler that
lacks the key. With the key, a hook that cannot run refuses the tool call
instead of letting it through. A refusal hook that silently stops running is
the failure this prevents. The cost is that a hook which fails on every call
locks the session out of the tools it guards.

## Symptom

Every guarded tool call is refused with a message like:

```
PreToolUse:Bash hook error: ... failed; blocking because onFailure is "block"
```

Bash, Edit, Write, MultiEdit and NotebookEdit are all refused, and Read is
refused too (`compaction_reload_probe.py` guards `Bash|Read`). The message
names the hook that failed. Your own commands do not cause this, and retrying
them will not help.

## Causes

| Cause | What it looks like |
|---|---|
| `python3` is missing from the hook shell's `PATH` | every keyed handler fails to start, on every tool |
| A shared module on forge main is broken (`shell_parse.py` or `block_lane1_status_claims.py`, which other hooks import) | nearly every hook that imports it exits non-zero; `block_raw_board_scan.py` instead catches the ImportError and allows every call (see § Exceptions) |
| One hook crashes on a particular input | only calls of that shape are refused; `tools/hooks/test_hook_crash_contract.py` exists to catch this before merge |
| A hook times out | the call is refused after the handler's `timeout`. A network-bound hook can time out on a GitHub stall: `block_missing_preclose_inspection.py` makes 7 s `gh` reads plus a 12 s fetch under a 25 s timeout, so a stall refuses a close or merge (the safe direction; retry once GitHub answers) |

## Recovery (from a terminal outside Claude)

There is no way out from inside the session, by design. The lane launch passes
the hook set inline with `disableAllHooks: false`, so nothing the session can
edit removes it. Recover from an ordinary terminal:

1. Check the interpreter:

   ```
   command -v python3
   ```

   If it prints nothing, fix `PATH` (or the mise/pyenv shim) for the shell
   the CLI starts hooks in, then relaunch.

2. Check the forge checkout the hooks run from. This is the forge root the
   launcher resolved (harmonic-forge#1526), `~/harmonic-forge` by default:

   ```
   git -C ~/harmonic-forge status
   git -C ~/harmonic-forge log --oneline -5
   ```

   Run the hook the refusal names, by hand, to see its traceback:

   ```
   echo '{"tool_name":"Bash","tool_input":{"command":"ls"},"cwd":"/tmp"}' | python3 ~/harmonic-forge/tools/hooks/<hook>.py; echo "exit=$?"
   ```

   Revert or fix the broken module (`git -C ~/harmonic-forge revert <sha>`, or
   restore the file from the last good commit).

3. Relaunch the lane sessions. A running session keeps the hook set it was
   launched with, and its hook scripts are read from the forge root on each
   call, so a fixed module takes effect on the next tool call. A changed hook
   set takes effect only on a new launch.

4. If the user-level `~/.claude/settings.json` also carries the key, fix it
   the same way.

## Exceptions: hooks whose crash does not reach the CLI

Some keyed hooks catch their own failures and exit 0, so a crash in them
allows the call instead of refusing it, key or no key. Four groups:

- The handlers registered as `f=...; [ -f "$f" ] && python3 "$f" || true`.
  The `|| true` turns a crash into exit 0; a timeout or invalid JSON still
  refuses.
- Five unguarded hooks with an internal catch-all around the whole decision:
  `precheck_gate.py`, `guard_gh_rest_budget.py`, `model_tier_gate.py`,
  `block_unchecked_push.py` and `mypy_cwd_trap.py` (its catch-all is in
  `main`, around `decision`).
- Six guarded hooks that also catch everything internally, so removing the
  `|| true` alone would not make their crash reach the CLI:
  `block_blind_repeat.py:15`, `require_ci_plan.py:199`,
  `gate_feedback_memory_write.py:223`, `afk_defer.py:82`,
  `enforce_belt_arming.py:739` and `worktree_busy_guard.py:77`.

- One hook that swallows a broken import: `block_raw_board_scan.py:46-50`
  catches an ImportError of `shell_parse` and sets `command_segments` to
  None, and `:65` then allows every call.

harmonic-forge#1613 owns all four groups: each one either lets its failure
reach the CLI or documents why it fails open.

Separate from crashes: two hooks allow a command they cannot tokenize, by
design. `block_stale_script_execution.py:219-222` (a `command_segments`
ValueError means the command is not checked) and `block_inline_prose.py:120`
(a parse failure fails open). That is an "unparseable input allows" path, not
a swallowed crash. Any other exception in `block_stale_script_execution.py`
reaches the CLI, so a crash there refuses the call like any keyed hook.
