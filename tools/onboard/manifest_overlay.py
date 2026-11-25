"""An app repo's `.claude/forge.toml`, resolved for one checkout (harmonic-forge#1527).

**Two consumers, no others (harmonic-forge#1527 sticky-wicket R1).** The
overlay is read only by the launcher's `manifest_identity.account_for_origin(...,
checkout=)` and by `tools/lane/ci_plan.resolve_project` (protocol `*_task` keys,
in the repo's own `mise.toml` trust domain). Every gate, hook, credential slot,
gh target, belt workspace and prefix map resolves exactly as origin/main did:
`project_for_path`, `repo_for_path_or_none`, `workspace_for`, `slot_env`,
`manifest.load()` and every global lookup read `projects.toml` alone. A later
change that lets the overlay reach one of them breaches this rule; it is not
fixed by a new guard here (R6).

**Discovery: an explicit root, never a process cwd.** `project_for_checkout(root)`
is the only reader, and `root` is required. `path_project` passes it the
checkout root of the path being resolved; a path in no checkout consults no
overlay.

**Which copy.** Always the MAIN checkout's, never a linked worktree's own: a
worktree is the code under test, and its edit must not reconfigure what checks
it. The main checkout is git's own answer (`main_checkout`), never a re-derived
path, and a layout git names no main worktree for (a bare repo's worktree, a
`--separate-git-dir` repo's linked worktree) reads no overlay. Residual, stated:
the main checkout's working-tree state (its branch, any uncommitted edit) is
what is read.

**Merge rule and precedence.** `projects.toml` (the base), then the overlay:
one `[project]` table with an optional `[project.protocol]`, for exactly one
repo. For a repo the base registers, the overlay merges into a copy of the
base entry key by key, and `protocol` key by key into a copy of the base
protocol table; arrays and other nested tables are replaced whole. A repo the
base does not register is validated together with the base and returned with
`path` = the main checkout. No environment variable overrides a key.

**The overlay is untrusted third-party data** in a cloned repo, parsed only by
`tomllib` and guarded by:
- instance keys (`forge_root`, `projects_root`, `timezone`) refused: they
  belong in instance.toml, so a clone cannot redirect the install;
- machine-local keys (`path`, `worktree_dir`) and `token_exception` refused:
  they belong in `projects.toml`;
- identity guard: a registered repo's `account`, `name`, `prefix` and
  `workspace` (slot, BATCH key, belt queue) are absent or the base value exactly;
- a regular file only: a FIFO, device or directory at the path is refused
  before any read, so it can never stall the caller;
- checkout binding: an overlay naming a registered repo applies only in that
  repo's registered checkout, and a registered checkout's overlay must name it;
- origin binding, on both branches: the overlay's repo must be the main
  checkout's real origin (`git remote get-url`), whether or not `projects.toml`
  registers it, and an overlay-only repo's `account`, when given, must be its
  owner exactly;
- base clash: an overlay-only repo may not claim a `name`, `prefix` or
  `workspace` `projects.toml` already declares (any case), so it never joins a
  registered repo's BATCH key or belt queue.

**An overlay refines a base hit, never erases it**: when it is rejected,
`path_project` returns the base Project.

**An overlay-only `prefix` is display-only**: validated and exposed on the
Project, and in no prefix map. A BATCH key needs `projects.toml` plus
`rules/lane-shorthand.md`.

**Scope limit.** An overlay-only repo resolves only through this resolver's two
consumers. The lane posting entrypoints resolve identity by
repo key (`apply_project_identity`, `require_onboarded_repo`) and refuse it
until the follow-up child resolves their identity from the invoking checkout.

Protocol `*_task` keys an overlay sets run as `mise run <task>`: the same trust
domain as the repo's own `mise.toml`, read from the same main checkout. No
other key runs code.

**Imports.** This module imports `manifest` and `manifest_git` (git's own
answers about a checkout); `manifest` never imports this, so `load()` cannot
reach an overlay. Stdlib only, and no import of `tools/settings`
or `manifest_identity`: the launcher test fixture copies `tools/onboard/` alone.
"""
from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

from manifest import Project, build_projects, manifest_path, raw_entries
from manifest_git import checkout_root, main_checkout, origin_of
from manifest_protocol import ManifestError, normalize_repo

OVERLAY_RELPATH = Path(".claude") / "forge.toml"
INSTANCE_KEYS = ("forge_root", "projects_root", "timezone")
MACHINE_LOCAL_KEYS = ("path", "worktree_dir")
OPERATOR_KEYS = ("token_exception",)
IDENTITY_KEYS = ("account", "name", "prefix", "workspace")
#: What an overlay-only repo may not share with any base entry (any case).
CLAIM_KEYS = ("name", "prefix", "workspace")


def read(path: Path) -> dict | None:
    """The parsed overlay, or None when nothing is at `path`. Only a regular file
    is read: a symlink is never followed, the open never blocks, and anything
    else is refused (R3; post-verdict finding 1)."""
    if not os.path.lexists(path):
        return None
    try:
        if os.path.islink(path.parent):  # a symlinked .claude/ is refused too
            raise OSError("the overlay's directory is a symlink")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("not a regular file")
            data = handle.read()
        return tomllib.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc


def _instance_error(path: Path, key: str) -> ManifestError:
    return ManifestError(
        f"{path}: `{key}` is an instance setting; it belongs in instance.toml (the "
        f"per-user settings file tools/settings/instance.py reads), not {path}")


def _validated(raw: dict, path: Path, known: set[str]) -> dict:
    """The overlay's `[project]` table, checked against every key rule."""
    for key in raw:
        if key in INSTANCE_KEYS:
            raise _instance_error(path, key)
    extra = sorted(set(raw) - {"project"})
    if extra:
        raise ManifestError(f"{path}: unknown top-level key(s): {', '.join(extra)}")
    project = raw.get("project")
    if not isinstance(project, dict):
        raise ManifestError(f"{path}: expected one [project] table")
    for key in project:
        if key in INSTANCE_KEYS:
            raise _instance_error(path, key)
        if key in MACHINE_LOCAL_KEYS:
            raise ManifestError(f"{path}: `{key}` is machine-local and derived from the "
                                "checkout; set it in projects.toml")
        if key in OPERATOR_KEYS:
            raise ManifestError(f"{path}: `{key}` belongs in projects.toml")
    unknown = sorted(set(project) - known - {"protocol"})
    if unknown:
        raise ManifestError(f"{path}: [project] has unknown key(s): {', '.join(unknown)}")
    _require(project, path, "repo")  # the rest only for an overlay-only entry
    if "protocol" in project and not isinstance(project["protocol"], dict):
        raise ManifestError(f"{path}: [project.protocol] must be a table")
    project = dict(project)
    project["repo"] = normalize_repo(str(project["repo"]))
    return project


def _require(project: dict, path: Path, *keys: str) -> None:
    for key in keys:
        if not project.get(key):
            raise ManifestError(f"{path}: [project] is missing `{key}`")


def _base_repo(entry: object) -> str | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("repo"), str):
        return None
    try:
        return normalize_repo(entry["repo"])
    except ManifestError:
        return None  # the loader's own pass reports it


def _bind_origin(main: Path, overlay: Path, repo: str) -> None:
    """Refuse unless `main`'s real origin is `repo`."""
    try:
        origin = origin_of(main)
    except ManifestError as exc:
        raise ManifestError(f"{overlay}: {exc}") from exc
    if origin != repo:
        raise ManifestError(
            f"{overlay}: declares {repo}, but this checkout's origin is {origin}; "
            "an overlay must name its own checkout's origin")


def _resolve(root: Path, manifest: Path | None) -> Project | None:
    if not Path(root).exists():
        raise ManifestError(f"{root}: no such checkout")
    main = main_checkout(Path(root).resolve())
    target = manifest or manifest_path()
    entries = raw_entries(target)
    known = {f.name for f in Project.__dataclass_fields__.values()}
    at_main = next((i for i, e in enumerate(entries) if isinstance(e, dict) and e.get("path")
                    and Path(str(e["path"])).expanduser().resolve() == main), None)
    overlay = main / OVERLAY_RELPATH
    raw = read(overlay)
    if raw is None:
        return None if at_main is None else build_projects(entries, str(target))[at_main]
    project = _validated(raw, overlay, known)
    repo = project["repo"]
    label = f"{target} + {overlay}"
    index = next((i for i, e in enumerate(entries) if _base_repo(e) == repo), None)
    if index is not None:
        base = entries[index]
        base_path = base.get("path")
        if not base_path or Path(str(base_path)).expanduser().resolve() != main:
            raise ManifestError(
                f"{overlay}: declares {repo}, which projects.toml registers at {base_path}; "
                "an overlay applies only in that repo's own checkout")
        _bind_origin(main, overlay, repo)  # R4: a registered path may hold another clone
        for key in IDENTITY_KEYS:
            if key in project and project[key] != base.get(key):
                raise ManifestError(
                    f"{overlay}: {key} for {repo} is set in projects.toml "
                    f"({base.get(key)!r}); an overlay may not change it")
        entry = dict(base)
        for key, value in project.items():
            if key == "protocol" and isinstance(base.get("protocol"), dict):
                entry["protocol"] = {**base["protocol"], **value}
            else:
                entry[key] = value
        merged = list(entries)
        merged[index] = entry
        return build_projects(merged, label)[index]
    if at_main is not None:
        raise ManifestError(
            f"{overlay}: this checkout is registered in projects.toml as "
            f"{_base_repo(entries[at_main])}; its overlay must name that repo")
    _bind_origin(main, overlay, repo)
    owner = repo.split("/")[0]
    if "account" in project and project["account"] != owner:
        raise ManifestError(
            f"{overlay}: an overlay-only repo's account must be its owner ({owner}); "
            "register a different account in projects.toml")
    _require(project, overlay, "name", "prefix")
    for key in (k for k in CLAIM_KEYS if k in project):
        mine = str(project[key]).casefold()
        for base in (e for e in entries if isinstance(e, dict)):
            other = base.get(key)
            if isinstance(other, str) and mine == other.casefold():
                raise ManifestError(
                    f"{overlay}: {key} {project[key]!r} clashes with projects.toml's {other!r}; "
                    "an overlay-only repo may not claim a name, prefix or workspace already "
                    "registered (its prefix is display-only)")
    entry = dict(project)
    if isinstance(entry.get("protocol"), dict):
        entry["protocol"] = dict(entry["protocol"])
    entry["path"] = str(main)
    return build_projects([*entries, entry], label)[-1]


def project_for_checkout(root: Path, manifest: Path | None = None) -> Project | None:
    """The Project for the checkout at `root`, with its main checkout's overlay
    merged; None when neither `projects.toml` nor an overlay names it.

    `root` is required: no process cwd is ever consulted. Every failure is a
    `ManifestError`, so a path-keyed caller's fail-open handling covers it.
    """
    try:
        result = _resolve(root, manifest)
        if result is not None:
            result.checkout, result.worktrees  # noqa: B018 -- a bad template raises here
        return result
    except ManifestError:
        raise
    except Exception as exc:  # noqa: BLE001 -- AC10: one exception type for callers
        raise ManifestError(f"{root}: cannot resolve its project ({type(exc).__name__}: {exc})") from exc


def path_project(target: Path, base_hit: Project | None,
                 manifest: Path | None = None) -> Project | None:
    """The Project for `target` given the base scan's hit: the checkout's
    resolved Project when it names the same repo (or there is no base hit),
    else the base hit. A path in no checkout consults no overlay, and a rejected
    overlay raises only when there is no base hit to fall back to."""
    root = checkout_root(target)
    if root is None:
        return base_hit
    try:
        resolved = project_for_checkout(root, manifest)
    except ManifestError:
        if base_hit is None:
            raise
        return base_hit  # a rejected overlay never erases what projects.toml resolves
    if resolved is not None and (base_hit is None or resolved.repo == base_hit.repo):
        return resolved
    return base_hit
