"""Protocol-block parsing helpers for :mod:`manifest` (harmonic-forge#705)."""
from __future__ import annotations

import re
from dataclasses import MISSING, dataclass
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
    #: The session boundary's other half (harmonic-forge#730). `lane3_begin_task`
    #: alone described half a cycle: a repo could declare how a Lane 3 session
    #: opens and say nothing about how it closes, and the check that reads these
    #: names would report green having verified four of the five tasks the
    #: universal minimum actually names.
    lane3_end_task: str
    runs_lane3: bool
    #: DJC 2 (harmonic-forge#730). A `runs_lane3` repo either ships
    #: `.claude/gate-adapter.json` or declares `needs_gate_adapter = false`
    #: here. `None` means neither, which is the state the check refuses: an
    #: absent manifest with no declaration is indistinguishable from an
    #: oversight, and silence is exactly what this issue exists to outlaw.
    needs_gate_adapter: bool | None = None

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
    # A field carrying a default is genuinely optional; everything else is
    # required. Without this split, adding any optional field would make every
    # existing projects.toml block invalid at once (harmonic-forge#730).
    optional = {f.name for f in Protocol.__dataclass_fields__.values()
                if f.default is not MISSING or f.default_factory is not MISSING}
    missing = known - optional - set(raw)
    if missing:
        raise ManifestError(
            f"{target}: {project} protocol is missing key(s): "
            f"{', '.join(sorted(missing))}")
    if not isinstance(raw["worktree_name"], str):
        raise ManifestError(
            f"{target}: {project} protocol.worktree_name must be a string")
    protocol = Protocol(**raw)
    # A SECOND declaration of the same set as `Protocol`'s fields above, and
    # the two are not derived from each other -- changing one and not the other
    # leaves the new field unvalidated while everything still imports and runs
    # (harmonic-forge#730). Keep them in step.
    task_fields = ("l1_post_task", "lane_comment_task", "gate_checkout_task",
                   "lane3_begin_task", "lane3_end_task")
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
