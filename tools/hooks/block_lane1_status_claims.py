#!/usr/bin/env python3
"""Deny raw GitHub issue-post transports; use the project capability wrappers.

Canonical copy (harmonic-forge#149) — lives here, not inside any project
worktree, so it can't go stale-by-branch: a project's `.claude/settings.json`
should invoke this file directly (or a thin in-repo shim that execs it), never
a copy checked out on a feature branch cut before a fix to this file landed.

An omission-class transition cannot be reliably inferred from free-form prose:
quoted examples and real claims are syntactically indistinguishable.  This hook
therefore gates the transport, never opens a comment body, and leaves all
validation/attestation to ``mise run l1-post`` and ``mise run post-comment``.

Also denies GitHub's native auto-close keyword syntax ("Closes #N", "Fixes
owner/repo#N", etc.) in any `gh pr create`/`gh pr edit` body or body-file —
that syntax closes the referenced issue automatically on merge, with no
`gh issue close` call ever appearing in Lane 1's own tool-call log, bypassing
the "no lane closes without the operator's explicit command" rule from a
different angle than the direct-`gh issue close` case (harmonic-forge#84/#85)
already guards. See harmonic-forge#136 for the live incident this was added
for — caught only because the operator happened to notice before the PR
merged, not because any guard existed at the time.

Also denies `mise run gate-checkout`/`gate-restart`/`gate-e2e` — categorically
Lane-3-only per `.devin/skills/lane3-gate/SKILL.md`. Three-case `LANE`
precedence (harmonic-forge#151, restructured from a marker-only check):
`LANE == "3"` allows outright; `LANE` set to anything else denies
unconditionally, even with a fresh `LANE3_ACTIVE` marker present (fixes a
live hole in the original design — a Lane 2 session `cd`d into a `-lane3`
worktree within the marker's 12h TTL got these tasks allowed, the exact
harmonic-forge#138 collision class this guard exists to prevent); `LANE`
unset falls back to the original fresh-marker check (written by `mise run
lane3-begin`), preserving the escape hatch for sessions not launched via
the `lane3` wrapper. Non-adversarial by design, matching this project's
existing tolerance for self-identification guards: the goal is catching an
accidental role mix-up (confirmed live, harmonic-forge#138 — a Lane 2
session ran `gate-checkout` directly against a live shared worktree "to
verify" something, briefly corrupting its state), not stopping a
deliberately dishonest session.

Also denies `Edit`/`Write` calls into the main checkout root when this
process's own `LANE` environment variable is `"2"` (harmonic-forge#142).
`LANE` is set once, at session launch, by harmonic-forge's
`tools/lane/lane2` script — never inferred from conversation text or
written by a marker file the session controls mid-session. Two earlier
designs for this same guard (a self-declared marker, and a marker armed
by regex-matching the operator's own trigger phrases) both failed
pitch-inspection review for structural reasons — see harmonic-forge#142's
comment history. `LANE` is inherently scoped to exactly the process it
was set for, for that process's entire lifetime, which is what makes this
version different: nothing about it depends on the session's own
compliance or on parsing free-form text. Real incident this guards
against: hrse#317/#318, a Claude Code Lane 2 session implementing
directly in the shared main checkout instead of its own dedicated
worktree.

The main-checkout root is resolved dynamically (harmonic-forge#149), not
hardcoded to one project: `resolve_main_checkout_root()` derives it from
the calling session's own cwd, the same way the `lane1`/`lane2`/`lane3`
launcher scripts do (`git rev-parse --show-toplevel`, strip a trailing
`-lane<N>` suffix) — so this one canonical file works correctly for any
project, not just HRSE2.

Also denies `Edit`/`Write` calls anywhere outside
`~/Harmonic_Projects/testplan/` when this process's own `LANE`
environment variable is `"3"` (harmonic-forge#150) — the first
mechanical enforcement of Lane 3's "never fixes anything, ever" rule
for Claude Code sessions, mirroring the hard tool-level enforcement
Devin has had all along (`.devin/agents/lane3-gate/AGENT.md`). This
Claude-Code-side hook has no true Codex-side mechanical counterpart for
general file-write scoping — `~/.codex/agents/lane3-gate.toml` is a
subagent-spawn config, never applied to a real top-level Lane 3 session
(confirmed live, harmonic-forge#184); Codex's actual working mechanism
(harmonic-forge#152) covers command-shaped mutations via `PreToolUse`,
a narrower scope than the general file-write deny this hook implements
for Claude Code. Same `LANE` mechanism as the Lane 2 guard above,
deny-by-default instead of deny-one-place, since Lane 3 has no
legitimate write target besides gate artifacts.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HRSE_REPO = "vitalharmony/hrse"
REPO_SETTING = re.compile(r'(?m)^\s*GH_REPO\s*=\s*"([^"]+)"\s*$')
HEREDOC_START = re.compile(
    r"<<(?P<strip_tabs>-?)\s*(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)"
)
AUTOCLOSE_KEYWORD = re.compile(
    r"\b(?:clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed))\b\s+"
    r"(?:[\w.-]+/[\w.-]+#\d+|#\d+)",
    re.IGNORECASE,
)
LANE3_ONLY_TASKS = {"gate-checkout", "gate-restart", "gate-e2e"}
LANE3_MARKER_MAX_AGE_SECONDS = 12 * 60 * 60
EDIT_WRITE_TOOLS = {"Edit", "Write"}
LANE_WORKTREE_SUFFIX = re.compile(r"^(.+)-lane\d+$")
TESTPLAN_ROOT = (Path.home() / "Harmonic_Projects" / "testplan").resolve()


def resolve_main_checkout_root(cwd: Path) -> Path | None:
    """Resolve the current project's main checkout root from cwd — the same
    derivation harmonic-forge's lane1/lane2/lane3 launcher scripts use: find
    the enclosing git worktree, strip a trailing -lane<N> suffix from its
    basename. Returns None if cwd isn't inside a git repo (fails open — this
    is a non-adversarial guard, not a hard security boundary)."""
    result = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        return None
    root = Path(result.stdout.strip())
    match = LANE_WORKTREE_SUFFIX.match(root.name)
    base_name = match.group(1) if match else root.name
    return (root.parent / base_name).resolve()


def repo_from_cwd(cwd: Path) -> str | None:
    """Resolve a project's repo from the nearest mise.toml."""
    for directory in (cwd, *cwd.parents):
        try:
            match = REPO_SETTING.search((directory / "mise.toml").read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError):
            continue
        if match:
            return match.group(1)
    return None


def command_repo(
    args: list[str],
    cwd: Path | None = None,
    prefer_cwd: bool = False,
) -> tuple[list[str], str]:
    """Return command args and the repo resolved from flags or environment."""
    repo: str | None = None
    while args and "=" in args[0] and not args[0].startswith("="):
        name, value = args[0].split("=", 1)
        if name == "GH_REPO":
            repo = value
        args = args[1:]
    for index, item in enumerate(args):
        if item == "--repo" and index + 1 < len(args):
            repo = args[index + 1]
        elif item.startswith("--repo="):
            repo = item.partition("=")[2]
    if repo in {"$GH_REPO", "${GH_REPO}"}:
        repo = os.environ.get("GH_REPO")
    cwd_repo = repo_from_cwd(cwd) if cwd is not None else None
    if prefer_cwd and cwd_repo and repo is None:
        repo = cwd_repo
    return args, repo or os.environ.get("GH_REPO") or cwd_repo or HRSE_REPO


def is_direct_transport(
    args: list[str],
    cwd: Path | None = None,
    prefer_cwd: bool = False,
) -> bool:
    """Recognize the executed command, never a quoted command-shaped string."""
    args, repo = command_repo(args, cwd, prefer_cwd)
    if len(args) >= 3 and args[:3] == ["gh", "issue", "comment"]:
        return True
    if len(args) >= 3 and args[:3] == ["gh", "issue", "create"]:
        return True
    if len(args) >= 3 and args[:3] == ["gh", "issue", "edit"]:
        return any(item in {"--body", "--body-file"} for item in args[3:])
    if len(args) >= 3 and args[:3] == ["mise", "run", "post-comment"]:
        return repo == HRSE_REPO
    if len(args) >= 2 and args[:2] == ["gh", "api"]:
        endpoint = next((item.lstrip("/") for item in args[2:] if "/issues/" in item), "")
        is_issue_comment = bool(re.fullmatch(
            r"repos/[^/]+/[^/]+/issues/(?:\d+/comments|comments/\d+)", endpoint
        ))
        method = next((args[index + 1].upper() for index, item in enumerate(args[:-1])
                       if item in {"-X", "--method"}), "")
        implicit_post = any(item in {"-f", "-F", "--raw-field", "--field", "--input"} for item in args)
        return is_issue_comment and (method in {"POST", "PATCH"} or (not method and implicit_post))
    is_post_comment_script = bool(args and Path(args[0]).name == "post_comment.py") or (
        len(args) >= 2 and args[0].startswith("python") and Path(args[1]).name == "post_comment.py"
    )
    return is_post_comment_script and repo == HRSE_REPO


def pr_body_autoclose_text(args: list[str], cwd: Path) -> str | None:
    """If args is a `gh pr create`/`gh pr edit` call carrying a --body or
    --body-file value that contains GitHub auto-close keyword syntax,
    return the matched text. Returns None for every other command, or if
    no such text is found. Does not resolve heredoc-substituted --body
    values (masked upstream) — --body-file is the reliable path."""
    if len(args) < 3 or args[0] != "gh" or args[1] != "pr" or args[2] not in {"create", "edit"}:
        return None
    for index, item in enumerate(args):
        value: str | None = None
        if item == "--body" and index + 1 < len(args):
            value = args[index + 1]
        elif item.startswith("--body="):
            value = item.partition("=")[2]
        elif item == "--body-file" and index + 1 < len(args):
            path = Path(args[index + 1]).expanduser()
            if not path.is_absolute():
                path = cwd / path
            try:
                value = path.read_text(encoding="utf-8")
            except OSError:
                continue
        elif item.startswith("--body-file="):
            path = Path(item.partition("=")[2]).expanduser()
            if not path.is_absolute():
                path = cwd / path
            try:
                value = path.read_text(encoding="utf-8")
            except OSError:
                continue
        if value:
            match = AUTOCLOSE_KEYWORD.search(value)
            if match:
                return match.group(0)
    return None


def lane3_task_without_marker(args: list[str], cwd: Path) -> str | None:
    """If args invokes a Lane-3-only mise task and this session isn't
    cleared to run it, return the task name; otherwise None.

    Three-case LANE precedence (harmonic-forge#151, restructured from the
    original marker-only check):

    1. LANE == "3" — allow outright, no marker, no `lane3-begin` ritual.
       Positive evidence of the right role.
    2. LANE set and != "3" — deny unconditionally, marker ignored even if
       fresh. This is the actual fix: the old marker-only check had a
       live hole here — a Lane 2 session that `cd`s into a `-lane3`
       worktree within the marker's 12h TTL got `gate-checkout` allowed,
       the exact harmonic-forge#138 collision class this guard exists to
       prevent. Positive evidence of the *wrong* role must not be
       overridable by a file the session itself can touch.
    3. LANE unset — fall back to the original fresh-marker check,
       unchanged. Preserves the escape hatch for sessions not launched
       via the `lane3` wrapper (`claude --resume`, an operator-attended
       run, any future non-wrapped entry point) — LANE unset is genuinely
       ambiguous, not evidence of the wrong role, so it must not be
       treated as a hard deny the way case 2 is."""
    if len(args) < 3 or args[0] != "mise" or args[1] != "run":
        return None
    task = args[2]
    if task not in LANE3_ONLY_TASKS:
        return None
    lane = os.environ.get("LANE")
    if lane == "3":
        return None
    if lane is not None:
        return task
    git_dir = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=cwd, text=True, capture_output=True, check=False,
    )
    if git_dir.returncode:
        return task
    marker = Path(git_dir.stdout.strip()) / "LANE3_ACTIVE"
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return task
    if age > LANE3_MARKER_MAX_AGE_SECONDS:
        return task
    return None


def lane2_write_in_main_checkout(file_path: str, cwd: Path) -> bool:
    """True if this hook invocation's own process has LANE=2 (set at
    session launch by harmonic-forge's `tools/lane/lane2` script,
    harmonic-forge#142) and file_path resolves inside the current
    project's main checkout root (resolved dynamically from cwd,
    harmonic-forge#149 — not hardcoded to one project). No marker file,
    no session bookkeeping — the env var is inherently scoped to exactly
    the process it was set for.

    Checks both the lexically-normalized path and the symlink-resolved
    path: a project's main checkout can contain real symlinks out to
    ~/harmonic-forge (e.g. HRSE2's `.claude/rules/backend-python.md` and
    siblings) — resolving only the symlinked form would let a LANE=2
    session edit those platform-rule files through an in-checkout path
    undetected. Fails open (returns False, i.e. allows) on any path it
    cannot resolve, or if cwd isn't inside a git repo at all — this is a
    non-adversarial, accidental-mix-up guard, same posture as the
    existing LANE3_ACTIVE check above, not a hard security boundary."""
    if os.environ.get("LANE") != "2":
        return False
    if not file_path:
        return False
    main_checkout_root = resolve_main_checkout_root(cwd)
    if main_checkout_root is None:
        return False
    try:
        raw = Path(file_path).expanduser()
        if not raw.is_absolute():
            raw = cwd / raw
        lexical = Path(os.path.normpath(raw))
        resolved = raw.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    for candidate in (lexical, resolved):
        try:
            candidate.relative_to(main_checkout_root)
            return True
        except ValueError:
            continue
    return False


def lane3_write_outside_testplan(file_path: str) -> bool:
    """True if this hook invocation's own process has LANE=3 (set at
    session launch by harmonic-forge's `tools/lane/lane3` script,
    harmonic-forge#150) and file_path resolves OUTSIDE TESTPLAN_ROOT —
    Lane 3's only legitimate write target, for gate artifacts too large
    for an issue comment. This is the first mechanical enforcement of the
    "never fixes anything, ever" rule for Claude Code, closing a gap
    Devin has had a hard profile for all along
    (`.devin/agents/lane3-gate/AGENT.md`). Codex has no true equivalent
    for general file-write scoping — `~/.codex/agents/lane3-gate.toml` is
    a subagent-spawn config, never applied to a real top-level Lane 3
    session (harmonic-forge#184); Codex's actual working mechanism
    (harmonic-forge#152) only covers command-shaped mutations.

    Deny-by-default (inverted from `lane2_write_in_main_checkout`, which
    denies one specific place): Lane 3 has no legitimate write target
    besides testplan artifacts, so anywhere else is denied. Requires
    BOTH the lexically-normalized and the symlink-resolved form of the
    path to fall inside TESTPLAN_ROOT before allowing — a symlink
    inside testplan pointing outside it must not be usable to escape
    the boundary. Still fails open (allows) on any path this can't
    resolve at all, for consistency with this file's non-adversarial
    posture elsewhere — a path-resolution edge case should not itself
    lock out a session; it is not a hard security boundary."""
    if os.environ.get("LANE") != "3":
        return False
    if not file_path:
        return False
    try:
        raw = Path(file_path).expanduser()
        if not raw.is_absolute():
            raw = Path.cwd() / raw
        lexical = Path(os.path.normpath(raw))
        resolved = raw.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    for candidate in (lexical, resolved):
        try:
            candidate.relative_to(TESTPLAN_ROOT)
        except ValueError:
            return True
    return False


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


def denial(message: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        },
        "systemMessage": message,
    }


def decision(command: object) -> dict:
    if not isinstance(command, str):
        return denial("Blocked: malformed Bash hook payload; refusing to bypass Lane 1 posting controls.")
    try:
        segments = command_segments(command)
    except (AttributeError, TypeError, ValueError):
        return denial("Blocked: malformed shell command; refusing to bypass Lane 1 posting controls.")
    effective_cwd = Path.cwd()
    for segment in segments:
        if len(segment) == 2 and segment[0] == "cd":
            target = Path(segment[1]).expanduser()
            effective_cwd = target if target.is_absolute() else effective_cwd / target
            continue
        autoclose_match = pr_body_autoclose_text(segment, effective_cwd)
        if autoclose_match is not None:
            return denial(
                f"Blocked: PR body contains GitHub auto-close syntax "
                f"({autoclose_match!r}). This closes the referenced issue on "
                "merge with no explicit operator command in the loop — "
                "against 'no lane closes without Marc's explicit command, "
                "ever' (harmonic-forge#136). Reference the issue without a "
                "closing keyword (e.g. 'Related to #N') and close it "
                "explicitly, by command, after merge."
            )
        unmarked_task = lane3_task_without_marker(segment, effective_cwd)
        if unmarked_task is not None:
            lane = os.environ.get("LANE")
            if lane is not None:
                return denial(
                    f"Blocked: {unmarked_task!r} is Lane-3-only, and this "
                    f"session was launched as Lane {lane} (harmonic-forge#151). "
                    "Restart it via harmonic-forge's `lane3` script if you "
                    "genuinely need to run this as Lane 3 — a LANE3_ACTIVE "
                    "marker cannot override an explicit LANE set to "
                    "anything else."
                )
            return denial(
                f"Blocked: {unmarked_task!r} is Lane-3-only (see its "
                "`mise.toml` description) and no fresh LANE3_ACTIVE marker "
                "exists for this worktree (harmonic-forge#138). If you are "
                "genuinely running as Lane 3 for this worktree, run `mise "
                "run lane3-begin` once first, then retry."
            )
        if is_direct_transport(segment, effective_cwd, prefer_cwd=True):
            if os.environ.get("LANE") in (None, "1"):
                break
            continue
    else:
        return {}
    return denial(
            "Blocked: raw GitHub issue posting bypasses the Lane 1 capability "
            "wrappers. Use `mise run l1-post` for a protocol artifact or "
            "`mise run lane-comment` for ordinary discussion (hrse#457). This "
            "restriction is Lane-1-specific (harmonic-forge#190) — Lane 2/3 "
            "sessions (`LANE=2`/`LANE=3`) may post their own results directly."
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("{}")
        return
    tool_name = payload.get("tool_name")
    cwd = Path(payload.get("cwd") or Path.cwd())
    if tool_name == "Bash":
        command = (payload.get("tool_input") or {}).get("command", "")
        print(json.dumps(decision(command)))
        return
    if tool_name in EDIT_WRITE_TOOLS:
        file_path = (payload.get("tool_input") or {}).get("file_path", "")
        if lane2_write_in_main_checkout(file_path, cwd):
            print(json.dumps(denial(
                "Blocked: this session was launched as Lane 2 (LANE=2) "
                "and is writing directly into the main checkout "
                "(harmonic-forge#142). Lane 2 work belongs in its own "
                "dedicated worktree — restart in the project's -lane2 "
                "worktree or a fresh /tmp/<project>-<issue>-impl worktree, "
                "not the main checkout."
            )))
            return
        if lane3_write_outside_testplan(file_path):
            print(json.dumps(denial(
                "Blocked: this session was launched as Lane 3 (LANE=3) "
                "and Lane 3 never fixes anything, ever, under any "
                "circumstance (harmonic-forge#150). The only writable "
                "path is ~/Harmonic_Projects/testplan/, for gate "
                "artifacts too large for an issue comment. Record the "
                "failure and report it for Lane 2 to fix instead."
            )))
            return
        print("{}")
        return
    print("{}")


if __name__ == "__main__":
    main()
