"""The code guard behind R-0374, the operator's standing production AE grant
(harmonic-forge#858).

R-0374 lets Lane 1 post the AE for a production write that a passing Tier R
gate verified. A rule alone does not bind: the tool that decides authorization
(`check_lane3_ready.resolve_gate_authority`) reads only footer kind and SHA, so
this module is where the rule's preconditions are checked mechanically.

- `cites_grant(body)` says whether an AE claims the grant. A citation inside a
  fenced block is quoted evidence, not a claim, and does not count.
- `grant_refusal(...)` is why such an AE must not be posted, or None. It fails
  closed: anything it cannot show from the thread refuses, and the operator's
  own AE remains the path.
- `check_lane3_ready.carry_forward` refuses to carry a grant AE to a new SHA
  (R-0374: a new SHA needs a fresh Tier R PASS).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

import check_lane3_ready as clr
import gate_ci
from _sweep_tier import parse_write_tier

GRANT_RULE = "R-0374"
_FENCE = re.compile(r"```.*?```", re.S)
_CITES = re.compile(r"\bR-0374\b")
_COMMENT_LINK = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)#issuecomment-(\d+)", re.I)
_GATED_SHA = re.compile(r"gated SHA\W{0,3}([0-9a-f]{7,40})\b", re.I)
_HEAD_SHA = re.compile(r"(?im)^\s*Head-SHA:\s*`?([0-9a-f]{7,40})\b")

GitRunner = Callable[..., subprocess.CompletedProcess]


def _unquoted(body: str) -> str:
    return _FENCE.sub("", body or "")


def cites_grant(body: str) -> bool:
    """Whether an AE body claims R-0374 outside any fenced block."""
    return bool(_CITES.search(_unquoted(body)))


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _gate_result(body: str, repo: str, issue: int, comments: list[dict]) -> dict | None:
    """The gate-result comment on this issue that the AE links to, or None."""
    by_id = {int(c["id"]): c for c in comments}
    for match in _COMMENT_LINK.finditer(_unquoted(body)):
        if match.group(1).lower() != repo.lower() or int(match.group(2)) != issue:
            continue
        comment = by_id.get(int(match.group(3)))
        if comment is None:
            continue
        kind = clr.FOOTER_KIND.search(comment.get("body", ""))
        if kind and kind.group(1).lower() == "gate-result":
            return comment
    return None


def _gate_tier(gate: dict, comments: list[dict]) -> str | None:
    """The write tier the gate ran at: its own report's ceiling, else the
    newest sweep posted before it. None when neither states one."""
    tier = parse_write_tier(gate.get("body", ""))
    if tier:
        return tier
    sweeps = [c for c in comments if int(c["id"]) < int(gate["id"])
              and (k := clr.FOOTER_KIND.search(c.get("body", "")))
              and k.group(1).lower() == "sweep"]
    if not sweeps:
        return None
    return parse_write_tier(max(sweeps, key=lambda c: int(c["id"])).get("body", ""))


def _tree_identical(gated: str, sha: str, git: GitRunner, cwd: Path | None) -> str | None:
    """None when `sha` carries the gated change unchanged: every file the gated
    change touched (relative to its merge base with origin/main) is identical
    at `sha`. Otherwise the reason it cannot be shown."""
    base = git("merge-base", "origin/main", gated, cwd=cwd)
    if base.returncode or not base.stdout.strip():
        return f"cannot resolve the gated commit {gated[:12]} locally (fetch it first)"
    files = git("diff", "--name-only", base.stdout.strip(), gated, cwd=cwd)
    if files.returncode:
        return f"cannot list the files the gated change {gated[:12]} touched"
    names = [line for line in files.stdout.splitlines() if line.strip()]
    if not names:
        return f"the gated commit {gated[:12]} touches no files relative to origin/main"
    diff = git("diff", "--quiet", gated, sha, "--", *names, cwd=cwd)
    if diff.returncode == 0:
        return None
    if diff.returncode == 1:
        return (f"{sha[:12]} differs from the gated commit {gated[:12]} in the files "
                "the gated change touched")
    return f"cannot compare {sha[:12]} with the gated commit {gated[:12]}"


def grant_refusal(body: str, repo: str, issue: int, sha: str, comments: list[dict], *,
                  git: GitRunner = _git, cwd: Path | None = None) -> str | None:
    """Why an AE claiming R-0374 must not be posted at `sha`, or None."""
    prefix = f"this AE cites {GRANT_RULE}, but"
    gate = _gate_result(body, repo, issue, comments)
    if gate is None:
        return (f"{prefix} links no gate-result comment on {repo}#{issue}; R-0374 requires the "
                "AE to link the passing Tier R gate-result. Otherwise the operator's AE is required.")
    if not clr.verify_body_sha256(gate):
        return f"{prefix} the linked gate-result ({gate.get('html_url')}) was edited after it was posted"
    verdict = gate_ci.verdict_of(gate.get("body", ""))
    if verdict != "PASS":
        return f"{prefix} the linked gate-result's verdict is {verdict or 'unreadable'}, not PASS"
    tier = _gate_tier(gate, comments)
    if tier != "R":
        return f"{prefix} the linked gate ran at write tier {tier or 'unstated'}, not R"
    head = _HEAD_SHA.search(gate.get("body", ""))
    if head is None:
        return f"{prefix} the linked gate-result names no Head-SHA"
    gated = head.group(1)
    named = _GATED_SHA.search(_unquoted(body))
    if named is None or not clr.same_sha(named.group(1), gated):
        return (f"{prefix} it does not name the gated SHA {gated[:12]} "
                "(\"gated SHA <sha>\" in the Authorized: line)")
    if not clr.same_sha(gated, sha):
        why = _tree_identical(gated, sha, git, cwd)
        if why:
            return f"{prefix} {why}"
    return None
