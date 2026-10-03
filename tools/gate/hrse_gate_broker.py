#!/usr/bin/python3 -I
"""Grant broker for Lane 3's production runs (harmonic-forge#878, Variant 2).

One operator-approved production run, and only the one approved. Every earlier
design held the grant in a medium the agent could write -- a GitHub comment, a
file under $HOME, a hook's regex -- and two preclose passes broke each one
(sticky-wicket REFORGE). Here the grant lives where the agent's uid gets EPERM:

    /var/lib/hrse-gate/            hrse-gate:hrse-gate 0700
        grants/<nonce>.json        written by `grant`, as root, chowned 0600
        receipts/<nonce>.json      the same file, renamed there by `consume`

Two verbs:

    grant <issue> <sha> <action> [--ttl-minutes N]
        Root only (the operator: `sudo /usr/local/libexec/hrse-gate grant ...`,
        which this host's `Defaults targetpw` makes ask for the root password).
        Prints the nonce.

    status <issue> <sha> <action>
        Read-only: the live grants and spent receipts for that exact action,
        so an operator whose run was refused can see whether the grant is
        still live before minting (a second live grant is refused anyway).

    consume <issue> <sha> <action>
        Run as hrse-gate through the single NOPASSWD sudoers rule. Finds a
        grant matching the action exactly and renames it into receipts/.
        Exits 0 only on that rename, so a grant is spent at most once even
        under concurrent callers (os.rename is atomic within one filesystem).

`<action>` is the same grammar `l1_post.py --prod-run` uses:
`script=scripts/1-<name>.py`, `script=scripts/1-<name>.py,apply`, or
`count-label=<Label>`.

There is no path-valued argument on any verb, abbreviated options are off, and
unknown tokens are refused: the sudoers rule is `consume *`, and `*` spans
spaces, so the argument grammar IS the rule's scope. The store root is a
module constant; tests patch it in-process, never through an argument.

`--version` prints the sha256 of this file. The installed copy is root-owned
and outside the repo, so editing the repo cannot change it; HRSE2's
`gate_production_run.py` compares this digest with the tracked source and
refuses on drift rather than running a stale install.

Self-contained by design (stdlib only, no forge imports): it is installed as a
single file under /usr/local/libexec.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import sys
from pathlib import Path

STORE_ROOT = Path("/var/lib/hrse-gate")
OWNER = "hrse-gate"
MAX_TTL_MINUTES = 24 * 60
DEFAULT_TTL_MINUTES = 60

SCRIPT = re.compile(r"^scripts/1-[A-Za-z0-9._-]+\.py$")
LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")

EXIT_OK = 0
EXIT_NO_GRANT = 3
EXIT_CONSUMED = 4
EXIT_EXPIRED = 5
EXIT_MISMATCH = 6
EXIT_REFUSED = 7
EXIT_DUPLICATE = 8


class Refused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _geteuid() -> int:
    return os.geteuid()


def _owner_ids() -> tuple[int, int]:
    entry = pwd.getpwnam(OWNER)
    return entry.pw_uid, entry.pw_gid


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_action(text: str) -> dict:
    """The `l1_post.py --prod-run` grammar, as a canonical dict."""
    if text.startswith("script="):
        rel, *flags = text[len("script="):].split(",")
        if not SCRIPT.match(rel):
            raise Refused(EXIT_REFUSED, f"{rel!r} is not a scripts/1-*.py migration script")
        if flags not in ([], ["apply"]):
            raise Refused(EXIT_REFUSED, f"unknown script flag(s) {flags!r}; the only one is 'apply'")
        return {"script": rel, "apply": flags == ["apply"]}
    if text.startswith("count-label="):
        label = text[len("count-label="):]
        if not LABEL.match(label):
            raise Refused(EXIT_REFUSED, f"{label!r} is not a node label")
        return {"count_label": label}
    raise Refused(EXIT_REFUSED, f"action must be script=... or count-label=..., got {text!r}")


def _key(issue: str, sha: str, action: str) -> dict:
    if not issue.isdecimal() or int(issue) < 1:
        raise Refused(EXIT_REFUSED, f"issue must be a positive integer, got {issue!r}")
    if not FULL_SHA.match(sha):
        raise Refused(EXIT_REFUSED, "sha must be a full 40-hex commit id")
    return {"issue": int(issue), "sha": sha, **parse_action(action)}


def _same_action(record: dict, key: dict) -> bool:
    return {k: record.get(k) for k in key} == key and \
        set(record) - {"nonce", "expires", "granted_at"} == set(key)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def grant(issue: str, sha: str, action: str, ttl_minutes: int) -> str:
    if _geteuid() != 0:
        raise Refused(EXIT_REFUSED, "grant runs as root only: the operator mints a grant, a lane never does")
    if not 1 <= ttl_minutes <= MAX_TTL_MINUTES:
        raise Refused(EXIT_REFUSED, f"--ttl-minutes must be 1..{MAX_TTL_MINUTES}")
    key = _key(issue, sha, action)
    with _store_lock():
        now = _now()
        _prune_expired(now)
        # One approval, one live grant (sticky-wicket PATCH 2, item 3). The
        # approval is (issue, action): the SHA is excluded because the agent
        # controls it, so a re-posted AE at a new SHA must not open a second
        # live grant while the first can still be spent at the old SHA.
        approval = {k: v for k, v in key.items() if k != "sha"}
        for path in sorted((STORE_ROOT / "grants").glob("*.json")):
            live = _read(path) if NONCE.match(path.stem) else None
            if live and {k: live.get(k) for k in approval} == approval:
                raise Refused(EXIT_DUPLICATE, f"duplicate: grant {live['nonce']} for this action "
                              f"(at {live['sha'][:12]}) is still live until {live['expires']}; "
                              "nothing minted")
        return _write_grant(key, now, ttl_minutes)


@contextlib.contextmanager
def _store_lock():
    """Serialize minting (item 3): two concurrent grants for one approval must
    not both succeed."""
    fd = os.open(STORE_ROOT / ".mint.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _prune_expired(now: dt.datetime) -> None:
    """Item 4: an expired, unspent grant is removed at the next mint, so it can
    never wedge its action behind a stale 'duplicate'."""
    for path in (STORE_ROOT / "grants").glob("*.json"):
        record = _read(path)
        try:
            expired = record is None or dt.datetime.fromisoformat(record["expires"]) <= now
        except (KeyError, TypeError, ValueError):
            expired = True
        if expired:
            path.unlink(missing_ok=True)


def _write_grant(key: dict, now: dt.datetime, ttl_minutes: int) -> str:
    nonce = secrets.token_hex(16)
    record = {**key, "nonce": nonce, "granted_at": now.isoformat(),
              "expires": (now + dt.timedelta(minutes=ttl_minutes)).isoformat()}
    path = STORE_ROOT / "grants" / f"{nonce}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        uid, gid = _owner_ids()
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o600)
        os.write(fd, json.dumps(record, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return nonce


def _matching(directory: Path, key: dict) -> list[dict]:
    found = []
    for path in sorted(directory.glob("*.json")):
        record = _read(path) if NONCE.match(path.stem) else None
        if record is not None and _same_action(record, key):
            found.append(record)
    return found


def status(issue: str, sha: str, action: str) -> str:
    """Read-only: the live grants and spent receipts for one exact action."""
    key = _key(issue, sha, action)
    now = _now()
    lines = []
    for label, directory in (("grant", STORE_ROOT / "grants"), ("receipt", STORE_ROOT / "receipts")):
        for record in _matching(directory, key):
            state = label
            if label == "grant" and dt.datetime.fromisoformat(record["expires"]) <= now:
                state = "expired grant"
            lines.append(f"{state} {record['nonce']} expires {record['expires']}")
    return "\n".join(lines) or "none"


def consume(issue: str, sha: str, action: str) -> str:
    key = _key(issue, sha, action)
    grants, receipts = STORE_ROOT / "grants", STORE_ROOT / "receipts"
    expired = mismatch = False
    for path in sorted(grants.glob("*.json")):
        if not NONCE.match(path.stem):
            continue
        record = _read(path)
        if record is None:
            continue
        if record.get("issue") == key["issue"] and record.get("sha") == key["sha"] \
                and not _same_action(record, key):
            mismatch = True
            continue
        if not _same_action(record, key):
            continue
        try:
            expires = dt.datetime.fromisoformat(record["expires"])
        except (KeyError, ValueError):
            continue
        if expires <= _now():
            expired = True
            continue
        try:
            os.rename(path, receipts / path.name)
        except FileNotFoundError:
            continue  # a concurrent consume spent it first
        return path.stem
    for path in receipts.glob("*.json"):
        record = _read(path)
        if record is not None and _same_action(record, key):
            raise Refused(EXIT_CONSUMED, "already consumed: that grant was spent; the operator must mint a new one")
    if expired:
        raise Refused(EXIT_EXPIRED, "expired: the matching grant's TTL has passed; the operator must mint a new one")
    if mismatch:
        raise Refused(EXIT_MISMATCH, "mismatch: a grant exists for this issue and SHA, but for a different action")
    raise Refused(EXIT_NO_GRANT, "no authorization: no grant matches this issue, SHA and action")


def version() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class _Parser(argparse.ArgumentParser):
    """Every malformed call exits with the declared refusal code, so a caller
    (and a test) can tell a refusal from any other non-zero exit."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"hrse-gate: {message}", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="hrse-gate", allow_abbrev=False)
    parser.add_argument("--version", action="store_true", help="print this file's sha256")
    verbs = parser.add_subparsers(dest="verb")
    verbs._parser_class = _Parser
    g = verbs.add_parser("grant", allow_abbrev=False)
    g.add_argument("issue")
    g.add_argument("sha")
    g.add_argument("action")
    g.add_argument("--ttl-minutes", type=int, default=DEFAULT_TTL_MINUTES)
    st = verbs.add_parser("status", allow_abbrev=False)
    st.add_argument("issue")
    st.add_argument("sha")
    st.add_argument("action")
    c = verbs.add_parser("consume", allow_abbrev=False)
    c.add_argument("issue")
    c.add_argument("sha")
    c.add_argument("action")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)  # parse_args, never parse_known_args: unknown tokens refuse
    if args.version:
        if args.verb:
            print("hrse-gate: --version takes no verb", file=sys.stderr)
            return EXIT_REFUSED
        print(version())
        return EXIT_OK
    try:
        if args.verb == "grant":
            print(grant(args.issue, args.sha, args.action, args.ttl_minutes))
        elif args.verb == "status":
            print(status(args.issue, args.sha, args.action))
        elif args.verb == "consume":
            print(f"consumed {consume(args.issue, args.sha, args.action)}")
        else:
            print("hrse-gate: a verb is required (grant | status | consume)", file=sys.stderr)
            return EXIT_REFUSED
    except Refused as exc:
        print(f"hrse-gate: {exc}", file=sys.stderr)
        return exc.code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
