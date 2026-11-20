#!/usr/bin/env python3
"""One gate (harmonic-forge#1291): the PreToolUse precheck gate on the effect
registry.

`decide(payload)` reads a Bash call, finds every simple command that runs a
registry tool with a precheck mode (directly, through `python3`, a shell `-c`
string, or a `mise run <task>` the registry maps in `mise_tasks`), and decides
each real run of a precheck mode:

- allow on a live, state-bound receipt for this exact call (`allowed-receipt`);
- allow on a usable `FORCE <KEY> gate <refusal-id>` grant bound to this tool,
  mode and argument fingerprint (`allowed-override`). The gate never spends
  it: the tool's own `consume()` does, as the real run starts;
- else deny, printing the exact precheck command, the fingerprint, a refusal
  id and the FORCE line that would release this one call (`refused`).

The precheck form itself is logged `precheck` and allowed. A session-less call
is decided in the fixed `no-session` bucket, never skipped. An outside family (`git push`, `gh pr merge`, ...) is logged
`guarded-elsewhere` and never refused here: its own hook decides it.

On the gate's own error, its internal 5 s deadline, or a command that does
not parse, the gate fails open and logs `error`: every precheck mode's tool
runs `consume()` itself, which still refuses (harmonic-forge#1291
sticky-wicket PATCH; the fail-closed paths guarded modes that no longer exist).

Every decision is one row of `decisions.jsonl` in the store: ts, outcome,
tool, mode, class, fp, session, tuid (the hashed `tool_use_id`, the join key
for the post-run outcome row, harmonic-forge#1448), exit, reason (`parse` or
`fault` on an `error` row). Never an argv token. A refusal also writes a
refusal record keyed by its id (never overwritten), which the FORCE gate line
binds to; that record holds the canonical argv so the FORCE receipt can echo
what the grant releases.

`refuses(command, cwd, env_names)` is the replay harness's layer function: the
store guard first, then the same classification with no session and no
store, so any real run of a precheck mode refuses.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORGE_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(FORGE_ROOT / "tools" / "hooks"))
sys.path.insert(0, str(FORGE_ROOT / "tools" / "gh"))
sys.path.insert(0, str(FORGE_ROOT / "tools" / "registry"))

import decision_row  # noqa: E402
import gate_receipt as receipts  # noqa: E402
import shell_parse  # noqa: E402

PRECEDENT = ("Precedent: F1279 fold 2: H2529's refusal was right; its cost was a refusal "
             "that did not print this command.")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
PYTHONS = re.compile(r"^python(?:3(?:\.\d+)?)?$")
REFUSAL_ID = re.compile(r"^[0-9a-f]{12}$")


class _Call:
    """One gated simple command: the registry tool, its argv, how it was spelled."""

    def __init__(self, tool: dict, argv: list[str], spelling: list[str], via_mise: dict | None):
        self.tool, self.argv, self.spelling, self.via_mise = tool, argv, spelling, via_mise


# --------------------------------------------------------------------------
# Finding the calls.

def _tool_for_program(token: str) -> dict | None:
    reg = receipts.registry()
    text = token.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
    text = os.path.expanduser(text)
    name = Path(text).name
    for tool in reg["tools"]:
        path = tool["path"]
        if text == path or text.endswith("/" + path) or (
                not path.endswith(".py") and "/" not in text and name == Path(path).name):
            return receipts.precheck_tool(path)
    return None


def _mise_call(argv: list[str]) -> _Call | None:
    if len(argv) < 3 or Path(argv[0]).name != "mise" or argv[1] not in ("run", "r"):
        return None
    task, rest = argv[2], argv[3:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    for tool in receipts.registry()["tools"]:
        for entry in tool.get("mise_tasks", []):
            if entry["task"] == task:
                found = receipts.precheck_tool(tool["path"])
                if found is None:
                    return None
                return _Call(found, [*entry["prefix"], *rest], ["mise", "run", task, "--"], entry)
    return None


_WORD_START = frozenset(" \t\n;|&()")


def _strip_fd_digits(command: str) -> str:
    """Drop a redirect's file-descriptor digit before the gate tokenizes
    (harmonic-forge#1461). shlex keeps no whitespace, so `2>&1` and `2 >&1`
    tokenize alike and shell_parse keeps the digit as an argument
    (harmonic-forge#1289); only the raw text tells them apart. A run of digits
    is removed when it is unquoted and unescaped, starts a word, and is
    followed immediately by `>` or `<`. Everything else is copied unchanged."""
    out: list[str] = []
    i, n = 0, len(command)
    quote: str | None = None
    boundary = True  # the previous character was an unescaped, unquoted word boundary
    while i < n:
        ch = command[i]
        if quote == "'":
            out.append(ch)
            quote = None if ch == "'" else quote
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            # An escaped character (an escaped space included) is word text,
            # never a boundary (preclose pass 1).
            out.append(command[i:i + 2])
            i += 2
            boundary = False
            continue
        if quote == '"':
            out.append(ch)
            quote = None if ch == '"' else quote
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            boundary = False
            continue
        # ASCII digits only: bash descriptors are never other Unicode digits
        # (preclose pass 1).
        if ch in _ASCII_DIGITS and boundary:
            j = i
            while j < n and command[j] in _ASCII_DIGITS:
                j += 1
            # `N<(` / `N>(` is a process-substitution argument, not a
            # descriptor (preclose pass 1).
            if j < n and command[j] in "<>" and command[j + 1:j + 2] != "(":
                i = j  # drop the descriptor digits; the operator follows
                continue
        out.append(ch)
        boundary = ch in _WORD_START
        i += 1
    return "".join(out)


_ASCII_DIGITS = frozenset("0123456789")


def _calls_in(command: str, depth: int = 0) -> tuple[list[_Call], list[list[str]], bool]:
    """(gated calls, other argvs, parsed). `parsed` is False when the text does not parse."""
    try:
        items = shell_parse.simple_commands(_strip_fd_digits(command))
    except (ValueError, shell_parse.NotOrdinary):
        return [], [], False
    calls: list[_Call] = []
    others: list[list[str]] = []
    for item in items:
        if not isinstance(item, shell_parse.SimpleCommand) or not item.argv:
            continue
        argv = shell_parse.strip_invocation_prefix(list(item.argv), unwrap_shells=False)
        # The shell expands a leading `~` before the tool sees it; so does the gate,
        # or the two would fingerprint different argv.
        argv = [os.path.expanduser(t) if t == "~" or t.startswith("~/") else t for t in argv]
        if not argv:
            continue
        program = Path(argv[0]).name
        if program in SHELLS and len(argv) > 2 and argv[1] in ("-c", "-lc", "--command") and depth < 3:
            inner_calls, inner_others, ok = _calls_in(argv[2], depth + 1)
            calls += inner_calls
            others += inner_others
            if not ok:
                return calls, others, False
            continue
        mise = _mise_call(argv)
        if mise is not None:
            calls.append(mise)
            continue
        if PYTHONS.match(program):
            index = 1
            while index < len(argv) and argv[index].startswith("-"):
                index += 1
            if index < len(argv):
                tool = _tool_for_program(argv[index])
                if tool is not None:
                    calls.append(_Call(tool, argv[index + 1:], argv[:index + 1], None))
                    continue
        tool = _tool_for_program(argv[0])
        if tool is not None:
            calls.append(_Call(tool, argv[1:], argv[:1], None))
            continue
        others.append(argv)
    return calls, others, True


# --------------------------------------------------------------------------
# The refusal text.

def _issue_key(argv: list[str]) -> str | None:
    repo = receipts._flag_value(argv, "--repo")
    issue = receipts._flag_value(argv, "--issue")
    if not repo or not issue or not issue.isdigit():
        return None
    try:
        import batch_auth  # noqa: PLC0415
        return batch_auth.issue_key(repo, int(issue))
    except Exception:  # noqa: BLE001 - no key is the placeholder, never an error
        return None


def precheck_command(call: _Call, mode: dict) -> str:
    """The exact precheck call, in the call's own spelling. A mise task that
    cannot carry the precheck form (its fixed prefix is a strip token, or the
    add token has a fixed position the task cannot reach) prints the python3
    form instead."""
    argv = receipts.precheck_argv(call.tool, mode, call.argv)
    form = mode["precheck_form"]
    spelling = call.spelling
    if call.via_mise is not None:
        fixed = call.via_mise["prefix"]
        if "add_at" in form or set(fixed) & set(form["strip"]):
            spelling = ["python3", str(FORGE_ROOT / call.tool["path"])]
        else:
            argv = argv[len(fixed):] if argv[:len(fixed)] == fixed else argv
    if not call.tool["path"].endswith(".py") and call.via_mise is not None:
        spelling = [str(FORGE_ROOT / call.tool["path"])]
    return shlex.join([*spelling, *argv])


def refusal_id(sid: str, tool: dict, mode: dict, fp: str, ts: float) -> str:
    return hashlib.sha256(f"{sid}|{tool['path']}|{mode['name']}|{fp}|{ts!r}".encode()).hexdigest()[:12]


def refusal_text(call: _Call, mode: dict, fp: str, rid: str | None, why: str = "") -> str:
    key = _issue_key(call.argv) or "<KEY>"
    lines = [f"precheck gate (harmonic-forge#1291): {call.tool['path']} {mode['name']} is a real "
             f"run with no live precheck receipt for these exact arguments at this state{why}.",
             "Run its precheck form first, then this call unchanged:",
             f"  {precheck_command(call, mode)}",
             f"Argument fingerprint: {fp[:12]}."]
    if rid:
        lines.append(f"Refusal id: {rid}. Only the operator can release this one call, by typing "
                     f"as a prompt's first line: FORCE {key} gate {rid}: <reason>")
    lines.append(PRECEDENT)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The store: decisions and refusal records.

def refusals_dir() -> Path:
    return receipts.store_dir() / "refusals"


def log(outcome: str, *, tool: str | None = None, mode: str | None = None, klass: str | None = None,
        fp: str | None = None, sid: str = "", now: float | None = None, tuid: str | None = None,
        exit: int | None = None, reason: str | None = None) -> None:
    decision_row.record_gate(receipts.store_dir(), decision_row.row(
        outcome, tool=tool, mode=mode, klass=klass, fp=fp, sid=sid, tuid=tuid, exit=exit,
        reason=reason, now=now))


def write_refusal(rid: str, sid: str, key: str | None, call: _Call, mode: dict, fp: str, ts: float) -> None:
    """Never overwritten: a later refusal never changes what an earlier id names."""
    path = refusals_dir() / f"{rid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"id": rid, "session": sid, "key": key, "tool": call.tool["path"], "mode": mode["name"],
              "fp": fp, "canonical": receipts.canonical(call.tool, mode, call.argv), "ts": ts}
    with contextlib.suppress(FileExistsError):
        with path.open("x", encoding="utf-8") as fh:
            json.dump(record, fh)


def read_refusal(rid: str) -> dict | None:
    if not REFUSAL_ID.match(rid or ""):
        return None
    try:
        data = json.loads((refusals_dir() / f"{rid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("id") == rid else None


# --------------------------------------------------------------------------
# The decision.

def _decide_call(call: _Call, sid: str, cwd: str, deadline: float, *, store: bool,
                 tuid: str | None = None) -> str | None:
    found = receipts.classify(call.tool, call.argv)
    if found is None:
        return None
    mode, real = found
    if not real:
        if store:
            log("precheck", tool=call.tool["path"], mode=mode["name"], klass=mode["class"], sid=sid, tuid=tuid)
        return None
    if not store:
        # The harness has no store and no state: the fingerprint is the argv's alone.
        canon = json.dumps(receipts.canonical(call.tool, mode, call.argv))
        return refusal_text(call, mode, hashlib.sha256(canon.encode()).hexdigest(), None)
    fp = receipts.fingerprint(call.tool, mode, call.argv, cwd, deadline)
    if receipts.live_receipt(sid, fp) is not None:
        log("allowed-receipt", tool=call.tool["path"], mode=mode["name"], klass=mode["class"], fp=fp, sid=sid,
            tuid=tuid)
        return None
    if receipts.gate_grant_usable(call.tool, mode, call.argv, fp):
        log("allowed-override", tool=call.tool["path"], mode=mode["name"], klass=mode["class"], fp=fp, sid=sid,
            tuid=tuid)
        return None
    ts = time.time()
    rid = refusal_id(sid, call.tool, mode, fp, ts)
    write_refusal(rid, sid, _issue_key(call.argv), call, mode, fp, ts)
    log("refused", tool=call.tool["path"], mode=mode["name"], klass=mode["class"], fp=fp, sid=sid, tuid=tuid)
    return refusal_text(call, mode, fp, rid)


def _cd_target(argv: list[str], cwd: str) -> str | None:
    if argv[:1] != ["cd"] or len(argv) != 2 or "$" in argv[1]:
        return None
    return str(Path(cwd) / Path(argv[1]).expanduser())


def decide_command(command: str, sid: str, cwd: str, *, store: bool = True,
                   tuid: str | None = None) -> str | None:
    """The deny reason for one Bash command, or None."""
    deadline = time.monotonic() + receipts.DEADLINE_S
    try:
        calls, others, parsed = _calls_in(command)
        if not parsed:
            if store:
                log("error", sid=sid, tuid=tuid, reason="parse")
            return None
        if store:
            import effects  # noqa: PLC0415
            for argv in others:
                found = effects.classify_outside(argv, receipts.registry())
                if found is not None and not found.read:
                    log("guarded-elsewhere", tool=found.family, klass=found.klass, sid=sid, tuid=tuid)
        current = cwd
        for item in shell_parse.simple_commands(command):
            if isinstance(item, shell_parse.SimpleCommand):
                target = _cd_target(list(item.argv), current)
                if target:
                    current = target
        where = current if len(calls) == 1 else cwd
        for call in calls:
            reason = _decide_call(call, sid, where, deadline, store=store, tuid=tuid)
            if reason:
                return reason
        return None
    except Exception:  # noqa: BLE001 - fail open: the tool's own consume() still refuses
        if store:
            with contextlib.suppress(Exception):
                log("error", sid=sid, tuid=tuid, reason="fault")
        return None


def decide(payload: dict) -> str | None:
    if not isinstance(payload, dict) or payload.get("tool_name") not in ("Bash", None):
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return None
    sid = str(payload.get("session_id") or "").strip() or receipts.NO_SESSION
    cwd = str(payload.get("cwd") or os.getcwd())
    tuid = payload.get("tool_use_id")
    return decide_command(command, sid, cwd, tuid=tuid if isinstance(tuid, str) and tuid else None)


def refuses(command: str, cwd: str | None, env_names) -> str | None:
    """The replay harness's layer function: the store guard, then the gate
    with no session and no store (every real run of a precheck mode refuses)."""
    import precheck_store_guard  # noqa: PLC0415
    reason = precheck_store_guard.guard({"tool_name": "Bash", "tool_input": {"command": command}})
    if reason:
        return reason
    return decide_command(command, "", cwd or str(FORGE_ROOT), store=False)
