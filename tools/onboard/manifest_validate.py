"""Cross-entry validation for :mod:`manifest` (harmonic-forge#1527).

Moved verbatim from `manifest.py` to keep that file under R-0006's 300-line
cap, with the `min_version` helpers both `manifest.load()` and
the per-checkout overlay resolver (harmonic-forge#1527) validate through. `Project` is typed
under `TYPE_CHECKING` only, so this module never imports `manifest` at runtime
(no cycle).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from manifest_protocol import ManifestError

if TYPE_CHECKING:
    from manifest import Project

#: A version-shaped milestone title, the shape of
#: tools/gh/release_milestone.RELEASE_TITLE (tools/onboard does not import tools/gh).
_VERSION_TITLE = re.compile(r"^\d+(?:\.\d+)*$")

_VERSION = re.compile(r"v(\d{4})\.(0[1-9]|1[0-2])\.(\d+)")


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


def validate(projects: list[Project], target: Path | str) -> None:
    seen_prefix: dict[str, str] = {}
    seen_repo: dict[str, str] = {}
    for project in projects:
        if not isinstance(project.onboarded, bool):
            raise ManifestError(
                f"{target}: {project.name} onboarded must be boolean")
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
            if not project.account:  # harmonic-forge#804: no repo without an identity
                raise ManifestError(f"{target}: {project.name} declares a repo but no `account`")
        if project.active_milestones and not project.milestones:  # harmonic-forge#952
            raise ManifestError(
                f"{target}: {project.name} lists active_milestones but carries no milestones")
        # harmonic-forge#1382 C1: the release is `@current-release`, resolved
        # by the one definition R-0404 and the filing gate use; a version
        # literal here drifts from them at the first turnover.
        pinned = [m for m in project.active_milestones if _VERSION_TITLE.match(m)]
        if pinned:
            raise ManifestError(
                f"{target}: {project.name} pins release milestone(s) {pinned} in "
                "active_milestones; use \"@current-release\" (harmonic-forge#1382)")
        if project.onboarded and not project.repo:
            raise ManifestError(
                f"{target}: {project.name} is onboarded but declares no repo")
        if project.onboarded and not project.workspace:  # harmonic-forge#917
            raise ManifestError(f"{target}: {project.name} is onboarded but declares no workspace")
        if project.onboarded and project.protocol is None:
            raise ManifestError(
                f"{target}: {project.name} is onboarded but declares no [project.protocol]")
