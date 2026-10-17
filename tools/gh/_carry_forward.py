#!/usr/bin/env python3
"""A patch-identical rebase carries a Lane 3 PASS forward (harmonic-forge#1036).

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
5. the branch's own code diff is unchanged: `git patch-id --verbatim` (not
   `--stable`, which ignores whitespace) of `git diff $(git merge-base
   <from-sha> origin/main) <from-sha>` equals that of `git diff origin/main
   <sha>`, after removing from both diffs the `"version":` lines of any
   `package.json`, the two top-level version lines of `package-lock.json`
   (root and `packages[""]`), and the whole section of every file the project
   declares under `[project.protocol] rebase_regenerate` (F1057 amendment) --
   so a `lane1-rebase` regeneration commit contributes nothing. A binary
   change in either diff refuses, since patch-id cannot see inside it;
6. CI on `--sha` is `green` under the repo's REQUIRED checks
   (`gate_ci.ci_conclusion(..., required=gate_ci.required_checks(repo))`).
   `pending` and `absent` are WAIT refusals: re-post once CI finishes;
7. the issue is not `tooling-exception` (no Lane 3 gate exists there).

Not reused: `_standing_grant._tree_identical`, which compares files at two
commits; a rebase legitimately changes every file `main` changed, so the
invariant here is the branch's own diff, a patch-id over each base.

`validate()` returns EVERY refusal, never only the first. A refusal is a
`Refusal` (a `str`) whose `.wait` says it is a CI wait rather than a
substantive refusal.

Merge-time check (operator ruling 6219929298, Q1): a hand-typed
`kind=carry-forward` footer looks the same as one `l1_post.py` accepted, so
Lane 1 re-runs the checks immediately before `gh pr merge`:

    python3 tools/gh/_carry_forward.py check --repo R --issue N --branch B

Exit codes: 0 every check holds; 1 a substantive refusal (the PASS does not
cover the head: `ready-for-l3` and a fresh AE); 2 no carry-forward found or
bad usage; 3 only CI-wait refusals.
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

WAIT_EXIT = 3
REFUSE_EXIT = 1
USAGE_EXIT = 2

KIND = "carry-forward"
_BODY_SHA_MARKER = re.compile(r"<!--\s*l1-post\s+v1;.*?\bbody-sha256=[0-9a-f]{64}\b", re.I | re.S)
_REMOTE_URL = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                         r"(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$")
_FOOTER_FIELD = {
    "sha": re.compile(r"(?<![\w-])sha=([0-9a-f]{7,40})\b"),
    "from-sha": re.compile(r"(?<![\w-])from-sha=([0-9a-f]{7,40})\b"),
    "pass": re.compile(r"(?<![\w-])pass=(\d+)\b"),
}
_DIFF_FLAGS = ("--no-ext-diff", "--no-textconv", "--no-color")
_SECTION = re.compile(r"^diff --git a/(?P<a>.*) b/(?P<b>.*)$")
_VERSION_LINE = re.compile(r'^[+-]\s*"version"\s*:')
_KEY_OPEN = re.compile(r'^[ +-](\s*)"([^"]*)"\s*:\s*\{')

GitRunner = Callable[..., subprocess.CompletedProcess]


class Refusal(str):
    """One failed check. `wait` marks a CI `pending`/`absent` refusal."""

    wait: bool = False

    def __new__(cls, text: str, wait: bool = False) -> "Refusal":
        obj = super().__new__(cls, text)
        obj.wait = wait
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
    return out


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
                           f"({(main or 'unresolved')[:12]})"))
    return out


def filter_diff(diff: str, excluded: set[str] | frozenset[str] = frozenset()) -> str:
    """The diff with its version lines and declared generated files removed.

    - every `^[+-]\\s*"version":` line, inside a `package.json` section only
      (never the whole-diff `grep -v`, which would also hide a `"version":`
      change in any other file);
    - in a `package-lock.json` section, only the root `"version"` (indent 2)
      and `packages[""].version` (the line under a `"": {` opener seen in the
      same hunk) -- any other lock difference survives and refuses;
    - the whole section of a path in `excluded`.

    A hunk left with no `+`/`-` line is dropped, and a file section left with
    no hunk: `lane1-rebase` resolves a lock version conflict to origin/main's
    lock unchanged, so the rebased diff has no lock section at all while the
    gated diff had a version-only one.
    """
    sections: list[tuple[list[str], list[list[str]]]] = []
    name, drop, opener = "", False, None
    for line in diff.splitlines(keepends=True):
        section = _SECTION.match(line.rstrip("\n"))
        if section:
            name = section.group("b")
            drop = name in excluded or section.group("a") in excluded
            opener = None
            if not drop:
                sections.append(([line], []))
            continue
        if drop or not sections:
            continue
        header, hunks = sections[-1]
        if line.startswith("@@"):
            opener = None
            hunks.append([line])
            continue
        if not hunks:
            header.append(line)
            continue
        base = name.rsplit("/", 1)[-1]
        if base == "package-lock.json":
            key = _KEY_OPEN.match(line)
            if key:
                opener = (len(key.group(1)), key.group(2))
        if _VERSION_LINE.match(line):
            if base == "package.json":
                continue
            if base == "package-lock.json":
                indent = len(line[1:]) - len(line[1:].lstrip(" "))
                if indent == 2 or (indent == 6 and opener == (4, "")):
                    continue
        hunks[-1].append(line)
    out: list[str] = []
    for header, hunks in sections:
        kept = [h for h in hunks if any(line.startswith(("+", "-")) for line in h[1:])]
        if kept or not hunks:
            out += header
            for hunk in kept:
                out += hunk
    return "".join(out)


PATCH_ID_MODE = "--verbatim"  # never --stable: it ignores whitespace (assumption 5)


def _patch_id(text: str, cwd) -> str | None:
    proc = subprocess.run(["git", "patch-id", PATCH_ID_MODE], input=text, cwd=cwd,
                          capture_output=True, text=True)
    if proc.returncode:
        return None
    parts = proc.stdout.split()
    return parts[0] if parts else None


def check_patch_identity(from_sha: str, sha: str, git: GitRunner, cwd,
                         excluded: set[str] | frozenset[str] = frozenset()) -> list[Refusal]:
    """AC1 bullet 4 / AC4: the branch's own code diff is unchanged."""
    base = git("merge-base", from_sha, "refs/remotes/origin/main", cwd=cwd)
    if base.returncode or not base.stdout.strip():
        return [Refusal(f"cannot find the merge base of {from_sha[:12]} and origin/main")]
    old_base = base.stdout.strip()
    pairs = ((old_base, from_sha), ("refs/remotes/origin/main", sha))
    for lo, hi in pairs:
        numstat = git("diff", *_DIFF_FLAGS, "--numstat", lo, hi, cwd=cwd)
        if numstat.returncode:
            return [Refusal(f"cannot diff {lo[:12]}..{hi[:12]}")]
        if any(line.startswith("-\t-\t") and line.split("\t", 2)[2] not in excluded
               for line in numstat.stdout.splitlines()):
            return [Refusal(f"{lo[:12]}..{hi[:12]} carries a binary change, which patch-id cannot compare")]
    ids = []
    for lo, hi in pairs:
        diff = git("diff", *_DIFF_FLAGS, lo, hi, cwd=cwd)
        if diff.returncode:
            return [Refusal(f"cannot diff {lo[:12]}..{hi[:12]}")]
        ids.append(_patch_id(filter_diff(diff.stdout, excluded), cwd))
    old_id, new_id = ids
    if not old_id or not new_id:
        return [Refusal(f"patch-id is empty for {'the gated diff' if not old_id else 'the rebased diff'} "
                        "after the version and generated-file filter, so the code diffs cannot be compared")]
    if old_id != new_id:
        return [Refusal(f"patch-id differs: the gated diff ({old_id[:12]}) and the rebased diff "
                        f"({new_id[:12]}) are not the same code change")]
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
    found = [(int(c["id"]), f) for c in comments if (f := footer_fields(c.get("body") or ""))]
    return max(found, key=lambda pair: pair[0])[1] if found else None


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
        return USAGE_EXIT, [f"no kind=carry-forward comment on {repo}#{issue}"]
    refusals = validate(repo, issue, int(fields["pass"]), fields["from-sha"], fields["sha"], branch,
                        run, git, cwd=cwd, comments=comments, labels=labels, excluded=excluded)
    if not refusals:
        return 0, []
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
    if code == 0:
        print(f"[carry-forward] {args.repo}#{args.issue}: the carried PASS covers origin/{args.branch}")
    else:
        print(f"[carry-forward] {args.repo}#{args.issue}: the carried PASS does NOT cover "
              f"origin/{args.branch}:\n" + "\n".join(f"{n}. {r}" for n, r in enumerate(refusals, 1)),
              file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
