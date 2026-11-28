#!/usr/bin/env python3
"""Stop hook: a command handed to the operator was probed this turn (harmonic-forge#1151).

Lane 1 kept handing the operator commands it never ran in any form: a script
given as a bare path that is not executable, and a recovery step that the
tool it named would have refused. Two memories and a rule already said
"probe every command first"; neither held. This makes the probe mechanical.

Epoch-2 reforge (operator-approved 2026-10-22): two earlier designs tried to
rebuild shell meaning from text (which strings are commands, which probe is
"the same"), and every pass found a new gap. This one does not interpret
shell at all.

- **The block is required.** Any final text containing "YOUR NEXT ACTIONS"
  carries exactly one well-formed fenced block whose info string is
  `operator-commands`, opened with 4 or more backticks (so an inner ``` cannot
  close it). `# none` is a valid body. Nothing outside the block is scanned.
- **One command per line.** A line shlex cannot parse, or one holding `|`,
  `||`, `;`, `&`, a redirect, `$(` or a backtick, is refused. `&&` is
  allowed: a whole-string exit 0 under `&&` means every part succeeded.
- **A probe is the line itself.** A line is probed only when a Bash call this
  turn, not errored and not run in the background, has a command string
  equal to the line after trimming, or to the line plus ` --help` or
  ` --dry-run` (only on a line without `&&`).
- **Runnable (AC2).** A line whose first token, after `os.path.expanduser`,
  is a file with no execute bit is refused whatever was probed.
- **No directive exemption here.** A FORCE, PARK, SCAN, UI or `close` line in
  the `operator-commands` block is a shell line like any other; when it is
  refused, the message says it goes in its own `operator-directive` block.

**Directive blocks (harmonic-forge#1639, sticky-wicket REFORGE, operator D71).**
A FORCE, PARK, SCAN, UI or `close` line is typed into the prompt, never run
in a shell, so it goes in its own fenced block whose info string is
`operator-directive`: opened at column 0 with four or more backticks, the
bare info string, exactly one body line (counted with `str.splitlines()`)
starting at column 0, and a closing fence at column 0. Any number of these
blocks may appear, one directive each. Every line that declares the block
(`^[ \t]*`{3,}[ \t]*operator-directive`, at any indent or backtick count)
must open a well-formed one, or the stop is blocked. The body is validated by
running the prompt hook's own authorizers (`expand_lane_shorthand.authorize_
force/park/scan/ui`) with `dry_run=True`, the body as the prompt, the Stop
payload's `session_id` and this process's `LANE`: exactly one of them must
answer, and its answer must be a would-grant. The authorizers are called
directly, never the prompt hook's `main()`, so nothing is fetched, posted or
recorded. A body no authorizer answers must be `close <KEY>`: lowercase
`close`, one space, a key `tasklist_owed.KEY_RE` matches whose number the
prompt hook's close trigger reads, written nonzero with no leading zero,
nothing after it (`close_problem`). These blocks are validated in the turn's
final text, as the commands block is, with or without YOUR NEXT ACTIONS, and
in a question turn too. An error while checking them, `SystemExit` included,
refuses the block; only `KeyboardInterrupt` propagates.

The final text is `final_text`: this turn's newest main-thread assistant text
record, read back from the transcript's end within `scan_turn`'s byte bound,
skipping subagent (`isSidechain`) records and stopping at the turn's start. A
turn with no text of its own has no final text and is allowed; the previous
turn's text is never re-read.

Accepted residual (two-pass sticky-wicket PATCH): a directive block in an
earlier text record of the turn is not read. If the operator types it, the
prompt hook judges it as it judges any typed line, so a malformed one may
still be accepted there (for example a block this hook ignores for a short
closing fence). A final text beyond the byte bound is not found, and the turn
is allowed.

Accepted residuals: the dry run binds the handing session's `session_id`,
`LANE` and `CLAUDE_CODE_ENTRYPOINT`, and state as it is when the turn ends.
A directive meant for a different session (a no-lane session handing over a
FORCE, PARK or UI line) is refused here although that session could use it,
and so is a FORCE line handed over before its preclose receipt exists. State
that changes after the hand-off (a new head pushed) is refused by the
authorizer at prompt time, which writes nothing.

The hook nudges at most once per turn: `stop_hook_active` is honoured before
anything else. A turn that ends at a genuine question (shared
`turn_question`) or a truncated transcript scan withholds only the "never
probed" finding; every other check still runs.

Documented limits: a stop that follows a sibling Stop hook's block carries
`stop_hook_active` and is not re-read; an unexpected error allows the stop;
and a relative path whose meaning changed through an earlier `cd` is out of
scope.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tier_model_stop_backstop as backstop  # noqa: E402
from tier_model_stop_backstop import scan_turn  # noqa: E402
from turn_question import asks_a_question  # noqa: E402

PROBE_SUFFIXES = (" --help", " --dry-run")
_ACTIONS_RE = re.compile(r"YOUR NEXT ACTIONS", re.I)
_BLOCK_RE = re.compile(r"^[ \t]*(?P<fence>`{4,})[ \t]*operator-commands[ \t]*\n"
                       r"(?P<body>.*?)^[ \t]*(?P=fence)`*[ \t]*$", re.M | re.S)
#: A fence line declaring the block, at any backtick count; never inline code.
_DECLARED_RE = re.compile(r"^[ \t]*`{3,}[ \t]*operator-commands[ \t]*$", re.M)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: harmonic-forge#1639: any line declaring a directive block, at any indent,
#: backtick count or info-string tail. Each must open a well-formed block.
_DIRECTIVE_DECLARED_RE = re.compile(r"^[ \t]*`{3,}[ \t]*operator-directive")
#: The one well-formed opener: column 0, four or more backticks, the bare info string.
_DIRECTIVE_OPEN_RE = re.compile(r"^(?P<fence>`{4,})operator-directive[ \t]*$")
DIRECTIVE_KINDS = "FORCE, PARK, SCAN, UI or `close <KEY>`"
_DIRECTIVE_WORDS = ("force", "park", "scan", "ui", "close")
_DIRECTIVE_HINT = ("; a FORCE/PARK/SCAN/UI/close line goes in its own `operator-directive` "
                   "block, not here")
CLOSE_REFUSAL = ("a close line is exactly `close <KEY>`: lowercase `close`, one space, the "
                 "key, nothing after it")


def blocks(text: str) -> list[str]:
    return [m.group("body") for m in _BLOCK_RE.finditer(text or "")]


def lines_of(body: str) -> list[str]:
    return [ln.strip() for ln in body.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def segments(line: str) -> tuple[list[str], set[str]] | None:
    """One quote-aware pass over `line` (epoch-2 sticky-wicket PATCH): the
    `&&`-joined commands and the operators found outside quotes, or None on
    an unterminated quote or a trailing escape. Every check reads quoting from
    here, never from raw text: `$(` and backticks count unquoted or inside
    double quotes (they run there), never inside single quotes."""
    parts, hits, cur = [], set(), []
    quote, i = None, 0
    while i < len(line):
        c = line[i]
        if quote == "'":
            if c == "'":
                quote = None
            cur.append(c)
        elif c == "\\":
            if i + 1 >= len(line):
                return None
            cur.append(line[i:i + 2])
            i += 2
            continue
        elif quote == '"':
            if c == '"':
                quote = None
            elif c == "`" or line.startswith("$(", i):
                hits.add("$(" if c == "$" else "`")
            cur.append(c)
        elif line.startswith("$'", i) or line.startswith('$"', i):
            return None  # ANSI-C or locale quoting: refused, never modelled
        elif c == "#" and (not cur or cur[-1].isspace()):
            return None  # a comment: everything after it is invisible here
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif line.startswith("&&", i):
            parts.append("".join(cur).strip())
            cur = []
            i += 2
            continue
        elif c in ";|&<>()":
            hits.add(c)
            cur.append(c)
        elif c == "`" or line.startswith("$(", i):
            hits.add("$(" if c == "$" else "`")
            cur.append(c)
        else:
            cur.append(c)
        i += 1
    if quote:
        return None
    parts.append("".join(cur).strip())
    return parts, hits


def shape_problem(line: str) -> str | None:
    """Why a line is not one plain command, or None."""
    parsed = segments(line)
    if parsed is None:
        return "the shell cannot split it; split it or simplify it"
    parts, hits = parsed
    if hits & {"$(", "`"}:
        return "holds command substitution; split it or simplify it"
    if hits or any(not p for p in parts):
        bad = " ".join(sorted(hits)) or "an empty command"
        return f"holds {bad}; one command per line (`&&` is allowed)"
    return None


def probed(line: str, probes: set[str]) -> bool:
    if line in probes:
        return True
    parsed = segments(line)
    return parsed is not None and len(parsed[0]) == 1 and any(
        line + s in probes for s in PROBE_SUFFIXES)


def not_runnable(line: str) -> bool:
    """AC2 for each `&&`-joined command, past any `VAR=value` prefix."""
    parsed = segments(line)
    if parsed is None:
        return False  # shape_problem already refuses an unparseable line
    for part in parsed[0]:
        try:
            tokens = shlex.split(part)
        except ValueError:
            return True  # fail closed: the part cannot be read as a command
        while tokens and _ASSIGNMENT_RE.match(tokens[0]):
            tokens.pop(0)
        if not tokens:
            continue
        path = os.path.expanduser(tokens[0])
        if "/" in path and os.path.isfile(path) and not os.access(path, os.X_OK):
            return True
    return False


def close_problem(line: str) -> str | None:
    """The `close` directive's whole-line check (harmonic-forge#1639): its only
    home, since `close` has no prompt authorizer. None when `line` is
    `close <KEY>` exactly, else the refusal."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tasklist_owed  # noqa: PLC0415

    word, sep, key = line.partition(" ")
    match = tasklist_owed.KEY_RE.fullmatch(key)
    if word != "close" or sep != " " or not match:
        return CLOSE_REFUSAL
    # Epoch pass 1, survivor 2: the number is the one the prompt hook's own
    # close grammar (`_ATTRIB_TRIGGER_RE`) reads off the line, written as the
    # task-list entry writes it: nonzero, no leading zero (PARK's rule).
    from expand_lane_shorthand import _ATTRIB_TRIGGER_RE  # noqa: PLC0415
    trigger = _ATTRIB_TRIGGER_RE.match(line)
    number = match.group(2)
    if trigger is None or trigger.group(1) != "close" or trigger.group(3) != number \
            or number != str(int(number)) or int(number) == 0:
        return CLOSE_REFUSAL
    return None


def directive_blocks(text: str) -> tuple[list[str], list[str]]:
    """`(bodies, problems)` for every `operator-directive` declaration in
    `text`. A declaration that does not open a well-formed block (column-0
    fence of four or more backticks, bare info string, one body line at
    column 0, closing fence at column 0) is a problem, never skipped."""
    lines = (text or "").split("\n")
    bodies, problems = [], []
    for index, line in enumerate(lines):
        if not _DIRECTIVE_DECLARED_RE.match(line):
            continue
        opener = _DIRECTIVE_OPEN_RE.match(line)
        if opener is None:
            problems.append(f"  - `{line.strip()}`: an `operator-directive` block opens at "
                            "column 0 with four or more backticks and the bare info string")
            continue
        fence = opener.group("fence")
        body = lines[index + 1] if index + 1 < len(lines) else None
        close = lines[index + 2] if index + 2 < len(lines) else None
        closed = close is not None and re.fullmatch(re.escape(fence) + r"`*[ \t]*", close)
        if body is None or not closed:
            problems.append("  - an `operator-directive` block holds exactly one line and "
                            "closes at column 0 with its own fence on the next line")
        elif not body.strip() or body.splitlines() != [body]:
            # Epoch pass 1, survivor 5: `splitlines()` drops a trailing U+2028 or
            # U+2029, so a count of 1 would pass one; any terminator is refused.
            problems.append(f"  - `{body.strip()}`: an `operator-directive` block holds "
                            "exactly one non-blank line")
        elif body[0] in " \t":
            problems.append(f"  - `{body.strip()}`: a directive line starts at column 0, "
                            "as the operator types it")
        else:
            bodies.append(body)
    return bodies, problems


def _authorizers():
    """The prompt hook's four authorizers, as dry runs. Import errors raise;
    the caller refuses every directive block (fail closed)."""
    from expand_lane_shorthand import (  # noqa: PLC0415
        authorize_force, authorize_park, authorize_scan, authorize_ui)
    return (lambda b, sid, lane: authorize_force(b, sid, lane, dry_run=True),
            lambda b, sid, lane: authorize_park(b, sid, lane, dry_run=True),
            lambda b, sid, lane: authorize_scan(b, sid, dry_run=True),
            lambda b, sid, lane: authorize_ui(b, sid, lane, dry_run=True))


def directive_problem(body: str, session_id: str | None, lane: str | None,
                      authorizers) -> str | None:
    """Why the prompt hook would not honor `body`, or None. Exactly one
    authorizer may answer, and its answer must be a would-grant; a body none
    answers must pass `close_problem`."""
    answers = [run(body, session_id, lane) for run in authorizers]
    answers = [a for a in answers if a is not None]
    if len(answers) > 1:
        return "it reads as more than one directive; one directive per block"
    if answers:
        return None if answers[0].ok is True else answers[0].receipt
    if body.split(" ", 1)[0].lower() == "close":
        return close_problem(body)
    return f"an `operator-directive` block holds only a {DIRECTIVE_KINDS} line"


def directive_problems(texts: list[str], session_id: str | None,
                       lane: str | None) -> list[str]:
    """Every directive block in `texts` (the turn's final text), checked once each."""
    bodies, problems = [], []
    for text in texts:
        found, wrong = directive_blocks(text)
        bodies += found
        problems += wrong
    if not bodies:
        return problems
    try:
        authorizers = _authorizers()
    except KeyboardInterrupt:
        raise  # the operator's interrupt, never a verdict
    except BaseException as exc:  # noqa: BLE001 - fail closed, SystemExit included
        return problems + [f"  - `{b}`: the prompt hook's authorizers could not load "
                           f"({type(exc).__name__})" for b in bodies]
    for body in bodies:
        try:
            why = directive_problem(body, session_id, lane, authorizers)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 - SystemExit must not end the hook as an allow
            why = f"the directive check failed ({type(exc).__name__})"
        if why:
            problems.append(f"  - `{body}`: the prompt hook would refuse it: {why}")
    return problems


def _directive_block(problems: list[str]) -> tuple[str, str]:
    return "block", ("An `operator-directive` block would not be honored as typed "
                     "(harmonic-forge#1639):\n" + "\n".join(problems) + "\n\nEach FORCE, PARK, "
                     "SCAN, UI or `close` line goes alone in its own block, opened at column 0 "
                     "with four backticks and `operator-directive`, and is checked by the prompt "
                     "hook's own authorizers as a dry run.")


def _hint(line: str) -> str:
    return _DIRECTIVE_HINT if line.split(None, 1)[0].lower() in _DIRECTIVE_WORDS else ""


def final_text(transcript: str) -> str:
    """This turn's newest main-thread assistant text, or "" (D8 re-patch,
    post-verdict findings 1 and 2). Walks back from the end, bounded like
    `scan_turn` (not a record count), skips subagent (`isSidechain`) records,
    and stops at the turn's start, so a textless turn yields "" rather than
    the previous turn's text."""
    try:
        for raw in backstop.session_model._tail_lines(
                transcript, chunk_size=backstop._SCAN_CHUNK_BYTES,
                max_bytes=backstop._SCAN_MAX_BYTES):
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if backstop._is_turn_start(entry):
                return ""
            if entry.get("type") != "assistant" or entry.get("isSidechain"):
                continue
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                parts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                if any(parts):
                    return "\n".join(parts)
    except OSError:
        return ""
    return ""


def decide(payload: dict, probes: list[str] | None = None) -> tuple[str, str]:
    transcript = payload.get("transcript_path") or ""
    if not transcript:
        return "allow", ""
    text = final_text(transcript)
    if not text:
        return "allow", ""
    directives = directive_problems([text], payload.get("session_id"), os.environ.get("LANE"))
    if not _ACTIONS_RE.search(text):
        return _directive_block(directives) if directives else ("allow", "")
    # A question turn withholds only "never probed" (operator-approved
    # 2026-10-22): the block shape and AC2 still run, so a question can end
    # the turn without a probe but never bypass the gate.
    question = asks_a_question(text)
    declared = blocks(text)
    if len(declared) != 1 or len(_DECLARED_RE.findall(text)) != 1:
        shape = ("A response with YOUR NEXT ACTIONS carries exactly one "
                 "`operator-commands` block, opened with four backticks, one command "
                 "per line (`# none` when there is no command) (harmonic-forge#1151).")
        if directives:
            return "block", _directive_block(directives)[1] + "\n\n" + shape
        return "block", shape
    complete = not question
    if probes is None and complete:
        calls, truncated = scan_turn(transcript, drop_background=True, require_result=True)[:2]
        complete = not truncated  # a partial view is not absence
        probes = [call[0] for call in calls]
    probe_set = {p.strip() for p in probes or []}
    problems = []
    for line in lines_of(declared[0]):
        why = shape_problem(line)
        if why:
            problems.append(f"  - `{line}`: {why}{_hint(line)}")
        elif not_runnable(line):
            problems.append(f"  - `{line}`: not executable as written; prefix it with "
                            "`bash` or `python3`")
        elif complete and not probed(line, probe_set):
            problems.append(f"  - `{line}`: no Bash call this turn ran exactly this line, "
                            f"or this line plus --help or --dry-run{_hint(line)}")
    if directives:
        verdict, reason = _directive_block(directives)
        if problems:
            reason += "\n\n" + _commands_reason(problems)
        return verdict, reason
    if not problems:
        return "allow", ""
    return "block", _commands_reason(problems)


def _commands_reason(problems: list[str]) -> str:
    return ("The operator-commands block holds commands this turn never probed "
            "(harmonic-forge#1151):\n" + "\n".join(problems) + "\n\nRun each line exactly as "
            "written (or with --help / --dry-run appended) in the foreground before handing it "
            "over. A probe that errored does not count.")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return 0
    try:
        verdict, reason = decide(payload)
    except Exception:
        return 0
    if verdict == "block":
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
