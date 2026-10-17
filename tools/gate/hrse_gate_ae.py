#!/usr/bin/python3 -I
"""`hrse-gate grant-from-ae`: mint a production grant from the posted AE
(harmonic-forge#1064).

The operator used to copy the issue number, the 40-hex SHA, the script and the
dry-run/apply flag out of a `--prod-run` AE by hand. Every value is already in
the AE's reserved footer field (`tools/gh/_prod_run.py`):

    prod-run=issue:<N>,sha:<40 hex>,script:scripts/1-<name>.py,apply:true|false
    prod-run=issue:<N>,sha:<40 hex>,count-label:<Label>

**Nothing read from GitHub is trusted.** The AE is written by the operator's
own account, which every lane also uses, and a lane posting through raw
`gh api` can forge the footer. The authorization is the operator's `y`, typed
at a root-password `sudo` prompt after reading the action shown here. This
verb only saves the typing.

Order of checks, each refusing with nothing minted: euid, TTY, token file,
TTL, the issue's comments, the AE for the requested mode, the SHA (an open PR's
head, or `main`'s current head for a post-merge step: operator ruling
2026-10-17), an `apply`'s spent dry-run receipt, then the confirmation.

Loaded only by `hrse_gate_broker.main` on this verb's branch, so the one
NOPASSWD path (`consume`) never imports it. Stdlib only, like the broker, and
installed root-owned beside it (`install-hrse-gate.md`). The footer grammar is
restated here, not imported from the forge tree; `test_hrse_gate_ae.py` checks
it against `_prod_run` on a fixture set.
"""
from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.request
from types import ModuleType

#: The running broker. Bound by `grant_from_ae`, never imported by name: the
#: broker is installed as `/usr/local/libexec/hrse-gate` (no `.py`), so
#: `import hrse_gate_broker` would not resolve there.
broker: ModuleType = None  # type: ignore[assignment]


def bind(module: ModuleType) -> None:
    global broker
    broker = module

_FENCE = re.compile(r"```.*?```", re.S)
#: Stricter than `_prod_run`: the footer must open a line of its own (so a `>`
#: blockquoted copy never counts; preclose pass 1, cross-family).
_TRAILING_FOOTER = re.compile(r"(?:\A|\n)<!--\s*l1-post\s+v1;([^>]*?)-->\s*\Z", re.I)
_FIELD = re.compile(r"(?:^|;)\s*prod-run=([^;\s]+)\s*(?:;|$)")
_KIND_AE = re.compile(r"(?:^|;)\s*kind=ae\s*(?:;|$)")
_SCRIPT_FIELD = re.compile(
    r"^issue:(\d{1,9}),sha:([0-9a-f]{40}),script:(scripts/1-[A-Za-z0-9._-]+\.py),apply:(true|false)$")
_LABEL_FIELD = re.compile(r"^issue:(\d{1,9}),sha:([0-9a-f]{40}),count-label:([A-Za-z][A-Za-z0-9_]*)$")


def _refuse(message: str) -> broker.Refused:
    return broker.Refused(broker.EXIT_REFUSED, message)


def footer_action(body: str) -> dict | None:
    """The declared action of an AE comment, or None.

    Read only from the comment's own trailing footer after fenced blocks are
    stripped, exactly as `_prod_run.raw_field` does, and only from a footer
    whose kind is `ae`."""
    body = body or ""
    if body.count("```") % 2:
        return None  # an unclosed fence: nothing after it is a real footer
    footer = _TRAILING_FOOTER.search(_FENCE.sub("", body))
    if not footer or not _KIND_AE.search(footer.group(1)):
        return None
    field = _FIELD.search(footer.group(1))
    if not field:
        return None
    script = _SCRIPT_FIELD.match(field.group(1))
    if script:
        return {"issue": int(script.group(1)), "sha": script.group(2),
                "script": script.group(3), "apply": script.group(4) == "true"}
    label = _LABEL_FIELD.match(field.group(1))
    if label:
        return {"issue": int(label.group(1)), "sha": label.group(2), "count_label": label.group(3)}
    return None


def action_text(action: dict) -> str:
    """The broker's CLI grammar for an action, so the mint goes through `_key`."""
    if "script" in action:
        return f"script={action['script']}" + (",apply" if action["apply"] else "")
    return f"count-label={action['count_label']}"


def _fits_mode(action: dict, mode: str) -> bool:
    if "script" in action:
        return action["apply"] == (mode == "apply")
    return mode == "dry-run"  # a count has no apply form


def _target(action: dict) -> tuple:
    return (action.get("script"), action.get("count_label"))


def select_ae(actions: list[dict], issue: int, mode: str) -> dict:
    """The newest AE for `mode`. `actions` is oldest-first, as GitHub returns
    comments. A dry-run AE then an apply AE at one SHA is R-0377's normal pair,
    not ambiguity: only two AEs for the SAME mode at the newest SHA that name
    different targets are ambiguous."""
    mine = [a for a in actions if a["issue"] == issue]
    if not mine:
        raise _refuse(f"no --prod-run AE on issue {issue}: nothing minted")
    fitting = [a for a in mine if _fits_mode(a, mode)]
    if not fitting:
        raise _refuse(f"mode mismatch: no --prod-run AE on issue {issue} declares a {mode}; "
                      "nothing minted")
    newest = fitting[-1]
    same_sha = [a for a in fitting if a["sha"] == newest["sha"]]
    if len(same_sha) >= 2 and _target(same_sha[-2]) != _target(newest):
        raise _refuse(f"ambiguous: the newest two {mode} AEs at {newest['sha'][:12]} name "
                      "different actions; mint by hand with `grant`")
    return newest


def read_token() -> str:
    """The token file must be a regular, root-owned file nobody else can read."""
    try:
        fd = os.open(broker_token_path(), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _refuse(f"token file {broker_token_path()} unreadable: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise _refuse(f"token file {broker_token_path()} must be a root-owned regular "
                          "file with mode 0600")
        try:
            token = os.read(fd, 4096).decode("ascii", "replace").strip()
        except OSError as exc:
            raise _refuse(f"token file {broker_token_path()} unreadable: {exc}") from exc
    finally:
        os.close(fd)
    if not token:
        raise _refuse(f"token file {broker_token_path()} is empty")
    return token


def broker_token_path():
    return broker.TOKEN_PATH


def github(path: str, token: str):
    """One GET against the GitHub REST API; any failure refuses."""
    request = urllib.request.Request(
        f"{broker.GITHUB_API}/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hrse-gate"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise _refuse(f"GitHub cannot be read ({path}): {exc}") from exc


def issue_comments(issue: int, token: str) -> list[str]:
    """Comment bodies, oldest first. More than MAX_COMMENT_PAGES pages refuses
    rather than reading a stale prefix: the newest AE is on the last page."""
    bodies: list[str] = []
    for page in range(1, broker.MAX_COMMENT_PAGES + 2):
        batch = github(f"repos/{broker.GATED_REPO}/issues/{issue}/comments"
                       f"?per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise _refuse("GitHub returned a non-list for issue comments")
        if not batch:
            return bodies
        if page > broker.MAX_COMMENT_PAGES:
            raise _refuse(f"issue {issue} has more than {broker.MAX_COMMENT_PAGES * 100} "
                          "comments; mint by hand with `grant`")
        bodies.extend(str(c.get("body") or "") for c in batch if isinstance(c, dict))
    return bodies


def sha_is_head(sha: str, token: str) -> str:
    """Where the SHA is a head, or refuse: an open PR's head first, then
    `main`'s current head (an R-0377 post-merge step)."""
    pulls = github(f"repos/{broker.GATED_REPO}/commits/{sha}/pulls", token)
    if not isinstance(pulls, list):
        raise _refuse("GitHub returned a non-list for the commit's pull requests")
    for pr in pulls:
        if not isinstance(pr, dict) or not isinstance(pr.get("head"), dict):
            raise _refuse("GitHub returned a malformed pull request entry")
        if pr.get("state") == "open" and pr["head"].get("sha") == sha:
            return f"head of open PR #{pr.get('number')}"
    branch = github(f"repos/{broker.GATED_REPO}/branches/main", token)
    if not isinstance(branch, dict) or not isinstance(branch.get("commit"), dict):
        raise _refuse("GitHub returned a malformed branch")
    if branch["commit"].get("sha") == sha:
        return "current head of main"
    raise _refuse(f"{sha[:12]} is neither an open PR's head nor main's current head: "
                  "the AE is stale; nothing minted")


def has_spent_dry_run(issue: int, sha: str, script: str) -> bool:
    """A consumed dry-run grant for this exact issue, SHA and script, read from
    the broker's own root-owned receipts (the lane uid cannot write there)."""
    for path in sorted((broker.STORE_ROOT / "receipts").glob("*.json")):
        record = broker._read(path) if broker.NONCE.match(path.stem) else None
        if isinstance(record, dict) and record.get("issue") == issue \
                and record.get("sha") == sha and record.get("script") == script \
                and record.get("apply") is False:
            return True
    return False


def isatty() -> bool:
    return os.isatty(0) and os.isatty(1)


def confirm(text: str) -> bool:
    """Show `text` and read one answer from the controlling TTY. Only `y` or
    `yes` is consent; anything else, EOF or an error is not."""
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            tty.write(text + "\nMint this grant? [y/N] ")
            tty.flush()
            answer = tty.readline()
    except (OSError, ValueError):  # ValueError covers undecodable input
        return False
    return answer.strip().lower() in ("y", "yes")


def describe(action: dict, where: str, ttl_minutes: int) -> str:
    target = f"script {action['script']}" if "script" in action else f"count-label {action['count_label']}"
    mode = ("APPLY (writes production)" if action.get("apply")
            else "DRY RUN" if "script" in action else "COUNT (read-only)")
    return (f"Grant to mint:\n  issue   {broker.GATED_REPO}#{action['issue']}\n"
            f"  sha     {action['sha']}  ({where})\n  action  {target}\n\n  {mode}\n\n"
            f"  ttl     {ttl_minutes} minutes")


def grant_from_ae(module: ModuleType, issue: str, mode: str, ttl_minutes: int) -> str:
    bind(module)
    if broker._geteuid() != 0:
        raise _refuse("grant-from-ae runs as root only: the operator mints a grant, a lane never does")
    if not isatty():
        raise _refuse("grant-from-ae needs a TTY: the confirmation is the authorization")
    if mode not in ("dry-run", "apply"):
        raise _refuse(f"mode must be dry-run or apply, got {mode!r}")
    if not (issue.isascii() and issue.isdecimal()) or len(issue) > 9 or int(issue) < 1:
        raise _refuse(f"issue must be a positive integer, got {issue!r}")
    if not 1 <= ttl_minutes <= broker.MAX_TTL_MINUTES:
        raise _refuse(f"--ttl-minutes must be 1..{broker.MAX_TTL_MINUTES}")
    token = read_token()
    actions = [a for a in (footer_action(b) for b in issue_comments(int(issue), token)) if a]
    action = select_ae(actions, int(issue), mode)
    where = sha_is_head(action["sha"], token)
    if mode == "apply" and not has_spent_dry_run(action["issue"], action["sha"], action["script"]):
        raise _refuse(f"apply refused: no spent dry-run grant for issue {action['issue']}, "
                      f"{action['sha'][:12]}, {action['script']}; run the dry run first")
    if not confirm(describe(action, where, ttl_minutes)):
        raise _refuse("not confirmed: nothing minted")
    key = broker._key(str(action["issue"]), action["sha"], action_text(action))
    return broker._mint(key, ttl_minutes)

