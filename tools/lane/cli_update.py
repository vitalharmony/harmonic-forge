#!/usr/bin/env python3
"""Keep every operator CLI current (harmonic-forge#1305).

The lane model aliases follow the installed CLI, not the vendor catalog, so a
stale CLI silently runs an agent on the wrong model. `forge-cli-update.timer`
runs this every four hours over the registry in `cli_update.toml`:

- a CLI whose binary is missing is logged `absent` and skipped, never an error;
- a `side-by-side` CLI (Claude Code keeps old versions beside the new one) runs
  its update command every time, and is `updated` or `current` from the version
  read before and after;
- an `npm-global` CLI is compared with the published version; when they differ
  it is updated, unless a process of that CLI is running, in which case it is
  `deferred` to the next tick (npm rewrites the package folder in place, which
  breaks a running Node CLI that loads modules lazily).

A process belongs to an npm CLI when the real path of `/proc/<pid>/exe`, or of
its first argument when the executable is node, is under the package folder in
`npm root -g`. Command-line text is never matched: it would catch an agent shell
that merely mentions the CLI and miss the native child codex hands off to.

Each run appends one JSON line per CLI (`ts`, `name`, `before`, `after`,
`status`, and `detail` on a failure) to the log. One CLI failing never stops the
others. The run exits non-zero if any CLI `failed`, or if one has been
`deferred` `defer_streak_limit` runs in a row.

Commands are argv lists run without a shell; `%h` is the only substitution.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "cli_update.toml"
STATUSES = ("current", "updated", "deferred", "failed", "absent")
KINDS = ("side-by-side", "npm-global")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+[0-9A-Za-z.+-]*")
VERSION_TIMEOUT, UPDATE_TIMEOUT = 60, 900

Runner = Callable[[list[str], int], "tuple[int, str]"]


def run(argv: list[str], timeout: int) -> tuple[int, str]:
    """Run argv without a shell; return (exit code, stdout + stderr)."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _expand(value, home: str):
    if isinstance(value, str):
        return value.replace("%h", home)
    if isinstance(value, list):
        return [_expand(v, home) for v in value]
    return value


def load_registry(path: Path, home: str) -> dict:
    """The registry with `%h` expanded. A malformed entry raises: it is checked-in data."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    clis = []
    for raw in data.get("cli", []):
        entry = {k: _expand(v, home) for k, v in raw.items()}
        for key in ("name", "kind", "binary", "version_cmd", "update_cmd"):
            if key not in entry:
                raise ValueError(f"{path}: entry {entry.get('name')!r} has no {key!r}")
        if entry["kind"] not in KINDS:
            raise ValueError(f"{path}: {entry['name']!r} kind must be one of {KINDS}")
        if entry["kind"] == "npm-global" and not (entry.get("package") and entry.get("latest_cmd")):
            raise ValueError(f"{path}: npm-global {entry['name']!r} needs package and latest_cmd")
        clis.append(entry)
    return {
        "log": Path(_expand(data["log"], home)),
        "npm_root_cmd": _expand(data["npm_root_cmd"], home),
        "defer_streak_limit": int(data.get("defer_streak_limit", 6)),
        "cli": clis,
    }


def parse_version(text: str) -> Optional[str]:
    match = VERSION_RE.search(text or "")
    return match.group(0) if match else None


def _version(entry: dict, runner: Runner) -> Optional[str]:
    code, out = runner(entry["version_cmd"], VERSION_TIMEOUT)
    return parse_version(out) if code == 0 else None


def _under(path: str, root: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(root)
    except (OSError, ValueError):
        return False


def running_pids(package_dir: Path, proc_root: Path = Path("/proc"), own_pid: Optional[int] = None) -> list[int]:
    """Processes whose executable, or node's first argument, resolves under `package_dir`."""
    root = Path(os.path.realpath(package_dir))
    found = []
    for entry in proc_root.iterdir() if proc_root.is_dir() else []:
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            exe = os.readlink(entry / "exe")
        except OSError:
            continue
        if _under(exe, root):
            found.append(int(entry.name))
            continue
        if os.path.basename(exe).startswith("node"):
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if len(argv) > 1 and argv[1]:
                first = argv[1].decode(errors="replace")
                if not os.path.isabs(first):
                    try:
                        first = os.path.join(os.readlink(entry / "cwd"), first)
                    except OSError:
                        continue
                if _under(first, root):
                    found.append(int(entry.name))
    return found


def update_one(entry: dict, runner: Runner, npm_root: Optional[Path], proc_root: Path) -> dict:
    """One CLI's log line, without `ts`. Raises only on a bug; the caller isolates it."""
    line = {"name": entry["name"], "before": None, "after": None}
    if not Path(entry["binary"]).exists():
        return {**line, "status": "absent"}
    before = _version(entry, runner)
    line["before"] = before
    if entry["kind"] == "npm-global":
        code, out = runner(entry["latest_cmd"], VERSION_TIMEOUT)
        latest = parse_version(out) if code == 0 else None
        if latest is None:
            return {**line, "after": before, "status": "failed", "detail": "latest version unreadable"}
        if before == latest:
            return {**line, "after": before, "status": "current"}
        if npm_root is None:
            return {**line, "after": before, "status": "failed", "detail": "npm root unreadable"}
        if running_pids(npm_root / entry["package"], proc_root, os.getpid()):
            return {**line, "after": before, "status": "deferred"}
    code, out = runner(entry["update_cmd"], UPDATE_TIMEOUT)
    after = _version(entry, runner)
    line["after"] = after
    if code != 0 or after is None:
        return {**line, "status": "failed", "detail": (out or "").strip()[-300:] or f"exit {code}"}
    return {**line, "status": "updated" if after != before else "current"}


def deferred_streak(log: Path, name: str) -> int:
    """How many of `name`'s most recent log lines in a row are `deferred`."""
    if not log.exists():
        return 0
    streak = 0
    for raw in log.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if row.get("name") == name:
            streak = streak + 1 if row.get("status") == "deferred" else 0
    return streak


def main(argv: Optional[list[str]] = None, *, runner: Runner = run,
         proc_root: Path = Path("/proc"), now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--home", default=os.path.expanduser("~"))
    args = parser.parse_args(argv)
    reg = load_registry(args.registry, args.home)
    code, out = runner(reg["npm_root_cmd"], VERSION_TIMEOUT)
    npm_root = Path(out.strip().splitlines()[-1]) if code == 0 and out.strip() else None
    reg["log"].parent.mkdir(parents=True, exist_ok=True)
    failed, stuck = [], []
    for entry in reg["cli"]:
        try:
            line = update_one(entry, runner, npm_root, proc_root)
        except Exception as exc:  # one CLI's bug never stops the others
            line = {"name": entry["name"], "before": None, "after": None,
                    "status": "failed", "detail": f"{type(exc).__name__}: {exc}"}
        line = {"ts": now().isoformat(), **line}
        with reg["log"].open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        print(json.dumps(line, sort_keys=True))
        if line["status"] == "failed":
            failed.append(entry["name"])
        if deferred_streak(reg["log"], entry["name"]) >= reg["defer_streak_limit"]:
            stuck.append(entry["name"])
    if stuck:
        print(f"cli_update: deferred {reg['defer_streak_limit']} runs in a row: {', '.join(stuck)}", file=sys.stderr)
    if failed:
        print(f"cli_update: failed: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed or stuck else 0


if __name__ == "__main__":
    sys.exit(main())
