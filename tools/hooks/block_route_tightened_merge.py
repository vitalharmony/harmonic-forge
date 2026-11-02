#!/usr/bin/env python3
"""PreToolUse hook: a merge whose diff route tightened is refused (harmonic-forge#1243).

Semantic routing A4a, child of harmonic-forge#1229. A3's handoff snapshot
records the path an issue's work was planned on (`_route_snapshot.py`), but
until this hook nothing compared it with the diff that actually merges: "Shadow
only -- nothing reads the route to grant or refuse anything yet"
(`tools/gh/_route_snapshot.py:8-9`). This is the first reader that refuses.

On every `gh pr merge` (and the `gh api` PUT merge form) the hook maps the PR
to its issue, evaluates the hard gates (`gates.evaluate`) on the PR's changed
files, and compares that route with the baseline: the issue's newest `handoff`
route event, else the route in force over every event not written here.

- Not stricter: allow.
- No route event at all: record the diff's route at checkpoint `pre-merge` and
  allow with a warning (operator ruling R2).
- Baseline path 3 and the diff falls to path 2 only through the `te-label`
  gate: allow with a warning, because runner eligibility is not carried yet
  (R7, child C).
- Stricter: record the tightened route at `pre-merge` (once per path and gate),
  then refuse unless the stricter path's clearer holds for the PR's head --
  path 1: the issue's newest gate report is a Lane 3 PASS naming that head
  (R1); path 2 from path 3: `preclose-inspected` plus a receipt covering the
  head (R6). Recording never clears anything: the baseline ignores this hook's
  own `pre-merge` events, so a retry is refused the same way (AC2).

Fails closed, unlike the opt-in hooks it borrows from: GitHub unreadable, a
corrupt route store, an unexpected error, or the internal deadline
(`DEADLINE_SECONDS`, below the 25 s registration timeout, since a timed-out
hook lets the command run) each deny. A PR that maps to no issue is allowed
with a warning, as `block_missing_preclose_inspection.py` allows it.

Parsing is not re-implemented: merge commands, repo resolution, labels, the
head SHA, the preclose receipt and `--match-head-commit` all come from
`block_missing_preclose_inspection.py`, and gate reports are read only through
`_carry_forward`'s checks. The operator's FORCE `route` line is A4b
(harmonic-forge#1244); its seam is `clearers`.

Trust level (good-faith): an author session can append to the route store
directly, and a forged `posted-by=LANE3` marker passes `check_pass_comment`.
This hook inherits both residuals and does not lower either.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "routing"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gh"))

import block_missing_preclose_inspection as pre  # noqa: E402
import gates  # noqa: E402
import route_store  # noqa: E402

DECIDER = "pre-merge-recheck"
CHECKPOINT = "pre-merge"
#: Below the 25 s registration timeout: a hook that times out lets the command run.
DEADLINE_SECONDS = 20.0
#: The longest one `gh` read can take (`_gh`'s `run_gh` timeout); a read starts
#: only when this much time is left.
READ_CEILING = 7.0
RUNNER_WARNING = ("runner (path 3) merge exempt from the route recheck until child C "
                  "carries runner eligibility (harmonic-forge#1229)")
#: `... (harmonic-forge#1242)` -- the issue a PR is for, named in its title
#: (operator ruling 2026-11-02, "1 A"). The slug is a repo under the PR's owner.
TITLE_ISSUE = re.compile(r"\((?P<slug>[A-Za-z0-9_.-]+)#(?P<number>\d+)\)\s*$")


class Refused(Exception):
    """The merge is refused; the message says why."""


class Deadline:
    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def need(self, seconds: float, what: str) -> None:
        if time.monotonic() + seconds > self.end:
            raise Refused(f"Blocked: the route recheck ran out of time before {what} "
                          f"(internal deadline {DEADLINE_SECONDS:g} s, below the hook's "
                          f"timeout, which would let the merge through unchecked). "
                          f"Retry once GitHub responds faster.")


def _read(deadline: Deadline, what: str, *args: str, repo: str):
    deadline.need(READ_CEILING, f"reading {what}")
    return pre._gh(*args, repo=repo)


def _pr_facts(deadline: Deadline, repo: str, pr: str) -> dict | None:
    out = _read(deadline, f"PR #{pr}", "api", f"repos/{repo}/pulls/{pr}",
                "--jq", "{title, changed_files}", repo=repo)
    try:
        return json.loads(out) if out else None
    except ValueError:
        return None


def _pr_files(deadline: Deadline, repo: str, pr: str) -> list[dict] | None:
    out = _read(deadline, f"PR #{pr}'s files", "api", "--paginate",
                f"repos/{repo}/pulls/{pr}/files", "--jq",
                ".[] | {filename, previous_filename}", repo=repo)
    if out is None:
        return None
    try:
        return [json.loads(line) for line in out.splitlines() if line.strip()]
    except ValueError:
        return None


def _comments(deadline: Deadline, repo: str, issue: str) -> list[dict] | None:
    out = _read(deadline, f"{repo}#{issue}'s comments", "api", "--paginate",
                f"repos/{repo}/issues/{issue}/comments", "--jq", ".[]", repo=repo)
    if out is None:
        return None
    try:
        return [json.loads(line) for line in out.splitlines() if line.strip()]
    except ValueError:
        return None


def issue_for_pr(deadline: Deadline, repo: str, pr: str, title: str) -> tuple[str, str] | None:
    """(issue repo, issue number) the PR is for; raises when unreadable.

    The title's `(<slug>#N)` suffix wins, so a cross-repo half (an hrse PR on
    branch `feat/1243-...` for harmonic-forge#1243) never resolves to hrse#1243.
    Without one, the branch number in the PR's own repo (`issues_closed_by_pr`)."""
    match = TITLE_ISSUE.search(title or "")
    if match:
        return f"{repo.split('/')[0]}/{match.group('slug')}", match.group("number")
    deadline.need(READ_CEILING, f"reading PR #{pr}'s branch")
    issues = pre.issues_closed_by_pr(repo, pr)
    if issues is None:
        raise Refused(f"Blocked: could not read which issue PR #{pr} in {repo} is for, so "
                      f"its route cannot be rechecked (harmonic-forge#1243 AC7).")
    return (repo, issues[0]) if issues else None


def _checkpoint(event: dict) -> str | None:
    return (event.get("attrs") or {}).get("checkpoint")


def baseline(prior: list[dict]) -> dict:
    """The path the work took: the newest handoff event, else the route in force.

    `prior` excludes this hook's own `pre-merge` events, so a tightening it
    recorded never becomes the baseline it is compared against (AC2). The
    in-force rule is `route_store.current`'s, over `prior` only."""
    handoffs = [e for e in prior if _checkpoint(e) == "handoff"]
    if handoffs:
        return handoffs[-1]
    evaluated = floor = None
    for event in prior:
        if event["attrs"].get("decider") == route_store.OVERRIDE:
            if floor is None or int(event["attrs"]["path"]) < int(floor["attrs"]["path"]):
                floor = event
        else:
            evaluated = event
    if evaluated is None:
        return floor
    if floor is not None and int(floor["attrs"]["path"]) < int(evaluated["attrs"]["path"]):
        return floor
    return evaluated


def _newest_pass_at(comments: list[dict], head: str) -> bool:
    """True when the issue's newest gate report is a Lane 3 PASS gated at `head`."""
    cf = _carry_forward()
    for comment in comments:
        if not cf.is_gate_report(comment.get("body") or ""):
            continue
        cid = int(comment.get("id", -1))
        if not cf.check_pass_comment(comments, cid, head) and \
                not cf.check_no_newer_gate_report(comments, cid):
            return True
    return False


def _carry_forward():
    import _carry_forward  # noqa: PLC0415 -- the gate-report grammar's one reader
    return _carry_forward


def clearers(deadline: Deadline, repo: str, issue: str, pr_repo: str, pr: str, head: str,
             diff: gates.Route, base_path: int, labels: set[str]) -> tuple[bool, str]:
    """Whether the stricter path's clearer holds for `head`, and what is missing.

    A4b (harmonic-forge#1244) adds the operator's FORCE `route` grant here."""
    if diff.path == 1:
        comments = _comments(deadline, repo, issue)
        if comments is None:
            raise Refused(f"Blocked: could not read {repo}#{issue}'s comments, so no Lane 3 "
                          f"PASS can be shown for head {head[:12]} (harmonic-forge#1243 AC7).")
        if _newest_pass_at(comments, head):
            return True, "a Lane 3 PASS names the head"
        return False, (f"a Lane 3 PASS gate report naming head {head[:12]} as the issue's "
                       f"newest gate report")
    if diff.path == 2 and base_path == 3:
        missing = f"{pre.PRECLOSE_LABEL!r} with a pre-close receipt covering head {head[:12]}"
        if pre.PRECLOSE_LABEL not in labels:
            return False, missing
        deadline.need(pre.FETCH_BUDGET_SECONDS, "checking the pre-close receipt")
        if pre._preclose_receipt_ok(repo, issue, head, pr):
            return True, "a pre-close receipt covers the head"
        return False, missing
    return False, f"a clearer for path {diff.path} (none is defined)"


def _route_text(path, gate_id, extra: str = "") -> str:
    return f"path {path} (gate {gate_id}{extra})"


def recheck(deadline: Deadline, command: str, pr_repo: str, pr: str) -> str | None:
    """Recheck one merge target. Returns a warning line, None, or raises Refused."""
    facts = _pr_facts(deadline, pr_repo, pr)
    if facts is None:
        raise Refused(f"Blocked: could not read PR #{pr} in {pr_repo}, so its route cannot be "
                      f"rechecked (harmonic-forge#1243 AC7).")
    target = issue_for_pr(deadline, pr_repo, pr, facts.get("title") or "")
    if target is None:
        return f"route recheck skipped: PR #{pr} maps to no issue"
    repo, issue = target
    key = f"{repo}#{issue}"

    deadline.need(READ_CEILING, f"reading {key}'s labels")
    labels = pre.labels_for(repo, issue)
    deadline.need(READ_CEILING, f"reading PR #{pr}'s head")
    head = pre._pr_head_sha(pr_repo, pr)
    files = _pr_files(deadline, pr_repo, pr)
    unread = [name for name, value in (("the issue's labels", labels), ("the PR's head SHA", head),
                                       ("the PR's changed files", files)) if value is None]
    if unread:
        raise Refused(f"Blocked: could not read {', '.join(unread)} for PR #{pr} ({key}), so "
                      f"its route cannot be rechecked (harmonic-forge#1243 AC7).")
    changed = facts.get("changed_files")
    if not isinstance(changed, int) or len(files) < changed:
        raise Refused(f"Blocked: GitHub listed {len(files)} of PR #{pr}'s {changed} changed "
                      f"files, so the route of the whole diff cannot be evaluated "
                      f"(harmonic-forge#1243 AC7).")
    paths = sorted({p for f in files for p in (f.get("filename"), f.get("previous_filename")) if p})
    diff = gates.evaluate(gates.Scope(repo=repo, halves={pr_repo: paths}, labels=sorted(labels),
                                      threat_model=None, runner_eligible=False))

    try:
        events = route_store.load(key)
    except (RuntimeError, ValueError) as exc:
        raise Refused(f"Blocked: the route store cannot be read for {key} ({exc}), so the "
                      f"merge's route cannot be rechecked (harmonic-forge#1243 AC7).") from exc
    mine = [e for e in events if _checkpoint(e) == CHECKPOINT]
    prior = [e for e in events if _checkpoint(e) != CHECKPOINT]
    diff_text = _route_text(diff.path, diff.gate_id, f": {diff.reason}")

    if not prior:
        if not mine:
            route_store.record(key, diff, decider=DECIDER, checkpoint=CHECKPOINT)
        return f"no route recorded for {key}; recorded {diff.path}/{diff.gate_id} at pre-merge"

    base = baseline(prior)["attrs"]
    base_path = int(base["path"])
    if diff.path >= base_path:
        return None
    # R7 as ruled: only a diff short of path 3 for want of runner eligibility
    # (the te-label gate) is exempt; any other stricter gate is enforced.
    if base_path == 3 and diff.gate_id == "te-label":
        return RUNNER_WARNING

    if not any(int(e["attrs"]["path"]) == diff.path and e["attrs"].get("gate_id") == diff.gate_id
               for e in mine):
        route_store.record_if_not_looser(key, diff, decider=DECIDER, checkpoint=CHECKPOINT)

    cleared, why = clearers(deadline, repo, issue, pr_repo, pr, head, diff, base_path, labels)
    if not cleared:
        raise Refused(
            f"Blocked: PR #{pr}'s diff routes {key} stricter than the path its work took "
            f"(harmonic-forge#1243).\n"
            f"  baseline: {_route_text(base_path, base.get('gate_id'))}, "
            f"checkpoint {base.get('checkpoint')}\n"
            f"  diff:     {diff_text}\n"
            f"  missing:  {why}\n\n"
            f"The tightened route is recorded at checkpoint pre-merge; recording it does not "
            f"clear the merge. A retry is refused the same way until the head carries what "
            f"path {diff.path} requires.")
    given = pre.merge_head_commits(command, pr)
    if any((value or "").lower() != head.lower() for value in given):
        raise Refused(pre._match_head_message(repo, issue, pr, head))
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        pre._allow()
        return
    if data.get("tool_name") != "Bash":
        pre._allow()
        return
    command = (data.get("tool_input") or {}).get("command", "")
    payload_cwd = data.get("cwd") or None

    targets = [t for t in (pre.find_gated_targets(command) or []) if t[2] == "pr" and t[1]]
    if not targets:
        # No PR number: the preclose hook owns that deny.
        pre._allow()
        return

    deadline = Deadline(DEADLINE_SECONDS)
    warnings: list[str] = []
    try:
        for explicit_repo, number, _kind in targets:
            deadline.need(READ_CEILING, "resolving the repo")
            repo = pre.resolve_repo(explicit_repo, payload_cwd)
            if repo is None:
                raise Refused(f"Blocked: could not resolve the repo of PR #{number}, so its "
                              f"route cannot be rechecked (harmonic-forge#1243 AC7). Pass --repo.")
            warning = recheck(deadline, command, repo, number)
            if warning:
                warnings.append(warning)
    except Refused as exc:
        pre._deny(str(exc))
        return
    except Exception as exc:  # noqa: BLE001 -- a refusal hook fails closed
        pre._deny(f"Blocked: the route recheck failed ({type(exc).__name__}: {exc}), so the "
                  f"merge's route cannot be shown not to have tightened (harmonic-forge#1243).")
        return
    print(json.dumps({"systemMessage": "\n".join(warnings)} if warnings else {}))


if __name__ == "__main__":
    main()
