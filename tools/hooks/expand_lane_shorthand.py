#!/usr/bin/env python3
"""UserPromptSubmit hook: expand lane-shorthand tokens inline before the
prompt reaches the model (harmonic-forge#383).

The token set is parsed from rules/lane-shorthand.md at hook runtime --
never re-declared here -- so a new row in the doc expands with no hook
edit (AC2). A malformed or unreadable doc fails open: the prompt passes
through byte-for-byte rather than blocking the operator's message. A
single malformed row (e.g. an empty Account cell) must not poison
expansion of every OTHER, well-formed token in the same prompt --
repo_issue_gloss() below is deliberately defensive rather than relying
on the outer fail-open try/except, which would otherwise blank the
whole prompt over one bad row (preclose review, correctness lens).

Expansion is additive annotation only (AC3): each recognized token gets
a bracketed gloss appended immediately after it; the operator's literal
text is never rewritten or dropped. Matches inside fenced code blocks are
left alone (AC4).

Discrimination (AC4), and its known limit: the lane-status and
EOQ/BATCH branches match only whole tokens read verbatim from the doc's
own tables/headings, so an ordinary English word never accidentally
matches unless it is actually listed there. The repo-prefix branch is
different -- it is a *generated* pattern (single letter + digits), not a
closed vocabulary, so it cannot rely on doc membership alone; single
capital-letter tokens (H, F, P, O, I, K) collide with common prose (HTML
heading levels, priority language, function keys). Every currently-active
issue number in this system is 2+ digits (see rules/lane-shorthand.md's
own examples: H26, H767, F316, F383, K42, H1304) with single-digit
numbers belonging to issues long since closed, so the repo-prefix pattern
requires 2+ digits -- this removes the H1/H2/F5/P0/P1/O2/I5 class of
false positive while still matching real usage. It is a mitigation, not
a proof: a 2+-digit collision (e.g. "H26 bus route") remains possible in
principle. Flagged as a residual, accepted risk rather than solved.

Run: reads the UserPromptSubmit JSON payload on stdin, writes
{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
"additionalContext": "..."}} or, on any parse/doc failure, exits 0 with
no output (pass-through).

Live issue re-read (harmonic-forge#397): whenever the prompt contains a
real (`/`-bearing) repo-prefixed issue reference -- the same tokens the
inline gloss above already recognizes -- this hook also live-fetches
that issue's current body and full comment list via `gh issue view` and
appends them to additionalContext. This is the mechanism half of
`feedback_always_reread_issue_on_every_trigger`: the fetch fires on
every match, unconditionally, regardless of whether the surrounding
prompt looks like a fresh "Implement #N" or a bare continuation
("continue", "unblocked") -- the regex match is on the token, not on
the verb around it, so both shapes are covered identically by
construction (AC3). No caching: a stale re-read defeats the point.
K/P-style account-only prefixes (no real `owner/repo` shorthand) are
never fetched -- there is nothing to `gh issue view`. A fetch failure
(network, auth, rate-limit, deleted issue) fails open with an explicit
"could not fetch" marker in the injected context, never a silent drop
and never a blocked prompt.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

DOC_PATH = Path(__file__).resolve().parent.parent.parent / "rules" / "lane-shorthand.md"

TABLE_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|")
FENCE = re.compile(r"^\s*```")
LANE_TEMPLATE = re.compile(r"^L<N>([A-Z])$")
STRIP_MARKDOWN = re.compile(r"\*\*|`")


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
    separator rows (their first cell isn't backtick-quoted).

    The doc's grammar is "L + lane digit + one letter" -- most rows are
    concrete tokens (L2D, L3F, ...), but the BLOCKED row is written as
    the metavariable template `L<N>B` (harmonic-forge#383 preclose
    review, all five lenses independently: a literal-string parse of
    that key can never match a real prompt, silently dropping the one
    token the doc calls load-bearing). A template row expands into its
    three concrete instances (L1B, L2B, L3B), all sharing the row's
    meaning text.
    """
    tokens: dict[str, str] = {}
    for line in _section(text.splitlines(), "Lane status tokens"):
        m = TABLE_ROW.match(line)
        if not m:
            continue
        key, meaning = m.group(1), m.group(2)
        template = LANE_TEMPLATE.match(key)
        if template:
            letter = template.group(1)
            for n in (1, 2, 3):
                tokens[f"L{n}{letter}"] = meaning
        else:
            tokens[key] = meaning
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
    """`## `EOQ`` / `## `BATCH`` style headings -> {TOKEN: full Meaning
    paragraph}. Both are prose grammar, not table rows, so they're parsed
    separately from parse_lane_tokens().

    Captures the WHOLE paragraph after "Meaning:" (up to the next blank
    line or heading), not just the first physical line -- the doc hard-
    wraps at ~72 columns, and a single-line, non-DOTALL capture truncates
    mid-sentence (harmonic-forge#383 preclose review, 3 of 5 lenses,
    live-reproduced: EOQ ended on a dangling "It", BATCH was cut before
    naming what it authorizes). Wrapped lines are collapsed to one
    paragraph; markdown emphasis markers are stripped.
    """
    out: dict[str, str] = {}
    for m in re.finditer(r"^## `([A-Z]+)`.*$", text, re.MULTILINE):
        token = m.group(1)
        start = m.end()
        next_heading = re.search(r"^## ", text[start:], re.MULTILINE)
        section = text[start : start + next_heading.start()] if next_heading else text[start:]
        meaning_m = re.search(r"Meaning:\s*(.+?)(?:\n\s*\n|\Z)", section, re.DOTALL)
        if meaning_m:
            collapsed = re.sub(r"\s+", " ", meaning_m.group(1)).strip()
            out[token] = STRIP_MARKDOWN.sub("", collapsed)
    return out


def repo_issue_gloss(repo: str, account: str, number: str) -> str:
    """Never raises: a malformed row (e.g. an empty Account cell) must
    degrade to a plain fallback for THAT token, not crash the regex
    substitution mid-prompt and silently blank every other token's
    expansion too (harmonic-forge#383 preclose review, correctness lens,
    live-reproduced IndexError). Renders the full account text rather
    than truncating to its first word -- the doc's own K/P caveats
    ("separate account, separate credentials"; "repo does not yet exist")
    are the load-bearing part of those two rows and must not be dropped.
    """
    clean_repo = STRIP_MARKDOWN.sub("", repo).strip()
    if "/" in clean_repo:
        return f"{clean_repo}#{number}"
    clean_account = STRIP_MARKDOWN.sub("", account).strip() or "unknown account"
    return f"{clean_repo} issue #{number} (account: {clean_account})"


def build_annotator(text: str):
    lane_tokens = parse_lane_tokens(text)
    prefixes = {p: rc for p, rc in parse_repo_prefixes(text).items() if p != "L"}
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
        # 2+ digits: see module docstring "Discrimination (AC4)" -- removes
        # the H1/H2/F5/P0/P1/O2/I5 single-digit prose-collision class.
        parts.append(rf"(?P<repo>\b(?:{prefix_alt})\d{{2,}}\b)")
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


def collect_live_issue_refs(prompt: str, doc_text: str) -> list[tuple[str, str]]:
    """Distinct (repo, number) pairs for every real (`/`-bearing)
    repo-prefixed issue reference in the prompt, outside fenced code
    blocks -- the set this hook must live-fetch for (harmonic-forge#397).
    Reuses build_annotator()'s own pattern so the fetch set is always
    exactly the set of tokens the inline gloss already recognizes; no
    second, divergent parse of the doc. K/P-style account-only prefixes
    (no `/` in their repo column) are excluded -- nothing to fetch."""
    built = build_annotator(doc_text)
    if built is None:
        return []
    pattern, _gloss = built
    prefixes = {p: rc for p, rc in parse_repo_prefixes(doc_text).items() if p != "L"}
    seen: list[tuple[str, str]] = []
    in_fence = False
    for line in prompt.splitlines(keepends=True):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in pattern.finditer(line):
            if not match.groupdict().get("repo"):
                continue
            token = match.group(0)
            prefix_char, number = token[0], token[1:]
            repo, _account = prefixes.get(prefix_char, ("", ""))
            clean_repo = STRIP_MARKDOWN.sub("", repo).strip()
            if "/" not in clean_repo:
                continue
            pair = (clean_repo, number)
            if pair not in seen:
                seen.append(pair)
    return seen


# harmonic-forge#397/#399 preclose-inspection finding, live-reproduced:
# an unbounded fetch measured ~93k characters (~23k tokens) for one real
# issue, re-injected in full on every continuation-shaped trigger --
# exactly the shape ("continue H1252", "H1252 unblocked") this feature
# exists to serve. These caps bound the injected block per issue while
# keeping it useful for "did I already see this" -- most-recent comments
# matter more than oldest for a mechanical re-read, so truncation drops
# from the front (oldest), not the back.
_BODY_CHAR_CAP = 4000
_MAX_COMMENTS_SHOWN = 8
_COMMENT_CHAR_CAP = 1500


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n...[truncated, {len(text) - cap} more characters]"


def fetch_issue_context(repo: str, number: str, timeout: int = 8) -> str | None:
    """Live `gh issue view` fetch of one issue's current title/state/body/
    comment list (harmonic-forge#397 AC2 -- "not cached"; bounded per
    harmonic-forge#399's preclose finding, see module-level caps above).
    Returns a formatted block, or None on any failure (network, auth,
    rate-limit, timeout, malformed JSON) -- the caller fails open on None
    rather than ever blocking the prompt on a fetch problem."""
    try:
        result = subprocess.run(
            [
                "gh", "issue", "view", number, "--repo", repo,
                "--json", "title,state,updatedAt,body,comments",
            ],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    comments = data.get("comments") or []
    lines = [
        f"### {repo}#{number} -- {data.get('title') or '(no title)'} "
        f"[{data.get('state') or '?'}]",
        f"Updated: {data.get('updatedAt') or '?'} | Comments: {len(comments)}",
        "",
        "Body:",
        _truncate(data.get("body") or "(empty)", _BODY_CHAR_CAP),
    ]
    if comments:
        lines.append("")
        shown = comments[-_MAX_COMMENTS_SHOWN:]
        omitted = len(comments) - len(shown)
        if omitted > 0:
            lines.append(
                f"Comments (most recent {len(shown)} of {len(comments)}, "
                f"{omitted} earlier omitted -- `gh issue view {number} "
                f"--repo {repo} --comments` for the full history):"
            )
        else:
            lines.append("Comments:")
        for c in shown:
            author = (c.get("author") or {}).get("login") or "unknown"
            lines.append(f"--- {author} @ {c.get('createdAt') or '?'} ---")
            lines.append(_truncate(c.get("body") or "(empty)", _COMMENT_CHAR_CAP))
    return "\n".join(lines)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        prompt = payload.get("prompt", "")
        if not isinstance(prompt, str) or not prompt:
            return
        doc_text = DOC_PATH.read_text(encoding="utf-8")
        expanded = annotate(prompt, doc_text)
        refs = collect_live_issue_refs(prompt, doc_text)
    except Exception:
        # Fail open (AC2): never block the operator's message on a doc
        # or parse problem.
        return
    if expanded == prompt and not refs:
        return
    context_parts = []
    if expanded != prompt:
        context_parts.append("Lane-shorthand expansion (harmonic-forge#383):\n" + expanded)
    if refs:
        live_blocks = []
        for repo, number in refs:
            try:
                block = fetch_issue_context(repo, number)
            except Exception:
                block = None
            live_blocks.append(
                block
                or f"### {repo}#{number}\n(live fetch failed -- network/auth/"
                   f"rate-limit/deleted; re-read manually before acting)"
            )
        context_parts.append(
            "Live issue re-read, mechanically enforced on every trigger "
            "(harmonic-forge#397, feedback_always_reread_issue_on_every_"
            "trigger):\n\n" + "\n\n".join(live_blocks)
        )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(context_parts),
        }
    }))


if __name__ == "__main__":
    main()
