#!/usr/bin/env python3
"""PreToolUse hook: a data-migration issue must be labelled executed to close.

hrse#859. The Lane 1 -> Lane 2 -> Lane 3 loop asserts that code is
correct; nothing asserts that a *migration* actually executed. When an
issue's scope IS a data migration, merging the code satisfies every
check that exists, so closing it looks legitimate while the data is
untouched.

Real incident, hrse#849 (2026-08-13): the classifier fix merged
(hrse#853, 6a2d907) and the issue was closed while all 219
null-direction Activity rows were still null. Its own closing comment
said the run was "still outstanding". It blocked hrse#847 and hrse#856
for as long as it read `closed`, and was reopened by hand only because
a human noticed in conversation.

## What this guarantees, stated exactly

Closing a `data-migration` issue through `gh issue close` or a
`gh api PATCH ... state=closed` requires a deliberate second action --
applying the `migration-executed` label -- rather than happening as a
side effect of merging code. **It does not and cannot prevent the
close.** The hook is fail-open, and it sees nothing of `gh api graphql`
mutations, `--input -`/heredoc bodies, the GitHub web UI, or a close
issued from Python or curl. Its audience is the honest-but-careless
agent; it has never constrained a dishonest one.

The load-bearing control *will be* the after-the-fact sweep for closed
`data-migration` issues lacking `migration-executed` -- filed as
hrse#867 and **not yet shipped**. That catches every close path this
hook structurally cannot see. Until it lands, this hook is the only
control, and it is the half that both documents describe as *not*
load-bearing. Do not mistake it for coverage.

## Why a label, not a marker in a comment

Four review rounds rejected comment-parsing designs, and three failed
the same way: prose treated as a credential. A marker's format must be
published -- in `3-lane-protocol.md`, `rules/testing-gate.md`, this
file's own deny message, and the companion issue hrse#866 -- so **every
published example is itself a valid credential**. Round 4 proved it:
hrse#866's body carries a fenced example receipt naming hrse#849, and
pasting that body onto the thread opened the gate for the exact issue
this hook was built to protect. Successive rounds narrowed where an
example may legally appear (not blockquoted, then not fenced, then not
indented) -- a blocklist maintained against your own documentation,
which does not converge.

A label ends the class rather than narrowing it: naming the label is
not applying it, so the docs may quote the mechanism freely. Label
application is also a timeline event carrying actor and timestamp,
which a pasted comment is not.

`model_tier_gate.py` is the only other hook here that reads GitHub
state to decide, and it reads a structured board field for the same
reason.

Known residual gaps of this text hook, recorded rather than left
implicit: `gh api graphql` mutations, `--input -`/heredoc bodies, any
close not issued through the Bash tool, a gh call behind a runner or an
expansion (`xargs`, `bash -c`, `$G issue close`, an alias), and which
repo a bare close binds to after the command rebinds it (`cd`, an
exported `GH_REPO`, `GIT_DIR`, ambient env). The gh shim
(`close_gate.shim_verdict`, harmonic-forge#1498 reforge) runs this
file's `decide_target` on the real argv, cwd and environment, and is
the authority for every one of those Bash-issued forms.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from hook_identity import repo_from_checkout as _repo_from_checkout, run_gh as _run_gh  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gh_argv_adapters as A  # noqa: E402
import gh_segment  # noqa: E402
from shell_parse import command_segments  # noqa: E402

LABEL = "data-migration"
EXECUTED_LABEL = "migration-executed"
ABANDONED_LABEL = "migration-abandoned"

def _allow() -> None:
    print(json.dumps({}))


def _deny(reason: str) -> None:
    # permissionDecisionReason is what the *model* sees. systemMessage is
    # only surfaced to the user, so a hook setting solely the latter
    # reports "Blocked by hook" to the agent -- which is precisely the
    # agent that then retries with a variant flag order.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
    }))


def _gh(*args: str, cwd: str | None = None, repo: str | None = None) -> str | None:
    """Return stdout, or None on any failure.

    Fail-open by design: a hook that blocks work whenever the network is
    slow trains people to disable it, and a disabled hook enforces
    nothing. Under the gh shim's strict mode (`close_gate.strict`) a failure
    raises `close_gate.GateReadError` instead, and the read runs the real gh,
    never the shim again (harmonic-forge#1498 reforge).
    """
    import close_gate  # noqa: PLC0415
    gh_bin = close_gate.STATE.gh_bin
    if (gh_bin is None and shutil.which("gh") is None) or (gh_bin and not os.path.isfile(gh_bin)):
        return close_gate.unread(args, "no gh binary")
    try:
        result = _run_gh(args, repo=repo, cwd=cwd, timeout=7, gh_bin=gh_bin)
    except (OSError, subprocess.SubprocessError) as exc:
        return close_gate.unread(args, f"{type(exc).__name__}: {exc}")
    if result.returncode:
        return close_gate.unread(args, "gh exited non-zero")
    return result.stdout


def _literal_gh_after_prefix(raw_tokens: list[str]) -> tuple[int, dict[str, str]] | None:
    """(index of the command word, inline env) when the segment is a literal gh
    command word behind only bare `env` words and NAME=value assignments; None
    otherwise. Reforged pass 1 survivor 1 (harmonic-forge#1498): a gh word in an
    assignment value (`GH_CONFIG_DIR=~/.config/gh gh issue close N`) or a second
    `env` must not hide a literal gh call, which main's prefix strip read."""
    env: dict[str, str] = {}
    for index, token in enumerate(raw_tokens):
        if token == "env":
            continue
        if match := gh_segment._ASSIGNMENT.match(token):
            env[match.group(1)] = match.group(2)
            continue
        if Path(token).name == "gh" and "$" not in token and "`" not in token:
            return index, env
        return None
    return None


def _close_result(raw_tokens: list[str]) -> A.Result | None:
    """The adapter's result for one segment that is a literal gh call and is,
    or cannot be proved not to be, an issue close; None otherwise.

    One structural cut, no word list (harmonic-forge#1498 reforge): only a
    literal gh call counts. A gh word in another program's arguments (`grep
    -rn 'gh issue close'`, `git commit -m 'gh api'`, `echo gh ...`), an
    expansion as the command word (`$G`, `$PYTHON`) or a runner (`bash -c`,
    `xargs`, `sudo`) is not-a-close here, as on main: the real-argv shim is the
    authority for those (`close_gate.shim_verdict`). A literal `gh` command
    word whose own arguments mention gh (`--comment 'see gh api'`) is still a
    gh call, read from the words after it. The environment is `{}` plus the
    segment's own inline assignments, so a bare close binds to the payload
    cwd's repo exactly as on main.
    """
    segment = gh_segment.gh_segment(raw_tokens)
    if segment.kind == gh_segment.GH:
        return A.close_target(raw_tokens, {})
    word = next((i for i, token in enumerate(raw_tokens) if Path(token).name == "gh"), None)
    head = None if word is None else gh_segment.gh_segment(raw_tokens[:word + 1])
    if head is not None and head.kind == gh_segment.GH:
        env = dict(head.env)
    else:
        literal = _literal_gh_after_prefix(raw_tokens)
        if literal is None:
            return None
        word, env = literal
        if any("$(" in value or "`" in value for value in env.values()):
            # A substitution in the prefix runs before gh: fail closed.
            return A.Result(A.UNPROVABLE, reason="a substitution in the inline assignment prefix")
    result = A.classify_argv(list(raw_tokens[word + 1:]), env)
    if (result.gated and result.kind == "issue") or result.outcome in (A.UNPARSEABLE, A.UNPROVABLE):
        return result
    return None


def find_close_targets(command: str, reasons: list[str] | None = None,
                       ) -> list[tuple[str | None, str]] | None:
    """All (repo, issue) pairs this command would close; None when the command
    cannot be tokenized, treated as fail-open by the caller.

    A bound issue close is `(repo, number)`. An unbound, unparseable or
    unprovable literal gh call is `(None, "")`, which `main()` denies before any
    repo or label read, appending why to `reasons` when given: a close the hook
    cannot name must not pass as not-a-close. Target and flag grammar is
    `gh_argv`'s, through `gh_argv_adapters` (harmonic-forge#1498); segmentation
    is the shared `shell_parse.command_segments` (harmonic-forge#167).
    """
    try:
        segments = command_segments(command)
    except ValueError:
        return None
    targets: list[tuple[str | None, str]] = []
    for raw_tokens in segments:
        result = _close_result(raw_tokens)
        if result is None:
            continue
        if result.outcome == A.BOUND:
            targets.append((result.repo, result.number))
            continue
        targets.append((None, ""))
        if reasons is not None:
            reasons.append(result.reason or result.outcome)
    return targets


def resolve_repo(explicit: str | None, cwd: str | None = None) -> str | None:
    """Repo for the command, falling back to the cwd's own remote.

    The bare `gh issue close N` form is how issues are actually closed,
    so treating a missing --repo as unresolvable would leave the most
    common path unguarded. Resolution runs in the *command's* directory:
    the payload carries `cwd`, and resolving against the hook process's
    own directory checked the wrong repository's issue #N.
    """
    if explicit:
        return explicit
    known = _repo_from_checkout(cwd)  # harmonic-forge#804: before any gh call
    if known:
        return known
    out = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner",
              cwd=cwd)
    return out.strip() if out and out.strip() else None


def labels_for(repo: str, issue: str) -> set[str] | None:
    """Label names on the issue, or None when they cannot be read."""
    out = _gh("api", f"repos/{repo}/issues/{issue}", "--jq", ".labels[].name", repo=repo)
    if out is None:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def decide_target(repo: str | None, issue: str, cwd: str | None, *,
                  strict_repo: bool = False) -> str | None:
    """The deny text for closing `issue` (repo None: the command's own), or None.

    Shared by `main()` and the gh shim (`close_gate.shim_verdict`,
    harmonic-forge#1498 reforge). `strict_repo` is the shim's: an unresolvable
    repo refuses, and a bare close's repo is asked of the real gh, which reads
    the same cwd and environment (`GIT_DIR` included) the close itself runs in.
    """
    if strict_repo and not repo:
        out = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner", cwd=cwd)
        resolved = out.strip() if out and out.strip() else None
    else:
        resolved = resolve_repo(repo, cwd)
    if resolved is None:
        return f"could not resolve the repository for issue {issue}" if strict_repo else None
    labels = labels_for(resolved, issue)
    if labels is None or LABEL not in labels:  # fail-open, same rationale as _gh
        return None
    if EXECUTED_LABEL in labels or ABANDONED_LABEL in labels:
        return None
    return (
        f"Blocked: {resolved}#{issue} is labelled {LABEL!r} and carries "
        f"neither {EXECUTED_LABEL!r} nor {ABANDONED_LABEL!r}, so nothing "
        f"records that the migration ran (hrse#859).\n\n"
        f"Merging the code is not running the migration. hrse#849 closed "
        f"exactly this way on 2026-08-13 with all 219 rows untouched, and "
        f"blocked two downstream issues until a human caught it.\n\n"
        f"Run the migration, then:\n"
        f"  gh issue edit {issue} --repo {resolved} --add-label "
        f"{EXECUTED_LABEL}\n\n"
        f"If it is being deliberately abandoned, apply {ABANDONED_LABEL!r} "
        f"instead and say why in a comment.\n\n"
        f"This hook only makes the close require a deliberate labelling "
        f"action -- it cannot prevent one, and does not see graphql "
        f"mutations, heredoc bodies, or web-UI closes."
    )


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _allow()
        return

    if data.get("tool_name") != "Bash":
        _allow()
        return

    command = (data.get("tool_input") or {}).get("command", "")
    payload_cwd = data.get("cwd") or None

    reasons: list[str] = []
    targets = find_close_targets(command, reasons)
    if not targets:
        _allow()
        return
    # Before resolve_repo and labels_for, whose fail-open paths must not run first.
    if any(not issue for _, issue in targets):
        _deny("Blocked: this hook cannot name or prove the issue this command "
              "closes, so it cannot check the data-migration label "
              f"(harmonic-forge#1498): {'; '.join(reasons)}.\n\n"
              "Name it as `gh issue close <N> --repo <owner/repo>`.")
        return

    for explicit_repo, issue in targets:
        denied = decide_target(explicit_repo, issue, payload_cwd)
        if denied:
            _deny(denied)
            return

    _allow()


if __name__ == "__main__":
    main()
