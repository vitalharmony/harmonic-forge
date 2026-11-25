"""Recorded R-0352 deferrals for Lane 1's belt (harmonic-forge#1564).

`candidates/deferrals/<owner>__<repo>__<issue>.json` holds
`{repo, issue, recorded_at, source, operator_login}` for an issue filed with a
recorded R-0039 exception and no handoff. It has no age prune: an entry leaves
only when the deferral is discharged. Its writers are two:

    gh_issue.py          -- record_deferral(..., "filing"), after the R-0039
                            comment posts, for RECORDED_EXCEPTIONS only, with
                            the POST response's `user.login`
    record_deferral.py   -- record_deferral(..., "backfill"), explicit keys and
                            a required `--operator-login`

`operator_login` is the filing login: the only login whose `go` releases the
deferral. An entry without one never releases.

`watch_lane_posts.py` is its one reader (`read_deferrals`, `deferral_entry`).
`discharge_deferral` has two callers: `l1_post.py`, when a handoff posts (the
writer-side discharge), and `watch_lane_posts.py`, for a closed or missing
issue, a verified Lane 1 handoff on the thread, or a thread with no deferral
comment (the backfill fallback).

**Repo keys are canonical** (`manifest_protocol.normalize_repo`, the helper
`preclose_check.normalize_repo` wraps): `vitalharmony/HRSE` and a URL land on
one key. A repo that does not normalize raises `ManifestError`, which every
public function here catches and reports on stderr, so one malformed key
never ends a belt tick.

A core module: filing and posting are core, and core never imports an
operator-side file (harmonic-forge#1523), so the store lives here rather than
in `belt_candidates.py`, which imports `REAL_CANDIDATES_DIR` and
`TESTING_ENV` from this module (operator to core). `_candidate_path`,
`_store_lock` and `_replace` are that module's helpers, copied unchanged.

**Every mutation takes `_store_lock` of the deferrals directory**:
`record_deferral`, `discharge_deferral`. A new mutation
site must take the lock too and join this list.

**A test never writes the operator's real store** (harmonic-forge#865):
`_refuses_real_store` is `belt_candidates`' detector, applied to the same
real candidates directory. `tools/run_tests.py`'s
`redirected_belt_candidates_dir` rebinds this module's
`DEFAULT_CANDIDATES_DIR` alongside `belt_candidates`'.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))
from manifest_protocol import (  # noqa: E402
    ManifestError, normalize_repo as normalize_manifest_repo,
)

UTC = timezone.utc

#: The operator's real belt candidate store. `belt_candidates.py` imports this
#: name, so the guard's notion of "the real store" is one literal in both.
REAL_CANDIDATES_DIR = Path.home() / ".claude" / "state" / "belt" / "candidates"
#: The base every deferral function defaults to; `tools/run_tests.py`
#: redirects by rebinding this name.
DEFAULT_CANDIDATES_DIR = REAL_CANDIDATES_DIR

#: The first line `gh_issue.py` posts for an R-0039 filing-bar exception, up
#: to the exception name. Already-posted comments are the belt's anchors, so
#: the posted text must stay byte-identical.
DEFERRAL_HEADING = "## R-0039 filing-bar exception:"
#: The exceptions that name a trigger (R-0352). `parent-epic` names none.
RECORDED_EXCEPTIONS = ("blocked-on-prerequisite", "deferred-design-record")
DEFERRALS_SUBDIR = "deferrals"
#: `record_deferral.py`'s per-run key cap.
MAX_BACKFILL_KEYS = 25
#: The release grammar, stated where the operator reads it: the end of
#: `gh_issue.py`'s deferral comment, and R-0352 (harmonic-forge#1564 design
#: amendment, item 7). `watch_lane_posts.RELEASE_RE` is the matcher.
RELEASE_SENTENCE = (
    "Lane 1's belt watches one trigger: a comment whose first line is `go`, "
    "optionally followed by a group or issue token, posted by the deferral's "
    "filing login. A `lane-comment` from a `LANE=1` session clears it, and a "
    "handoff discharges it. A release worded any other way is not watched and "
    "still needs the chat relay.")

#: Set by the test runners (`tools/run_tests.py`,
#: `tools/gh/run_lane1_transport_tests.py`, `tools/gh/kill_check.py`) and
#: inherited by any writer a test spawns as a subprocess (harmonic-forge#865).
TESTING_ENV = "HARMONIC_FORGE_TESTING"


def _refuses_real_store(base: Path) -> bool:
    """Whether a mutation of `base` must be refused because this is a test
    process and `base` is the operator's real store (harmonic-forge#865).

    In-process, `unittest` being imported marks a test (no production writer
    imports it); a writer a test launched as a subprocess has its own
    `sys.modules`, so it is marked by the inherited `HARMONIC_FORGE_TESTING=1`."""
    under_test = "unittest" in sys.modules or os.environ.get(TESTING_ENV) == "1"
    if not under_test:
        return False
    try:
        return Path(base).resolve() == REAL_CANDIDATES_DIR.resolve()
    except OSError:
        return True  # cannot tell: refuse, never write the real store from a test


def _refused(action: str) -> None:
    print(f"[belt-candidates] test process: not {action} the real candidate store "
          "(pass base_dir) -- harmonic-forge#865", file=sys.stderr)


def _candidate_path(base_dir: Path, repo: str, issue: int) -> Path:
    """`<base_dir>/<owner>__<repo>__<issue>.json` (harmonic-forge#691 AC3').
    `/` cannot survive into a filename, so it is replaced rather than left to
    fail `mkdir`."""
    owner, _, name = repo.partition("/")
    if not name:
        owner, name = "unknown", repo
    safe_owner = owner.replace("/", "_") or "unknown"
    safe_name = name.replace("/", "_") or "unknown"
    return base_dir / f"{safe_owner}__{safe_name}__{issue}.json"


@contextlib.contextmanager
def _store_lock(base: Path):
    """An exclusive lock over a store directory's writers (harmonic-forge#854
    preclose): a read-and-rewrite held under it can never lose a write that
    lands between its two steps."""
    base.mkdir(parents=True, exist_ok=True)
    with open(base / ".lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _replace(path: Path, entry: dict) -> None:
    """Atomic: a temp file in the same directory, then `os.replace`, so a
    reader never observes a partially written file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(tmp, path)


def _canonical(repo: str) -> str | None:
    """`repo` as the one key every entry uses, or None (one stderr line) when
    it does not normalize. Never raises."""
    try:
        return normalize_manifest_repo(repo)
    except (ManifestError, AttributeError, TypeError) as exc:
        print(f"[belt-candidates] deferral key {repo!r} skipped: {exc}", file=sys.stderr)
        return None


def _deferrals_dir(base_dir: Path | None = None) -> Path:
    """`candidates/deferrals/`, resolved at call time from
    `DEFAULT_CANDIDATES_DIR`, so the test runners' redirect covers it. A
    separate directory because a candidate file keeps only an issue's newest
    post and is pruned at 14 days; a deferral can wait longer than that."""
    return (base_dir or DEFAULT_CANDIDATES_DIR) / DEFERRALS_SUBDIR


def _deferral_path(repo: str, issue: int, base_dir: Path | None) -> Path:
    """The one path every deferral function uses: the issue's candidate-file
    basename, one directory down."""
    return _candidate_path(_deferrals_dir(base_dir), repo, issue)


def _read_entry(path: Path) -> dict | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


def record_deferral(repo: str, issue: int, source: str, *,
                    operator_login: str | None = None,
                    base_dir: Path | None = None) -> bool:
    """Record that `repo`#`issue` carries an R-0352 deferral comment.
    `source` is `"filing"` (`gh_issue.py`) or `"backfill"`
    (`record_deferral.py`). `operator_login` is the filing login, the one
    login whose `go` releases it; an entry recorded without one never
    releases. Best-effort and non-raising, like
    `belt_candidates.record_candidate`. Returns whether the entry was
    written."""
    key = _canonical(repo)
    if key is None:
        return False
    if _refuses_real_store(base_dir or DEFAULT_CANDIDATES_DIR):
        _refused("writing")
        return False
    directory = _deferrals_dir(base_dir)
    path = _deferral_path(key, issue, base_dir)
    entry = {
        "repo": key,
        "issue": issue,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
    }
    if operator_login:
        entry["operator_login"] = operator_login
    try:
        with _store_lock(directory):
            _replace(path, entry)
    except OSError as exc:
        print(f"[belt-candidates] deferral record failed (non-fatal): {exc}",
              file=sys.stderr)
        return False
    return True


def read_deferrals(repos: Iterable[str], *,
                   base_dir: Path | None = None) -> set[tuple[str, int]]:
    """`(repo, issue)` for every recorded deferral in `repos`, each pair
    spelled as the caller spelled its repo. No age cutoff and no GitHub
    call; a malformed file or key is skipped."""
    wanted: dict[str, str] = {}
    for repo in repos:
        key = _canonical(repo)
        if key is not None:
            wanted.setdefault(key, repo)
    found: set[tuple[str, int]] = set()
    try:
        paths = sorted(_deferrals_dir(base_dir).glob("*.json"))
    except OSError:
        return found
    for path in paths:
        entry = _read_entry(path)
        try:
            repo = entry["repo"]
            issue = int(entry["issue"])
        except (TypeError, KeyError, ValueError):
            continue
        key = _canonical(repo) if isinstance(repo, str) else None
        if key in wanted:
            found.add((wanted[key], issue))
    return found


def deferral_entry(repo: str, issue: int, *,
                   base_dir: Path | None = None) -> dict | None:
    """The recorded deferral for `repo`#`issue`, or None."""
    key = _canonical(repo)
    if key is None:
        return None
    return _read_entry(_deferral_path(key, issue, base_dir))


def discharge_deferral(repo: str, issue: int, reason: str, *,
                       base_dir: Path | None = None) -> bool:
    """Remove `repo`#`issue`'s recorded deferral, with one stderr line. Not
    archived: the deferral comment on GitHub is the R-0352 record. Returns
    whether an entry was removed."""
    key = _canonical(repo)
    if key is None:
        return False
    path = _deferral_path(key, issue, base_dir)
    if not path.exists():
        return False
    if _refuses_real_store(base_dir or DEFAULT_CANDIDATES_DIR):
        _refused("discharging")
        return False
    try:
        with _store_lock(_deferrals_dir(base_dir)):
            path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        print(f"[belt-candidates] could not discharge deferral {repo}#{issue} "
              f"(non-fatal): {exc}", file=sys.stderr)
        return False
    print(f"[belt-candidates] deferral discharged {repo}#{issue}: {reason}",
          file=sys.stderr)
    return True
