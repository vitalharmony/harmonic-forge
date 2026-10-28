#!/usr/bin/env python3
"""PreToolUse(Bash): refuse an unchecked Tooling Exception push before it runs.

harmonic-forge#1135. The pre-push hook (`check_receipt.py verify`) already
refuses a TE branch with no passing `mise run check` receipt, but only after
the Bash call has started, so every command bundled behind the push runs
against a push that never happened. This hook asks the same question first,
through `check_receipt.push_refusal`, so the two can never disagree.

It is a mistake-detector and fails open: any form it does not judge (a
subshell, a group, a non-literal `cd`, a push after another command, an
option it does not model) is allowed, and pre-push still refuses it later.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

#: The only options this hook models. Any other option (`--dry-run`,
#: `--no-verify`, `--all`, `--mirror`, `--tags`, `--delete`, ...) means pre-push
#: does not apply or the refspecs are not ours to read, so the push is allowed.
MODELLED_OPTIONS = frozenset({"-u", "--set-upstream", "-f", "--force", "--force-with-lease"})


def _git(cwd: str, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _judged_pushes(command: str, cwd: str):
    """Yield (cwd, push args) for each push this hook judges; see AC2."""
    from shell_parse import command_segments  # noqa: PLC0415
    if any(char in command for char in "(){}"):
        return  # AC2: a subshell or group anywhere is not a literal form
    # pass 1 survivor 3: an expansion elsewhere (`; echo rc=$?`) must not stop
    # the judging of a fully literal leading push, so `$` and backticks are
    # checked only inside the segments this hook reads (below).
    here = cwd
    for segment in command_segments(command):
        if any("$" in token or "`" in token for token in segment):
            return  # a non-literal path or refspec in a judged segment
        if segment[:1] == ["cd"] and len(segment) == 2:
            here = os.path.normpath(os.path.join(here, os.path.expanduser(segment[1])))
            continue
        if segment[:2] == ["git", "push"]:
            yield here, segment[2:]
        return  # anything else before a push, or the push itself: stop judging


def _refspecs(cwd: str, args: list[str]):
    """(remote, [(src, dst)]) for one push, or None when it is not modelled."""
    if any(a.startswith("-") and a.split("=", 1)[0] not in MODELLED_OPTIONS for a in args):
        return None  # an option this hook does not model: allow, pre-push still runs
    positional = [a for a in args if not a.startswith("-")]
    if positional:
        remote, specs = positional[0], positional[1:]
    else:
        upstream = _git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}")
        if not upstream or "/" not in upstream:
            return None
        remote, branch = upstream.split("/", 1)
        specs = [f"HEAD:{branch}"]
    if _git(cwd, "config", "--get-all", f"remote.{remote}.push"):
        return None  # a configured push refspec: not ours to model
    if not specs:
        upstream = _git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}")
        if not upstream or "/" not in upstream:
            return None
        specs = [f"HEAD:{upstream.split('/', 1)[1]}"]
    pairs = []
    for spec in specs:
        spec = spec.removeprefix("+")
        src, _, dst = spec.partition(":")
        if not src:
            continue  # `:branch` is a deletion
        if not dst:
            # pass 1 survivor 1: a colonless HEAD, @ or short name pushes to the
            # branch git resolves it to, which is what pre-push sees.
            full = _git(cwd, "rev-parse", "--symbolic-full-name", src) or src
            dst = full
        pairs.append((src, dst.removeprefix("refs/heads/")))
    return remote, pairs


def decide(payload: dict) -> str | None:
    """The deny reason for this Bash call, or None to allow."""
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "push" not in command:
        return None  # fast exit: no git helper is imported or spawned
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import check_receipt  # noqa: PLC0415
    for here, args in _judged_pushes(command, payload.get("cwd") or os.getcwd()):
        parsed = _refspecs(here, args)
        if parsed is None:
            continue
        remote, pairs = parsed
        # pass 1 survivor 4: the push URL, which is where the push goes.
        slug = check_receipt.repo_slug(_git(here, "remote", "get-url", "--push", remote) or "")
        if not slug:
            continue
        for src, dst in pairs:
            # pass 1 survivor 5: one ref's fault skips that ref, never the rest.
            try:
                sha = _git(here, "rev-parse", f"{src}^{{commit}}")
                reason = check_receipt.push_refusal(slug, dst, sha) if sha else None
            except Exception:  # noqa: BLE001 -- that ref is left to pre-push
                continue
            if reason:
                return f"{reason}. Run `mise run check` in that worktree first (harmonic-forge#1135)."
    return None


def main() -> None:
    try:
        reason = decide(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 -- a mistake-detector never blocks on its own fault
        return
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason},
                          "systemMessage": reason}))


if __name__ == "__main__":
    main()
