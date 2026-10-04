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
`UserPromptSubmit`, and a model-invoked Skill call does not.

- `on` needs at least one live `BATCH` lease (unexpired, with an unspent
  `gh pr merge` target), or it refuses loudly and writes nothing (ruling 3).
- `on`/`off` write `~/.claude/state/auto-ae.json`; `status` writes nothing.
- Every outcome prints one `systemMessage` line, also given to the session as
  `additionalContext` so Lane 1 knows the state it is working under.
- Outside LANE=1, or from a non-interactive entrypoint (or none), the toggle
  refuses out loud. Any internal error writes nothing (fail closed).

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
  harmonic-forge#650). The match is on the path, never on the word "auto-ae",
  so editing this file, its tests or the skill stays possible.

THIS IS A MISTAKE-DETECTOR, NOT A BOUNDARY (R-0378)
---------------------------------------------------
It stops an agent turning auto-AE on through its own tools by an ordinary
path. It does not stop: a path composed indirectly (`'auto-' + 'ae.json'` in
`python -c`, a shell variable), or model-written `/auto-ae on` text that the
operator pastes, or that a `/loop` scheduled before this guard existed echoes
into an interactive Lane 1 session. Every lane runs as the same user.
"""
from __future__ import annotations

import json
import os
import re
import sys
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


def _write_state(data: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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
    if lane != "1":
        return f"auto-AE: nothing changed. It is toggled only in a Lane 1 session (LANE={lane or 'unset'})."
    if not _interactive(entrypoint):
        return (f"auto-AE: nothing changed. The session's entrypoint ({entrypoint or 'unset'}) "
                "is not one an operator types into.")
    import _auto_ae  # noqa: PLC0415
    leases = sorted(_auto_ae.live_lease_keys())
    current = _auto_ae.state(state_path()).get("on") is True
    if action == "status":
        return (f"auto-AE is {'ON' if current else 'OFF'}. Live BATCH leases: "
                f"{', '.join(leases) or 'none'}.")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    record = {"set_at": stamp, "lane": "1", "session_id": payload.get("session_id")}
    if action == "off":
        _write_state({"on": False, **record})
        return "auto-AE is OFF. Every Lane 3 spec needs the operator's approval and AE again."
    if not leases:
        return ("auto-AE REFUSED: there is no live BATCH lease. Type a BATCH line naming the "
                "issues first; auto-AE covers only issues under a live lease. Nothing was written.")
    _write_state({"on": True, "leases_at_set": leases, **record})
    return (f"auto-AE is ON for issues under a live BATCH lease ({', '.join(leases)}): Lane 1 "
            "approves their Tier R/W specs and posts AE+sweep itself. Never Tier P. "
            "`/auto-ae off` ends it.")


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


def guard(payload: dict) -> str | None:
    """The deny reason, or None to let the call through."""
    tool = str(payload.get("tool_name") or "")
    text = "\n".join(_strings(payload.get("tool_input")))
    if tool in _SCHEDULE_TOOLS and _SCHEDULE_MENTION.search(text):
        return (f"auto-AE guard (R-0378): {tool} may not carry auto-ae. A scheduled prompt is "
                "delivered into an interactive session and would toggle auto-AE as if the "
                "operator typed it. Only the operator types `/auto-ae`.")
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
