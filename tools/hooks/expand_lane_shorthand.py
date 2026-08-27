#!/usr/bin/env python3
"""UserPromptSubmit hook: expand lane-shorthand tokens inline before the
prompt reaches the model (harmonic-forge#383).

The token set is parsed from rules/lane-shorthand.md at hook runtime --
never re-declared here -- so a new row in the doc expands with no hook
edit (AC2). A malformed or unreadable doc fails open: the prompt passes
through byte-for-byte rather than blocking the operator's message.

Expansion is additive annotation only (AC3): each recognized token gets
a bracketed gloss appended immediately after it; the operator's literal
text is never rewritten or dropped. Matches inside fenced code blocks are
left alone (AC4) -- and since the token set comes only from the doc's own
tables, an ordinary English word (e.g. a token that happens to also be a
common word) never accidentally matches unless it is actually listed
there.

Run: reads the UserPromptSubmit JSON payload on stdin, writes
{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
"additionalContext": "..."}} or, on any parse/doc failure, exits 0 with
no output (pass-through).
"""
import json
import re
import sys
from pathlib import Path

DOC_PATH = Path(__file__).resolve().parent.parent.parent / "rules" / "lane-shorthand.md"

TABLE_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|")
FENCE = re.compile(r"^\s*```")


def _section(lines: list[str], heading: str) -> list[str]:
    out: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith("## "):
            in_section = line.strip() == f"## {heading}"
            continue
        if in_section:
            out.append(line)
    return out


def parse_lane_tokens(text: str) -> dict[str, str]:
    """## Lane status tokens table -> {TOKEN: meaning}. Skips the header/
    separator rows (their first cell isn't backtick-quoted)."""
    tokens: dict[str, str] = {}
    for line in _section(text.splitlines(), "Lane status tokens"):
        m = TABLE_ROW.match(line)
        if m:
            tokens[m.group(1)] = m.group(2)
    return tokens


def parse_repo_prefixes(text: str) -> dict[str, tuple[str, str]]:
    """## Repo prefixes table -> {PREFIX: (repo_column, account_column)}."""
    prefixes: dict[str, tuple[str, str]] = {}
    lines = _section(text.splitlines(), "Repo prefixes")
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        prefix_m = re.match(r"^`([^`]+)`$", cells[0])
        if not prefix_m:
            continue
        prefix = prefix_m.group(1)
        if len(prefix) != 1 or not prefix.isalpha():
            continue
        repo_m = re.match(r"^`([^`]+)`$", cells[1])
        repo = repo_m.group(1) if repo_m else cells[1]
        prefixes[prefix] = (repo, cells[2])
    return prefixes


def parse_named_directives(text: str) -> dict[str, str]:
    """`## `EOQ`` / `## `BATCH`` style headings -> {TOKEN: first sentence
    after 'Meaning:'}. Both are prose grammar, not table rows, so they're
    parsed separately from parse_lane_tokens()."""
    out: dict[str, str] = {}
    for m in re.finditer(r"^## `([A-Z]+)`.*$", text, re.MULTILINE):
        token = m.group(1)
        start = m.end()
        next_heading = re.search(r"^## ", text[start:], re.MULTILINE)
        section = text[start : start + next_heading.start()] if next_heading else text[start:]
        meaning_m = re.search(r"Meaning:\s*(.+)", section)
        if meaning_m:
            sentence = re.split(r"(?<=[.:])\s{2,}|\n\n", meaning_m.group(1), maxsplit=1)[0]
            out[token] = re.sub(r"\*\*|`", "", sentence).strip()
    return out


def repo_issue_gloss(repo: str, account: str, number: str) -> str:
    if "/" in repo:
        return f"{repo}#{number}"
    account_word = account.split()[0].strip("`,")
    return f"{repo} issue #{number} (account: {account_word})"


def build_annotator(text: str):
    lane_tokens = parse_lane_tokens(text)
    prefixes = parse_repo_prefixes(text)
    directives = parse_named_directives(text)

    lane_alt = "|".join(re.escape(t) for t in sorted(lane_tokens, key=len, reverse=True))
    directive_alt = "|".join(re.escape(t) for t in sorted(directives, key=len, reverse=True))
    prefix_alt = "|".join(re.escape(p) for p in prefixes)

    parts = []
    if lane_alt:
        parts.append(rf"(?P<lane>\b(?:{lane_alt})\b)")
    if directive_alt:
        parts.append(rf"(?P<directive>\b(?:{directive_alt})\b)")
    if prefix_alt:
        parts.append(rf"(?P<repo>\b(?:{prefix_alt})\d+\b)")
    if not parts:
        return None
    pattern = re.compile("|".join(parts))

    def gloss(match: re.Match) -> str:
        gd = match.groupdict()
        if gd.get("lane"):
            return f"{match.group(0)} [{lane_tokens[match.group(0)]}]"
        if gd.get("directive"):
            return f"{match.group(0)} [{directives[match.group(0)]}]"
        if gd.get("repo"):
            token = match.group(0)
            prefix_char = token[0]
            number = token[1:]
            repo, account = prefixes[prefix_char]
            return f"{token} [{repo_issue_gloss(repo, account, number)}]"
        return match.group(0)

    return pattern, gloss


def annotate(prompt: str, doc_text: str) -> str:
    built = build_annotator(doc_text)
    if built is None:
        return prompt
    pattern, gloss = built

    out_segments = []
    in_fence = False
    for line in prompt.splitlines(keepends=True):
        if FENCE.match(line):
            in_fence = not in_fence
            out_segments.append(line)
            continue
        if in_fence:
            out_segments.append(line)
            continue
        out_segments.append(pattern.sub(gloss, line))
    return "".join(out_segments)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        prompt = payload.get("prompt", "")
        if not isinstance(prompt, str) or not prompt:
            return
        doc_text = DOC_PATH.read_text(encoding="utf-8")
        expanded = annotate(prompt, doc_text)
    except Exception:
        # Fail open (AC2): never block the operator's message on a doc
        # or parse problem.
        return
    if expanded == prompt:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "Lane-shorthand expansion (harmonic-forge#383):\n" + expanded,
        }
    }))


if __name__ == "__main__":
    main()
