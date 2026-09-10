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
        else:
            break
    return working[index:]
