"""The close and merge gates as one decision, shared by the PreToolUse hooks
and the gh shim (harmonic-forge#1447).

The hooks read a Bash command's text; the shim reads the real argv `gh`
receives, so no shell spelling (an alias, `xargs`, `$G`, a function, a
comment hiding a reserved word) can hide a close or merge from it. Both call
the same decisions here: `block_missing_preclose_inspection.decide_targets`
(the preclose label and receipt) and
`block_undetermined_phase_close.segment_verdict` (the shipped-evidence
phase rule). The hooks stay fail-open; the shim runs them in strict mode,
where any read the decision needs and cannot make refuses (`GateReadError`).
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


@dataclass
class _State:
    strict: bool = False
    gh_bin: str | None = None


STATE = _State()


class GateReadError(Exception):
    """A read the gate needs could not be made (strict mode only)."""


def unread(args, why: str):
    """What a hook's `_gh` returns for a failed read: None (fail open) on the
    hook path, a raised `GateReadError` on the shim path."""
    if STATE.strict:
        raise GateReadError(f"could not read `gh {' '.join(map(str, args))}` ({why})")
    return None


@contextlib.contextmanager
def strict(gh_bin: str | None):
    saved = (STATE.strict, STATE.gh_bin)
    STATE.strict, STATE.gh_bin = True, gh_bin
    try:
        yield
    finally:
        STATE.strict, STATE.gh_bin = saved


PREFIX = "gh-shim: refused (close/merge gate, harmonic-forge#1447): "
BUILTINS = frozenset({
    "alias", "api", "attestation", "auth", "browse", "cache", "co", "codespace",
    "completion", "config", "extension", "gist", "gpg-key", "help", "issue", "label",
    "org", "pr", "project", "release", "repo", "ruleset", "run", "search", "secret",
    "ssh-key", "status", "variable", "workflow", "--version", "--help", "-h", "version",
})
_MAYBE = ("close", "merge", "graphql", "--input")
_API_MUTATING = ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input")
_GRAPHQL_MUTATION = re.compile(
    r"\b(closeIssue|mergePullRequest|enablePullRequestAutoMerge|updateIssue|updatePullRequest)\b")


def could_be_close_or_merge(argv: list[str]) -> bool:
    """Cheap and import-light: True for every argv the gate must look at."""
    if not argv:
        return False
    if argv[0] not in BUILTINS:
        return True  # an alias could expand to anything
    if any(any(m in token for m in _MAYBE) for token in argv):
        return True
    # harmonic-forge#1447 pass 1: a plain `gh api` read (`api user --jq .login`)
    # is never a close; only a mutating api call can be.
    return argv[0] == "api" and _api_mutating(argv[1:])


def _api_mutating(rest: list[str]) -> bool:
    return any(t in _API_MUTATING or t.startswith(("-X", "--method=", "--field=", "--raw-field=",
                                                    "--input=", "-f", "-F"))
               for t in rest)


def _expand_alias(argv: list[str], real_gh: str) -> list[str] | None:
    """`argv` with a gh alias in argv[0] expanded once, or None when the alias
    list cannot be read. A `!` (shell) alias runs `sh`, whose own `gh` meets the
    shim again, so it is returned unchanged."""
    if not argv or argv[0] in BUILTINS:
        return argv
    try:
        done = subprocess.run([real_gh, "alias", "list"], capture_output=True, text=True, timeout=7)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode:
        return None
    for line in done.stdout.splitlines():
        name, sep, body = line.partition(":")
        if sep and name.strip() == argv[0]:
            body = body.strip().strip("'\"")
            if body.startswith("!"):
                return argv
            import shlex  # noqa: PLC0415
            return [*shlex.split(body), *argv[1:]]
    return argv


def _rest_close_target(tokens: list[str]) -> tuple[str | None, str, str] | None:
    """A `gh api` issue PATCH that closes, widened past the hook's parser: any
    non-GET method (or no method with a field, which gh sends as POST), and a
    `state=closed` field in any spelling, or an unreadable `--input` body."""
    import batch_auth  # noqa: PLC0415
    if len(tokens) < 2 or tokens[1] != "api":
        return None
    path = next((m for t in tokens[2:] for m in [batch_auth.API_ISSUE_PATH.search(t)] if m), None)
    if path is None:
        return None
    method, has_field, closes, has_input = None, False, False, False
    i = 2
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if t in ("-X", "--method"):
            method, i = nxt.upper(), i + 2
            continue
        if t.startswith("--method="):
            method = t.split("=", 1)[1].upper()
        elif t.startswith("-X") and len(t) > 2:
            method = t[2:].upper()
        field = None
        if t in ("-f", "-F", "--field", "--raw-field"):
            field, i = nxt, i + 1
        elif t.startswith(("--field=", "--raw-field=")):
            field = t.split("=", 1)[1]
        elif t.startswith(("-f", "-F")) and len(t) > 2:
            field = t[2:]
        if field is not None:
            has_field = True
            key, _, value = field.partition("=")
            value = value.strip().strip("'\"")
            if key.strip() == "state" and (value.lower() == "closed" or value.startswith("@")):
                closes = True  # `-F state=@file` reads the value from a file the gate cannot see
        if t == "--input" or t.startswith("--input="):
            has_input = True
        i += 1
    if method in ("GET", "HEAD") or (method is None and not has_field and not has_input):
        return None
    if not (closes or has_input):
        return None
    return (path.group(1), path.group(2), "issue")


def _graphql_refusal(tokens: list[str]) -> str | None:
    """Any `gh api` call whose endpoint is graphql, in any spelling or
    position (`graphql`, `/graphql`, after `-X POST`), carrying a close/merge
    mutation or a body the gate cannot read (`@file`, `$var`, `--input`)."""
    if len(tokens) < 3 or tokens[1] != "api":
        return None
    if not any(t.rstrip("/").endswith("graphql") and not t.startswith("-") for t in tokens[2:]):
        return None
    text = " ".join(tokens[2:])
    if ("@" in text or "$" in text or "--input" in text
            or _GRAPHQL_MUTATION.search(text) or "mutation" in text.lower()):
        return ("a `gh api graphql` close or merge mutation, or a query the gate cannot "
                "read, is refused outright: its target cannot be bound without another read")
    return None


def _apply_repo_env(tokens: list[str]) -> tuple[list[str], str | None]:
    """gh honors GH_REPO and GH_HOST, which the hooks never see."""
    host = os.environ.get("GH_HOST", "").strip()
    if host and host != "github.com":
        return tokens, f"GH_HOST={host} names another host; the gate checks github.com only"
    env_repo = os.environ.get("GH_REPO", "").strip()
    if not env_repo or len(tokens) < 2 or tokens[1] not in ("issue", "pr"):
        return tokens, None
    named = next((tokens[i + 1] for i, t in enumerate(tokens[:-1]) if t in ("-R", "--repo")), None)
    named = named or next((t.split("=", 1)[1] for t in tokens if t.startswith("--repo=")), None)
    if named is None:
        return [*tokens, "--repo", env_repo], None
    if named != env_repo:
        return tokens, f"GH_REPO={env_repo} and --repo {named} disagree"
    return tokens, None


def shim_verdict(argv: list[str], *, cwd: str, real_gh: str) -> str | None:
    """None to let the shim run `gh argv`; otherwise the refusal it prints.
    Any exception is the caller's to refuse on (fail closed)."""
    expanded = _expand_alias(list(argv), real_gh)
    if expanded is None:
        # harmonic-forge#1447 pass 1: an alias (`gh c 5`) need not spell close
        # or merge, so an unreadable alias list refuses every unknown command.
        return PREFIX + "the gh alias list could not be read to expand this command"
    tokens, conflict = _apply_repo_env(["gh", *expanded])
    if conflict:
        return PREFIX + conflict
    refusal = _graphql_refusal(tokens)
    if refusal:
        return PREFIX + refusal
    import block_missing_preclose_inspection as preclose  # noqa: PLC0415
    import block_undetermined_phase_close as phase  # noqa: PLC0415
    parsed = (preclose._parse_issue_close(tokens) or preclose._parse_pr_merge(tokens)
              or preclose._parse_api_merge(tokens) or preclose._parse_api_close(tokens)
              or _rest_close_target(tokens))
    if not parsed:
        import batch_auth  # noqa: PLC0415
        endpoints = [t for t in tokens[2:] if not t.startswith("-") and "/" in t]
        unbound = [t for t in endpoints
                   if re.search(r"(^|/)(issues/[^/\s]+|pulls/[^/\s]+/merge)/?$", t)
                   and not batch_auth.API_ISSUE_PATH.search(t)]
        if len(tokens) > 1 and tokens[1] == "api" and _api_mutating(tokens[2:]) and unbound:
            # harmonic-forge#1447 pass 1 (cross-family): a placeholder or other
            # spelling the parsers cannot bind to a number is refused.
            return PREFIX + "a mutating issues/pulls API call whose target the gate cannot read"
        return None
    exempt = (parsed[2] == "issue" and not any(t == "--input" or t.startswith("--input=") for t in tokens)
              and preclose._canonical_not_planned_close(tokens))
    try:
        with strict(real_gh):
            verdict = preclose.decide_targets(
                [(*parsed, exempt)], cwd,
                lambda number: preclose.merge_head_commits_from_tokens(tokens, number),
                strict_repo=True)
            if verdict:
                return PREFIX + verdict[0]
            verdict = phase.segment_verdict(tokens, cwd, {}, strict_repo=True)
            if verdict:
                return PREFIX + verdict[0]
    except GateReadError as exc:
        return (PREFIX + f"{exc}; a close or merge fails closed when its gate cannot be "
                "evaluated. Retry once GitHub is reachable.")
    return None
