#!/usr/bin/env python3
"""Plan an adversarial pre-close check for Tooling Exception work (hrse#1208).

Canonical platform copy (harmonic-forge#704, P2 of #208's portable 3-lane
protocol epic) -- HRSE2's ``scripts/preclose_check.py`` is now a ``runpy``
shim onto this file. ``--repo`` is required here, never defaulted, per
ADR-008 decision 2: a platform script defaulting to one consuming repo is
a two-tier violation in miniature.

The Tooling Exception is the one path where Lane 1 writes code directly and
then closes the loop on it -- the role boundary's whole purpose, a second set
of eyes, is suspended for exactly that class. This computes the panel that
fills the gap, and refuses a second pass on the same diff.

**It is not a gate and it emits no verdict.** A subagent panel whose findings
Lane 1 interprets and summarizes is still self-grading with one level of
indirection. Closure authority is unchanged: only the operator's explicit
`Close H<N>`/`Close F<N>`. This script takes no action on GitHub.

## Panel size

Blast radius is primary, Tier secondary -- deliberately, because every
incident in this class so far was a small diff: a hook that locked out Bash,
`l1_post.py`'s worktree-overlap check, a stale `harmonic-forge` checkout.
Sizing by diff size would have under-reviewed all three.

Anything touching git state, hooks, hook *wiring*, CI, or live data gets the
full panel regardless of size. Otherwise `Tier` scales it.

## Fail direction

This script is advisory, so most of its surface may fail toward planning a
larger panel -- over-reviewing costs credits, under-reviewing costs a defect.
Its one *enforced* invariant is "one pass, then escalate", and that is a gate
in miniature: an unreadable or corrupt receipt is therefore treated as **no
prior pass** (review again) rather than as a refusal, and never as a crash.
The receipt records completion, not intent -- see `complete_receipt`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# A change under any of these runs on every session, every commit, or every
# gate -- so its failure mode is silent and total rather than local.
HIGH_BLAST_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|/)tools/hooks/"), "hook implementations — fire on every tool call"),
    # hrse#1208 review: the patterns originally covered where hooks LIVE but not
    # where they are SWITCHED ON. The module's own motivating incident ("a hook
    # that locked out Bash") edits wiring, not implementation.
    (re.compile(r"(^|/)\.claude/settings(\.local)?\.json$"), "hook wiring — arms/disarms every hook"),
    (re.compile(r"(^|/)\.codex/hooks\.json$"), "hook wiring — Codex"),
    (re.compile(r"(^|/)agents/[^/]+\.md$"), "agent definitions — carry executable hooks: frontmatter"),
    (re.compile(r"(^|/)\.githooks/"), "git hooks — core.hooksPath, fire on every commit"),
    (re.compile(r"(^|/)scripts/gate_[^/]+\.py$"), "in-repo gate scripts"),
    (re.compile(r"(^|/)tools/lane/"), "lane launchers — every session in every repo starts here"),
    (re.compile(r"(^|/)\.github/workflows/"), "CI — gates every merge"),
    (re.compile(r"(^|/)scripts/l1_post\.py$"), "Lane 1 posting gate"),
    (re.compile(r"(^|/)scripts/post_lane1_issue\.py$"), "Lane 1 issue-body write path"),
    (re.compile(r"(^|/)scripts/.*migrat"), "migration — touches live data"),
    (re.compile(r"(^|/)backend/app/"), "application code — not tooling at all"),
    (re.compile(r"(^|/)frontend/src/"), "application code — not tooling at all"),
    (re.compile(r"(^|/)mise\.toml$"), "task definitions — the operator's command surface"),
)

# One lens per refuter. Diversity beats redundancy: three identical skeptics
# find the same thing three times.
LENSES: tuple[str, ...] = (
    "correctness — what input produces wrong output or a crash",
    "silent-bypass — what reaches the permissive branch that should not",
    "fail-direction — what happens when this cannot decide, and is that safe",
    "second-run — caches, receipts, state, concurrency, crash-midway",
    "test-honesty — would each new test fail if the behavior it names were removed",
)

TIER_PANEL = {"fast": 1, "standard": 3, "deep": 5}

NOT_A_GATE = (
    "This is an adversarial pre-close check, not a Lane 3 gate. It raises the "
    "floor; it does not authorize closure. Closure remains the operator's "
    "explicit call."
)


def run(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False, cwd=cwd)


def repo_root() -> Path:
    """Anchor state to the repo, never to the caller's cwd.

    hrse#1208 review, reproduced by four independent refuters: a cwd-relative
    receipt directory made the one-pass rule evadable by `cd scripts`, and it
    vanished entirely with the disposable impl worktree the protocol mandates.
    """
    result = run("git", "rev-parse", "--show-toplevel")
    if result.returncode:
        raise SystemExit("preclose-check: not inside a git repository.")
    return Path(result.stdout.strip())


def receipt_dir() -> Path:
    return repo_root() / ".claude" / "cache" / "preclose"


def normalize_repo(value: str) -> str:
    """`owner/name`, however it was spelled.

    Without this, `vitalharmony/HRSE` and a full URL each mint their own
    receipt for the same work, re-arming the one-pass rule for free.
    """
    text = value.strip().removesuffix(".git")
    text = re.sub(r"^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)", "", text)
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        raise SystemExit(f"preclose-check: --repo must be owner/name, got {value!r}")
    return f"{parts[-2].lower()}/{parts[-1].lower()}"


def origin_repo() -> str | None:
    result = run("git", "remote", "get-url", "origin")
    if result.returncode or not result.stdout.strip():
        return None
    try:
        return normalize_repo(result.stdout.strip())
    except SystemExit:
        return None


def changed_files(base: str, head: str) -> list[str]:
    """Committed changes, with rename and quoting defeats closed off.

    `--no-renames` so removing a guard from `tools/hooks/` is still seen as
    touching `tools/hooks/`; `core.quotePath=false` plus `-z` so a non-ASCII
    filename is not returned wrapped in quotes that defeat every `^` anchor.
    """
    result = run("git", "-c", "core.quotePath=false", "diff", "--name-only",
                 "--no-renames", "-z", f"{base}...{head}")
    if result.returncode:
        raise SystemExit(f"preclose-check: cannot diff {base}...{head}: {result.stderr.strip()}")
    return [path for path in result.stdout.split("\0") if path]


def uncommitted_files() -> list[str]:
    """Anything the committed diff cannot see -- staged, dirty, or untracked."""
    result = run("git", "-c", "core.quotePath=false", "status", "--porcelain", "-z")
    if result.returncode:
        return []
    entries = [entry for entry in result.stdout.split("\0") if entry]
    return [entry[3:] for entry in entries if len(entry) > 3]


def blast_radius(files: list[str]) -> list[str]:
    """Reasons this change is high blast radius. Empty means it is not."""
    reasons = []
    for path in files:
        for pattern, why in HIGH_BLAST_PATTERNS:
            if pattern.search(path):
                reasons.append(f"{path}: {why}")
                break
    return reasons


def panel_size(reasons: list[str], tier: str | None) -> tuple[int, str]:
    if reasons:
        return len(LENSES), "high blast radius — full panel regardless of Tier or diff size"
    if tier in TIER_PANEL:
        return TIER_PANEL[tier], f"Tier {tier}"
    # An unset Tier is not an error (population is lazy, per planning.md), but
    # it is also not a reason to skip: default to the middle, because the cheap
    # direction to be wrong is one refuter too many.
    return TIER_PANEL["standard"], "Tier unset — defaulting to standard"


def receipt_path(repo: str, issue: int) -> Path:
    return receipt_dir() / f"{repo.replace('/', '_')}_{issue}.json"


def read_receipt(path: Path) -> dict | None:
    """A receipt that cannot be read is no receipt.

    Deliberately fails toward re-reviewing rather than toward refusing or
    crashing: an unreadable file must never make an issue permanently
    un-checkable, and a corrupt one must never be mistaken for a completed pass.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_one_pass(repo: str, issue: int, head_sha: str, force: bool) -> None:
    """One pass per diff, then escalate -- the rule pitch-inspection carries.

    Keyed on the reviewed SHA, not merely on the issue: a revised diff is new
    work and must be reviewable, or the correct response to a finding (fix it)
    would be gated behind a flag the tool tells the caller not to use.
    """
    if force:
        return
    prior = read_receipt(receipt_path(repo, issue))
    if not prior or prior.get("status") != "complete":
        return
    if prior.get("reviewed_sha") != head_sha:
        return
    raise SystemExit(
        f"preclose-check: a completed pass already covers {repo}#{issue} at {head_sha[:12]}.\n"
        "One pass per diff. If the findings are disputed after one revision, escalate to the "
        "operator rather than re-running -- a second panel on the same diff is Lane 1 arguing "
        "with itself at the operator's cost.\n"
        "Revising the diff re-arms this automatically; --force is for an operator instruction."
    )


def write_receipt(repo: str, issue: int, head_sha: str, size: int, status: str) -> Path:
    """Atomic, so an interrupted write cannot leave a half-file behind."""
    directory = receipt_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = receipt_path(repo, issue)
    payload = {"repo": repo, "issue": issue, "reviewed_sha": head_sha,
               "refuters": size, "status": status}
    handle = tempfile.NamedTemporaryFile("w", dir=directory, delete=False, suffix=".tmp")
    try:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)
    return path


def _require_repo_and_head(repo: str, args: argparse.Namespace) -> str:
    """Shared by plan() and complete() -- harmonic-forge#704 preclose finding:
    complete() originally had neither guard, so a wrong-cwd cross-repo call
    (a receipt minted under the wrong repo's .claude/cache/preclose/, leaving
    the actually-reviewed repo's one-pass rule silently disarmed) or an
    unresolvable --head (a fake sha that no later check could ever match --
    see the returncode check below) both wrote a confident 'status: complete'
    receipt anyway. #704 turns this from a latent single-repo bug into a live
    one by making one script, with --repo required, serve every consuming repo.
    """
    actual = origin_repo()
    # harmonic-forge#704 preclose finding: `if actual and ...` treated "no
    # origin remote at all" (a fork clone using `upstream`, a worktree off a
    # bare/mirror clone, a renamed-remote CI checkout) as "no objection" and
    # skipped the check entirely -- unreachable in practice while --repo
    # defaulted to vitalharmony/hrse and the script lived only inside HRSE2,
    # but AC4's required --repo plus one platform copy invoked from arbitrary
    # repos makes an unrecognized-remote checkout ordinary traffic, not an
    # edge case. Cannot-decide is refused, exactly like a real mismatch --
    # never silently treated as a pass.
    if actual != repo and not args.allow_repo_mismatch:
        reason = (f"this checkout's origin is {actual}" if actual
                   else "this checkout has no `origin` remote to compare against")
        raise SystemExit(
            f"preclose-check: --repo says {repo} but {reason}.\n"
            "The diff would be read from this checkout while the receipt was filed under "
            "another repo -- one issue number, two repos, one receipt. Run from the repo "
            "being reviewed, or pass --allow-repo-mismatch deliberately."
        )
    resolved = run("git", "rev-parse", args.head)
    head_sha = resolved.stdout.strip()
    # `git rev-parse <bad-ref>` echoes the literal argument back to stdout even
    # on failure (exit 128) -- an emptiness check alone never fires, so an
    # unresolvable --head silently passed through as a fake "sha" (harmonic-forge#704
    # preclose finding, second-run/fail-direction lenses).
    if resolved.returncode or not head_sha:
        raise SystemExit(f"preclose-check: cannot resolve --head {args.head!r}")
    return head_sha


def plan(args: argparse.Namespace) -> int:
    repo = normalize_repo(args.repo)
    head_sha = _require_repo_and_head(repo, args)
    check_one_pass(repo, args.issue, head_sha, args.force)

    files = changed_files(args.base, args.head)
    dirty = uncommitted_files()
    dirty_blast = blast_radius(dirty)
    if dirty and not args.allow_dirty:
        # Found by three refuters against this very change: the committed range
        # cannot see a working tree, so a partially-committed change is reviewed
        # with the high-blast half invisible, and a wholly-uncommitted one reads
        # as "nothing to review".
        detail = "\n".join(f"    - {reason}" for reason in dirty_blast) or "    (no high-blast paths among them)"
        raise SystemExit(
            f"preclose-check: {len(dirty)} uncommitted/untracked path(s) -- the refuters review a "
            f"committed diff, so these would be invisible to them and to the panel sizing:\n{detail}\n"
            "Commit the work first. --allow-dirty proceeds anyway, reviewing only what is committed."
        )
    if not files:
        raise SystemExit(f"preclose-check: {args.base}...{args.head} changes nothing -- nothing to review.")

    reasons = blast_radius(files)
    size, why = panel_size(reasons, args.tier)

    print(f"preclose-check plan for {repo}#{args.issue}")
    print(f"  diff:     {args.base}...{args.head} @ {head_sha[:12]} ({len(files)} files)")
    print(f"  refuters: {size} — {why}")
    if reasons:
        print("  blast radius:")
        for reason in reasons:
            print(f"    - {reason}")
    print("  lenses:")
    for lens in list(LENSES)[:size]:
        print(f"    - {lens}")
    print()
    print("Spawn one FRESH-CONTEXT preclose-inspection agent per lens. Never a fork:")
    print("a fork inherits the reasoning that produced the defect. Give each only the")
    print("issue's acceptance criteria, the diff, and the repo.")
    print()
    print("Discard any finding without a file:line-anchored concrete failure scenario.")
    print("Post ALL surviving findings verbatim to the issue, plus every dismissed")
    print("finding with the reason it was dismissed -- that is what makes this")
    print("auditable by the operator instead of another Lane 1 self-report.")
    print()
    print(f"When the panel has actually run, record it:\n"
          f'  python3 "${{HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}}/tools/gh/preclose_check.py" '
          f"--repo {repo} --issue {args.issue} --complete")
    print()
    print(NOT_A_GATE)
    write_receipt(repo, args.issue, head_sha, size, status="planned")
    return 0


def complete(args: argparse.Namespace) -> int:
    """Mark the pass done -- separate from planning, on purpose.

    The receipt originally recorded planning intent, so an interrupted session
    consumed its one pass without a single refuter running, and the retry was
    then refused with a message asserting a review that never happened.
    """
    repo = normalize_repo(args.repo)
    head_sha = _require_repo_and_head(repo, args)
    prior = read_receipt(receipt_path(repo, args.issue))
    size = prior.get("refuters", 0) if prior else 0
    path = write_receipt(repo, args.issue, head_sha, size, status="complete")
    print(f"preclose-check: recorded a completed pass for {repo}#{args.issue} at {head_sha[:12]}")
    print(f"  receipt: {path}")
    print()
    print(NOT_A_GATE)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan an adversarial pre-close check for Tooling Exception work.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", required=True,
                        help="owner/name of the repo under review. Required, never defaulted -- "
                             "a platform script defaulting to one consuming repo is a two-tier "
                             "violation in miniature (harmonic-forge#703 decision 2).")
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--tier", choices=sorted(TIER_PANEL), help="Board Tier; omit to default to standard.")
    parser.add_argument("--complete", action="store_true",
                        help="Record that the panel actually ran. Planning alone does not.")
    parser.add_argument("--force", action="store_true",
                        help="Re-run despite a completed receipt for this same diff. Operator instruction only.")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Plan against the committed diff even with uncommitted changes present.")
    parser.add_argument("--allow-repo-mismatch", action="store_true",
                        help="Permit --repo to differ from this checkout's origin remote.")
    args = parser.parse_args()
    sys.exit(complete(args) if args.complete else plan(args))


if __name__ == "__main__":
    main()
