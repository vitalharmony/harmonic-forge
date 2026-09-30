"""Per-issue preclose pass accounting (harmonic-forge#834).

Operator ruling, 2026-09-30: **at most two preclose passes per issue.** A
rebase, or any head change whose diff is patch-identical to a reviewed one,
is not a new pass. A third pass is never run:

- both passes left surviving findings: the sticky-wicket agent decides patch
  (the operator's ``--force`` covers the final head) or reforge (a new branch,
  and the count restarts);
- anything else goes to the operator.

**Reforge** is explicit, never inferred from a branch name (a rename would
otherwise reset the cap): ``--reforge`` is accepted only when the current
epoch holds two passes that both left survivors, and only from a branch other
than the one those passes reviewed. It starts a new epoch; the old passes stay
in ``pass_history`` as the record.

The count rides in the receipt itself (``pass_history``), carried forward by
every receipt write, planned or complete. It never depends on the telemetry
archive (harmonic-forge#826), which is best-effort and swallows every error.
Shared by ``preclose_check.py`` and ``block_missing_preclose_inspection.py``
so both enforcement points say exactly the same thing (AC4).
"""
from __future__ import annotations

import subprocess

MAX_PASSES = 2

STICKY_WICKET = (
    "Two preclose passes are complete, and both left surviving findings. Do not run a "
    "third. Invoke the sticky-wicket agent on this issue: 'patch' means the operator "
    "--forces the final head; 'reforge' means a new branch, and the pass count restarts."
)
OPERATOR = (
    "Two preclose passes are complete. Do not run a third. Escalate to the operator; "
    "--force is their instruction, not yours."
)


def patch_id(diff_text: str | None) -> str | None:
    """``git patch-id --verbatim`` of a unified diff, or None when it cannot be
    computed. Works outside any repository, so the merge hook can feed it
    ``gh pr diff`` output (verified equal to ``git diff base...head`` on
    hrse PR #2139).

    ``--verbatim``, never ``--stable``: ``--stable`` strips whitespace, so a
    Python or YAML re-indent that changes behavior hashed as "the same diff"
    and merged unreviewed (harmonic-forge#834 preclose, three refuters)."""
    if not diff_text:
        return None
    try:
        result = subprocess.run(["git", "patch-id", "--verbatim"], input=diff_text,
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = result.stdout.split()
    return fields[0] if result.returncode == 0 and fields else None


def history(receipt: dict | None) -> list[dict]:
    """The completed passes on record, oldest first. A legacy complete receipt
    written before #834 carries no history and counts as one pass."""
    if not receipt:
        return []
    carried = receipt.get("pass_history")
    if isinstance(carried, list):
        return [entry for entry in carried if isinstance(entry, dict)]
    if receipt.get("status") == "complete":
        return [{"sha": receipt.get("reviewed_sha"),
                 "patch_id": receipt.get("reviewed_patch_id"),
                 "surviving": int(receipt.get("surviving_findings") or 0)}]
    return []


def current(passes: list[dict]) -> list[dict]:
    """The passes of the latest reforge epoch -- the ones the cap counts."""
    if not passes:
        return []
    epoch = max(int(p.get("epoch") or 0) for p in passes)
    return [p for p in passes if int(p.get("epoch") or 0) == epoch]


def reviewed(receipt: dict | None) -> dict | None:
    """The last completed pass of the current epoch, whatever the receipt's
    own status: an abandoned ``--plan`` for the next pass must not un-review
    the diff the last pass covered (harmonic-forge#834 preclose)."""
    passes = current(history(receipt))
    return passes[-1] if passes else None


def cap_message(passes: list[dict]) -> str:
    """AC2/AC4's case split, read from the last two completed passes."""
    last_two = passes[-MAX_PASSES:]
    if len(last_two) == MAX_PASSES and all(int(p.get("surviving") or 0) > 0 for p in last_two):
        return STICKY_WICKET
    return OPERATOR


def covered(passes: list[dict], sha: str, current_patch_id: str | None) -> bool:
    """True when the last completed pass already reviewed this exact diff:
    the same head, or a patch-identical one (a rebase)."""
    if not passes:
        return False
    last = passes[-1]
    if last.get("sha") == sha:
        return True
    return bool(current_patch_id) and last.get("patch_id") == current_patch_id


def refusal(receipt: dict | None, sha: str, current_patch_id: str | None,
            branch: str | None = None, reforge: bool = False) -> str | None:
    """Why a new pass must not run, or None when it may."""
    passes = current(history(receipt))
    if reforge:
        if len(passes) < MAX_PASSES or cap_message(passes) != STICKY_WICKET:
            return ("--reforge applies only after two passes that both left surviving findings "
                    "and sticky-wicket's reforge verdict.")
        if branch and passes[-1].get("branch") == branch:
            return ("--reforge needs a new branch; this is the branch the two reviewed passes "
                    "were on.")
        return None
    if covered(passes, sha, current_patch_id):
        return ("A completed pass already covers this diff (same head, or patch-identical: "
                "a rebase is not a pass). Nothing new to review. If a finding is disputed, "
                "escalate to the operator rather than re-running.")
    if len(passes) >= MAX_PASSES:
        return cap_message(passes)
    return None


def record(receipt: dict | None, sha: str, current_patch_id: str | None, surviving: int,
           branch: str | None = None, reforge: bool = False) -> dict:
    """The receipt fields for a newly completed pass (AC1)."""
    everything = history(receipt)
    passes = current(everything)
    epoch = max((int(p.get("epoch") or 0) for p in everything), default=0) + (1 if reforge else 0)
    prior_surviving = int(passes[-1].get("surviving") or 0) if passes and not reforge else None
    everything = everything + [{"sha": sha, "patch_id": current_patch_id, "surviving": int(surviving),
                                "branch": branch, "epoch": epoch}]
    return {"pass_history": everything, "pass_count": len(current(everything)),
            "surviving_findings": int(surviving), "prior_surviving_findings": prior_surviving,
            "reviewed_patch_id": current_patch_id}


def carried(receipt: dict | None) -> dict:
    """The fields a non-completing write (``plan``) must preserve."""
    passes = history(receipt)
    return {"pass_history": passes, "pass_count": len(current(passes))} if passes else {}
