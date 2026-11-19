#!/usr/bin/env python3
"""One gate (harmonic-forge#1291): the PreToolUse precheck gate on the effect
registry.

`decide(payload)` reads a Bash call, finds every simple command that runs a
registry tool with a precheck mode (directly, through `python3`, a shell `-c`
string, or a `mise run <task>` the registry maps in `mise_tasks`), and decides
each real run of a precheck mode:

- allow on a live, state-bound receipt for this exact call (`allowed-receipt`);
- allow on a usable `FORCE <KEY> gate <refusal-id>` grant bound to this tool,
  mode and argument fingerprint, spending it (`allowed-override`);
- else deny, printing the exact precheck command, the fingerprint, a refusal
  id and the FORCE line that would release this one call (`refused`).

The precheck form itself is logged `precheck` and allowed. A session-less call
is `ungated`. An outside family (`git push`, `gh pr merge`, ...) is logged
`guarded-elsewhere` and never refused here: its own hook decides it.

On the gate's own error, or its internal 5 s deadline, the decision follows
the mode's `backstop` (design alternative 7): a mode with a tool-side backstop
fails open (the tool's `consume` still refuses), one without fails closed.

Every decision is one row of `decisions.jsonl` in the store: ts, outcome,
tool, mode, class, fp, session. Never an argv token. A refusal also writes a
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


def _calls_in(command: str, depth: int = 0) -> tuple[list[_Call], list[list[str]], bool]:
    """(gated calls, other argvs, parsed). `parsed` is False when the text does not parse."""
    try:
        items = shell_parse.simple_commands(command)
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


def _names_unbackstopped(command: str) -> bool:
    for tool, mode in _precheck_modes():
        if not mode["backstop"] and Path(tool["path"]).name in command:
            return True
    return False


def _precheck_modes():
    import effects  # noqa: PLC0415
    return list(effects.precheck_modes(receipts.registry()))


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
# The store: decisions, refusal records, overrides.

def decisions_path() -> Path:
    return receipts.store_dir() / "decisions.jsonl"


def refusals_dir() -> Path:
    return receipts.store_dir() / "refusals"


def log(outcome: str, *, tool: str | None = None, mode: str | None = None, klass: str | None = None,
        fp: str | None = None, sid: str = "", now: float | None = None) -> None:
    row = {"ts": time.time() if now is None else now, "outcome": outcome, "tool": tool,
           "mode": mode, "class": klass, "fp": fp[:12] if fp else None,
           "session": hashlib.sha256(sid.encode()).hexdigest()[:16] if sid else None}
    with contextlib.suppress(OSError):
        path = decisions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


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


def _override(sid: str, call: _Call, mode: dict, fp: str, *, spend: bool = True) -> bool:
    """Spend a usable gate grant bound to this tool, mode and fingerprint.

    With `spend=False` it only reports whether such a grant exists (harmonic-forge#1291
    sticky-wicket PATCH b): the command's calls are all decided before any grant is
    spent, so a sibling call that is refused never wastes the operator's grant."""
    import force_grant  # noqa: PLC0415
    key = _issue_key(call.argv)
    candidates = [key] if key else [p.stem for p in sorted(force_grant.state_dir().glob("*.json"))]
    binding = {"gate_tool": call.tool["path"], "gate_mode": mode["name"], "arg_fp": fp}
    for candidate in candidates:
        grant = force_grant.read_grant(candidate)
        if not grant or grant.get("mode") != "gate" or grant.get("arg_fp") != fp:
            continue
        if not spend:
            if grant.get("status") == "usable":
                return True
            continue
        try:
            force_grant.consume(candidate, grant.get("granted_at"), "", mode="gate", gate=binding)
        except force_grant.GrantRefused:
            continue
        # harmonic-forge#1291 (sticky-wicket PATCH): the grant itself is the one use.
        # The override receipt exists only for the tool-side backstop to read and
        # spend; a mode with no backstop gets none, and the gate never accepts one.
        if mode["backstop"]:
            receipts.write_receipt(sid, call.tool, mode, fp, kind="override", ttl=receipts.OVERRIDE_TTL_S)
        return True
    return False


# --------------------------------------------------------------------------
# The decision.

def _decide_call(call: _Call, sid: str, cwd: str, deadline: float, *, store: bool,
                 spend: bool = True) -> str | None:
    found = receipts.classify(call.tool, call.argv)
    if found is None:
        return None
    mode, real = found
    if not real:
        if store and spend:
            log("precheck", tool=call.tool["path"], mode=mode["name"], klass=mode["class"], sid=sid)
        return None
    if store and not sid:
        if spend:
            log("ungated", tool=call.tool["path"], mode=mode["name"], klass=mode["class"])
        if mode["backstop"]:
            return None
        # harmonic-forge#1291 (sticky-wicket PATCH): no session means no receipt can
        # match, and nothing on the tool side would catch it: fail closed.
        return (f"precheck gate (harmonic-forge#1291): {call.tool['path']} {mode['name']} has no "
                "tool-side backstop and this call carries no session id, so no precheck receipt "
                "can be matched; denied.")
    if not store:
        # The harness has no store and no state: the fingerprint is the argv's alone.
        canon = json.dumps(receipts.canonical(call.tool, mode, call.argv))
        return refusal_text(call, mode, hashlib.sha256(canon.encode()).hexdigest(), None)
    fp = receipts.fingerprint(call.tool, mode, call.argv, cwd, deadline)
    live = receipts.live_receipt(sid, fp)
    if live is not None and live.get("kind") != "override":
        if spend:
            log("allowed-receipt", tool=call.tool["path"], mode=mode["name"], klass=mode["class"], fp=fp, sid=sid)
        return None
    if _override(sid, call, mode, fp, spend=spend):
        if spend:
            log("allowed-override", tool=call.tool["path"], mode=mode["name"], klass=mode["class"], fp=fp, sid=sid)
        return None
    if not spend:
        return "refused"  # decided; the spending pass writes the refusal record
    ts = time.time()
    rid = refusal_id(sid, call.tool, mode, fp, ts)
    write_refusal(rid, sid, _issue_key(call.argv), call, mode, fp, ts)
    log("refused", tool=call.tool["path"], mode=mode["name"], klass=mode["class"], fp=fp, sid=sid)
    return refusal_text(call, mode, fp, rid)


def _cd_target(argv: list[str], cwd: str) -> str | None:
    if argv[:1] != ["cd"] or len(argv) != 2 or "$" in argv[1]:
        return None
    return str(Path(cwd) / Path(argv[1]).expanduser())


def decide_command(command: str, sid: str, cwd: str, *, store: bool = True) -> str | None:
    """The deny reason for one Bash command, or None."""
    deadline = time.monotonic() + receipts.DEADLINE_S
    matched: dict | None = None
    try:
        calls, others, parsed = _calls_in(command)
        if not parsed:
            if _names_unbackstopped(command):
                if store:
                    log("error", sid=sid)
                return ("precheck gate (harmonic-forge#1291): this command does not parse, and it "
                        "names a gated tool with no tool-side backstop; denied. Run it as a plain "
                        "command line.")
            return None
        if store:
            import effects  # noqa: PLC0415
            for argv in others:
                found = effects.classify_outside(argv, receipts.registry())
                if found is not None and not found.read:
                    log("guarded-elsewhere", tool=found.family, klass=found.klass, sid=sid)
        current = cwd
        for item in shell_parse.simple_commands(command):
            if isinstance(item, shell_parse.SimpleCommand):
                target = _cd_target(list(item.argv), current)
                if target:
                    current = target
        where = current if len(calls) == 1 else cwd
        # harmonic-forge#1291 (sticky-wicket PATCH b): decide every call before
        # spending any grant. Pass 1 spends nothing; if any call would be refused,
        # pass 2 runs only up to it, so its refusal record is written and no
        # earlier call's grant is consumed. Otherwise pass 2 spends the grants.
        verdicts = []
        for call in calls:
            found = receipts.classify(call.tool, call.argv)
            matched = found[0] if found else matched
            verdicts.append(_decide_call(call, sid, where, deadline, store=store, spend=False)
                            if store else None)
        refused = next((i for i, v in enumerate(verdicts) if v), None)
        if refused is not None:
            # The refused call has no usable grant, so this spends nothing and only
            # writes its refusal record and text.
            return _decide_call(calls[refused], sid, where, deadline, store=store)
        for call in calls:
            reason = _decide_call(call, sid, where, deadline, store=store)
            if reason:
                return reason
        return None
    except Exception as exc:  # noqa: BLE001 - the backstop rule decides
        if store:
            log("error", tool=None, mode=(matched or {}).get("name"), sid=sid)
        unbackstopped = (matched is not None and not matched["backstop"]) or (
            matched is None and _safe_names_unbackstopped(command))
        if unbackstopped:
            return (f"precheck gate (harmonic-forge#1291): the gate could not decide this call "
                    f"({type(exc).__name__}), and its mode has no tool-side backstop; denied "
                    "(fail closed). Retry; if it persists, the operator runs it.")
        return None


def _safe_names_unbackstopped(command: str) -> bool:
    try:
        return _names_unbackstopped(command)
    except Exception:  # noqa: BLE001 - the registry itself is unreadable: fail closed
        return True


def decide(payload: dict) -> str | None:
    if not isinstance(payload, dict) or payload.get("tool_name") not in ("Bash", None):
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return None
    sid = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or os.getcwd())
    return decide_command(command, sid, cwd)


def refuses(command: str, cwd: str | None, env_names) -> str | None:
    """The replay harness's layer function: the store guard, then the gate
    with no session and no store (every real run of a precheck mode refuses)."""
    import precheck_store_guard  # noqa: PLC0415
    reason = precheck_store_guard.guard({"tool_name": "Bash", "tool_input": {"command": command}})
    if reason:
        return reason
    return decide_command(command, "", cwd or str(FORGE_ROOT), store=False)
