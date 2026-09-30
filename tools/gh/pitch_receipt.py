#!/usr/bin/env python3
"""Pitch-inspection receipts for Tooling Exception handoffs (harmonic-forge#838).

R-0238 trigger 4: every handoff on an issue labeled ``tooling-exception`` gets
``pitch-inspection`` before it is posted. ``l1_post.py --kind handoff`` reads the
labels live at post time and refuses such a handoff unless this module holds a
receipt for the issue.

The receipt is bound to the ISSUE, never to the handoff body: R-0239 allows one
revision after the verdict, and a body-bound receipt would demand a second
review of that revision, which is the loop R-0239 forbids. It covers ONE
posted handoff: ``l1_post`` consumes it on a successful post, so a later,
redesigned handoff on the same issue needs a fresh review. A ``REFORGE``
verdict refuses posting until a new verdict is recorded, which is how the
operator's ruling on it lands.

``WAIVED`` is the one escape hatch, and **only the operator authorizes it**: for
a Lane 1 session that cannot run the Claude Code agent (a Codex Lane 1 on
hrse, say). It requires ``--reason`` quoting the operator's instruction, and
the reason is stored in the receipt, so a waiver is always attributable.

Honest limit, the same one the preclose receipt has: Lane 1 records this
itself after the agent returns. It proves the step was not skipped. It does
not prove the review was any good.

    pitch_receipt.py record --repo OWNER/REPO --issue N --verdict PROCEED [--model M]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import tempfile
from pathlib import Path

VERDICTS = ("PROCEED", "PROCEED_WITH_NAMED_CHANGES", "REFORGE", "WAIVED")
LABEL = "tooling-exception"


def receipt_dir() -> Path:
    """User-level, like the preclose receipts: it must outlive the worktree
    that drafted the handoff."""
    return Path.home() / ".claude" / "state" / "pitch"


def receipt_path(repo: str, issue: int) -> Path:
    return receipt_dir() / f"{repo.replace('/', '_')}_{issue}.json"


def read(repo: str, issue: int) -> dict | None:
    """A receipt that cannot be read is no receipt, so posting fails closed."""
    try:
        data = json.loads(receipt_path(repo, issue).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record(repo: str, issue: int, verdict: str, model: str | None = None,
           reason: str | None = None) -> Path:
    if verdict not in VERDICTS:
        raise SystemExit(f"pitch-receipt: verdict must be one of {', '.join(VERDICTS)}")
    if verdict == "WAIVED" and not (reason or "").strip():
        raise SystemExit("pitch-receipt: WAIVED is the operator's instruction only, and needs "
                         "--reason quoting it")
    directory = receipt_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"repo": repo, "issue": issue, "verdict": verdict, "model": model, "reason": reason,
               "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    handle = tempfile.NamedTemporaryFile("w", dir=directory, delete=False, suffix=".tmp")
    try:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    path = receipt_path(repo, issue)
    os.replace(handle.name, path)
    return path


def consume(repo: str, issue: int, posted_url: str | None = None) -> None:
    """Mark the verdict used by the handoff it reviewed. The post has already
    happened, so a failure here cannot un-post it -- but it must not be silent:
    an unconsumed receipt would let the NEXT handoff on this issue post on this
    review (F838 sticky-wicket PATCH). Raise, naming the post and the fix."""
    receipt = read(repo, issue)
    if not receipt or receipt.get("consumed_by"):
        return
    receipt["consumed_by"] = posted_url or "posted"
    try:
        handle = tempfile.NamedTemporaryFile("w", dir=receipt_dir(), delete=False, suffix=".tmp")
        with handle:
            json.dump(receipt, handle, indent=2)
        os.replace(handle.name, receipt_path(repo, issue))
    except OSError as exc:
        raise SystemExit(
            f"pitch-receipt: the handoff posted ({posted_url or 'posted'}), but its verdict could "
            f"not be marked used: {exc}. Delete {receipt_path(repo, issue)} by hand so the next "
            "handoff on this issue needs a fresh review.") from exc


def _usable(receipt: dict | None, repo: str, issue: int) -> dict | None:
    """A receipt that names another issue, or was already used by a posted
    handoff, is no receipt (F838 preclose pass 1)."""
    if not receipt or receipt.get("consumed_by"):
        return None
    if receipt.get("repo") != repo or receipt.get("issue") != issue:
        return None
    return receipt


def refusal(repo: str, issue: int, labels: set[str]) -> str | None:
    """Why a handoff on this issue must not post yet, or None."""
    if LABEL not in labels:
        return None
    receipt = _usable(read(repo, issue), repo, issue)
    command = (f"python3 ~/harmonic-forge/tools/gh/pitch_receipt.py record --repo {repo} "
               f"--issue {issue} --verdict <VERDICT>")
    if receipt is None or receipt.get("verdict") not in VERDICTS:
        return (f"{repo}#{issue} is labeled {LABEL}, so R-0238 trigger 4 requires pitch-inspection "
                f"before this handoff posts, and no verdict is recorded. Run the agent, then:\n  {command}")
    if receipt["verdict"] == "REFORGE":
        return (f"pitch-inspection ruled REFORGE on {repo}#{issue}. Rework the design; a "
                f"disputed verdict goes to the operator (R-0239), and the ruling is recorded with:\n"
                f"  {command}")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="Record the pitch-inspection verdict for an issue.")
    rec.add_argument("--repo", required=True)
    rec.add_argument("--issue", type=int, required=True)
    rec.add_argument("--verdict", required=True, choices=VERDICTS)
    rec.add_argument("--model", help="The reviewing model, so the pass report can compare tiers.")
    rec.add_argument("--reason", help="Required with WAIVED: the operator's instruction, quoted.")
    args = parser.parse_args()
    path = record(args.repo, args.issue, args.verdict, args.model, args.reason)
    print(f"pitch-receipt: {args.verdict} recorded for {args.repo}#{args.issue}\n  {path}")


if __name__ == "__main__":
    main()
