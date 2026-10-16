#!/usr/bin/env python3
"""A lane's identity label from its checkout (harmonic-forge#1014).

`lane1`/`lane2`/`lane3` and `lanes-claude` name a session `<ws>:<lane>` (for
example `vh:lane2`), where `<ws>` is the short label `projects.toml`'s
`[workspace_labels]` table gives the checkout's `workspace`. One resolver feeds
three readers: the Claude session name, the pinned tmux pane title, and the
teardown name sets in `lanes-claude`/`lane_targets.py`.

Lives beside `manifest_identity.py`, not in it: that file is near R-0006's
300-line cap. An unregistered checkout never refuses a launch: it gets the bare
lane name plus a reason.

CLI: `manifest_labels.py label <path> <lane>` prints the label on stdout, the
reason (if any) on stderr, and always exits 0.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from manifest import ManifestError, manifest_path
from manifest_identity import workspace_for, workspaces

LANES = ("lane1", "lane2", "lane3")
ADVISOR = "advisor"


def workspace_labels(manifest: Path | None = None) -> dict[str, str]:
    """workspace -> short label, for every declared workspace.

    A workspace missing from `[workspace_labels]` falls back to its own name, so
    one missing entry never degrades the other ventures. Raises `ManifestError`
    only when two workspaces resolve to the same label.
    """
    target = manifest or manifest_path()
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"cannot read {target}: {exc}") from exc
    table = raw.get("workspace_labels") or {}
    if not isinstance(table, dict):
        raise ManifestError(f"{target}: [workspace_labels] is not a table")
    labels = {ws: str(table.get(ws) or ws) for ws in workspaces(manifest)}
    seen: dict[str, str] = {}
    for ws, label in labels.items():
        if label in seen:
            raise ManifestError(
                f"{target}: workspaces {seen[label]!r} and {ws!r} share label {label!r}")
        seen[label] = ws
    return labels


def session_label(path: str | Path, lane: str,
                  manifest: Path | None = None) -> tuple[str, str]:
    """(label, reason). `lane` is lane1..3 or advisor; reason is '' on success."""
    try:
        labels = workspace_labels(manifest)
        return f"{labels[workspace_for(path, manifest)]}:{lane}", ""
    except (ManifestError, KeyError, OSError) as exc:
        return lane, f"{path}: not in projects.toml ({exc})"


def lane_label_names(manifest: Path | None = None) -> set[str]:
    """Every `<label>:lane1..3`, plus the bare `lane1..3` an unregistered checkout gets."""
    labels = workspace_labels(manifest).values()
    return {f"{label}:{lane}" for label in labels for lane in LANES} | set(LANES)


def advisor_label_names(manifest: Path | None = None) -> set[str]:
    """Every `<label>:advisor`."""
    return {f"{label}:{ADVISOR}" for label in workspace_labels(manifest).values()}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "label":
        print("usage: manifest_labels.py label <path> <lane>", file=sys.stderr)
        return 2
    label, reason = session_label(argv[1], argv[2])
    # Reason first, label last: _lane_identity.sh reads both from one 2>&1
    # stream (no temp file, which a bad TMPDIR would break) and takes the
    # last line as the label.
    if reason:
        print(reason, file=sys.stderr, flush=True)
    print(label, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
