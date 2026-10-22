#!/usr/bin/env python3
"""A lane's task list holds one entry per issue until it closes (harmonic-forge#1126).

This docstring is R-0413's authority: the rule's text in `3-lane-protocol.md`
is a pointer here, and the hooks below quote this shape in every refusal.

## The entry

One entry per held issue, keyed by the issue token at the start of the subject:

    <KEY> — <last action> · waiting on <who> <step>

- `H2463 — L2D posted (a1b2c3d) · waiting on Lane 1 review`
- `F1112 — merged 5a1b2c3, deployed · waiting on YOU: close`
- `F1112 — closed`

`<who>` is a lane, `YOU:` (the operator), CI, or `nothing` when the next step
is this lane's own.

## The rules

1. An issue's entry leaves the list only when the issue closes. While the
   issue is open, its last entry cannot be completed or deleted
   (`pretooluse_decide`). After `gh issue close`, the entry is first updated
   to `<KEY> — closed` (with the merge SHA when there is one), so the operator
   sees the close, and only then completed.
2. Every lane post, merge, PR or close on an issue owes its entry an update
   naming that action (`tasklist_owed.py` records it; `stop_decide` refuses to
   end the turn until the entry is rewritten). One marker per key, the latest
   wins. Stop reads the issue's state live: a closed issue owes a
   `<KEY> — closed` entry updated after the marker; an open one owes its open
   entry's update. The `— closed` subject is display, never proof.
3. An entry names a concrete step (read, review, push/PR, post), never only
   the issue.
4. With no active step, nothing is `in_progress`.
5. The belt watch is never listed.
6. Only operator-owned items carry a `YOU:` prefix.

Rules 3-6 are not enforced by these hooks; they live here so the rule has
one home.

## Scope and failure direction

Claude Code lane sessions only: a session with no `LANE` is not subject to
either hook. Codex lanes keep R-0376's record. An unreadable task file or task
directory allows (with a stderr note): the layout is Claude Code's, and
failing closed on a format change would wedge every lane at once. An
unreadable GitHub state denies, with the reason. A K/Y/D key (the
harmonicarchitect account) is never read under the ambient credentials: an
open K/Y/D entry is denied, naming the account (`rules/lane-shorthand.md`: an
empty result is never absence), and a `<KEY> — closed` entry may be completed
on the lane's own word, because its state cannot be read here. In Stop, an
unreadable state (K/Y/D included) allows.

The deny is JSON on stdout with exit 0, because HRSE2 wraps every forge hook
in `|| true` (AC0, probed on Claude Code 2.1.305).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tasklist_owed as owed  # noqa: E402
from turn_question import asks_a_question, last_assistant_text  # noqa: E402

SHAPE = "<KEY> — <last action> · waiting on <who> <step>"
OTHER_ACCOUNT = {"K", "Y", "D"}
# Well under the hooks' 10 s timeout (preclose pass 2, F): a read Claude Code
# kills prints nothing, which would let the update through; a timeout here
# raises and the caller denies.
GH_TIMEOUT = 6
#: Stop's total budget for live reads, under its 10 s hook timeout (epoch 2
#: pass 1): a killed hook prints nothing, which would allow. Past it, each
#: remaining key is judged locally.
STOP_READ_BUDGET = 7


def _closed_subject(key: str, subject: str) -> bool:
    return re.match(rf"^{re.escape(key)} — closed\b", subject or "") is not None


def _repo_for(prefix: str) -> str | None:
    import batch_auth  # noqa: PLC0415
    for repo, value in batch_auth.REPO_PREFIXES.items():
        if value == prefix:
            return repo
    return None


def issue_closed(key: str) -> bool:
    """True when `key`'s issue is closed. Raises RuntimeError when that
    cannot be established; the caller denies. Read live every time, never
    cached (reforge epoch 1 sticky-wicket): a stored closed state is exactly
    the stale evidence a reopen invalidates."""
    prefix, number = key[0], key[1:]
    if prefix in OTHER_ACCOUNT:
        raise RuntimeError(f"{key} is on the harmonicarchitect account; its state is "
                           "not read under the ambient credentials")
    repo = _repo_for(prefix)
    if not repo:
        raise RuntimeError(f"no repo maps to prefix {prefix}")
    try:
        done = subprocess.run(["gh", "api", f"repos/{repo}/issues/{number}", "--jq", ".state"],
                              capture_output=True, text=True, timeout=GH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"reading {repo}#{number}: {exc}") from exc
    state = done.stdout.strip()
    if done.returncode or state not in ("open", "closed"):
        raise RuntimeError(f"reading {repo}#{number}: exit {done.returncode} "
                           f"{(done.stderr or state).strip()[:200]}")
    return state == "closed"


def _note(text: str) -> None:
    print(f"tasklist_hold: {text}", file=sys.stderr)


def pretooluse_decide(payload: dict) -> tuple[str, str]:
    """AC1/AC3: `("allow", "")` or `("deny", reason)`.

    Reforged after preclose pass 2 (sticky-wicket, issuecomment-6333367330):
    a rule over the task list, not over the one call that completes. It runs
    on every TaskUpdate and compares the list before and after this update.
    When the update takes away the last open entry carrying the key its target
    had BEFORE the update (by completing, deleting, or renaming it away), it
    is allowed only when that issue is closed and the STORED entry already
    reads `<KEY> — closed`, so the close was shown first. Renaming within the
    same key, or any update that leaves another open entry for the key, is
    always allowed."""
    if not os.environ.get("LANE"):
        return "allow", ""
    tool_input = payload.get("tool_input") or {}
    session = payload.get("session_id") or ""
    task_id = str(tool_input.get("taskId") or "")
    if not session or not task_id:
        return "allow", ""
    try:
        entries = owed.read_entries(session)
    except OSError as exc:
        _note(f"task directory unreadable, allowing: {exc}")
        return "allow", ""
    target = next((e for e in entries if str(e.get("id")) == task_id), None)
    if target is None:
        _note(f"task {task_id} not readable, allowing")
        return "allow", ""
    if target.get("status") not in owed.OPEN_STATUSES:
        return "allow", ""
    key = owed.entry_key(target)
    if not key:
        return "allow", ""
    stored = str(target.get("subject") or "")
    applied = dict(target)
    if "status" in tool_input and tool_input["status"] is not None:
        applied["status"] = tool_input["status"]
    if tool_input.get("subject"):
        applied["subject"] = tool_input["subject"]
    if applied.get("status") in owed.OPEN_STATUSES and owed.entry_key(applied) == key:
        return "allow", ""
    if any(str(e.get("id")) != task_id and e.get("status") in owed.OPEN_STATUSES
           and owed.entry_key(e) == key for e in entries):
        return "allow", ""
    if key[0] in OTHER_ACCOUNT and _closed_subject(key, stored):
        return "allow", ""
    try:
        closed = issue_closed(key)
    except RuntimeError as exc:
        return "deny", (f"{key}'s state could not be read ({exc}), so its last task-list "
                        f"entry stays. Update it to \"{SHAPE}\" instead.")
    if closed and _closed_subject(key, stored):
        return "allow", ""
    if closed:
        return "deny", (f"{key} is closed. Update this entry to \"{key} — closed\" (with the "
                        "merge SHA when there is one) first, so the close is shown, then "
                        "complete it.")
    return "deny", (f"{key} is still open, and this is its only entry: an issue's entry leaves "
                    f"the list only when the issue closes, and it cannot be renamed away from "
                    f"its key. Update this entry to \"{SHAPE}\" instead "
                    "(tools/hooks/tasklist_hold.py).")


def stop_decide(payload: dict) -> tuple[str, str]:
    """AC2/AC3: `("allow", "")` or `("block", reason)`."""
    if not os.environ.get("LANE"):
        return "allow", ""
    session = payload.get("session_id") or ""
    if not session:
        return "allow", ""
    found = owed.markers(session)
    if not found:
        return "allow", ""
    transcript = payload.get("transcript_path") or ""
    if transcript and asks_a_question(last_assistant_text(Path(transcript))):
        return "allow", ""
    try:
        entries = owed.read_entries(session)
    except OSError as exc:
        _note(f"task directory unreadable, allowing stop: {exc}")
        return "allow", ""
    missing = []
    deadline = time.monotonic() + STOP_READ_BUDGET
    for marker in found:
        key, kind = str(marker["key"]), str(marker.get("kind") or "post")
        # A `— closed` entry shows this close only when it did not already read
        # closed when the marker was recorded: an entry that turned closed since
        # was written after the marker, and an old one's mtime proves nothing
        # (epoch 2 pass 1: a status-only touch bumps it).
        stale = {str(i) for i in marker.get("stale_ids") or []}
        shown = any(owed.entry_key(e) == key and _closed_subject(key, str(e.get("subject") or ""))
                    and str(e.get("id")) not in stale for e in entries)
        # Reforge epoch 2: the issue's live state decides which update is owed;
        # a `— closed` subject is display, never proof. An unreadable state
        # (K/Y/D, network, or Stop's read budget spent) is judged locally below.
        try:
            if time.monotonic() > deadline:
                raise RuntimeError("Stop's read budget is spent")
            closed: bool | None = issue_closed(key)
        except RuntimeError as exc:
            _note(f"{key}'s state unreadable, judging its entry locally: {exc}")
            closed = None
        if closed or (closed is None and shown):
            if shown or not any(owed.entry_key(e) == key for e in entries):
                # Shown, or its entry already left the list (a delete AC1
                # allows only after the close was shown).
                owed.clear(session, key)
            else:
                missing.append(f"  - {key}: it is closed; set its entry to \"{key} — closed\"")
            continue
        entry = owed.open_entry(entries, key)
        if entry is None:
            missing.append(f"  - {key}: after `{kind}`, it has no open entry; add one")
        elif float(marker.get("ts") or 0) > float(entry["_mtime"]):
            missing.append(f"  - {key}: after `{kind}`, its entry was not updated")
        elif str(entry.get("subject") or "") == str(marker.get("subject") or ""):
            missing.append(f"  - {key}: after `{kind}`, its entry's subject is unchanged")
        else:
            owed.clear(session, key)
    if not missing:
        return "allow", ""
    return "block", ("Task-list entries owe an update for this turn's posts:\n"
                     + "\n".join(missing)
                     + f"\n\nEach entry reads \"{SHAPE}\" (tools/hooks/tasklist_hold.py).")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name")
    try:
        if event == "PreToolUse":
            verdict, reason = pretooluse_decide(payload)
            if verdict == "deny":
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": reason}}))
        elif event == "Stop":
            if payload.get("stop_hook_active"):
                return 0
            verdict, reason = stop_decide(payload)
            if verdict == "block":
                print(json.dumps({"decision": "block", "reason": reason}))
    except Exception as exc:
        _note(f"error, allowing: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
