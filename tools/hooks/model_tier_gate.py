#!/usr/bin/env python3
"""PreToolUse hook: require the high-tier model on `deep`-tier issues.

harmonic-forge#202. Reads the PreToolUse JSON payload from stdin, resolves
the issue currently being worked on from the branch name in cwd, looks up
its board `Tier` field, and denies a code-writing tool call if the
session's active model doesn't match the required tier.

Fail-open on resolution failures (no branch match, no board or repo mapping,
no gh, unexpected payload shape) -- this hook must never wedge a lane over its
own telemetry breaking. See 3-lane-protocol.md Tooling Exception.

One exception, harmonic-forge#656 AC6: when the branch DID name an issue and
the board read itself failed (`GhItemListError`, e.g. a rate-limit 403),
`resolve_tier` returns `LOOKUP_FAILED` rather than None, and a session not
already on a high-tier model is denied. Treating a failed read as "no Tier"
allowed every `deep` edit during exactly the windows the lanes are busiest.
The Claude Code session model now comes from `session_model.current_model`
(transcript attachment, `/model` output or `message.model`, then the
SessionStart record, then settings). The trigger-time companion is
`tier_model_trigger_check.py`; the turn-end backstop is
`tier_model_stop_backstop.py`. Those two are Claude Code only.

Wired identically for Claude Code (Edit|Write matcher) and Codex
(apply_patch matcher) -- both feed the same PreToolUse JSON shape over
stdin, confirmed live 2026-08-09 (harmonic-forge#202 comments).

harmonic-forge#440: also wired on `Bash`, and gated the same way -- eleven
of thirteen `PreToolUse` hooks in `hrse`'s own `.claude/settings.json`
guard `Bash`, but this one guarded only `Edit|Write`, and the default
launcher permission mode (`--permission-mode auto`) explicitly steers
sessions toward heredocs/`sed`/short scripts over the dedicated Edit/Write
tools -- so a `deep`-tier implementation could, and did (hrse#1438), land
entirely through Bash writes with the gate never firing. Detection reuses
`shell_parse`'s shared heredoc-masking/segment-splitting (do not write a
second parser) rather than a whole-string regex, the same failure class
two prior hooks in this directory were rewritten to fix
(harmonic-forge#167/#245).

Also fills a coverage gap this issue's own audit missed: `harmonic-forge`'s
own `.claude/settings.json` had never wired this hook on `Edit|Write` at
all (only `Bash`, added by an earlier, narrower fix) -- so hook/tooling
work in this repo itself, exactly the kind of `deep`-tier work this gate
exists to cover, was ungated on that path.

Lane 3 is exempted outright (`LANE == "3"`), before any board lookup:
Lane 3 verifies an implementation that already exists and should not need
the high-tier model to run a test suite against it (operator decision,
2026-09-02) -- and it was bound here only by accident, since gating
`Edit|Write` alone left Lane 3's gate work untouched, but extending
coverage to `Bash` without this exemption would make Lane 3 the *most*
bound lane on every heredoc in its own gate scripts.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_parse import command_segments, mask_heredoc_bodies, strip_invocation_prefix  # noqa: E402
import session_model  # noqa: E402
from session_model import _tail_lines  # noqa: E402,F401 -- re-exported; one bounded reader
from lane3_codex_write_guard import apply_patch_targets  # noqa: E402 -- harmonic-forge#630, one patch parser, not two
from lane3_codex_write_guard import _resolve as _codex_resolve  # noqa: E402 -- one relative-path resolver, not two (preclose finding 2)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gh"))
try:
    import item_list_cache as _item_list_cache
except ImportError:
    _item_list_cache = None

# harmonic-forge#257: which Tier values require the high-tier model. Defined
# locally rather than imported from item_list_cache, because this hook must keep
# working when that module is not importable — the `except ImportError` above is
# the fail-open path, and a NameError here would deny-by-crash on every
# Edit/Write instead.
#
# `deep` preserves the former >= 8 escalation boundary. Tier is now the only
# routing input; do not add a second, numeric fallback here.
ESCALATING_TIERS = frozenset({"deep"})

# harmonic-forge#203: `gh project item-list --limit 1000` is the single most
# expensive call this hook makes (GitHub's GraphQL limiting is cost-based on
# query complexity/node count, confirmed live -- a handful of these calls
# fully drained a 5000-point quota). This hook runs as a fresh process on
# every single code-writing tool call, so an in-process cache is useless;
# a short-TTL on-disk cache collapses a whole editing burst into one real
# fetch. 120s is short enough that a mid-session re-estimation is picked up
# within about two minutes, long enough to absorb dozens of Edit/Write
# calls in a normal burst.
#
# Known tradeoff (harmonic-forge#202's own guarantee, narrowed slightly):
# a mid-lane re-estimation that crosses the >= 8 threshold can be silently
# missed by the gate for up to this TTL after the board write. Accepted as
# the cost of the burst-collapse -- fail-open already tolerates larger,
# unbounded gaps (missing gh entirely), so a bounded ~2min window is a
# strict improvement, not a new class of risk.
#
# harmonic-forge#219: the actual fetch/cache-file mechanics now live in
# tools/gh/item_list_cache.py, shared with board_sync.py/board_drift_check.py/
# l1_post.py (previously 4 independent duplicate implementations). This
# module keeps its own cache directory/TTL constants and fail-open wrapper
# unchanged -- only the fetch internals moved.
_CACHE_DIR = Path(tempfile.gettempdir()) / "harmonic-forge-gh-item-list-cache"
_CACHE_TTL = 120

# Claude Code model families are substring-matched against message.model
# (e.g. "claude-opus-5", "claude-sonnet-5"); Codex models are matched
# exactly against payload["model"] (e.g. "gpt-5.6-sol", "gpt-5.6-terra").
# Families that satisfy a `deep`-tier requirement -- Opus and above.
# harmonic-forge#314: this was a single `CLAUDE_HIGH = "opus"` substring,
# correct only while Opus was the family's ceiling. Fable 5 is above Opus,
# not a sibling, and `"opus" in "claude-fable-5"` is False -- so a
# deep-tier issue denied the most capable model available and told it to
# `/model opus`, i.e. to downgrade, on the work that most needs capability.
#
# Deliberately an explicit allowlist, not a wider pattern: model names are
# fluid as new models ship, and a new top tier should require a reviewed
# one-line addition here rather than being granted implicitly by a match
# that happens to be broad enough. Do not seed it with speculative names.
CLAUDE_HIGH_FAMILIES = frozenset({"opus", "fable"})
CODEX_HIGH = "gpt-5.6-sol"

# The former `CLAUDE_LOW`/`CODEX_LOW` constants are gone, not relocated.
# Both were defined and never read: the gate only ever asks "is this high
# enough", so "not high" is the complete answer, and a positive low-tier
# assertion would be a second independently-driftable list of names to
# maintain -- the same staleness this issue was filed for.

BRANCH_ISSUE_RE = re.compile(r"^[\w.-]+/[a-zA-Z]?(\d+)-")
HINTED_BRANCH_ISSUE_RE = re.compile(
    r"^[\w.-]+/(?P<hint>hrse|harmonic-forge|forge|h|f)-?(?P<number>\d+)-"
)
WORKTREE_ISSUE_RE = re.compile(r"^/tmp/hrse2-(?P<number>\d+)-impl$")
HINTED_TARGETS = {
    "hrse": ("vitalharmony/hrse", "1"), "h": ("vitalharmony/hrse", "1"),
    "harmonic-forge": ("vitalharmony/harmonic-forge", "3"),
    "forge": ("vitalharmony/harmonic-forge", "3"), "f": ("vitalharmony/harmonic-forge", "3"),
}


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


# harmonic-forge#656 preclose fix 5: a board read with no timeout could hang
# until the hook's registration timeout killed it, which Claude Code treats as
# a non-blocking error -- the edit went through. A timed-out read raises
# `TimeoutExpired`, which `read_tier` turns into LOOKUP_FAILED.
_READ_TIMEOUT_SECONDS = 4


def timed_run(cmd: list[str], timeout: float = _READ_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """`_run` with a timeout -- the one timed board-read runner every Tier
    reader in this directory uses (edit gate, trigger check, Stop backstop)."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False,
                          timeout=timeout)


def _allow() -> None:
    sys.exit(0)


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def resolve_issue_target(cwd: str) -> tuple[int, str | None] | None:
    result = _run(["git", "-C", cwd, "branch", "--show-current"])
    if result.returncode == 0:
        branch = result.stdout.strip()
        hinted = HINTED_BRANCH_ISSUE_RE.match(branch)
        if hinted:
            return int(hinted.group("number")), hinted.group("hint")
        m = BRANCH_ISSUE_RE.match(branch)
        if m:
            return int(m.group(1)), None
    worktree = WORKTREE_ISSUE_RE.match(os.path.realpath(cwd))
    return (int(worktree.group("number")), "hrse") if worktree else None


def resolve_issue_number(cwd: str) -> int | None:
    target = resolve_issue_target(cwd)
    return target[0] if target else None


# harmonic-forge#630: which directories a write-capable tool call actually
# targets. `resolve_issue_target` takes any directory inside the target
# worktree -- `git -C <dir> branch --show-current` and the worktree-path
# regex both work from a subdirectory, not only the worktree root -- so
# these return the nearest EXISTING ancestor directory of each write path,
# never the file path itself: Write can create a file (and parent dirs)
# that do not exist yet, and `git -C` needs a real directory to chdir into.
def _nearest_existing_dir(path: str) -> str | None:
    d = os.path.dirname(path) or path
    seen = set()
    while d and d not in seen:
        if os.path.isdir(d):
            return d
        seen.add(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _worktree_root_for(path: str) -> str | None:
    """The git worktree root containing `path`'s nearest existing ancestor.

    harmonic-forge#630 preclose finding 1: `resolve_issue_target`'s branch
    check already works from any subdirectory (`git -C <dir> branch
    --show-current` understands subdirectories), but its worktree-path
    fallback (`WORKTREE_ISSUE_RE`) is anchored to the exact worktree root
    and never matches a subdirectory. A file nested inside a worktree
    (anything but a file sitting directly in the worktree root) fell
    through to `_nearest_existing_dir`'s bare answer, which is normally a
    subdirectory, silently dropping that fallback for a detached-HEAD or
    non-conforming-branch worktree -- exactly the state a conflicted
    rebase leaves a Lane 2 worktree in. Resolving to the real git root
    first makes the worktree-regex fallback see what it expects again,
    without changing `resolve_issue_target`'s own contract for its
    existing callers.
    """
    existing = _nearest_existing_dir(path)
    if existing is None:
        return None
    result = _run(["git", "-C", existing, "rev-parse", "--show-toplevel"])
    top = result.stdout.strip() if result.returncode == 0 else ""
    return top or existing


def write_target_dirs(payload: dict) -> list[str] | None:
    """The write target(s) a tool call names, as existing worktree roots.

    None means this tool call has no resolvable write target at all (no
    `file_path`, an unrecognized `apply_patch` body, or a `Bash` command --
    `bash_command_writes_files` proves ONLY that a write happens, never
    where, so Bash keeps resolving from the session cwd, unchanged from
    before this issue). An empty list is impossible by construction: every
    branch below either returns `None` or at least one directory.

    AC3: apply_patch is the one shape that can name several paths in one
    call (`apply_patch_targets` already parses this for
    `lane3_codex_write_guard`, harmonic-forge#644 -- reused here rather than
    a second patch parser). Each target's own existing ancestor is returned;
    callers decide what "different issues in one call" means for them.

    harmonic-forge#630 preclose finding 2: `apply_patch` bodies routinely
    carry repo-relative paths, not just absolute ones. `apply_patch_targets`
    returns them as written; resolving a relative target requires the
    PAYLOAD's own cwd, never the hook process's own cwd (which is whatever
    directory happened to invoke this hook, unrelated to the tool call).
    `lane3_codex_write_guard._resolve` already has this exact contract --
    reused here rather than a second relative-path resolver.
    """
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    if tool_name in ("Edit", "Write", "MultiEdit"):
        path = tool_input.get("file_path")
        if not path:
            return None
        d = _worktree_root_for(path)
        return [d] if d else None
    if tool_name == "apply_patch":
        targets = apply_patch_targets(tool_input.get("command", ""))
        if not targets:
            return None
        payload_cwd = Path(payload.get("cwd") or os.getcwd())
        resolved_targets = [_codex_resolve(t, payload_cwd) for t in targets]
        dirs = [d for d in (_worktree_root_for(t) for t in resolved_targets if t) if d]
        return dirs or None
    return None


def _mise_env_value(mise_toml: str, key: str) -> str | None:
    m = re.search(rf'^{key}\s*=\s*"([^"]*)"', mise_toml, re.MULTILINE)
    return m.group(1) if m else None


def resolve_project_board(cwd: str) -> tuple[str, str] | None:
    """Read GH_PROJECT_OWNER/GH_PROJECT_NUMBER from the repo's own mise.toml.

    Deliberately does NOT read os.environ -- those vars are only correct if
    the calling shell already ran mise's cd-hook for this exact cwd, which
    is not guaranteed (confirmed live, harmonic-forge#202: a shell that had
    activated HRSE2's mise env leaked GH_PROJECT_NUMBER=1 into a hook
    invocation whose payload cwd was actually the harmonic-forge worktree).
    Reading the file directly removes that timing dependency.
    """
    root_result = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    if root_result.returncode != 0:
        return None
    root = root_result.stdout.strip()
    try:
        with open(os.path.join(root, "mise.toml")) as f:
            toml_text = f.read()
    except OSError:
        return None
    owner = _mise_env_value(toml_text, "GH_PROJECT_OWNER")
    number = _mise_env_value(toml_text, "GH_PROJECT_NUMBER")
    return (owner, number) if owner and number else None


def resolve_repo(cwd: str) -> str | None:
    """Read GH_REPO from the repo's own mise.toml (harmonic-forge#250).

    The targeted per-issue query needs "owner/name", which
    `resolve_project_board` does not supply -- it returns the *project* owner
    and number. Both hrse and harmonic-forge already declare GH_REPO in
    mise.toml, so this reuses the same file-reading path for the same reason
    `resolve_project_board` documents: os.environ is only correct if the
    calling shell ran mise's cd-hook for this exact cwd, which a hook
    invocation cannot assume (harmonic-forge#202, confirmed live).
    """
    root_result = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    if root_result.returncode != 0:
        return None
    try:
        with open(os.path.join(root_result.stdout.strip(), "mise.toml")) as f:
            return _mise_env_value(f.read(), "GH_REPO")
    except OSError:
        return None


class _TierLookupFailed:
    """harmonic-forge#656 AC6: the board read itself failed (rate limit, 403,
    timeout) -- distinct from "the issue has no Tier" (None).

    Before this, `resolve_tier` turned `GhItemListError` into None and `_main`
    allowed, exactly as if no Tier were set -- so a `deep` edit on the wrong
    model went through whenever the board read failed, which is precisely a
    rate-limit window, when the lanes are busiest. A singleton rather than a
    string so it can never collide with a real Tier value or be matched by
    `in ESCALATING_TIERS`.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "LOOKUP_FAILED"


LOOKUP_FAILED = _TierLookupFailed()


class _NotAnIssue:
    """harmonic-forge#656 preclose fix 3: GitHub has no issue with that number
    (a PR number, or a number that does not exist). Not a failed read -- a
    rate limit or a 403 is -- so it must never block; callers skip it."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NOT_AN_ISSUE"


NOT_AN_ISSUE = _NotAnIssue()

# The GraphQL error for `repository.issue(number: N)` when N is not an issue.
# `gh` surfaces it on stderr ("GraphQL: Could not resolve to an Issue with the
# number of 1833. (repository.issue)") and, on a zero exit, in the payload's
# `errors[].message`; both become the `GhItemListError` text. `NOT_FOUND` is
# that error's `type`. Deliberately nothing broader: a 403, a timeout or a
# network error must stay LOOKUP_FAILED.
_NOT_AN_ISSUE_RE = re.compile(r"Could not resolve to an Issue\b|\bNOT_FOUND\b")


def is_not_an_issue_error(message: str) -> bool:
    """True when a `GhItemListError` message says the number is not an issue."""
    return bool(_NOT_AN_ISSUE_RE.search(message or ""))


def read_tier(repo: str, issue_number: int, project_number: str, run=None,
              ttl: float = _CACHE_TTL):
    """`(tier, error)` for one issue on one board.

    `tier` is a Tier string, None (no Tier set, or the shared module is not
    importable), NOT_AN_ISSUE (GitHub has no issue with that number), or
    LOOKUP_FAILED with `error` carrying the reason. `ttl` defaults to the edit
    gate's `_CACHE_TTL`, which collapses an editing burst into one read; the
    trigger check and Stop backstop pass `ttl=0` (harmonic-forge#656 preclose
    fix 7), because a cached "no Tier" or stale Tier there lets a trigger
    through for the whole window after the Tier is raised -- the reason
    `fetch_issue_tier` itself defaults to `ttl=0`.
    """
    if _item_list_cache is None:
        return None, None  # shared module unavailable -- fail-open
    try:
        return _item_list_cache.fetch_issue_tier(
            repo, issue_number, project_number,
            run=run or _run, ttl=ttl, cache_dir=_CACHE_DIR,
        ), None
    except _item_list_cache.GhItemListError as exc:
        message = str(exc).strip() or exc.__class__.__name__
        if is_not_an_issue_error(message):
            return NOT_AN_ISSUE, message
        return LOOKUP_FAILED, message
    except subprocess.TimeoutExpired as exc:
        return LOOKUP_FAILED, f"board read timed out after {exc.timeout}s"


def resolve_tier(cwd: str, issue_number: int, repo_hint: str | None = None):
    """Return the issue's Tier (harmonic-forge#257).

    harmonic-forge#250: reads *one issue* rather than fetching the whole
    board. `gh project item-list --limit 1000` was the single most expensive
    call this hook made, and it made it from a `PreToolUse` hook -- a fresh
    process on every code-writing tool call. The targeted GraphQL read costs
    roughly one complexity point instead of hundreds.

    The disk cache is kept, and that is deliberate rather than incidental:
    a targeted-but-uncached read would swap one cheap cached board fetch per
    TTL window for a network round-trip on every single edit -- fewer quota
    points, far more requests, and latency on every operation. Cheap *and*
    cached is the point.

    Returns None when the issue carries no tier or when the board or repo
    cannot be resolved -- both keep meaning allow. Returns LOOKUP_FAILED when
    the read itself failed (harmonic-forge#656 AC6); `_main` denies on that
    unless the session is already on a high-tier model.
    """
    hinted_target = HINTED_TARGETS.get(repo_hint or "")
    if hinted_target:
        repo, number = hinted_target
    else:
        board = resolve_project_board(cwd)
        if board is None:
            return None
        repo = resolve_repo(cwd)
        if repo is None:
            return None
        _owner, number = board
    tier = read_tier(repo, issue_number, number, run=timed_run)[0]
    # A branch naming a number GitHub has no issue for carries no Tier: the
    # same allow as "no Tier set", never a LOOKUP_FAILED deny.
    return None if tier is NOT_AN_ISSUE else tier


def resolve_claude_model(transcript_path: str) -> str | None:
    """Newest model-bearing transcript entry (harmonic-forge#656: now also a
    `/model` switch or a model attachment, not only `message.model`)."""
    return session_model.transcript_model(transcript_path)


def claude_model_is_high(model: str | None) -> bool:
    """True when a Claude model string names a family in CLAUDE_HIGH_FAMILIES.

    Case-insensitive, because `session_model.current_model` can return a
    `/model` display name (`Opus 5 (1M context)`) or a settings value (`opus`)
    as well as a model id (`claude-opus-5`). None is not high.
    """
    if not model:
        return False
    lowered = model.lower()
    return any(family in lowered for family in CLAUDE_HIGH_FAMILIES)


# harmonic-forge#440: a redirection token, optionally fd-prefixed
# (`>`, `>>`, `2>`, `>>file`, `2>file.log`) but not an fd-duplication
# target (`&1`, `&2`, ...). Applied per-token after `command_segments`/
# `mask_heredoc_bodies` has already stripped heredoc bodies and split on
# shell control operators -- a token is either a real redirection or
# ordinary command-argument text, and `mask_heredoc_bodies` is what keeps
# prose *inside* a heredoc body (e.g. a comment mentioning "redirect with
# >") from ever reaching this check.
#
# `&` is itself one of `command_segments`' punctuation_chars (needed
# elsewhere to split `&&`/background `&`), and a pure-punctuation token is
# dropped entirely rather than kept as a segment's content (it only ends
# the current segment) -- so `2>&1` is not one token and the `&` does not
# survive as a token at all: `command_segments("cmd 2>&1")` returns
# `[["cmd", "2>"], ["1"]]`. A bare `_REDIRECT_TOKEN_RE` match on `"2>"`
# alone cannot tell an fd-duplication apart from a real redirection whose
# target landed in the next segment; `bash_command_writes_files` below
# does that by checking whether a *bare* redirect operator (nothing glued
# after it) is immediately followed by a next segment that is exactly one
# all-digit token -- the shape `N>&M`/`>&M` always takes once shredded.
_REDIRECT_TOKEN_RE = re.compile(r"^\d*>{1,2}([^&].*)?$")
_BARE_REDIRECT_RE = re.compile(r"^\d*>{1,2}$")
_ALL_DIGITS_RE = re.compile(r"^\d+$")

# harmonic-forge#440 (preclose-inspection finding): a redirect to a device
# that discards or is not a real persisted file. `>/dev/null 2>&1` is one
# of the single most common shapes in this exact codebase's own scripts
# (silencing a probe command) and must not cost a board lookup.
_NULL_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})

# `tee` always writes every path argument it's given (a bare `tee` with
# zero file args just echoes stdin to stdout, but that shape is not worth
# special-casing -- the false positive costs a `/model` switch, not a
# blocked command).
_WRITE_PROGRAMS = frozenset({"tee"})

# harmonic-forge#440 (preclose-inspection finding, the exact incident
# shape): an interpreter fed a script over a heredoc can write arbitrary
# files from inside that script, and `mask_heredoc_bodies` -- correctly,
# for every other purpose this module and its siblings use it for --
# replaces the body before any of this runs, so nothing here can inspect
# what the script actually does. Conservative in the direction this
# module's docstring already commits to: treat ANY of these interpreters
# reading a heredoc as a write, rather than trying to read the (masked)
# body.
_SCRIPT_INTERPRETERS = frozenset({
    "python3", "python", "python2", "node", "nodejs", "ruby", "perl",
    "bash", "sh", "zsh", "ksh",
})
_HEREDOC_MARKER_RE = re.compile(r"^<<-?['\"]?\w+['\"]?$")


def _sed_has_in_place_flag(tokens: list[str]) -> bool:
    """`sed -i`/`-i.bak`/bundled `-ie`/`--in-place[=SUFFIX]` all write the
    input file in place. Bundled short flags use the same set-membership
    check `block_irreversible_ops.py`'s `_check_git_clean` already uses for
    `git clean`'s `-fd`/`-xfd`/`-fdx` -- one convention for "is this letter
    among the bundled short flags", not a second one invented here."""
    for token in tokens:
        if token == "--in-place" or token.startswith("--in-place="):
            return True
        if token.startswith("-") and not token.startswith("--") and "i" in set(token[1:]):
            return True
    return False


def _raw_quoted_segments(command: str) -> list[list[str]]:
    """Same shape as `shell_parse.command_segments`, but tokenized with
    `shlex.shlex(..., posix=False)` so quote characters survive on each
    token instead of being stripped.

    Needed only for the redirect check below: `command_segments`'s posix
    mode dequotes `'>'` down to the same bare `>` token a real operator
    produces, so a quoted literal `>` (e.g. `grep -rn '>' .`) was
    indistinguishable from an unquoted redirect (harmonic-forge#440,
    preclose-inspection finding). Real bash agrees this distinction
    matters: a redirect operator inside quotes is not recognized as one.
    Local to this module -- `shell_parse`'s shared tokenizer stays posix
    on purpose, since every one of its other callers wants arguments
    dequoted -- so this is an additional narrow pass, not a competing
    general-purpose parser.
    """
    punctuation = ";&|()\n"
    lexer = shlex.shlex(mask_heredoc_bodies(command), posix=False, punctuation_chars=punctuation)
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


def bash_command_writes_files(command: str) -> bool:
    """True if this Bash command's own text can write to the filesystem.

    Conservative by construction, matching this directory's established
    posture (see `block_irreversible_ops.py`'s module docstring): this
    hook denies a code-writing tool call, so a false negative here just
    means the gate stays silent on a write it should have caught (the
    same fail-open direction every other resolution failure in this module
    already takes), while a false positive only costs an unnecessary
    `/model` switch. Static parsing cannot resolve command substitution,
    aliases, or a function -- not attempted here, same as elsewhere in
    this directory.
    """
    try:
        segments = command_segments(command)
        raw_segments = _raw_quoted_segments(command)
    except ValueError:
        return False
    for seg_index, raw_tokens in enumerate(segments):
        raw_quoted = raw_segments[seg_index] if seg_index < len(raw_segments) else []
        for tok_index, token in enumerate(raw_tokens):
            if not _REDIRECT_TOKEN_RE.match(token):
                continue
            quoted_form = raw_quoted[tok_index] if tok_index < len(raw_quoted) else token
            if quoted_form[:1] in ("'", '"'):
                continue  # a quoted literal (e.g. `grep '>' file`), not a real redirect
            if tok_index == len(raw_tokens) - 1 and _BARE_REDIRECT_RE.match(token):
                next_segment = segments[seg_index + 1] if seg_index + 1 < len(segments) else None
                if next_segment and len(next_segment) == 1 and _ALL_DIGITS_RE.match(next_segment[0]):
                    continue  # `N>` + next segment `M` == shredded `N>&M`, an fd duplication
            target = _REDIRECT_TOKEN_RE.match(token).group(1) or ""
            if target and target in _NULL_TARGETS:
                continue  # writes to a device that discards, not a real file
            if not target and tok_index + 1 < len(raw_tokens) and raw_tokens[tok_index + 1] in _NULL_TARGETS:
                continue  # bare `>` `/dev/null` as two tokens
            return True
        tokens = strip_invocation_prefix(raw_tokens)
        if not tokens:
            continue
        program = os.path.basename(tokens[0])
        if program in _WRITE_PROGRAMS:
            return True
        if program == "sed" and _sed_has_in_place_flag(tokens[1:]):
            return True
        if program in _SCRIPT_INTERPRETERS and any(_HEREDOC_MARKER_RE.match(t) for t in tokens[1:]):
            return True
    return False


def required_tier_met(payload: dict, high_required: bool) -> bool:
    if "model" in payload:  # Codex: model is a direct field
        model = payload["model"]
        is_high = CODEX_HIGH in model
    else:  # Claude Code: no model on the payload (harmonic-forge#656 AC4)
        model = session_model.current_model(
            payload.get("transcript_path", ""), payload.get("cwd") or "",
            payload.get("session_id"),
        )
        if model is None:
            # harmonic-forge#630 AC4: fail open, but say so -- this is the
            # exact silent path the issue named ("a deep issue worked while
            # the board is briefly unreachable is ungated and nothing
            # anywhere records it").
            print("model_tier_gate: session model unresolvable, failing "
                  "open (tier requirement not enforced this call)",
                  file=sys.stderr)
            return True  # fail open -- can't resolve, don't block
        is_high = claude_model_is_high(model)
    return is_high if high_required else True


def _main() -> None:
    if os.environ.get("LANE_MODEL"):
        _allow()  # explicit operator override

    if os.environ.get("LANE") == "3":
        _allow()  # harmonic-forge#440: Lane 3 verifies, never designs/implements

    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        _allow()

    tool_name = payload.get("tool_name")
    if tool_name == "Bash":
        command = (payload.get("tool_input") or {}).get("command", "")
        if not bash_command_writes_files(command):
            _allow()  # no board lookup for a command that writes nothing
    elif tool_name not in ("Edit", "Write", "MultiEdit", "apply_patch"):
        _allow()

    # harmonic-forge#630: resolve the issue from the WRITE TARGET when this
    # tool call has one, falling back to the session cwd only when it does
    # not (Bash -- `bash_command_writes_files` proves a write happens, never
    # where -- or an Edit/Write/apply_patch payload this hook can't parse).
    # A session sitting in `main` editing a `deep`-tier worktree by absolute
    # path is gated on THAT worktree's issue now, not on `main`'s (no issue).
    write_dirs = write_target_dirs(payload)
    resolve_dirs = write_dirs if write_dirs is not None else [payload.get("cwd") or os.getcwd()]

    # AC3: a single call CAN name more than one issue (apply_patch touching
    # two worktrees in one patch). Every resolved target is kept, deduped by
    # (issue_number, repo_hint) -- never silently narrowed to one -- and the
    # call is gated on the WORST outcome across all of them below.
    resolved: list[tuple[str, int, str | None]] = []
    seen_issues: set[tuple[int, str | None]] = set()
    for d in resolve_dirs:
        target = resolve_issue_target(d)
        if target is None or target in seen_issues:
            continue
        seen_issues.add(target)
        resolved.append((d, target[0], target[1]))

    if not resolved:
        # harmonic-forge#630 AC4 (preclose finding 3): a write TARGET was
        # named but none of its candidate directories resolved to a
        # tracked issue -- most commonly a genuinely untracked path, but
        # also the residual case of a resolvable-in-principle worktree
        # that `resolve_issue_target` could not read (deleted mid-session,
        # a `git` error). Both look identical from here; say so on stderr
        # either way rather than let a deep-tier write through in silence.
        if write_dirs is not None:
            print(f"model_tier_gate: no tracked issue resolved from "
                  f"{len(resolve_dirs)} write target(s) ({resolve_dirs}); "
                  f"allowing", file=sys.stderr)
        _allow()

    lookup_failed_issue: int | None = None
    escalating_issue: int | None = None
    escalating_tier: str | None = None
    for d, issue_number, repo_hint in resolved:
        tier = resolve_tier(d, issue_number, repo_hint)
        if tier is LOOKUP_FAILED:
            lookup_failed_issue = issue_number
            break  # a failed lookup is the worst outcome any target can have
        if tier in ESCALATING_TIERS and escalating_issue is None:
            escalating_issue = issue_number
            escalating_tier = tier

    if lookup_failed_issue is not None:
        # harmonic-forge#656 AC6: fail closed on a failed read, but only for a
        # session not already on a high-tier model -- a high model satisfies
        # any Tier, so the unknown Tier cannot matter. Deliberately names no
        # override.
        if required_tier_met(payload, high_required=True):
            _allow()
        _deny(
            f"Tier lookup failed for issue #{lookup_failed_issue}; refusing a "
            f"code write rather than risking a deep issue on the wrong "
            f"model. A high-tier model (`/model opus`) is not affected."
        )
    if escalating_issue is None:
        _allow()

    if required_tier_met(payload, high_required=True):
        _allow()

    switch_cmd = "/model gpt-5.6-sol" if "model" in payload else "/model opus"
    _deny(
        f"Issue #{escalating_issue} is Tier '{escalating_tier}' "
        f"-- harmonic-forge#202 requires the high-tier model for this work. "
        f"Run `{switch_cmd}` and retry, or set LANE_MODEL to override."
    )


def main() -> None:
    # This must never wedge a lane over its own telemetry breaking -- any
    # unexpected exception (missing `git` on PATH, a malformed payload
    # shape, etc.) falls through to allow rather than crashing the hook
    # process, which would otherwise deny-by-side-effect (a nonzero exit
    # with no permissionDecision is not the same as an explicit allow, and
    # differs by host). Found live, harmonic-forge#202 verification pass.
    try:
        _main()
    except SystemExit:
        raise
    except Exception:
        _allow()


if __name__ == "__main__":
    main()
