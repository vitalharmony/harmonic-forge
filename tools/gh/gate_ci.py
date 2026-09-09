#!/usr/bin/env python3
"""A Lane 3 PASS may not outrun the PR's own CI (harmonic-forge#504).

## The hole this closes

2026-09-07, measured:

| Time | Event |
|---|---|
| 22:34 | last green `main` — `1dd4d8b` |
| 00:12 | **Lane 3 Gate Results — harmonic-forge#494 — PASS** |
| 00:13 | `main` CI on the F494 merge commit `0359854`: **failure**, 3 tests |
| 03:19 | `main` CI on `3be6c75`: **failure**, the same 3 tests |

The gate said PASS forty seconds before CI said failure on the same code, and
`main` stayed red for three hours across two merges with no lane noticing.

The three failures were environment-bound — they read the operator's live
`~/.claude/settings.json`, which exists on this machine and not on a runner.
**Lane 3 runs on the operator's machine**, so the gate is structurally
incapable of catching that class, and that class is exactly what gets written
when the feature under test *is* operator-local state.

This is not a Lane 3 performance failure. Lane 3 executed its spec correctly
and the spec passed. The gate's environment is simply not the environment the
merge has to survive.

## Why a check and not a sentence in the procedure

`testing-gate.md` could say "also look at CI" — and this repo's own repeated
finding is that prose compliance degrades under context pressure, which is
precisely the condition a long gate run creates. AC1 says so outright: *"a
prose instruction alone does not satisfy this."* So the refusal lives at the
one place a gate result becomes real, `post_lane_discussion.py --kind
gate-result`, and a PASS that cannot show a green CI conclusion for the SHA it
names does not get posted.

## What it does NOT do

It does not gate FAIL or BLOCKED. Those are reports of a problem and must
always be publishable — a verification layer that can silence a failure report
is worse than none. Only PASS, the verdict that authorises a merge, has to
clear this bar.

## The general shape, named because this is an instance of it

*A verification asserts against an endpoint, and the endpoint that matters is
the last one, not the first one convenient to read.* The amendment on #504
cites an independent witness a week and a domain away: an AI-generated test
suite passing 14 of 14 while the application charged the wrong amount, because
the prompt asserted what the screen displayed and never what was charged. The
fix was one line — *follow the money all the way through.* Structurally
identical to `00:12 PASS -> 00:13 CI failure`: the gate asserted the first
green thing it could read.
"""
from __future__ import annotations

import json
import re
import subprocess

#: The verdict, from the gate report's own text. `lane_state.py` reads the
#: same shorthand; this deliberately does not import it, because that module
#: lives in HRSE2 and this check has to work for every repo.
_VERDICT = re.compile(r"(?im)^\s*(?:\*\*)?(?:Verdict|Result)(?:\*\*)?\s*[:—-]\s*"
                      r"(?:\*\*)?\s*(PASS|FAIL|BLOCKED)\b")
_VERDICT_INLINE = re.compile(r"(?im)^#{1,4}[ \t]*Lane 3 Gate Results\b.*?\b(PASS|FAIL|BLOCKED)\b")

#: The SHA the report claims to have gated (AC2). Full-length or abbreviated to
#: at least 7, the git minimum for an unambiguous short hash here.
#: `\**` on BOTH sides of the colon, because Markdown bolds the label together
#: with its colon — `**Head-SHA:** abc123`, not `**Head-SHA**: abc123`. The
#: first draft allowed only the latter, so it read no SHA from any real report
#: and every PASS was refused for the wrong reason.
_SHA = re.compile(r"(?im)^[^\S\n]*\**[^\S\n]*(?:Head[- ]SHA|Gated[- ]SHA|SHA)"
                  r"[^\S\n]*\**[^\S\n]*[:—-][^\S\n]*\**[^\S\n]*"
                  r"`?([0-9a-f]{7,40})`?")

#: Conclusions GitHub reports for a finished check run that mean "did not pass".
_BAD = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
#: Conclusions that are finished and fine. `neutral` and `skipped` are not
#: failures; a skipped job is a job that correctly decided it had nothing to do.
_GOOD = {"success", "neutral", "skipped"}


def verdict_of(body: str) -> str | None:
    """PASS / FAIL / BLOCKED, or None when the body states none."""
    match = _VERDICT.search(body) or _VERDICT_INLINE.search(body)
    return match.group(1).upper() if match else None


def gated_sha(body: str) -> str | None:
    """The head SHA the report says it gated (AC2), or None."""
    match = _SHA.search(body)
    return match.group(1) if match else None


def ci_conclusion(repo: str, sha: str, run=None) -> tuple[str, str]:
    """`(state, detail)` for a commit's checks.

    `state` is one of `green`, `red`, `pending`, `absent`, `unknown`.

    **`absent` is NOT green.** A commit with no check runs has not passed CI;
    it has not been asked. Treating an empty list as "nothing failing" is the
    same mistake in miniature that this whole module exists to prevent, and I
    made it in a wait-loop while implementing this issue: the loop tested
    "every check completed", an empty list satisfied it vacuously, and it
    reported done on a PR whose CI had not yet registered.
    """
    run = run or _run
    code, out = run(["gh", "api", f"repos/{repo}/commits/{sha}/check-runs",
                     "--jq", ".check_runs"])
    if code != 0:
        return "unknown", f"could not read checks for {sha[:8]}: {out.strip()[:200]}"
    try:
        runs = json.loads(out or "[]") or []
    except ValueError:
        return "unknown", f"unparseable check-run payload for {sha[:8]}"
    if not runs:
        return "absent", f"{sha[:8]} has no check runs at all"

    pending = [r["name"] for r in runs if r.get("status") != "completed"]
    if pending:
        return "pending", f"still running: {', '.join(sorted(pending))}"
    bad = [f"{r['name']}:{r.get('conclusion')}" for r in runs
           if (r.get("conclusion") or "").lower() in _BAD]
    if bad:
        return "red", ", ".join(sorted(bad))
    unknown = [f"{r['name']}:{r.get('conclusion')}" for r in runs
               if (r.get("conclusion") or "").lower() not in _GOOD]
    if unknown:
        return "unknown", "unrecognised conclusion(s): " + ", ".join(sorted(unknown))
    return "green", ", ".join(sorted(f"{r['name']}:{r.get('conclusion')}" for r in runs))


def check_gate_result(repo: str, body: str, run=None) -> tuple[bool, str]:
    """May this gate report be posted? `(ok, message)`.

    Refuses only a PASS. FAIL and BLOCKED are reports of a problem and must
    always be publishable.
    """
    verdict = verdict_of(body)
    if verdict != "PASS":
        return True, f"verdict is {verdict or 'unstated'}; CI check does not apply"

    sha = gated_sha(body)
    if sha is None:
        return False, (
            "[GATE] A PASS must state the head SHA it gated (harmonic-forge#504 "
            "AC2). Add a line like `Head-SHA: <sha>` so a reader can tell what "
            "was actually checked rather than trusting the verdict.")

    state, detail = ci_conclusion(repo, sha, run=run)
    if state == "green":
        return True, f"[GATE] CI green for {sha[:8]} ({detail})"
    if state == "red":
        return False, (
            f"[GATE] REFUSED: CI is RED for the SHA this PASS names.\n"
            f"  {sha[:8]}: {detail}\n"
            "A green local gate is necessary and not sufficient — Lane 3 runs on "
            "the operator's machine, which is not the environment the merge has "
            "to survive. Post FAIL, or fix the failure and re-gate.")
    # pending / absent / unknown all land here, and all mean the same thing:
    # the second signal has not been read yet. AC3 says waiting is the correct
    # behaviour, not a judgment call.
    return False, (
        f"[GATE] REFUSED: CI has not completed for the SHA this PASS names.\n"
        f"  {sha[:8]}: {state} — {detail}\n"
        "Report BLOCKED and wait (harmonic-forge#504 AC3). 'No checks yet' is "
        "not 'nothing failing'.")


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, result.stdout if result.returncode == 0 else result.stderr


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--file", type=Path, required=True,
                        help="gate report body to check")
    args = parser.parse_args(argv)
    ok, message = check_gate_result(args.repo, args.file.read_text(encoding="utf-8"))
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
