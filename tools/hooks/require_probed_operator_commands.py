#!/usr/bin/env python3
"""Stop hook: a command handed to the operator was probed this turn (harmonic-forge#1151).

Lane 1 kept handing the operator commands it never ran in any form: a script
given as a bare path that is not executable, and a recovery step that the
tool it named would have refused. Two memories and a rule already said
"probe every command first"; neither held. This makes the probe mechanical.

When the turn's final text has a `#`-prefixed `YOUR NEXT ACTIONS` heading,
every command in that section (an inline code span or a fenced block) whose
first token is an allowlisted program or an existing executable path must
match, by key, a Bash call this turn that did not error. The key is the
program plus its first argument or subcommand (`mise run <task>`), with `~`
expanded and paths compared by their trailing parts, so `bash ~/x/y.sh` and
`bash -n /home/u/x/y.sh` are the same command. A command whose first token is
a path to a file with no execute bit is refused whatever was probed: it
cannot run as written.

The hook nudges at most once per turn: `stop_hook_active` is honoured before
anything else, the same order as `enforce_handoff_owed.py`. A turn that ends
at a genuine question is allowed (shared `turn_question`). An operator
command is never a tool call, so the response text is the only signal; the
allowlist and token-wise keys keep the grammar closed.

Documented limits (preclose pass 1, sticky-wicket PATCH; each fix would need
a new mechanism, R-0380/R-0381): a stop that follows a sibling Stop hook's
block carries `stop_hook_active` and is not re-read; an unexpected error
allows the stop; only `~`/`/` paths are script commands, never relative
ones; and a probe skipped by shell control flow (`true || cmd`) still counts.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_parse import command_segments, mask_heredoc_bodies, strip_invocation_prefix  # noqa: E402
from tier_model_stop_backstop import scan_turn  # noqa: E402
from turn_question import asks_a_question, last_assistant_text  # noqa: E402

PROGRAMS = {"python3", "bash", "mise", "systemctl", "gh", "git"}
DOCUMENT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".log", ".toml", ".yml", ".yaml"}
_SECTION_RE = re.compile(r"^[ \t]*#{1,4}[ \t]*YOUR NEXT ACTIONS[ \t]*$", re.I | re.M)
_HEADING_RE = re.compile(r"^[ \t]*#{1,4}[ \t]+\S", re.M)
_FENCE_RE = re.compile(r"(```|~~~)[^\n]*\n(.*?)\1", re.S)
#: Flags whose next token is a value, never the subcommand.
VALUE_FLAGS = {"-C", "-R", "--repo", "-c", "--git-dir", "--work-tree"}
_SPAN_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")


def section(text: str) -> str:
    match = _SECTION_RE.search(text or "")
    if not match:
        return ""
    rest = text[match.end():]
    fences = [(m.start(), m.end()) for m in _FENCE_RE.finditer(rest)]
    for end in _HEADING_RE.finditer(rest):
        if not any(a <= end.start() < b for a, b in fences):  # never a fenced `# comment`
            return rest[:end.start()]
    return rest


def snippets(body: str) -> list[str]:
    fenced = [m.group(2) for m in _FENCE_RE.finditer(body)]
    spans = [m.group(1) for m in _SPAN_RE.finditer(_FENCE_RE.sub("", body))]
    return fenced + spans


def _expand(token: str) -> str:
    return os.path.expanduser(token) if token.startswith("~") else token


def _is_path_command(token: str) -> bool:
    if not token.startswith(("~", "/")):
        return False
    path = Path(_expand(token))
    return path.is_file() and path.suffix not in DOCUMENT_SUFFIXES


def _args(tokens: list[str]) -> list[str]:
    """Non-flag tokens, skipping the value of a value-taking flag."""
    out, skip = [], False
    for token in tokens:
        if skip:
            skip = False
        elif token in VALUE_FLAGS:
            skip = True
        elif not token.startswith("-"):
            out.append(token)
    return out


def _first_arg(tokens: list[str]) -> str:
    args = _args(tokens)
    return args[0] if args else ""


def key_of(tokens: list[str]) -> tuple[str, str] | None:
    """`(program, argument)` for a command the hook gates, else None."""
    tokens = strip_invocation_prefix(tokens)
    if not tokens:
        return None
    head = tokens[0]
    if head in PROGRAMS:
        arg = _first_arg(tokens[1:])
        args = _args(tokens[1:])
        if head in ("mise", "gh") and len(args) > 1:  # `mise run <task>`, `gh pr merge`
            arg = args[0] + " " + args[1]
        return head, _expand(arg)
    if _is_path_command(head):
        return "path", _expand(head)
    return None


def commands(snippet: str) -> list[tuple[str, tuple[str, str], list[str]]]:
    found = []
    try:
        segments = command_segments(mask_heredoc_bodies(snippet))
    except Exception:  # one malformed span never allows the whole response
        return found
    for tokens in segments:
        if not tokens or tokens[0] == "cd":
            continue
        key = key_of(tokens)
        if key:
            found.append((" ".join(tokens), key, tokens))
    return found


def _same_arg(a: str, b: str) -> bool:
    if a == b:
        return True
    if "/" in a and "/" in b:
        pa, pb = Path(a).parts, Path(b).parts
        n = min(len(pa), len(pb))
        return n > 0 and pa[-n:] == pb[-n:]
    return False


def probed(key: tuple[str, str], probe_keys: list[tuple[str, str]]) -> bool:
    program, arg = key
    for p_program, p_arg in probe_keys:
        if p_program == program and _same_arg(arg, p_arg):
            return True
        # `bash -n <script>` probes a bare-path script command.
        if program == "path" and p_program in ("bash", "python3") and _same_arg(arg, p_arg):
            return True
    return False


def not_runnable(tokens: list[str]) -> bool:
    head = strip_invocation_prefix(tokens)[:1]
    if not head or not head[0].startswith(("~", "/")):
        return False
    path = Path(_expand(head[0]))
    return path.is_file() and not os.access(path, os.X_OK)


def decide(payload: dict, probes: list[str] | None = None) -> tuple[str, str]:
    transcript = payload.get("transcript_path") or ""
    if not transcript:
        return "allow", ""
    text = last_assistant_text(Path(transcript))
    body = section(text)
    if not body or asks_a_question(text):
        return "allow", ""
    offered = [c for s in snippets(body) for c in commands(s)]
    if not offered:
        return "allow", ""
    if probes is None:
        calls, truncated = scan_turn(transcript)[:2]
        if truncated:  # an early probe may be unseen: a partial view is not absence
            return "allow", ""
        probes = [call[0] for call in calls]
    probe_keys = [c[1] for p in probes for c in commands(p)]
    unprobed, unrunnable = [], []
    for raw, key, tokens in offered:
        if not_runnable(tokens):
            unrunnable.append(raw)
        elif not probed(key, probe_keys):
            unprobed.append(raw)
    if not unprobed and not unrunnable:
        return "allow", ""
    lines = []
    for raw in unrunnable:
        lines.append(f"  - `{raw}`: not executable as written; prefix it with `bash` or `python3`")
    for raw in unprobed:
        lines.append(f"  - `{raw}`: no Bash call this turn ran it in any form")
    return "block", (
        "YOUR NEXT ACTIONS hands the operator commands this turn never probed "
        "(harmonic-forge#1151):\n" + "\n".join(lines) + "\n\nProbe each one read-only "
        "before handing it over: `bash -n <script>`, `<cmd> --help`, `<cmd> --dry-run`, "
        "or the command without its mutating flag. A probe that errored does not count.")


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
