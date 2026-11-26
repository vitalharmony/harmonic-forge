#!/usr/bin/env python3
"""The ci-outage release (harmonic-forge#1565): an operator-typed
`FORCE <KEY> ci-outage <40-hex sha>: <reason>` grant releases one pre-merge AE
at that SHA past a CI outage, and Lane 3's PASS on that AE, only beside a
`check-full` receipt in its own sidecar storage
(`check_receipt.check_full_path`, keyed by repo SHA and forge SHA).

The AE side (l1_post): `ae_release` requires the usable grant, no visible red
job, a non-empty forge HEAD and the sidecar keyed by it; `substitute_line`
names the substitute above the footer; `reserve` consumes the grant right
before the POST; `bind` writes a write-once release record after the
confirmed post. The record binds the grant to the AE's comment id and to the
SHA-256 of the AE's exact posted bytes, footer included.

The PASS side (post_lane_discussion): `pass_release` lifts only
`check_gate_result`'s unfinished-CI refusal, and only through a release
record bound to the round's AE comment id, at exactly the gated SHA, whose
posted digest still matches the AE's current body, and whose forge snapshot's
sidecar is present. It never reads the grant slot and never parses a grant id
from comment text. A failed release-record write fails closed: the PASS refuses.
A released PASS spends the record (`<record>.pass`, O_EXCL): one grant, one PASS.

Like FORCE, IMC and BATCH, the grant store, the release records and the receipt
store are an audit trail, not proof against a session that writes them itself
(force_grant.py's docstring). What this protects against is an edited comment.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _path in (HERE, HERE.parent / "hooks"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
import check_lane3_ready  # noqa: E402
import check_receipt  # noqa: E402
import force_grant  # noqa: E402
import gate_ci  # noqa: E402
import imc_waiver  # noqa: E402

MODE = "ci-outage"
CHECK_NAME = "ci-outage-grant"
#: The first line of `check_gate_result`'s unfinished-CI refusal (gate_ci.py),
#: newline included. Job names appear only after it, so no job name can forge it.
UNFINISHED_CI_REFUSAL = "[GATE] REFUSED: CI has not completed for the SHA this PASS names.\n"
#: Operator ruling Q2 (2026-11-25): only `red` is negative evidence.
RELEASE_STATES = ("pending", "absent", "unknown")
VERSION = 1
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
UNREAD_CI = "cannot read CI to rule out a visibly red job; retry"
#: What `ae_release` read, by SHA, for `substitute_line` and `reserve` in the same run.
_RELEASES: dict[str, dict] = {}


def force_line(key: str, sha: str) -> str:
    return f"`FORCE {key} {MODE} {sha}: <reason>`"


def records_dir() -> Path:
    return force_grant.state_dir() / MODE


def record_path(key: str, sha: str, grant_comment_id) -> Path:
    return records_dir() / f"{key}-{sha}-{grant_comment_id}.json"


def source_slug(source_repo: str) -> str | None:
    return check_receipt.repo_slug(f"https://github.com/{source_repo}")


def visible_red(repo: str, sha: str, reader=None) -> list[str] | None:
    """Every job the CI read shows red (operator's visible-red ruling, 2026-11-25):
    a red job means CI ran and failed, which is not an outage. None when the read
    fails: it cannot rule out a red job, so both callers refuse (#1565 P4, fail closed)."""
    runs, failure = gate_ci._read_check_runs(repo, sha, reader=reader)
    if failure is not None:
        return None
    return sorted(f"{r.get('name')}:{r.get('conclusion')}" for r in gate_ci._latest_per_name(runs)
                  if (r.get("conclusion") or "").lower() in gate_ci._BAD)


def _consumed_refusal(repo: str, issue: int, sha: str, fail) -> None:
    """A ci-outage grant already spent on this SHA refuses loudly (exit 1, never
    the pending wait), naming the AE it bound or that none was bound."""
    key = imc_waiver.issue_key(repo, issue)
    grant = force_grant.read_grant(key) if key else None
    if not (grant and grant.get("mode") == MODE and grant.get("status") == "consumed"
            and grant.get("consumed_sha") == sha):
        return
    record = imc_waiver.read_json(record_path(key, sha, grant.get("comment_id")))
    bound = (f"it is bound to the AE {record.get('ae_url')}" if record else
             "no AE was bound (the post or the release record failed after it was consumed)")
    fail(f"the ci-outage grant for {key} at {sha[:12]} is already consumed; {bound}. A further "
         f"AE at this SHA needs a new grant: the operator types {force_line(key, sha)}")


def ae_release(repo: str, issue: int, source_repo: str, sha: str, state: str, fail,
               reader=None) -> str | None:
    """`CHECK_NAME` when a usable ci-outage grant releases this AE, else None
    (the caller's wait or refusal applies). Requires, never consumes."""
    if state not in RELEASE_STATES or not _FULL_SHA.fullmatch(sha or ""):
        return None
    try:
        grant = force_grant.require(repo, issue, sha, mode=MODE)
    except force_grant.GrantRefused:
        _consumed_refusal(repo, issue, sha, fail)
        return None
    key = grant["key"]
    red = visible_red(source_repo, sha, reader=reader)
    if red is None:
        fail(f"CI is {state} on {sha[:12]}, and the ci-outage grant for {key} {UNREAD_CI}; nothing was posted")
    if red:
        fail(f"CI is {state} on {sha[:12]} but job(s) are visibly red ({', '.join(red)}): CI ran "
             f"and failed, which is not an outage, so the ci-outage grant for {key} releases nothing")
    forge = check_receipt.forge_sha()
    if not forge:
        fail(f"the ci-outage grant for {key} needs the forge identity, and "
             f"{check_receipt.forge_unusable_reason()}; nothing was posted")
    slug = source_slug(source_repo)
    receipt = check_receipt.check_full(slug, sha, forge) if slug else None
    if receipt is None:
        fail(f"the ci-outage grant for {key} needs a check-full receipt for {sha[:12]} against forge "
             f"{forge[:12]}: run `mise run check-full` at {sha} in {source_repo}, then re-post")
    _RELEASES[sha] = {"grant": grant, "receipt": {"source_slug": slug, "forge_sha": forge}}
    return CHECK_NAME


def substitute_line(body: str, sha: str, checks: list[str]) -> str:
    """The human-readable `**CI substitute:**` line, above the footer (covered by
    body-sha256, parsed by nothing)."""
    release = _RELEASES.get(sha)
    if CHECK_NAME not in checks or release is None:
        return body
    return (body.rstrip("\n") + f"\n\n**CI substitute:** check-full receipt for {sha} (forge "
            f"{str(release['receipt']['forge_sha'])[:12]}), ci-outage grant {release['grant'].get('comment_url')}\n")


def reserve(repo: str, issue: int, sha: str, checks: list[str], fail) -> dict | None:
    """Consume exactly the grant `ae_release` read, immediately before the POST.
    A crash after this and before `bind` leaves the grant spent and no record:
    the next AE refuses loudly and the PASS refuses (#894's P2 shape)."""
    if CHECK_NAME not in checks:
        return None
    release = _RELEASES.get(sha)
    if release is None:
        fail(f"the ci-outage release for {sha[:12]} was not read in this run; nothing was posted")
    grant = release["grant"]
    try:
        spent = force_grant.consume(grant["key"], grant["granted_at"], sha, mode=MODE)
    except Exception as exc:  # noqa: BLE001 - a GrantRefused or a store error
        fail(f"the ci-outage grant for {grant['key']} could not be consumed ({exc}); nothing was "
             f"posted. The operator types {force_line(grant['key'], sha)}")
    return {**release, "grant": spent, "repo": repo, "issue": issue, "sha": sha}


def bind(spent: dict, comment_id, url: str, posted: str) -> None:
    """Write the write-once release record after the confirmed post. Any failure
    is printed and never raised: the post happened, and a missing record fails
    closed (Lane 3's PASS refuses)."""
    grant, sha = spent["grant"], spent["sha"]
    try:
        records_dir().mkdir(parents=True, exist_ok=True)
        record = {"version": VERSION, "repo": spent["repo"], "issue": int(spent["issue"]),
                  "key": grant["key"], "sha": sha, "mode": MODE,
                  "grant_comment_id": grant.get("comment_id"), "grant_comment_url": grant.get("comment_url"),
                  "granted_at": grant.get("granted_at"), "consumed_at": grant.get("consumed_at"),
                  "ae_comment_id": comment_id, "ae_url": url,
                  "posted_sha256": hashlib.sha256(posted.rstrip("\n").encode()).hexdigest(),
                  "receipt": spent["receipt"], "bound_at": imc_waiver._iso(imc_waiver._now())}
        fd = os.open(record_path(grant["key"], sha, grant.get("comment_id")),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
    except Exception as exc:  # noqa: BLE001 - never re-raised (pitch change 7)
        print(f"[l1-post] the AE posted at {url}, but its ci-outage release was not recorded "
              f"({type(exc).__name__}: {exc}), so Lane 3's PASS will refuse. Recovery: the operator "
              f"types {force_line(grant.get('key', '?'), sha)}, then post a new AE", file=sys.stderr)


def _records(repo: str, issue: int, ae_comment_id) -> list[dict]:
    """Release records for this repo and issue bound to exactly this AE id (R1, R2)."""
    found = []
    for path in sorted(records_dir().glob("*.json")) if records_dir().is_dir() else ():
        record = imc_waiver.read_json(path)
        if (record and str(record.get("repo", "")).lower() == repo.lower()
                and str(record.get("issue")) == str(issue)
                and str(record.get("ae_comment_id")) == str(ae_comment_id)):
            found.append({**record, "_path": str(path)})
    return found


def pass_release(repo: str, issue: int, body: str, message: str) -> tuple[bool, str]:
    """`(True, line)` when a bound release record lifts the unfinished-CI refusal
    `message`, else `(False, message)` with the reason appended."""
    if not message.startswith(UNFINISHED_CI_REFUSAL):  # R0
        return False, message

    def refuse(why: str) -> tuple[bool, str]:
        return False, f"{message}\n[GATE] ci-outage: not released: {why}"
    sha = gate_ci.gated_sha(body)
    if not sha or not _FULL_SHA.fullmatch(sha):  # R4a
        return refuse("the PASS must name the full 40-hex Head-SHA")
    authority, why = check_lane3_ready.resolve_gate_authority(
        check_lane3_ready.fetch_comments(repo, issue), sha)
    kind = check_lane3_ready.FOOTER_KIND.search((authority or {}).get("body", ""))
    if authority is None or not kind or kind.group(1).lower() != "ae":  # R5
        return refuse(f"the round's authority is not an AE ({why})")
    records = _records(repo, issue, authority.get("id"))
    if len(records) != 1:  # R1, R2
        return refuse(f"{len(records)} release record(s) are bound to the AE {authority.get('html_url')}")
    record = records[0]
    if record.get("sha") != sha:  # R4
        return refuse(f"the release record is for {str(record.get('sha'))[:12]}, not {sha[:12]}")
    if hashlib.sha256(authority.get("body", "").rstrip("\n").encode()).hexdigest() != record.get("posted_sha256"):
        return refuse(f"the AE {authority.get('html_url')} was edited after it was posted")  # R3
    snapshot = record.get("receipt") or {}
    forge = snapshot.get("forge_sha")
    # R6's single owner is check_full, which refuses an empty forge (pitch change 1, #1565 P3).
    if check_receipt.check_full(snapshot.get("source_slug") or "", sha, forge) is None:  # R6
        return refuse(f"no check-full receipt for {sha[:12]} against the recorded forge {str(forge)[:12]}")
    red = visible_red(repo, sha)
    if red is None:
        return refuse(UNREAD_CI)
    if red:
        return refuse(f"job(s) are visibly red ({', '.join(red)}), which is not an outage")
    spent = _spend(record, sha, body)
    if spent is not None:
        return refuse(spent)
    return True, (f"[GATE] ci-outage: {sha[:8]} released by the consumed ci-outage grant "
                  f"{record.get('grant_comment_url')} bound to AE {record.get('ae_url')}")


def _spend(record: dict, sha: str, body: str) -> str | None:
    """Make the PASS release single-use (#1565 P6, the AE side's reserve shape):
    create `<record>.pass` with O_EXCL before the PASS posts. None when this call
    spent it, else why not. A post that fails after it leaves the release spent."""
    marker = Path(record["_path"]).with_suffix(".pass")
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        first = imc_waiver.read_json(marker) or {}
        return (f"this release record already released a PASS at {first.get('at')} (PASS body sha256 "
                f"{str(first.get('pass_sha256'))[:12]}); one grant releases one PASS, so a PASS whose post "
                f"failed after that spent it too. Recovery: the operator types "
                f"{force_line(record.get('key', '?'), sha)}, then a new AE and sweep")
    except OSError as exc:
        return f"the release could not be marked spent ({type(exc).__name__}: {exc})"
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"pass_sha256": hashlib.sha256(body.rstrip("\n").encode()).hexdigest(),
                   "at": imc_waiver._iso(imc_waiver._now())}, handle)
    return None
