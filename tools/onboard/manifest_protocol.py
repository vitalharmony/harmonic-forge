"""Protocol-block parsing helpers for :mod:`manifest` (harmonic-forge#705)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from string import Formatter


class ManifestError(RuntimeError):
    """The manifest is missing, unparseable, or internally inconsistent."""


@dataclass(frozen=True)
class Protocol:
    """Repo-specific names consumed by the otherwise-portable lane protocol."""

    worktree_name: str
    l1_post_task: str
    lane_comment_task: str
    gate_checkout_task: str
    lane3_begin_task: str
    runs_lane3: bool

    def worktree_names(self, checkout: str) -> list[str]:
        lanes = (2, 3) if self.runs_lane3 else (2,)
        fields = set()
        try:
            for _literal, field, spec, conversion in Formatter().parse(self.worktree_name):
                if field:
                    fields.add(field)
                    if field not in {"checkout", "lane"} or spec or conversion:
                        raise ValueError(field)
            if fields != {"checkout", "lane"}:
                raise ValueError("missing placeholder")
            return [self.worktree_name.format(checkout=checkout, lane=lane)
                    for lane in lanes]
        except (KeyError, ValueError) as exc:
            raise ManifestError(
                "projects.toml: protocol.worktree_name may use only "
                "{checkout} and {lane}") from exc


def load_protocol(raw: object, target: Path, project: str) -> Protocol | None:
    """Parse one nested ``[project.protocol]`` table, rejecting drift."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError(f"{target}: {project} protocol must be a table")
    known = {f.name for f in Protocol.__dataclass_fields__.values()}
    unknown = set(raw) - known
    if unknown:
        raise ManifestError(
            f"{target}: {project} protocol has unknown key(s): "
            f"{', '.join(sorted(unknown))}")
    missing = known - set(raw)
    if missing:
        raise ManifestError(
            f"{target}: {project} protocol is missing key(s): "
            f"{', '.join(sorted(missing))}")
    if not isinstance(raw["worktree_name"], str):
        raise ManifestError(
            f"{target}: {project} protocol.worktree_name must be a string")
    protocol = Protocol(**raw)
    task_fields = ("l1_post_task", "lane_comment_task", "gate_checkout_task",
                   "lane3_begin_task")
    for field in task_fields:
        value = getattr(protocol, field)
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(
                f"{target}: {project} protocol.{field} must be non-empty")
    if not isinstance(protocol.runs_lane3, bool):
        raise ManifestError(
            f"{target}: {project} protocol.runs_lane3 must be boolean")
    names = protocol.worktree_names("checkout")
    if len(names) != len(set(names)) or any("/" in name or not name for name in names):
        raise ManifestError(
            f"{target}: {project} protocol.worktree_name must produce unique basenames")
    return protocol


def normalize_repo(value: str) -> str:
    """Return a lowercase ``owner/name`` from a repo name or GitHub URL."""
    text = value.strip()
    text = re.sub(
        r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)",
        "", text, flags=re.IGNORECASE)
    text = text.removesuffix(".git")
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", text)
    if match is None or any(part in {".", ".."} for part in match.groups()):
        raise ManifestError(f"repo {value!r} is not owner/name")
    owner, repo = match.groups()
    return f"{owner.lower()}/{repo.lower()}"


def lane_shorthand_prefixes(platform_root: Path | None = None) -> dict[str, str]:
    """Return the prefix table stated in ``rules/lane-shorthand.md``."""
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
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        match = re.match(r"^`([A-Za-z])`$", cells[0])
        if match:
            table[match.group(1)] = cells[1].strip("`")
    return table
