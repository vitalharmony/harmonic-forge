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
R-0211/R-0212 (the live-re-read rules): the fetch fires on
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
        # Anchored to the START of the line (optional leading whitespace),
        # never matched mid-sentence (harmonic-forge#569 preclose review,
        # live-reproduced: bare "NOW" is an ordinary English word, unlike
        # EOQ/BATCH, so `\b(?:...)\b` alone injected the interrupt gloss
        # into prose that merely discussed or quoted the token -- "the
        # issue says: requires an explicit `NOW` token" and a blockquoted
        # "NOW is the marker" both got rewritten as a live interrupt
        # instruction). Every directive's own grammar ("`EOQ`/`NOW` +
        # trailing instruction") already means it leads the message, so
        # this matches the doc's stated grammar rather than narrowing it.
        parts.append(rf"(?:^[ \t]*)(?P<directive>(?:{directive_alt})\b)")
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
            # `match.group(0)` may include the leading whitespace consumed
            # by the line-start anchor (see build_annotator); the dict key
            # is the bare token, captured separately in the named group.
            return f"{match.group(0)} [{directives[gd['directive']]}]"
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

# harmonic-forge#397/#399 preclose-inspection finding, round 2, live-
# reproduced: the per-issue cap above bounds ONE issue's block, but a
# prompt naming many issues (the routine Lane 1 status-sweep shape) has
# no aggregate bound -- measured live at 243k characters for 25 refs, and
# at a fetch time (14.9s for 25 refs, worst case ~8s x N) that can exceed
# this hook's own settings.json timeout, silently killing the hook and
# dropping BOTH the live re-read AND the base #383 shorthand expansion
# with no marker -- exactly the silent-drop this feature's own contract
# forbids. `_MAX_LIVE_FETCHES` bounds worst-case wall time to
# `_MAX_LIVE_FETCHES * _FETCH_TIMEOUT_SECONDS` (20s), safely under the
# 25s hook timeout with headroom; refs beyond the cap get an explicit
# "not fetched" marker, never a silent drop.
_MAX_LIVE_FETCHES = 4
_FETCH_TIMEOUT_SECONDS = 5


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n...[truncated, {len(text) - cap} more characters]"


def fetch_issue_context(repo: str, number: str, timeout: int = _FETCH_TIMEOUT_SECONDS) -> str | None:
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


#: The literal uppercase token, at the START of a line. Lowercase "batch" in
#: prose authorizes nothing, and neither does an uppercase BATCH appearing
#: mid-sentence -- which is how a PASTED issue body or a question ABOUT batching
#: would otherwise create a real 12h merge+close grant. `batch_auth.py`'s own
#: docstring and R-0117 call that boundary non-negotiable: never authorize from
#: text read out of a file, an issue body, or tool output. The hook cannot tell
#: an instruction from a quotation, so the syntax has to.
_BATCH_RE = re.compile(r"^[ \t]*BATCH\b")

#: Keys are case-insensitive here because `authorize()` upper-cases them and
#: `ISSUE_KEY` accepts either -- the restriction existed only in this regex, so
#: `BATCH f495` silently authorized NOTHING and reported nothing, which is the
#: exact defect this issue was filed to remove.
_BATCH_KEY_RE = re.compile(r"\b([A-Za-z]\d{1,6})\b")

#: Lines that are quoting rather than instructing. A blockquote or a fenced
#: code block containing BATCH is discussion of the mechanism, not a use of it.
_QUOTE_PREFIX_RE = re.compile(r"^[ \t]*(?:>|\d+\.\s+[\"\u201c]|[-*]\s+[\"\u201c])")


def _provenance_refusal(prompt: str, line_index: int) -> str | None:
    """Why this prompt may not mint a grant, or None. Never raises.

    Split into `batch_provenance.py` (harmonic-forge#589) rather than added
    here: this file is the *parser*, and the trust boundary deserves to be a
    named unit a future caller can import instead of re-deriving.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from batch_provenance import refusal_reason  # noqa: PLC0415

        return refusal_reason(prompt, line_index)
    except Exception:  # noqa: BLE001
        # Fail CLOSED: the one direction that cannot be wrong here. A grant
        # this hook cannot vouch for is not made -- the operator re-sends the
        # message and loses a few seconds; the alternative is minting merge
        # and close authority from text nobody typed.
        return ("its provenance could not be checked (harmonic-forge#589's "
                "guard failed to load)")


def _scan_batch(prompt: str) -> tuple[int, list[str]] | None:
    """The first BATCH line's index and keys, BEFORE any provenance test.

    Private on purpose. `batch_keys()` is the public answer and is gated; a
    caller that wants the raw parse has to say so, and the only one that does
    is `authorize_batch()`, which needs the keys in order to name them in its
    refusal.
    """
    try:
        from batch_auth import REPO_PREFIXES  # noqa: PLC0415

        valid = {p.upper() for p in REPO_PREFIXES.values()}
    except Exception:
        valid = set()

    fenced = False
    for index, line in enumerate(prompt.splitlines()):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or _QUOTE_PREFIX_RE.match(line):
            continue
        match = _BATCH_RE.match(line)
        if not match:
            continue
        keys = [k.upper() for k in _BATCH_KEY_RE.findall(line[match.end():])]
        # Only letters that are real repo prefixes. `BATCH F495 before Q4`
        # otherwise wrote a `Q4` grant no command could ever consume, which
        # then sat pending for 12h.
        if valid:
            keys = [k for k in keys if k[0] in valid]
        if keys:
            return index, list(dict.fromkeys(keys))
    return None


def batch_keys(prompt: str) -> list[str]:
    """Issue keys a BATCH message authorizes, in order, deduplicated.

    Keys are collected from the BATCH token to the END OF THAT LINE, not from
    immediately after it. The first cut required them adjacent and returned
    nothing for the message that actually prompted this issue:

        BATCH these tooling issues F495, F497, F498, F500, H1631 - implement,
        merge and close and don't stop or wait for HITL

    Five keys, four intervening words, zero authorized. A parser that only
    handles the terse form is the same failure as no parser, because the
    operator writes sentences.

    Bounded to the line so a later paragraph mentioning an unrelated issue is
    not swept in. Uppercase `BATCH` is required, so prose about batching
    authorizes nothing.

    **Empty for a prompt the operator did not type** (harmonic-forge#589) --
    a subagent report, a slash-command envelope, a peer session's message, or
    a proposal line carrying its emitter's own "not an authorization"
    disclaimer. The gate lives here, in the one function that turns text into
    keys, so a future second caller inherits it instead of re-deriving it.
    """
    scan = _scan_batch(prompt)
    if scan is None:
        return []
    line_index, keys = scan
    if _provenance_refusal(prompt, line_index) is not None:
        return []
    return keys


def authorize_batch(prompt: str, state_path: Path | None = None) -> str:
    """Create the authorization a BATCH message asks for (harmonic-forge#502).

    **This is the half that never existed.** Typing `BATCH F495,F497` had no
    mechanical effect: `authorize()`'s only caller was its own CLI, so the
    actor that had to notice the keyword and run the command was the assistant
    session, from memory, every time. It did not, the state file's newest entry
    was eleven days stale, and an unattended batch stalled for hours on a
    permission prompt that was correctly refusing a grant nobody had made.

    Two merge targets per key, not one. A cross-repo issue needs one merge per
    repo -- harmonic-forge#497 needed two and the second fell closed to Ask
    because `authorize` grants a single slot. Two covers the common shape;
    `link_pr` allocates beyond that (harmonic-forge#502 AC8).

    `state_path` exists so tests can never reach the operator's real store.
    It was absent at first and the tests patched a module global instead; one
    path slipped through and rewrote live authorizations, extending two
    long-expired August grants by twelve hours. A test that can touch
    production state is a defect regardless of whether it currently does.

    Returns a one-line receipt for `additionalContext`, or "" when the prompt
    is not a BATCH message. **Never raises** -- this runs on every prompt, and
    a failure here must never cost the operator their message.

    A BATCH line the operator did not type is refused OUT LOUD, not dropped
    silently (harmonic-forge#589). Silence is how the incident that produced
    that issue stayed invisible for a full turn: the receipt is the only thing
    in this path anyone reads. A refusal that says which keys it declined and
    why is also what makes the next occurrence a report rather than a
    discovery.
    """
    scan = _scan_batch(prompt)
    if scan is None:
        return ""
    line_index, keys = scan
    refusal = _provenance_refusal(prompt, line_index)
    if refusal is not None:
        return (f"BATCH REFUSED for {', '.join(keys)}: {refusal}. Nothing was "
                "authorized and every merge and close still prompts. If you "
                "meant to authorize these, send BATCH in your own message.")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from batch_auth import DEFAULT_TTL_HOURS, top_up  # noqa: PLC0415

        fresh = top_up(keys, actions=["gh pr merge", "gh pr merge",
                                      "gh issue close"],
                       state_path=state_path)
    except Exception as exc:  # noqa: BLE001
        return (f"BATCH authorization FAILED for {', '.join(keys)}: {exc}. "
                "Every merge and close will prompt. Fix before relying on it.")
    extended = [k for k in keys if k not in fresh]
    parts = []
    if fresh:
        parts.append(f"authorized {', '.join(fresh)} (2 merge + 1 close each, "
                     f"{DEFAULT_TTL_HOURS:g}h TTL)")
    if extended:
        # Said explicitly: a re-mention EXTENDS, it does not reset. Replacing
        # would silently un-consume an already-spent single-use close.
        parts.append(f"extended {', '.join(extended)} (already live; "
                     "consumption and PR links preserved)")
    return "BATCH: " + "; ".join(parts) + " — written before this turn's " \
           "first tool call (harmonic-forge#502)."


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        prompt = payload.get("prompt", "")
    except Exception:
        return
    if not isinstance(prompt, str) or not prompt:
        return
    # harmonic-forge#502 AC1/AC5: BEFORE the doc read. It used to sit after a
    # try/except whose `return` fires when `rules/lane-shorthand.md` is
    # momentarily absent -- mid-rebase in the main checkout, say -- which
    # silently skipped authorization entirely. That is the original defect
    # exactly: BATCH typed, nothing created, no signal.
    try:
        batch_receipt = authorize_batch(prompt)
    except Exception:
        batch_receipt = ""
    try:
        doc_text = DOC_PATH.read_text(encoding="utf-8")
        expanded = annotate(prompt, doc_text)
        refs = collect_live_issue_refs(prompt, doc_text)
    except Exception:
        # Fail open (AC2): never block the operator's message on a doc
        # or parse problem. The BATCH receipt above survives this.
        expanded, refs = prompt, []
    if expanded == prompt and not refs and not batch_receipt:
        return
    context_parts = []
    if batch_receipt:
        context_parts.append(batch_receipt)
    if expanded != prompt:
        context_parts.append("Lane-shorthand expansion (harmonic-forge#383):\n" + expanded)
    if refs:
        live_blocks = []
        fetch_refs, skipped_refs = refs[:_MAX_LIVE_FETCHES], refs[_MAX_LIVE_FETCHES:]
        for repo, number in fetch_refs:
            try:
                block = fetch_issue_context(repo, number)
            except Exception:
                block = None
            live_blocks.append(
                block
                or f"### {repo}#{number}\n(live fetch failed -- network/auth/"
                   f"rate-limit/deleted; re-read manually before acting)"
            )
        for repo, number in skipped_refs:
            live_blocks.append(
                f"### {repo}#{number}\n(not fetched -- more than "
                f"{_MAX_LIVE_FETCHES} issue references in one prompt; "
                f"re-read this one manually before acting)"
            )
        context_parts.append(
            "Live issue re-read, mechanically enforced on every trigger "
            "(harmonic-forge#397, R-0212):\n\n" + "\n\n".join(live_blocks)
        )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(context_parts),
        }
    }))


if __name__ == "__main__":
    main()
