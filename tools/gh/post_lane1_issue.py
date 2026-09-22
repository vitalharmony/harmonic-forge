#!/usr/bin/env python3
"""Validate and set a Lane 1 issue body -- a new stub's, or an amendment to an existing one.

Two modes, because they are two different jobs (a private-repo incident):

* **fill** -- the issue body is still `gh-new-issue`'s placeholder. The full
  new-issue schema applies: routing declaration, approach section, a
  backticked path, a numbered test case, an allowed label.
* **amend** -- the body is already substantive. Those shape checks assert
  properties of the *original filing*, which this session did not perform and
  cannot retroactively supply, so requiring them means Lane 1 cannot correct
  so much as a stale line in any issue it did not file itself. Since
  `block_lane1_status_claims.py` denies Lane 1 the raw `gh issue edit`
  transport, that made every pre-existing body unmaintainable.

Mode is inferred from the live body and can be forced with `--amend`. What
amend mode keeps are the *honesty* checks -- placeholder text, the reserved
attestation namespace, the supersedes-names-its-issue rule, and a
destructive-shrink tripwire. It drops only the checks about shape.

The shrink check is a tripwire for gross operator error (a `--body-file`
pointed at the wrong, much smaller file), not a content guarantee: it cannot
see a same-length rewrite that removes everything meaningful, and a longer
body passes it unconditionally. The empty-body rejection above it is what
carries the real weight.

Real incident (2026-08-22): correcting a two-line stale blocker reference on
harmonic-forge#322 was rejected twice on shape grounds, neither related to the
edit. The tempting workaround was to reword the issue to match the validator
-- editing the artifact to fit the tool.
"""

import argparse
import re
from pathlib import Path

from l1_post import (
    fail, is_substantive, regular_body, reject_reserved_marker, resolve_repo, run,
)

ALLOWED_LABELS = {"bug", "feature", "tech-debt", "ui", "infrastructure"}
PLACEHOLDER_BODY = "*Created via mise gh-new-issue*"
# An amendment may legitimately shorten a body (deleting a stale section is a
# normal edit). It may not gut it: `edit_issue_body` PATCHes the whole body, so
# a malformed amend silently destroys the issue, and GitHub's UI offers no
# recovery short of the edit history. Anything below this ratio needs the
# operator to say so explicitly.
SHRINK_FLOOR = 0.75
PATH_TOKEN = re.compile(r"`[^`\s]+/[^`\s]+\.[A-Za-z0-9]+`")
# a private-repo incident: this previously required the literal token "full 3-lane", so
# "3-lane, not Tooling Exception" -- an unambiguous declaration, and the
# wording real issues already use -- did not match. Widening it to a bare
# "3-lane" went too far the other way: `3-lane-protocol.md` cited as a file
# path matches, as does prose like "this is not 3-lane work", so the check
# degrades from "declares routing" to "mentions the string". So: the phrase
# must not be a filename (no trailing hyphen-word), and must share its line
# with a routing word. A validator that accepts any mention teaches authors
# nothing; one that demands a specific adjective teaches them to write for
# the regex.
_LANE_PHRASE = re.compile(r"(?:3|three)[ -]lane\b(?!-)", re.I)
_ROUTING_WORD = re.compile(r"rout(?:e[sd]?|ing)|cycle|loop|\bfull\b|not\s+tooling\s+exception", re.I)


def declares_routing(body: str) -> bool:
    """True when some line both names the 3-lane cycle and reads as a declaration."""
    return any(_LANE_PHRASE.search(line) and _ROUTING_WORD.search(line) for line in body.splitlines())
TEST_CASE = re.compile(
    r"(?im)^\s*(?:\d+\.|[-*])\s+.*\b(?:must|should|expected|returns?|den(?:y|ied)|allow(?:s|ed)|refuses?)\b"
)


def section_content(body: str, headings: tuple[str, ...]) -> str:
    alternatives = "|".join(re.escape(heading) for heading in headings)
    match = re.search(rf"(?ims)^## (?:{alternatives})\s*$\n(.*?)(?=^## |\Z)", body)
    return match.group(1).strip() if match else ""


def validate_body(body: str, labels: set[str]) -> None:
    if body.strip() == PLACEHOLDER_BODY:
        fail("substantive issue body may not be the gh-new-issue placeholder")
    tooling = section_content(body, ("Tooling Exception",))
    # a private-repo incident: previously required the literal token "full 3-lane", so
    # "3-lane, not Tooling Exception" -- an unambiguous declaration, and the
    # wording several real issues already use -- did not match. A validator
    # that requires a specific adjective teaches authors to write for the
    # regex rather than to declare routing.
    full_cycle = declares_routing(body)
    if not tooling and not full_cycle:
        fail("body must declare a Tooling Exception or full 3-lane routing")
    numbered_condition = re.search(r"(?m)^\s*[123][.)]\s+|\([123]\)", tooling)
    if tooling and not ("ADR-002" in tooling and numbered_condition):
        fail("Tooling Exception must name ADR-002 and at least one numbered condition")
    approach = section_content(body, ("Selected Approach", "Fix", "Design"))
    first_approach_line = next((line.strip() for line in approach.splitlines() if line.strip()), "")
    if not is_substantive(first_approach_line):
        fail("body requires a substantive ## Selected Approach, ## Fix, or ## Design section")
    if not PATH_TOKEN.search(body):
        fail("body requires at least one backticked file path")
    if not TEST_CASE.search(body):
        fail("body requires at least one concrete numbered or bulleted test case")
    if not labels & ALLOWED_LABELS:
        fail("issue requires one of these labels: bug, tech-debt, infrastructure")
    if "bug" in labels:
        if not section_content(body, ("Root Cause", "Problem", "Diagnosis")):
            fail("bug issue body requires a Root Cause, Problem, or Diagnosis section")
        if "```" not in body:
            fail("bug issue body requires fenced reproduction evidence")
    if re.search(r"\bcorrection to\b|\bsupersedes\b|\bprior filing\b", body, re.I) and not re.search(r"#\d+", body):
        fail("body that supersedes or corrects a prior filing must name its issue number")


def validate_amendment(body: str, current: str, allow_shrink: bool) -> None:
    """Honesty checks only -- see the module docstring for why shape is dropped."""
    if body.strip() == PLACEHOLDER_BODY:
        fail("amended issue body may not be the gh-new-issue placeholder")
    if not body.strip():
        fail("amended issue body may not be empty")
    if not is_substantive(body):
        fail("amended issue body still contains unfilled template placeholder text")
    # The one check in validate_body that is about honesty rather than shape --
    # and amend mode is exactly where "this corrects the prior description"
    # language shows up, so dropping it here would invert the split's principle.
    if re.search(r"\bcorrection to\b|\bsupersedes\b|\bprior filing\b", body, re.I) and not re.search(r"#\d+", body):
        fail("amended body that supersedes or corrects a prior filing must name its issue number")
    if not allow_shrink and current.strip() and len(body) < len(current) * SHRINK_FLOOR:
        fail(
            f"amended body is {len(body)} chars against the current {len(current)} -- "
            f"below the {SHRINK_FLOOR:.0%} floor. Re-run with --allow-shrink if the "
            "removal is intended; this guard exists because the edit replaces the "
            "whole body and GitHub offers no recovery outside the edit history."
        )


def issue_body(repo: str, issue: int) -> str:
    result = run("gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".body")
    if result.returncode:
        fail(f"cannot read the current body for {repo}#{issue}: {result.stderr.strip()}")
    return result.stdout


def issue_labels(repo: str, issue: int) -> set[str]:
    # harmonic-forge#220: REST migration off GraphQL-backed `gh issue
    # view` -- confirmed live, `.labels[].name` output is identical.
    result = run("gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".labels[].name")
    if result.returncode:
        fail(f"cannot read labels for {repo}#{issue}: {result.stderr.strip()}")
    return {label for label in result.stdout.splitlines() if label}


def edit_issue_body(repo: str, issue: int, body_file: Path, body: str) -> None:
    # harmonic-forge#220: REST migration off GraphQL-backed `gh issue
    # edit`/`gh issue view`.
    edited = run("gh", "api", "-X", "PATCH", f"repos/{repo}/issues/{issue}", "-F", f"body=@{body_file}")
    if edited.returncode:
        fail("GitHub issue edit failed: " + edited.stderr.strip())
    fetched = run("gh", "api", f"repos/{repo}/issues/{issue}", "--jq", ".body")
    if fetched.returncode or fetched.stdout.rstrip("\n") != body.rstrip("\n"):
        fail("edited issue body did not match the validated body")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and set a substantive Lane 1 issue body")
    parser.add_argument("--repo")
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument(
        "--amend",
        action="store_true",
        help=(
            "Force amend mode: honesty checks only, skipping the new-issue schema. "
            "Inference already covers every substantive body, so this flag's only "
            "reachable effect is to bypass the full schema on a stub -- an operator "
            "override, deliberately."
        ),
    )
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        # argparse runs help strings through %-formatting, so a literal percent
        # must be escaped -- an unescaped one raises on --help, not at parse
        # time, which is why only `wrapper-parity` (which shells out to
        # --help) caught it.
        help=f"Permit an amendment below {SHRINK_FLOOR:.0%} of the current body's length."
        .replace("%", "%%"),
    )
    args = parser.parse_args()
    args.repo = resolve_repo(args.repo)
    body = regular_body(args.body_file)
    reject_reserved_marker(body)
    current = issue_body(args.repo, args.issue)
    # `gh-new-issue` only defaults to PLACEHOLDER_BODY when neither --body nor
    # --body-file was passed, and --body-file is the *preferred* filing route
    # (a private-repo incident). So a stub filed with a short scoping note is still a stub:
    # matching the placeholder exactly would route it to amend mode and skip
    # the new-issue schema entirely -- silently, and after the PATCH landed.
    stub = not current.strip() or current.strip().startswith(PLACEHOLDER_BODY)
    amending = args.amend or not stub
    mode = "amend" if amending else "fill"
    # Announced BEFORE validating, so a mis-inference is visible while it can
    # still be aborted rather than discovered from the success line.
    print(f"[l1-issue] {mode} mode" + (" -- new-issue schema NOT applied" if amending else ""))
    if amending:
        validate_amendment(body, current, args.allow_shrink)
    else:
        validate_body(body, issue_labels(args.repo, args.issue))
    edit_issue_body(args.repo, args.issue, args.body_file, body)
    print(f"[l1-issue] {'amended' if amending else 'filled'} and refetched {args.repo}#{args.issue}")


if __name__ == "__main__":
    main()
