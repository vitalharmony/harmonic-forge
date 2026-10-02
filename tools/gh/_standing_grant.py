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

**Threat model (operator decision, harmonic-forge#858, 2026-10-02): this guard
is a mistake-detector, not an authorization boundary.** Every lane posts as the
same GitHub account, so nothing here can establish who wrote a comment: a Lane 1
session that writes the right sentences, or posts its own gate report, can take
production write authority. What the guard catches is the honest mistake -- an
AE posted for a SHA no Tier R gate passed, a FAIL or a Tier W gate cited as the
evidence, a later commit that changed the gated or applied files, an edited
gate report. Provenance findings (forged author stamps, self-posted gate
reports, a rule named without its ID) are out of this model by that decision.
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
#: A claim is a citation of R-0374 on the AE's own **Authorized:** line, never a
#: mention elsewhere (an operator AE that discusses the rule is not a grant AE).
_CITES = re.compile(r"(?im)^[^\S\n]*\**[^\S\n]*Authorized\**:?\**[^\n]*\bR-0374\b")
_COMMENT_LINK = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)#issuecomment-(\d+)", re.I)
_GATED_SHA = re.compile(r"gated SHA\W{0,3}([0-9a-f]{7,40})\b", re.I)
#: The AE names the files its production step executes ("Apply path: a.py, b.py");
#: they join the tree-identity set, so a later commit cannot change them unseen.
_APPLY_PATH = re.compile(r"(?im)^[^\S\n]*\**[^\S\n]*Apply path\**:?\**[^\S\n]*(.+)$")
_BODY_SHA_MARKER = re.compile(r"<!--\s*l1-post\s+v1;.*?\bbody-sha256=[0-9a-f]{64}\b", re.I)

GitRunner = Callable[..., subprocess.CompletedProcess]


def _unquoted(body: str) -> str:
    return _FENCE.sub("", body or "")


_AUTHORIZED_LINE = re.compile(r"(?im)^[^\S\n]*\**[^\S\n]*Authorized\**:?\**[^\n]*$")


def _authorized_line(body: str) -> str:
    """The AE's **Authorized:** line(s): the gate link and the gated SHA are
    read only here, so a correct value elsewhere cannot mask a wrong one on
    the line that states the authority (post-verdict check)."""
    return "\n".join(m.group(0) for m in _AUTHORIZED_LINE.finditer(_unquoted(body)))


def cites_grant(body: str) -> bool:
    """Whether an AE body claims R-0374 outside any fenced block."""
    return bool(_CITES.search(_unquoted(body)))


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _gate_result(body: str, repo: str, issue: int, comments: list[dict]) -> dict | None:
    """The gate-result comment on this issue that the AE links to, or None."""
    by_id = {int(c["id"]): c for c in comments}
    for match in _COMMENT_LINK.finditer(_authorized_line(body)):
        if match.group(1).lower() != repo.lower() or int(match.group(2)) != issue:
            continue
        comment = by_id.get(int(match.group(3)))
        if comment is None:
            continue
        body_text = comment.get("body", "")
        kind = clr.FOOTER_KIND.search(body_text)
        # A gate report is recognized by its footer or by its heading, as
        # gate_ci does: most real reports carry no kind=gate-result footer.
        if (kind and kind.group(1).lower() == "gate-result") or gate_ci.looks_like_a_gate_report(body_text):
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


def _apply_files(body: str) -> list[str]:
    files: list[str] = []
    for match in _APPLY_PATH.finditer(_unquoted(body)):
        files += [f.strip(" `*") for f in re.split(r"[,\s]+", match.group(1)) if f.strip(" `*")]
    return files


def _tree_identical(gated: str, sha: str, git: GitRunner, cwd: Path | None,
                    extra: list[str] | None = None) -> str | None:
    """None when `sha` carries the gated change unchanged: every file the gated
    change touched (relative to its merge base with origin/main) is identical
    at `sha`. Otherwise the reason it cannot be shown."""
    base = git("merge-base", "origin/main", gated, cwd=cwd)
    if base.returncode or not base.stdout.strip():
        return f"cannot resolve the gated commit {gated[:12]} locally (fetch it first)"
    files = git("diff", "--name-only", base.stdout.strip(), gated, cwd=cwd)
    if files.returncode:
        return f"cannot list the files the gated change {gated[:12]} touched"
    names = sorted({line for line in files.stdout.splitlines() if line.strip()} | set(extra or []))
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
    if len(_AUTHORIZED_LINE.findall(_unquoted(body))) != 1:
        return (f"{prefix} it has more than one Authorized: line; a grant AE states its authority, "
                "gate link and gated SHA on exactly one")
    gate = _gate_result(body, repo, issue, comments)
    if gate is None:
        return (f"{prefix} its Authorized: line links no gate-result comment on {repo}#{issue}; "
                "R-0374 requires that line to link the passing Tier R gate report. Otherwise the "
                "operator's AE is required.")
    if not _BODY_SHA_MARKER.search(gate.get("body", "")):
        return (f"{prefix} the linked gate report ({gate.get('html_url')}) carries no body-sha256 "
                "marker, so it cannot be shown unedited")
    if not clr.verify_body_sha256(gate):
        return f"{prefix} the linked gate-result ({gate.get('html_url')}) was edited after it was posted"
    verdict = gate_ci.verdict_of(gate.get("body", ""))
    if verdict != "PASS":
        return f"{prefix} the linked gate-result's verdict is {verdict or 'unreadable'}, not PASS"
    tier = _gate_tier(gate, comments)
    if tier != "R":
        return f"{prefix} the linked gate ran at write tier {tier or 'unstated'}, not R"
    gated = gate_ci.gated_sha(gate.get("body", ""))
    if gated is None:
        return f"{prefix} the linked gate-result names no Head-SHA"
    named = _GATED_SHA.search(_authorized_line(body))
    if named is None or not clr.same_sha(named.group(1), gated):
        return (f"{prefix} it does not name the gated SHA {gated[:12]} "
                "(\"gated SHA <sha>\" on the Authorized: line)")
    if not clr.same_sha(gated, sha):
        why = _tree_identical(gated, sha, git, cwd, _apply_files(body))
        if why:
            return f"{prefix} {why}"
    return None
