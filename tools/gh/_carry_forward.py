#!/usr/bin/env python3
"""A rebase that is still the gated change carries a Lane 3 PASS forward (harmonic-forge#1036).

A PASS covers only the SHA it names (R-0354). A branch that only needed a
rebase onto a newer `main` used to go back through `ready-for-l3`, an AE, a
sweep and a full gate on code Lane 3 had already passed. `l1_post.py --kind
carry-forward` posts a `## Gate carry-forward` comment instead, and only when
every check below holds, each re-derived live from git and GitHub at post time:

1. the named comment is a Lane 3 gate report on this issue, verdict PASS,
   `Head-SHA` equal to `--from-sha`, and an intact `body-sha256` marker that
   is PRESENT (`check_lane3_ready.verify_body_sha256` returns True when the
   marker is absent, so presence is checked first, as `_standing_grant.py`
   does);
2. no gate report on the issue is newer than that PASS;
3. the checkout's origin is `--repo`;
4. `--sha` is the current `origin/<branch>` head and contains `origin/main`;
5. `--sha` is the gated change on a newer base: replaying `--from-sha` onto
   `--sha`'s merge base with `origin/main` gives exactly `--sha`'s tree, the
   JSON version keys of `package.json` / `package-lock.json` aside
   (`preclose_passes.accepts`, F1077's rule, with harmonic-forge#1086's
   version-only conflict). A declared generated file that differs refuses
   (operator ruling on gap 5, option A);
6. CI on `--sha` is `green` under the repo's REQUIRED checks
   (`gate_ci.ci_conclusion(..., required=gate_ci.required_checks(repo))`).
   `pending` and `absent` are WAIT refusals: re-post once CI finishes;
7. the issue is not `tooling-exception` (no Lane 3 gate exists there).

Reused, not re-implemented: the replay-and-compare is the one rule every
preclose consumer already uses (sticky-wicket REFORGE 6240719003).

`validate()` returns EVERY refusal, never only the first. A refusal is a
`Refusal` (a `str`) whose `.wait` says it is a CI wait rather than a
substantive refusal.

Merge-time check (operator ruling 6219929298, Q1): a hand-typed
`kind=carry-forward` footer looks the same as one `l1_post.py` accepted, so
Lane 1 re-runs the checks immediately before `gh pr merge`:

    python3 tools/gh/_carry_forward.py check --repo R --issue N --branch B

Exit codes: 0 every check holds; 1 a substantive refusal (the PASS does not
cover the head: `ready-for-l3` and a fresh AE); 2 no carry-forward governs the head (none, or a newer gate report supersedes it) or
bad usage; 3 only CI-wait refusals; 4 the base moved -- `origin/main`
advanced past the carried head while the head is still the gated change
(`preclose_passes.accepts`), so `lane1-rebase` then a new carry-forward,
not `ready-for-l3`.

The PASS comment must also be unedited by GitHub's own record: its REST
`updated_at` must equal `created_at`. The body-sha256 marker is an unkeyed
digest anyone editing the comment can recompute, so it proves nothing alone.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import check_lane3_ready as clr  # noqa: E402
import gate_ci  # noqa: E402
import preclose_passes as pp  # noqa: E402

WAIT_EXIT = 3
BASE_EXIT = 4
REFUSE_EXIT = 1
USAGE_EXIT = 2

KIND = "carry-forward"
# Presence is checked with the verifier's OWN regex, so a marker the verifier
# cannot find (e.g. on its own line: no DOTALL there) counts as absent.
_BODY_SHA_MARKER = clr.FOOTER_BODY_SHA
_REMOTE_URL = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                         r"(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$")
_FOOTER_FIELD = {
    "sha": re.compile(r"(?<![\w-])sha=([0-9a-f]{7,40})\b"),
    "from-sha": re.compile(r"(?<![\w-])from-sha=([0-9a-f]{7,40})\b"),
    "pass": re.compile(r"(?<![\w-])pass=(\d+)\b"),
}

GitRunner = Callable[..., subprocess.CompletedProcess]


class Refusal(str):
    """One failed check. `wait` marks a CI `pending`/`absent` refusal; `base`
    marks a stale-base refusal (the head does not contain origin/main)."""

    wait: bool = False
    base: bool = False

    def __new__(cls, text: str, wait: bool = False, base: bool = False) -> "Refusal":
        obj = super().__new__(cls, text)
        obj.wait = wait
        obj.base = base
        return obj


def _git(*args: str, cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _watch():
    import watch_lane_posts  # noqa: PLC0415 -- lazy: the network reader
    return watch_lane_posts


def _detail(body: str) -> str:
    found = _watch()._classify(body)
    return found[1] if found else ""


def is_gate_report(body: str) -> bool:
    return _watch()._is_gate_report(_detail(body), body)


# ---------------------------------------------------------------- the checks

def check_pass_comment(comments: list[dict], pass_comment_id: int, from_sha: str) -> list[Refusal]:
    """AC1 bullet 1: a gate report on this issue, PASS, Head-SHA = from_sha, body hash present and intact."""
    comment = next((c for c in comments if int(c.get("id", -1)) == int(pass_comment_id)), None)
    if comment is None:
        return [Refusal(f"comment {pass_comment_id} is not a comment on this issue")]
    body = comment.get("body") or ""
    out: list[Refusal] = []
    if not is_gate_report(body):
        out.append(Refusal(f"comment {pass_comment_id} is not a Lane 3 gate report"))
    # F1036 epoch 2 survivor 1: only Lane 3's own post is a PASS to carry.
    marker = _watch()._own_marker(body) or ""
    poster = _watch()._POSTED_BY_RE.search(marker)
    if not poster or poster.group(1).upper() != "LANE3":
        out.append(Refusal(f"comment {pass_comment_id} was not posted by Lane 3 "
                           f"(posted-by={poster.group(1) if poster else 'absent'})"))
    verdict = gate_ci.verdict_of(body)
    if verdict != "PASS":
        out.append(Refusal(f"comment {pass_comment_id}'s verdict is {verdict or 'unreadable'}, not PASS"))
    gated = gate_ci.gated_sha(body)
    if not clr.same_sha(gated, from_sha):
        out.append(Refusal(f"comment {pass_comment_id} gated {(gated or 'no SHA')[:12]}, "
                           f"not --from-sha {from_sha[:12]}"))
    if not _BODY_SHA_MARKER.search(body):
        out.append(Refusal(f"comment {pass_comment_id} carries no body-sha256 marker, "
                           "so it cannot be shown unedited"))
    elif not clr.verify_body_sha256(comment):
        out.append(Refusal(f"comment {pass_comment_id} was edited after it was posted "
                           "(body-sha256 mismatch)"))
    created, updated = _when(comment.get("created_at")), _when(comment.get("updated_at"))
    if created is None or updated is None:
        out.append(Refusal(f"comment {pass_comment_id} has no readable created_at/updated_at, "
                           "so it cannot be shown unedited"))
    elif updated > created:
        out.append(Refusal(f"comment {pass_comment_id} was edited after it was posted "
                           f"(updated_at {comment.get('updated_at')} is after created_at "
                           f"{comment.get('created_at')})"))
    return out


def _when(value):
    from datetime import datetime  # noqa: PLC0415
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def check_no_newer_gate_report(comments: list[dict], pass_comment_id: int) -> list[Refusal]:
    """AC1 bullet 2: the named PASS is the issue's newest gate report."""
    newer = [int(c["id"]) for c in comments
             if int(c.get("id", -1)) > int(pass_comment_id) and is_gate_report(c.get("body") or "")]
    if newer:
        return [Refusal(f"a newer gate report ({max(newer)}) supersedes PASS {pass_comment_id}")]
    return []


def check_repo(repo: str, git: GitRunner, cwd) -> list[Refusal]:
    remote = git("remote", "get-url", "origin", cwd=cwd)
    match = _REMOTE_URL.match(remote.stdout.strip()) if remote.returncode == 0 else None
    found = match.group("repo") if match else None
    if not found or found.lower() != repo.lower():
        return [Refusal(f"the checkout's origin is {found or 'unreadable'}, not {repo}")]
    return []


def _resolve(git: GitRunner, cwd, ref: str) -> str | None:
    proc = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd)
    if proc.returncode:
        return None
    return proc.stdout.strip() or None


def fetch(branch: str, from_sha: str, git: GitRunner, cwd) -> list[Refusal]:
    """`lane1_rebase.fetch`'s explicit refspecs, plus an explicit fetch of --from-sha."""
    proc = git("fetch", "origin", "+refs/heads/main:refs/remotes/origin/main",
               f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=cwd)
    if proc.returncode:
        return [Refusal(f"cannot fetch origin/main and origin/{branch}: {proc.stderr.strip()[:200]}")]
    if _resolve(git, cwd, from_sha) is None:
        git("fetch", "origin", from_sha, cwd=cwd)
    if _resolve(git, cwd, from_sha) is None:
        return [Refusal(f"--from-sha {from_sha[:12]} does not resolve to a commit, even after a fetch")]
    return []


def check_head(branch: str, sha: str, git: GitRunner, cwd) -> list[Refusal]:
    """AC1 bullet 3: --sha is origin/<branch>'s head and contains origin/main."""
    head = _resolve(git, cwd, f"refs/remotes/origin/{branch}")
    main = _resolve(git, cwd, "refs/remotes/origin/main")
    out: list[Refusal] = []
    if not clr.same_sha(head, sha):
        out.append(Refusal(f"--sha {sha[:12]} is not origin/{branch}'s head "
                           f"({(head or 'unresolved')[:12]})"))
    if not main or git("merge-base", "--is-ancestor", main, sha, cwd=cwd).returncode:
        out.append(Refusal(f"--sha {sha[:12]} does not contain origin/main "
                           f"({(main or 'unresolved')[:12]})", base=bool(main)))
    return out


def check_patch_identity(from_sha: str, sha: str, git: GitRunner, cwd,
                         excluded: set[str] | frozenset[str] = frozenset()) -> list[Refusal]:
    """AC1 bullet 4 / AC4: ``sha`` is the gated change on a newer base.

    Sticky-wicket REFORGE (6240719003) replaced the patch-id comparison with
    F1077's change-identity rule, ``preclose_passes.accepts``: replay the gated
    commit onto ``sha``'s merge base with ``origin/main`` and require exactly
    ``sha``'s tree, the JSON version keys aside (harmonic-forge#1086 accepts a
    replay that conflicts only there). ``excluded`` is kept for the caller's
    signature and deliberately unused: a declared generated file that differs
    refuses, and goes back through Lane 3 (operator ruling, gap 5, option A)."""
    del excluded
    old = git("merge-base", from_sha, "refs/remotes/origin/main", cwd=cwd)
    if old.returncode or not old.stdout.strip():
        return [Refusal(f"cannot find the merge base of {from_sha[:12]} and origin/main")]
    new = git("merge-base", sha, "refs/remotes/origin/main", cwd=cwd)
    if new.returncode or not new.stdout.strip():
        return [Refusal(f"cannot find the merge base of {sha[:12]} and origin/main")]
    recorded = {"head_sha": from_sha, "merge_base_sha": old.stdout.strip()}
    if not pp.accepts(recorded, sha, cur_base=new.stdout.strip(), repo_dir=cwd):
        return [Refusal(f"{sha[:12]} is not the gated change {from_sha[:12]} on a newer base: replaying it "
                        "does not give exactly that tree (JSON version keys aside)")]
    return []


def check_ci(repo: str, sha: str, run=None) -> list[Refusal]:
    """AC1 bullet 5: CI green under the REQUIRED checks; pending/absent wait."""
    state, detail = gate_ci.ci_conclusion(repo, sha, run=run,
                                          required=gate_ci.required_checks(repo, run=run))
    if state == "green":
        return []
    if state in ("pending", "absent"):
        return [Refusal(f"CI on {sha[:12]} is {state} ({detail}); wait for CI to finish, "
                        "then re-post", wait=True)]
    return [Refusal(f"CI on {sha[:12]} is {state} ({detail}), not green")]


def check_not_tooling_exception(labels: set[str] | None) -> list[Refusal]:
    if labels is None:
        return [Refusal("cannot read the issue's labels")]
    if "tooling-exception" in labels:
        return [Refusal("the issue is tooling-exception: no Lane 3 gate exists to carry forward")]
    return []


def declared_generated(cwd) -> set[str]:
    """`[project.protocol] rebase_regenerate` paths, read as `lane1-rebase` reads them."""
    sys.path.insert(0, str(_HERE.parent / "lane"))
    import lane1_rebase  # noqa: PLC0415
    declared, _ = lane1_rebase.regenerate_config(str(cwd or "."))
    return set(declared)


def validate(repo: str, issue: int, pass_comment_id: int, from_sha: str, sha: str, branch: str,
             run=None, git: GitRunner | None = None, *, cwd=None,
             comments: list[dict] | None = None, labels: set[str] | None = None,
             excluded: set[str] | None = None) -> list[Refusal]:
    """Every refusal for carrying PASS `pass_comment_id` from `from_sha` to `sha`."""
    git = git or _git
    watch = _watch()
    if comments is None:
        comments = watch._fetch_all_comments(repo, issue)
    if labels is None:
        labels = watch._read_issue_meta(repo, issue)[1]
    refusals: list[Refusal] = []
    if comments is None:
        refusals.append(Refusal(f"cannot read the comments of {repo}#{issue}"))
    else:
        refusals += check_pass_comment(comments, pass_comment_id, from_sha)
        refusals += check_no_newer_gate_report(comments, pass_comment_id)
    refusals += check_repo(repo, git, cwd)
    fetched = fetch(branch, from_sha, git, cwd)
    refusals += fetched
    if not fetched:
        refusals += check_head(branch, sha, git, cwd)
        if excluded is None:
            excluded = declared_generated(cwd)
        refusals += check_patch_identity(from_sha, sha, git, cwd, excluded)
    refusals += check_ci(repo, sha, run)
    refusals += check_not_tooling_exception(labels)
    return refusals


# ---------------------------------------------------------------- merge-time check

def footer_fields(body: str) -> dict[str, str] | None:
    """`sha`/`from-sha`/`pass` of a comment whose OWN marker is kind=carry-forward."""
    marker = _watch()._own_marker(body)
    if not marker or not re.search(r"\bkind=carry-forward\b", marker):
        return None
    fields = {}
    for name, pattern in _FOOTER_FIELD.items():
        match = pattern.search(marker)
        if not match:
            return None
        fields[name] = match.group(1)
    return fields


def newest_carry_forward(comments: list[dict]) -> dict[str, str] | None:
    """The newest carry-forward's fields, or None when there is none or a
    gate report is newer than it (F1036 preclose survivor 3): a newer gate
    report gates the head directly, so no carry-forward governs it."""
    found = [(int(c["id"]), f) for c in comments if (f := footer_fields(c.get("body") or ""))]
    if not found:
        return None
    newest_id, fields = max(found, key=lambda pair: pair[0])
    if any(int(c.get("id", -1)) > newest_id and is_gate_report(c.get("body") or "") for c in comments):
        return None
    return fields


def check(repo: str, issue: int, branch: str, run=None, git: GitRunner | None = None, *,
          cwd=None, comments: list[dict] | None = None, labels: set[str] | None = None,
          excluded: set[str] | None = None) -> tuple[int, list[str]]:
    """Re-run every check against the newest carry-forward's own fields."""
    if comments is None:
        comments = _watch()._fetch_all_comments(repo, issue)
    if comments is None:
        return REFUSE_EXIT, [f"cannot read the comments of {repo}#{issue}"]
    fields = newest_carry_forward(comments)
    if fields is None:
        return USAGE_EXIT, [f"no kind=carry-forward governs {repo}#{issue} (none posted, or a newer "
                            "gate report supersedes it: merge on that report, R-0354)"]
    refusals = validate(repo, issue, int(fields["pass"]), fields["from-sha"], fields["sha"], branch,
                        run, git, cwd=cwd, comments=comments, labels=labels, excluded=excluded)
    if not refusals:
        return 0, []
    if all(r.wait or r.base for r in refusals) and any(r.base for r in refusals):
        return BASE_EXIT, list(refusals)
    return (WAIT_EXIT if all(r.wait for r in refusals) else REFUSE_EXIT), list(refusals)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check", help="merge-time re-validation of the newest carry-forward")
    chk.add_argument("--repo", required=True)
    chk.add_argument("--issue", type=int, required=True)
    chk.add_argument("--branch", required=True)
    args = parser.parse_args(argv)
    try:
        sys.path.insert(0, str(_HERE.parent / "onboard"))
        from manifest_identity import apply_project_identity  # noqa: PLC0415
        apply_project_identity(args.repo)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- the read then fails loudly on its own
        print(f"[carry-forward] identity not applied: {exc}", file=sys.stderr)
    code, refusals = check(args.repo, args.issue, args.branch)
    if code == BASE_EXIT:
        print(f"[carry-forward] {args.repo}#{args.issue}: origin/main moved past origin/{args.branch}; "
              "the head is still the gated change -- run lane1-rebase, then post a new carry-forward "
              "(not ready-for-l3):\n" + "\n".join(f"{n}. {r}" for n, r in enumerate(refusals, 1)),
              file=sys.stderr)
    elif code == 0:
        print(f"[carry-forward] {args.repo}#{args.issue}: the carried PASS covers origin/{args.branch}")
    else:
        print(f"[carry-forward] {args.repo}#{args.issue}: the carried PASS does NOT cover "
              f"origin/{args.branch}:\n" + "\n".join(f"{n}. {r}" for n, r in enumerate(refusals, 1)),
              file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
