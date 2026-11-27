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
| A shared module on forge main is broken (`shell_parse.py` or `block_lane1_status_claims.py`, which other hooks import) | every hook that imports it exits non-zero |
| One hook crashes on a particular input | only calls of that shape are refused; `tools/hooks/test_hook_crash_contract.py` exists to catch this before merge |
| A hook times out | the call is refused after the handler's `timeout` |

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
allows the call instead of refusing it, key or no key. Two groups:

- The handlers registered as `f=...; [ -f "$f" ] && python3 "$f" || true`.
  The `|| true` turns a crash into exit 0; a timeout or invalid JSON still
  refuses.
- Six hooks with an internal catch-all: `precheck_gate.py`,
  `guard_gh_rest_budget.py`, `model_tier_gate.py`, `block_unchecked_push.py`,
  `mypy_cwd_trap.py` and `block_stale_script_execution.py`.

harmonic-forge#1613 owns both groups: each one either lets its failure reach
the CLI or documents why it fails open.
