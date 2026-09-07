#!/usr/bin/env python3
"""Read `projects.toml`, the onboarded-repo manifest (harmonic-forge#498).

**Why this exists.** The same list of repos was kept in five places, each with
its own copy: `gh_issue.py`'s `REPO_BOARDS`, `repo_hygiene.py`'s `_REPO_BOARDS`
(a verbatim duplicate whose own comment said so), the prefix table in
`rules/lane-shorthand.md`, the sweep's default repo list, and the per-lane
worktree procedure. Duplication is the only source of drift, and drift here is
silent -- a repo missing from one copy still files issues, just onto the wrong
board, which is exactly the failure harmonic-forge#107 fixed once already.

**Stdlib only, no install step.** `tomllib` has been in the standard library
since 3.11 and every tool under `tools/` is dependency-free by rule, so this
adds nothing to install.

**Fails loudly on a malformed manifest.** A loader that returned an empty list
when it could not parse the file would make every consumer silently behave as
if no repo were onboarded: `gh_issue.py` would skip board-add (which it treats
as a legitimate no-op, not an error), and the hygiene sweep would audit nothing
and report clean. Both would look like success.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ManifestError(RuntimeError):
    """The manifest is missing, unparseable, or internally inconsistent."""


def manifest_path() -> Path:
    """The manifest, resolved relative to this module.

    Module-relative, never `Path.home()`. harmonic-forge#500 shipped a check
    that resolved a corpus through `Path.home()`; on CI those paths do not
    exist, so the validation became a silent no-op that still exited 0.
    """
    override = os.environ.get("FORGE_PROJECTS_MANIFEST")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "projects.toml"


@dataclass(frozen=True)
class Project:
    name: str
    prefix: str
    account: str | None = None
    repo: str | None = None
    path: str | None = None
    worktree_dir: str | None = None
    board_owner: str | None = None
    board_number: str | None = None
    milestones: bool = False
    onboarded: bool = False

    @property
    def checkout(self) -> Path | None:
        """The local checkout as an absolute path, or None when not declared.

        Existence is NOT checked here. Whether a declared checkout is actually
        present is a finding `forge-onboard --verify` reports, not a reason for
        the loader to drop the row -- dropping it would make a missing checkout
        indistinguishable from a repo nobody onboarded.
        """
        return Path(self.path).expanduser() if self.path else None

    @property
    def worktrees(self) -> list[Path]:
        """`<name>-lane2/` and `<name>-lane3/`, wherever this repo keeps them.

        Named from the CHECKOUT DIRECTORY, not the manifest `name`: the
        `lane2`/`lane3` launchers resolve by path, and HRSE2's directory is
        `HRSE2` while its manifest name is `hrse`.
        """
        if self.checkout is None:
            return []
        parent = (Path(self.worktree_dir).expanduser() if self.worktree_dir
                  else self.checkout.parent)
        return [parent / f"{self.checkout.name}-lane{n}" for n in (2, 3)]

    @property
    def board(self) -> tuple[str, str] | None:
        if self.board_owner and self.board_number:
            return (self.board_owner, self.board_number)
        return None


def load(path: Path | None = None) -> list[Project]:
    """Every entry in declaration order, validated."""
    target = path or manifest_path()
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"no manifest at {target}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"cannot read {target}: {exc}") from exc

    entries = raw.get("project")
    if not isinstance(entries, list) or not entries:
        raise ManifestError(f"{target} declares no [[project]] entries")

    projects: list[Project] = []
    known = {f.name for f in Project.__dataclass_fields__.values()}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ManifestError(f"{target}: [[project]] #{index + 1} is not a table")
        for required in ("name", "prefix"):
            if not entry.get(required):
                raise ManifestError(
                    f"{target}: [[project]] #{index + 1} is missing `{required}`")
        # An unknown key is a typo, and a typo in a manifest is exactly the
        # silent drift this file exists to end: `board_num = 3` would parse,
        # load, and leave the project boardless with no complaint.
        unknown = set(entry) - known
        if unknown:
            raise ManifestError(
                f"{target}: {entry['name']} has unknown key(s): "
                f"{', '.join(sorted(unknown))}")
        projects.append(Project(**entry))

    _validate(projects, target)
    return projects


def _validate(projects: list[Project], target: Path) -> None:
    seen_prefix: dict[str, str] = {}
    seen_repo: dict[str, str] = {}
    for project in projects:
        if len(project.prefix) != 1 or not project.prefix.isalpha():
            raise ManifestError(
                f"{target}: {project.name} prefix {project.prefix!r} must be a "
                "single letter")
        clash = seen_prefix.get(project.prefix)
        if clash:
            raise ManifestError(
                f"{target}: prefix {project.prefix!r} claimed by both {clash} "
                f"and {project.name}")
        seen_prefix[project.prefix] = project.name
        if project.repo:
            if project.repo.count("/") != 1:
                raise ManifestError(
                    f"{target}: {project.name} repo {project.repo!r} is not owner/name")
            dupe = seen_repo.get(project.repo)
            if dupe:
                raise ManifestError(
                    f"{target}: {project.repo} declared twice ({dupe}, {project.name})")
            seen_repo[project.repo] = project.name
        if project.onboarded and not project.repo:
            raise ManifestError(
                f"{target}: {project.name} is onboarded but declares no repo")


def by_repo(path: Path | None = None) -> dict[str, Project]:
    """`{owner/name: Project}` for every entry that HAS a repo.

    Projected repos are absent by construction. A caller keyed on repo cannot
    act on a repo that does not exist, and including them would make
    `"vitalharmony/leasepal" in by_repo()` a lie.
    """
    return {p.repo: p for p in load(path) if p.repo}


def repo_boards(path: Path | None = None) -> dict[str, tuple[str, str]]:
    """`{owner/name: (board_owner, board_number)}` — replaces the two copies."""
    return {repo: project.board
            for repo, project in by_repo(path).items() if project.board}


def sweep_repos(path: Path | None = None) -> list[str]:
    """Repos the hygiene sweep audits: every real repo on the vitalharmony account.

    **Not filtered by `onboarded`.** That flag says whether the 3-lane
    apparatus is installed; stranded work is worth finding either way, and
    openclaw-projects (onboarded = false) is in the live sweep today. Filtering
    on it here would silently drop a repo the sweep currently covers, which is
    the exact class of regression this manifest exists to prevent.

    ke'nekted IS excluded, because it is a **separate account with separate
    credentials**, and a vitalharmony-authed query against it returns EMPTY
    rather than erroring. Sweeping it under the wrong credential would report a
    clean repo, which is worse than not sweeping it.
    """
    return [p.repo for p in load(path)
            if p.repo and p.account == "vitalharmony"]


def prefixes(path: Path | None = None) -> dict[str, str]:
    """`{PREFIX: name}` for every entry, projected repos included.

    Projected entries ARE included here: the point of listing them is to
    reserve the letter so a future repo cannot claim a prefix that already
    means something in the operator's muscle memory.
    """
    return {p.prefix: p.name for p in load(path)}


def lane_shorthand_prefixes(platform_root: Path | None = None) -> dict[str, str]:
    """`{PREFIX: repo-or-name}` as `rules/lane-shorthand.md` states it.

    `expand_lane_shorthand.py` parses that table at runtime and is NOT rewired
    to the manifest: the doc is prose a human reads and the parser already
    treats it as the single source. Duplicating it into the manifest would
    recreate exactly the drift this file exists to end -- so the manifest does
    not own the prefixes, it is CHECKED against them.
    """
    root = platform_root or Path(__file__).resolve().parents[2]
    text = (root / "rules" / "lane-shorthand.md").read_text(encoding="utf-8")
    table: dict[str, str] = {}
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## Repo prefixes"
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        match = re.match(r"^`([A-Za-z])`$", cells[0])
        if match:
            table[match.group(1)] = cells[1].strip("`")
    return table


def check_prefix_agreement(path: Path | None = None,
                           platform_root: Path | None = None) -> list[str]:
    """Findings where the manifest and the shorthand table disagree.

    A prefix claimed in one and not the other is how `L2B F496` reaches the
    wrong repo -- the kind of failure that produces a correct-looking action
    against the wrong issue.
    """
    manifest = prefixes(path)
    doc = lane_shorthand_prefixes(platform_root)
    findings = []
    for letter in sorted(set(manifest) | set(doc)):
        if letter not in doc:
            findings.append(f"{letter}: in projects.toml ({manifest[letter]}) "
                            "but not in rules/lane-shorthand.md")
        elif letter not in manifest:
            findings.append(f"{letter}: in rules/lane-shorthand.md ({doc[letter]}) "
                            "but not in projects.toml")
    return findings
