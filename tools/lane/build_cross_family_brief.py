#!/usr/bin/env python3
"""Build the cold, self-contained brief a cross-family `verify` call is
handed (harmonic-forge#598 AC4).

Why this exists rather than each agent writing its own brief inline: a brief
assembled by hand per run is a brief whose completeness depends on the caller
remembering what completeness means. Both advisory consumers
(`pitch-inspection`, `product-strategy`) need the same four things in it, and
`cross_family_call.sh`'s `verify` posture will return `invalid-report` for a
brief that carries no assumptions at all -- a failure that surfaces only after
the call has been spent, and the ONE-pass rule means there is no second one.

The four required sections, and why each is required:

  * **artifact** -- the reviewer starts cold. Embedded as text, not cited as
    a path: the brief must stand alone even though the `verify` reviewer
    happens to have read access, because a brief that only names paths
    silently degrades to "go find out what I meant" when a path moves.
  * **intent** -- what the artifact is trying to achieve. Without it the
    reviewer grades against a goal it invented.
  * **question** -- the specific thing being asked. A brief with no question
    gets a general opinion, which is what the calling agent already has.
  * **assumptions** -- at least one, because `verify` produces one verdict per
    asserted assumption and nothing else. Zero assumptions is a call with no
    product.
  * **evidence paths** -- at least one, and this is the section whose absence
    was the defect (harmonic-forge#598 preclose finding 1). Embedding the
    artifact makes the brief READABLE cold; it does not make the assumptions
    CHECKABLE. `verify`'s whole product is a verdict backed by output the
    reviewer actually obtained, and a reviewer with nothing to run returns
    all-`uncheckable` -- a call that spends the one pass, exits 0, reports
    `status: ok`, and checks nothing. The paths are resolved relative to the
    `--cwd` the call is given, which is why that must be the repository the
    artifact lives in and not an empty scratch directory.

What this deliberately does NOT carry: the caller's own reasoning for its
assumptions, and the caller's opinion of the artifact. Supplying either
re-introduces the prior the second family exists to escape (ADR-007). The
builder cannot detect an opinion smuggled into `--intent`, so that remains a
caller obligation stated in the agent prose -- not a guarantee claimed here.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

_HEADER = """# Cross-family review brief

You are reviewing this cold. You have no history with this artifact and no
access to the conversation that produced it. Everything you need is below.
"""

# harmonic-forge#648 -- `--evidence-run` lets the BUILDER (not the model)
# execute a read-only `gh api`/`npm view` command and embed its actual output,
# so a `verify` reviewer running under `--sandbox read-only` with no network
# (see cross_family_call.sh) can still be handed real GitHub/npm evidence.
#
# POSITIVE allowlist, never a denylist: `gh` accepts glued (`-fx=y`) and `=`
# (`--input=f`, `--field=a=b`) flag forms that a denylist scoped to space-
# separated tokens would miss entirely. Every token after `gh api <endpoint>`
# must be recognized and read-only, or the whole command is refused.
_GH_API_READ_FLAGS_WITH_VALUE = {"--jq", "-q", "-t", "--template"}
_GH_API_READ_FLAGS_BARE = {"--paginate", "--slurp"}

_EVIDENCE_RUN_TIMEOUT = 30
_TRUNCATE_AT = 8000


def _truncate(text: str) -> str:
    if len(text) > _TRUNCATE_AT:
        return text[:_TRUNCATE_AT] + "[truncated]"
    return text


def _gh_api_token_permitted(token: str, next_token: str | None) -> tuple[bool, bool]:
    """Returns (permitted, consumes_next) for one token of a `gh api` call.

    `consumes_next` is True when this token takes a following value (e.g.
    `--jq <expr>`) that must be skipped rather than independently validated.
    """
    lowered = token.lower()

    if lowered in _GH_API_READ_FLAGS_BARE:
        return True, False
    if lowered in _GH_API_READ_FLAGS_WITH_VALUE:
        return True, True
    # glued/`=` forms: --jq=... , -q=... , --template=...
    for flag in _GH_API_READ_FLAGS_WITH_VALUE:
        if lowered.startswith(flag + "="):
            return True, False
    if lowered == "-h":
        # Only the spaced form `-H 'Accept: ...'` is permitted; a glued
        # `-Hxxx` never reaches this branch (it fails the `== "-h"` test
        # below and falls through to the final refusal).
        if next_token is not None and next_token.lower().startswith("accept:"):
            return True, True
        return False, False
    if lowered in ("-x", "--method"):
        if next_token is not None and next_token.upper() == "GET":
            return True, True
        return False, False
    if lowered.startswith("--method="):
        return lowered[len("--method="):].upper() == "GET", False
    if lowered.startswith("-x") and len(lowered) > 2:
        return lowered[2:].upper() == "GET", False
    return False, False


def _gh_api_allowlisted(tokens: list[str]) -> bool:
    """`tokens` is everything after `gh api`. The endpoint is the first
    non-flag token anywhere in the list (per the spec); every other token
    must be exactly one recognized read-only flag."""
    if not tokens:
        return False
    endpoint_index = next(
        (i for i, t in enumerate(tokens) if not t.startswith("-")), None
    )
    if endpoint_index is None:
        return False
    endpoint = tokens[endpoint_index]
    if endpoint == "graphql":
        return False

    rest = tokens[:endpoint_index] + tokens[endpoint_index + 1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        next_token = rest[index + 1] if index + 1 < len(rest) else None
        permitted, consumes_next = _gh_api_token_permitted(token, next_token)
        if not permitted:
            return False
        index += 2 if consumes_next else 1
    return True


def _npm_view_allowlisted(tokens: list[str]) -> bool:
    """`tokens` is everything after `npm view`. Positional arguments only,
    zero flags of any kind."""
    return all(not token.startswith("-") for token in tokens)


def _evidence_run_allowlisted(argv: list[str]) -> bool:
    if len(argv) >= 2 and argv[0] == "gh" and argv[1] == "api":
        return _gh_api_allowlisted(argv[2:])
    if len(argv) >= 2 and argv[0] == "npm" and argv[1] == "view":
        return _npm_view_allowlisted(argv[2:])
    return False


def run_evidence(cmd: str) -> dict:
    """Parse and, if allowlisted, execute one `--evidence-run` command.

    Returns a dict with keys: cmd, exit (int or the string "timeout"),
    stdout, stderr. Raises ValueError (never runs anything) if the command
    is not allowlisted.
    """
    try:
        argv = shlex.split(cmd)
    except ValueError as exc:
        raise ValueError(f"refused --evidence-run (not allowlisted): {cmd}") from exc

    if not argv or not _evidence_run_allowlisted(argv):
        raise ValueError(f"refused --evidence-run (not allowlisted): {cmd}")

    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                               timeout=_EVIDENCE_RUN_TIMEOUT)
        return {
            "cmd": cmd,
            "exit": proc.returncode,
            "stdout": _truncate(proc.stdout),
            "stderr": _truncate(proc.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        return {
            "cmd": cmd,
            "exit": "timeout",
            "stdout": _truncate(stdout),
            "stderr": _truncate(stderr),
        }


def render_evidence_run_section(results: list[dict]) -> str:
    parts = ["\n## Pre-executed evidence (captured by the brief builder)\n"]
    for result in results:
        parts.append(
            f"\n```\n$ {result['cmd']}\nexit: {result['exit']}\n"
            f"--- stdout ---\n{result['stdout']}\n"
            f"--- stderr ---\n{result['stderr']}\n```\n"
        )
    return "".join(parts)


def _clean(text: str) -> str:
    return text.strip()


def build(
    artifacts: list[tuple[str, str]],
    intent: str,
    question: str,
    assumptions: list[str],
    evidence: list[str] | None = None,
    evidence_run_results: list[dict] | None = None,
) -> str:
    """Render the brief. Raises ValueError naming every missing section at
    once -- reporting only the first would make filling one in reveal the
    next, one round trip at a time."""
    missing = []
    if not artifacts:
        missing.append("--artifact (at least one)")
    # An artifact that exists but is empty is the same failure as no artifact
    # at all -- found by the cross-family reviewer on this mechanism's own
    # first live run (harmonic-forge#598), which is a fair demonstration of
    # why the branch exists: the in-family author checked that the FLAG was
    # supplied and never that it carried anything.
    for name, body in artifacts:
        if not _clean(body):
            missing.append(f"--artifact {name} (file is empty)")
    if not _clean(intent):
        missing.append("--intent")
    if not _clean(question):
        missing.append("--question")
    if not [a for a in assumptions if _clean(a)]:
        missing.append("--assumption (at least one)")
    if not [e for e in (evidence or []) if _clean(e)]:
        missing.append("--evidence (at least one path or command)")
    if missing:
        raise ValueError(
            "the brief would not be self-contained; missing: " + ", ".join(missing)
        )

    parts = [_HEADER, "\n## Design intent\n\n" + _clean(intent) + "\n"]
    parts.append("\n## The question\n\n" + _clean(question) + "\n")
    parts.append("\n## Asserted assumptions -- one verdict each\n")
    for index, assumption in enumerate(a for a in assumptions if _clean(a)):
        parts.append(f"\n{index + 1}. {_clean(assumption)}\n")
    parts.append(
        "\n## Where the evidence is\n\nResolve these from your working "
        "directory. Run what you need; a verdict without executed output is "
        "discarded.\n"
    )
    for item in (e for e in (evidence or []) if _clean(e)):
        parts.append(f"\n- `{_clean(item)}`\n")
    parts.append("\n## The artifact\n")
    for name, body in artifacts:
        parts.append(f"\n### `{name}`\n\n```\n{body.rstrip()}\n```\n")
    if evidence_run_results:
        parts.append(render_evidence_run_section(evidence_run_results))
    return "".join(parts)


def _read_artifacts(paths: list[str]) -> list[tuple[str, str]]:
    artifacts = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise ValueError(f"--artifact path is not a file: {raw}")
        artifacts.append((raw, path.read_text()))
    return artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", action="append", default=[],
                        help="path to a file whose CONTENTS are embedded; repeatable")
    parser.add_argument("--intent", default="", help="what the artifact is trying to achieve")
    parser.add_argument("--question", default="", help="the specific question for the reviewer")
    parser.add_argument("--assumption", action="append", default=[],
                        help="an asserted, unverified claim; repeatable, at least one required")
    parser.add_argument("--evidence", action="append", default=[],
                        help="a path or command the reviewer can run to check an "
                             "assumption, resolved from --cwd; repeatable, at least "
                             "one required")
    parser.add_argument("--evidence-run", action="append", default=[],
                        help="a read-only `gh api`/`npm view` command the BUILDER "
                             "executes itself and embeds verbatim (harmonic-forge#648); "
                             "repeatable. Refused if it is not on the positive "
                             "allowlist, in which case no brief is written.")
    parser.add_argument("--out", required=True, help="path to write the brief to")
    args = parser.parse_args(argv)

    evidence_run_results = []
    for cmd in args.evidence_run:
        try:
            evidence_run_results.append(run_evidence(cmd))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    try:
        artifacts = _read_artifacts(args.artifact)
        brief = build(artifacts, args.intent, args.question, args.assumption,
                      args.evidence, evidence_run_results)
    except ValueError as exc:
        print(f"build_cross_family_brief: {exc}", file=sys.stderr)
        return 2

    Path(args.out).write_text(brief)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
