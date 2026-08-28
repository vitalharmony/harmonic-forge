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


def strip_invocation_prefix(tokens: list[str]) -> list[str]:
    """Remove shell wrappers before the invoked program."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT.match(token) or token in ("command", "nohup", "time"):
            index += 1
        elif token == "env":
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                if tokens[index] in ("-u", "--unset"):
                    index += 1
                index += 1
        else:
            break
    return tokens[index:]
