#!/usr/bin/env python3
"""The te-runner's queue: named issues only, eligibility re-checked (harmonic-forge#1055, G1).

    te_queue.py add --queue FILE F<N>...   # append pending items; an id already queued, or
                                          # one failing the preflight's per-item checks, is refused
    te_queue.py check --queue FILE         # per pending item: eligible, or the first failing reason

The queue file is `tools/lane/queue_state.py`'s format, imported unmodified.
Only issues the operator names are queued: nothing in `tools/runner/` lists or
searches issues.

`check` reports, per pending item, `eligible` or the first failing reason, as
`ineligible` or `deny-path` (the reducer's reasons, harmonic-forge#1051):

1. the `tooling-exception` label;
2. board Tier `fast` or `standard`;
3. a posted Lane 1 handoff;
4. a pitch receipt with verdict `PROCEED` or `PROCEED_WITH_NAMED_CHANGES`
   (a `consumed_by` receipt is expected: the handoff consumed it);
5. the handoff's Delegated Judgment Calls is `none`;
6. a kill-check bundle on disk whose `bundle_sha256` equals the handoff's
   `kill-bundle-sha256: <hex>` line;
7. `deny_check.paths` over the handoff's Affected Files has no hit.

Exit 0 when the check ran (whatever it found), 2 when it could not.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for sub in ("lane", "gh"):
    sys.path.insert(0, str(HERE.parent / sub))
sys.path.insert(0, str(HERE))
import deny_check  # noqa: E402
import fetch_lane1_context  # noqa: E402
import item_list_cache  # noqa: E402
import kill_runner  # noqa: E402
import pitch_receipt  # noqa: E402
import queue_state  # noqa: E402

REPO = "vitalharmony/harmonic-forge"
BOARD_OWNER, BOARD_NUMBER = "vitalharmony", "3"
KIND = "te-issue"
#: Lane 1 posts handoffs as the operator's account (tools/gh/l1_post.py).
LANE1_LOGIN = "vitalharmony"
ELIGIBLE_TIERS = ("fast", "standard")
PROCEED = ("PROCEED", "PROCEED_WITH_NAMED_CHANGES")
DIGEST_RE = re.compile(r"kill-bundle-sha256:\s*`?([0-9a-f]{64})`?")
KEY_RE = re.compile(r"^F([1-9][0-9]*)$")


class QueueError(Exception):
    """The command could not run; the message says why."""


def bundle_root() -> Path:
    return Path(deny_check.manifest()["bundle_root"]).expanduser()


def bundle_dir(issue: int) -> Path:
    """Lane 1's kill-check bundle for one issue, outside any checkout."""
    return bundle_root() / str(issue)


def issue_number(item_id: str) -> int:
    match = KEY_RE.match(item_id)
    if not match:
        raise QueueError(f"not a harmonic-forge issue key: {item_id!r} (expected F<N>)")
    return int(match.group(1))


def load(path: Path) -> dict:
    try:
        return queue_state.load(path)
    except ValueError as exc:
        raise QueueError(str(exc)) from exc


@contextlib.contextmanager
def queue_lock(path: Path):
    """The one writer of a queue file: a run holds this for its whole length,
    and `add` refuses while it is held, so no stale whole-file save drops an
    item or a second run blocks the first one's live item."""
    lock = path.with_name(path.name + ".lock")
    with open(lock, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise QueueError(f"{path} is in use by a running te_run.py; try again after it ends") \
                from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def add(path: Path, keys: list[str], checks=None) -> list[str]:
    """Append each key as a pending item; refuse any already present, and any
    that fails the preflight's per-item checks (harmonic-forge#1112 AC15), so
    an item that would fail them is refused now, not at the next run start.
    Every failure of every key is listed together; nothing is added then."""
    for key in keys:
        issue_number(key)
    checks = item_failures if checks is None else checks
    failures = [line for key in keys for line in checks(issue_number(key))]
    if failures:
        raise QueueError("refused, the per-item checks fail:\n  " + "\n  ".join(failures))
    with queue_lock(path):
        return _add(path, keys)


def add_context() -> tuple[Path, str, dict]:
    """(SOURCE, its `origin/main` SHA, the runner's env) for the add-time checks."""
    import preclose_driver  # noqa: PLC0415 -- imports this module's siblings
    source = HERE.parent.parent
    base = subprocess.run(["git", "-C", str(source), "rev-parse", "--verify", "origin/main"],
                          capture_output=True, text=True)
    if base.returncode:
        raise QueueError(f"cannot resolve origin/main in {source}: {base.stderr.strip()}")
    try:
        env = preclose_driver.runner_env()
    except preclose_driver.DriverError as exc:
        raise QueueError(str(exc)) from exc
    return source, base.stdout.strip(), env


def item_failures(issue: int) -> list[str]:
    """The preflight's own per-item checks (`preflight.item_failures`).
    Imported here, not at module load: preflight imports this module."""
    import tempfile  # noqa: PLC0415
    import preflight  # noqa: PLC0415
    source, base, env = add_context()
    with tempfile.TemporaryDirectory(prefix="te-queue-add-") as scratch:
        return preflight.item_failures(issue, source, base, Path(scratch), env)


def _add(path: Path, keys: list[str]) -> list[str]:
    state = load(path) if path.exists() else {"items": []}
    present = {item["id"]: item["status"] for item in state["items"]}
    clash = [f"{key} ({present[key]})" for key in keys if key in present]
    if clash or len(set(keys)) != len(keys):
        raise QueueError("already queued, never re-queued: " + ", ".join(clash or keys))
    for key in keys:
        state["items"].append({"id": key, "kind": KIND, "status": "pending", "note": ""})
    queue_state.save(path, state)
    return keys


# --- the reads, each a separate seam for tests -------------------------------

def gh_json(*args: str) -> object:
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if result.returncode:
        raise QueueError(f"gh api {' '.join(args)} failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise QueueError(f"gh api {' '.join(args)}: unparseable output") from exc


def labels(issue: int) -> list[str]:
    data = gh_json(f"repos/{REPO}/issues/{issue}")
    return [label.get("name") for label in (data.get("labels") or []) if isinstance(label, dict)]


def tier(issue: int) -> str | None:
    try:
        # The runner's own gh identity, never the operator's (harmonic-forge#1107).
        return item_list_cache.fetch_issue_tier(
            REPO, issue, BOARD_NUMBER, owner=BOARD_OWNER,
            run=lambda args: subprocess.run(args, capture_output=True, text=True,
                                            env=deny_check.runner_env()))
    except Exception as exc:  # noqa: BLE001 -- any failure to read is a failure, never "no tier"
        raise QueueError(f"cannot read the board Tier of F{issue}: {exc}") from exc


def is_handoff(comment: dict) -> bool:
    """A Lane 1 handoff: posted by the Lane 1 account, and the l1-post marker's
    own kind is `handoff`. Body text alone is never trusted: anyone who can
    comment, the runner's own identity included, can write a marker."""
    body = comment.get("body") or ""
    login = (comment.get("user") or {}).get("login")
    marker = fetch_lane1_context._MARKER_RE.search(body)
    kind = fetch_lane1_context._KIND_RE.search(marker.group(0)) if marker else None
    return login == LANE1_LOGIN and bool(kind) and kind.group(1) == "handoff"


def handoff(issue: int) -> str | None:
    """The newest posted Lane 1 handoff's body, or None."""
    try:
        comments = fetch_lane1_context.fetch_comments(REPO, issue)
    except (SystemExit, ValueError) as exc:  # ValueError: an unparseable body
        raise QueueError(f"cannot read the comments of F{issue}") from exc
    handoffs = [c.get("body", "") for c in comments if is_handoff(c)]
    return handoffs[-1] if handoffs else None


def lane1_marker_id(issue: int) -> int | None:
    """The newest Lane 1 handoff or rework comment's id (harmonic-forge#1147),
    by the same login-plus-marker test as `is_handoff`, or None."""
    try:
        comments = fetch_lane1_context.fetch_comments(REPO, issue)
    except (SystemExit, ValueError) as exc:  # ValueError: an unparseable body
        raise QueueError(f"cannot read the comments of F{issue}") from exc
    ids = []
    for comment in comments:
        marker = fetch_lane1_context._MARKER_RE.search(comment.get("body") or "")
        kind = fetch_lane1_context._KIND_RE.search(marker.group(0)) if marker else None
        if (comment.get("user") or {}).get("login") == LANE1_LOGIN and kind \
                and kind.group(1) in ("handoff", "rework"):
            ids.append(comment.get("id"))
    return ids[-1] if ids else None


#: The runner's own checkout (`te_run.SOURCE`): set-aside and `check` read
#: the tree of this one path, so they compare the same checkout by construction.
SOURCE = HERE.parent.parent


def runner_tree(source: Path = SOURCE) -> str:
    """`tools/runner/`'s tree hash at the runner checkout's HEAD, or QueueError."""
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD:tools/runner"],
                            capture_output=True, text=True)
    if result.returncode:
        raise QueueError(f"cannot read the tools/runner tree in {source}: "
                         f"{result.stderr.strip()[:200]}")
    return result.stdout.strip()


def outcome_record(issue: int, run_id: str, reason: str | None) -> dict:
    """What a set-aside depended on (harmonic-forge#1147 AC1). Taken BEFORE
    the item is worked, so a Lane 1 post that lands mid-run re-arms it."""
    try:
        digest = kill_runner.bundle_digest(bundle_dir(issue))
    except kill_runner.RunnerError as exc:  # undecidable, as in unchanged()
        raise QueueError(f"kill bundle of F{issue} unreadable: {exc}") from exc
    return {"run_id": run_id, "reason": reason, "lane1_marker_id": lane1_marker_id(issue),
            "bundle_digest": digest, "runner_tree": runner_tree(), "runner_source": str(SOURCE)}


#: Which change re-arms each verdict reason (AC2); a reason absent here is
#: never gated, because the existing eligibility clauses re-check it.
REARMED_BY = {
    "check-fail": "lane1", "kill-check-fail": "lane1",
    "preclose-survivor": "lane1", "self-flag": "lane1",
    "tool-refusal": "runner", "budget": "runner",
}


def unchanged(item: dict) -> str | None:
    """`unchanged since <run_id>, skipped; ...` when nothing that could change
    the item's last set-aside has changed, else None (harmonic-forge#1147)."""
    last = item.get("last_outcome")
    if item.get("status") != "pending" or not isinstance(last, dict):
        return None
    rule = REARMED_BY.get(last.get("reason") or "")
    if rule is None:
        return None
    issue = issue_number(item["id"])
    if rule == "lane1":
        if lane1_marker_id(issue) != last.get("lane1_marker_id"):
            return None
        try:
            if kill_runner.bundle_digest(bundle_dir(issue)) != last.get("bundle_digest"):
                return None
        except kill_runner.RunnerError as exc:  # undecidable: never worked on a guess
            raise QueueError(f"kill bundle of F{issue} unreadable: {exc}") from exc
        after = "a new Lane 1 handoff or rework, or a changed kill bundle"
    else:
        if runner_tree() != last.get("runner_tree"):
            return None
        after = "a change to tools/runner/"
    return f"unchanged since {last.get('run_id')}, skipped; eligible after {after}"


def receipt(issue: int) -> dict | None:
    return pitch_receipt.read(REPO, issue)


def bundle_pin(issue: int) -> str:
    """The handoff's `kill-bundle-sha256`, or QueueError."""
    body = handoff(issue)
    pinned = DIGEST_RE.search(body or "")
    if not pinned:
        raise QueueError("the handoff pins no kill-bundle-sha256")
    return pinned.group(1)


def snapshot_bundle(issue: int, pin: str, scratch: Path) -> Path:
    """The bundle copied into `scratch` and verified against the handoff's pin.
    The oracle uses only this copy, so nothing done to the bundle on disk
    afterwards reaches it. The preflight runs this same function per item
    (harmonic-forge#1112 AC12); `te_run` runs it before the agent."""
    import shutil  # noqa: PLC0415
    snapshot = scratch / "bundle"
    try:
        shutil.copytree(bundle_dir(issue), snapshot, symlinks=True)
    except OSError as exc:
        raise QueueError(f"cannot snapshot the kill-check bundle: {exc}") from exc
    # A symlink anywhere in the copy (checks.json included) would point the
    # oracle at a file the agent can still write, so none is accepted. The copy
    # is what is scanned: it is exactly what the oracle will use.
    links = [str(p.relative_to(snapshot)) for p in snapshot.rglob("*") if p.is_symlink()]
    if links:
        raise QueueError(f"the kill-check bundle holds a symlink ({', '.join(links[:3])}); refused")
    try:
        digest = kill_runner.bundle_digest(snapshot)
    except kill_runner.RunnerError as exc:
        raise QueueError(f"cannot snapshot the kill-check bundle: {exc}") from exc
    if digest != pin:
        raise QueueError("the kill-check bundle no longer matches the handoff's kill-bundle-sha256")
    return snapshot


# --- handoff parsing -----------------------------------------------------------

def section(body: str, title: str) -> str | None:
    match = re.search(rf"^###\s+{re.escape(title)}\s*$(.*?)(?=^#{{2,3}}\s|\Z)", body,
                      re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else None


def affected_files(body: str) -> list[str]:
    table = section(body, "Affected Files") or ""
    paths = []
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= set("-: ") or cells[0].lower() == "file":
            continue
        paths.append(cells[0].strip("`"))
    return paths


def first_failure(issue: int) -> tuple[str, str] | None:
    """(reason, detail) for the first failing G1 condition, or None."""
    if "tooling-exception" not in labels(issue):
        return "ineligible", "no tooling-exception label"
    board_tier = tier(issue)
    if board_tier not in ELIGIBLE_TIERS:
        return "ineligible", f"board Tier is {board_tier!r}, not fast or standard"
    body = handoff(issue)
    if body is None:
        return "ineligible", "no posted Lane 1 handoff"
    pitch = receipt(issue)
    if not pitch or pitch.get("verdict") not in PROCEED:
        return "ineligible", f"pitch verdict is {(pitch or {}).get('verdict')!r}"
    calls = section(body, "Delegated Judgment Calls")
    if (calls or "").strip().lower() != "none":
        return "ineligible", "Delegated Judgment Calls is not none"
    pinned = DIGEST_RE.search(body)
    if not pinned:
        return "ineligible", "the handoff pins no kill-bundle-sha256"
    try:
        actual = kill_runner.bundle_digest(bundle_dir(issue))
    except kill_runner.RunnerError as exc:
        return "ineligible", f"kill-check bundle unreadable: {exc}"
    if actual != pinned.group(1):
        return "ineligible", "kill-check bundle digest differs from the handoff's"
    paths = affected_files(body)
    if not paths:
        return "ineligible", "the handoff names no Affected Files"
    try:
        hits = deny_check.paths_hits(paths)
    except deny_check.DenyError as exc:
        return "deny-path", str(exc)
    if hits:
        return "deny-path", "Affected Files hit the deny list: " + ", ".join(hits)
    return None


def check(path: Path) -> list[dict]:
    rows = []
    for item in load(path)["items"]:
        if item["status"] != "pending":
            continue
        try:
            skipped = unchanged(item)
        except QueueError as exc:
            rows.append({"id": item["id"], "eligible": False, "reason": "undecidable",
                         "detail": str(exc)})
            continue
        if skipped:
            rows.append({"id": item["id"], "eligible": False, "reason": "unchanged",
                         "detail": skipped})
            continue
        failure = first_failure(issue_number(item["id"]))
        rows.append({"id": item["id"], "eligible": failure is None,
                     **({"reason": failure[0], "detail": failure[1]} if failure else {})})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    added = sub.add_parser("add")
    added.add_argument("--queue", required=True)
    added.add_argument("keys", nargs="+")
    checked = sub.add_parser("check")
    checked.add_argument("--queue", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            print(json.dumps({"added": add(Path(args.queue), args.keys)}))
        else:
            print(json.dumps({"items": check(Path(args.queue))}, indent=1))
        return 0
    except (QueueError, kill_runner.RunnerError, OSError) as exc:
        print(f"te-queue: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
