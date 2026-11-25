"""Merge an app repo's `.claude/forge.toml` over `projects.toml` (harmonic-forge#1527).

**Precedence.** The in-forge `projects.toml` (the base), then the app repo's
`.claude/forge.toml` (the overlay). No environment variable overrides a key.

**Merge rule, per table.** The overlay is one `[project]` table with an
optional `[project.protocol]`; it describes exactly one repo. When the base
registers that repo, the overlay merges into a copy of the base entry key by
key, and its `protocol` merges key by key into a copy of the base protocol
table. Every other value, arrays (`active_milestones`) and nested tables
(`rebase_regenerate`, `rebase_visual_baselines`) included, is replaced whole.
The entry keeps its base position. When the base does not register the repo,
the overlay is appended as a new entry whose `path` is the main checkout.

**Discovery.** The path being resolved when the caller has one
(`manifest_identity.project_for_path` passes its target's checkout root), else
the process cwd's checkout root. Never a hook's assumed cwd: a hook process's
cwd is not the session's. The copy read is always the MAIN checkout's, never a
linked worktree's: a worktree is the code under test, and its own edit must
not reconfigure the hooks that check it. Residual, stated: the main
checkout's working-tree state (its branch, any uncommitted edit) is read.

**The overlay is untrusted third-party data** in a cloned repo. It is parsed
only by `tomllib`, and guarded by:
- instance keys (`forge_root`, `projects_root`, `timezone`) are refused: they
  belong in instance.toml, so a clone cannot redirect the install;
- machine-local keys (`path`, `worktree_dir`) and `token_exception` are
  refused: they belong in `projects.toml`;
- identity keys (`account`, `name`, `prefix`) of a registered repo may only
  repeat the base value exactly, so no BATCH key or prefix moves;
- checkout binding: an overlay naming a registered repo applies only in that
  repo's registered checkout;
- origin binding: an overlay-only repo must be the main checkout's real
  origin (one `git remote get-url`, the only subprocess here), and its
  `account`, when given, must be its owner exactly.

**Scope limit.** An overlay-only repo is visible only to a consumer run inside
its checkout (or asked about a path inside it); enumeration from anywhere else
(belt, sweep, BATCH from another session) does not see it.

Protocol `*_task` keys an overlay sets run as `mise run <task>`: the same trust
domain as the repo's own `mise.toml`, read from the same main checkout. No
other key runs code.

Stdlib only, and no import of `tools/settings` or `manifest_identity`: the
launcher test fixture copies `tools/onboard/` alone, and `manifest_identity`
imports `manifest`, which imports this.
"""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

from manifest_protocol import ManifestError, normalize_repo, origin_repo

OVERLAY_RELPATH = Path(".claude") / "forge.toml"
INSTANCE_KEYS = ("forge_root", "projects_root", "timezone")
MACHINE_LOCAL_KEYS = ("path", "worktree_dir")
OPERATOR_KEYS = ("token_exception",)
IDENTITY_KEYS = ("account", "name", "prefix")
#: Passed as `app_root` by a path-keyed resolver whose path is in no checkout:
#: no overlay applies, and the cwd is not consulted.
NO_OVERLAY = Path("<no overlay>")
_VERSION = re.compile(r"v(\d{4})\.(0[1-9]|1[0-2])\.(\d+)")
_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


def checkout_root(start: Path) -> Path | None:
    """The nearest of `start` (resolved) and its ancestors holding a `.git` entry."""
    here = Path(start).resolve()
    for candidate in (here, *here.parents):
        if os.path.lexists(candidate / ".git"):
            return candidate
    return None


def main_checkout(root: Path) -> Path:
    """The main checkout of a checkout root (a linked worktree maps to its main)."""
    dotgit = root / ".git"
    if dotgit.is_dir():
        return root
    try:
        text = dotgit.read_text(encoding="utf-8").strip()
    except OSError:
        return root
    if not text.startswith("gitdir:"):
        return root
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = root / gitdir
    commondir = gitdir / "commondir"
    try:
        common = commondir.read_text(encoding="utf-8").strip()
    except OSError:
        return root
    return (gitdir / common).resolve().parent


def overlay_path(app_root: Path | None) -> Path | None:
    """`<main checkout>/.claude/forge.toml`, never the worktree's own copy."""
    if app_root is NO_OVERLAY:
        return None
    root = Path(app_root).resolve() if app_root is not None else checkout_root(Path.cwd())
    if root is None:
        return None
    return main_checkout(root) / OVERLAY_RELPATH


def read(path: Path) -> dict | None:
    """The parsed overlay, or None when nothing is at `path`."""
    if not os.path.lexists(path):
        return None
    try:
        if not path.is_file():
            raise OSError("not a file")
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc


def origin_of(main: Path) -> str:
    """The normalized repo `main`'s origin names. The module's only subprocess."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV}
    try:
        result = subprocess.run(["git", "-C", str(main), "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError(f"cannot read the origin of {main}: {exc}") from exc
    url = result.stdout.strip()
    if result.returncode != 0 or not url:
        raise ManifestError(f"cannot read the origin of {main}: it has no origin remote")
    try:
        return origin_repo(url)
    except ManifestError as exc:
        raise ManifestError(f"cannot read the origin of {main}: {exc}") from exc


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
    for required in ("name", "prefix", "repo"):
        if not project.get(required):
            raise ManifestError(f"{path}: [project] is missing `{required}`")
    if "protocol" in project and not isinstance(project["protocol"], dict):
        raise ManifestError(f"{path}: [project.protocol] must be a table")
    project = dict(project)
    project["repo"] = normalize_repo(str(project["repo"]))
    return project


def _base_repo(entry: object) -> str | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("repo"), str):
        return None
    try:
        return normalize_repo(entry["repo"])
    except ManifestError:
        return None  # the loader's own pass reports it


def apply(entries: list, target: Path, known: set[str],
          app_root: Path | None) -> tuple[list, str]:
    """`(entries, label)` with the overlay merged; the caller's dicts are not mutated."""
    overlay = overlay_path(app_root)
    raw = read(overlay) if overlay is not None else None
    if raw is None:
        return entries, str(target)
    main = overlay.parent.parent
    project = _validated(raw, overlay, known)
    repo = project["repo"]
    merged = list(entries)
    index = next((i for i, e in enumerate(entries) if _base_repo(e) == repo), None)
    if index is not None:
        base = entries[index]
        base_path = base.get("path")
        if not base_path or Path(str(base_path)).expanduser().resolve() != main:
            raise ManifestError(
                f"{overlay}: declares {repo}, which projects.toml registers at {base_path}; "
                "an overlay applies only in that repo's own checkout")
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
        merged[index] = entry
    else:
        try:
            origin = origin_of(main)
        except ManifestError as exc:
            raise ManifestError(f"{overlay}: {exc}") from exc
        if origin != repo:
            raise ManifestError(
                f"{overlay}: declares {repo}, but this checkout's origin is {origin}; "
                "an overlay-only entry must name its own origin")
        owner = repo.split("/")[0]
        if "account" in project and project["account"] != owner:
            raise ManifestError(
                f"{overlay}: an overlay-only repo's account must be its owner ({owner}); "
                "register a different account in projects.toml")
        entry = dict(project)
        if isinstance(entry.get("protocol"), dict):
            entry["protocol"] = dict(entry["protocol"])
        entry["path"] = str(main)
        merged.append(entry)
    return merged, f"{target} + {overlay}"


def check_min_version(value: object, label: str, name: str) -> None:
    """`min_version` is absent or the epic's `vYYYY.MM.N`."""
    if value is None:
        return
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ManifestError(f"{label}: {name} min_version {value!r} is not vYYYY.MM.N")


def parse_version(value: str) -> tuple[int, int, int]:
    """`(year, month, n)` of a valid `vYYYY.MM.N` (for S10's `doctor`)."""
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ManifestError(f"version {value!r} is not vYYYY.MM.N")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))
