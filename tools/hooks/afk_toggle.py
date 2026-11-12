#!/usr/bin/env python3
"""The operator's `/afk` toggle (harmonic-forge#1338): UserPromptSubmit.

The operator types `/afk on`, `/afk off` or `/afk status` (a bare `/afk` is
`status`) as the WHOLE prompt. As with `/auto-ae` (`auto_ae_toggle.py`), the
harness delivers that raw text with no envelope, so the match is exact:
surrounding whitespace stripped, nothing else on the line, bounded length.
Only operator-typed text fires `UserPromptSubmit` (R-0117); the same text
inside a longer prompt, from a tool result or from a file changes nothing.

- `off` is honored from anywhere, first: turning afk off is always safe. It
  unlinks `~/.claude/state/afk/state.json` and, in the same lock every
  queue append takes, renames `queue.jsonl` to `drained-<UTC ts>.jsonl`.
  From a non-interactive entrypoint (`_interactive`), `off` only removes the
  state: the queue is neither archived nor listed and stays for the
  operator's interactive `/afk off`. Interactively it then lists every line from every workspace, exactly once, as data
  (time, original cwd, JSON-escaped command), never as runnable shell. If the archive cannot be
  read it says afk is off and names the archive path. It says "STILL ON"
  if the unlink fails.
- `on` and `status` also need an interactive entrypoint (`_interactive`, as
  `auto_ae_toggle.py`). `on` writes `{"on": true, "since": <iso>}` atomically
  under the lock, in any LANE or none. `status` writes nothing.
- Every outcome prints one `systemMessage`, also given to the session as
  `additionalContext`. Any internal error writes nothing.

While afk is on, `afk_defer.py` turns every `ask` from `batch_auth.decide()`
into a deny plus one queue line.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

STATE_RELDIR = Path(".claude") / "state" / "afk"
MAX_PROMPT = 16
RERUN_SENTENCE = ("Ask the session to re-run any of these; do not paste them into a "
                  "terminal (hooks would not run).")
_COMMAND = re.compile(r"/afk(?:[ \t]+(on|off|status))?")
_MENTION = re.compile(r"^\s*/afk\b", re.I)


def state_dir() -> Path:
    return Path.home() / STATE_RELDIR


def state_path() -> Path:
    return state_dir() / "state.json"


def queue_path() -> Path:
    return state_dir() / "queue.jsonl"


def is_on() -> bool:
    return state_path().exists()


def workspace_of(cwd: str | None) -> str:
    """The git toplevel of `cwd`, else `cwd` itself resolved."""
    cwd = cwd or os.getcwd()
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return str(Path(out.stdout.strip()).resolve())
    except Exception:  # noqa: BLE001 - fall back to the cwd itself
        pass
    return str(Path(cwd).resolve())


class Locked:
    """The one critical section for the afk state and queue: every queue
    append (`afk_defer.py`) and `off` take it."""

    def __enter__(self):
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        self._lock = (d / "afk.lock").open("a")
        fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self._lock.close()
        return False


def _atomic_write(path: Path, text: str) -> None:
    """A unique temp file renamed into place. Callers hold `Locked`."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def append_queue(entry: dict) -> bool:
    """Append one line, re-checking under the lock that afk is still on.
    False, writing nothing, when `off` removed the state first."""
    with Locked():
        if not is_on():
            return False
        with queue_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return True


def _read_queue() -> list[str]:
    path = queue_path()
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse(prompt: object) -> str | None:
    """`on`/`off`/`status` when the whole prompt is the command, else None."""
    if not isinstance(prompt, str):
        return None
    text = prompt.strip()
    if len(text) > MAX_PROMPT:
        return None
    match = _COMMAND.fullmatch(text)
    return (match.group(1) or "status") if match else None


def _interactive(entrypoint: str | None) -> bool:
    from batch_provenance import _INTERACTIVE_ENTRYPOINTS  # noqa: PLC0415
    return entrypoint in _INTERACTIVE_ENTRYPOINTS


def _off(payload: dict) -> str:
    """One critical section: remove the state, then rename the whole queue
    (every workspace) to `drained-<UTC ts>.jsonl`. The rename is atomic, so
    each line is drained exactly once and nothing is truncated in place."""
    archive: Path | None = None
    try:
        with Locked():
            state_path().unlink(missing_ok=True)
            if queue_path().exists():
                ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                archive = state_dir() / f"drained-{ts}.jsonl"
                n = 1
                while archive.exists():
                    archive = state_dir() / f"drained-{ts}-{n}.jsonl"
                    n += 1
                os.replace(queue_path(), archive)
    except Exception as exc:  # noqa: BLE001
        if is_on():
            return (f"afk is STILL ON: the state file could not be removed ({type(exc).__name__}). "
                    "Remove ~/.claude/state/afk/state.json by hand.")
        if archive is None or not archive.exists():
            return (f"afk is OFF; the queue could not be archived ({type(exc).__name__}). "
                    f"It is still at {queue_path()}.")
    # No post-lock is_on() re-check: the unlink above succeeded (or raised),
    # and a concurrent `/afk on` after the lock must not suppress the listing
    # of the queue this call already archived.
    head = "afk is OFF."
    if archive is None:
        return head + " Nothing was queued."
    try:
        entries = []
        for line in archive.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            n = len(entries) + 1
            if isinstance(entry, dict):
                entries.append(f"{n}. time: {entry.get('ts') or 'unknown'}; "
                               f"cwd: {json.dumps(entry.get('cwd'))}; "
                               f"command: {json.dumps(entry.get('command'))}")
            else:
                entries.append(f"{n}. unparseable queue line: {json.dumps(line)}")
    except Exception as exc:  # noqa: BLE001
        return (f"afk is OFF; queue archived to {archive}, could not be read "
                f"({type(exc).__name__}: {exc}).")
    if not entries:
        return f"{head} Nothing was queued (empty archive {archive})."
    listing = "\n".join(entries)
    return (f"{head} {len(entries)} queued command(s), every workspace "
            f"(archived to {archive}):\n{listing}\n{RERUN_SENTENCE}")


def _off_headless() -> str:
    """`off` from a non-interactive entrypoint: remove the state only. The
    queue is neither archived nor listed; it stays for the operator's
    interactive `/afk off`."""
    try:
        with Locked():
            state_path().unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        if is_on():
            return (f"afk is STILL ON: the state file could not be removed ({type(exc).__name__}). "
                    "Remove ~/.claude/state/afk/state.json by hand.")
    if is_on():
        return "afk is STILL ON: the state file could not be removed."
    return ("afk is OFF. The queue was left in place for an interactive `/afk off` "
            "(this entrypoint is not one an operator types into).")


def toggle(payload: dict, entrypoint: str | None, now: float | None = None) -> str | None:
    """The message to show, or None when the prompt is not an `/afk` line."""
    prompt = payload.get("prompt")
    action = parse(prompt)
    if action is None:
        if isinstance(prompt, str) and _MENTION.match(prompt):
            return ("afk: nothing changed. The whole prompt must be exactly `/afk on`, "
                    "`/afk off` or `/afk status`.")
        return None
    if action == "off":
        if not _interactive(entrypoint):
            return _off_headless()
        return _off(payload)
    if not _interactive(entrypoint):
        return (f"afk: nothing changed. The session's entrypoint ({entrypoint or 'unset'}) "
                "is not one an operator types into.")
    if action == "status":
        if not is_on():
            return "afk is OFF."
        try:
            since = json.loads(state_path().read_text(encoding="utf-8")).get("since")
        except Exception:  # noqa: BLE001
            since = None
        return f"afk is ON since {since or 'unknown'}."
    now = time.time() if now is None else now
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    with Locked():
        _atomic_write(state_path(), json.dumps({"on": True, "since": since}, sort_keys=True) + "\n")
    return (f"afk is ON since {since}: commands batch_auth would ask about are denied and queued; "
            "`/afk off` lists the queue.")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable payload decides nothing
        return 0
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in (None, "UserPromptSubmit"):
        return 0
    if "tool_name" in payload:
        return 0
    try:
        message = toggle(payload, os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
    except Exception as exc:  # noqa: BLE001 - nothing written
        message = f"afk: nothing changed ({type(exc).__name__})."
    if message:
        print(json.dumps({"systemMessage": message,
                          "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": message}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
