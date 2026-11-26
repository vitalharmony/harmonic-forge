#!/usr/bin/env python3
"""The ci-outage release (harmonic-forge#1565, Option A): an operator-typed
`FORCE <KEY> ci-outage <40-hex sha>: <reason>` grant releases one pre-merge AE
at that SHA past a CI outage, only beside a `check-full` receipt in its own
sidecar storage (`check_receipt.check_full_path`, keyed by repo SHA and forge
SHA), and only when gate_ci's own judge reads `pending` or `absent`: a closed
allow-set, so `red`, `unknown` and any future state refuse (operator D10).
An AE declaring `--prod-run` never takes this release (R-0374's grant path).

`ae_release` requires the usable grant and the sidecar keyed by a non-empty
forge HEAD; `substitute_line` names the substitute above the footer; `reserve`
consumes the grant right before the POST. Nothing else is released: Lane 3's
PASS is unchanged and still needs green CI, and nothing reads a grant id from
comment text.

Like FORCE, IMC and BATCH, the grant store and the receipt store are an audit
trail, not proof against a session that writes them itself (force_grant.py's
docstring).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _path in (HERE, HERE.parent / "hooks"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
import check_receipt  # noqa: E402
import force_grant  # noqa: E402
import imc_waiver  # noqa: E402

MODE = "ci-outage"
CHECK_NAME = "ci-outage-grant"
#: The CI states a grant may release; anything else refuses (operator D10, #1565 pitch change 2).
RELEASE_STATES = ("pending", "absent")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
#: What `ae_release` read, by SHA, for `substitute_line` and `reserve` in the same run.
_RELEASES: dict[str, dict] = {}


def force_line(key: str, sha: str) -> str:
    return f"`FORCE {key} {MODE} {sha}: <reason>`"


def source_slug(source_repo: str) -> str | None:
    return check_receipt.repo_slug(f"https://github.com/{source_repo}")


def _consumed_refusal(repo: str, issue: int, sha: str, fail) -> None:
    """A ci-outage grant already spent on this SHA refuses loudly (exit 1, never
    the pending wait)."""
    key = imc_waiver.issue_key(repo, issue)
    grant = force_grant.read_grant(key) if key else None
    if not (grant and grant.get("mode") == MODE and grant.get("status") == "consumed"
            and grant.get("consumed_sha") == sha):
        return
    fail(f"the ci-outage grant for {key} at {sha[:12]} is already consumed (one grant, one AE). A "
         f"further AE at this SHA needs a new grant: the operator types {force_line(key, sha)}")


def ae_release(repo: str, issue: int, source_repo: str, sha: str, state: str, prod_run,
               fail) -> str | None:
    """`CHECK_NAME` when a usable ci-outage grant releases this AE, else None (the
    caller's wait or refusal applies). `state` is the caller's one
    `authorizing_conclusion` read. Requires, never consumes."""
    if not _FULL_SHA.fullmatch(sha or ""):
        return None
    try:
        grant = force_grant.require(repo, issue, sha, mode=MODE)
    except force_grant.GrantRefused:
        _consumed_refusal(repo, issue, sha, fail)
        return None
    key = grant["key"]
    if source_repo.lower() != repo.lower():  # the sidecar and the grant name the issue repo's SHA
        fail(f"the ci-outage grant for {key} releases an AE whose CI is {repo}'s, not {source_repo}'s "
             f"(a cross-repo AE); nothing was posted")
    if prod_run:  # harmonic-forge#1565 AC3, after merged_readonly_refusal's text
        fail(f"the AE declares --prod-run; a production step is R-0374's grant path, not the "
             f"ci-outage grant for {key}; nothing was posted")
    if state not in RELEASE_STATES:  # a closed allow-set: red, unknown and any future state refuse
        fail(f"CI is {state} on {sha[:12]}; a ci-outage release needs pending or absent, so the "
             f"ci-outage grant for {key} releases nothing; nothing was posted")
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
    A post that fails after this leaves the grant spent: the next AE at this SHA
    refuses loudly and names the new FORCE line (#894's P2 shape)."""
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
