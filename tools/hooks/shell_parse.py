"""Shared shell-command parsing for tools/hooks/ (harmonic-forge#167).

Extracted from block_lane1_status_claims.py, which originally carried this
logic as its own private helpers — mypy_cwd_trap.py needed the same
heredoc-masking + segment-splitting, and a second copy is exactly the drift
risk both hooks exist to avoid: block_lane1_status_claims.py's own
command_segments() already had a real bug (payload cwd never threaded into
decision(), tracked separately, not fixed here) that a duplicated copy would
have silently forked instead of shared.

No fail-open/fail-closed policy lives here — that's each caller's own
decision()/denial() logic. This module only parses; it never denies.
"""

import re
import shlex
from pathlib import Path
from typing import NamedTuple

HEREDOC_START = re.compile(
    r"<<(?P<strip_tabs>-?)\s*(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)"
)


def mask_heredoc_bodies(command: str) -> str:
    """Replace complete heredoc bodies so their prose is not parsed as shell."""
    masked: list[str] = []
    cursor = 0
    search_from = 0
    while match := HEREDOC_START.search(command, search_from):
        line_end = command.find("\n", match.end())
        if line_end == -1:
            break
        delimiter = match.group("delimiter")
        body_start = line_end + 1
        line_start = body_start
        while line_start < len(command):
            next_line_end = command.find("\n", line_start)
            if next_line_end == -1:
                candidate = command[line_start:]
                next_search_from = len(command)
            else:
                candidate = command[line_start:next_line_end]
                next_search_from = next_line_end + 1
            if (candidate.lstrip("\t") if match.group("strip_tabs") else candidate) == delimiter:
                masked.extend((command[cursor:body_start], "__HEREDOC_BODY__\n"))
                cursor = line_start
                search_from = next_search_from
                break
            if next_line_end == -1:
                return command
            line_start = next_line_end + 1
        else:
            return command
    masked.append(command[cursor:])
    return "".join(masked)


def command_segments(command: str) -> list[list[str]]:
    """Split shell control operators and bare newlines while retaining quoted text."""
    punctuation = ";&|()\n"
    lexer = shlex.shlex(mask_heredoc_bodies(command), posix=True, punctuation_chars=punctuation)
    lexer.whitespace_split = True
    lexer.whitespace = lexer.whitespace.replace("\n", "")
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and all(char in punctuation for char in token):
            if segments[-1]:
                segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def strip_invocation_prefix(tokens: list[str], unwrap_shells: bool = True) -> list[str]:
    """Remove shell wrappers before the invoked program.

    Also strips both house-mandated account-scoping wrappers for `gh`
    (`reference_two_account_routing`: "Never `gh auth switch` -- use the
    `gha`/`gh-as` wrapper") so `batch_gate.py`'s classifiers -- which
    require the post-strip token to resolve to `gh` -- see the underlying
    `gh` invocation regardless of which wrapper preceded it
    (harmonic-forge#578):

    - `gh-as <account> <command...>` (`~/harmonic-forge/tools/gh/gh-as`)
      already places a literal `gh` token after the account
      (`gh-as vitalharmony gh pr merge ...`) -- stripping the two leading
      tokens is enough, mirroring the identical handling already present
      in `block_lane2_status_claims.py` and `enforce_gate_ci_on_raw_post.py`
      for this same wrapper.
    - `gha <account> <gh-args...>` (`~/.local/bin/gha`) never places a
      literal `gh` token after the account -- `gha vh issue close 395
      --repo ...` passes `issue close 395 --repo ...` straight through its
      own `exec env GH_TOKEN=... gh "$@"` line. A bare prefix-strip would
      therefore leave tokens starting with `issue`, which still doesn't
      resolve to `gh` -- so this branch additionally *synthesizes* the `gh`
      token `gha`'s own exec line supplies, rather than merely skipping
      past it. Without this, `gha vh issue close ...`/`gha vh pr merge ...`
      classify as `None` ("not this hook's business") -- an unconditional,
      unprompted allow with zero BATCH grant, the identical incident shape
      #578 exists to close, and `gha` is independently mandated by the same
      `reference_two_account_routing` memory as `gh-as` (verified live,
      preclose-inspection finding on this issue) -- not a hypothetical
      wrapper nobody reaches for.
    """
    working = list(tokens)
    index = 0
    while index < len(working):
        token = working[index]
        if _ASSIGNMENT.match(token) or token in ("command", "nohup", "time"):
            index += 1
        elif token == "env":
            index += 1
            while index < len(working) and working[index].startswith("-"):
                if working[index] in ("-u", "--unset"):
                    index += 1
                index += 1
        elif Path(token).name == "gh-as" and index + 1 < len(working):
            index += 2
        elif Path(token).name == "gha" and index + 1 < len(working):
            working = working[:index] + ["gh"] + working[index + 2:]
        # harmonic-forge#787: timeout/nice/stdbuf are the same "invocation
        # prefix" class as env/nohup/time above -- they wrap the real
        # command without invoking a shell, and were simply missing here.
        # Each strips its own flags, then its own positional argument
        # (timeout's DURATION; nice's -n/-N adjustment, when given as a
        # separate token), leaving the wrapped command for the next
        # iteration of this same loop (so a stack like `nice timeout 5 tee`
        # unwraps fully).
        elif Path(token).name == "timeout":
            index += 1
            while index < len(working) and working[index].startswith("-"):
                opt = working[index]
                if opt in ("-k", "--kill-after", "-s", "--signal"):
                    index += 1
                    if index < len(working):
                        index += 1
                else:
                    # bare flags (-v, --preserve-status, --foreground) and
                    # the --kill-after=X/--signal=X joined forms both just
                    # consume one token.
                    index += 1
            if index < len(working):  # the DURATION positional
                index += 1
        elif Path(token).name == "nice":
            index += 1
            # Looped, not a single peek: a preclose refuter reproduced
            # `nice -n5 tee <protected>` and `nice -- tee <protected>` both
            # slipping past a single-token check. `-n` (getopt allows the
            # value attached: `-n5`) and the bare `-NUM` short form both fall
            # to the generic branch below, matching timeout/stdbuf's own
            # permissive consume-any-dash-token style -- an unrecognized
            # flag is consumed rather than left standing in front of the
            # write verb, which would otherwise hide it from every rule below.
            while index < len(working) and working[index].startswith("-") and working[index] != "--":
                opt = working[index]
                if opt in ("-n", "--adjustment"):
                    index += 1
                    if index < len(working):
                        index += 1
                else:
                    index += 1
            if index < len(working) and working[index] == "--":
                index += 1
        elif Path(token).name == "stdbuf":
            index += 1
            while index < len(working) and working[index].startswith("-"):
                opt = working[index]
                if opt in ("-i", "-o", "-e"):
                    index += 1
                    if index < len(working):
                        index += 1
                else:
                    # combined -oL/-iL/-eL, and --input=/--output=/--error=
                    index += 1
        # harmonic-forge#785: write guards pass unwrap_shells=False -- they must
        # still see the nested shell, or their nested-shell rules never fire.
        # harmonic-forge#787: dash/ksh join bash/sh/zsh here -- NESTED_SHELLS
        # in block_lane1_status_claims.py already named all five; only this
        # unwrap set had fallen behind.
        elif (unwrap_shells
              and Path(token).name in {"bash", "sh", "zsh", "dash", "ksh"}
              and index + 2 < len(working)
              and working[index + 1] in {"-c", "-lc", "--command"}):
            try:
                nested = shlex.split(working[index + 2])
            except ValueError:
                return []
            working = working[:index] + nested + working[index + 3:]
        else:
            break
    return working[index:]


# harmonic-forge#1289: simple commands with redirects, for the never-refuse
# sample's ordinary classifier. Additive: nothing above changes.
# `command_segments` leaves `>`/`<` inside words, so `2>&1` yields a program
# `1` and loop keywords and heredoc delimiters read as programs; this lexer
# treats `<>` as punctuation and returns each simple command's argv apart from
# its redirects.

_SIMPLE_PUNCTUATION = ";&|()<>\n"
#: Every operator, longest first within a shared prefix: a punctuation token
#: from shlex (`&&(`, `);`, `<(`) is split left to right by longest match.
_OPERATORS = ("&>>", "&>", "<<<", "<<", "<&", "<>", "<(", ">>", ">|", ">&", ">(", "&&", "||",
              "|&", ";;", ";", "&", "|", "(", ")", "\n", "<", ">")
_SEPARATORS = frozenset({";", "&", "|", "&&", "||", "|&", ";;", "\n"})
#: Reserved words stripped from the front of a simple command's argv.
RESERVED_WORDS = frozenset({"if", "then", "else", "elif", "fi", "do", "done", "while", "until",
                            "{", "}", "!"})
_HEREDOC_MASK = "__HEREDOC_BODY__"


class NotOrdinary(Exception):
    """A command that parses but is not an ordinary read, probe or scratch
    write; `str(exc)` is the counted reason."""


class SimpleCommand(NamedTuple):
    argv: list[str]
    redirects: list[tuple[str, str | None]]
    sep_before: str | None
    sep_after: str | None


class GroupMarker(NamedTuple):
    kind: str  # "open" or "close"


def _split_operators(token: str) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(token):
        for op in _OPERATORS:
            if token.startswith(op, index):
                out.append(op)
                index += len(op)
                break
        else:
            raise ValueError(f"unrecognized shell operator in {token!r}")
    return out


def simple_commands(command: str) -> list[SimpleCommand | GroupMarker]:
    """The simple commands of `command`, in order, with grouping markers.

    Raises `ValueError` when the text does not parse (an unbalanced quote, an
    unknown operator, a redirect with no target) and `NotOrdinary` on an
    output process substitution `>(`."""
    lexer = shlex.shlex(mask_heredoc_bodies(command), posix=True,
                        punctuation_chars=_SIMPLE_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.whitespace = lexer.whitespace.replace("\n", "")
    items: list[tuple[str, str]] = []
    for token in lexer:
        if token and all(char in _SIMPLE_PUNCTUATION for char in token):
            items.extend(("op", op) for op in _split_operators(token))
        else:
            items.append(("word", token))

    out: list[SimpleCommand | GroupMarker] = []
    argv: list[str] = []
    redirects: list[tuple[str, str | None]] = []
    sep_before: str | None = None
    pending: str | None = None
    heredocs: list[str] = []
    closing_delimiter: str | None = None

    def end(sep: str | None) -> None:
        nonlocal argv, redirects, sep_before
        while argv and argv[0] in RESERVED_WORDS:
            argv.pop(0)
        if argv or redirects:
            out.append(SimpleCommand(argv, redirects, sep_before, sep))
        argv, redirects, sep_before = [], [], sep

    for kind, value in items:
        if pending is not None:
            op, pending = pending, None
            if kind != "word":
                raise ValueError(f"redirect {op} has no target")
            if op in (">&", "<&") and (value.isdigit() or value == "-"):
                redirects.append((op, None))
            else:
                if op == "<<":
                    heredocs.append(value[1:] if value.startswith("-") else value)
                redirects.append((op, value))
            continue
        if kind == "word":
            if value == _HEREDOC_MASK:
                closing_delimiter = heredocs.pop(0) if heredocs else None
                continue
            if closing_delimiter is not None:
                delimiter, closing_delimiter = closing_delimiter, None
                if value == delimiter and not argv and not redirects:
                    continue
            argv.append(value)
            continue
        if value == ">(":
            raise NotOrdinary("process-substitution-write")
        if value in _SEPARATORS:
            end(value)
        elif value in ("(", ")"):
            end(value)
            out.append(GroupMarker("open" if value == "(" else "close"))
        elif value == "<(":
            end(value)
            out.append(GroupMarker("open"))
        else:
            if argv and argv[-1].isdigit():
                argv.pop()
            pending = value
    if pending is not None:
        raise ValueError(f"redirect {pending} has no target")
    end(None)
    return out


def substitution_bodies(token: str) -> list[str]:
    """Every `$(...)` body (matched by parenthesis depth) and every backtick
    body in one word. Raises `ValueError` when one is unbalanced."""
    bodies: list[str] = []
    index = 0
    while index < len(token):
        if token.startswith("$(", index):
            depth, end = 1, index + 2
            while end < len(token) and depth:
                depth += {"(": 1, ")": -1}.get(token[end], 0)
                end += 1
            if depth:
                raise ValueError("unbalanced $( substitution")
            bodies.append(token[index + 2:end - 1])
            index = end
        elif token[index] == "`":
            end = token.find("`", index + 1)
            if end < 0:
                raise ValueError("unbalanced backtick substitution")
            bodies.append(token[index + 1:end])
            index = end + 1
        else:
            index += 1
    return bodies
