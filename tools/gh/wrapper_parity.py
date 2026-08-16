#!/usr/bin/env python3
"""Assert a mise wrapper exposes every flag of the script it wraps.

harmonic-forge#290. The recurring failure this exists to stop: a script in
`tools/gh/` gains or hardens a flag, and the `mise.toml` wrapper in the
consuming repo is not updated in the same change. Three occurrences before
this check existed:

  hrse#883            `--tier`, `--body-file` missing -> documented command errored
  harmonic-forge#266  `--body` corruption -> `--body-file` needed
  harmonic-forge#290  `--milestone` missing -> hrse filing failed outright

The third was total: `--milestone` became *required* for any repo with
milestones (harmonic-forge#283), and the wrapper had no way to pass it, so
every hrse filing through the sanctioned path failed.

Design notes
------------
**Flags are read from the script's own `--help` usage block, not by
importing it.** All three current scripts build their parser inline inside
`main()`, so introspection would mean refactoring production tooling to
expose a factory. Parsing `--help` keeps the check decoupled and works for
any script, including ones this repo does not own.

Specifically the *usage block*, not the options section: argparse help text
frequently mentions other flags (`gh_issue.py`'s `--milestone` help cites
`--tier`; `--body-file`'s cites `--body`), which would be picked up as
phantom flags and produce false failures. The usage line contains only real
flags.

**Omission must be declared, not inferred.** Some flags are deliberately not
exposed -- the wrapper supplies `--repo` from `$GH_REPO`, board coordinates
come from environment defaults. A check that
guessed at intent would either miss real gaps or nag about settled ones, so
intentional omissions are listed explicitly per wrapper and an *undeclared*
missing flag is the failure.

That also means removing a flag from the allow-list is how you re-open the
question later, rather than the check silently forgetting it was ever asked.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# Extract the `usage:` block: everything up to the first blank line or the
# first line starting at column 0 with a letter (argparse's description).
_USAGE_BLOCK = re.compile(r"^usage:(.*?)(?:\n\n|\n[A-Za-z])", re.S | re.M)
_LONG_FLAG = re.compile(r"--[A-Za-z][\w-]*")

# `flag "--name <value>"` inside a mise task's `usage = '''...'''` block.
_MISE_FLAG = re.compile(r"""^\s*flag\s+"(--[\w-]+)""", re.M)


class ParityError(RuntimeError):
    """The check could not run. Distinct from the check reporting drift."""


def script_flags(script: Path) -> set[str]:
    """Long flags a script accepts, per its own `--help` usage block."""
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ParityError(f"could not run {script} --help: {exc}") from exc
    if proc.returncode != 0:
        raise ParityError(
            f"{script} --help exited {proc.returncode}: {proc.stderr.strip()[:200]}"
        )
    match = _USAGE_BLOCK.search(proc.stdout)
    if not match:
        raise ParityError(f"no usage block in {script} --help output")
    flags = set(_LONG_FLAG.findall(match.group(1)))
    flags.discard("--help")
    if not flags:
        raise ParityError(f"parsed zero flags from {script} usage block")
    return flags


def wrapper_flags(mise_toml: Path, task: str) -> set[str]:
    """Long flags a mise task's `usage` block declares."""
    try:
        data = tomllib.loads(mise_toml.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ParityError(f"could not read {mise_toml}: {exc}") from exc
    tasks = data.get("tasks")
    if not isinstance(tasks, dict) or task not in tasks:
        raise ParityError(f"no [tasks.{task}] in {mise_toml}")
    entry = tasks[task]
    usage = entry.get("usage", "") if isinstance(entry, dict) else ""
    return set(_MISE_FLAG.findall(usage))


def normalize(flag: str) -> str:
    """`repo` and `--repo` mean the same thing.

    argparse refuses a value that begins with `--`, so the natural
    `--allow-missing --repo,--tier` fails with a confusing parse error
    rather than doing what it obviously means. Accepting both spellings is
    cheaper than making every caller remember the `=` form.
    """
    return "--" + flag.strip().lstrip("-")


def check(mise_toml: Path, task: str, script: Path, allow_missing: set[str]) -> list[str]:
    """Return undeclared-missing flags. Empty list means parity holds."""
    allow = {normalize(f) for f in allow_missing}
    missing = script_flags(script) - wrapper_flags(mise_toml, task) - allow
    return sorted(missing)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Assert a mise wrapper exposes every flag of the script it wraps",
    )
    ap.add_argument("--mise-toml", required=True, type=Path,
                    help="Path to the consuming repo's mise.toml")
    ap.add_argument("--task", required=True,
                    help="mise task name, e.g. gh-new-issue")
    ap.add_argument("--script", required=True, type=Path,
                    help="Path to the wrapped script")
    ap.add_argument("--allow-missing", default="",
                    help="Comma-separated flags deliberately not exposed, with or "
                         "without leading dashes, e.g. 'repo,project-owner'. Each one is a "
                         "recorded decision; an undeclared missing flag is the failure "
                         "this check exists for.")
    args = ap.parse_args()

    allow = {f.strip() for f in args.allow_missing.split(",") if f.strip()}
    try:
        missing = check(args.mise_toml, args.task, args.script, allow)
    except ParityError as exc:
        # The check itself broke. Exit 2 so a caller can tell "could not
        # check" from "checked, found drift" -- conflating them is how a
        # silently-broken check reads as a passing one.
        print(f"[wrapper-parity] ERROR: {exc}", file=sys.stderr)
        return 2

    if missing:
        print(
            f"[wrapper-parity] {args.task} does not expose: {', '.join(missing)}\n"
            f"  script: {args.script}\n"
            f"  Add each to the task's usage block and pass it through, or "
            f"declare it in --allow-missing with a reason.",
            file=sys.stderr,
        )
        return 1

    print(f"[wrapper-parity] {args.task}: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
