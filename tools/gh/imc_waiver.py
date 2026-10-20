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

`end` (harmonic-forge#1042) is the IMC stop's exit: at a sticky-wicket stop or
a ruled mechanism cluster it archives the receipt (never deletes it), revokes
the key's BATCH merge grant, archives the hook record, and posts and refetches
`## IMC ended — <KEY>`. The issue is then a normal Tooling Exception owing
R-0229's human pass.

Run:
    python3 tools/gh/imc_waiver.py record --key F892
    python3 tools/gh/imc_waiver.py end --key F892 --reason "sticky-wicket PATCH"
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
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


def waiver_satisfied(receipt: dict | None) -> bool:
    """The one test of "the waiver is on the issue": a receipt that exists and
    says `recorded`. A pending or unreadable receipt is not (harmonic-forge#895
    sticky-wicket: every caller asks this one question)."""
    return bool(receipt) and receipt.get("status", "recorded") == "recorded" \
        and not receipt.get("unreadable")


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
    # Only a RECORDED receipt says the waiver is on the issue; a pending one
    # means the post never confirmed (harmonic-forge#895 reforged pass 1).
    if waiver_satisfied(receipt) and receipt.get("granted_at") == record.get("granted_at"):
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


def archive_dir() -> Path:
    return state_dir() / "archive"


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def end_heading(key: str) -> str:
    return f"## IMC ended — {key.upper()}"


def end_marker(key: str, ended_at: str) -> str:
    return f"<!-- imc-ended key={key.upper()} ended_at={ended_at} -->"


def end_comment_body(key: str, repo: str, issue: int, receipt: dict) -> str:
    return "\n".join([
        end_heading(key),
        "",
        f"- **Reason:** {receipt.get('end_reason')}",
        f"- **Original waiver comment:** {receipt.get('comment_url') or 'none recorded'}",
        f"- **What changes:** the IMC waiver on {repo}#{issue} is ended; the key's BATCH "
        "merge grant is revoked and the IMC hook record archived.",
        f"- **What the issue now owes:** R-0229's human pass, as any Tooling Exception "
        f"issue ({RULE_ID}).",
        "",
        end_marker(key, str(receipt.get("ended_at"))),
    ])


def _under_waiver(entry: dict, granted: datetime | None) -> bool:
    """A pass that completed after the waiver was granted and ran the forced
    cross-family call. Passes from before the waiver never count (pass 1)."""
    done = _parse_time(entry.get("completed_at"))
    return (granted is not None and done is not None and done >= granted
            and bool(entry.get("cross_family_ran")))


def imc_stop(repo: str, issue: int, granted_at: str | None = None) -> str | None:
    """AC2: the reason this issue is at an IMC stop, or None. Read from the
    preclose receipt through preclose_check's own reader and preclose_passes'
    own sticky-wicket and cluster logic (the test `post_verdict` applies).
    Only passes run under this waiver (`granted_at`) count, so `end` cannot
    dodge the forced cross-family call with passes recorded before it."""
    import preclose_check  # noqa: PLC0415  (preclose_check imports this module)
    import preclose_passes  # noqa: PLC0415
    granted = _parse_time(granted_at)
    receipt = preclose_check.find_receipt(repo, issue)
    passes = preclose_passes.current(preclose_passes.history(receipt))
    waived = [p for p in passes if _under_waiver(p, granted)]
    if (len(waived) >= preclose_passes.MAX_PASSES
            and preclose_passes.cap_message(waived) == preclose_passes.STICKY_WICKET):
        return "sticky-wicket stop (two passes both left surviving findings)"
    epoch = max((int(p.get("epoch") or 0) for p in preclose_passes.history(receipt)), default=0)
    cluster = preclose_passes._mechanism_cluster(receipt, epoch)
    if cluster and cluster.get("verdict") in ("PATCH", "REFORGE") and waived:
        return f"mechanism-cluster verdict {cluster.get('verdict')} (current epoch)"
    return None


def _revoke_authority(key: str, moment: datetime, batch_state_path: Path | None) -> None:
    """AC3: stand down the BATCH merge grant and archive the hook record, so
    neither a lane merge nor a `record` re-run survives `end`. Idempotent."""
    import batch_auth  # noqa: PLC0415
    batch_auth.revoke([key], state_path=batch_state_path)
    hook = hook_record_path(key)
    if hook.exists():
        archive_dir().mkdir(parents=True, exist_ok=True)
        os.replace(hook, archive_dir() / f"{key}.hook.{_stamp(moment)}.json")


def _pending_end(key: str) -> Path | None:
    """The newest archived receipt still marked `ending`: an `end` that
    crashed under the pre-journal layout. Completed by a retry."""
    candidates = sorted(archive_dir().glob(f"{key}.waiver.*.json"))
    for path in reversed(candidates):
        data = read_json(path)
        if data is not None and data.get("status") == "ending":
            return path
    return None


@contextlib.contextmanager
def _end_lock(key: str):
    """One `end` per key at a time (pass 2, second-run): overlapping runs
    would each post. Blocks until the holder finishes."""
    state_dir().mkdir(parents=True, exist_ok=True)
    with open(state_dir() / f"{key}.end.lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _gh_login(runner) -> str | None:
    found = runner(["gh", "api", "user", "--jq", ".login"],
                   capture_output=True, text=True, check=False)
    return (found.stdout or "").strip() or None if found.returncode == 0 else None


def _existing_end_comment(repo: str, issue: int, key: str, run_id: str, runner) -> str | None:
    """A prior `## IMC ended` post for this run: it carries this run's marker,
    starts with the end heading, and was written by the current gh user.
    Keyed on the journal's stable run id, never on regenerated body text
    (#1042 reforge). Any read failure refuses rather than re-posting."""
    login = _gh_login(runner)
    if not login:
        raise SystemExit("imc-waiver: could not resolve the gh user to check for a prior "
                         "`## IMC ended` post; nothing was posted.")
    found = runner(["gh", "api", "--paginate", f"repos/{repo}/issues/{issue}/comments",
                    "--jq", ".[] | [.html_url, .body, .user.login] | @json"],
                   capture_output=True, text=True, check=False)
    if found.returncode != 0:
        raise SystemExit(f"imc-waiver: could not read {repo}#{issue}'s comments to check for a "
                         f"prior `## IMC ended` post: {(found.stderr or '').strip()}")
    marker = end_marker(key, run_id)
    for line in (found.stdout or "").splitlines():
        try:
            url, body, author = json.loads(line)
        except (ValueError, TypeError):
            continue
        body = str(body)
        if (author == login and marker in body
                and body.lstrip().startswith(end_heading(key))):
            return str(url)
    return None


def end(key: str, *, reason: str, now: datetime | None = None,
        batch_state_path: Path | None = None, runner=subprocess.run) -> dict:
    """harmonic-forge#1042: end an IMC waiver at the IMC stop. Returns the
    final archived receipt (status `ended`).

    Reforge (sticky-wicket, two passes): one journal -- the live receipt,
    written once as `ending` with a stable `ended_at` run id -- drives every
    step under a per-key lock. A retry from any crash point resumes that
    journal; nothing is re-derived. The live receipt is never rewritten as
    `ended`: the archived `ended` file is written first, then the live one is
    removed, so IMC stays enforced until the record is complete."""
    key = key.strip().upper()
    if not reason or not reason.strip():
        raise SystemExit("imc-waiver: --reason must be non-empty.")
    target = key_target(key)
    if target is None:
        raise SystemExit(f"imc-waiver: {key} is not a repo-prefixed issue key.")
    repo, issue = target
    moment = now or _now()
    with _end_lock(key):
        live = receipt_path(key)
        if live.exists():
            data = read_json(live)
            if data is None:
                data = {"unreadable": True,
                        "unreadable_original": live.read_text(encoding="utf-8",
                                                              errors="replace")}
            if data.get("status") in ("ending", "ended") and data.get("ended_at"):
                ending = data  # resume the journal: same run id, same reason
            else:
                stop = imc_stop(repo, issue, data.get("granted_at"))
                if stop is None:
                    raise SystemExit(
                        f"imc-waiver: {repo}#{issue} is not at an IMC stop: its preclose receipt "
                        "shows neither two current-epoch passes run under this waiver (forced "
                        "cross-family) that both left surviving findings nor a current-epoch "
                        "PATCH/REFORGE mechanism-cluster verdict after such a pass. `end` is the "
                        "exit from that stop, never a way around the forced cross-family pass.")
                ending = {**data, "status": "ending", "end_reason": reason.strip(),
                          "ended_at": _iso(moment), "end_stop": stop}
                write_json_atomic(live, ending)
        else:
            pending = _pending_end(key)
            if pending is None:
                raise SystemExit(f"imc-waiver: no IMC waiver receipt for {key} and no "
                                 "interrupted `end` to complete.")
            ending = read_json(pending) or {}
        run_id = str(ending.get("ended_at"))
        archived = archive_dir() / f"{key}.waiver.{_stamp(_parse_time(run_id) or moment)}.json"
        _revoke_authority(key, moment, batch_state_path)
        url = ending.get("end_comment_url") or _existing_end_comment(repo, issue, key, run_id,
                                                                     runner)
        if url is None:
            url = post_and_confirm(repo, issue, end_comment_body(key, repo, issue, ending),
                                   runner)
        final = {**ending, "status": "ended", "end_comment_url": url}
        archive_dir().mkdir(parents=True, exist_ok=True)
        write_json_atomic(archived, final)
        if live.exists():
            live.unlink()
        return final


def main() -> int:
    parser = argparse.ArgumentParser(description="Record an IMC waiver on its issue (R-0384).")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="post the waiver comment and write the receipt")
    rec.add_argument("--key", required=True, help="the IMC key, e.g. F892")
    fin = sub.add_parser("end", help="end the waiver at an IMC stop (harmonic-forge#1042)")
    fin.add_argument("--key", required=True, help="the IMC key, e.g. F892")
    fin.add_argument("--reason", required=True, help="why the waiver ends")
    args = parser.parse_args()
    target = key_target(args.key)
    if target is None:
        raise SystemExit(f"imc-waiver: {args.key} is not a repo-prefixed issue key.")
    from manifest_identity import apply_project_identity  # noqa: PLC0415
    apply_project_identity(target[0])  # harmonic-forge#804
    if args.command == "end":
        ended = end(args.key, reason=args.reason)
        print(f"imc-waiver: ended {ended.get('key', args.key.upper())} -- "
              f"{ended['end_comment_url']}")
        print(f"  archived receipt: {archive_dir()}")
        print("  the issue now owes R-0229's human pass.")
        return 0
    receipt = record(args.key)
    print(f"imc-waiver: recorded {receipt['key']} -- {receipt['comment_url']}")
    print(f"  receipt: {receipt_path(receipt['key'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
