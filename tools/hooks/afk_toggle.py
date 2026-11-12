#!/usr/bin/env python3
"""The operator's `/afk` toggle (harmonic-forge#1338): UserPromptSubmit.

The operator types `/afk on`, `/afk off` or `/afk status` (a bare `/afk` is
`status`) as the WHOLE prompt. As with `/auto-ae` (`auto_ae_toggle.py`), the
harness delivers that raw text with no envelope, so the match is exact:
surrounding whitespace stripped, nothing else on the line, bounded length.
Only operator-typed text fires `UserPromptSubmit` (R-0117); the same text
inside a longer prompt, from a tool result or from a file changes nothing.

- `off` is honored from anywhere, first: turning afk off is always safe. It
  unlinks `~/.claude/state/afk/state.json`, then, under the same lock every
  queue append takes, prints and removes only the queue lines whose
  `workspace` equals this session's (the git toplevel of the prompt's cwd),
  as one fenced block of `cd <cwd> && <command>` lines. Other workspaces'
  lines stay. It says "STILL ON" if the unlink fails.
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
    append (`afk_defer.py`) and the `off` drain take it."""

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


def append_queue(entry: dict) -> None:
    with Locked():
        with queue_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _read_queue() -> list[str]:
    path = queue_path()
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def drain(workspace: str) -> list[dict]:
    """Remove and return this workspace's queue lines; keep every other line.
    The read and the rewrite are one critical section with the appends."""
    with Locked():
        lines = _read_queue()
        mine, kept = [], []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            if isinstance(entry, dict) and entry.get("workspace") == workspace:
                mine.append(entry)
            else:
                kept.append(line)
        if mine:
            _atomic_write(queue_path(), "".join(line + "\n" for line in kept))
        return mine


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
    try:
        with Locked():
            state_path().unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        if is_on():
            return (f"afk is STILL ON: the state file could not be removed ({type(exc).__name__}). "
                    "Remove ~/.claude/state/afk/state.json by hand.")
    head = "afk is OFF."
    if is_on():
        return "afk is STILL ON: the state file could not be removed."
    entries = drain(workspace_of(payload.get("cwd")))
    if not entries:
        return head + " Nothing was queued for this workspace."
    block = "\n".join(f"cd {e.get('cwd')} && {e.get('command')}" for e in entries)
    return f"{head} {len(entries)} queued command(s) for this workspace:\n```\n{block}\n```"


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
            "`/afk off` prints the queue.")


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
