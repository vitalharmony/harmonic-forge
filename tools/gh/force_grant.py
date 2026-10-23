#!/usr/bin/env python3
"""The FORCE grant: an operator-typed, single-use authorization for one plain
`preclose_check.py --force` (harmonic-forge#1169).

`FORCE <KEY>: <reason>`, typed as the prompt's first line in a LANE=1 session,
is honored by `expand_lane_shorthand.py` on `UserPromptSubmit` (R-0117, the
same provenance BATCH and IMC use). It writes `<KEY>.json` under
`FORCE_STATE_DIR` (default `~/.claude/state/force/`) as `pending`, bound to the
head the force covers. Nothing can use it yet:

- `record --key` posts `## FORCE grant — <KEY>` with the reason, refetches it,
  and only then rewrites the grant as `usable` with the comment id. A crash
  between the post and that rewrite leaves it `pending`; a retry posts again
  and completes it (the IMC waiver's two-step shape, `imc_waiver.record`).
- `require()` answers whether a usable, unexpired, unconsumed grant covers
  exactly this repo#issue at this head, and names which test failed if not.
- `consume()` marks it spent. Only `--complete --force` consumes; `--plan
  --force` only checks.

It covers the plain force only. `--force --reforge` and `--cluster-verdict
--force` take no grant (harmonic-forge#1174 is that follow-up), and nothing
here touches `imc_refusal`: an issue holding an IMC receipt is refused before
any grant is consulted (operator ruling 2026-10-22).

Like BATCH and IMC, this trusts the operator-typed line and the session. The
grant file and the public comment are an audit trail, not proof against a
session that writes this directory itself.

Run:
    python3 tools/gh/force_grant.py record --key F1166
    python3 tools/gh/force_grant.py status --key F1166
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import imc_waiver  # noqa: E402  (key, time and atomic-write helpers, one copy)

#: `None` means `~/.claude/state/force`, resolved at call time, so a test that
#: patches this global or `HOME` never reaches the live store.
FORCE_STATE_DIR: Path | None = None
STATE_RELPATH = Path(".claude") / "state" / "force"
#: Long enough for the #1076 preflight and both force runs; a named constant
#: the operator can change (the handoff's one asserted assumption).
TTL_HOURS = 1.0
HOOK_VERSION = "1"


class GrantRefused(Exception):
    """`require()`'s refusal; the message names the failed test."""


def state_dir() -> Path:
    return FORCE_STATE_DIR if FORCE_STATE_DIR is not None else Path.home() / STATE_RELPATH


def grant_path(key: str) -> Path:
    return state_dir() / f"{key.upper()}.json"


def reason_sha256(reason: str) -> str:
    return hashlib.sha256(reason.encode("utf-8")).hexdigest()


def covered_head(repo: str, issue: int) -> str | None:
    """The head a force for this issue covers: the latest post-verdict check's
    `head_sha` when one exists (the PATCH path, where `--force` is what covers
    the final head), else the receipt's latest pass head. None: no receipt."""
    import preclose_check  # noqa: PLC0415  (imports this module; never at top level)
    import preclose_passes  # noqa: PLC0415
    return preclose_passes.force_head(preclose_check.find_receipt(repo, issue))


def write_pending(key: str, repo: str, issue: int, sha: str, reason: str,
                  session_id: str | None, line: str, now: datetime | None = None) -> dict:
    """The hook's write. A new FORCE line replaces any earlier grant for the key."""
    moment = now or imc_waiver._now()
    grant = {
        "key": key.upper(), "repo": repo, "issue": issue, "sha": sha, "reason": reason,
        "reason_sha256": reason_sha256(reason), "granted_at": imc_waiver._iso(moment),
        "expires_at": imc_waiver._iso(moment + timedelta(hours=TTL_HOURS)),
        "session_id": session_id or "", "line": line, "status": "pending",
        "comment_url": None, "comment_id": None, "hook_version": HOOK_VERSION,
    }
    imc_waiver.write_json_atomic(grant_path(key), grant)
    return grant


def heading(key: str) -> str:
    return f"## FORCE grant — {key.upper()}"


def comment_body(grant: dict) -> str:
    return "\n".join([
        heading(grant["key"]),
        "",
        f"- **Who:** the operator, through a typed prompt that passed the BATCH provenance "
        f"check (R-0117), in session `{grant.get('session_id') or 'unknown'}`.",
        f"- **When:** `{grant.get('granted_at')}`, usable until `{grant.get('expires_at')}`.",
        f"- **What it authorizes:** one plain `preclose_check.py --force` for "
        f"{grant['repo']}#{grant['issue']} at `{grant['sha'][:12]}`, single use. Never "
        "`--reforge` or `--cluster-verdict`, and never past an IMC receipt.",
        f"- **Reason:** {grant['reason']}",
        "",
        f"<!-- force-grant key={grant['key']} granted_at={grant.get('granted_at')} "
        f"sha={grant['sha']} -->",
    ])


def _expired(grant: dict, now: datetime) -> bool:
    expiry = imc_waiver._parse_time(grant.get("expires_at"))
    return expiry is None or now >= expiry


def record(key: str, *, now: datetime | None = None, runner=subprocess.run) -> dict:
    """AC2: post the grant, refetch it, and only then mark it usable."""
    key = key.strip().upper()
    lane = os.environ.get("LANE", "")
    if lane != "1":
        raise SystemExit(f"force-grant: LANE is {lane!r}; a FORCE grant is recorded only in a "
                         "LANE=1 session.")
    grant = imc_waiver.read_json(grant_path(key))
    if grant is None:
        raise SystemExit(f"force-grant: no FORCE grant for {key}. Only an operator-typed "
                         f"`FORCE {key}: <reason>` line creates one.")
    moment = now or imc_waiver._now()
    if _expired(grant, moment):
        raise SystemExit(f"force-grant: the FORCE grant for {key} expired at "
                         f"{grant.get('expires_at')}. The operator types a new FORCE line.")
    if grant.get("status") == "consumed":
        raise SystemExit(f"force-grant: the FORCE grant for {key} is already consumed.")
    if grant.get("status") == "usable":
        return grant
    url = imc_waiver.post_and_confirm(grant["repo"], int(grant["issue"]), comment_body(grant), runner)
    usable = {**grant, "status": "usable", "comment_url": url,
              "comment_id": url.rsplit("#issuecomment-", 1)[1],
              "recorded_at": imc_waiver._iso(moment)}
    imc_waiver.write_json_atomic(grant_path(key), usable)
    return usable


def require(repo: str, issue: int, head_sha: str, now: datetime | None = None) -> dict:
    """AC3: the usable grant covering exactly this force, or GrantRefused."""
    key = imc_waiver.issue_key(repo, issue)
    if key is None:
        raise GrantRefused(f"{repo}#{issue} has no repo-prefixed issue key")
    grant = imc_waiver.read_json(grant_path(key))
    if grant is None:
        raise GrantRefused(f"no FORCE grant exists for {key}. The operator types "
                           f"`FORCE {key}: <reason>` as the first line of a prompt")
    if grant.get("repo", "").lower() != repo.lower() or int(grant.get("issue") or 0) != issue:
        raise GrantRefused(f"the FORCE grant for {key} names {grant.get('repo')}#{grant.get('issue')}")
    status = grant.get("status")
    if status == "consumed":
        raise GrantRefused(f"the FORCE grant for {key} is consumed (single use)")
    if status != "usable":
        raise GrantRefused(f"the FORCE grant for {key} is {status or 'unreadable'}: run "
                           f"`python3 tools/gh/force_grant.py record --key {key}` first")
    if _expired(grant, now or imc_waiver._now()):
        raise GrantRefused(f"the FORCE grant for {key} expired at {grant.get('expires_at')}")
    if not head_sha or str(grant.get("sha")) != head_sha:
        raise GrantRefused(f"the FORCE grant for {key} covers {str(grant.get('sha'))[:12]}, not "
                           f"{head_sha[:12]}: the head moved after the grant")
    return grant


def consume(key: str, head_sha: str, now: datetime | None = None) -> dict:
    grant = imc_waiver.read_json(grant_path(key))
    if grant is None:
        raise GrantRefused(f"no FORCE grant exists for {key}")
    spent = {**grant, "status": "consumed", "consumed_at": imc_waiver._iso(now or imc_waiver._now()),
             "consumed_sha": head_sha}
    imc_waiver.write_json_atomic(grant_path(key), spent)
    return spent


def main() -> int:
    parser = argparse.ArgumentParser(description="FORCE grants for preclose --force (harmonic-forge#1169).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "status"):
        command = sub.add_parser(name)
        command.add_argument("--key", required=True)
    args = parser.parse_args()
    if args.command == "record":
        grant = record(args.key)
        print(f"force-grant: {grant['key']} usable for {grant['repo']}#{grant['issue']} at "
              f"{grant['sha'][:12]} until {grant['expires_at']} -- {grant['comment_url']}")
        return 0
    grant = imc_waiver.read_json(grant_path(args.key))
    print(f"force-grant: {args.key.upper()}: " + (
        "none" if grant is None else
        f"{grant.get('status')} at {str(grant.get('sha'))[:12]}, expires {grant.get('expires_at')}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
