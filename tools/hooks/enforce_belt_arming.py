#!/usr/bin/env python3
"""`PreToolUse` guard: belt-and-suspenders is armed with exactly the calls
`tools/lane/belt_plan.py` prints, or not at all (harmonic-forge#659).

Matcher: `Monitor|CronCreate|Skill|Bash|Write|Edit|MultiEdit|NotebookEdit`.
Also registered on `PostToolUse` with matcher `CronCreate|CronDelete`, where it
only maintains the arming record and never decides (harmonic-forge#675).
The arming checks are active only when `LANE` is 1, 2 or 3; the grant-directory
write guard (below) is active in every session.

WHY A HOOK
----------
On 2026-09-14 Lane 3 armed the protocol wrong after the canonical table (#651)
already existed: a multi-paragraph `/loop` prompt it wrote itself, frozen into a
`CronCreate` job, and the repo-wide account-wide belt sweep that exhausted the
shared REST budget twice that day. The literal strings were in context. Prose
was not enough; `watch_lane_posts.py` validates its own argv but nothing saw the
tool calls. This does.

WHAT IS DENIED (in a LANE=1/2/3 session)
----------------------------------------
- `Skill` `loop` (or `<plugin>:loop`) whose args, stripped, are not exactly
  `10m proactively find work to do`.
- `CronCreate` that is not exactly `{"cron": "*/10 * * * *", "prompt":
  "proactively find work to do", "recurring": true}` (`recurring` absent counts
  as true, the tool's default). That is the call the `/loop` skill makes for the
  canonical loop, observed in real transcripts right after the `Skill` call.
- A second canonical `CronCreate` in the same session ("already armed"): the
  first allowed one is recorded under `~/.cache/harmonic-forge/belt_arming/
  <session_id>`, so a re-arm cannot stack a duplicate job (#659 preclose C).

THE ARMING RECORD (harmonic-forge#675)
--------------------------------------
PreToolUse writes the record with `created` and `owner_pid` (the session's
`claude` process: `CLAUDE_PID` when its `/proc/<pid>/comm` is `claude`, else the
nearest ancestor whose `comm` is -- the kernel's process name, so a checkout path
or a wrapper shell that merely mentions "claude" is never mistaken for it). PostToolUse `CronCreate` adds the job `id`, and
PostToolUse `CronDelete` of that same id removes the record, so a deleted
suspenders job can be re-armed. Deleting any other cron leaves it. A record that
cannot correspond to a live job is stale and does not block a re-arm: one with no
`id` older than `ID_LESS_STALE_SECONDS` (the create was denied or failed after
PreToolUse wrote it), or one whose `owner_pid` is gone or whose `comm` is no longer
`claude` (the session was resumed in a new process, and session-only crons die with
the old one). A record with NO owner (no `claude` process was found, such as a
node-based install whose `comm` is `node`) keeps the mechanics unchanged: with no `id`
it expires after `ID_LESS_STALE_SECONDS`; with an `id` it never goes stale, so a re-arm
is denied with the message naming the marker to remove (harmonic-forge#927).
- `Monitor` whose command *executes* `watch_lane_posts.py` -- as the program, or
  as the script argument to `python`/`python3` -- and is not, after whitespace
  normalization, the lane's canonical command (`~`, `$HOME`, `${HOME}` and the
  absolute home path are equivalent spellings). Commands that only mention the
  name (`pgrep -af watch_lane_posts.py`, `grep`, `tail`) are allowed.

OPERATOR OVERRIDE: `ALLOW LOOP`
------------------------------
A line the operator types starting `ALLOW LOOP` makes `grant_loop_override.py`
(`UserPromptSubmit`) write `<session_id>.loop_grant` into the arming directory.
The grant is valid for `GRANT_TTL_SECONDS` and covers ONE non-canonical arming:

- a non-canonical `Skill` `loop` is allowed and records its args in the grant,
  WITHOUT consuming it, because the loop skill's own `CronCreate` follows;
- a non-canonical `CronCreate` consumes (deletes) the grant. After a `Skill`
  call it must be that skill's cron -- its prompt a substring of the recorded
  args -- so the pair is one arming, and a second `Skill` under the same grant
  is denied. With no prior `Skill` call, any one `CronCreate` consumes it.

The override never touches the `Monitor`/`watch_lane_posts.py` check, and an
expired grant is deleted and ignored.

Agents cannot write the grant: any `Bash` or `Monitor` command that mentions
the arming directory (or a `.loop_grant` file) is denied unless every command
segment is a read-only program with no redirect, and `Write`/`Edit`/
`MultiEdit`/`NotebookEdit` into the directory are denied. Accident prevention
against a self-minted override, not a sandbox (same framing as
`batch_provenance.py`).

There is no keyword matching (#659 preclose A and D): a regex over free text
let reworded suspenders prompts through and denied unrelated crons. Instead,
every non-canonical loop or cron in a lane session is denied, and the deny
reason points other timed work at `Monitor` or Bash `run_in_background`, which
this hook does not restrict. Back-off between ticks is `ScheduleWakeup`, which
this hook does not govern either.

HOW IT DENIES
-------------
JSON on stdout (`hookSpecificOutput.permissionDecision: "deny"`), exit 0 --
never exit 2, so the guarded registration's `|| true` swallows nothing. Every
deny reason quotes the exact calls from `belt_plan.canonical_calls`. The hook
fails open on its own exceptions: a broken guard must never wedge a lane.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

LANES = ("1", "2", "3")

#: The `CronCreate` the `/loop` skill makes for `/loop 10m proactively find work
#: to do` (observed in real transcripts). `recurring` absent means true.
LOOP_CRON = {"cron": "*/10 * * * *", "prompt": "proactively find work to do",
             "recurring": True}

WATCHER_NAME = "watch_lane_posts.py"

#: Where the per-session "canonical cron already allowed" markers live.
#: Overridable for tests.
ARMING_DIR_ENV = "HARMONIC_FORGE_BELT_ARMING_DIR"
DEFAULT_ARMING_DIR = Path.home() / ".cache" / "harmonic-forge" / "belt_arming"

#: An arming record with no job id older than this never had a successful create.
ID_LESS_STALE_SECONDS = 120

#: `ALLOW LOOP` grants: `<session_id>` + this suffix, valid for the TTL.
GRANT_SUFFIX = ".loop_grant"
GRANT_TTL_SECONDS = 600

#: Programs that cannot write a file on their own (a redirect is checked
#: separately). Anything else mentioning the arming directory is denied.
_READ_ONLY_PROGRAMS = frozenset({
    "ls", "cat", "stat", "head", "tail", "grep", "rg", "wc", "file", "cd",
    "pwd", "test", "[", "echo", "printf", "true", "find", "du", "jq", "less",
})
_FIND_WRITE_FLAGS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir",
                               "-fprint", "-fprint0", "-fprintf", "-fls"})
_HARMLESS_REDIRECT_RE = re.compile(r"^\d*>>?(?:/dev/null|&\d)$")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PYTHON_RE = re.compile(r"^python(\d+(\.\d+)?)?$")


def _load_belt_plan():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lane"))
    import belt_plan  # noqa: PLC0415
    return belt_plan


def _belt_plan_or_error() -> tuple[Any | None, str | None]:
    """`(belt_plan, None)`, or `(None, why)` when the plan cannot load.

    harmonic-forge#917: the canonical table is built from projects.toml at import,
    so a manifest the loader rejects (an onboarded row with no `workspace`, say)
    raises here. Reaching main()'s fail-open would ALLOW any belt, /loop or
    CronCreate, so every arming branch fails closed on it instead, while still
    honoring the operator's plan-free ALLOW LOOP grant (sticky-wicket ruling).
    """
    try:
        return _load_belt_plan(), None
    except Exception as exc:  # noqa: BLE001 -- reported in the deny reason
        return None, f"{type(exc).__name__}: {exc}"


def _unloadable(what: str, why: str) -> str:
    return (f"Denied: {what}, but the belt plan cannot load ({why}), so nothing is "
            "canonical. Fix projects.toml, then run `python3 "
            "~/harmonic-forge/tools/lane/belt_plan.py` (harmonic-forge#917)."
            + _OVERRIDE_HINT)


def _session_calls(belt_plan, lane: str,
                   payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """The arming calls for the session's own workspace, and why they are missing.

    harmonic-forge#917: the canonical Monitor command carries the workspace of the
    session's checkout, read from the payload's `cwd` and nothing else. When that
    cannot be resolved the second value says why and `calls["monitor"]` is a note,
    not a command: the Monitor branch denies (an unresolved session must never arm
    a belt over some default workspace), while `/loop` and `CronCreate`, whose calls
    are the same in every workspace, are judged exactly as before.
    """
    try:
        workspace = belt_plan.workspace_for(payload.get("cwd"))
        return belt_plan.canonical_calls(lane, workspace), None
    except (belt_plan.ManifestError, KeyError) as exc:
        why = str(exc)
        return {"monitor": f"(no canonical belt here: {why})",
                "loop": belt_plan.loop_call(lane)}, why


def arming_dir() -> Path:
    override = os.environ.get(ARMING_DIR_ENV)
    return Path(override) if override else DEFAULT_ARMING_DIR


def grant_path(session_id: object) -> Path | None:
    """Where `session_id`'s `ALLOW LOOP` grant lives, or None for a bad id."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return None
    return arming_dir() / (session_id + GRANT_SUFFIX)


def write_grant(session_id: str, reason: str, now: float | None = None) -> Path | None:
    """Record a fresh grant (replacing any earlier one). Hook-internal only."""
    path = grant_path(session_id)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    created = time.time() if now is None else now
    path.write_text(json.dumps({"created": created, "reason": reason}) + "\n",
                    encoding="utf-8")
    return path


def _archive_state_file(path: Path, reason: str) -> None:
    """Best-effort archive before a *semantic* unlink (harmonic-forge#826).

    Unlike a housekeeping prune, these deletes are the state transition
    itself: removing a grant is how it is consumed or expired, and removing
    the cron marker is how CronDelete retires it. Gating them on the archive
    would leave a consumed grant reusable, so the unlink always happens and
    this only records what it removed."""
    try:
        telemetry = str(Path(__file__).resolve().parent.parent / "telemetry")
        if telemetry not in sys.path:
            sys.path.insert(0, telemetry)
        import archive  # noqa: PLC0415
        archive.archive_file("belt-arming-state", path, reason=reason)
    except Exception:
        pass


def read_grant(session_id: object, now: float | None = None) -> dict[str, Any] | None:
    """The live grant for this session, or None. An expired or unreadable
    grant is deleted: it must not linger to be revived."""
    path = grant_path(session_id)
    if path is None or not path.exists():
        return None
    now = time.time() if now is None else now
    try:
        grant = json.loads(path.read_text(encoding="utf-8"))
        created = float(grant["created"])
    except (OSError, ValueError, KeyError, TypeError):
        _archive_state_file(path, "grant-unreadable")
        path.unlink(missing_ok=True)
        return None
    if not (created <= now + 60 and now - created <= GRANT_TTL_SECONDS):
        _archive_state_file(path, "grant-expired")
        path.unlink(missing_ok=True)
        return None
    return grant


def _grant_markers() -> list[str]:
    markers = {str(arming_dir()), str(DEFAULT_ARMING_DIR),
               "harmonic-forge/belt_arming", "belt_arming/", GRANT_SUFFIX}
    return sorted(markers)


def _mentions_arming_dir(text: str) -> bool:
    home = os.path.expanduser("~").rstrip("/")
    expanded = (text.replace("${HOME}", home).replace("$HOME", home)
                .replace("~/", home + "/"))
    return any(marker in expanded for marker in _grant_markers())


def _segment_is_read_only(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if ">" in token and not _HARMLESS_REDIRECT_RE.match(token):
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            if not (re.match(r"^\d*>>?$", token) and following in ("/dev/null", "&1", "&2")):
                return False
    tokens = strip_invocation_prefix(tokens)
    if not tokens:
        return True
    program = Path(tokens[0]).name
    if program not in _READ_ONLY_PROGRAMS:
        return False
    return not (program == "find" and _FIND_WRITE_FLAGS & set(tokens[1:]))


def command_writes_arming_dir(command: str) -> bool:
    """True when a shell command mentions the arming directory and is not
    plainly read-only -- the test for a self-written `ALLOW LOOP` grant."""
    if not _mentions_arming_dir(command):
        return False
    try:
        segments = command_segments(command)
    except ValueError:  # unbalanced quotes: cannot show it is read-only
        return True
    return not all(_segment_is_read_only(segment) for segment in segments)


def _path_in_arming_dir(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return _mentions_arming_dir(value)


def _write_guard_reason(tool: str, tool_input: dict[str, Any]) -> str | None:
    denial = ("Denied: this {what} writes into the belt-arming state directory "
              f"({arming_dir()}). That directory holds hook-owned state -- the "
              "per-session arming record and the operator's ALLOW LOOP grant -- "
              "and only the hooks write it (harmonic-forge#659). Reading it is "
              "fine; nothing in a lane session needs to write it.")
    if tool in ("Bash", "Monitor"):
        command = tool_input.get("command")
        if isinstance(command, str) and command_writes_arming_dir(command):
            return denial.format(what=f"{tool} command")
    elif tool in _WRITE_TOOLS:
        for key in ("file_path", "notebook_path", "path"):
            if _path_in_arming_dir(tool_input.get(key)):
                return denial.format(what=f"{tool} call")
    return None


def _normalize_command(command: str) -> str:
    text = " ".join(command.split())
    home = os.path.expanduser("~").rstrip("/")
    for spelling in ("${HOME}/", "$HOME/", home + "/"):
        text = text.replace(spelling, "~/")
    return text


#: Words that can stand in front of the command a segment really runs: `exec` replaces the shell
#: with the watcher (the idiomatic wrapped belt), and the control keywords land at the head of a
#: segment once `;` splits `if c; then CMD; fi` and `while c; do CMD; done` (harmonic-forge#922).
_SHELL_PREFIXES = {"exec", "if", "then", "else", "elif", "while", "until", "do", "!", "{"}


def _executes_watcher(tokens: list[str]) -> bool:
    """True when this command segment runs `watch_lane_posts.py`."""
    tokens = strip_invocation_prefix(tokens)
    if tokens and tokens[0] in _SHELL_PREFIXES:
        # Kept local rather than added to `shell_parse`'s prefix set, which ten hooks share
        # (harmonic-forge#922). See _SHELL_PREFIXES.
        return _executes_watcher(tokens[1:])
    if tokens and tokens[0] == "timeout":
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        return _executes_watcher(rest[1:])  # drop the DURATION
    if not tokens:
        return False
    program = Path(tokens[0]).name
    if program == WATCHER_NAME:
        return True
    if _PYTHON_RE.match(program):
        for arg in tokens[1:]:
            if not arg.startswith("-"):
                return Path(arg).name == WATCHER_NAME
    return False


_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
#: Wider than the `{-c, -lc}` literal other hooks use: `-ic`, `-xc` and `-ec` are the same
#: "run this string" spelling, and over-detecting here only denies a wrapped belt.
_SHELL_C_FLAG = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")


def _peel_prefixes(tokens: list[str]) -> list[str]:
    """Strip the words that can precede the program: the shared invocation prefixes, this hook's
    own `_SHELL_PREFIXES`, and `env` spelled as a path. `shell_parse` matches `env` by exact
    token, so `/usr/bin/env bash -c ...` kept `/usr/bin/env` as the program; it is normalised
    here rather than in the file ten hooks share (harmonic-forge#922)."""
    tokens = strip_invocation_prefix(tokens, unwrap_shells=False)
    while tokens:
        if tokens[0] in _SHELL_PREFIXES:
            tokens = tokens[1:]
        elif tokens[0] != "env" and Path(tokens[0]).name == "env":
            tokens = ["env"] + tokens[1:]
        else:
            break
        tokens = strip_invocation_prefix(tokens, unwrap_shells=False)
    return tokens


def _wrapper_shell(command: str) -> str | None:
    """harmonic-forge#922: the shell (`bash`, `sh`, ...) whose `-c <string>` runs the watcher,
    else None. `strip_invocation_prefix` unwraps `bash -c` but splits the inner string as ONE
    command, so `bash -c 'cd X; python3 .../watch_lane_posts.py ...'` showed `cd` as the program
    and the Monitor passed every check this hook makes. The inner string is segmented with
    `command_segments`, like the outer one, and searched the same way.

    The recursion ends because each inner string is strictly shorter than the command that
    carried it.

    Allowed by contract (this hook is a mistake-detector, not a boundary, R-0374/R-0378; the
    honest mistake is one `cd`-first `-c` wrapper). These are NOT defects and a finding that
    matches one is a documentation check, not a survivor: an inner script `shlex` cannot parse
    (an apostrophe inside a double-quoted script) or a nest of three or more shells whose
    escaped quotes it cannot round-trip; a shell fed its script on stdin (`bash <<EOF`,
    `bash -s`); `eval`; carriers such as `setsid`, `xargs`, `ssh`, `script -c`; and the
    non-space spelling `bash -c"..."`. An unparseable inner script is allowed rather than
    searched as text, because a text search cannot tell a quoted `pgrep` pattern from a command
    and denies honest Monitors."""
    try:
        segments = command_segments(command)
    except ValueError:
        return None
    for segment in segments:
        tokens = _peel_prefixes(segment)
        if not tokens or Path(tokens[0]).name not in _SHELLS:
            continue
        for index, token in enumerate(tokens[1:], start=1):
            if token == "--command" or _SHELL_C_FLAG.match(token):
                # The script is the first non-option word after the flag: `bash -c -- 'S'`
                # and `bash -cx 'S'` are as valid as `bash -c 'S'`.
                script = next((t for t in tokens[index + 1:] if t != "--" and not t.startswith("-")), None)
                if script is None:
                    break
                try:
                    command_segments(script)
                except ValueError:
                    break  # unparseable inner script: allowed by contract (see above)
                if _monitor_runs_watcher(script):
                    return Path(tokens[0]).name
                break
    return None


def _monitor_runs_watcher(command: str) -> bool:
    try:
        segments = command_segments(command)
    except ValueError:  # unbalanced quotes: the shell would refuse it too
        return WATCHER_NAME in command
    return (any(_executes_watcher(segment) for segment in segments)
            or _wrapper_shell(command) is not None)


_OVERRIDE_HINT = (
    " If the operator wants a one-off non-canonical /loop or CronCreate in this "
    "session, the operator can grant one with a line starting `ALLOW LOOP` in "
    "their own message (valid 10 minutes, one use).")


def _reason(lane: str, calls: dict[str, Any], what: str, override_hint: bool = False) -> str:
    return (
        f"{what}\n\n"
        f"LANE={lane} arms belt-and-suspenders with exactly these two calls, "
        "character for character (from `python3 ~/harmonic-forge/tools/lane/"
        "belt_plan.py`, harmonic-forge#659):\n"
        f"  Monitor {json.dumps(calls['monitor'])}\n"
        f"  Skill {json.dumps(calls['loop'])}\n"
        "The loop skill then makes exactly this CronCreate, once per session:\n"
        f"  CronCreate {json.dumps(LOOP_CRON)}\n"
        "In a lane session those are the only /loop and CronCreate calls allowed. "
        "For any other timed or repeated work use Monitor (a streaming command) "
        "or Bash with run_in_background -- neither is restricted. Back off "
        "between suspenders ticks with ScheduleWakeup, never a new /loop or "
        "CronCreate."
        + (_OVERRIDE_HINT if override_hint else "")
    )


def _is_canonical_cron(tool_input: dict[str, Any]) -> bool:
    return (tool_input.get("cron") == LOOP_CRON["cron"]
            and tool_input.get("prompt") == LOOP_CRON["prompt"]
            and tool_input.get("recurring", True) is True)


def _comm(proc_root: str, pid: int) -> str | None:
    """The kernel's name for the process (`/proc/<pid>/comm`), None if it is gone."""
    try:
        with open(f"{proc_root}/{pid}/comm") as fh:
            return fh.read().strip()
    except OSError:
        return None


def owner_claude_pid(proc_root: str = "/proc", pid: int | None = None,
                     env: Any = os.environ) -> int | None:
    """The session's Claude Code process, identified by `/proc/<pid>/comm` == `claude`
    (harmonic-forge#927): `CLAUDE_PID` when set and its `comm` is `claude` (the recycled-pid
    guard), else the nearest ancestor whose `comm` is, walking `/proc/<pid>/status` `PPid:`
    like `session_model.launch_model`. A text match on the command line took any path or
    wrapper that contained "claude" for the CLI."""
    candidate = str(env.get("CLAUDE_PID", ""))
    if candidate.isdigit() and _comm(proc_root, int(candidate)) == "claude":
        return int(candidate)
    current = os.getpid() if pid is None else pid
    for _ in range(12):
        comm = _comm(proc_root, current)
        if comm is None:
            return None
        if comm == "claude":
            return current
        try:
            with open(f"{proc_root}/{current}/status") as fh:
                parent = next((int(line.split()[1]) for line in fh
                               if line.startswith("PPid:")), 0)
        except (OSError, ValueError):
            return None
        if parent <= 1:
            return None
        current = parent
    return None


def _session_marker(session_id: object) -> Path | None:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return None
    return arming_dir() / session_id


def _read_record(marker: Path) -> dict[str, Any]:
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _record_created(marker: Path, record: dict[str, Any]) -> float:
    created = record.get("created")
    if isinstance(created, (int, float)):
        return float(created)
    return marker.stat().st_mtime  # the pre-#675 format carries no `created`


def _record_is_stale(marker: Path, record: dict[str, Any], now: float, proc_root: str) -> bool:
    if not record.get("id") and now - _record_created(marker, record) > ID_LESS_STALE_SECONDS:
        return True
    owner = record.get("owner_pid")
    if isinstance(owner, int):
        if _comm(proc_root, owner) != "claude":
            return True
    return False


def _write_record(marker: Path, now: float, proc_root: str) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    record = dict(LOOP_CRON, created=now, owner_pid=owner_claude_pid(proc_root))
    marker.write_text(json.dumps(record) + "\n", encoding="utf-8")


def record_post_tool_use(payload: dict[str, Any], lane: str | None) -> None:
    """PostToolUse only maintains the arming record; it never decides (AC7).
    A successful canonical CronCreate adds its job id to the PreToolUse record;
    a successful CronDelete of that id removes the record."""
    if lane not in LANES:
        return
    marker = _session_marker(payload.get("session_id"))
    tool_input = payload.get("tool_input") or {}
    if marker is None or not isinstance(tool_input, dict) or not marker.exists():
        return
    tool = payload.get("tool_name") or ""
    record = _read_record(marker)
    if tool == "CronCreate" and _is_canonical_cron(tool_input) and not record.get("id"):
        response = payload.get("tool_response")
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                return
        job_id = response.get("id") if isinstance(response, dict) else None
        if isinstance(job_id, str) and job_id:
            record["id"] = job_id
            marker.write_text(json.dumps(record) + "\n", encoding="utf-8")
    elif tool == "CronDelete" and record.get("id") and tool_input.get("id") == record["id"]:
        _archive_state_file(marker, "cron-deleted")
        marker.unlink(missing_ok=True)


def decide(payload: dict[str, Any], lane: str | None,
           notes: list[str] | None = None, now: float | None = None,
           proc_root: str = "/proc") -> str | None:
    """The deny reason for this tool call, or None to allow.

    Allowing the canonical CronCreate records it for the session, so the same
    session's next canonical CronCreate is denied as already armed. Allowing a
    call under an `ALLOW LOOP` grant appends a line to `notes` for the caller
    to surface.
    """
    notes = [] if notes is None else notes
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    write_denial = _write_guard_reason(tool, tool_input)
    if write_denial is not None:
        return write_denial
    if lane not in LANES:
        return None
    session_id = payload.get("session_id")

    if tool == "Monitor":
        command = tool_input.get("command") or ""
        if not isinstance(command, str) or not _monitor_runs_watcher(command):
            return None
        belt_plan, broken = _belt_plan_or_error()
        if broken is not None:
            return _unloadable("this Monitor runs watch_lane_posts.py", broken)
        calls, unresolved = _session_calls(belt_plan, lane, payload)
        if unresolved is not None:
            return _reason(lane, calls,
                           "Denied: this Monitor runs watch_lane_posts.py, but the session's "
                           f"workspace cannot be resolved ({unresolved}). A belt arms only "
                           "from inside an onboarded checkout or one of its worktrees, "
                           "scoped to that workspace (harmonic-forge#917).")
        wrapper = _wrapper_shell(command)
        if wrapper is not None:
            return _reason(lane, calls,
                           f"Denied: this Monitor runs watch_lane_posts.py inside `{wrapper} -c`. "
                           "A wrapper is never the canonical belt command: it can `cd` into "
                           "another checkout, and the watcher scopes itself from where it runs "
                           "(harmonic-forge#917, #922). Run the canonical command directly, "
                           "from the checkout the session is in.")
        if _normalize_command(command) != _normalize_command(calls["monitor"]["command"]):
            return _reason(lane, calls,
                           "Denied: this Monitor runs watch_lane_posts.py but is not "
                           f"LANE={lane}'s canonical belt command for this checkout's "
                           "workspace (harmonic-forge#917).")
        # harmonic-forge#680 NC3. The command alone was compared, and
        # `timeout_ms` appeared nowhere in this file — so a belt armed with the
        # canonical command and a NON-canonical lifetime passed the gate, while
        # the poller held a `--deadline-seconds` derived from the intended one.
        # That is the original defect one layer up: a derived number and a real
        # lifetime that nothing checks against each other. The poller's deadline
        # is only trustworthy if the Monitor it runs inside is actually given
        # the matching timeout, so both are compared or neither means anything.
        expected_timeout = calls["monitor"]["timeout_ms"]
        actual_timeout = tool_input.get("timeout_ms")
        if actual_timeout != expected_timeout:
            return _reason(lane, calls,
                           f"Denied: canonical belt command but timeout_ms="
                           f"{actual_timeout!r}, not {expected_timeout!r}. The "
                           "poller's --deadline-seconds is derived from the "
                           "canonical lifetime; arming a different one makes it "
                           "schedule against a window it does not have "
                           "(harmonic-forge#680).")
        return None

    if tool == "Skill":
        skill = tool_input.get("skill") or ""
        if not isinstance(skill, str) or not (skill == "loop" or skill.endswith(":loop")):
            return None
        belt_plan, broken = _belt_plan_or_error()
        calls = None
        if broken is None:
            calls, _unresolved = _session_calls(belt_plan, lane, payload)
        args = tool_input.get("args")
        if calls is not None and isinstance(args, str) and args.strip() == calls["loop"]["args"]:
            return None
        grant = read_grant(session_id)
        if grant is not None and "skill_args" not in grant:
            grant["skill_args"] = args.strip() if isinstance(args, str) else ""
            path = grant_path(session_id)
            assert path is not None  # read_grant returned a grant for this id
            path.write_text(json.dumps(grant) + "\n", encoding="utf-8")
            notes.append("enforce_belt_arming: non-canonical /loop allowed under the "
                         "operator's ALLOW LOOP grant; its CronCreate consumes it.")
            return None
        if broken is not None:
            return _unloadable("this is a /loop in a lane session", broken)
        what = "Denied: in a lane session /loop runs only the canonical suspenders prompt."
        if grant is not None:
            what += (" The operator's ALLOW LOOP grant already covered one /loop "
                     "in this session.")
        return _reason(lane, calls, what, override_hint=True)

    if tool == "CronCreate":
        belt_plan, broken = _belt_plan_or_error()
        calls = None
        if broken is None:
            calls, _unresolved = _session_calls(belt_plan, lane, payload)
        if broken is not None or not _is_canonical_cron(tool_input):
            grant = read_grant(session_id)
            if grant is not None:
                skill_args = grant.get("skill_args")
                prompt = tool_input.get("prompt")
                if skill_args is None or (isinstance(prompt, str) and prompt.strip()
                                          and prompt.strip() in skill_args):
                    path = grant_path(session_id)
                    assert path is not None
                    _archive_state_file(path, "grant-consumed")
                    path.unlink(missing_ok=True)
                    notes.append("enforce_belt_arming: non-canonical CronCreate allowed; "
                                 "the operator's ALLOW LOOP grant is now consumed.")
                    return None
            if broken is not None:
                return _unloadable("this is a CronCreate in a lane session", broken)
            return _reason(lane, calls,
                           "Denied: in a lane session CronCreate is allowed only as "
                           "the canonical suspenders job the loop skill creates.",
                           override_hint=True)
        marker = _session_marker(session_id)
        if marker is not None:
            now = time.time() if now is None else now
            if marker.exists():
                record = _read_record(marker)
                if not _record_is_stale(marker, record, now, proc_root):
                    if not record.get("id"):
                        # harmonic-forge#814: a record with no job id is the PreToolUse write
                        # of a CronCreate that never completed (the classifier denied it, or it
                        # failed) -- PostToolUse never ran to record an id. It expires on its own
                        # after ID_LESS_STALE_SECONDS, so the right instruction is to wait, not
                        # to delete a file.
                        age = int(now - _record_created(marker, record))
                        remaining = max(1, ID_LESS_STALE_SECONDS - age)
                        return _reason(lane, calls,
                                       f"Denied: an earlier CronCreate attempt in this session "
                                       f"left a record with no job id (job id unknown, recorded "
                                       f"{age}s ago). Run CronList first: if it shows the "
                                       f"suspenders job, they are already armed, so do NOT retry. "
                                       f"If it shows nothing, the attempt was denied or failed: "
                                       f"Retry in {remaining}s, when that record expires.")
                    job = f"as cron `{record['id']}`"
                    return _reason(lane, calls,
                                   f"Denied: the suspenders are already armed in this "
                                   f"session {job}; a second job would stack duplicate "
                                   f"ticks. If CronList does not show it, the operator "
                                   f"removes `{marker}`.")
            _write_record(marker, now, proc_root)
        return None

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            print(json.dumps({}))
            return 0
        if (payload.get("hook_event_name") or "PreToolUse") == "PostToolUse":
            record_post_tool_use(payload, os.environ.get("LANE"))
            print(json.dumps({}))  # never a permissionDecision on PostToolUse (#675 AC7)
            return 0
        notes: list[str] = []
        reason = decide(payload, os.environ.get("LANE"), notes)
    except Exception as exc:  # noqa: BLE001 - fail open, visibly
        print(json.dumps({"systemMessage":
                          f"enforce_belt_arming: guard did not run ({type(exc).__name__}: {exc})"}))
        return 0
    if reason is None:
        print(json.dumps({"systemMessage": " ".join(notes)} if notes else {}))
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
