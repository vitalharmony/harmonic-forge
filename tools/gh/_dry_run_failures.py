"""A failed background `l1_post --dry-run` is recorded so the session that ran
it sees the failure on its next prompt (harmonic-forge#1544, reforged after the
pass-1 sticky-wicket verdict, issuecomment-7147532424, and the design amendment,
issuecomment-7147756741).

Identity comes from l1_post, never from argv: `main()` hands its parsed
Namespace to `bind()` on the `parse_args()` line, and `resolve_repo` /
`resolve_sha` write the resolved values back onto it. `run(main, resolve_repo)`
wraps the entry point; on a non-zero exit of a parsed `--dry-run` it records one
entry keyed by `(repo, issue, kind)`, the repo always passed through
`resolve_repo` (idempotent, and its own failure leaves the given value and the
original exit untouched). An exit before parsing (argparse, exit 2) has no
Namespace, so it records one `unparsed` entry with the stderr tail, dry run or
not: nothing was posted, so surfacing it is harmless. Every non-zero exit is
recorded with its code, `WAIT_EXIT` (3) included, shown as a wait.

Audience is the session: entries live under
`state_dir()/<sha256(session)[:32]>/`, the session being `CLAUDE_CODE_SESSION_ID`
exactly as the precheck receipt reads it (`gate_receipt.session`). The
`no-session` bucket (CI, tests, the operator's shell) is never surfaced, so
nothing is written for it. Each record is its own file, so a newer failure is
never overwritten by a stale read; the hook deletes exactly the file it showed,
and `clean()` (called by l1_post's `_dry_run_clean`, the one clear site) deletes
every file for its key in its own session. A state error never changes an exit.
"""
from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import pwd
import sys
import time
from pathlib import Path

WAIT_EXIT = 3  # _carry_forward.WAIT_EXIT: a wait for CI, not a refusal
NO_SESSION = "no-session"  # gate_receipt.NO_SESSION
UNPARSED = "unparsed"
_TAIL_BYTES = 2048

_bound = None
_started: int | None = None


def state_dir() -> Path:
    return Path(os.environ.get("HOME", str(Path.home()))) / ".claude" / "state" / "l1post" / "dry-run-failed"


#: The account's real store, from the password database so it does not move
#: with `$HOME` (harmonic-forge#1555; precedent tools/runner/sandbox.py).
REAL_STATE_DIR = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude" / "state" / "l1post" / "dry-run-failed"
#: Set by the test runners and inherited by a writer a test spawns.
TESTING_ENV = "HARMONIC_FORGE_TESTING"


def _refuses_real_store() -> bool:
    """harmonic-forge#865's detector (tools/gh/belt_candidates.py
    `_refuses_real_store`), reused for this store (harmonic-forge#1555): a test
    process -- `unittest` imported in-process, or `HARMONIC_FORGE_TESTING=1`
    inherited by a spawned writer -- never writes the operator's real store."""
    under_test = "unittest" in sys.modules or os.environ.get(TESTING_ENV) == "1"
    if not under_test:
        return False
    try:
        return state_dir().resolve() == REAL_STATE_DIR.resolve()
    except OSError:
        return True  # cannot tell: refuse, never write the real store from a test


def session() -> str:
    """The same value `gate_receipt.session()` reads."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip() or NO_SESSION


def session_dir(sid: str) -> Path:
    return state_dir() / hashlib.sha256(sid.encode()).hexdigest()[:32]


def _slug(repo: str, issue, kind: str) -> str:
    return f"{repo.replace('/', '_')}#{issue}-{kind}"


def bind(args):
    """Keep l1_post's parsed Namespace; return it unchanged."""
    global _bound
    _bound = args
    return args


def record(repo: str, issue, kind: str, sha: str, text: str, sid: str | None = None,
           code: int | None = None) -> None:
    _write(_slug(repo, issue, kind), {"repo": repo, "issue": str(issue), "kind": kind,
                                       "sha": sha, "text": text, "exit": code}, sid)


def _write(slug: str, data: dict, sid: str | None) -> None:
    sid = sid or session()
    if sid == NO_SESSION:
        return
    if _refuses_real_store():  # harmonic-forge#1555 AC1
        return
    folder = session_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    # Stamped with the RUN's start, so "newer" means started later
    # (epoch-1 pass 2 survivor); unique per record (stamp plus pid).
    started = _started if _started is not None else time.time_ns()
    path = folder / f"{slug}.{started}{os.getpid():07d}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({**data, "at": int(time.time())}))
    os.replace(tmp, path)


def _files(folder: Path, slug: str) -> list[Path]:
    return sorted(folder.glob(f"{glob.escape(slug)}.*.json"))


def _stamp(path: Path) -> int | None:
    """The recording run's start (ns), from `<slug>.<start ns><pid:07d>.json`."""
    try:
        tail = path.name[:-len(".json")].rsplit(".", 1)[1]
        return int(tail[:-7]) if len(tail) > 7 else None
    except (IndexError, ValueError):
        return None


def _watermark(folder: Path, slug: str) -> int:
    """The start (ns) of the latest clean dry run for this key, or 0."""
    try:
        return int((folder / f"{slug}.clean").read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def clean(kind: str, repo: str, issue, sha: str) -> None:
    """l1_post's clean-dry-run line, then clear this session's entries for the key
    that were recorded before this run started: a newer failure from a concurrent
    dry run is never erased by an older clean one (epoch-1 pass 1 survivor)."""
    print(f"[l1-post] dry run clean: {kind} {repo}#{issue} @ {sha[:12]}")
    try:
        sid = session()
        if sid == NO_SESSION:
            return
        if _refuses_real_store():  # harmonic-forge#1555 AC1: no test watermark
            return
        cutoff = _started if _started is not None else time.time_ns()
        folder, slug = session_dir(sid), _slug(repo, issue, kind)
        # The watermark makes "the latest-started run wins" hold whichever order
        # a failure and a clean land in (Thomas write rule; epoch-1 two-pass
        # PATCH issuecomment-7148701001): pending() hides an entry from a run
        # that started before the latest clean run.
        if cutoff > _watermark(folder, slug):
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f"{slug}.clean.tmp"
            tmp.write_text(str(cutoff))
            os.replace(tmp, folder / f"{slug}.clean")
        for path in _files(folder, slug):
            stamp = _stamp(path)
            if stamp is not None and stamp <= cutoff:
                path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - clearing never changes the post
        pass


def pending(sid: str) -> list[tuple[Path, dict]]:
    """This session's entries, oldest first; a malformed entry is skipped."""
    out: list[tuple[Path, dict]] = []
    try:
        paths = sorted(session_dir(sid).glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not (isinstance(data, dict) and data.get("text")):
            continue
        stamp = _stamp(path)
        slug = path.name[:-len(".json")].rsplit(".", 1)[0]
        if stamp is not None and stamp < _watermark(path.parent, slug):
            continue  # a later-started clean dry run superseded it
        out.append((path, data))
    return out


def consume(path: Path) -> None:
    path.unlink(missing_ok=True)


class _Tee(io.TextIOBase):
    """Passes writes through to stderr and keeps the last _TAIL_BYTES."""

    def __init__(self, real):
        self._real, self._buf = real, ""

    def write(self, s: str) -> int:
        self._buf = (self._buf + s)[-_TAIL_BYTES:]
        return self._real.write(s)

    def flush(self) -> None:
        self._real.flush()

    @property
    def tail(self) -> str:
        return self._buf


def run(main, resolve_repo=None) -> None:
    """Call `main()`; record a failed parsed dry run or a pre-parse exit, then re-raise."""
    global _bound, _started
    _bound = None  # a stale binding from an earlier in-process call is never this run's
    _started = time.time_ns()
    tee = _Tee(sys.stderr)
    sys.stderr = tee
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        # Every non-zero exit is recorded, WAIT_EXIT (3) included: the precheck
        # gate refuses a real post after anything but a clean dry run, so a wait
        # is as much an unread blocker as a refusal, and _carry_forward also
        # exits 3 on a read or fetch error (epoch-1 pass 1 cluster, PATCH).
        if code not in (None, 0):
            text = code if isinstance(code, str) else (tee.tail.strip() or f"exit {code}")
            _safe_record(str(text), resolve_repo, code if isinstance(code, int) else 1)
        raise
    except BaseException as exc:  # a traceback is a failed run too
        _safe_record(f"{type(exc).__name__}: {exc}", resolve_repo, 1)
        raise
    finally:
        sys.stderr = tee._real
        _started = None  # a clean() outside a run cuts off at its own call time


def _safe_record(text: str, resolve_repo, code: int = 1) -> None:
    try:
        args = _bound
        if args is None:
            _write(UNPARSED, {"repo": "", "issue": "", "kind": UNPARSED, "sha": "",
                              "text": text.strip(), "exit": code}, None)
            return
        if not getattr(args, "dry_run", False):
            return
        repo = args.repo
        if resolve_repo is not None:
            try:
                repo = resolve_repo(args.repo)
            except BaseException:  # noqa: BLE001 - fail() raises SystemExit; keep the given value
                pass
        record(repo or "", args.issue, args.kind, str(args.sha or ""), text.strip(), code=code)
    except Exception:  # noqa: BLE001 - recording never changes the exit
        pass
