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
import contextlib
import fcntl
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


@contextlib.contextmanager
def grant_lock(key: str):
    """Serialize every read-modify-write of one key's grant (preclose pass 1,
    harmonic-forge#1169). Short: never held across the GitHub post. The hook,
    `record` and `consume` all take it; the receipt lock does not cover them."""
    path = state_dir() / f"{key.upper()}.json.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def reason_sha256(reason: str) -> str:
    return hashlib.sha256(reason.encode("utf-8")).hexdigest()


def covered_head(repo: str, issue: int) -> str | None:
    """The head a force for this issue covers: the latest post-verdict check's
    `head_sha` when one exists (the PATCH path, where `--force` is what covers
    the final head), else the receipt's latest pass head. None: no receipt."""
    import preclose_check  # noqa: PLC0415  (imports this module; never at top level)
    import preclose_passes  # noqa: PLC0415
    return preclose_passes.force_head(preclose_check.find_receipt(repo, issue))


def read_grant(key: str) -> dict | None:
    """The current grant file for the key, or None."""
    return imc_waiver.read_json(grant_path(key))


def write_pending(key: str, repo: str, issue: int, sha: str | None, reason: str,
                  session_id: str | None, line: str, now: datetime | None = None,
                  mode: str = "plain", sha_prefix: str | None = None) -> dict:
    """The hook's write. A new FORCE line replaces any earlier grant for the key.
    `mode` is plain, reforge or cluster (harmonic-forge#1174): plain and cluster
    bind the full `sha`; reforge binds the operator-typed `sha_prefix`."""
    moment = now or imc_waiver._now()
    grant = {
        "key": key.upper(), "repo": repo, "issue": issue, "sha": sha, "reason": reason,
        "mode": mode, "sha_prefix": sha_prefix,
        "reason_sha256": reason_sha256(reason), "granted_at": imc_waiver._iso(moment),
        "expires_at": imc_waiver._iso(moment + timedelta(hours=TTL_HOURS)),
        "session_id": session_id or "", "line": line, "status": "pending",
        "comment_url": None, "comment_id": None, "hook_version": HOOK_VERSION,
    }
    with grant_lock(key):
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
        f"- **What it authorizes:** {_authorizes(grant)} for "
        f"{grant['repo']}#{grant['issue']} at `{_bound(grant)[:12]}`, single use, and nothing "
        "else: never another mode, and never past an IMC receipt.",
        f"- **Reason:** {grant['reason']}",
        "",
        f"<!-- force-grant key={grant['key']} granted_at={grant.get('granted_at')} "
        f"sha={_bound(grant)} mode={grant.get('mode') or 'plain'} -->",
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
    # The post ran outside the lock. Mark usable only the grant this run
    # posted, and only if it is still pending (preclose pass 1, findings 2, 5).
    with grant_lock(key):
        current = imc_waiver.read_json(grant_path(key))
        if current is None or current.get("granted_at") != grant.get("granted_at"):
            raise SystemExit(f"force-grant: the FORCE grant for {key} was replaced during the "
                             f"post (now granted {(current or {}).get('granted_at')}); record "
                             "the newer grant instead.")
        if current.get("status") != "pending":
            return current
        usable = {**current, "status": "usable", "comment_url": url,
                  "comment_id": url.rsplit("#issuecomment-", 1)[1],
                  "recorded_at": imc_waiver._iso(moment)}
        imc_waiver.write_json_atomic(grant_path(key), usable)
    return usable


#: harmonic-forge#1174: what each mode authorizes, for the public comment.
_AUTHORIZES = {"plain": "one plain `preclose_check.py --force`",
               "reforge": "one `preclose_check.py --force --reforge`",
               "cluster": "one `preclose_check.py --cluster-verdict ... --force`"}


def _mode(grant: dict) -> str:
    """A grant with no `mode` (F1169-era) is plain."""
    return grant.get("mode") or "plain"


def _authorizes(grant: dict) -> str:
    return _AUTHORIZES.get(_mode(grant), _AUTHORIZES["plain"])


def _bound(grant: dict) -> str:
    return str((grant.get("sha_prefix") if _mode(grant) == "reforge" else grant.get("sha")) or "")


def _scope(grant: dict) -> str:
    """What a grant is bound to, for the CLI: a reforge binds a prefix and a
    cluster grant may bind nothing, so `sha` alone printed None (F1174)."""
    bound = _bound(grant)
    return f"({_mode(grant)}) at {bound[:12]}" if bound else f"({_mode(grant)})"


def _binding_refusal(grant: dict, key: str, head_sha: str, mode: str) -> str | None:
    """Why this grant does not cover a `mode` force at `head_sha`, or None."""
    if _mode(grant) != mode:
        return (f"the FORCE grant for {key} authorizes a {_mode(grant)} force, not a {mode} one; "
                f"the operator types `FORCE {key}{'' if mode == 'plain' else ' ' + mode} ...: <reason>`")
    if mode == "reforge":
        prefix = str(grant.get("sha_prefix") or "")
        if len(prefix) < 12 or not head_sha or not head_sha.startswith(prefix):
            return f"the reforge grant for {key} names {prefix[:12] or '?'}, not {head_sha[:12]}"
        return None
    if not head_sha or str(grant.get("sha")) != head_sha:
        return (f"the FORCE grant for {key} covers {str(grant.get('sha'))[:12]}, not "
                f"{head_sha[:12]}: the head moved after the grant")
    return None


def require(repo: str, issue: int, head_sha: str, now: datetime | None = None,
            mode: str = "plain") -> dict:
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
    refusal = _binding_refusal(grant, key, head_sha, mode)
    if refusal:
        raise GrantRefused(refusal)
    return grant


def consume(key: str, expected_granted_at: str, head_sha: str, now: datetime | None = None,
            mode: str = "plain") -> dict:
    """Spend exactly the grant `require()` returned: same `granted_at`, still
    usable and unexpired, bound to this head. Anything else refuses (preclose
    pass 1, finding 1), so a newer grant is never spent in an older one's place."""
    moment = now or imc_waiver._now()
    with grant_lock(key):
        grant = imc_waiver.read_json(grant_path(key))
        if grant is None:
            raise GrantRefused(f"no FORCE grant exists for {key}")
        if grant.get("granted_at") != expected_granted_at:
            raise GrantRefused(f"the FORCE grant for {key} was replaced (granted "
                               f"{grant.get('granted_at')}, not {expected_granted_at})")
        if grant.get("status") != "usable":
            raise GrantRefused(f"the FORCE grant for {key} is {grant.get('status')}, not usable")
        if _expired(grant, moment):
            raise GrantRefused(f"the FORCE grant for {key} expired at {grant.get('expires_at')}")
        refusal = _binding_refusal(grant, key, head_sha, mode)
        if refusal:
            raise GrantRefused(refusal)
        spent = {**grant, "status": "consumed", "consumed_at": imc_waiver._iso(moment),
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
        print(f"force-grant: {grant['key']} usable for {grant['repo']}#{grant['issue']} "
              f"{_scope(grant)} until {grant['expires_at']} -- {grant['comment_url']}")
        return 0
    grant = imc_waiver.read_json(grant_path(args.key))
    print(f"force-grant: {args.key.upper()}: " + (
        "none" if grant is None else
        f"{grant.get('status')} {_scope(grant)}, expires {grant.get('expires_at')}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
