"""A process-level time budget for a PreToolUse refusal hook (harmonic-forge#1274).

A refusal hook that outlives its registration timeout lets the command run: the
harness reads no decision and proceeds. An internal deadline checked before
each read bounds only the reads it is written into; a retry inside a read, an
unbudgeted read on a deny path, or a `gh repo view` fallback can still carry
the hook past the timeout (harmonic-forge#1274 preclose pass 1, survivor 6).

`Watchdog` bounds every path instead. Started first thing in `main()`, it
fires once at `SECONDS` (below the 25 s registration timeout), writes the deny
JSON exactly once, flushes, and ends the process with `os._exit(0)` from the
timer thread, whatever the main thread is blocked in. The hook's own output
goes through `emit()`, under the same lock, so the two can never both print:
whichever takes the lock first is the only output.

Used by `block_route_tightened_merge.py` (its `run_gh` retry could overrun the
timeout); tested in `test_hook_watchdog.py`.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Callable

#: Below the 25 s registration timeout, above the hooks' 20 s internal deadline.
SECONDS = 22.0

#: Written if building the hook's own timeout message itself fails: a refusal
#: hook that times out must still refuse.
_FALLBACK = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Blocked: the check ran out of time (harmonic-forge#1274).",
    },
})


class Watchdog:
    """Fires `on_timeout` once after `seconds` unless `emit()` ran first, then
    exits the process. `on_timeout` prints the deny; it must not exit itself."""

    def __init__(self, seconds: float, on_timeout: Callable[[], None]) -> None:
        self._on_timeout = on_timeout
        self._lock = threading.Lock()
        self._done = False
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def start(self) -> "Watchdog":
        self._timer.start()
        return self

    def _fire(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
            try:
                self._on_timeout()
            except BaseException:  # noqa: BLE001 -- the deny must still be written
                print(_FALLBACK)
            finally:
                sys.stdout.flush()
                os._exit(0)

    def emit(self, write: Callable[[], None]) -> bool:
        """Run the hook's own `write` unless the watchdog already answered.
        Returns whether it ran."""
        with self._lock:
            if self._done:
                return False
            self._done = True
            self._timer.cancel()
            write()
            sys.stdout.flush()
            return True

    def cancel(self) -> None:
        """Stop the timer without writing (a `main()` that returned through a
        path with no output must not be killed later, e.g. under a test)."""
        self._timer.cancel()
