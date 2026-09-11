#!/usr/bin/env python3
"""PreToolUse hook: block GitHub closing keywords in PR bodies/comments.

Extends harmonic-forge#85's gh-issue-close permission block to the
closing *intent*, not just that one command surface. #85 blocks
`gh issue close`/`gh issue reopen` directly; it does not and cannot
catch a `Closes #N`-style keyword written into a PR body or comment,
which delegates the same closing action to GitHub's own merge-triggered
automation. See harmonic-forge#93 for the incident record (6th instance
of the closed-without-authorization pattern, this one via a new
mechanism).

Only fires on `gh pr create`, `gh pr edit`, `gh issue comment`,
`gh issue edit`, and `gh api ... -X PATCH .../comments/...` — the
Bash commands whose string arguments can carry a closing keyword.
Non-closing references (Implements/Part of/Refs #N) are unaffected.

## harmonic-forge#612 — the one live-BATCH exception

Closing stays a manual, explicit action whenever no BATCH is in effect
(BATCH itself no longer grants `gh issue close` at all -- see
`batch_auth.py`'s own `DEFAULT_ACTIONS`/`CLOSE_ACTION_REMOVED`). But a live
BATCH batch needs sequenced issues to close automatically on merge, because
a later issue in the sequence may depend on an earlier one's closure to
become pickupable. The operator's own framing: *"if BATCH is in effect I
need to be able to batch sequence issues that depend on the prior one
closing before they get picked up. The only way to do that is by making
close automatic. We have preclose inspection to provide an adversarial
check of each issue before close so I'm ok with this one exception."*

So a `Closes #N` is allowed ONLY when a live BATCH grant currently covers
`gh pr merge` for that exact issue -- checked here, live, against the same
state file `batch_auth.py` owns. Every other case (no live grant, expired,
wrong issue, state file unreadable) still denies exactly as before.

**Reads the state file directly rather than importing `batch_auth`**
(harmonic-forge#600 AC4: "Neither mechanism gains a dependency on the
other's internals. A shared state file or a documented interface, not
`tools/gh/` importing `tools/hooks/`.") The coupling that remains is one
path and the `targets`/`expires_at` shape, pinned by this file's own tests
so a format drift fails here rather than silently reading as "never
authorized" -- see `belt_batch_view.py` for the identical precedent.

**Fails toward deny, not toward allow**, on every uncertain case (state
file absent/unreadable/malformed, repo unresolvable, prefix unknown) --
the opposite failure direction from `belt_batch_view.py`'s advisory
"fail open, report no batch" posture, because a false ALLOW here is the
exact unauthorized-close risk this hook exists to prevent, while a false
DENY only costs one extra explicit close.
"""
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CLOSING_KEYWORD = re.compile(
    r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r"\s+(?P<repo>[\w.-]+/[\w.-]+)?#(?P<number>\d+)"
)

RELEVANT_COMMAND = re.compile(
    r"(?i)\bgh\s+(pr\s+(create|edit)|issue\s+(comment|edit)|api\b.*-X\s*PATCH)"
)

def _real_repo_flag(command: str) -> str | None:
    """The command's own `--repo` flag, as an actual argv token -- never a
    same-looking substring sitting inside a quoted `--body`/`--title`
    argument's VALUE.

    Preclose finding: a naive command-wide regex matched `--repo ...` text
    pasted into a PR body (this house's own PR bodies routinely paste `gh
    ... --repo ...` example commands), redirecting a bare `#N` to whichever
    repo that quoted example named -- a live grant on the wrong issue then
    read as a false ALLOW for the real one. `shlex.split` tokenizes the
    command the way a shell would: a quoted argument is ONE token
    regardless of what it contains, so text inside `--body "..."` can never
    be mistaken for a top-level `--repo` flag.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes or similar -- cannot safely tokenize, so no
        # flag can be trusted. Falls through to the cwd-remote fallback.
        return None
    for i, tok in enumerate(tokens):
        if tok == "--repo" and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--repo="):
            return tok.split("=", 1)[1]
    return None

#: Owned and written by `tools/hooks/batch_auth.py` (`STATE_PATH` there).
#: Named here rather than imported (harmonic-forge#600 AC4) -- see module
#: docstring.
BATCH_STATE_PATH = Path.home() / ".claude" / "state" / "batch-authorized.json"

#: Mirrors `batch_auth._repo_prefixes()`'s own hardcoded fallback exactly --
#: duplicated rather than imported for the same AC4 reason. Both read the
#: same `tools/onboard/manifest.py` first; this is the resilience fallback
#: only, and the two fallback tables are pinned equal by this file's tests.
_FALLBACK_PREFIXES = {
    "vitalharmony/hrse": "H",
    "vitalharmony/harmonic-forge": "F",
    "vitalharmony/cymagraph-infra": "I",
    "vitalharmony/openclaw-projects": "O",
}


def _repo_prefixes() -> dict[str, str]:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "onboard"))
        import manifest as _manifest  # noqa: PLC0415

        return {p.repo: p.prefix for p in _manifest.load()
                if p.repo and (p.account or "vitalharmony") == "vitalharmony"}
    except Exception:  # noqa: BLE001 -- a hook must never fail closed on import
        return dict(_FALLBACK_PREFIXES)


def _issue_key(repo: str, number: str) -> str | None:
    prefix = _repo_prefixes().get(repo)
    return f"{prefix}{number}" if prefix else None


def _resolve_repo(command: str, explicit: str | None) -> str | None:
    """The repo a bare `#N` (no owner/repo prefix) refers to: an explicit
    `--repo` flag on the command, else the cwd's own git remote -- never
    guessed from the closing keyword's own text when that text carries none."""
    if explicit:
        return explicit
    real_flag = _real_repo_flag(command)
    if real_flag:
        return real_flag
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001
        return None
    if result.returncode != 0:
        return None
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?\s*$", result.stdout.strip())
    return m.group(1) if m else None


def _is_merge_live(key: str) -> bool:
    """Does a live BATCH grant currently cover `gh pr merge` for this key?

    Deliberately checks for a live `gh pr merge` target only, never
    `consumed` state -- this hook fires at `gh pr create`/`gh pr edit` time,
    before any merge has happened, so the target is expected to still be
    unconsumed. Every failure (file absent, unreadable, malformed entry,
    unparseable/timezone-naive `expires_at`) reads as False -- deny, the
    safe direction here.
    """
    try:
        raw = BATCH_STATE_PATH.read_text(encoding="utf-8")
        state = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return False
    entry = state.get(key)
    if not isinstance(entry, dict):
        return False
    try:
        expires = datetime.fromisoformat(entry["expires_at"])
        if datetime.now(timezone.utc) >= expires:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    targets = entry.get("targets", [])
    if not isinstance(targets, list):
        return False
    return any(
        isinstance(t, dict) and t.get("action") == "gh pr merge"
        for t in targets
    )


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({}))
        return

    if data.get("tool_name") != "Bash":
        print(json.dumps({}))
        return

    command = (data.get("tool_input") or {}).get("command", "")
    if not RELEVANT_COMMAND.search(command):
        print(json.dumps({}))
        return

    matches = list(CLOSING_KEYWORD.finditer(command))
    if not matches:
        print(json.dumps({}))
        return

    explicit_repo_flag = _real_repo_flag(command)

    unauthorized: list[str] = []
    for match in matches:
        repo = _resolve_repo(command, match.group("repo") or explicit_repo_flag)
        number = match.group("number")
        key = _issue_key(repo, number) if repo else None
        live = key is not None and _is_merge_live(key)
        if not live:
            unauthorized.append(match.group(0))

    if not unauthorized:
        print(json.dumps({}))
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
        },
        "systemMessage": (
            f"Blocked: this command's body contains a GitHub closing "
            f"keyword ({unauthorized[0]!r}), which would auto-close the "
            f"referenced issue on merge without explicit human "
            f"authorization (harmonic-forge#84/#93). Use a non-closing "
            f"reference instead — \"Implements #N\" or \"Part of #N\" — "
            f"and close the issue explicitly, separately, only when told to. "
            f"(harmonic-forge#612: a live BATCH grant covering `gh pr "
            f"merge` for this exact issue is the one exception — none was "
            f"found live for this command.)"
        ),
    }))


if __name__ == "__main__":
    main()
