#!/usr/bin/env python3
"""The gate adapter: how the platform calls a consuming repo's own gate steps.

ADR-008 Decision 1 splits the 3-lane protocol from each repo's gate
environment. The protocol is repo-agnostic and lives here; the handful of
steps whose behaviour depends on a repo's graph schema, process topology or
data shapes stay in that repo, declared in a tracked manifest at
`.claude/gate-adapter.json` and validated against
`schemas/gate-adapter.schema.json`.

**The rule that matters most is the absent case.** ADR-008 AC4:

    Absent manifest = no adapter registered = platform gate steps that
    require one are skipped with an explicit `BLOCKED` finding -- same
    fast-fail posture as a genuine external precondition gap
    (`rules/testing-gate.md` rule 8) -- never silently no-op'd.

So every call here returns a finding, and a missing manifest, a missing key,
an unimportable module or a wrong return shape is `blocked`, never `pass`.
A gate that cannot run a step must say so; it must not read as a step that
ran and found nothing.

The platform owns *when* a step runs and *what shape its result must take*;
the adapter module owns everything downstream of the call. Nothing here grants
the adapter privilege it would not otherwise have: it is the consuming repo's
own module, imported in-process, exactly as before ADR-008.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

MANIFEST_RELPATH = Path(".claude") / "gate-adapter.json"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "gate-adapter.schema.json"

#: The three statuses a gate finding may carry (ADR-008 AC4).
PASS, FAIL, BLOCKED = "pass", "fail", "blocked"


def finding(status: str, *evidence: str) -> dict[str, Any]:
    return {"status": status, "evidence": list(evidence)}


def repo_root(cwd: Path | None = None) -> Path | None:
    """The consuming repo's top level, or None outside a repository."""
    result = subprocess.run(
        ["git", "-C", str(cwd or Path.cwd()), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        return None
    return Path(result.stdout.strip())


def manifest_path(cwd: Path | None = None) -> Path | None:
    root = repo_root(cwd)
    return None if root is None else root / MANIFEST_RELPATH


def load_manifest(cwd: Path | None = None) -> tuple[dict[str, Any] | None, str]:
    """(manifest, why-not). A malformed manifest is NOT an empty one: it
    returns None with its parse error, so the caller BLOCKs rather than
    treating a typo as 'no adapter registered'."""
    path = manifest_path(cwd)
    if path is None:
        return None, "not inside a git repository, so no adapter manifest could be located"
    if not path.is_file():
        return None, f"no adapter manifest at {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"adapter manifest at {path} could not be read: {exc}"
    if not isinstance(data, dict):
        return None, f"adapter manifest at {path} is not a JSON object"
    return data, ""


def _import_module(root: Path, relpath: str) -> Any:
    target = (root / relpath).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise ValueError(f"adapter module {relpath!r} escapes the repository root")
    if not target.is_file():
        raise FileNotFoundError(f"adapter module not found: {target}")
    spec = importlib.util.spec_from_file_location(f"_gate_adapter_{target.stem}", target)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load adapter module: {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call(capability: str, entrypoint_key: str, *, cwd: Path | None = None,
         **kwargs: Any) -> dict[str, Any]:
    """Call one declared adapter entrypoint and return its finding.

    `capability` is a manifest key (`residue_sweep`, `lease`); `entrypoint_key`
    names the entry within it (`entrypoint`, `check_owner`, `acquire`,
    `release`). Everything that can go wrong -- absent manifest, absent key,
    unimportable module, missing function, exception, wrong return shape --
    is `blocked` with the reason as evidence.
    """
    manifest, why_not = load_manifest(cwd)
    if manifest is None:
        return finding(BLOCKED, f"{capability}: {why_not}")
    entry = manifest.get(capability)
    if not isinstance(entry, dict):
        return finding(BLOCKED, f"{capability}: not declared in the adapter manifest")
    name = entry.get(entrypoint_key)
    if not name:
        return finding(BLOCKED, f"{capability}.{entrypoint_key}: not declared in the adapter manifest")
    root = repo_root(cwd)
    assert root is not None  # load_manifest already proved we are in a repo
    try:
        module = _import_module(root, entry["module"])
        function = getattr(module, name)
    except Exception as exc:  # noqa: BLE001 -- every failure is BLOCKED, never a pass
        return finding(BLOCKED, f"{capability}.{entrypoint_key}: {type(exc).__name__}: {exc}")
    try:
        result = function(**kwargs)
    except Exception as exc:  # noqa: BLE001
        return finding(BLOCKED, f"{capability}.{entrypoint_key} raised {type(exc).__name__}: {exc}")
    if (not isinstance(result, dict) or result.get("status") not in (PASS, FAIL, BLOCKED)
            or not isinstance(result.get("evidence", []), list)):
        return finding(BLOCKED, f"{capability}.{entrypoint_key} returned {result!r}, "
                                'not {"status": "pass"|"fail"|"blocked", "evidence": [...]}')
    result.setdefault("evidence", [])
    return result


def declared(capability: str, cwd: Path | None = None) -> dict[str, Any] | None:
    """The manifest entry for `capability`, or None. For data-only keys
    (`tier_w_message`) that have no entrypoint to call."""
    manifest, _ = load_manifest(cwd)
    if manifest is None:
        return None
    entry = manifest.get(capability)
    return entry if isinstance(entry, dict) else None
