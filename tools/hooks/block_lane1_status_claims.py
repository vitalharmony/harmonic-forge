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

Both of those path predicates also apply on the `Bash` surface
(harmonic-forge#458). They did not, originally, and the gap was found the
only way it could be: a LANE=2 session wrote a protected file with a
`python3` script after the `Edit` tool denied it. Reproduced live, all six
shapes — `Edit` and `Write` denied; a scripted write, a heredoc script, and
a bare `echo x > <protected>` redirect all allowed. The bare redirect is
the finding that mattered: the issue was filed as a scripted-write bypass,
but the simplest possible shell construct did it with no script at all.

Note that broadening the tool matcher — the obvious fix, and the one the
handoff proposed — would not have caught the reported incident. The
matcher was ALREADY `Bash` in the checkout the write ran from; the hook was
invoked and chose not to block, because its `Bash` branch only ever checked
transports. One predicate, two surfaces, wired to one of them. The matcher
was separately missing in harmonic-forge's own settings, which is fixed in
the same change, but it was the second half of the bug, not the first.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shell_parse import (  # noqa: E402  (harmonic-forge#167)
    command_segments,
    strip_invocation_prefix,
)

HRSE_REPO = "vitalharmony/hrse"
REPO_SETTING = re.compile(r'(?m)^\s*GH_REPO\s*=\s*"([^"]+)"\s*$')
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

#: Shell builtins that change the working directory. `cd` was the only one
#: recognized until harmonic-forge#529; `pushd` changes directory identically
#: and was invisible, so a relative write target after one resolved against the
#: session's own worktree and passed the guard.
DIR_CHANGE_VERBS = {"cd", "pushd"}

#: Flags `cd`/`pushd` accept before their operand. Their absence from the old
#: match is why `cd -P <dir>` was ignored entirely: the segment is three tokens,
#: the check required exactly two, and the whole directory change was dropped.
#: That failed in the dangerous direction -- adding a flag made the guard blind
#: while the command still ran.
DIR_CHANGE_FLAGS = {"-L", "-P", "-e", "-@"}

#: A token carrying a value no static read can know. `cd "$D"` and a loop's
#: `cd "$d"` are the shapes measured live on harmonic-forge#529.
DYNAMIC_TOKEN = re.compile(r"[$`]")

#: Shell keywords that can precede a command inside a compound statement.
#: `command_segments` keeps them attached, so a loop body's directory change
#: arrives as `['do', 'cd', '$d']` — the verb is not token 0, and the whole
#: change was missed (harmonic-forge#529, measured live).
COMPOUND_KEYWORDS = {"do", "then", "else", "elif", "{", "(", "!"}

#: Shells that take a script as a `-c` argument. The outer parse never enters
#: that string, so a directory change inside it is unobservable.
NESTED_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}

#: A directory-change verb at the start of a nested `-c` script or after a
#: command separator inside it.
NESTED_DIR_CHANGE = re.compile(
    r"(?:^|[;&|(]\s*)(?:" + "|".join(sorted(DIR_CHANGE_VERBS)) + r")\s")

# --- Bash-surface write detection (harmonic-forge#458) ----------------------
#: A standalone redirect operator: `>`, `>>`, `2>`, `&>`. The target is the
#: NEXT token. `shell_parse.command_segments` does not treat `>` as
#: punctuation, so both this and the glued form below are needed.
REDIRECT_TOKEN = re.compile(r"^(?:[0-9]*|&)>>?$")

#: The glued form `echo x>/path` / `2>/dev/null`, which arrives as one token.
#: Rejects any token containing whitespace, so a quoted `-m "a > b"` commit
#: message is never mistaken for a redirect.
GLUED_REDIRECT = re.compile(r"^(?P<pre>[^>\s]*)>{1,2}(?P<target>[^>\s]+)$")

#: Interpreters whose one-liners/heredocs can write without any shell-visible
#: write construct — the shape that produced this issue's incident.
INTERPRETERS = {"python", "python3", "node", "nodejs", "ruby", "perl",
                "bash", "sh", "zsh"}

#: `INTERPRETER_WRITE_VERB` and `PATH_CANDIDATE` lived here until
#: harmonic-forge#522. They were an uncorrelated pair — "some write verb
#: anywhere" AND "every path-shaped run anywhere" — and their replacement,
#: `INTERPRETER_WRITE_PAIR`, matches a construct together with its own operand.
#: Do not reintroduce a bare path-harvesting regex here: harvesting paths that
#: no write construct names is the entire defect that issue records.

#: Size-shaped operands (`truncate -s 0`), never paths.
SIZE_OPERAND = re.compile(r"^[+\-<>/%]?\d+[KMGTPkmgtp]?[Bb]?$")


def resolve_main_checkout_root(cwd: Path) -> Path | None:
    """Resolve the current project's main checkout root from cwd — the same
    derivation harmonic-forge's lane1/lane2/lane3 launcher scripts use: find
    the enclosing git worktree, strip a trailing -lane<N> suffix from its
    basename. Returns None if cwd isn't inside a git repo (fails open — this
    is a non-adversarial guard, not a hard security boundary).

    Kept as the single-root accessor; `protected_checkout_roots` below is the
    full answer and the one the predicates use (harmonic-forge#529)."""
    roots = protected_checkout_roots(cwd)
    return roots[0] if roots else None


def protected_checkout_roots(cwd: Path) -> list[Path]:
    """Every checkout root a Lane 2 session must not write into, from cwd.

    Three signals, in this order, because no single one is right on its own
    (harmonic-forge#529, AC5):

    1. **`--git-common-dir`'s parent, when cwd is a LINKED worktree.** This is
       the shared main working tree by construction, and it is the signal the
       old derivation lacked. Stripping a `-lane<N>` suffix was the only rule,
       so a `/tmp/<repo>-<issue>-impl` worktree — the working directory the
       protocol REQUIRES a Lane 2 session to use — matched nothing, kept its
       own basename, and was returned as its own main checkout. Every shell
       write a Lane 2 session made inside its own impl worktree was then denied
       as "writes into the main checkout." Measured live 2026-09-09 on
       `/tmp/hrse2-1730-impl`; also the true cause of two occurrences this
       issue's original plan misattributed to `cd`-tracking.

    2. **The `-lane<N>` sibling, when the basename carries that suffix.** Not
       redundant with (1): a lane worktree set up as an independent clone
       rather than `git worktree add` has its own `.git`, so git reports it as
       its own main tree and only the naming convention connects it to the
       checkout it shadows. Dropping this would silently stop protecting that
       arrangement.

    3. **cwd's own toplevel, when neither of the above produced anything.**
       That is the main checkout itself, or an unrelated repo — both protected,
       exactly as before.

    A disposable `-impl` worktree reaches (1) and stops, so it is never in the
    list. That is the whole of AC5."""
    toplevel_result = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, check=False,
    )
    if toplevel_result.returncode:
        return []
    toplevel = Path(toplevel_result.stdout.strip()).resolve()
    roots: list[Path] = []
    common = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute",
         "--git-common-dir"],
        text=True, capture_output=True, check=False,
    )
    if not common.returncode:
        common_dir = Path(common.stdout.strip())
        if common_dir.name == ".git":
            shared = common_dir.parent.resolve()
            if shared != toplevel:
                roots.append(shared)
    match = LANE_WORKTREE_SUFFIX.match(toplevel.name)
    if match:
        sibling = (toplevel.parent / match.group(1)).resolve()
        if sibling not in roots:
            roots.append(sibling)
    if not roots:
        roots.append(toplevel)
    return roots


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


_BULK_COMMENTS_URL_RE = re.compile(r"/issues/\d+/comments(?:$|[/?])")

_FETCH_LANE1_CONTEXT_HINT = (
    "reads Lane 2's comment bodies too -- there is no server-side filter by "
    "author/role, and every observed contamination on this project (hrse#793, "
    "five separate Lane 3 sessions) came from exactly this class of command. "
    "Use `python3 ~/harmonic-forge/tools/gh/fetch_lane1_context.py --repo "
    "OWNER/REPO --issue N` instead, or fetch one already-known comment ID "
    "directly via `gh api repos/OWNER/REPO/issues/comments/<id>` "
    "(harmonic-forge#253/hrse#824/harmonic-forge#258)."
)


def bulk_comment_read_denial(segment: list[str]) -> str | None:
    """harmonic-forge#260: the fifth Lane 3 contamination incident on
    hrse#793 came through a Claude Code Lane 3 session -- #258 added this
    exact check to the Codex-side gate_codex_tool.py hook only, and this
    canonical Claude-Code-side hook (the one `.claude/settings.json`
    actually wires for a Claude Lane 3 session) had no equivalent at all,
    so `gh issue view --comments` sailed through unblocked. Mirrors
    gate_codex_tool.py's bulk_comment_read_denial() exactly -- keep the
    two in sync if either changes. Only called when LANE == "3" (see
    call site in decision())."""
    if not segment or segment[0] != "gh":
        return None
    if "issue" in segment and "view" in segment and "--comments" in segment:
        return f"`gh issue view --comments` {_FETCH_LANE1_CONTEXT_HINT}"
    if segment[1:2] == ["api"]:
        for arg in segment[2:]:
            if _BULK_COMMENTS_URL_RE.search(arg):
                return f"a bulk `gh api .../issues/<N>/comments` listing {_FETCH_LANE1_CONTEXT_HINT}"
    return None


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
    main_checkout_roots = protected_checkout_roots(cwd)
    if not main_checkout_roots:
        return False
    try:
        raw = Path(file_path).expanduser()
        if not raw.is_absolute():
            raw = cwd / raw
        lexical = Path(os.path.normpath(raw))
        resolved = raw.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    for main_checkout_root in main_checkout_roots:
        for candidate in (lexical, resolved):
            try:
                candidate.relative_to(main_checkout_root)
                return True
            except ValueError:
                continue
    return False


def write_on_main_branch(file_path: str, cwd: Path) -> bool:
    """True if file_path is a TRACKED file inside a git worktree that
    currently has `main` checked out by name (harmonic-forge#384).

    Identity-independent, unlike `lane2_write_in_main_checkout` above: this
    fires regardless of `LANE` -- set to anything, or unset entirely. Lane 1
    sets no `LANE` at session launch, so it had no equivalent guard; a
    docs-only edit landed directly in the main checkout on `main` and was
    only caught later, at `git commit`, by a *different* guard
    (`block_stale_script_execution.py`'s "direct commits to 'main' are
    blocked"), after the edit had already landed in the working tree
    (harmonic-forge#384, live 2026-08-27). This is a pure git-state check:
    it cannot be defeated by an unset environment variable, and it fails in
    the direction the project's own documented rule already requires --
    branch before the first Edit/Write.

    Resolved from file_path's own directory, not cwd -- a session can `cd`
    away from the worktree it is editing into (the hook payload's own `cwd`
    field is best-effort, same caveat `resolve_main_checkout_root` above
    already documents). Untracked files are never blocked (`git ls-files
    --error-unmatch`): a brand-new file about to be created, or a scratch/
    gitignored path, is not what this guard exists to stop -- only editing
    a tracked file while `main` is checked out is the violation the
    "creating files and branching afterward is the same violation" rule
    names. A detached HEAD returns `""` from `git branch --show-current`,
    never the literal string `"main"`, so the Lane 3 `gate-checkout`
    fallback (which detaches rather than checking out `main` by name) is
    correctly not treated as `main` here.

    Fails open (returns False) on any path or git-state resolution it
    cannot complete -- same non-adversarial posture as every other guard in
    this file. This is an ADDITIONAL condition alongside
    `lane2_write_in_main_checkout`, not a replacement: that check still
    fires independently for a LANE=2 session, regardless of which branch is
    checked out."""
    if not file_path:
        return False
    try:
        raw = Path(file_path).expanduser()
        if not raw.is_absolute():
            raw = cwd / raw
        lexical = Path(os.path.normpath(raw))
        resolved = raw.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    # Check BOTH the lexical path and the symlink-resolved path, same as
    # `lane2_write_in_main_checkout` above and for the identical reason
    # (harmonic-forge#384 preclose review, live-reproduced): a project's
    # checkout can contain real symlinks out to another repo entirely --
    # HRSE2's `.claude/rules/backend-python.md` and siblings point at
    # ~/harmonic-forge. Checking only the lexical form lets an Edit/Write
    # through a symlink whose OWN directory sits in a feature-branch (or
    # untracked-there) checkout silently modify a tracked file in a
    # DIFFERENT worktree that has `main` checked out.
    for candidate in (lexical, resolved):
        target_dir = candidate.parent
        branch = subprocess.run(
            ["git", "-C", str(target_dir), "branch", "--show-current"],
            text=True, capture_output=True, check=False,
        )
        if branch.returncode or branch.stdout.strip() != "main":
            continue
        tracked = subprocess.run(
            ["git", "-C", str(target_dir), "ls-files", "--error-unmatch", str(candidate)],
            text=True, capture_output=True, check=False,
        )
        if tracked.returncode == 0:
            return True
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


def ignorable_write_target(target: str) -> bool:
    """Targets that are never a file this project protects.

    `&1` is the tail of `2>&1` after the glued-redirect split, not a path.
    `/dev/*` covers the single most frequent redirect in this codebase
    (`> /dev/null`) — treating it as a protected write would deny it under
    LANE=3, where every path outside testplan is protected.
    """
    return (not target
            or target.startswith("&")
            or target == "/dev"
            or target.startswith("/dev/"))


def bash_write_targets(segment: list[str]) -> list[str]:
    """Paths a single shell segment writes to, by static construct.

    Statically-decidable shapes only — redirects, `tee`, `sed -i`,
    `cp`/`mv`/`install` destinations, `truncate`, `dd of=`. Interpreter
    one-liners carry no shell-visible write construct and are handled
    separately by `interpreter_write_targets` (harmonic-forge#458).
    """
    targets: list[str] = []
    index = 0
    while index < len(segment):
        token = segment[index]
        if REDIRECT_TOKEN.match(token) and index + 1 < len(segment):
            targets.append(segment[index + 1])
            index += 2
            continue
        glued = GLUED_REDIRECT.match(token)
        if glued:
            targets.append(glued.group("target"))
        index += 1

    tokens = strip_invocation_prefix(segment)
    if not tokens:
        return targets
    verb = Path(tokens[0]).name
    args = tokens[1:]
    positional = [arg for arg in args if not arg.startswith("-")]

    if verb == "tee":
        targets.extend(positional)
    elif verb == "sed" and any(a.startswith("-i") or a == "--in-place" for a in args):
        # Without -e/-f, sed's FIRST positional is the script (`s/x/y/`), not a
        # file — and a script is path-shaped enough to resolve inside a
        # protected checkout, so dropping it matters.
        explicit_script = any(
            a in ("-e", "--expression", "-f", "--file")
            or a.startswith(("--expression=", "--file="))
            for a in args
        )
        targets.extend(positional if explicit_script else positional[1:])
    elif verb in ("cp", "mv", "install") and len(positional) >= 2:
        targets.append(positional[-1])
    elif verb == "truncate":
        targets.extend(a for a in positional if not SIZE_OPERAND.match(a))
    elif verb == "dd":
        targets.extend(a[len("of="):] for a in args if a.startswith("of="))

    return [t for t in targets if not ignorable_write_target(t)]


def interpreter_write_targets(
    segment: list[str], command: str
) -> list[tuple[str, str]]:
    """`(construct, operand)` pairs from an interpreter invocation that writes.

    BOTH signals are required — a write construct AND its own operand, matched
    as one pattern. The previous shape required them only to be CO-PRESENT
    anywhere in the command text, and that is the whole of harmonic-forge#522:
    every path-shaped run in the raw text was returned as a write target, so a
    heredoc body's prose path, a `grep` operand, and the interpreter's own
    script argument were all reported as things the command wrote to. Ten live
    false positives, none of them a parser bug.

    Correlating does not weaken the guard: a write's target is adjacent to its
    verb by construction — that adjacency is what makes it a write.

    Scans the RAW command rather than the parsed segment because
    `command_segments` masks heredoc bodies — `python3 <<'PY' ... PY` is
    exactly one of the shapes reproduced live for this issue, and its body is
    `__HEREDOC_BODY__` by the time it reaches a segment.

    Evadable, and stated as such: `getattr(open, ...)`, a base64'd payload,
    `exec()`. The threat model is an agent taking a convenient shortcut — which
    is what actually happened in harmonic-forge#446 — not an adversary.
    `block_irreversible_ops.py` concedes the same class for shell wrappers
    rather than pretending to close it; this follows that precedent.
    """
    tokens = strip_invocation_prefix(segment)
    if not tokens or Path(tokens[0]).name not in INTERPRETERS:
        return []
    return interpreter_write_pairs(command)


#: A write construct AND the operand it writes to, as ONE pattern.
#:
#: harmonic-forge#522: the previous rule asked two uncorrelated questions —
#: "is there a write verb anywhere in this command" and "what paths appear
#: anywhere in this command" — and returned the cross product. So every path
#: in the raw text became a write target as soon as any write verb appeared
#: anywhere: the interpreter's own script argument, a `grep` operand, a path
#: mentioned in passing inside a heredoc body. Ten documented false positives,
#: every one on read-only work, including two whose cited "write target" was a
#: `/tmp` impl worktree the command never touched.
#:
#: **The raw-text scan is deliberately kept.** Restricting to parsed segments
#: was the issue's own scope item 1, and it would delete this rule's reason for
#: existing: `command_segments` masks heredoc bodies, so
#: `python3 <<'PY' ... open('<tracked>','w') ... PY` — the harmonic-forge#446
#: shape this was built for — has its target visible only in raw text. Lane 1
#: ratified the correlated reading over item 1's literal wording for exactly
#: that reason.
#:
#: Correlation is a change of shape, not of strength. Every genuine write has
#: its operand adjacent to its verb, because that adjacency is what makes it a
#: write; nothing real is lost, and everything invented disappears.
#: Every alternative names BOTH its construct and its operand, and the operand
#: group is what the denial reports. An earlier revision let the construct
#: groups fall through to "first non-empty group", which returned the whole
#: matched expression as the operand — a denial reading
#: `Path('mise.toml').write_text(` rather than `mise.toml`, which is AC3's own
#: defect in a new place.
INTERPRETER_WRITE_PAIR = re.compile(
    r"""(?P<open>open\s*\(\s*['"](?P<open_op>[^'"]+)['"]\s*,\s*['"][rbt]*[wax+][^'"]*['"])"""
    r"""|(?P<pathwrite>Path\s*\(\s*['"](?P<pathwrite_op>[^'"]+)['"]\s*\)\s*\."""
    r"""\s*write_(?:text|bytes)\s*\()"""
    r"""|(?P<writelines>['"](?P<writelines_op>[^'"]+)['"][^\n]{0,40}\.writelines\s*\()"""
    r"""|(?P<move>(?:os\.(?:replace|rename)|shutil\.(?:copy\w*|move))"""
    r"""\s*\([^,)]*,\s*['"](?P<move_op>[^'"]+)['"]\s*\))"""
    r"""|(?P<redirect>>>?\s*['"]?(?P<redirect_op>(?:~|\.{0,2}/)[\w.\-/]*))"""
)

#: Construct name -> the group holding its operand.
_WRITE_OPERAND_GROUP = {
    "open": "open_op",
    "pathwrite": "pathwrite_op",
    "writelines": "writelines_op",
    "move": "move_op",
    "redirect": "redirect_op",
}


def interpreter_write_pairs(command: str) -> list[tuple[str, str]]:
    """`(construct, operand)` for every write whose target is identifiable.

    The operand is what the denial message must name (AC3) — citing the first
    path candidate instead is how the old rule produced messages naming files
    the command never wrote to.
    """
    pairs: list[tuple[str, str]] = []
    for match in INTERPRETER_WRITE_PAIR.finditer(command):
        for construct, operand_group in _WRITE_OPERAND_GROUP.items():
            if match.group(construct) is None:
                continue
            operand = match.group(operand_group)
            if operand and not ignorable_write_target(operand):
                pairs.append((construct, operand))
            break
    return pairs


#: Upper bound on paths examined per segment. `write_on_main_branch` shells out
#: to git twice per candidate, and the interpreter rule's path extraction is
#: deliberately coarse — a pathological command must not turn a PreToolUse hook
#: into a visible stall.
MAX_WRITE_TARGETS = 20


#: Human-readable name for each correlated write construct, so the denial
#: message says HOW the command writes, not just that something somewhere in it
#: looked like a write (harmonic-forge#522 AC3). The old generic phrasing --
#: "an interpreter one-liner" -- was accurate about the segment and silent
#: about the operand, which is exactly what made a false positive unreadable:
#: nothing in the message let a reader check the claim.
CONSTRUCT_LABELS = {
    "open": "an `open(..., 'w')` call",
    "pathwrite": "a `Path(...).write_text()`/`.write_bytes()` call",
    "writelines": "a `.writelines()` call",
    "move": "an `os.replace`/`os.rename`/`shutil` move",
    "redirect": "a shell redirect (`>`/`>>`)",
}


def _worktree_root(path: Path) -> str | None:
    """Top-level directory of the git worktree containing `path`, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode:
        return None
    return result.stdout.strip() or None


def branch_advice(target: str, cwd: Path) -> str:
    """The followable half of a `write_on_main_branch` denial (harmonic-forge#522).

    `Branch first: git checkout -b <name>` is only actionable when the write is
    into the checkout the session is actually sitting in -- then branching that
    checkout is precisely the fix. When the target lives in a DIFFERENT worktree
    that happens to have `main` checked out (the shape that bit: a Lane 2
    session correctly scoped to its own `/tmp/<repo>-<issue>-impl` worktree,
    reaching out to a path in the shared main checkout), branching does nothing
    -- the session is already on the branch it should be on, and the fix is to
    write inside its own worktree instead. Telling it to branch sent it looking
    for a problem that was not there.
    """
    try:
        raw = Path(target).expanduser()
        if not raw.is_absolute():
            raw = cwd / raw
        target_dir = Path(os.path.normpath(raw)).parent
    except (OSError, ValueError, RuntimeError):
        return "Branch first: `git checkout -b <name>`."
    target_root = _worktree_root(target_dir)
    session_root = _worktree_root(cwd)
    if target_root and session_root and target_root != session_root:
        return (
            f"Branching will not help: this session is in {session_root!r}, "
            f"which is not the checkout being written to. The write reaches "
            f"OUT to {target_root!r}, a separate checkout that has `main` "
            "checked out. Write inside this session's own worktree instead."
        )
    return "Branch first: `git checkout -b <name>`."


def protected_write_denial(
    pairs: list[tuple[str, str]], cwd: Path
) -> dict | None:
    """The `Edit`/`Write` predicates, reached from the `Bash` surface.

    ALL THREE of them, which the plan for this issue got wrong: it named
    `lane2_write_in_main_checkout` and `lane3_write_outside_testplan` only, and
    with just those two the live re-run still allowed every scripted and
    redirect shape. The predicate that actually denies the reported incident is
    `write_on_main_branch` (harmonic-forge#384) — the write was into ANOTHER
    project's main checkout (`~/harmonic-forge` from an HRSE2 session), which
    the Lane 2 predicate deliberately does not cover, since it resolves the
    protected root from the session's own cwd. Wiring the first two alone would
    have shipped a fix that passed its tests and left the bypass open.

    No new protected surface: a command denied here is one that would already
    have been denied had the same file been touched with `Edit`. The bug
    (harmonic-forge#458) is that the two surfaces shared these predicates and
    only one was wired to them.
    """
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for construct, target in pairs:
        if target not in seen:
            seen.add(target)
            deduped.append((construct, target))
    for construct, target in deduped[:MAX_WRITE_TARGETS]:
        if lane2_write_in_main_checkout(target, cwd):
            return denial(
                f"Blocked: this session was launched as Lane 2 (LANE=2) and "
                f"this command writes into the main checkout via {construct} "
                f"({target!r}) — harmonic-forge#458. A shell write is the same "
                "violation as an `Edit` here (harmonic-forge#142): Lane 2 work "
                "belongs in its own dedicated worktree. Re-run it against the "
                "project's -lane2 worktree or a fresh "
                "/tmp/<project>-<issue>-impl worktree."
            )
        if lane3_write_outside_testplan(target):
            return denial(
                f"Blocked: this session was launched as Lane 3 (LANE=3) and "
                f"this command writes outside ~/Harmonic_Projects/testplan/ "
                f"via {construct} ({target!r}) — harmonic-forge#458. Lane 3 "
                "never fixes anything, ever (harmonic-forge#150); the only "
                "writable path is the testplan root, for gate artifacts too "
                "large for an issue comment. Redirect scratch output there "
                "instead."
            )
        if write_on_main_branch(target, cwd):
            return denial(
                f"Blocked: {target!r} is a tracked file in a checkout that has "
                f"`main` checked out, and this command writes to it via "
                f"{construct} (harmonic-forge#384/#458). "
                f"{branch_advice(target, cwd)} A shell write is the same "
                "violation as an `Edit`, and creating files and branching "
                "afterward is the same violation again. This check applies "
                "regardless of LANE, including a Lane 1 session with no LANE "
                "set at all."
            )
    return None


def directory_change(segment: list[str]) -> tuple[str | None, bool] | None:
    """`(target, resolvable)` when `segment` changes directory, else None.

    `resolvable` is False when the change is real but its destination cannot be
    known statically — a bare `cd` (to `$HOME`), a bare `pushd` (a stack swap),
    or any operand carrying `$`/backtick. The caller must not silently keep
    using the old cwd in that case: a relative write target resolved against a
    directory the command has already left is a guess, and the guess fails
    open (harmonic-forge#529).
    """
    index = 0
    while index < len(segment) and segment[index] in COMPOUND_KEYWORDS:
        index += 1
    tokens = segment[index:]
    if not tokens or tokens[0] not in DIR_CHANGE_VERBS:
        return None
    operands = [token for token in tokens[1:] if token not in DIR_CHANGE_FLAGS]
    if not operands:
        return (None, False)
    target = operands[0]
    if DYNAMIC_TOKEN.search(target):
        return (None, False)
    return (target, True)


def nested_shell_script(segment: list[str]) -> str | None:
    """The script text of a `<shell> -c '<script>'` invocation, else None."""
    tokens = strip_invocation_prefix(segment)
    if len(tokens) < 3 or Path(tokens[0]).name not in NESTED_SHELLS:
        return None
    if "-c" not in tokens[1:]:
        return None
    return " ".join(tokens[2:])


def nested_shell_write_targets(script: str) -> list[str]:
    """Write targets inside a nested `-c` script.

    They are invisible to the outer pass: the whole script arrives as ONE
    token, so no redirect operator is ever a separate token and
    `bash_write_targets` sees nothing to check. Re-parsing the script is what
    makes its writes reachable at all — without this the nested shape is not
    denied by the unresolved-cwd rule, because there is no target to deny.
    """
    try:
        return [target
                for segment in command_segments(script)
                for target in bash_write_targets(segment)]
    except (AttributeError, TypeError, ValueError):
        return []


def unresolved_cwd_denial(targets: list[str]) -> dict | None:
    """Deny a RELATIVE write target after a directory change we could not follow.

    Fail loud, not open (harmonic-forge#529, ratified). The alternative is to
    resolve the path against a directory the command is no longer in, which
    silently permits a write into a checkout on `main` — the failure this issue
    was filed for, and the one nothing surfaces afterwards.

    Absolute targets are deliberately untouched: they do not depend on cwd, so
    an unresolved directory change tells us nothing about them, and denying
    them would break every `> /tmp/...` inside a loop.
    """
    for target in targets:
        if ignorable_write_target(target):
            continue
        try:
            if Path(target).expanduser().is_absolute():
                continue
        except (OSError, ValueError, RuntimeError):
            pass
        return denial(
            f"Blocked: this command changes directory to somewhere this guard "
            f"cannot resolve statically (a variable, a bare `cd`/`pushd`, or a "
            f"nested `bash -c`), and then writes to the RELATIVE path "
            f"{target!r} (harmonic-forge#529). Where that lands depends on the "
            "unresolved directory, so the guard cannot tell whether it is a "
            "protected checkout — and guessing would fail open. Pass an "
            "absolute path for the write target and it will be checked "
            "normally."
        )
    return None


def denial(message: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _batch_note(message),
        },
        "systemMessage": _batch_note(message),
    }


def decision(command: object, cwd: Path) -> dict:
    if not isinstance(command, str):
        return denial("Blocked: malformed Bash hook payload; refusing to bypass Lane 1 posting controls.")
    try:
        segments = command_segments(command)
    except (AttributeError, TypeError, ValueError):
        return denial("Blocked: malformed shell command; refusing to bypass Lane 1 posting controls.")
    # harmonic-forge#210: seed from the payload's own cwd, not the hook
    # process's — hooks run as subprocesses, and nothing guarantees the
    # two match (this is exactly why the payload carries a cwd field).
    effective_cwd = cwd
    # harmonic-forge#529: sticky once set. A command that has changed directory
    # to somewhere unknown does not come back to a known one just because a
    # later segment looks ordinary.
    cwd_unresolved = False
    for segment in segments:
        change = directory_change(segment)
        if change is not None:
            target, resolvable = change
            if not resolvable or target is None:
                cwd_unresolved = True
                continue
            expanded = Path(target).expanduser()
            effective_cwd = (expanded if expanded.is_absolute()
                             else effective_cwd / expanded)
            continue
        nested = nested_shell_script(segment)
        if nested is not None and NESTED_DIR_CHANGE.search(nested):
            cwd_unresolved = True
            nested_denial = unresolved_cwd_denial(
                nested_shell_write_targets(nested))
            if nested_denial is not None:
                return nested_denial
        # harmonic-forge#458: the path predicates below govern the Edit/Write
        # surface and, until now, nothing else — so `echo x > <protected>` and
        # `python3 -c "open(<protected>,'w')"` walked straight past a guard
        # that denies the identical write through `Edit`.
        shell_targets = bash_write_targets(segment)
        interpreter_pairs = interpreter_write_targets(segment, command)
        if cwd_unresolved:
            unresolved = unresolved_cwd_denial(
                shell_targets + [target for _c, target in interpreter_pairs])
            if unresolved is not None:
                return unresolved
        write_denial = protected_write_denial(
            [("a shell write construct", target) for target in shell_targets],
            effective_cwd)
        if write_denial is not None:
            return write_denial
        write_denial = protected_write_denial(
            [(CONSTRUCT_LABELS.get(construct, "an interpreter one-liner"), target)
             for construct, target in interpreter_pairs],
            effective_cwd)
        if write_denial is not None:
            return write_denial
        if os.environ.get("LANE") == "3":
            bulk_read_reason = bulk_comment_read_denial(segment)
            if bulk_read_reason is not None:
                return denial(f"Blocked: {bulk_read_reason}")
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



def _batch_note(message: str, target_key: str | None = None) -> str:
    """Append the interrupted-batch line (harmonic-forge#509 AC3).

    Message-only. Never softens this hook's verdict — see `batch_context`'s
    docstring for why a hook consulting `batch_auth` to return `allow` would
    reintroduce the `#336` composition failure.
    """
    try:
        from batch_context import annotate  # noqa: PLC0415

        return annotate(message, target_key=target_key)
    except Exception:
        return message

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
        print(json.dumps(decision(command, cwd)))
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
        if write_on_main_branch(file_path, cwd):
            print(json.dumps(denial(
                "Blocked: this checkout has `main` checked out and "
                f"{file_path!r} is a tracked file (harmonic-forge#384). "
                "Branch first: `git checkout -b <name>` — creating files "
                "and branching afterward is the same violation. This "
                "check applies regardless of LANE, including a Lane 1 "
                "session with no LANE set at all."
            )))
            return
        print("{}")
        return
    print("{}")


if __name__ == "__main__":
    main()
