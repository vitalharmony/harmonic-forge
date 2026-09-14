#!/usr/bin/env python3
"""UserPromptSubmit hook: check the session model against every referenced
issue's Tier before a lane starts work (harmonic-forge#656).

`model_tier_gate.py` fires only on code-writing tool calls, so a `deep` plan,
handoff, review or L2D produced on Sonnet was never checked -- and on
2026-09-14 every lane ran on Sonnet. Pinning the lanes to Opus was rejected
(it burns high-tier tokens on the `fast`/`standard` majority), so this checks
at the moment work is assigned instead, when a block costs nothing.

Active only when `LANE` is `1` or `2`. Lane 3 is exempt (AC7) for the same
reason the edit gate exempts it, and a session with no `LANE` is not a lane.

For every issue reference in the prompt (AC1) -- prefixed `H<N>`/`F<N>` via
`expand_lane_shorthand`'s own parser (imported, any case), `owner/repo#N`,
a GitHub issue URL, bare `#N`, and a trigger verb followed by a bare number
or a `then`/comma chain of them (both resolved against the cwd repo through
`model_tier_gate.resolve_repo`) -- read the board Tier through
`model_tier_gate.read_tier` (the gate's 120 s cache; at most
`_MAX_TIER_READS` per prompt). Belt/Monitor notifications arrive as prompts
too, so `vitalharmony/hrse#1830 queued-for-l2` is checked like a trigger.

- Tier `deep`, model not in `CLAUDE_HIGH_FAMILIES`: block, naming the issue,
  Tier, model and the fix.
- Tier unreadable, model not high (AC2): block with the error and the ways
  forward. On a high model: proceed with a visible note, so a rate-limit
  window cannot wedge the lanes.
- Tier `fast`/`standard`, model high (AC3): proceed, suggesting
  `/model sonnet` -- unless another ref in the same prompt needs the high
  model, where that suggestion would be wrong.
- Refs past the read cap are listed as unchecked and never block.
- `LANE_MODEL` (launch-time, which an agent cannot set on its own running
  session) skips the check with a visible note.

Fails open only on this hook's own internal exceptions (logged to stderr),
never on a failed Tier read.

Claude Code only. Codex and Gemini lanes do not run this hook and are not
covered.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))
import expand_lane_shorthand  # noqa: E402
import model_tier_gate  # noqa: E402
import session_model  # noqa: E402

_MAX_TIER_READS = 6
# Per board read. `_MAX_TIER_READS * _READ_TIMEOUT_SECONDS` stays under the
# 30 s registration timeout; a timed-out read is a failed read, not an allow.
_READ_TIMEOUT_SECONDS = 4
_ERROR_CHARS = 200

_VERBS = (
    r"(?:re-?)?implement|(?:re-?)?plan|fix|blueprint|test|verify"
    r"|approve\s+and\s+execute|run\s+lane\s+[123]\s+gate|lane\s+[123]\s+execute"
)
_ITEM = r"[A-Za-z]?#?\d+"
_SEP = r"\s*(?:,|&|\bthen\b|\band\b)\s*"
_VERB_CHAIN_RE = re.compile(
    rf"\b(?:{_VERBS})\s+({_ITEM}(?:(?:{_SEP})(?:(?:{_VERBS})\s+)?{_ITEM})*)\b",
    re.IGNORECASE,
)
_BARE_IN_CHAIN_RE = re.compile(r"(?<![A-Za-z#\d])(\d+)\b")
_OWNER_REPO_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9][\w.-]*/[\w.-]+)#(\d+)\b")
_ISSUE_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)\b")
_BARE_HASH_RE = re.compile(r"(?<![\w/#&])#(\d+)\b")
_LOWER_PREFIX_RE = re.compile(r"\b([a-z])(\d{2,})\b")


def _unfenced(prompt: str) -> str:
    out, in_fence = [], False
    for line in prompt.splitlines(keepends=True):
        if expand_lane_shorthand.FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "".join(out)


def _prefixed_refs(prompt: str) -> list[tuple[str, int]]:
    """`H1830`/`h1830` through `expand_lane_shorthand`'s parser, not a copy.

    The parser matches uppercase prefixes only; lane prompts are often typed
    lowercase (`plan h1430`), so a lowercase token whose letter IS a declared
    prefix is uppercased first. That is input normalization, not a second
    parse of the prefix table.
    """
    doc_text = expand_lane_shorthand.DOC_PATH.read_text(encoding="utf-8")
    prefixes = expand_lane_shorthand.parse_repo_prefixes(doc_text)

    def upper(match: re.Match) -> str:
        letter = match.group(1).upper()
        return letter + match.group(2) if letter in prefixes else match.group(0)

    normalized = _LOWER_PREFIX_RE.sub(upper, prompt)
    return [(repo, int(number)) for repo, number
            in expand_lane_shorthand.collect_live_issue_refs(normalized, doc_text)]


def collect_refs(prompt: str, cwd: str) -> list[tuple[str, int]]:
    """Every issue reference in the prompt, deduplicated, in a stable order."""
    refs: list[tuple[str, int]] = []

    def add(repo: str | None, number: int) -> None:
        if repo and number > 0 and (repo, number) not in refs:
            refs.append((repo, number))

    for repo, number in _prefixed_refs(prompt):
        add(repo, number)
    text = _unfenced(prompt)
    for match in _OWNER_REPO_RE.finditer(text):
        add(match.group(1), int(match.group(2)))
    for match in _ISSUE_URL_RE.finditer(text):
        add(match.group(1), int(match.group(2)))

    bare: list[int] = [int(m.group(1)) for m in _BARE_HASH_RE.finditer(text)]
    for match in _VERB_CHAIN_RE.finditer(text):
        bare += [int(n) for n in _BARE_IN_CHAIN_RE.findall(match.group(1))]
    if bare:
        cwd_repo = model_tier_gate.resolve_repo(cwd) if cwd else None
        for number in bare:
            add(cwd_repo, number)
    return refs


def _boards() -> dict[str, str]:
    """`{owner/name: board_number}` from `projects.toml`, else the gate's
    hinted targets (hrse and harmonic-forge) if the manifest cannot load."""
    try:
        sys.path.insert(0, str(HOOKS_DIR.parent / "onboard"))
        from manifest import repo_boards  # noqa: PLC0415

        return {repo: board[1] for repo, board in repo_boards().items()}
    except Exception:  # noqa: BLE001
        return {repo: number for repo, number in model_tier_gate.HINTED_TARGETS.values()}


def _timed_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False,
                          timeout=_READ_TIMEOUT_SECONDS)


def lookup_tier(repo: str, number: int, boards: dict[str, str]):
    """`(tier, error)`; `(None, None)` for a repo with no board."""
    board = boards.get(repo)
    if board is None:
        return None, None
    return model_tier_gate.read_tier(repo, number, board, run=_timed_run)


def decide(refs: list[tuple[str, int]], model: str | None, lookup) -> dict | None:
    """The hook's JSON output for these refs, or None for a silent allow."""
    high = model_tier_gate.claude_model_is_high(model)
    model_label = model or "an unresolved model"
    checked, unchecked = refs[:_MAX_TIER_READS], refs[_MAX_TIER_READS:]
    blocks: list[str] = []
    notes: list[str] = []
    suggestions: list[str] = []
    needs_high = False
    for repo, number in checked:
        ref = f"{repo}#{number}"
        tier, error = lookup(repo, number)
        if tier is model_tier_gate.LOOKUP_FAILED:
            needs_high = True
            error_text = (error or "unknown error")[:_ERROR_CHARS]
            if high:
                notes.append(f"Tier for {ref} could not be read ({error_text}).")
            else:
                blocks.append(
                    f"Tier for {ref} could not be read ({error_text}); this session is "
                    f"{model_label}. Run /model opus (or /model fable) and resend, wait "
                    f"for the rate-limit reset and resend, or relaunch the lane with "
                    f"LANE_MODEL set."
                )
        elif tier in model_tier_gate.ESCALATING_TIERS:
            needs_high = True
            if not high:
                blocks.append(
                    f"{ref} is Tier {tier}; this session is {model_label}. "
                    f"Run /model opus (or /model fable) and resend."
                )
        elif tier in ("fast", "standard") and high:
            suggestions.append(f"{ref} is Tier {tier}; /model sonnet is sufficient.")
    if not needs_high:
        notes += suggestions
    if unchecked:
        notes.append(
            "Tier not checked (more than "
            f"{_MAX_TIER_READS} issue references in one prompt): "
            + ", ".join(f"{repo}#{number}" for repo, number in unchecked) + "."
        )
    if blocks:
        return {"decision": "block",
                "reason": "\n".join(blocks + notes) + " (harmonic-forge#656)"}
    if notes:
        return {"systemMessage": "\n".join(notes)}
    return None


_UNSET = object()


def run(payload: dict, env: dict | None = None, lookup=None, model=_UNSET) -> dict | None:
    env = os.environ if env is None else env
    if env.get("LANE") not in ("1", "2"):
        return None
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    cwd = payload.get("cwd") or os.getcwd()
    refs = collect_refs(prompt, cwd)
    if not refs:
        return None
    if env.get("LANE_MODEL"):
        return {"systemMessage": (
            f"LANE_MODEL={env['LANE_MODEL']} launch override is set; tier/model check "
            f"skipped for {', '.join(f'{r}#{n}' for r, n in refs)}."
        )}
    if model is _UNSET:
        model = session_model.current_model(
            payload.get("transcript_path"), cwd, payload.get("session_id"))
    if lookup is None:
        boards = _boards()

        def lookup(repo, number):
            return lookup_tier(repo, number, boards)
    return decide(refs, model, lookup)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return
        result = run(payload)
    except Exception as exc:  # noqa: BLE001 -- fail open on the hook's own bugs only
        print(f"tier_model_trigger_check: internal error, allowing: {exc!r}", file=sys.stderr)
        return
    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
