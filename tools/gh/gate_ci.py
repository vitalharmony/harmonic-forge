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
prose instruction alone does not satisfy this."*

## Keyed on the BODY, never on the author's chosen stamp

The first draft fired only for `post_lane_discussion.py --kind gate-result`.
That is the author declaring what they are posting, and preclose inspection
found the obvious consequence: omit the flag and the check never runs, while
`lane_state.py` still scores the comment `gate.pass` from its heading. 74 of 98
real `Lane 3 Gate Results` comments across both repos carry no
`kind=gate-result` footer at all, so the bypass was not a corner case — it was
the majority path.

A gate report is whatever has the `## Lane 3 Gate Results` heading, which is
exactly how `lane_state.py` decides, and the two must not derive it from
different text.

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

#: The gate-report heading. `lane_state.py` treats this as AUTHORITATIVE when
#: the `kind=` footer disagrees, and it is right to: 74 of 98 real
#: `Lane 3 Gate Results` comments across both repos carry no
#: `kind=gate-result` footer at all. So this module keys on the heading too —
#: keying on the author's chosen stamp meant omitting `--kind gate-result`
#: skipped the check entirely while `lane_state.py` still scored the comment
#: `gate.pass`. That is the exact 00:12-PASS / 00:13-CI-failure sequence this
#: module exists to prevent, reachable by leaving a flag off.
GATE_HEADING = re.compile(r"(?im)^#{1,4}[ \t]*Lane 3 Gate Results\b(?P<rest>.*)$")

#: The verdict, and ONLY from a place a verdict may legitimately live: the
#: heading, or the lead block above the first `###`/`<details>`.
#:
#: An unanchored whole-body search was wrong in both directions. A per-case
#: line — `**Result:** FAIL on TC4's optional half only; overall PASS.` — made
#: a PASS report read as FAIL and skip the CI check; and hrse#373's genuine
#: FAIL report, whose per-TC lines say `**Result:** PASS`, read as PASS and
#: would have been routed into the refusal path, which is the "worse than
#: none" outcome this module's own docstring warns about.
_LEAD_VERDICT = re.compile(r"(?im)^[^\S\n]*\**[^\S\n]*(?:Verdict|Result)"
                           r"[^\S\n]*\**[^\S\n]*[:—-][^\S\n]*\**[^\S\n]*"
                           r"(PASS|FAIL|BLOCKED)\b")
_HEADING_VERDICT = re.compile(r"\b(PASS|FAIL|BLOCKED)\b")

#: Where the lead block ends: the first section heading or collapsible.
_LEAD_END = re.compile(r"(?im)^(?:#{3,6}[ \t]|<details)")


def looks_like_a_gate_report(body: str) -> bool:
    """Is this body a Lane 3 gate report, whatever it was stamped?"""
    return GATE_HEADING.search(body) is not None


def _lead_block(body: str) -> str:
    """The text above the first `###` section — where the verdict may live.

    Everything below is per-case detail, and a per-case `Result:` is not the
    gate's verdict.
    """
    match = GATE_HEADING.search(body)
    start = match.end() if match else 0
    rest = body[start:]
    end = _LEAD_END.search(rest)
    return rest[: end.start()] if end else rest


def verdict_of(body: str) -> str | None:
    """PASS / FAIL / BLOCKED, or None.

    Returns `"CONFLICT"` when the heading and the lead block disagree — which
    is refused rather than resolved. A report whose own two statements of its
    verdict differ has not stated one.
    """
    heading = GATE_HEADING.search(body)
    from_heading = None
    if heading:
        found = _HEADING_VERDICT.search(heading.group("rest") or "")
        from_heading = found.group(1).upper() if found else None

    found = _LEAD_VERDICT.search(_lead_block(body))
    from_lead = found.group(1).upper() if found else None

    if from_heading and from_lead and from_heading != from_lead:
        return "CONFLICT"
    return from_heading or from_lead


#: `\**` on BOTH sides of the colon, because Markdown bolds the label together
#: with its colon — `**Head-SHA:** abc123`, not `**Head-SHA**: abc123`. The
#: first draft allowed only the latter, so it read no SHA from any real report
#: and every PASS was refused for the wrong reason.
_SHA = re.compile(r"(?im)^[^\S\n]*\**[^\S\n]*(?:Head[- ]SHA|Gated[- ]SHA|SHA)"
                  r"[^\S\n]*\**[^\S\n]*[:—-][^\S\n]*\**[^\S\n]*"
                  r"`?([0-9a-f]{7,40})`?")

#: Conclusions GitHub reports for a finished check run that mean "did not pass".
_BAD = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
#: Finished and fine. `neutral` and `skipped` are not failures — a skipped job
#: is one that correctly decided it had nothing to do, and every hrse PR has
#: two (`build-and-push`, `open-image-bump-pr`), so treating them as red would
#: refuse every legitimate PASS in that repo.
_GOOD = {"success", "neutral", "skipped"}


#: Real reports state the SHA in the HEADING, not as a labelled line:
#: `## Lane 3 Gate Results — H1739 (..., at `f09eeeff`)` and `... @ `3b8c55ec``.
#: The label-anchored regex alone read a SHA from 0 of 23 real gate comments,
#: so a docstring claiming it "reads real reports" was simply wrong.
_HEADING_SHA = re.compile(r"(?:\bat\b|@)[^\S\n]*`([0-9a-f]{7,40})`")


def gated_sha(body: str) -> str | None:
    """The head SHA the report says it gated (AC2), or None.

    Checked in both the labelled-line form this issue asks for and the
    heading form the corpus actually uses.
    """
    match = _SHA.search(body)
    if match:
        return match.group(1)
    heading = GATE_HEADING.search(body)
    if heading:
        found = _HEADING_SHA.search(heading.group("rest") or "")
        if found:
            return found.group(1)
    return None


def required_checks(repo: str, branch: str = "main", run=None) -> set[str] | None:
    """Names of the branch's REQUIRED status checks, or None if unreadable.

    AC1 says "the PR's own **required** checks". The first draft said "any
    check run ever attached to this SHA", and the difference is not academic:
    hrse `main`'s tip carries thirty runs of a repeatedly re-dispatched
    `render-and-deploy` dashboard workflow, one of them `cancelled`. Its only
    required check, `verify`, is `success`. Reading every run poisoned that SHA
    permanently for gate purposes, and the refusal's own advice — "fix the
    failure and re-gate" — is unactionable, because you cannot un-cancel a
    historical run of an unrelated scheduled workflow.

    None means "could not establish", and the caller then falls back to
    considering every check. That fallback is the stricter direction.
    """
    run = run or _run
    code, out = run(["gh", "api", f"repos/{repo}/branches/{branch}/protection",
                     "--jq", ".required_status_checks.contexts"])
    if code != 0:
        return None
    try:
        contexts = json.loads(out or "null")
    except ValueError:
        return None
    return set(contexts) if isinstance(contexts, list) and contexts else None


def _latest_per_name(runs: list[dict]) -> list[dict]:
    """One run per check name — the most recent.

    `filter=latest` does NOT dedupe these: thirty same-named runs came back
    from a single request. A historical run of a workflow that has since
    succeeded is not evidence about this commit's health.
    """
    newest: dict[str, dict] = {}
    for entry in runs:
        name = entry.get("name") or ""
        stamp = entry.get("completed_at") or entry.get("started_at") or ""
        if name not in newest or stamp >= (newest[name].get("completed_at")
                                           or newest[name].get("started_at") or ""):
            newest[name] = entry
    return list(newest.values())


def ci_conclusion(repo: str, sha: str, run=None,
                  required: set[str] | None = None) -> tuple[str, str]:
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
    # `--paginate`, because the default page is 30 and hrse's `main` tip
    # already carries exactly thirty runs. The first draft's `--jq .check_runs`
    # also discarded `total_count`, so a truncated list could not even be
    # DETECTED — the same "a partial list read as nothing failing" error one
    # level up.
    #
    # `--jq` streams one object per line here rather than emitting an array,
    # and `--slurp` (which would wrap the pages) is rejected outright when
    # combined with `--jq` — confirmed against gh 2.99.0:
    # "the `--slurp` option is not supported with `--jq` or `--template`".
    # So: line-delimited output, parsed per line.
    code, out = run(["gh", "api", "--paginate",
                     f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
                     "--jq", ".check_runs[]"])
    if code != 0:
        return "unknown", f"could not read checks for {sha[:8]}: {out.strip()[:200]}"
    runs = []
    try:
        for line in (out or "").splitlines():
            line = line.strip()
            if line:
                runs.append(json.loads(line))
    except ValueError:
        return "unknown", f"unparseable check-run payload for {sha[:8]}"
    if not runs:
        return "absent", f"{sha[:8]} has no check runs at all"

    runs = _latest_per_name(runs)
    if required:
        scoped = [r for r in runs if r.get("name") in required]
        missing = sorted(required - {r.get("name") for r in runs})
        if missing:
            return "absent", (f"{sha[:8]} has not run its required check(s): "
                              + ", ".join(missing))
        runs = scoped

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


def stale_against_pr(repo: str, sha: str, run=None) -> tuple[bool, str]:
    """Is the gated SHA behind the head of the PR it belongs to?

    The SHA is self-declared, and the report is written after the gate ran. A
    correction pushed while the report is being typed produces exactly the
    original incident plus one push: Lane 3 truthfully gated commit A, which is
    green; the PR now heads at B, which is red; the report names A and the
    check reads A. `(True, ...)` means refuse.
    """
    run = run or _run
    code, out = run(["gh", "api", f"repos/{repo}/commits/{sha}/pulls",
                     "--jq", "[.[] | select(.state == \"open\") "
                             "| {number, head: .head.sha}]"])
    if code != 0:
        return False, "could not resolve the SHA to a PR"
    try:
        pulls = json.loads(out or "[]") or []
    except ValueError:
        return False, "unparseable PR payload"
    for pull in pulls:
        head = (pull.get("head") or "")
        if head and not head.startswith(sha) and not sha.startswith(head):
            return True, (f"PR #{pull.get('number')} now heads at {head[:8]}, "
                          f"not the {sha[:8]} this PASS gated")
    return False, "SHA is current for its PR"


def check_gate_result(repo: str, body: str, run=None) -> tuple[bool, str]:
    """May this gate report be posted? `(ok, message)`.

    Refuses only a PASS. FAIL and BLOCKED are reports of a problem and must
    always be publishable.
    """
    verdict = verdict_of(body)
    if verdict == "CONFLICT":
        return False, (
            "[GATE] REFUSED: the heading and the lead block state DIFFERENT "
            "verdicts. A report whose own two statements of its verdict "
            "disagree has not stated one — say which it is.")
    if verdict is None:
        if looks_like_a_gate_report(body):
            # Fail CLOSED on a gate report whose verdict cannot be read.
            # `L3P`, `Conditional PASS — 8/9 cases`, and `✅ PASS` all satisfy
            # `validate_lead` (which requires the LINE, never its content) and
            # all derived to None — so each posted with the CI check silently
            # skipped, indistinguishable from a genuine green. Two of the
            # twenty-three real reports in the corpus also carry no verdict
            # word in the heading, so this is the live shape, not a corner.
            return False, (
                "[GATE] REFUSED: this is a gate report and its verdict could "
                "not be read. State it plainly as `**Verdict:** PASS` / `FAIL` "
                "/ `BLOCKED`, or in the heading. A hedged or decorated verdict "
                "is not a verdict, and silently skipping the CI check on one "
                "is indistinguishable from passing it.")
        return True, "not a gate report; CI check does not apply"
    if verdict != "PASS":
        return True, f"verdict is {verdict}; CI check does not apply"

    sha = gated_sha(body)
    if sha is None:
        return False, (
            "[GATE] A PASS must state the head SHA it gated (harmonic-forge#504 "
            "AC2). Add a line like `Head-SHA: <sha>` so a reader can tell what "
            "was actually checked rather than trusting the verdict.")

    stale, why = stale_against_pr(repo, sha, run=run)
    if stale:
        return False, (
            f"[GATE] REFUSED: the SHA this PASS names is not the current head.\n"
            f"  {why}\n"
            "The gate ran against code that is no longer what would merge. "
            "Re-gate at the current head.")

    state, detail = ci_conclusion(repo, sha, run=run,
                                  required=required_checks(repo, run=run))
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
