#!/usr/bin/env python3
"""`forge_onboard.py --install` (harmonic-forge#1532).

Links every launcher `check_installed_lane_sources.LANES` names from the bin
dir (`~/.local/bin`) into the forge checkout's `tools/lane/`, and creates the
repo's `<base>-lane2` and `<base>-lane3` worktrees exactly as the launchers
derive them (tools/lane/lane2:26-35, tools/lane/lane3:30-39). All five
launchers are linked, not three (operator ruling D55): the forge's own
`mise run check` runs the source checker with `--skip-if-absent`, which
passes only when all five are absent, so a partial install leaves it red.

Idempotent and non-interactive: a second run changes nothing and says so, and
no subprocess can read a TTY or raise an askpass prompt. Links are compared
by RESOLVED path, never by text, as `tools/gh/install_gh_shim.sh` does: the
operator's own links are spelled through `~/harmonic-forge`, an alias of the
real checkout. A link resolving elsewhere is replaced only with `--force`; a
real file or directory is never replaced. Worktrees are never forced,
removed or pruned. Shell startup files are never edited: the one `export`
line is printed.

Two phases, one boundary (harmonic-forge#1532 pass-2 amendment):

1. Preflight writes no link or worktree, and every failure in it is
   `InstallError`, exit 2: git's version, the forge root, the worktree pair,
   `origin`, the worktree listing, every per-worktree probe, and origin's
   main itself whenever a worktree needs creating. A real run's fetch does
   write objects and FETCH_HEAD; a dry run never fetches.
2. The write phase raises nothing. Each item (one launcher, one worktree) is
   wrapped whole, its reads as well as its writes, the same in a dry run as in
   a real one: an `OSError` or a failed git result becomes a FAIL Check on
   that item, the loop goes on, and the run exits 1 with every Check printed.

`_git` never raises: an `OSError` launching git comes back as a failed result
(returncode 127), and each caller maps it by phase.

The worktree is cut at exactly the SHA preflight proved (harmonic-forge#1532
two-pass PATCH, P1), as `tools/lane/_lane_refresh.sh` does (NC2,
harmonic-forge#322): `git ls-remote` names it, `git fetch` brings it in,
`git cat-file -e` proves it is here, and `git worktree add --detach <path>
<sha>` uses it. The local `refs/remotes/origin/main` is never read: it can be
stale (exit 0 at the wrong commit) or absent (a single-branch clone).

Worktree classification lives in `worktree_table.py` (harmonic-forge#1532
reforge, operator D86): a strict parser over the listing's bytes and one
ordered outcome table. This module reads only its typed `Outcome`.

Beside `forge_onboard.py`, not in it, for R-0006, as `forge_onboard_skills.py`.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_onboard as fo  # noqa: E402
from forge_onboard import FAIL, OK, WARN, Check  # noqa: E402
import worktree_table  # noqa: E402

#: The launchers' own rule: a `-laneN` suffix is stripped from the toplevel's
#: basename (`[[ "$base" =~ ^(.+)-lane[0-9]+$ ]]`). Bash's ERE `.` matches a
#: newline and its `$` is end of string only, hence DOTALL and fullmatch.
_LANE_SUFFIX = re.compile(r"(.+)-lane[0-9]+", re.DOTALL)
WORKTREE_LANES = ("lane2", "lane3")
_CHANGED = ("linked", "replaced", "created")
#: `_git`'s returncode when git could not be launched at all.
GIT_LAUNCH_FAILED = 127
#: No prompt of any kind (gitcredentials(7)): `GIT_TERMINAL_PROMPT` gates only
#: the terminal step, so askpass is disabled outright, ssh never uses one, and
#: Git Credential Manager is non-interactive. `GIT_SSH_COMMAND` is deliberately
#: not set: it would override an adopter's `core.sshCommand`.
#: `git worktree list --porcelain -z` (git 2.36).
MIN_GIT = (2, 36)
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
NON_INTERACTIVE_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false",
                       "SSH_ASKPASS_REQUIRE": "never", "GCM_INTERACTIVE": "false"}


class InstallError(Exception):
    """The run could not happen (exit 2). Raised only in preflight."""


def _checker():
    """`tools/lane/check_installed_lane_sources.py`, loaded by path, as
    `forge_onboard._lane_hookset_module` loads its sibling: one `LANES`."""
    spec = importlib.util.spec_from_file_location(
        "check_installed_lane_sources",
        fo._THIS_CHECKOUT / "tools" / "lane" / "check_installed_lane_sources.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_raw(args: list[str], cwd: Path | None) -> subprocess.CompletedProcess:
    """Never raises; stdout and stderr are BYTES, exactly as git wrote them
    (harmonic-forge#1532 reforge, S2). `start_new_session` detaches git (and
    any ssh it runs) from the controlling terminal, which `stdin=DEVNULL`
    alone does not. `cwd=None` passes no `-C` (`git version`)."""
    cmd = ["git", *(["-C", str(cwd)] if cwd is not None else []), *args]
    try:
        return subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                              env={**os.environ, **NON_INTERACTIVE_ENV}, start_new_session=True)
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, GIT_LAUNCH_FAILED, b"", f"cannot run git: {exc}".encode())


def _git(args: list[str], cwd: Path | None) -> subprocess.CompletedProcess:
    """`_git_raw`, decoded with no newline translation (S3): `text=True`
    would turn a CR in a path into an LF."""
    proc = _git_raw(args, cwd)
    return subprocess.CompletedProcess(proc.args, proc.returncode,
                                       proc.stdout.decode("utf-8", "surrogateescape"),
                                       proc.stderr.decode("utf-8", "surrogateescape"))


def _why(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"


def _lstat(path: Path) -> os.stat_result | None:
    """None when nothing is there. Any other OSError (EACCES under a mode-000
    parent) propagates to the item's wrap: `Path.exists` would hide it on
    some Pythons and raise it on others."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


# --- Preflight: no writes; every failure is InstallError --------------------

def require_git_version() -> None:
    """git 2.36+, for `worktree list --porcelain -z`; fails closed."""
    proc = _git(["version"], None)  # no -C: a deleted cwd must not matter
    match = re.match(r"git version (\d+)\.(\d+)", proc.stdout) if proc.returncode == 0 else None
    if match is None:
        raise InstallError(f"cannot tell git's version ({_why(proc)})")
    if (int(match.group(1)), int(match.group(2))) < MIN_GIT:
        raise InstallError(f"git {match.group(1)}.{match.group(2)} is too old; --install needs git "
                           f"{MIN_GIT[0]}.{MIN_GIT[1]} or later (`git worktree list --porcelain -z`)")


def install_source(environ=None) -> Path:
    """The forge root a launcher would resolve; a linked worktree is refused,
    since links into one dangle when it is removed (`platform_source`). A git
    error in that probe refuses too: it fails closed."""
    instance = fo.instance
    try:
        root, source = instance.resolve_forge_root(fo.platform_source(), environ)
    except fo.ManifestError as exc:
        raise InstallError(f"cannot resolve the forge root: {exc}") from None
    except ValueError as exc:
        raise InstallError(str(exc)) from None
    named = instance.named_root(str(root), source)
    proc = _git(["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], root)
    if proc.returncode != 0:
        raise InstallError(f"cannot tell whether {named} is a linked git worktree "
                           f"(git rev-parse failed: {_why(proc)}) -- refusing to link into it")
    dirs = [Path(line).resolve() for line in proc.stdout.splitlines() if line]
    if len(dirs) != 2:
        raise InstallError(f"cannot tell whether {named} is a linked git worktree "
                           f"(git rev-parse printed {proc.stdout!r}) -- refusing to link into it")
    if dirs[0] != dirs[1]:
        raise InstallError(f"{named} is a linked git worktree; links into it would dangle "
                           f"when it is removed -- run --install from the main checkout")
    try:
        missing = [n for n in _checker().LANES if not (root / "tools" / "lane" / n).is_file()]
    except OSError as exc:
        raise InstallError(f"cannot read {named}/tools/lane: {exc}") from None
    if missing:
        raise InstallError(f"{named} has no tools/lane/{', tools/lane/'.join(missing)}")
    return root


def lane_worktree_paths(repo: Path) -> list[Path]:
    """`[<parent>/<base>-lane2, <parent>/<base>-lane3]`, as the launchers derive
    them. Only trailing newlines are removed, as bash's `$(...)` does: a
    checkout name ending in a space keeps it. Read as bytes and decoded as
    strict UTF-8 (harmonic-forge#1532 reforge, pitch change 8): a lone
    surrogate in a lane path would raise when a Check is printed after
    writes, so a non-UTF-8 toplevel is refused here, in preflight."""
    proc = _git_raw(["rev-parse", "--show-toplevel"], repo)
    if proc.returncode != 0 or not proc.stdout.strip():
        why = proc.stderr.decode("utf-8", "replace").strip()
        raise InstallError(f"{repo} is not inside a git repository"
                           + (f" ({why})" if proc.returncode == GIT_LAUNCH_FAILED else ""))
    raw = proc.stdout.rstrip(b"\n")
    try:
        toplevel = Path(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise InstallError(f"{repo}'s toplevel {raw!r} is not UTF-8; --install "
                           f"cannot represent its lane paths -- rename it; nothing was written") from None
    match = _LANE_SUFFIX.fullmatch(toplevel.name)
    base = match.group(1) if match else toplevel.name
    return [toplevel.parent / f"{base}-{lane}" for lane in WORKTREE_LANES]


def _require_origin(repo: Path) -> None:
    proc = _git(["remote", "get-url", "origin"], repo)
    if proc.returncode == GIT_LAUNCH_FAILED:
        raise InstallError(f"cannot read {repo}'s `origin` remote: {_why(proc)}")
    if proc.returncode != 0:
        raise InstallError(f"{repo} has no `origin` remote; lane worktrees are cut from origin/main")


def plan_worktrees(repo: Path) -> list[tuple[Path, Check | None]]:
    """Every worktree check, before the first write. A repo-level failure is
    InstallError; a per-item failure (or an unreadable item) is a FAIL Check."""
    paths = lane_worktree_paths(repo)
    _require_origin(repo)
    try:
        records = worktree_table.read_listing(repo, _git_raw)
    except worktree_table.ListingError as exc:
        raise InstallError(f"git worktree list failed in {repo}: {exc} -- nothing was written") from None
    try:
        common = worktree_table.common_dir(repo, _git_raw)
    except worktree_table.ListingError as exc:
        raise InstallError(f"{exc} -- nothing was written") from None
    plan: list[tuple[Path, Check | None]] = []
    for path in paths:
        try:
            outcome = worktree_table.classify(path, repo, common, records, _git_raw)
        except (OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop before Python 3.13
            plan.append((path, Check(f"worktree {path.name}", FAIL, f"cannot inspect {path}: {exc}")))
            continue
        plan.append((path, _outcome_check(path, outcome)))
    return plan


def _outcome_check(path: Path, outcome: worktree_table.Outcome) -> Check | None:
    """The only reading of `worktree_table`'s typed result: `main` is
    InstallError (the run's base name is wrong for both paths, so nothing is
    written), `create` is None (owed), `present` OK, `fail` FAIL."""
    label = f"worktree {path.name}"
    if outcome.kind == worktree_table.MAIN:
        raise InstallError(outcome.detail)
    if outcome.kind == worktree_table.CREATE:
        return None
    if outcome.kind == worktree_table.PRESENT:
        return Check(label, OK, outcome.detail)
    return Check(label, FAIL, f"{outcome.detail} [row {outcome.row}]")


def _reach_origin_main(repo: Path, *, dry_run: bool) -> str:
    """Before any write, the SHA the worktrees are cut at: origin's
    `refs/heads/main` as `git ls-remote` reports it (exact ref name, one
    line, a full SHA). A real run then fetches it and proves the commit is
    here; a dry run never fetches. If origin is force-pushed between the two,
    `cat-file -e` fails (InstallError); if it is fast-forwarded, the old
    commit still arrives as an ancestor and is the one used."""
    def fail(why: str) -> InstallError:
        return InstallError(f"{why} in {repo}; lane worktrees are cut from origin/main -- nothing was written")

    proc = _git(["ls-remote", "--exit-code", "origin", "refs/heads/main"], repo)
    if proc.returncode == 2:
        raise fail("git ls-remote: origin has no main branch")
    if proc.returncode != 0:
        raise fail(f"git ls-remote --exit-code origin refs/heads/main failed: {_why(proc)}")
    exact = [line.split("\t", 1)[0] for line in proc.stdout.splitlines()
             if line.split("\t", 1)[1:] == ["refs/heads/main"]]
    if not exact:
        raise fail("git ls-remote: origin has no main branch")
    if len(exact) != 1 or not _SHA.match(exact[0]):
        raise fail(f"git ls-remote printed {proc.stdout!r} for refs/heads/main")
    sha = exact[0]
    if dry_run:
        return sha
    proc = _git(["fetch", "-q", "origin", "main"], repo)
    if proc.returncode != 0:
        raise fail(f"git fetch origin main failed: {_why(proc)}")
    proc = _git(["cat-file", "-e", f"{sha}^{{commit}}"], repo)
    if proc.returncode != 0:
        raise fail(f"origin/main {sha} is not here after git fetch origin main ({_why(proc)})")
    return sha


# --- Write phase: nothing below raises -------------------------------------

def _inspect_link(target: Path, src: Path, label: str, *, force: bool,
                  dry_run: bool) -> tuple[Check | None, str]:
    """Reads only. `(settled Check, "")`, or `(None, was)` for a write still
    owed: `was` is the old link text for a replace, "" for a fresh link."""
    st = _lstat(target)
    if st is not None and stat.S_ISLNK(st.st_mode):
        if os.path.realpath(target) == os.path.realpath(src):
            return Check(label, OK, f"already in place ({target} -> {src})"), ""
        was = os.readlink(target)
        if not force:
            return Check(label, FAIL, f"{target} is a symlink to {was}, not {src} -- left alone; "
                                      f"inspect it, then re-run with --force to replace it"), ""
        if dry_run:
            return Check(label, OK, f"would replace {target} -> {was} with {src}"), ""
        return None, was
    if st is not None:
        kind = "directory" if stat.S_ISDIR(st.st_mode) else "real file"
        return Check(label, FAIL, f"{target} already exists as a {kind} -- refusing to replace "
                                  f"it, with or without --force; inspect it and move it aside"), ""
    if dry_run:
        return Check(label, OK, f"would link {target} -> {src}"), ""
    return None, ""


def _write_link(bin_dir: Path, name: str, target: Path, src: Path, was: str) -> str:
    if not was:
        target.symlink_to(src)
        return f"linked {target} -> {src}"
    tmp = bin_dir / f".{name}.forge-install"
    if _lstat(tmp) is not None:
        tmp.unlink()
    tmp.symlink_to(src)
    os.replace(tmp, target)
    return f"replaced {target} (was -> {was}) with -> {src}"


def _named(exc: BaseException) -> str:
    """An exception as the write-phase backstop reports it: its type, then its text."""
    return f"{type(exc).__name__}: {exc}"


def link_launchers(bin_dir: Path, source: Path, *, force: bool, dry_run: bool) -> list[Check]:
    """Every launcher is inspected before the bin dir is made; then each owed
    write runs. Each item is wrapped whole: any Exception is a FAIL on it."""
    plan: list[tuple[str, str, Path, Path, Check | None, str]] = []
    for name in _checker().LANES:
        target, src = bin_dir / name, source / "tools" / "lane" / name
        label = f"launcher {name}"
        try:
            done, was = _inspect_link(target, src, label, force=force, dry_run=dry_run)
        except Exception as exc:  # the write-phase backstop: never BaseException
            done, was = Check(label, FAIL, f"cannot inspect {target}: {_named(exc)}"), ""
        plan.append((name, label, target, src, done, was))
    mkdir_error: Exception | None = None
    if any(item[4] is None for item in plan):
        try:
            bin_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            mkdir_error = exc
    checks: list[Check] = []
    for name, label, target, src, done, was in plan:
        if done is not None:
            checks.append(done)
        elif mkdir_error is not None:
            checks.append(Check(label, FAIL, f"cannot create {bin_dir}: {_named(mkdir_error)} -- {target} not linked"))
        else:
            try:
                checks.append(Check(label, OK, _write_link(bin_dir, name, target, src, was)))
            except Exception as exc:
                checks.append(Check(label, FAIL, f"linking {target} -> {src} failed: {_named(exc)}"))
    return checks


def create_worktrees(repo: Path, plan: list[tuple[Path, Check | None]], sha: str | None, *,
                     dry_run: bool) -> list[Check]:
    """Runs the plan `plan_worktrees` made, at `sha` (the one
    `_reach_origin_main` proved; None only when nothing is to be created). A
    failed `git worktree add` (or a failed re-listing after it) is a FAIL
    Check, never an exception."""
    prune = f"git -C {repo} worktree prune"
    checks: list[Check] = []
    for path, done in plan:
        label = f"worktree {path.name}"
        try:
            if done is not None:
                checks.append(done)
                continue
            if sha is None:
                checks.append(Check(label, FAIL, f"no origin/main SHA was proved -- {path} not created"))
                continue
            if dry_run:
                checks.append(Check(label, OK, f"would create {path} at {sha[:12]} (origin/main)"))
                continue
            proc = _git(["worktree", "add", "--detach", str(path), sha], repo)
            if proc.returncode == 0:
                checks.append(Check(label, OK, f"created {path} detached at {sha[:12]} (origin/main)"))
                continue
            detail = _why(proc)
            try:
                after = worktree_table.read_listing(repo, _git_raw)
            except worktree_table.ListingError as exc:
                after = None
                detail += f"; could not re-list worktrees ({exc}) -- inspect: git -C {repo} worktree list"
            if after is not None and worktree_table.listed(path, after):
                detail += f"; a registration was left behind -- not pruned automatically; inspect, then run: {prune}"
            checks.append(Check(label, FAIL, detail))
        except Exception as exc:  # the write-phase backstop: never BaseException
            checks.append(Check(label, FAIL, f"creating {path} failed: {_named(exc)}"))
    return checks


def path_lines(bin_dir: Path) -> list[Check]:
    """The `export` line when the bin dir is off PATH; otherwise one WARN
    naming every launcher an earlier PATH entry shadows."""
    want = bin_dir.resolve()
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e]
    if not any(Path(e).resolve() == want for e in entries):
        spelled = "$HOME/.local/bin" if bin_dir == Path.home() / ".local" / "bin" else str(bin_dir)
        return [Check("PATH", WARN, f"{bin_dir} is not on PATH; add this line yourself: "
                                    f'export PATH="{spelled}:$PATH"  # add to your shell startup file')]
    shadowed = []
    for name in _checker().LANES:
        found = shutil.which(name)
        if found and os.path.realpath(found) != os.path.realpath(bin_dir / name):
            shadowed.append(f"{name} on PATH resolves to {found}, not {bin_dir / name}")
    if shadowed:
        return [Check("PATH", WARN, "; ".join(shadowed) + f"; an earlier PATH entry shadows {bin_dir}")]
    return []


def _source_check(bin_dir: Path, source: Path, dry_run: bool) -> Check:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = _checker().check(expected_root=source / "tools" / "lane", bin_dir=bin_dir)
    detail = " / ".join(line.removeprefix("[lane-source] ")
                        for line in (err.getvalue() or out.getvalue()).splitlines()[-5:])
    if code == 0:
        return Check("launcher source check", OK, detail)
    # Read-only under --dry-run: links not yet written are expected to be missing.
    return Check("launcher source check", WARN if dry_run else FAIL,
                 ("(dry run: nothing was linked) " if dry_run else "") + detail)


def install(repo: Path | None, bin_dir: Path | None = None, *, force: bool = False,
            dry_run: bool = False) -> tuple[list[Check], int]:
    """Preflight (InstallError, exit 2, nothing written), then link, create
    and check, where every failure is a printed FAIL Check (exit 1).

    The phase decides the channel (harmonic-forge#1532 epoch-1 sticky-wicket
    PATCH): any Exception in preflight becomes InstallError naming its type;
    any Exception in the write phase becomes a FAIL Check on its item or step,
    and the Checks already collected are kept. `repo=None` means the cwd;
    `bin_dir=None` means ~/.local/bin (`Path.home()` can raise: preflight)."""
    try:
        if bin_dir is None:
            bin_dir = Path.home() / ".local" / "bin"
        if repo is None:
            try:
                repo = Path.cwd()
            except FileNotFoundError:
                raise InstallError("the current directory no longer exists; pass --repo "
                                   "-- nothing was written") from None
        source = install_source()
        require_git_version()
        worktrees = plan_worktrees(repo)
        sha = _reach_origin_main(repo, dry_run=dry_run) if any(done is None for _, done in worktrees) else None
    except InstallError:
        raise
    except Exception as exc:  # the preflight backstop: never BaseException
        raise InstallError(f"{_named(exc)} -- nothing was written") from exc
    # Write phase: nothing below raises.
    checks: list[Check] = []
    try:
        checks += link_launchers(bin_dir, source, force=force, dry_run=dry_run)
    except Exception as exc:
        checks.append(Check("launchers", FAIL, f"could not run: {_named(exc)}"))
    try:
        checks += create_worktrees(repo, worktrees, sha, dry_run=dry_run)
    except Exception as exc:
        checks.append(Check("worktrees", FAIL, f"could not run: {_named(exc)}"))
    try:
        checks += path_lines(bin_dir)
    except Exception as exc:  # advisory whatever the type (a pre-3.13 symlink loop is RuntimeError)
        checks.append(Check("PATH", WARN, f"could not probe PATH: {_named(exc)}"))
    try:
        checks.append(_source_check(bin_dir, source, dry_run))
    except Exception as exc:
        checks.append(Check("launcher source check", FAIL, f"could not run: {_named(exc)}"))
    changed = sum(1 for c in checks if c.status == OK and c.detail.startswith(_CHANGED))
    failed = sum(1 for c in checks if c.status == FAIL)
    summary = (f"{changed} link(s)/worktree(s) written" if changed
               else "nothing changed" + (" (dry run)" if dry_run else ""))
    checks.append(Check("install", FAIL if failed else OK,
                        summary + (f", {failed} failing check(s)" if failed else "")))
    return checks, (1 if failed else 0)


#: The write phase. None of these may raise InstallError or call a preflight
#: function; `test_write_phase_never_raises_install_error` enforces it.
WRITE_PHASE = (_inspect_link, _write_link, link_launchers, create_worktrees, path_lines, _source_check)
PREFLIGHT = (require_git_version, install_source, lane_worktree_paths, _require_origin,
             _outcome_check, plan_worktrees, _reach_origin_main)
