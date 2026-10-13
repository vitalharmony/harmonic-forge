#!/usr/bin/env python3
"""The IMC waiver: record it on the issue, and answer whether it exists
(harmonic-forge#895, R-0384).

`IMC` on an operator-typed `BATCH` line waives R-0229's human-reviewed pass and
R-0230's second read for each named Tooling Exception issue. It mints no
authority: the merge grant stays BATCH's. Three files carry it, all under
`IMC_STATE_DIR` (default `~/.claude/state/imc/`):

- `<KEY>.json` -- the HOOK RECORD. Written only by `expand_lane_shorthand.py`
  on `UserPromptSubmit`, when the line passed every check. It expires with
  BATCH's TTL, and only for *starting* a run.
- `<KEY>.waiver.json` -- the RECEIPT. Written by `record` below BEFORE the
  irreversible post (`status: "pending"`, no comment URL), so preclose
  enforcement starts at once; rewritten with the comment URL and
  `status: "recorded"` once the fixed-format `## IMC waiver — <KEY>` comment
  is posted AND refetched. A crash between the two leaves the pending receipt,
  and a retry for the same `granted_at` re-posts and completes it.
  Once it exists, `preclose_check.py` keys every IMC refusal on its presence,
  never on the hook record's or the grant's liveness (keying on liveness
  inverts at expiry: `--complete` would then demand `--not-triggered` for a
  check `--plan` had required). A receipt file that exists but does not parse
  as a JSON object counts as PRESENT (fail closed).

Like BATCH, IMC trusts the operator-typed line and the session. Nothing guards
this directory: every lane runs as the same user, so the receipt and the
public `## IMC waiver` comment are an audit trail, not proof against a session
that can write files.

Run:
    python3 tools/gh/imc_waiver.py record --key F892
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))
sys.path.insert(0, str(HERE.parent / "onboard"))

#: The one place the state directory is named. `None` means
#: `~/.claude/state/imc`, resolved at CALL time through `state_dir()` -- so a
#: test patching this global, or `HOME`, never reaches the live store.
IMC_STATE_DIR: Path | None = None
STATE_RELPATH = Path(".claude") / "state" / "imc"
#: Bumped whenever the hook record's shape changes.
HOOK_VERSION = "1"
#: BATCH's own TTL (`batch_auth.DEFAULT_TTL_HOURS`); restated so this module
#: imports nothing from the hook path just to read a number.
TTL_HOURS = 12.0
RULE_ID = "R-0384"
VITALHARMONY = "vitalharmony/"
_KEY = re.compile(r"^([A-Za-z])(\d+)$")


def state_dir() -> Path:
    return IMC_STATE_DIR if IMC_STATE_DIR is not None else Path.home() / STATE_RELPATH


def hook_record_path(key: str) -> Path:
    return state_dir() / f"{key.upper()}.json"


def receipt_path(key: str) -> Path:
    return state_dir() / f"{key.upper()}.waiver.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def key_target(key: str) -> tuple[str, int] | None:
    """`F892` -> (`vitalharmony/harmonic-forge`, 892), or None if unmapped."""
    match = _KEY.match(key.strip())
    if not match:
        return None
    from batch_auth import REPO_PREFIXES  # noqa: PLC0415
    prefix = match.group(1).upper()
    for repo, repo_prefix in REPO_PREFIXES.items():
        if repo_prefix.upper() == prefix:
            return repo, int(match.group(2))
    return None


def issue_key(repo: str, issue: int) -> str | None:
    from batch_auth import issue_key as _issue_key  # noqa: PLC0415
    return _issue_key(repo, issue)


def hook_record_expiry(record: dict) -> datetime | None:
    granted = _parse_time(record.get("granted_at"))
    return granted + timedelta(hours=TTL_HOURS) if granted else None


def live_hook_record(key: str, now: datetime | None = None) -> dict | None:
    """The hook record, only while it may still START a run."""
    record = read_json(hook_record_path(key))
    if record is None:
        return None
    expiry = hook_record_expiry(record)
    if expiry is None or (now or _now()) >= expiry:
        return None
    return record


#: Returned for a receipt FILE that exists but is not a JSON object, so every
#: IMC refusal stays active (fail closed).
UNREADABLE_RECEIPT = {"unreadable": True}


def waiver_receipt(repo: str, issue: int) -> dict | None:
    """The receipt for this issue, if one exists. Presence is the whole test:
    an expired hook record or BATCH grant never removes a waiver already
    recorded on the issue, and an unparseable receipt file is still present."""
    key = issue_key(repo, issue)
    if key is None:
        return None
    path = receipt_path(key)
    if not path.exists():
        return None
    data = read_json(path)
    return data if data is not None else dict(UNREADABLE_RECEIPT)


def unmatched_hook_record(repo: str, issue: int, now: datetime | None = None) -> dict | None:
    """A live hook record with no receipt carrying the same `granted_at`: an
    IMC run that skipped `record` and so never put the waiver on the issue."""
    key = issue_key(repo, issue)
    if key is None:
        return None
    record = live_hook_record(key, now)
    if record is None:
        return None
    receipt = read_json(receipt_path(key))
    if receipt and receipt.get("granted_at") == record.get("granted_at"):
        return None
    return record


def live_merge_grant(key: str, state_path: Path | None = None,
                     now: datetime | None = None) -> bool:
    """A live BATCH entry for the key with an unspent `gh pr merge` target."""
    import batch_auth  # noqa: PLC0415
    state = batch_auth._load(state_path)
    entry = state.get(key.upper()) if isinstance(state, dict) else None
    if not isinstance(entry, dict) or not batch_auth._entry_live(entry, now or _now()):
        return False
    return any("merge" in str(t.get("action", "")).lower() and not t.get("consumed")
               for t in entry.get("targets", []) if isinstance(t, dict))


def branch_matches(branch: str | None, issue: int) -> bool:
    return bool(branch) and re.match(rf"^(feat|fix)/{issue}-", branch) is not None


def current_branch() -> str | None:
    result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() or None if result.returncode == 0 else None


def heading(key: str) -> str:
    return f"## IMC waiver — {key.upper()}"


def comment_body(key: str, repo: str, issue: int, record: dict, branch: str) -> str:
    return "\n".join([
        heading(key),
        "",
        f"- **Who:** the operator, through a typed prompt that passed the BATCH provenance "
        f"check (R-0117), in session `{record.get('session_id') or 'unknown'}`.",
        f"- **When:** `{record.get('granted_at')}`.",
        f"- **What is waived:** R-0229 (the human-reviewed pass) and R-0230 (the second read), "
        f"for {repo}#{issue} only ({RULE_ID}).",
        "- **What replaces them:** the kill-check receipt, the preclose passes, and a forced "
        "cross-family check.",
        "- **What is not waived:** everything else.",
        f"- **Branch:** `{branch}`.",
        "",
        f"<!-- imc-waiver key={key.upper()} granted_at={record.get('granted_at')} -->",
    ])


def post_and_confirm(repo: str, issue: int, body: str, runner=subprocess.run) -> str:
    """POST the comment over REST, then refetch that exact comment. Returns its
    URL; raises SystemExit if it did not land as written."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        body_file = handle.name
    try:
        posted = runner(["gh", "api", "--method", "POST", f"repos/{repo}/issues/{issue}/comments",
                         "-F", f"body=@{body_file}", "--jq", ".html_url"],
                        capture_output=True, text=True, check=False)
    finally:
        Path(body_file).unlink(missing_ok=True)
    url = (posted.stdout or "").strip()
    if posted.returncode != 0 or "#issuecomment-" not in url:
        raise SystemExit(f"imc-waiver: posting the waiver comment failed: "
                         f"{(posted.stderr or '').strip() or url or 'no URL returned'}")
    comment_id = url.rsplit("#issuecomment-", 1)[1]
    fetched = runner(["gh", "api", f"repos/{repo}/issues/comments/{comment_id}", "--jq", ".body"],
                     capture_output=True, text=True, check=False)
    if fetched.returncode != 0 or (fetched.stdout or "").rstrip("\n") != body.rstrip("\n"):
        raise SystemExit(f"imc-waiver: the refetch did not find the waiver comment as written "
                         f"({url}). The receipt stays pending; check the issue thread and retry.")
    return url


def record(key: str, *, branch: str | None = None, now: datetime | None = None,
           batch_state_path: Path | None = None, runner=subprocess.run) -> dict:
    """AC5: refuse unless every precondition holds; then write a PENDING
    receipt (preclose enforcement starts here), post and refetch, and rewrite
    the receipt as recorded. A pending receipt from a crashed run for the same
    `granted_at` is simply overwritten by the retry. Returns the receipt."""
    key = key.strip().upper()
    lane = os.environ.get("LANE", "")
    if lane != "1":
        # The hook records only in LANE=1; the waiver is posted only there too
        # (harmonic-forge#895 preclose pass 1).
        raise SystemExit(f"imc-waiver: LANE is {lane!r}; an IMC waiver is recorded only in a "
                         "LANE=1 session.")
    target = key_target(key)
    if target is None:
        raise SystemExit(f"imc-waiver: {key} is not a repo-prefixed issue key.")
    repo, issue = target
    if not repo.lower().startswith(VITALHARMONY):
        raise SystemExit(f"imc-waiver: {key} is on {repo}, not a vitalharmony repo; IMC never "
                         "crosses an account boundary.")
    moment = now or _now()
    record_data = read_json(hook_record_path(key))
    if record_data is None:
        raise SystemExit(f"imc-waiver: no IMC hook record for {key}. Only an operator-typed "
                         f"`BATCH ... IMC {key}` line creates one.")
    if live_hook_record(key, moment) is None:
        raise SystemExit(f"imc-waiver: the IMC hook record for {key} has expired (granted "
                         f"{record_data.get('granted_at')}, {TTL_HOURS:g}h TTL). The operator "
                         "types a new IMC line to start a run.")
    if not live_merge_grant(key, batch_state_path, moment):
        raise SystemExit(f"imc-waiver: no live BATCH merge grant covers {key}. IMC mints no "
                         "authority of its own.")
    branch = branch if branch is not None else current_branch()
    if not branch_matches(branch, issue):
        raise SystemExit(f"imc-waiver: branch {branch!r} does not match ^(feat|fix)/{issue}- . "
                         "The merge hook resolves only that shape, and fails open on any other.")
    pending = {"key": key, "repo": repo, "issue": issue,
               "granted_at": record_data.get("granted_at"), "comment_url": None,
               "branch": branch, "recorded_at": _iso(moment), "rule": RULE_ID,
               "status": "pending"}
    # Before the irreversible post: a crash after this line still leaves the
    # receipt, so preclose's refusals hold (harmonic-forge#895 reforge).
    write_json_atomic(receipt_path(key), pending)
    url = post_and_confirm(repo, issue, comment_body(key, repo, issue, record_data, branch), runner)
    receipt = {**pending, "comment_url": url, "status": "recorded"}
    write_json_atomic(receipt_path(key), receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Record an IMC waiver on its issue (R-0384).")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="post the waiver comment and write the receipt")
    rec.add_argument("--key", required=True, help="the IMC key, e.g. F892")
    args = parser.parse_args()
    target = key_target(args.key)
    if target is None:
        raise SystemExit(f"imc-waiver: {args.key} is not a repo-prefixed issue key.")
    from manifest_identity import apply_project_identity  # noqa: PLC0415
    apply_project_identity(target[0])  # harmonic-forge#804
    receipt = record(args.key)
    print(f"imc-waiver: recorded {receipt['key']} -- {receipt['comment_url']}")
    print(f"  receipt: {receipt_path(receipt['key'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
