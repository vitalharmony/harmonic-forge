#!/usr/bin/env python3
"""Issue-scoped command receipts for Lane 2 (harmonic-forge#371).

Records one immutable JSON receipt per consequential command a Lane 2
session runs -- argv, exit code, SHA-256 digests of stdout/stderr, bounded
previews, a UTC timestamp -- so `l2_post.py` can compose a status report
from verified facts instead of free-form assertion. A non-zero exit
additionally writes a LOCKED.json marker under the same issue scope;
`l2_post.py` refuses ordinary status composition while it is present (see
its `--resolve-lock` path). A `--kind blocked` status is not gated by
this -- see l2_post.py's own lock check, which distinguishes "the
underlying command failed" from "Lane 2 correctly reported it is
blocked."

Receipts live under `<git-dir>/lane2-receipts/<issue>/` (`git rev-parse
--git-path`) -- inside `.git/`, so never staged and never committed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

PREVIEW_BYTES = 2000

#: Three shapes of ANSI/VT100 escape sequence, in order of specificity
#: (harmonic-forge#571; preclose review found the CSI-only version misses
#: real sequences a CLI tool can still emit -- OSC hyperlinks/titles,
#: charset-select, cursor save/restore):
#:
#:   1. CSI  -- `ESC [` + parameter bytes + intermediate bytes + one final
#:      byte. Colour codes (`ESC[33m`) and cursor movement. The shape the
#:      issue's own fixture (`vite`'s reporter) uses, and the one that
#:      broke `l2_post.py`'s self-check live.
#:   2. OSC  -- `ESC ]` + text, terminated by BEL or `ESC \`. Hyperlinks
#:      (`ESC]8;;url ... BEL`) and window-title sequences.
#:   3. Everything else short-form -- `ESC` + optional intermediate bytes
#:      (0x20-0x2f) + one final byte (0x30-0x7e). Covers charset-select
#:      (`ESC(B`) and cursor save/restore (`ESC7`/`ESC8`), which are
#:      neither CSI nor OSC but are still single/short escape sequences a
#:      terminal-aware CLI can emit. Tried last so real CSI/OSC sequences
#:      match their own, more specific alternative first.
#:
#: Verified live: `npm run build` (vite's reporter) still emits colour even
#: with `NO_COLOR=1 FORCE_COLOR=0` set, so this cannot be avoided by how a
#: caller invokes its gate commands -- the escape has to be stripped here,
#: at capture, not asked away.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9:;<=>?]*[ -/]*[@-~]"       # CSI
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC, BEL- or ST-terminated
    r"|[\x20-\x2f]*[\x30-\x7e]"        # any other short escape sequence
    r")"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI escape sequences (colour codes, cursor movement).

    harmonic-forge#571 AC1: a receipt preview containing a raw ESC byte is
    what broke `l2_post.py`'s post/fetch/diff self-check -- the escape is
    mangled in transit on the send side, not stripped by GitHub (that
    hypothesis was tested live and is wrong). Stripping here, before the
    byte ever reaches a receipt or a composed comment body, is the fix that
    does not depend on every caller remembering `NO_COLOR`.
    """
    return _ANSI_ESCAPE_RE.sub("", text)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def receipt_dir(issue: int) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", f"lane2-receipts/{issue}"],
        capture_output=True, text=True, check=True,
    )
    path = Path(result.stdout.strip())
    path.mkdir(parents=True, exist_ok=True)
    return path


def lock_path(issue: int) -> Path:
    return receipt_dir(issue) / "LOCKED.json"


def is_locked(issue: int) -> bool:
    return lock_path(issue).exists()


def write_receipt(issue: int, kind: str, body: dict) -> Path:
    directory = receipt_dir(issue)
    timestamp = _now()
    payload = {"kind": kind, "timestamp": timestamp, **body}
    path = directory / f"{timestamp.replace(':', '-')}-{kind}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def write_lock(issue: int, receipt_path: Path, exit_code: int) -> Path:
    path = lock_path(issue)
    path.write_text(json.dumps({
        "receipt": receipt_path.name,
        "exit_code": exit_code,
        "timestamp": _now(),
    }, indent=2, sort_keys=True))
    return path


def clear_lock(issue: int) -> None:
    lock_path(issue).unlink(missing_ok=True)


def run_command(issue: int, argv: list[str]) -> int:
    """Run argv, record a receipt, lock the issue on non-zero exit.

    The SHA-256 digests are computed over the RAW, unstripped output -- they
    exist to prove exactly what the command produced, byte for byte. Only
    the previews (the part that ever gets embedded verbatim into a posted
    comment body via `l2_post.py`'s composed receipts JSON) are ANSI-stripped
    (harmonic-forge#571 AC1) -- stripping the digest input would make the
    digest prove something other than what actually ran.
    """
    result = subprocess.run(argv, capture_output=True, text=True)
    receipt_path = write_receipt(issue, "command", {
        "argv": argv,
        "exit_code": result.returncode,
        "stdout_sha256": _digest(result.stdout.encode()),
        "stderr_sha256": _digest(result.stderr.encode()),
        "stdout_preview": strip_ansi(result.stdout)[:PREVIEW_BYTES],
        "stderr_preview": strip_ansi(result.stderr)[:PREVIEW_BYTES],
    })
    if result.returncode != 0:
        write_lock(issue, receipt_path, result.returncode)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    print(f"[receipt] {receipt_path}", file=sys.stderr)
    return result.returncode


ISSUE_FORMS = "--issue takes N, #N, owner/repo#N or a key like H2667"
_QUALIFIED = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_KEY = re.compile(r"^([A-Za-z])(\d+)$")
_ORIGIN = re.compile(r"github\.com(?::\d+)?[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)


def _prefixes() -> dict[str, str]:
    """The house repo -> letter map (batch_auth.REPO_PREFIXES), with the same
    fallback `l1_post` uses when it cannot be imported."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
    try:
        from batch_auth import REPO_PREFIXES  # noqa: PLC0415
        return dict(REPO_PREFIXES)
    except ImportError:
        return {"vitalharmony/hrse": "H", "vitalharmony/harmonic-forge": "F",
                "vitalharmony/cymagraph-infra": "I", "vitalharmony/openclaw-projects": "O"}


def parse_issue(text: str) -> tuple[str | None, int]:
    """harmonic-forge#1343: (repo or None, number) for the forms the lanes type."""
    value = text.strip()
    if re.fullmatch(r"#?\d+", value):
        return None, int(value.lstrip("#"))
    match = _QUALIFIED.match(value)
    if match:
        return match.group(1).lower(), int(match.group(2))
    match = _KEY.match(value)
    if match:
        letter = match.group(1).upper()
        for repo, prefix in _prefixes().items():
            if prefix.upper() == letter:
                return repo.lower(), int(match.group(2))
    raise argparse.ArgumentTypeError(ISSUE_FORMS)


def origin_repo() -> str | None:
    """`owner/repo` of the current checkout's origin, lowercased, or None."""
    done = subprocess.run(["git", "remote", "get-url", "origin"],
                          capture_output=True, text=True)
    match = _ORIGIN.search(done.stdout.strip()) if done.returncode == 0 else None
    return f"{match.group(1)}/{match.group(2)}".lower() if match else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a command under a Lane 2 issue scope and record its receipt (harmonic-forge#371)."
    )
    parser.add_argument("--issue", type=parse_issue, required=True, help=ISSUE_FORMS)
    parser.add_argument("command", nargs=argparse.REMAINDER,
                         help="the command to run, after a literal '--'")
    args = parser.parse_args()
    if not args.command or args.command[0] != "--":
        parser.error("pass the command to run after a literal '--', e.g. --issue 371 -- pytest -q")
    repo, number = args.issue
    if repo is not None:
        here = origin_repo()
        if here != repo:
            # Receipts and LOCKED.json live under this checkout's git dir, so a key
            # for another repo would scope the wrong issue (harmonic-forge#1343).
            print(f"receipt_runner: --issue names {repo}, but this checkout's origin is "
                  f"{here or 'unreadable'}; run it from a {repo} checkout. Nothing written.",
                  file=sys.stderr)
            return 2
    return run_command(number, args.command[1:])


if __name__ == "__main__":
    raise SystemExit(main())
