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
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from hook_identity import slot_env as _slot_env  # noqa: E402

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
    second, divergent parse of the doc. Account-only prefixes (no `/` in their
    repo column) are excluded -- nothing to fetch -- and so are repos on another
    account than vitalharmony (harmonic-forge#806)."""
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
            repo, account = prefixes.get(prefix_char, ("", ""))
            clean_repo = STRIP_MARKDOWN.sub("", repo).strip()
            if "/" not in clean_repo:
                continue
            # harmonic-forge#806: K, Y and D live on another account. The inline gloss still
            # fires, but no live fetch: this hook has no per-account identity, `Y2038` and
            # `D30` are ordinary prose, and a false positive would cost a doomed cross-account
            # `gh` call plus a misleading "fetch failed" block in the model's context.
            if not STRIP_MARKDOWN.sub("", account).strip().lower().startswith("vitalharmony"):
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


def _lane1_comment_filter():
    """`fetch_lane1_context.is_lane1_comment`, imported rather than copied
    (harmonic-forge#698 AC1: one filter, not two). None if it cannot load --
    the caller then withholds every comment, never shows them unfiltered."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
        from fetch_lane1_context import is_lane1_comment  # noqa: PLC0415

        return is_lane1_comment
    except Exception:  # noqa: BLE001
        return None


def _lane3_view(comments: list[dict]) -> tuple[list[dict], str]:
    """The comments a `LANE=3` session may see, plus a header line saying
    what was withheld (harmonic-forge#698).

    Lane 3 must not see Lane 2's comments before deriving its spec (R-0163,
    the hrse#824 exception), and this hook runs before any tool can -- so an
    unfiltered re-read voided two hrse specs in two minutes. The filter
    applies for the whole Lane 3 session, not only until its spec is posted
    (DJC 1): recognizing "Lane 3's spec exists" means classifying a comment
    Lane 3 writes by hand, and a false positive there re-opens an exposure
    that cannot be undone, while staying filtered costs one manual
    full-thread read once gate execution starts.
    """
    is_lane1 = _lane1_comment_filter()
    if is_lane1 is None:
        return [], (f"Lane 3 view: all {len(comments)} comment(s) withheld -- "
                    "the Lane-1 filter failed to load (harmonic-forge#698). "
                    "Use `fetch_lane1_context.py`.")
    kept = [c for c in comments if is_lane1(c.get("body") or "")]
    return kept, (f"Lane 3 view (harmonic-forge#698, R-0163): {len(kept)} "
                  f"Lane-1 comment(s) shown, {len(comments) - len(kept)} "
                  "withheld (Lane 2/3 posts). After your spec is posted, read "
                  "the full thread with `gh api repos/OWNER/REPO/issues/N/"
                  "comments --paginate`.")


def fetch_issue_context(repo: str, number: str, timeout: int = _FETCH_TIMEOUT_SECONDS,
                        lane: str | None = None) -> str | None:
    """Live `gh issue view` fetch of one issue's current title/state/body/
    comment list (harmonic-forge#397 AC2 -- "not cached"; bounded per
    harmonic-forge#399's preclose finding, see module-level caps above).
    Returns a formatted block, or None on any failure (network, auth,
    rate-limit, timeout, malformed JSON) -- the caller fails open on None
    rather than ever blocking the prompt on a fetch problem.

    `lane` defaults to the session's `LANE`; for Lane 3 only Lane-1 comments
    are shown (harmonic-forge#698, see `_lane3_view`)."""
    if lane is None:
        lane = os.environ.get("LANE", "")
    try:
        result = subprocess.run(
            [
                "gh", "issue", "view", number, "--repo", repo,
                "--json", "title,state,updatedAt,body,comments,url",
            ],
            capture_output=True, text=True, timeout=timeout, check=False,
            env=_slot_env(repo),  # harmonic-forge#804
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
    body = data.get("body") or "(empty)"
    lane_note = None
    if lane == "3":
        if "/pull/" in (data.get("url") or ""):
            # A PR number resolves here too, and a PR body is the
            # implementer's own report -- withheld whole, not filtered.
            body, comments = "(withheld in Lane 3 -- pull request body)", []
            lane_note = ("Lane 3 view (harmonic-forge#698, R-0163): this is a "
                         "pull request; its body and comments are withheld.")
        else:
            comments, lane_note = _lane3_view(comments)
    lines = [
        f"### {repo}#{number} -- {data.get('title') or '(no title)'} "
        f"[{data.get('state') or '?'}]",
        f"Updated: {data.get('updatedAt') or '?'} | Comments: {len(comments)}",
        *([lane_note] if lane_note else []),
        "",
        "Body:",
        _truncate(body, _BODY_CHAR_CAP),
    ]
    if comments:
        lines.append("")
        shown = comments[-_MAX_COMMENTS_SHOWN:]
        omitted = len(comments) - len(shown)
        if omitted > 0:
            # Lane 3 is pointed at the filtered fetch, never the full thread.
            history = (f"`fetch_lane1_context.py --repo {repo} --issue {number}`"
                       if lane == "3" else
                       f"`gh issue view {number} --repo {repo} --comments`")
            lines.append(
                f"Comments (most recent {len(shown)} of {len(comments)}, "
                f"{omitted} earlier omitted -- {history} for the full history):"
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


def directive_lines(prompt: str):
    """Yield `(index, line)` for every line that could carry a typed directive:
    outside fenced code blocks and not quote-prefixed (`_QUOTE_PREFIX_RE`).

    Shared by BATCH (`_scan_batch`) and `ALLOW LOOP` (`grant_loop_override.py`,
    harmonic-forge#659) so the two directives cannot disagree about what counts
    as quoting. A courtesy, not the trust boundary -- `_provenance_refusal` is.
    """
    # CommonMark fences (harmonic-forge#895): opened by >=3 backticks or
    # tildes after up to 3 spaces; closed only by the SAME character, at least
    # the opener's length, followed by nothing but whitespace.
    opener: str | None = None
    for index, line in enumerate(prompt.splitlines()):
        match = _FENCE_RE.match(line)
        if opener is None:
            if match:
                opener = match.group(1)
                continue
        else:
            if (match and match.group(1)[0] == opener[0]
                    and len(match.group(1)) >= len(opener) and not match.group(2).strip()):
                opener = None
            continue
        if _QUOTE_PREFIX_RE.match(line):
            continue
        yield index, line


#: Any indentation opens a fence, as before the CommonMark change: a quoted,
#: indented fence is still quotation (harmonic-forge#895 sticky-wicket).
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")


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

    for index, line in directive_lines(prompt):
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


def authorize_batch(prompt: str, state_path: Path | None = None,
                    session_id: str | None = None) -> str:
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

        # harmonic-forge#612: no `gh issue close` target -- BATCH grants
        # merges only. A batched issue closes by an explicit close command
        # after its merge (harmonic-forge#911), not a target here.
        # harmonic-forge#1145: only this operator-prompt path re-arms a key whose
        # every target was revoked (an ended IMC waiver revokes them).
        rearmed: list[str] = []
        fresh = top_up(keys, actions=["gh pr merge", "gh pr merge"],
                       state_path=state_path, session_id=session_id,
                       rearm_revoked=True, rearmed=rearmed)
    except Exception as exc:  # noqa: BLE001
        return (f"BATCH authorization FAILED for {', '.join(keys)}: {exc}. "
                "Every merge will prompt. Fix before relying on it.")
    extended = [k for k in keys if k not in fresh and k.upper() not in rearmed]
    parts = []
    if rearmed:
        parts.append(f"re-armed {', '.join(rearmed)} (every target revoked; 2 fresh merge "
                     f"slots each, {DEFAULT_TTL_HOURS:g}h TTL)")
    if fresh:
        parts.append(f"authorized {', '.join(fresh)} (2 merge slots each, "
                     f"{DEFAULT_TTL_HOURS:g}h TTL)")
    if extended:
        # Said explicitly: a re-mention EXTENDS, it does not reset. Replacing
        # would silently un-consume an already-spent single-use close.
        parts.append(f"extended {', '.join(extended)} (already live; "
                     "consumption and PR links preserved)")
    return "BATCH: " + "; ".join(parts) + " — written before this turn's " \
           "first tool call (harmonic-forge#502)."


#: `IMC` (harmonic-forge#895, R-0384): uppercase, a whole word. Lowercase is
#: noticed only so it can be refused out loud.
_IMC_RE = re.compile(r"\bIMC\b")
_IMC_ANY_CASE_RE = re.compile(r"\bimc\b", re.IGNORECASE)
_IMC_LABEL = "tooling-exception"


def _imc_module():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
    import imc_waiver  # noqa: PLC0415

    return imc_waiver


def read_issue_labels(repo: str, number: int,
                      timeout: int = _FETCH_TIMEOUT_SECONDS) -> tuple[list[str] | None, str]:
    """One `gh issue view <N> --repo <r> --json labels` read (the handoff's
    amended AC3 -- the shape `fetch_issue_context` already uses; the gh shim
    refuses `search/issues` from this caller). `(labels, "")`, or
    `(None, reason)` on any failure, a shim refusal included."""
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(number), "--repo", repo, "--json", "labels"],
            capture_output=True, text=True, timeout=timeout, check=False,
            env=_slot_env(repo),  # harmonic-forge#804
        )
    except subprocess.TimeoutExpired:
        return None, f"the label read timed out after {timeout}s"
    except OSError as exc:
        return None, f"the label read could not run ({exc})"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return None, ("the label read failed or was refused"
                      + (f" ({detail[0][:160]})" if detail else ""))
    try:
        data = json.loads(result.stdout)
        return [str(item.get("name")) for item in data.get("labels") or []], ""
    except (ValueError, AttributeError, TypeError):
        return None, "the label read returned unparseable output"


def authorize_imc(prompt: str, session_id: str | None, lane: str | None,
                  label_reader=None, now=None) -> str:
    """Record an IMC waiver for each key after `IMC` on the BATCH line
    (harmonic-forge#895). Mints NOTHING: the merge grant is `authorize_batch`'s,
    and nothing here touches the batch state, so no refusal here can affect it.

    Returns a receipt for `additionalContext`, or "" when the prompt carries no
    IMC token at all. Every refusal names its key and reason. Never raises.
    """
    try:
        return _authorize_imc(prompt, session_id, lane, label_reader or read_issue_labels, now)
    except Exception as exc:  # noqa: BLE001 - fail closed: nothing recorded
        return f"IMC REFUSED: the IMC check failed ({type(exc).__name__}: {exc}). No waiver recorded."


def _authorize_imc(prompt, session_id, lane, label_reader, now) -> str:
    directive = dict(directive_lines(prompt))
    lines = prompt.splitlines()
    scan = _scan_batch(prompt)
    batch_index = scan[0] if scan else None
    notes: list[str] = []
    for index, line in enumerate(lines):
        if index == batch_index or not _IMC_ANY_CASE_RE.search(line):
            continue
        if index not in directive:
            notes.append("IMC REFUSED: an IMC line inside a fenced block or blockquote is "
                         "quotation, not an instruction. No waiver recorded.")
        elif _IMC_RE.search(line):
            notes.append("IMC REFUSED: IMC needs BATCH first on the same line (`BATCH <keys> "
                         "IMC <keys>`). No waiver recorded.")
    if batch_index is None:
        return " ".join(dict.fromkeys(notes))
    line = lines[batch_index]
    imc = _IMC_RE.search(line)
    if imc is None:
        if _IMC_ANY_CASE_RE.search(line):
            notes.append("IMC REFUSED: `imc` must be uppercase IMC. No waiver recorded.")
        return " ".join(dict.fromkeys(notes))
    try:
        from batch_auth import REPO_PREFIXES  # noqa: PLC0415
        valid = {p.upper() for p in REPO_PREFIXES.values()}
    except Exception:  # noqa: BLE001
        valid = set()
    keys = [k.upper() for k in _BATCH_KEY_RE.findall(line[imc.end():])]
    keys = list(dict.fromkeys(k for k in keys if not valid or k[0] in valid))
    if not keys:
        notes.append("IMC REFUSED: no issue keys follow IMC on the BATCH line.")
        return " ".join(notes)
    refused: list[str] = []

    def refuse_all(reason: str) -> str:
        notes.append(f"IMC REFUSED for {', '.join(keys)}: {reason}. No waiver recorded; the "
                     "BATCH grant on the same line is unaffected.")
        return " ".join(notes)

    # The operator types IMC as the prompt's first line, unindented. Anywhere
    # else (an indented code block, a list-item fence, an HTML comment, a
    # pasted post below the first line) it is quotation, however the Markdown
    # nests (harmonic-forge#895 reforged pass 1).
    first = next((i for i, text in enumerate(lines) if text.strip()), None)
    if batch_index != first or not line.startswith("BATCH"):
        return refuse_all("IMC is honored only on the prompt's first line, typed unindented")
    provenance = _provenance_refusal(prompt, batch_index)
    if provenance is not None:
        return refuse_all(provenance)
    if lane != "1":
        return refuse_all(f"LANE is {lane!r}, and IMC is recorded only in a LANE=1 session")
    imc = _imc_module()
    recorded: list[str] = []
    for key in keys:
        target = imc.key_target(key)
        if target is None:
            refused.append(f"{key}: no repo maps to its prefix")
            continue
        repo, number = target
        if not repo.lower().startswith(imc.VITALHARMONY):
            refused.append(f"{key}: {repo} is a cross-account repo, and IMC fails closed there")
            continue
        labels, why = label_reader(repo, number)
        if labels is None:
            refused.append(f"{key}: {why}")
            continue
        if _IMC_LABEL not in labels:
            refused.append(f"{key}: {repo}#{number} does not carry `{_IMC_LABEL}`")
            continue
        moment = now or imc._now()
        imc.write_json_atomic(imc.hook_record_path(key), {
            "key": key, "repo": repo, "issue": number, "granted_at": imc._iso(moment),
            "session_id": session_id or "", "line": line, "hook_version": imc.HOOK_VERSION,
        })
        recorded.append(key)
    if recorded:
        notes.append(f"IMC: waiver hook record written for {', '.join(recorded)} "
                     f"({imc.TTL_HOURS:g}h to start the run; R-0384). Next: "
                     "`python3 tools/gh/imc_waiver.py record --key <K>` on a "
                     "`feat|fix/<N>-` branch.")
    for item in refused:
        notes.append(f"IMC REFUSED for {item}. No waiver recorded; the BATCH grant is unaffected.")
    return " ".join(notes)


#: `FORCE <KEY>: <reason>` (harmonic-forge#1169): uppercase, unindented, on the
#: prompt's first line. A lowercase or misplaced FORCE is refused out loud.
#: harmonic-forge#1174: `FORCE <KEY> [reforge <sha> | cluster]: <reason>`. The
#: tokens between the key and the colon are captured loosely and validated in
#: `_force_mode`, so a malformed mode gets its own refusal, never the
#: "first line" message.
_FORCE_RE = re.compile(r"^FORCE[ \t]+([A-Za-z]\d{1,6})((?:[ \t]+[^ \t:]+)*?)[ \t]*:[ \t]*(.*)$")
_FORCE_SHA_RE = re.compile(r"^[0-9a-f]{12,40}$")


def _force_mode(tokens: list[str]) -> tuple[str, str | None] | str:
    """`(mode, sha_prefix)` for a FORCE line's mode tokens, or a refusal reason."""
    if not tokens:
        return "plain", None
    word, rest = tokens[0], tokens[1:]
    if word == "reforge":
        if len(rest) != 1:
            return "a reforge FORCE names the head it approves: `FORCE <KEY> reforge <sha>: <reason>`"
        if not _FORCE_SHA_RE.match(rest[0].lower()):
            return f"{rest[0]!r} is not a 12-40 character hex commit SHA"
        return "reforge", rest[0].lower()
    if word == "cluster":
        if rest:
            return "a cluster FORCE takes no SHA: `FORCE <KEY> cluster: <reason>`"
        return "cluster", None
    return f"unknown FORCE mode {word!r}: the modes are `reforge <sha>` and `cluster` (lowercase)"
_FORCE_ANY_RE = re.compile(r"^[ \t>]*force[ \t]+[A-Za-z]\d{1,6}\b", re.IGNORECASE)


def _force_module():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
    import force_grant  # noqa: PLC0415

    return force_grant


def authorize_force(prompt: str, session_id: str | None, lane: str | None, now=None) -> str:
    """Write a PENDING FORCE grant (harmonic-forge#1169 AC1). Nothing can use it
    until `force_grant.py record` posts and refetches it (AC2). Returns a
    receipt for `additionalContext`, or "" when no line looks like FORCE.
    Every refusal says why and writes nothing. Never raises."""
    try:
        return _authorize_force(prompt, session_id, lane, now)
    except Exception as exc:  # noqa: BLE001 - fail closed: nothing written
        return f"FORCE REFUSED: the FORCE check failed ({type(exc).__name__}: {exc}). No grant written."


def _authorize_force(prompt, session_id, lane, now) -> str:
    lines = prompt.splitlines()
    if not any(_FORCE_ANY_RE.match(line) for line in lines):
        return ""
    # The same placement rule IMC is honored by: the prompt's first line, typed
    # unindented, uppercase. Anywhere else it is quotation (harmonic-forge#895).
    first = next((i for i, text in enumerate(lines) if text.strip()), None)
    match = _FORCE_RE.match(lines[first]) if first is not None else None
    if match is None:
        return ("FORCE REFUSED: FORCE is honored only as the prompt's first line, typed "
                "unindented and uppercase, as `FORCE <KEY>: <reason>`. No grant written.")
    if first not in dict(directive_lines(prompt)):
        return "FORCE REFUSED: a FORCE line inside a fenced block or blockquote is quotation. No grant written."
    key, reason = match.group(1).upper(), match.group(3).strip()
    mode = _force_mode(match.group(2).split())
    if isinstance(mode, str):
        return f"FORCE REFUSED for {key}: {mode}. No grant written (harmonic-forge#1174)."
    mode, sha_prefix = mode
    if not reason:
        return f"FORCE REFUSED for {key}: the reason is empty. No grant written."
    provenance = _provenance_refusal(prompt, first)
    if provenance is not None:
        return f"FORCE REFUSED for {key}: {provenance}. No grant written."
    if lane != "1":
        return f"FORCE REFUSED for {key}: LANE is {lane!r}, and FORCE is recorded only in a LANE=1 session."
    imc = _imc_module()
    target = imc.key_target(key)
    if target is None:
        return f"FORCE REFUSED for {key}: no repo maps to its prefix. No grant written."
    repo, number = target
    if not repo.lower().startswith(imc.VITALHARMONY):
        return (f"FORCE REFUSED for {key}: {repo} is a cross-account repo, and FORCE fails closed "
                "there. No grant written.")
    grants = _force_module()
    head = None
    if mode != "reforge":
        # A reforge binds the head the operator typed; it may start before
        # that head has any receipt (harmonic-forge#1174).
        head = grants.covered_head(repo, number)
        if not head:
            return (f"FORCE REFUSED for {key}: {repo}#{number} has no preclose receipt, so there is "
                    "no head to bind the force to. No grant written.")
    previous = grants.read_grant(key)
    grant = grants.write_pending(key, repo, number, head, reason, session_id, lines[first], now,
                                 mode=mode, sha_prefix=sha_prefix)
    bound = (head or sha_prefix or "")[:12]
    replaced = ""
    if previous:
        replaced = (f" It replaces the {previous.get('mode') or 'plain'} grant at "
                    f"{str(previous.get('sha') or previous.get('sha_prefix') or '?')[:12]}.")
    return (f"FORCE: pending {mode} grant written for {key} at {bound} (single use, until "
            f"{grant['expires_at']}).{replaced} Next: `python3 tools/gh/force_grant.py record --key "
            f"{key}`, then the non-force preflight and the --force run (harmonic-forge#1169, #1174).")


#: harmonic-forge#1188 AC7/AC8: the fixed operator->lane trigger vocabulary
#: (rules/lane-shorthand.md "Operator -> lane triggers"), at a line start. The
#: issue comes only from the trigger's own key, never from other prompt text.
_ATTRIB_TRIGGER_RE = re.compile(
    r"^[ \t]*(Plan|Implement|Fix|Spec|AE|close|Blueprint)[ \t]+([A-Za-z])(\d{1,6})\b")
_ATTRIB_TRIGGER_LANE = {"Plan": "2", "Implement": "2", "Fix": "2", "Spec": "3",
                        "AE": "1", "close": "1", "Blueprint": "1"}
#: harmonic-forge#1188 amendment 1: a belt event names its repo, issue and lane
#: in fixed fields (`watch_lane_posts.py`'s `<owner>/<repo>#<n> queued-for-l<N>`),
#: so belt-driven work gets its own sidecar line instead of inheriting the
#: previous trigger's issue. Structured fields only, never free text.
_ATTRIB_BELT_RE = re.compile(
    r"^[ \t]*(?:<event>)?[ \t]*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d{1,6})[ \t]+queued-for-l([123])\b")
#: Belt and harness-injected prompts are not operator attention (AC11).
#: "This session is being continued" is the harness's post-compaction prompt.
_NON_HUMAN_PREFIXES = ("<task-notification", "<system-reminder", "<local-command", "<command-",
                       "This session is being continued")
#: A headless `claude -p` session (te-runner agent, cross-family reviewer,
#: research agent) has no operator at its prompt; transcripts show it as
#: `entrypoint` sdk-*, against `cli` for an interactive session.
_HEADLESS_ENTRYPOINT = "sdk-"
_TRANSCRIPT_TAIL_BYTES = 262144


def _telemetry_attrib():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telemetry"))
    import attrib  # noqa: E402
    return attrib


def record_attribution(prompt: str, session_id, lane, doc_text: str, transcript_path=None) -> int:
    """Append one sidecar line per recognized lane trigger, with the transcript
    holding the prompt as the audit's evidence (AC10). Returns lines written."""
    attrib = _telemetry_attrib()
    written = 0
    stripped = prompt.lstrip()
    # 1188 preclose pass 1: a belt line counts only inside a belt notification,
    # and an operator trigger only as a human prompt's first non-blank line
    # (R-0117's first-line rule). Quoted, pasted or injected text never does.
    if stripped.startswith("<task-notification"):
        for line in prompt.splitlines():
            belt = _ATTRIB_BELT_RE.match(line)
            if belt and attrib.append(session_id, f"{belt.group(1)}#{belt.group(2)}",
                                      lane or belt.group(3), None, "belt:queued",
                                      transcript_path=transcript_path):
                written += 1
        return written
    if stripped.startswith(_NON_HUMAN_PREFIXES):
        return 0
    first = next((line for line in prompt.splitlines() if line.strip()), "")
    m = _ATTRIB_TRIGGER_RE.match(first)
    if not m:
        return 0
    prefixes = parse_repo_prefixes(doc_text)
    repo = STRIP_MARKDOWN.sub("", prefixes.get(m.group(2).upper(), ("", ""))[0]).strip()
    if "/" in repo and attrib.append(session_id, f"{repo}#{m.group(3)}",
                                     lane or _ATTRIB_TRIGGER_LANE[m.group(1)], None,
                                     f"shorthand:{m.group(1)}", transcript_path=transcript_path):
        written += 1
    return written


def _last_lead(transcript_path) -> tuple:
    """(ts, bytes) of the last assistant text in the transcript tail -- sizes
    and times only; no text is kept."""
    path = Path(str(transcript_path or ""))
    if not path.is_file():
        return None, 0
    with path.open("rb") as handle:
        handle.seek(max(path.stat().st_size - _TRANSCRIPT_TAIL_BYTES, 0))
        tail = handle.read().decode("utf-8", errors="replace").splitlines()
    for raw in reversed(tail):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        size = sum(len(str(c.get("text", "")).encode("utf-8")) for c in content or []
                   if isinstance(c, dict) and c.get("type") == "text") if isinstance(content, list) else 0
        if size:
            return rec.get("timestamp"), size
    return None, 0


def record_attention(prompt: str, payload: dict) -> bool:
    """One operator-attention line per human prompt (AC11, unvalidated)."""
    if prompt.lstrip().startswith(_NON_HUMAN_PREFIXES):
        return False
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith(_HEADLESS_ENTRYPOINT):
        return False
    session_id = str(payload.get("session_id") or "")
    if not re.match(r"^[A-Za-z0-9._-]{1,128}$", session_id):
        return False
    now = datetime.now(timezone.utc)
    last_ts, lead = _last_lead(payload.get("transcript_path"))
    gap = 0.0
    if isinstance(last_ts, str):
        try:
            gap = max((now - datetime.fromisoformat(last_ts.replace("Z", "+00:00"))).total_seconds(), 0.0)
        except ValueError:
            gap = 0.0
    if not os.environ.get("HARMONIC_FORGE_ATTENTION_DIR") and _telemetry_attrib().under_test():
        return False  # a test never writes the operator's real attention lines
    base = Path(os.environ.get("HARMONIC_FORGE_ATTENTION_DIR")
                or Path.home() / ".claude" / "state" / "attention").expanduser()
    base.mkdir(parents=True, exist_ok=True)
    line = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "session_id": session_id,
            "gap_s": round(gap, 1), "lead_bytes": lead}
    with (base / f"{session_id}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
    return True


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
        # harmonic-forge#1063: the grant records this prompt's session, so
        # block_batch_stop holds only the session that authorized it.
        batch_receipt = authorize_batch(prompt, session_id=payload.get("session_id"))
    except Exception:
        batch_receipt = ""
    # harmonic-forge#895: after BATCH, never instead of it. IMC reads its
    # labels outside the R-0212 re-read budget below.
    try:
        imc_receipt = authorize_imc(prompt, payload.get("session_id"), os.environ.get("LANE"))
    except Exception:
        imc_receipt = ""
    if imc_receipt:
        batch_receipt = " ".join(p for p in (batch_receipt, imc_receipt) if p)
    # harmonic-forge#1169: FORCE writes only a pending grant; it mints nothing
    # BATCH or IMC own, so it sits beside them and never replaces either.
    try:
        force_receipt = authorize_force(prompt, payload.get("session_id"), os.environ.get("LANE"))
    except Exception:
        force_receipt = ""
    if force_receipt:
        batch_receipt = " ".join(p for p in (batch_receipt, force_receipt) if p)
    # harmonic-forge#1188 AC8: attribution and attention ride on this hook and
    # never fail it; each sits in its own try.
    try:
        record_attribution(prompt, payload.get("session_id"), os.environ.get("LANE"),
                           DOC_PATH.read_text(encoding="utf-8"), payload.get("transcript_path"))
    except Exception:
        pass
    try:
        record_attention(prompt, payload)
    except Exception:
        pass
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
