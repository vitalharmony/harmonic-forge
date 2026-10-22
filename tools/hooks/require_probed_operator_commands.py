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

from tier_model_stop_backstop import scan_turn  # noqa: E402
from turn_question import asks_a_question, last_assistant_text  # noqa: E402

PROBE_SUFFIXES = (" --help", " --dry-run")
_ACTIONS_RE = re.compile(r"YOUR NEXT ACTIONS", re.I)
_BLOCK_RE = re.compile(r"^[ \t]*(?P<fence>`{4,})[ \t]*operator-commands[ \t]*\n"
                       r"(?P<body>.*?)^[ \t]*(?P=fence)`*[ \t]*$", re.M | re.S)
#: A fence line declaring the block, at any backtick count; never inline code.
_DECLARED_RE = re.compile(r"^[ \t]*`{3,}[ \t]*operator-commands[ \t]*$", re.M)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_OPERATOR_CHARS = set(";&|()<>")


def blocks(text: str) -> list[str]:
    return [m.group("body") for m in _BLOCK_RE.finditer(text or "")]


def lines_of(body: str) -> list[str]:
    return [ln.strip() for ln in body.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def shape_problem(line: str) -> str | None:
    """Why a line is not one plain command, or None. Tokens keep their quotes
    (posix=False), so quoted text is never read as an operator, and single-
    quoted text is never read as a substitution (epoch 2 pass 1)."""
    lexer = shlex.shlex(line, posix=False, punctuation_chars="".join(sorted(_OPERATOR_CHARS)))
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return "the shell cannot split it; split it or simplify it"
    if any(("$(" in t or "`" in t) and not t.startswith("'") for t in tokens):
        return "holds command substitution; split it or simplify it"
    bad = sorted({t for t in tokens if t and set(t) <= _OPERATOR_CHARS and t != "&&"})
    if bad:
        return f"holds {' '.join(bad)}; one command per line (`&&` is allowed)"
    return None


def probed(line: str, probes: set[str]) -> bool:
    if line in probes:
        return True
    return "&&" not in line and any(line + s in probes for s in PROBE_SUFFIXES)


def not_runnable(line: str) -> bool:
    """AC2 for each `&&`-joined command, past any `VAR=value` prefix."""
    for part in line.split("&&"):
        try:
            tokens = [t for t in shlex.split(part)]
        except ValueError:
            return False
        while tokens and _ASSIGNMENT_RE.match(tokens[0]):
            tokens.pop(0)
        if not tokens:
            continue
        path = os.path.expanduser(tokens[0])
        if "/" in path and os.path.isfile(path) and not os.access(path, os.X_OK):
            return True
    return False


def decide(payload: dict, probes: list[str] | None = None) -> tuple[str, str]:
    transcript = payload.get("transcript_path") or ""
    if not transcript:
        return "allow", ""
    text = last_assistant_text(Path(transcript))
    if not text or not _ACTIONS_RE.search(text):
        return "allow", ""
    # A question turn withholds only "never probed" (operator-approved
    # 2026-10-22): the block shape and AC2 still run, so a question can end
    # the turn without a probe but never bypass the gate.
    question = asks_a_question(text)
    declared = blocks(text)
    if len(declared) != 1 or len(_DECLARED_RE.findall(text)) != 1:
        return "block", ("A response with YOUR NEXT ACTIONS carries exactly one "
                         "`operator-commands` block, opened with four backticks, one command "
                         "per line (`# none` when there is no command) (harmonic-forge#1151).")
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
            problems.append(f"  - `{line}`: {why}")
        elif not_runnable(line):
            problems.append(f"  - `{line}`: not executable as written; prefix it with "
                            "`bash` or `python3`")
        elif complete and not probed(line, probe_set):
            problems.append(f"  - `{line}`: no Bash call this turn ran exactly this line, "
                            "or this line plus --help or --dry-run")
    if not problems:
        return "allow", ""
    return "block", (
        "The operator-commands block holds commands this turn never probed "
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
