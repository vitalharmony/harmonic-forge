#!/usr/bin/env python3
"""The operator's `/auto-ae` toggle (harmonic-forge#874, R-0378): one hook
file, two events.

UserPromptSubmit -- THE TOGGLE
------------------------------
The operator types `/auto-ae on`, `/auto-ae off` or `/auto-ae status` (a bare
`/auto-ae` is `status`) as the WHOLE prompt, in an interactive Lane 1 session.
The step 1 probe showed the harness delivers that raw text, with no
`<command-name>` envelope (probe ruling, 2026-10-04), so the match is exact:
surrounding whitespace stripped, nothing else on the line, bounded length.
Provenance rests on the same basis as `BATCH`: only operator-typed text fires
`UserPromptSubmit`, and a model-invoked Skill call does not. And because an
agent could pipe a fake payload into this file, `on` also requires the session
transcript's newest typed user turn to be this command (the harness records a
typed `/auto-ae on` as its `<command-name>` envelope); a faked payload appends
no such turn (sticky-wicket PATCH, fix 1).

- `on` needs at least one live `BATCH` lease (unexpired, with an unspent
  `gh pr merge` target), or it refuses loudly and writes nothing (ruling 3).
  It records each lease live at that moment by key AND expiry, and lapses
  itself after 12 hours, so a later or renewed `BATCH`, even for the same key,
  needs `/auto-ae on` typed again. The lease read and the write are one
  critical section, so an `on` in flight cannot land after an `off`.
- `on` writes `~/.claude/state/auto-ae.json` (atomic, under a lock); `off`
  unlinks it, and says "STILL ON" if it cannot; `status` writes nothing.
- `on` and `status` refuse out loud outside LANE=1 or from a non-interactive
  entrypoint (or none). `off` is honored from anywhere and before any other
  check: revocation is always safe, and a failure that left auto-AE on would
  fail open.
- Every outcome prints one `systemMessage` line, also given to the session as
  `additionalContext` so Lane 1 knows the state it is working under. Any
  internal error writes nothing.

PreToolUse -- THE GUARD
-----------------------
No agent may toggle auto-AE (ruling 2). This half denies, in every lane:

- any `CronCreate`, `ScheduleWakeup` or `RemoteTrigger` whose input mentions
  auto-ae: a `/loop` re-fire or a scheduled wakeup is delivered into the same
  interactive session and is indistinguishable from a typed prompt at the
  hook, so no session may schedule its own toggle;
- any other tool call whose input names the toggle's state file or the probe
  log: the state is written only by this hook. Default-deny on the path, not
  an allowlist of write shapes (the `guard_gh_rest_budget.py` lesson,
  harmonic-forge#650). A command (`Bash`, `Monitor`) is judged on its whole
  text, except one that is purely a read (`cat`, `grep`, `git grep`, `rg`,
  `ls`, `jq`, no redirection, `tee` or chaining); a file tool (`Write`, `Edit`,
  `MultiEdit`, `NotebookEdit`) only on the path it writes, never on its
  content; Codex `apply_patch` on its `*** ... File:` lines, or its whole text
  when it has none (fail closed).

THIS IS A MISTAKE-DETECTOR, NOT A BOUNDARY (R-0378)
---------------------------------------------------
It stops an agent turning auto-AE on through its own tools by an ordinary
path. It does not stop: a path composed indirectly (`'auto-' + 'ae.json'` in
`python -c`, a shell variable); a crafted transcript file under
`~/.claude/projects` handed to a faked payload; or model-written `/auto-ae on`
text that the operator pastes, or that a `/loop` scheduled before this guard
existed echoes into an interactive Lane 1 session. Every lane runs as the same
user.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "gh"))

STATE_RELPATH = Path(".claude") / "state" / "auto-ae.json"
PROBE_RELPATH = Path(".local") / "state" / "auto-ae" / "probe.jsonl"
MAX_PROMPT = 32
_COMMAND = re.compile(r"/auto-ae(?:[ \t]+(on|off|status))?")
_MENTION = re.compile(r"^\s*/auto-ae\b", re.I)
#: The state file and the probe log, however spelled: `auto-ae.json`,
#: `auto_ae.json`, `state/auto-ae/`, `auto-ae/probe.jsonl`.
_PROTECTED_PATH = re.compile(r"auto[-_]ae\.json|state/auto[-_]ae\b|auto[-_]ae/probe", re.I)
_SCHEDULE_TOOLS = frozenset({"CronCreate", "ScheduleWakeup", "RemoteTrigger"})
_SCHEDULE_MENTION = re.compile(r"auto[-_ ]?ae", re.I)


def state_path() -> Path:
    return Path.home() / STATE_RELPATH


# --- UserPromptSubmit: the toggle -------------------------------------------

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


#: ON lapses on its own, like a BATCH grant (sticky-wicket PATCH, fix 5).
TTL_SECONDS = 12 * 60 * 60
#: How much of the transcript's tail is read for the operator's newest turn.
_TRANSCRIPT_TAIL = 512 * 1024
_ENVELOPE_NAME = re.compile(r"<command-name>\s*/auto-ae\s*</command-name>")
_ENVELOPE_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


class _Locked:
    """The one critical section for the state file: every read-decide-write of
    `on` and every revocation happens inside it, so an `on` already in flight
    cannot land after an `off` (sticky-wicket PATCH, fix 6)."""

    def __enter__(self):
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = path.with_name(path.name + ".lock").open("a")
        fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self._lock.close()
        return False


def _write_state(data: dict) -> None:
    """Atomic: a unique temp file renamed into place. Callers hold `_Locked`."""
    path = state_path()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _revoke(record: dict) -> str:
    """`off`: unlink first (a missing file reads as off), then leave a record.
    If the file cannot be removed or overwritten, say auto-AE is STILL ON;
    never "nothing changed" (sticky-wicket PATCH, fix 7)."""
    path = state_path()
    try:
        with _Locked():
            path.unlink(missing_ok=True)
            try:
                _write_state({"on": False, **record})
            except OSError:
                pass  # the unlink already turned it off
        return "auto-AE is OFF. Every Lane 3 spec needs the operator's approval and AE again."
    except Exception as exc:  # noqa: BLE001
        try:
            _write_state({"on": False, **record})
            return "auto-AE is OFF. Every Lane 3 spec needs the operator's approval and AE again."
        except Exception:  # noqa: BLE001
            return (f"auto-AE is STILL ON: the state file could not be removed or rewritten "
                    f"({type(exc).__name__}). Remove ~/.claude/state/auto-ae.json by hand.")


def _turn_text(row: dict) -> str | None:
    """A typed user turn's text, or None for a tool result or a meta row."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("toolUseResult") is not None:
        return None
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return None


def typed_turn_confirms(payload: dict, action: str) -> bool:
    """True when the newest typed user turn in the session's own transcript is
    this command (sticky-wicket PATCH, fix 1). The harness records a typed
    `/auto-ae on` as its `<command-name>` envelope with `<command-args>`; the
    raw text is accepted too. An agent piping a fake payload into this hook
    appends no user turn, so the newest typed turn is whatever the operator
    last typed, and the check refuses without naming any interpreter."""
    raw = payload.get("transcript_path")
    if not isinstance(raw, str) or not raw:
        return False
    path = Path(raw).expanduser()
    try:
        path.resolve().relative_to((Path.home() / ".claude" / "projects").resolve())
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _TRANSCRIPT_TAIL))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return False
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        text = _turn_text(row) if isinstance(row, dict) else None
        if text is None:
            continue
        if text.strip() == str(payload.get("prompt") or "").strip():
            return True
        if _ENVELOPE_NAME.search(text):
            args = _ENVELOPE_ARGS.search(text)
            return ((args.group(1).strip() if args else "") or "status") == action
        return False
    return False


def toggle(payload: dict, lane: str | None, entrypoint: str | None,
           now: float | None = None) -> str | None:
    """The message to show, or None when the prompt is not an `/auto-ae` line."""
    prompt = payload.get("prompt")
    action = parse(prompt)
    if action is None:
        if isinstance(prompt, str) and _MENTION.match(prompt):
            return ("auto-AE: nothing changed. The whole prompt must be exactly `/auto-ae on`, "
                    "`/auto-ae off` or `/auto-ae status`.")
        return None
    now = time.time() if now is None else now
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    record = {"set_at": stamp, "lane": lane, "session_id": payload.get("session_id")}
    if action == "off":
        # Revocation never waits on the arming path's gates or imports: turning
        # auto-AE off is safe from anywhere, and a failure that left it on would
        # fail open (preclose fail-direction finding).
        return _revoke(record)
    if lane != "1":
        return f"auto-AE: nothing changed. It is turned on only in a Lane 1 session (LANE={lane or 'unset'})."
    if not _interactive(entrypoint):
        return (f"auto-AE: nothing changed. The session's entrypoint ({entrypoint or 'unset'}) "
                "is not one an operator types into.")
    import _auto_ae  # noqa: PLC0415
    if action == "status":
        current = _auto_ae.state(state_path())
        covered = ", ".join(sorted(current.get("leases_at_set") or {})) or "none"
        return (f"auto-AE is {'ON, covering ' + covered if current.get('on') else 'OFF'}. "
                f"Live BATCH leases: {', '.join(sorted(_auto_ae.live_leases())) or 'none'}.")
    if not typed_turn_confirms(payload, action):
        return ("auto-AE REFUSED: the session transcript's newest typed turn is not `/auto-ae on`, "
                "so this did not come from the operator's keyboard. Nothing was written.")
    with _Locked():
        leases = _auto_ae.live_leases()
        if not leases:
            return ("auto-AE REFUSED: there is no live BATCH lease. Type a BATCH line naming the "
                    "issues first; auto-AE covers only issues under a live lease. Nothing was written.")
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + TTL_SECONDS))
        _write_state({"on": True, "leases_at_set": leases, "expires_at": expires, **record})
    return (f"auto-AE is ON until {expires} for the BATCH leases live now "
            f"({', '.join(sorted(leases))}): Lane 1 approves their Tier R/W specs and posts "
            "AE+sweep itself. Never Tier P. A renewed or later BATCH is not covered until "
            "`/auto-ae on` is typed again; `/auto-ae off` ends it.")


# --- PreToolUse: the guard --------------------------------------------------

def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


#: File tools are judged by the path they write, never by the content they
#: carry: this hook's own source and tests contain the protected path, and a
#: content match would deny every edit to them (preclose finding).
_FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_PATH_KEYS = ("file_path", "notebook_path", "path")
_PATCH_PATH = re.compile(r"(?m)^\*\*\* (?:(?:Add|Update|Delete) File|Move to):[ \t]*(.+)$")
#: Commands whose whole text is a read may name the state file: an allowlist of
#: READS, whose misses are false denials, never false permits (the opposite
#: direction from #650's allowlist of writes). Any redirection, `tee`, command
#: chaining or substitution disqualifies the whole command (sticky-wicket
#: PATCH, fix 8).
_READ_COMMANDS = ("cat", "grep", "egrep", "fgrep", "rg", "ls", "jq", "git grep")
_NOT_A_READ = re.compile(r"[;&`>]|\$\(|<\(|\btee\b|\n")


def _read_only(command: str) -> bool:
    if _NOT_A_READ.search(command):
        return False
    segments = [s.strip() for s in command.split("|")]
    return all(s and any(s == c or s.startswith(c + " ") for c in _READ_COMMANDS) for s in segments)


def guard(payload: dict) -> str | None:
    """The deny reason, or None to let the call through.

    There is no check for an agent running this hook itself: a fake payload is
    refused at the toggle, which requires the session transcript's newest typed
    turn to be the command (`typed_turn_confirms`), not by enumerating
    interpreter spellings here (sticky-wicket PATCH, fix 1)."""
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if tool in _FILE_TOOLS and isinstance(tool_input, dict):
        text = "\n".join(str(tool_input.get(k) or "") for k in _PATH_KEYS)
    elif tool == "apply_patch":
        # Codex: the patch carries its paths on `*** Add/Update/Delete File:`
        # and `*** Move to:` lines; its content is judged as Write's is. A shape
        # with no such line is judged on its whole text (fails closed, fix 3).
        whole = "\n".join(_strings(tool_input))
        paths = [m.group(1) for m in _PATCH_PATH.finditer(whole)]
        text = "\n".join(paths) if paths else whole
    else:
        text = "\n".join(_strings(tool_input))
    if tool in _SCHEDULE_TOOLS and _SCHEDULE_MENTION.search(text):
        return (f"auto-AE guard (R-0378): {tool} may not carry auto-ae. A scheduled prompt is "
                "delivered into an interactive session and would toggle auto-AE as if the "
                "operator typed it. Only the operator types `/auto-ae`.")
    if tool in ("Bash", "Monitor", "shell") and isinstance(tool_input, dict) \
            and _read_only(str(tool_input.get("command") or "")):
        return None
    if _PROTECTED_PATH.search(text):
        return ("auto-AE guard (R-0378): this call names the auto-AE state file or probe log. "
                "Only the /auto-ae hook writes them; read the state with `/auto-ae status`.")
    return None


def _emit(obj: dict) -> None:
    print(json.dumps(obj))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable payload decides nothing
        return 0
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name")
    # A tool payload is guarded whatever its event name says: a Codex payload
    # may not carry one, and a guard that silently skipped it guards nothing.
    if event == "PreToolUse" or (event is None and "tool_name" in payload):
        try:
            reason = guard(payload)
        except Exception as exc:  # noqa: BLE001 - fail closed on the guard
            reason = f"auto-AE guard could not inspect this call ({type(exc).__name__}); denied"
        if reason:
            _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                          "permissionDecision": "deny",
                                          "permissionDecisionReason": reason}})
        return 0
    try:
        message = toggle(payload, os.environ.get("LANE"), os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
    except Exception as exc:  # noqa: BLE001 - fail closed: nothing written
        message = f"auto-AE: nothing changed ({type(exc).__name__})."
    if message:
        _emit({"systemMessage": message,
               "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                      "additionalContext": message}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
