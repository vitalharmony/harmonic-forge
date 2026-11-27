#!/usr/bin/env python3
"""Stop hook: report a lane artifact posted on a `deep` issue from a model
that is not high-tier (harmonic-forge#656 AC5).

The backstop behind `tier_model_trigger_check.py`. That hook blocks at the
moment work is assigned; this one catches what got through anyway -- a
trigger phrased in a way the parser missed, a check skipped by `LANE_MODEL`,
or a post on an issue the prompt never named. It fires after the work is
done, so it cannot prevent anything; it guarantees the operator sees it.

Reads the current turn from `transcript_path` (back to the last real prompt,
bounded like every other transcript read in this directory) and collects the
turn's post **receipts** (harmonic-forge#843 reforge): the comment URL a
sanctioned poster prints, read from the Bash `tool_result` it landed in. The
URL names the repo and issue exactly, so no command-text parser decides where a
post went. A receipt counts only when all three hold (NC1): the result belongs
to a Bash call; that call's command names a poster; and the URL is on that
poster's own receipt line (or is the whole stdout of a bare `gh` post). Reads
that merely quote a comment URL -- a `Read`, `fetch_lane1_context.py`, `gh api
.../comments`, `l1_post.py`'s own `[check-lane3-ready]` lines -- never count.
For each posted `(repo, issue)` it reads the board Tier fresh (`ttl=0`) and,
for `deep`, compares the model that made the call (that assistant entry's
`message.model`, else `session_model.current_model`).

Residual, recorded by the operator's ruling: a post whose output never lands in
this turn's `tool_result` (a backgrounded call) yields no receipt.

`posted_targets` (command text) survives only as the downshift reminder's
suppression hint, where a wrong guess costs silence.

The transcript read is bounded (`_SCAN_MAX_BYTES`). When the bound is hit
before the start of the turn, the report says so ("backstop scan truncated"),
because a post made early in a long turn was otherwise silently unchecked.

**Never blocks.** A Stop `block` forces Claude to continue rather than handing
control to the operator, which is the opposite of what a report is for. Output
is a `systemMessage` only. Lane 3 is skipped (AC7). Deliberately names no
override.

Claude Code only. Codex lanes do not run this hook and are not covered.
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
import model_tier_gate  # noqa: E402
import session_model  # noqa: E402
from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

_MAX_TIER_READS = 6
_READ_TIMEOUT_SECONDS = 3
_POSTER_SCRIPTS = frozenset({"l1_post.py", "l2_post.py", "post_comment.py"})
# harmonic-forge#843 AC1: no poster defaults to hrse. `l1_post.resolve_repo()`
# resolves the INVOKING checkout, so a harmonic-forge post was being scored
# against an hrse issue of the same number.
_MISE_POSTERS = frozenset({"l1-post", "l2-post", "lane-comment", "post-comment"})

# harmonic-forge#843 reforge, NC1: a receipt is the URL on a poster's OWN line.
_RECEIPT_URL_RE = re.compile(
    r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)#issuecomment-\d+")
_POSTER_IN_COMMAND_RE = re.compile(
    r"l1-post|l1_post|l2-post|l2_post|post-comment|post_comment|lane-comment"
    r"|post_lane_discussion|gh\s+issue\s+comment|--method\s+POST|-X\s+POST"
    # `gh api …/comments -f body=…` is an implicit POST (preclose).
    r"|gh\s+api\s+\S*issues/\d+/comments\b.*(?:\s-[fF]\s|--(?:raw-)?field[\s=]|--input\b)")
_RECEIPT_LINE_RE = re.compile(
    r"^\s*(?:\[l1-post\] (?:posted and refetched|AE posted|sweep posted) "
    r"|\[POST-COMMENT\] Posted: |\[post-comment\] posted and refetched )")
_API_COMMENTS_RE = re.compile(r"(?:^|/)repos/([^/\s]+/[^/\s]+)/issues/(\d+)/comments/?$")
_ISSUE_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")
_GH_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
# The transcript tail read's bound, passed to `session_model._tail_lines`.
_SCAN_MAX_BYTES = 4 << 20
_SCAN_CHUNK_BYTES = 65536
_TRUNCATED_NOTE = "backstop scan truncated; earlier posts in this turn were not checked"


def _flag(tokens: list[str], *names: str) -> str | None:
    for index, token in enumerate(tokens):
        for name in names:
            if token == name and index + 1 < len(tokens):
                return tokens[index + 1]
            if name.startswith("--") and token.startswith(name + "="):
                return token.split("=", 1)[1]
    return None


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    match = _ISSUE_URL_RE.search(value)
    if match:
        return int(match.group(2))
    return int(value) if value.isdigit() else None


def posted_targets(command: str, cwd_repo) -> list[tuple[str, int]]:
    """`(repo, issue)` pairs a Bash command posts a comment to.

    `cwd_repo` is a zero-argument callable, so the cwd's `git`/`mise.toml`
    read happens only for a post that actually needs it.
    """
    try:
        segments = command_segments(command)
    except ValueError:
        return []
    targets: list[tuple[str, int]] = []
    for raw in segments:
        tokens = strip_invocation_prefix(raw)
        if not tokens:
            continue
        names = [os.path.basename(t) for t in tokens]
        repo: str | None = None
        issue: int | None = None
        script = next((i for i, n in enumerate(names) if n in _POSTER_SCRIPTS), None)
        if script is not None:
            rest = tokens[script + 1:]
            if names[script] == "l2_post.py" and "post" not in rest:
                continue  # snapshot / resolve-lock post nothing
            issue = _as_int(_flag(rest, "--issue"))
            repo = _flag(rest, "--repo")
        elif names[0] == "mise" and any(t in _MISE_POSTERS for t in tokens):
            task = next(t for t in tokens if t in _MISE_POSTERS)
            rest = tokens[tokens.index(task) + 1:]
            issue = _as_int(_flag(rest, "--issue"))
            repo = _flag(rest, "--repo")
        elif names[0] == "gh" and tokens[1:3] == ["issue", "comment"] and len(tokens) > 3:
            issue = _as_int(tokens[3])
            url = _ISSUE_URL_RE.search(tokens[3])
            repo = _flag(tokens[4:], "--repo", "-R") or (url.group(1) if url else None)
        elif names[0] == "gh" and len(tokens) > 1 and tokens[1] == "api":
            path = next((t for t in tokens[2:] if _API_COMMENTS_RE.search(t)), None)
            if path is None:
                continue
            method = (_flag(tokens, "-X", "--method") or "").upper()
            has_fields = any(t in _GH_FIELD_FLAGS or t.startswith("--field=")
                             or t.startswith("--raw-field=") for t in tokens)
            if method != "POST" and not (has_fields and not method):
                continue
            match = _API_COMMENTS_RE.search(path)
            repo = match.group(1)
            issue = int(match.group(2))
            if "{owner}" in repo or "{repo}" in repo:
                repo = None
        else:
            continue
        if issue is None:
            continue
        repo = repo or cwd_repo()
        if repo and (repo, issue) not in targets:
            targets.append((repo, issue))
    return targets


def _is_turn_start(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def turn_posts(transcript_path: str) -> list[tuple[str, str | None, str]]:
    """`(command, model_of_that_call, tool_use_id)` for this turn's successful
    Bash calls, oldest first."""
    return scan_turn(transcript_path)[0]


def _bytes_scanned(size: int) -> int:
    """How much of a `size`-byte file `_tail_lines` reads: whole chunks until
    `_SCAN_MAX_BYTES` is reached, never more than the file."""
    chunks = -(-_SCAN_MAX_BYTES // _SCAN_CHUNK_BYTES)
    return min(size, chunks * _SCAN_CHUNK_BYTES)


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


_SYNTHETIC_MODEL = "<synthetic>"
_SCRIPT_ID_PREFIX = "repl_"


def _is_script_call(entry: dict | None, tool_use_id: str) -> bool:
    """A Code Mode script call (harmonic-forge#1642): a harness-written virtual
    assistant record whose model is `"<synthetic>"` and whose tool_use id is
    `repl_`-keyed."""
    return (isinstance(entry, dict) and entry.get("isVirtual") is True
            and (entry.get("message") or {}).get("model") == _SYNTHETIC_MODEL
            and str(tool_use_id or "").startswith(_SCRIPT_ID_PREFIX))


def _record_result_text(entry: dict, block: dict, call: dict | None) -> str:
    """`_result_text(block)`, except for a Code Mode script call's result
    (harmonic-forge#1642): its block `content` is empty and its output sits in
    the record's `toolUseResult`. That fallback is read only when BOTH records
    of the pair are a script call's: this virtual user record with a `repl_`
    id, and `call`, the assistant record that made the call with that id."""
    text = _result_text(block)
    tool_use_id = str(block.get("tool_use_id") or "")
    tool_use_result = entry.get("toolUseResult")
    if (text != "" or entry.get("isVirtual") is not True
            or not tool_use_id.startswith(_SCRIPT_ID_PREFIX)
            or not isinstance(tool_use_result, dict)
            or not _is_script_call(call, tool_use_id)):
        return text
    return "\n".join(value for value in (tool_use_result.get("stdout"),
                                          tool_use_result.get("stderr"))
                     if isinstance(value, str))


def _repl_model(start_uuid: str, by_uuid: dict[str, dict]) -> str | None:
    """The model of the REPL call that ran the script call recorded at
    `start_uuid` (harmonic-forge#1642), from the transcript's own links: the
    `parentUuid` chain to the REPL's tool_result record, whose
    `sourceToolAssistantUUID` names the REPL assistant record. None when the
    chain is broken, leaves the scanned window, or does not match."""
    seen: set[str] = set()
    current = by_uuid.get(start_uuid)
    while isinstance(current, dict):
        parent = current.get("parentUuid")
        if not isinstance(parent, str) or parent in seen or parent not in by_uuid:
            return None
        seen.add(parent)
        current = by_uuid[parent]
        source_uuid = current.get("sourceToolAssistantUUID")
        content = (current.get("message") or {}).get("content")
        if current.get("type") != "user" or current.get("isSidechain") \
                or not isinstance(source_uuid, str) or not isinstance(content, list):
            continue
        result_ids = {block.get("tool_use_id") for block in content
                      if isinstance(block, dict) and block.get("type") == "tool_result"}
        if not result_ids:
            continue
        source = by_uuid.get(source_uuid)
        if not isinstance(source, dict) or source.get("type") != "assistant" \
                or source.get("isSidechain"):
            return None
        message = source.get("message") or {}
        model = message.get("model")
        source_content = message.get("content")
        if not isinstance(model, str) or not model or model == _SYNTHETIC_MODEL \
                or not isinstance(source_content, list):
            return None
        if any(isinstance(block, dict) and block.get("type") == "tool_use"
               and block.get("name") == "REPL" and block.get("id") in result_ids
               for block in source_content):
            return model
        return None
    return None


_GH_COMMENT_SEGMENT_RE = re.compile(
    r"gh\s+issue\s+comment\s+(?:\S*/issues/)?(\d+)\b")
_GH_API_POST_SEGMENT_RE = re.compile(
    r"gh\s+api\s+\S*issues/(\d+)/comments\b[^&;|\n]*?"
    r"(?:--method\s+POST|-X\s*POST|\s-[fF]\s|--(?:raw-)?field[\s=]|--input\b)"
    # the POST flag may also come before the path (reforge pass-2 #4)
    r"|gh\s+api\s+(?:--method\s+POST|-X\s*POST)\s+\S*issues/(\d+)/comments\b")
# A command that RUNS TESTS prints fixture receipts, never real ones (pass-2 #3).
_TEST_RUN_RE = re.compile(r"\b(?:unittest|pytest|run_tests\.py|vitest)\b|mise\s+run\s+(?:test|check)\b")


def _bare_gh_post_numbers(command: str) -> set[int]:
    """Issue numbers a bare `gh` POST in this command targets. A bare `gh` post
    prints only the URL (or JSON with it), so its receipt is bound to the
    issue the posting SEGMENT names: a chained read of another issue never
    counts, and a chained post still does (harmonic-forge#843 preclose)."""
    numbers = {int(n) for n in _GH_COMMENT_SEGMENT_RE.findall(command)}
    for groups in _GH_API_POST_SEGMENT_RE.findall(command):
        numbers |= {int(n) for n in groups if n}
    return numbers


def _receipt_urls(command: str, text: str) -> list[tuple[str, int]]:
    """`(repo, issue)` this one Bash result proves it posted to (NC1).

    A named poster's receipt is the URL on its own receipt line (or, for
    `l2_post`, the `url` of its one JSON result). A bare `gh` post's receipt is
    a printed comment URL whose issue number the posting segment names."""
    if not _POSTER_IN_COMMAND_RE.search(command) or _TEST_RUN_RE.search(command):
        return []
    found: list[tuple[str, int]] = []
    bare_numbers = _bare_gh_post_numbers(command)
    l2_post = "l2_post" in command or "l2-post" in command

    def add(repo: str, number: str) -> None:
        pair = (repo.lower(), int(number))
        if pair not in found:
            found.append(pair)

    for line in text.splitlines():
        if _RECEIPT_LINE_RE.match(line):
            for repo, number in _RECEIPT_URL_RE.findall(line):
                add(repo, number)
            continue
        if l2_post and line.lstrip().startswith("{"):
            # `l2_post.py` prints ONE JSON result whose `url` is the created
            # comment; a chained `gh api .../comments` read prints `html_url`.
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("url"), str):
                for repo, number in _RECEIPT_URL_RE.findall(data["url"]):
                    add(repo, number)
            continue
        for repo, number in _RECEIPT_URL_RE.findall(line):
            if int(number) in bare_numbers:
                add(repo, number)
    return found


def scan_turn(transcript_path: str, with_receipts: bool = False, drop_background: bool = False,
              require_result: bool = False):
    """`(turn_posts, truncated)`. `truncated` is True when the bounded tail
    read ran out before reaching the turn's first entry (harmonic-forge#656
    preclose fix 6), so earlier calls in this turn were never seen.

    `with_receipts=True` (harmonic-forge#843 reforge) also returns this turn's
    post receipts, `(repo, issue, model)`, from the SAME bounded pass:
    `(turn_posts, truncated, receipts)`. The default shape is unchanged.

    `drop_background=True` (harmonic-forge#1151) also drops a Bash call run
    with `run_in_background`: its tool_result acknowledges the launch, so a
    non-error result says nothing about the command's exit. `require_result=True`
    also drops a call with no tool_result yet: no result is no evidence.

    Code Mode (harmonic-forge#1642): a script call is a virtual assistant
    record with model `"<synthetic>"` and a `repl_` tool_use id, answered by a
    virtual user record whose tool_result `content` is empty and whose output
    is the record's `toolUseResult`. That output is read only for such a pair
    (`_record_result_text`). The call takes the model of the REPL call its
    `parentUuid`/`sourceToolAssistantUUID` links name (`_repl_model`), resolved
    after the walk from the records this same bounded pass saw; when the links
    do not resolve, the call keeps `"<synthetic>"`, which is never high-tier,
    so the report fires. Every other record is read as before."""
    errored: set[str] = set()
    answered: set[str] = set()
    calls: list[tuple[str, str | None, str]] = []
    results: dict[str, str] = {}
    script_results: dict[str, tuple[dict, dict]] = {}
    call_records: dict[str, dict] = {}
    by_uuid: dict[str, dict] = {}
    pending: list[tuple[int, str]] = []
    reached_turn_start = False
    try:
        size = os.path.getsize(transcript_path)
        for line in session_model._tail_lines(transcript_path, chunk_size=_SCAN_CHUNK_BYTES,
                                              max_bytes=_SCAN_MAX_BYTES):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if _is_turn_start(entry):
                reached_turn_start = True
                break
            if isinstance(entry.get("uuid"), str):
                by_uuid.setdefault(entry["uuid"], entry)
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, list) or entry.get("isSidechain"):
                continue
            if entry.get("type") == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        answered.add(block.get("tool_use_id"))
                        if block.get("is_error"):
                            errored.add(block.get("tool_use_id"))
                        elif with_receipts:
                            tool_use_id = block.get("tool_use_id") or ""
                            results[tool_use_id] = _result_text(block)
                            # harmonic-forge#1642: met before its call in this
                            # backward walk; admitted after the walk.
                            script_results[tool_use_id] = (entry, block)
            elif entry.get("type") == "assistant":
                model = (entry.get("message") or {}).get("model")
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use" \
                            and block.get("name") == "Bash":
                        command = (block.get("input") or {}).get("command")
                        if drop_background and (block.get("input") or {}).get("run_in_background"):
                            continue
                        if isinstance(command, str):
                            tool_use_id = block.get("id") or ""
                            call_records.setdefault(tool_use_id, entry)
                            if _is_script_call(entry, tool_use_id) \
                                    and isinstance(entry.get("uuid"), str):
                                pending.append((len(calls), entry["uuid"]))
                            calls.append((command, model, tool_use_id))
    except OSError:
        return ([], False, []) if with_receipts else ([], False)
    truncated = not reached_turn_start and size > _bytes_scanned(size)
    for index, record_uuid in pending:
        repl_model = _repl_model(record_uuid, by_uuid)
        if repl_model is not None:
            command, _model, tool_use_id = calls[index]
            calls[index] = (command, repl_model, tool_use_id)
    for tool_use_id, (entry, block) in script_results.items():
        results[tool_use_id] = _record_result_text(entry, block, call_records.get(tool_use_id))
    calls.reverse()
    kept = [call for call in calls if call[2] not in errored
            and (not require_result or call[2] in answered)]
    if not with_receipts:
        return kept, truncated
    receipts: list[tuple[str, int, str | None]] = []
    for command, model, tool_id in kept:
        for repo, issue in _receipt_urls(command, results.get(tool_id, "")):
            if not any(r[:2] == (repo, issue) for r in receipts):
                receipts.append((repo, issue, model))
    return kept, truncated, receipts


def _timed_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return model_tier_gate.timed_run(cmd, timeout=_READ_TIMEOUT_SECONDS)


def run(payload: dict, env: dict | None = None, lookup=None, fallback_model=None) -> dict | None:
    env = os.environ if env is None else env
    if env.get("LANE") == "3":
        return None
    transcript_path = payload.get("transcript_path") or ""
    cwd = payload.get("cwd") or os.getcwd()

    # harmonic-forge#843 reforge: receipts only (operator ruling 4).
    _calls, truncated, posted = scan_turn(transcript_path, with_receipts=True)
    messages: list[str] = [_TRUNCATED_NOTE + "."] if truncated else []
    if not posted:
        return _report(messages)

    if lookup is None:
        from tier_model_trigger_check import NO_BOARD, _boards, lookup_tier  # noqa: PLC0415

        boards = _boards()

        def lookup(repo, number):
            # harmonic-forge#820: `lookup_tier` reads each repo's board as that repo's own slot
            # and board owner (ttl=0, preclose fix 7: a Tier raised mid-turn must not be hidden by
            # the edit gate's cache), so a kenekted issue is not read as vitalharmony.
            tier, error = lookup_tier(repo, number, boards)
            return (None, None) if tier is NO_BOARD else (tier, error)

    for repo, issue, call_model in posted[:_MAX_TIER_READS]:
        model = call_model or fallback_model or session_model.current_model(
            transcript_path, cwd, payload.get("session_id"))
        if model_tier_gate.claude_model_is_high(model):
            continue  # no board read needed: a high model satisfies any Tier
        label = model or "an unresolved model"
        tier, error = lookup(repo, issue)
        if tier is model_tier_gate.LOOKUP_FAILED:
            messages.append(
                f"Posted on {repo}#{issue} from {label}, but its Tier could not be read "
                f"({(error or 'unknown error')[:200]}); check whether it is deep."
            )
        elif tier in model_tier_gate.ESCALATING_TIERS:
            messages.append(
                f"Posted on {repo}#{issue} (Tier {tier}) from {label}; this needs "
                f"redoing on a high-tier model."
            )
    return _report(messages)


def _report(messages: list[str]) -> dict | None:
    if not messages:
        return None
    return {"systemMessage": "\n".join(messages) + " (harmonic-forge#656)"}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return
        result = run(payload)
    except Exception as exc:  # noqa: BLE001 -- a report must never break a turn
        print(f"tier_model_stop_backstop: internal error: {exc!r}", file=sys.stderr)
        return
    if result:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
