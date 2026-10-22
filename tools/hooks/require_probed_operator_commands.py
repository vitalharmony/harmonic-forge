#!/usr/bin/env python3
"""Stop hook: a command handed to the operator was probed this turn (harmonic-forge#1151).

Lane 1 kept handing the operator commands it never ran in any form: a script
given as a bare path that is not executable, and a recovery step that the
tool it named would have refused. Two memories and a rule already said
"probe every command first"; neither held. This makes the probe mechanical.

Reforged after two preclose passes (sticky-wicket, operator-approved AC1/AC3
rewrite): the hook no longer guesses commands out of prose or keys them
coarsely. The author declares them, and a probe must match exactly.

- **Declared.** Commands for the operator go in one fenced block whose info
  string is `operator-commands`, opened with 4 or more backticks so an inner
  ``` cannot close it. Each non-blank line not starting with `#` is one
  command. Nothing outside the block is parsed.
- **Exact.** A command is probed only when a Bash call this turn that did not
  error has the same tokens, after `command_segments` and
  `strip_invocation_prefix`, with `~` expanded and path tokens resolved to
  absolute paths (a `cd` earlier in the same line or call moves the base).
  Exactly one fixed transform is also accepted: `--help` or `--dry-run`
  added, one listed mutating flag removed, or `bash -n` /
  `python3 -m py_compile` on the same resolved script. `bash -c` / `sh -c`
  strings are split again and each inner command is matched the same way.
- **Runnable.** A command whose first token is a file with no execute bit is
  refused whatever was probed, and a `cd` to a missing directory is refused.
- **Presence.** A `YOUR NEXT ACTIONS` section that holds a backticked token
  starting with a known program or a `~`/`/` path, but no block, is blocked
  once, asking for the block.

The hook nudges at most once per turn: `stop_hook_active` is honoured before
anything else. A turn that ends at a genuine question is allowed (shared
`turn_question`).

Documented limits (preclose pass 1, accepted): a stop that follows a sibling
Stop hook's block carries `stop_hook_active` and is not re-read; an
unexpected error allows the stop; and a probe skipped by shell control flow
(`true || cmd`) still counts.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402
from tier_model_stop_backstop import scan_turn  # noqa: E402
from turn_question import asks_a_question, last_assistant_text  # noqa: E402

PROGRAMS = ("python3", "bash", "mise", "systemctl", "gh", "git", "cd")
SHELLS = ("bash", "sh")
#: Appending one of these is a read-only form of the same command.
PROBE_FLAGS = ("--help", "--dry-run")
#: Removing one of these is a read-only form of the same command.
MUTATING_FLAGS = ("--force", "--apply", "--push", "--execute", "--yes", "-y", "--reforge")
_BLOCK_RE = re.compile(r"^[ \t]*(?P<fence>`{4,})[ \t]*operator-commands[ \t]*\n"
                       r"(?P<body>.*?)^[ \t]*(?P=fence)`*[ \t]*$", re.M | re.S)
_SECTION_RE = re.compile(r"^[ \t]*#{1,4}[ \t]*YOUR NEXT ACTIONS[ \t]*$", re.I | re.M)
_HEADING_RE = re.compile(r"^[ \t]*#{1,4}[ \t]+\S", re.M)
_COMMAND_SHAPED = re.compile(r"`+[ \t]*(?:(?:%s)\b|~/|/)" % "|".join(PROGRAMS))


def blocks(text: str) -> list[str]:
    return [m.group("body") for m in _BLOCK_RE.finditer(text or "")]


def section(text: str) -> str:
    match = _SECTION_RE.search(text or "")
    if not match:
        return ""
    rest = text[match.end():]
    end = _HEADING_RE.search(rest)
    return rest[:end.start()] if end else rest


def _resolve(token: str, cwd: str) -> str:
    if token.startswith("~"):
        token = os.path.expanduser(token)
    if "/" in token and not token.startswith("-") and "://" not in token:
        return os.path.normpath(os.path.join(cwd, token))
    return token


def normalize(command: str, cwd: str) -> list[tuple[str, ...]]:
    """Each segment of `command` as resolved tokens, `cd` included, with a
    `cd` moving the base for the segments after it. Raises ValueError on a
    line the shell grammar cannot split."""
    out: list[tuple[str, ...]] = []
    for raw in command_segments(command):
        if len(raw) >= 3 and raw[0] in SHELLS and raw[1] == "-c":
            out.extend(normalize(raw[2], cwd))
            continue
        tokens = strip_invocation_prefix(raw, unwrap_shells=False)
        if not tokens:
            continue
        resolved = tuple(_resolve(t, cwd) for t in tokens)
        if resolved[0] == "cd" and len(resolved) > 1:
            cwd = _resolve(tokens[1] if "/" in tokens[1] or tokens[1].startswith("~")
                           else "./" + tokens[1], cwd)
            resolved = ("cd", cwd)
        out.append(resolved)
    return out


def _script_of(offer: tuple[str, ...]) -> str | None:
    if offer and offer[0].startswith("/"):
        return offer[0]
    if len(offer) > 1 and offer[0] in ("bash", "sh", "python3") and offer[1].startswith("/"):
        return offer[1]
    return None


def probed(offer: tuple[str, ...], probes: set[tuple[str, ...]]) -> bool:
    if offer in probes:
        return True
    for probe in probes:
        if len(probe) == len(offer) + 1:
            for i, token in enumerate(probe):
                if token in PROBE_FLAGS and probe[:i] + probe[i + 1:] == offer:
                    return True
        if len(offer) == len(probe) + 1:
            for i, token in enumerate(offer):
                if token in MUTATING_FLAGS and offer[:i] + offer[i + 1:] == probe:
                    return True
    script = _script_of(offer)
    return bool(script) and (("bash", "-n", script) in probes
                             or ("python3", "-m", "py_compile", script) in probes)


def not_runnable(offer: tuple[str, ...]) -> str | None:
    head = offer[0] if offer else ""
    if head == "cd":
        target = offer[1] if len(offer) > 1 else ""
        return None if not target or os.path.isdir(target) else "its directory does not exist"
    if head.startswith("/") and os.path.isfile(head) and not os.access(head, os.X_OK):
        return "not executable as written; prefix it with `bash` or `python3`"
    return None


def decide(payload: dict, probes: list[str] | None = None) -> tuple[str, str]:
    transcript = payload.get("transcript_path") or ""
    if not transcript:
        return "allow", ""
    text = last_assistant_text(Path(transcript))
    if not text or asks_a_question(text):
        return "allow", ""
    declared = blocks(text)
    if not declared:
        if _COMMAND_SHAPED.search(section(text)):
            return "block", ("YOUR NEXT ACTIONS hands the operator commands outside an "
                             "`operator-commands` block (harmonic-forge#1151). Put every "
                             "command in one block opened with four backticks and the info "
                             "string `operator-commands`, one command per line.")
        return "allow", ""
    cwd = payload.get("cwd") or os.getcwd()
    if probes is None:
        calls, truncated = scan_turn(transcript)[:2]
        if truncated:  # an early probe may be unseen: a partial view is not absence
            return "allow", ""
        probes = [call[0] for call in calls]
    probe_set: set[tuple[str, ...]] = set()
    for command in probes:
        try:
            probe_set.update(normalize(command, cwd))
        except ValueError:
            continue
    lines = []
    for body in declared:
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                offers = normalize(line, cwd)
            except ValueError:
                lines.append(f"  - `{line}`: the shell cannot split it")
                continue
            for offer in offers:
                why = not_runnable(offer)
                if why:
                    lines.append(f"  - `{' '.join(offer)}`: {why}")
                elif offer[0] != "cd" and not probed(offer, probe_set):
                    lines.append(f"  - `{' '.join(offer)}`: no Bash call this turn ran it "
                                 "or a read-only form of it")
    if not lines:
        return "allow", ""
    return "block", (
        "The operator-commands block holds commands this turn never probed "
        "(harmonic-forge#1151):\n" + "\n".join(lines) + "\n\nProbe each one first: the same "
        "command, the command with `--help` or `--dry-run`, the command without its "
        "mutating flag, or `bash -n <script>`. A probe that errored does not count.")


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
