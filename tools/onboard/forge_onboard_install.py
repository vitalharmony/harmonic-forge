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
no subprocess can read a TTY. Links are compared by RESOLVED path, never by
text, as `tools/gh/install_gh_shim.sh` does: the operator's own links are
spelled through `~/harmonic-forge`, an alias of the real checkout. A link
resolving elsewhere is replaced only with `--force`; a real file or
directory is never replaced. Worktrees are never forced, removed or pruned.
Shell startup files are never edited: the one `export` line is printed.

Beside `forge_onboard.py`, not in it, for R-0006, as `forge_onboard_skills.py`.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_onboard as fo  # noqa: E402
from forge_onboard import FAIL, OK, WARN, Check  # noqa: E402

#: The launchers' own rule: a `-laneN` suffix is stripped from the toplevel's
#: basename (`[[ "$base" =~ ^(.+)-lane[0-9]+$ ]]`).
_LANE_SUFFIX = re.compile(r"^(.+)-lane[0-9]+$")
WORKTREE_LANES = ("lane2", "lane3")
_CHANGED = ("linked", "replaced", "created")


class InstallError(Exception):
    """The run could not happen (exit 2)."""


def _checker():
    """`tools/lane/check_installed_lane_sources.py`, loaded by path, as
    `forge_onboard._lane_hookset_module` loads its sibling: one `LANES`."""
    spec = importlib.util.spec_from_file_location(
        "check_installed_lane_sources",
        fo._THIS_CHECKOUT / "tools" / "lane" / "check_installed_lane_sources.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=env)


def install_source(environ=None) -> Path:
    """The forge root a launcher would resolve; a linked worktree is refused,
    since links into one dangle when it is removed (`platform_source`)."""
    instance = fo.instance
    try:
        root, source = instance.resolve_forge_root(fo.platform_source(), environ)
    except fo.ManifestError as exc:
        raise InstallError(f"cannot resolve the forge root: {exc}") from None
    except ValueError as exc:
        raise InstallError(str(exc)) from None
    named = instance.named_root(str(root), source)
    proc = _git(["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], root)
    if proc.returncode == 0:
        dirs = [Path(line).resolve() for line in proc.stdout.splitlines() if line]
        if len(dirs) == 2 and dirs[0] != dirs[1]:
            raise InstallError(f"{named} is a linked git worktree; links into it would dangle "
                               f"when it is removed -- run --install from the main checkout")
    missing = [n for n in _checker().LANES if not (root / "tools" / "lane" / n).is_file()]
    if missing:
        raise InstallError(f"{named} has no tools/lane/{', tools/lane/'.join(missing)}")
    return root


def lane_worktree_paths(repo: Path) -> list[Path]:
    """`[<parent>/<base>-lane2, <parent>/<base>-lane3]`, as the launchers derive them."""
    proc = _git(["rev-parse", "--show-toplevel"], repo)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise InstallError(f"{repo} is not inside a git repository")
    toplevel = Path(proc.stdout.strip())
    match = _LANE_SUFFIX.match(toplevel.name)
    base = match.group(1) if match else toplevel.name
    return [toplevel.parent / f"{base}-{lane}" for lane in WORKTREE_LANES]


def _require_origin(repo: Path) -> None:
    if _git(["remote", "get-url", "origin"], repo).returncode != 0:
        raise InstallError(f"{repo} has no `origin` remote; lane worktrees are cut from origin/main")


def link_launchers(bin_dir: Path, source: Path, *, force: bool, dry_run: bool) -> list[Check]:
    if not dry_run:
        bin_dir.mkdir(parents=True, exist_ok=True)
    checks: list[Check] = []
    for name in _checker().LANES:
        target, src = bin_dir / name, source / "tools" / "lane" / name
        label = f"launcher {name}"
        if target.is_symlink():
            if os.path.realpath(target) == os.path.realpath(src):
                checks.append(Check(label, OK, f"already in place ({target} -> {src})"))
                continue
            was = os.readlink(target)
            if not force:
                checks.append(Check(label, FAIL, f"{target} is a symlink to {was}, not {src} -- left alone; "
                                                 f"inspect it, then re-run with --force to replace it"))
                continue
            if dry_run:
                checks.append(Check(label, OK, f"would replace {target} -> {was} with {src}"))
                continue
            tmp = bin_dir / f".{name}.forge-install"
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            tmp.symlink_to(src)
            os.replace(tmp, target)
            checks.append(Check(label, OK, f"replaced {target} (was -> {was}) with -> {src}"))
        elif target.exists():
            kind = "directory" if target.is_dir() else "real file"
            checks.append(Check(label, FAIL, f"{target} already exists as a {kind} -- refusing to replace "
                                             f"it, with or without --force; inspect it and move it aside"))
        elif dry_run:
            checks.append(Check(label, OK, f"would link {target} -> {src}"))
        else:
            target.symlink_to(src)
            checks.append(Check(label, OK, f"linked {target} -> {src}"))
    return checks


def _registered(repo: Path) -> dict[Path, bool]:
    """`{resolved worktree path: prunable}` from `git worktree list --porcelain`."""
    proc = _git(["worktree", "list", "--porcelain"], repo)
    found: dict[Path, bool] = {}
    current: Path | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):]).resolve()
            found[current] = False
        elif line.startswith("prunable") and current is not None:
            found[current] = True
    return found


def create_worktrees(repo: Path, *, dry_run: bool) -> list[Check]:
    paths = lane_worktree_paths(repo)
    _require_origin(repo)
    registered = _registered(repo)
    prune = f"git -C {repo} worktree prune"
    checks: list[Check] = []
    fetched = False
    for path in paths:
        label = f"worktree {path.name}"
        listed = path.resolve() in registered
        if path.exists():
            if listed:
                checks.append(Check(label, OK, f"already present ({path})"))
            else:
                checks.append(Check(label, FAIL, f"{path} exists but is not a worktree of {repo} -- left "
                                                 f"alone; inspect it, then remove it and re-run"))
            continue
        if listed and registered[path.resolve()]:
            checks.append(Check(label, FAIL, f"{path} is gone but still registered as a worktree of {repo} "
                                             f"-- not pruned automatically; inspect, then run: {prune}"))
            continue
        if dry_run:
            checks.append(Check(label, OK, f"would create {path} detached at origin/main"))
            continue
        if not fetched:
            proc = _git(["fetch", "-q", "origin", "main"], repo)
            if proc.returncode != 0:
                raise InstallError(f"git fetch origin main failed in {repo}: "
                                   f"{(proc.stderr or proc.stdout).strip()}")
            fetched = True
        proc = _git(["worktree", "add", "--detach", str(path), "origin/main"], repo)
        if proc.returncode == 0:
            checks.append(Check(label, OK, f"created {path} detached at origin/main"))
            continue
        detail = (proc.stderr or proc.stdout).strip()
        if path.resolve() in _registered(repo):
            detail += f"; a registration was left behind -- not pruned automatically; inspect, then run: {prune}"
        checks.append(Check(label, FAIL, detail))
    return checks


def path_line(bin_dir: Path) -> Check | None:
    want = bin_dir.resolve()
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e]
    if not any(Path(e).resolve() == want for e in entries):
        spelled = "$HOME/.local/bin" if bin_dir == Path.home() / ".local" / "bin" else str(bin_dir)
        return Check("PATH", WARN, f"{bin_dir} is not on PATH; add this line yourself: "
                                   f'export PATH="{spelled}:$PATH"  # add to your shell startup file')
    found = shutil.which("lane2")
    if found and os.path.realpath(found) != os.path.realpath(bin_dir / "lane2"):
        return Check("PATH", WARN, f"lane2 on PATH resolves to {found}, not {bin_dir / 'lane2'}; "
                                   f"an earlier PATH entry shadows {bin_dir}")
    return None


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


def install(repo: Path, bin_dir: Path, source: Path, *, force: bool = False,
            dry_run: bool = False) -> tuple[list[Check], int]:
    """Link, create, then check. Raises InstallError (exit 2) before writing
    anything when the repo is not a git repo or has no `origin`."""
    lane_worktree_paths(repo)
    _require_origin(repo)
    checks = link_launchers(bin_dir, source, force=force, dry_run=dry_run)
    checks += create_worktrees(repo, dry_run=dry_run)
    line = path_line(bin_dir)
    if line is not None:
        checks.append(line)
    checks.append(_source_check(bin_dir, source, dry_run))
    changed = sum(1 for c in checks if c.status == OK and c.detail.startswith(_CHANGED))
    failed = sum(1 for c in checks if c.status == FAIL)
    summary = (f"{changed} link(s)/worktree(s) written" if changed
               else "nothing changed" + (" (dry run)" if dry_run else ""))
    checks.append(Check("install", FAIL if failed else OK,
                        summary + (f", {failed} failing check(s)" if failed else "")))
    return checks, (1 if failed else 0)
