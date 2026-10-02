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

Receipts are an honesty mechanism against mistakes, not an authentication
boundary: same-account forgery is out of scope (operator ruling 2026-09-27,
F774).
"""

from __future__ import annotations

import argparse
import fcntl
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))

from manifest import (  # noqa: E402
    ManifestError, normalize_repo as normalize_manifest_repo,
    require_onboarded_repo,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preclose_passes  # noqa: E402  (harmonic-forge#834)

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

# harmonic-forge#701. The file:line anchor a finding must carry to survive the
# filter. `path:line` or `path:line-line`, the shape the plan output demands.
_ANCHOR = re.compile(r"\S+:\d+(-\d+)?")

# Prefixes of the labels `cross_family_provenance.py` prints. The label is
# computed there and pasted here, never composed (rules/cross-family-review.md
# R-0359). These only CLASSIFY a pasted label; nothing here writes one.
PROVENANCE_TRIGGERED = ("Red-team provenance: cross-family (",
                        "Red-team provenance: in-family fallback (")
PROVENANCE_NOT_TRIGGERED = "Red-team provenance: in-family only ("

# preclose finding (three refuters): a prefix check on a TYPED label let a
# hand-written "cross-family (" line record a two-family pass that never ran.
# The label is now computed by running the platform classifier on the
# envelope itself; there is no flag to type one.
# Resolved beside this file, not under $HOME (harmonic-forge#713): the
# planner is platform-owned since #704, and its sibling is the classifier.
PROVENANCE_TOOL = Path(os.environ.get(
    "PRECLOSE_PROVENANCE_TOOL",
    Path(__file__).resolve().parents[1] / "lane" / "cross_family_provenance.py"))

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
    """User-level, shared across every checkout of every repo -- the same
    model `BATCH_STATE_PATH` already uses (harmonic-forge#778 AC3).

    Before this, the receipt lived under `repo_root()`, usually a disposable
    Lane 1 impl worktree: a merging session (a different worktree, or the
    same one after the review worktree was removed) could not see it at all,
    so `block_missing_preclose_inspection.py` could only check the
    `preclose-inspected` LABEL, which says a review happened once, never
    which diff it covered. Binding the merge-time check to a specific
    `reviewed_sha` (`check_one_pass`'s own key) needs the receipt to
    outlive the worktree that wrote it.
    """
    return Path.home() / ".claude" / "state" / "preclose"


def require_writable(directory: Path) -> None:
    """Fail before review work when this session cannot record its receipt."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory):
            pass
    except OSError as exc:
        print(f"preclose-check: receipt dir {directory} is not writable from this session; "
              "add it to the lane sandbox (AGENT_LANE_ADD_DIR, harmonic-forge#783)",
              file=sys.stderr)
        raise SystemExit(2) from exc


def legacy_receipt_dir() -> Path:
    """The pre-#778 repo-anchored location. `read_receipt`'s caller checks
    this only when the user-level store has nothing -- one release's fallback
    for a receipt written just before this change, never a second write
    target (see `find_receipt`)."""
    return repo_root() / ".claude" / "cache" / "preclose"


def normalize_repo(value: str) -> str:
    """`owner/name`, however it was spelled.

    Without this, `vitalharmony/HRSE` and a full URL each mint their own
    receipt for the same work, re-arming the one-pass rule for free.
    """
    try:
        return normalize_manifest_repo(value)
    except ManifestError as exc:
        raise SystemExit(f"preclose-check: {exc}") from exc


def registered_repo(value: str) -> str:
    """Resolve through projects.toml's closed, onboarded-only registry."""
    try:
        project = require_onboarded_repo(value)
    except ManifestError as exc:
        raise SystemExit(f"preclose-check: {exc}") from exc
    assert project.repo is not None
    return project.repo


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


def surviving_findings(findings: list) -> list[dict]:
    """The findings that survive the filter the plan output states (AC4).

    A survivor carries a `file:line` anchor AND a concrete failure scenario.
    Counted here, in code, because criterion 1 turns on this number: a panel
    that produced ten unanchored guesses has zero survivors, and must trigger
    exactly as a silent panel does.
    """
    kept = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        # preclose finding: a finding Lane 1 dismissed is not a survivor, however
        # well anchored -- or a pass that dismissed everything never triggers.
        if str(finding.get("dismissed") or "").strip():
            continue
        anchor = str(finding.get("anchor") or "").strip()
        scenario = str(finding.get("scenario") or "").strip()
        if _ANCHOR.fullmatch(anchor) and scenario:
            kept.append(finding)
    return kept


def cross_family_gate(surviving: int, reasons: list[str], requested: bool) -> tuple[bool, str]:
    """Whether this pass takes the cross-family branch (harmonic-forge#701).

    Evaluated AFTER the panel and its filter, never inside `panel_size()`: that
    runs before any refuter exists, and criterion 1 depends on what the panel
    returned.

    **Silence triggers; findings do not.** Counter-intuitive, so stated: a
    unanimous no-defect verdict from one family is indistinguishable from a
    blind spot that family shares with the implementer. When the panel already
    found real defects there is work to do, and a second family only adds
    latency to a decided outcome -- the clean re-run after the fix is where
    criterion 1 fires. Tier and diff size are deliberately not inputs.
    """
    if requested:
        return True, "operator requested"
    if reasons:
        return True, "high blast radius — a deny or permission surface: " + "; ".join(reasons)
    if surviving == 0:
        return True, "zero findings survived the filter — silence is the trigger"
    return False, f"{surviving} finding(s) survived the filter — rework first, not a second family"


def check_provenance(required: bool, label: str) -> None:
    """The pasted provenance label must agree with the gate (AC5/AC6).

    A required branch accepts `cross-family` or the loud `in-family fallback`
    (the call could not run; rules/cross-family-review.md R-0360). A
    not-required one accepts only the not-triggered label. Anything else is a
    same-family pass being recorded as something it was not.
    """
    label = label.strip()
    if required and not label.startswith(PROVENANCE_TRIGGERED):
        raise SystemExit(
            "preclose-check: the cross-family branch is required for this pass, but the provenance "
            f"label is not a cross-family or in-family-fallback label:\n  {label!r}\n"
            "Take the branch per rules/cross-family-review.md and paste the label "
            "cross_family_provenance.py prints for its envelope. If the call could not run, that "
            "label is the in-family fallback one -- never relabel it.")
    if not required and not label.startswith(PROVENANCE_NOT_TRIGGERED):
        raise SystemExit(
            "preclose-check: the cross-family branch did not trigger, so the provenance label must "
            f"be the not-triggered one (cross_family_provenance.py --not-triggered), got:\n  {label!r}")


def compute_provenance(envelope: str | None, not_triggered: bool,
                       own_model: str | None = None) -> str:
    """Run `cross_family_provenance.py` and return the label it prints.

    harmonic-forge#848 AC8: `own_model` is the CALLING session's model, so a
    fallback or not-triggered label names the family that actually did the
    work rather than the tool's `claude-opus-5` default.
    """
    if not_triggered == bool(envelope):
        raise SystemExit("preclose-check: pass exactly one of --envelope <path> or --not-triggered.")
    argv = ["python3", str(PROVENANCE_TOOL), "--envelope", envelope or "/dev/null"]
    if own_model:
        argv += ["--own-model", own_model]
    if not_triggered:
        argv.append("--not-triggered")
    result = run(*argv)
    label = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if result.returncode or not label:
        raise SystemExit(f"preclose-check: {PROVENANCE_TOOL.name} failed ({result.returncode}): "
                         f"{result.stderr.strip() or 'no label printed'}")
    return label


def require_recorded_envelope(path: str) -> None:
    """A required branch may use fallback only with a real failure record."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"preclose-check: required cross-family envelope is unreadable: {exc}")
    if not text:
        raise SystemExit("preclose-check: required cross-family envelope is empty")
    decoder = json.JSONDecoder()
    envelopes = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            raise SystemExit("preclose-check: required cross-family envelope is missing or unparsable")
        if not isinstance(value, dict):
            raise SystemExit("preclose-check: required cross-family envelope is missing or unparsable")
        envelopes.append(value)
    valid = True
    for item in envelopes:
        status = item.get("status")
        if not isinstance(status, str):
            valid = False
            break
        if status == "ok":
            report = item.get("report")
            if not isinstance(report, dict) or not isinstance(report.get("assumptions"), list) or not report["assumptions"]:
                valid = False
                break
            native = item.get("native")
            if (item.get("posture") != "verify" or item.get("exit_code") != 0
                    or item.get("caller_family") == item.get("target_family")
                    or item.get("target_family") != item.get("family")
                    or not ((item.get("family") == "codex" and _codex_verify_trace(native))
                            or (item.get("family") == "claude" and _claude_verify_trace(native, item.get("verify_model"))))):
                valid = False
                break
        elif not isinstance(item.get("exit_code"), int):
            valid = False
            break
    if not envelopes or not valid:
        raise SystemExit("preclose-check: required cross-family envelope is missing or unparsable")


def _codex_verify_trace(native: object) -> bool:
    return (isinstance(native, list)
            and any(record.get("type") == "thread.started" for record in native if isinstance(record, dict))
            and any(isinstance(record, dict) and record.get("type") == "item.completed"
                    and isinstance(record.get("item"), dict)
                    and record["item"].get("type") == "agent_message" for record in native))


def _claude_blocks(event: dict, kind: str) -> list[dict]:
    message = event.get("message")
    blocks = message.get("content") if isinstance(message, dict) else []
    return [block for block in blocks if isinstance(block, dict) and block.get("type") == kind] if isinstance(blocks, list) else []


def _claude_verify_trace(native: object, verify_model: object) -> bool:
    expected_model = "claude-opus-5-5"
    if not isinstance(native, list) or verify_model != expected_model:
        return False
    inits = [event for event in native if isinstance(event, dict)
             and event.get("type") == "system" and event.get("subtype") == "init"]
    if len(inits) != 1:
        return False
    init = inits[0]
    if (not isinstance(init.get("tools"), list) or init["tools"] != ["Glob", "Grep", "Read"]
            or init.get("mcp_servers") != [] or init.get("model") != verify_model):
        return False
    uses: set[str] = set()
    results: set[str] = set()
    for event in native:
        if not isinstance(event, dict):
            continue
        use_blocks = _claude_blocks(event, "tool_use")
        for block in use_blocks:
            if block.get("name") in {"Read", "Grep"} and isinstance(block.get("id"), str):
                uses.add(block["id"])
        result_blocks = _claude_blocks(event, "tool_result")
        for block in result_blocks:
            ident = block.get("tool_use_id")
            if isinstance(ident, str) and not block.get("is_error", False):
                results.add(ident)
    return bool(uses & results) and any(isinstance(event, dict) and event.get("type") == "result"
                                        and event.get("subtype") == "success"
                                        and isinstance(event.get("result"), str) for event in native)


def load_findings(path: str) -> list:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"preclose-check: cannot read --findings {path}: {exc}")
    if not isinstance(data, list):
        raise SystemExit("preclose-check: --findings must be a JSON list of {anchor, scenario, mechanism} objects")
    return data


def require_mechanisms(findings: list) -> None:
    """At completion only, every survivor names the approach that failed."""
    missing = [str(finding.get("anchor") or "<missing anchor>")
               for finding in surviving_findings(findings)
               if not isinstance(finding.get("mechanism"), str)
               or not finding["mechanism"].strip()]
    if missing:
        raise SystemExit("preclose-check: surviving findings need a non-empty mechanism: "
                         + ", ".join(missing))


def gate_decision(args: argparse.Namespace) -> tuple[bool, str, int, list[str]]:
    findings = load_findings(args.findings)
    surviving = len(surviving_findings(findings))
    reasons = blast_radius(changed_files(args.base, args.head))
    required, why = cross_family_gate(surviving, reasons, args.cross_family)
    return required, why, surviving, reasons


def gate(args: argparse.Namespace) -> int:
    """Print whether this pass takes the cross-family branch. Writes nothing
    beyond the receipt-dir writability probe, which leaves no file."""
    require_writable(receipt_dir())
    repo = registered_repo(args.repo)
    required, why, surviving, _ = gate_decision(args)
    print(f"preclose-check cross-family gate: {'REQUIRED' if required else 'not triggered'}")
    print(f"  surviving findings: {surviving}")
    print(f"  reason: {why}")
    print()
    if required:
        print("Take the branch exactly as rules/cross-family-review.md states -- it is the whole")
        print("mechanism, and nothing here restates it. It is part of this ONE pass, not a second")
        print("round. Then paste the label cross_family_provenance.py prints for its envelope.")
        print("The cross-family command MUST use --out <envelope path>; stdout alone is not a receipt.")
    else:
        print("Record the not-triggered label: cross_family_provenance.py --not-triggered")
        print("(rules/cross-family-review.md, Provenance).")
    print()
    print("Then record the pass with the same --findings. The label is computed from the")
    print("envelope, never typed:")
    tail = ("--envelope <envelope path>" if required else "--not-triggered") + \
        " --own-model <your session's model>"
    print(f'  python3 "${{HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}}/tools/gh/preclose_check.py" '
          f"--repo {repo} --issue {args.issue} --complete --findings {args.findings} {tail}"
          + (" --cross-family" if args.cross_family else ""))
    print()
    print(NOT_A_GATE)
    return 0


def _repo_key(repo: str) -> str:
    """Normalize `repo` to the SAME filesystem-key form regardless of
    casing. GitHub repo slugs are case-insensitive, but nothing upstream of
    this function guarantees a caller always passes the manifest's
    canonical casing -- `preclose_check.py --repo` goes through
    `registered_repo()`'s fixed casing, but a merge-time reader can resolve
    `repo` from a raw `--repo` flag or `gh repo view`'s own casing.
    Preclose finding: without this, a case mismatch between the write and
    read paths silently keys two different receipt files for the same
    repo, producing a permanent deny loop -- the receipt exists, but never
    under the name the reader looks for."""
    return repo.strip().lower().replace("/", "_")


def receipt_path(repo: str, issue: int) -> Path:
    return receipt_dir() / f"{_repo_key(repo)}_{issue}.json"


@contextmanager
def receipt_lock(repo: str, issue: int):
    """Serialize an issue's full receipt read-check-write transaction."""
    directory = receipt_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_repo_key(repo)}_{issue}.lock"
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def serialized_receipt(function):
    @functools.wraps(function)
    def locked(args: argparse.Namespace):
        require_writable(receipt_dir())
        with receipt_lock(registered_repo(args.repo), args.issue):
            return function(args)
    return locked


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


def find_receipt(repo: str, issue: int) -> dict | None:
    """The user-level receipt for `repo`#`issue`, or the pre-#778
    repo-anchored one if the user-level store has nothing -- one release's
    fallback, never a second write target: every write still goes only to
    `receipt_dir()` (`write_receipt`), so this fallback drains on its own as
    old receipts age out rather than needing a migration step."""
    receipt = read_receipt(receipt_path(repo, issue))
    if receipt is not None:
        return receipt
    try:
        legacy_path = legacy_receipt_dir() / f"{_repo_key(repo)}_{issue}.json"
    except SystemExit:
        return None
    return read_receipt(legacy_path)


def local_patch_id(base: str, head: str, dots: str = "...") -> str | None:
    """`git patch-id --verbatim` of `base...head` (harmonic-forge#834 AC1), or
    of `base..head` when the caller needs exactly base's descendants (#838).

    The rendering is pinned (context, prefixes, external diff and textconv
    off, renames on) so a local `diff.*` setting cannot make it differ from
    the `gh pr diff` rendering the merge hook hashes."""
    diff = run("git", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "diff",
               "--no-ext-diff", "--no-textconv", "--no-color", "-U3", "-M",
               "--src-prefix=a/", "--dst-prefix=b/", f"{base}{dots}{head}").stdout
    return preclose_passes.patch_id(diff)


def current_branch() -> str | None:
    name = run("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return name if name and name != "HEAD" else None


def check_pass_cap(repo: str, issue: int, head_sha: str, patch: str | None, force: bool,
                   reforge: bool = False) -> None:
    """harmonic-forge#834: at most two passes per issue, and a patch-identical
    head (a rebase) is not a new pass. Replaces the old per-SHA check, which
    re-armed on every head change and so allowed unbounded passes."""
    if force and not reforge:
        receipt = find_receipt(repo, issue)
        reason = preclose_passes.refusal(receipt, head_sha, patch, reforge, force,
                                         repo=repo, issue=issue)
        if reason:
            raise SystemExit(f"preclose-check: {repo}#{issue} at {head_sha[:12]}: {reason}")
        # harmonic-forge#838 AC5: the operator's --force still needs the
        # post-verdict check in the sticky-wicket case.
        reason = preclose_passes.post_verdict_refusal(receipt, head_sha, repo, issue)
        if reason:
            raise SystemExit(f"preclose-check: {repo}#{issue} at {head_sha[:12]}: {reason}")
        return
    reason = preclose_passes.refusal(find_receipt(repo, issue), head_sha, patch, reforge, force,
                                     repo=repo, issue=issue)
    if reason:
        raise SystemExit(f"preclose-check: {repo}#{issue} at {head_sha[:12]}: {reason}")


def kill_receipt_ok(repo: str, issue: int, head_sha: str, patch_id: str | None) -> bool:
    """The independent kill-check receipt must cover this exact review diff."""
    import kill_check  # Lazy: kill_check reuses this module's receipt helpers.
    return kill_check.covering_receipt(repo, issue, head_sha, patch_id)


def write_receipt(repo: str, issue: int, head_sha: str, size: int, status: str,
                  extra: dict | None = None) -> Path:
    """Atomic, so an interrupted write cannot leave a half-file behind.

    `extra` carries the cross-family half (harmonic-forge#701) in the SAME
    receipt, under the same key -- one pass is one receipt, whichever families
    it used (AC3).
    """
    directory = receipt_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = receipt_path(repo, issue)
    # harmonic-forge#826: one receipt per issue is overwritten by each pass,
    # so the previous pass is archived first -- best-effort, because the new
    # receipt is what the merge guard reads and must always be written.
    _archive_previous_receipt(path, repo)
    payload = {"repo": repo, "issue": issue, "reviewed_sha": head_sha,
               "refuters": size, "status": status, **(extra or {})}
    handle = tempfile.NamedTemporaryFile("w", dir=directory, delete=False, suffix=".tmp")
    try:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)
    return path


def _archive_previous_receipt(path: Path, repo: str) -> None:
    if not path.exists():
        return
    try:
        telemetry = str(Path(__file__).resolve().parent.parent / "telemetry")
        if telemetry not in sys.path:
            sys.path.insert(0, telemetry)
        import archive  # noqa: PLC0415
        archive.archive("preclose-receipts",
                        [json.loads(path.read_text(encoding="utf-8"))],
                        origin=archive.origin_for_repo(repo))
    except Exception:
        return


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
    # harmonic-forge#778 preclose finding: a string that merely LOOKS like a
    # full 40-hex SHA is a valid `git rev-parse` argument -- it is echoed back
    # verbatim with exit 0 even when no such object exists in this repo. The
    # check above only rejects an unresolvable ref/short-hash; it never
    # confirmed the resolved SHA is a real, reachable commit. `--complete
    # --head <any 40 hex chars>` would otherwise mint a "complete" receipt for
    # a commit that was never read, let alone reviewed -- a merge authorized
    # with zero refuters and no diff.
    verified = run("git", "cat-file", "-e", f"{head_sha}^{{commit}}")
    if verified.returncode:
        raise SystemExit(
            f"preclose-check: {head_sha!r} does not resolve to a commit object "
            f"in this checkout -- refusing to write a receipt for a SHA that "
            f"was never actually read.")
    return head_sha


@serialized_receipt
def plan(args: argparse.Namespace) -> int:
    # F783 preclose finding: plan is the first step and its output is the
    # instruction to spend a panel, so it must refuse before printing any.
    require_writable(receipt_dir())
    repo = registered_repo(args.repo)
    head_sha = _require_repo_and_head(repo, args)
    check_pass_cap(repo, args.issue, head_sha, local_patch_id(args.base, args.head), args.force,
                   getattr(args, "reforge", False))
    if not args.force and not kill_receipt_ok(
            repo, args.issue, head_sha, local_patch_id(args.base, args.head)):
        raise SystemExit(
            f"preclose-check: no passing kill-check receipt covers {repo}#{args.issue} "
            f"at {head_sha[:12]}. Run: mise run kill-check -- run --repo {repo} "
            f"--issue {args.issue} --checks <file>")

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
    print("When the panel has actually run, write its findings to a JSON list of")
    print("{anchor, scenario, mechanism} objects and evaluate the cross-family gate")
    print("(mechanism is required on survivors at --complete only):")
    print(f'  python3 "${{HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}}/tools/gh/preclose_check.py" '
          f"--repo {repo} --issue {args.issue} --gate --findings <file>")
    print()
    print(NOT_A_GATE)
    # harmonic-forge#834: a planned receipt overwrites the complete one, so
    # it must carry the pass history forward or the count would reset.
    write_receipt(repo, args.issue, head_sha, size, status="planned",
                  extra=preclose_passes.carried(find_receipt(repo, args.issue)))
    return 0


@serialized_receipt
def complete(args: argparse.Namespace) -> int:
    """Mark the pass done -- separate from planning, on purpose.

    The receipt originally recorded planning intent, so an interrupted session
    consumed its one pass without a single refuter running, and the retry was
    then refused with a message asserting a review that never happened.
    """
    require_writable(receipt_dir())
    repo = registered_repo(args.repo)
    head_sha = _require_repo_and_head(repo, args)
    if not args.findings:
        raise SystemExit(
            "preclose-check: --complete needs --findings (the panel's findings, JSON) and one of "
            "--envelope/--not-triggered -- a pass is not complete until the cross-family gate "
            "has been evaluated (harmonic-forge#701).")
    # harmonic-forge#701 preclose finding (two refuters): without this, a
    # second --complete on the same SHA overwrote the receipt and could
    # relabel a required two-family pass as in-family only.
    patch = local_patch_id(args.base, args.head)
    reforge = getattr(args, "reforge", False)
    check_pass_cap(repo, args.issue, head_sha, patch, args.force, reforge)
    findings = load_findings(args.findings)
    require_mechanisms(findings)
    mechanisms = [preclose_passes.normalize_mechanism(finding["mechanism"])
                  for finding in surviving_findings(findings)]
    required, why, surviving, _ = gate_decision(args)
    if required and args.envelope:
        require_recorded_envelope(args.envelope)
    provenance = compute_provenance(args.envelope, args.not_triggered, getattr(args, "own_model", None))
    check_provenance(required, provenance)
    prior = find_receipt(repo, args.issue)
    size = prior.get("refuters", 0) if prior else 0
    path = write_receipt(repo, args.issue, head_sha, size, status="complete", extra={
        **preclose_passes.record(prior, head_sha, patch, surviving, mechanisms,
                                 current_branch(), reforge),
        "cross_family_required": required,
        "cross_family_reason": why,
        "provenance": provenance,
    })
    print(f"preclose-check: recorded pass "
          f"{len(preclose_passes.current(preclose_passes.history(read_receipt(path))))} of at most "
          f"{preclose_passes.MAX_PASSES} for {repo}#{args.issue} at {head_sha[:12]}")
    print(f"  receipt: {path}")
    print(f"  cross-family: {'required' if required else 'not triggered'} — {why}")
    print(f"  {provenance}")
    print(f"  mechanisms: {', '.join(sorted(set(mechanisms))) or '(none)'}")
    route = preclose_passes.cluster_message(read_receipt(path), repo, args.issue)
    if route:
        print(f"  {route}")
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
    parser.add_argument("--gate", action="store_true",
                        help="After the panel: evaluate the cross-family gate (harmonic-forge#701).")
    parser.add_argument("--findings",
                        help="JSON list of findings {anchor, scenario, mechanism}; mechanism is required "
                             "on survivors at --complete only.")
    parser.add_argument("--envelope",
                        help="With --complete: the cross-family call's envelope; its label is computed.")
    parser.add_argument("--not-triggered", action="store_true",
                        help="With --complete: the gate did not trigger; records the computed label.")
    parser.add_argument("--own-model",
                        help="The calling session's model (harmonic-forge#848). Required with "
                             "--complete and --post-verdict.")
    parser.add_argument("--cross-family", action="store_true",
                        help="Operator asked for the cross-family branch (gate criterion 3).")
    parser.add_argument("--force", action="store_true",
                        help="Run despite the per-issue cap (two passes; a patch-identical rebase is not "
                             "a pass) or a pass already covering this diff. Operator instruction only.")
    parser.add_argument("--reforge", action="store_true",
                        help="Operator instruction only, and only with --force: after sticky-wicket "
                             "ruled 'reforge', start a new pass epoch for a changed diff (harmonic-forge#834).")
    parser.add_argument("--main", default="origin/main",
                        help="With --post-verdict: the ref pass 2's patch id was taken against.")
    parser.add_argument("--post-verdict", action="store_true",
                        help="After sticky-wicket's PATCH verdict: record one cross-family refuter's "
                             "read of --base (the pass-2 head)...--head. Not a pass (harmonic-forge#838).")
    parser.add_argument("--cluster-verdict", choices=("PATCH", "REFORGE"),
                        help="Record sticky-wicket's verdict for a pass 1 mechanism cluster; not a pass.")
    parser.add_argument("--comment-url",
                        help="With --cluster-verdict: the sticky-wicket comment URL. --force corrects it.")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Plan against the committed diff even with uncommitted changes present.")
    parser.add_argument("--allow-repo-mismatch", action="store_true",
                        help="Permit --repo to differ from this checkout's origin remote.")
    args = parser.parse_args()
    if (args.complete or args.post_verdict) and not args.own_model:
        parser.error("--complete/--post-verdict need --own-model <the calling session's model>: "
                     "the receipt's label names the family that did the work (harmonic-forge#848)")
    if args.gate:
        if not args.findings:
            parser.error("--gate needs --findings")
        sys.exit(gate(args))
    if args.post_verdict:
        if not (args.findings and args.envelope):
            parser.error("--post-verdict needs --findings and --envelope (a cross-family call)")
        sys.exit(post_verdict(args))
    if args.cluster_verdict:
        if not args.comment_url:
            parser.error("--cluster-verdict needs --comment-url")
        sys.exit(cluster_verdict(args))
    sys.exit(complete(args) if args.complete else plan(args))


@serialized_receipt
def cluster_verdict(args: argparse.Namespace) -> int:
    """Record sticky-wicket's ruling without changing the vouched-for head."""
    require_writable(receipt_dir())
    repo = registered_repo(args.repo)
    prior = find_receipt(repo, args.issue)
    if not prior:
        raise SystemExit("preclose-check: --cluster-verdict needs a prior receipt")
    passes = preclose_passes.current(preclose_passes.history(prior))
    epoch = max((int(p.get("epoch") or 0) for p in passes), default=0)
    cluster = prior.get("mechanism_cluster")
    if not isinstance(cluster, dict) or int(cluster.get("epoch") or 0) != epoch:
        raise SystemExit("preclose-check: no mechanism cluster exists in the current epoch")
    if cluster.get("verdict") and not args.force:
        raise SystemExit("preclose-check: the mechanism cluster already has a verdict; "
                         "the operator may correct it with --force")
    match = re.search(r"(?:issuecomment-|/issues/comments/)(\d+)", args.comment_url)
    if not match:
        raise SystemExit("preclose-check: --comment-url must identify a GitHub issue comment")
    result = run("gh", "api", f"repos/{repo}/issues/comments/{match.group(1)}")
    if result.returncode:
        raise SystemExit("preclose-check: cannot verify --comment-url through the GitHub API")
    try:
        comment = json.loads(result.stdout)
    except ValueError as exc:
        raise SystemExit(f"preclose-check: invalid GitHub comment response: {exc}") from exc
    expected = f"https://api.github.com/repos/{repo}/issues/{args.issue}"
    if comment.get("issue_url") != expected:
        raise SystemExit("preclose-check: --comment-url resolves to a different issue")
    canonical = comment.get("html_url")
    if not isinstance(canonical, str) or not canonical:
        raise SystemExit("preclose-check: verified comment has no html_url")
    updated = {**cluster, "verdict": args.cluster_verdict, "comment_url": canonical}
    path = write_receipt(repo, args.issue, preclose_passes.reviewed_head(prior),
                         prior.get("refuters", 0), status=prior.get("status", "complete"),
                         extra={**preclose_passes.carried(prior), "mechanism_cluster": updated})
    print(f"preclose-check: mechanism-cluster verdict {args.cluster_verdict} recorded; not a pass.")
    print(f"  receipt: {path}")
    return 0


@serialized_receipt
def post_verdict(args: argparse.Namespace) -> int:
    """harmonic-forge#838 AC5: record the one cross-family refuter's read of
    the patch applied after a sticky-wicket PATCH verdict. ``--base`` is the
    pass-2 head, so the diff is just the patch. Never appended to the pass
    history, so it never counts toward the cap."""
    require_writable(receipt_dir())
    repo = registered_repo(args.repo)
    head_sha = _require_repo_and_head(repo, args)
    base_sha = run("git", "rev-parse", "--verify", f"{args.base}^{{commit}}").stdout.strip()
    if not base_sha:
        raise SystemExit(f"preclose-check: --base {args.base!r} does not resolve to a commit")
    prior = find_receipt(repo, args.issue) or {}
    passes = preclose_passes.current(preclose_passes.history(prior))
    if (len(passes) < preclose_passes.MAX_PASSES
            or preclose_passes.cap_message(passes) != preclose_passes.STICKY_WICKET):
        raise SystemExit("preclose-check: --post-verdict applies only after two passes that both "
                         "left surviving findings (the sticky-wicket case).")
    # F838 sticky-wicket PATCH: bind --base by PATCH ID, not by commit id.
    # Lane 1 rebases finished branches, after which the pass-2 commit is
    # unreachable (and absent from a fresh clone), so a commit-id binding made
    # --force permanently unreachable. Pass 2's patch id is already the
    # rebase-stable identity (#834: "a patch-identical rebase is not a pass").
    pass_two_head = preclose_passes.reviewed_head(prior)
    pass_two_patch = preclose_passes.reviewed_patch_id(prior)
    if base_sha != pass_two_head and not (
            pass_two_patch and local_patch_id(args.main, base_sha) == pass_two_patch):
        raise SystemExit(f"preclose-check: --base {base_sha[:12]} is neither the pass-2 head "
                         f"{str(pass_two_head)[:12]} nor patch-identical to it against "
                         f"{args.main}. The post-verdict check reads only the patch since pass 2.")
    # Two-dot: exactly base's descendants. Three-dot would widen to the whole
    # branch whenever a rebase moved the merge base.
    patch = local_patch_id(base_sha, head_sha, dots="..")
    if patch is None:
        raise SystemExit("preclose-check: there is no patch between --base and --head to check.")
    require_recorded_envelope(args.envelope)
    provenance = compute_provenance(args.envelope, False, getattr(args, "own_model", None))
    check_provenance(True, provenance)
    # This check IS the one refuter, so it has no in-family fallback: a
    # cross-family call that did not run means nobody read the patch.
    if not provenance.startswith(PROVENANCE_TRIGGERED[0]):
        raise SystemExit("preclose-check: the post-verdict check needs a cross-family call that ran; "
                         f"got {provenance!r}. Retry the call; a fallback is not a check.")
    surviving = len(surviving_findings(load_findings(args.findings)))
    # The vouched-for head and status stay exactly as pass 2 left them: this
    # check is not a pass, and the operator's --force is what covers the final
    # head (harmonic-forge#838 plan review).
    path = write_receipt(repo, args.issue, preclose_passes.reviewed_head(prior),
                         prior.get("refuters", 0), status=prior.get("status", "complete"),
                         extra=preclose_passes.post_verdict_fields(
                             prior, base_sha, head_sha, patch, provenance, surviving))
    print(f"preclose-check: post-verdict check recorded for {repo}#{args.issue}, "
          f"{base_sha[:12]}..{head_sha[:12]} ({surviving} surviving finding(s)); not a pass.")
    print(f"  receipt: {path}")
    print(f"  {provenance}")
    return 0


if __name__ == "__main__":
    main()
