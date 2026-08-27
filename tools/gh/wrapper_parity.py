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

harmonic-forge#368 additions
-----------------------------
**`usage`-vs-script was never the whole check.** A flag can be declared in a
task's `usage` block and still never reach the script if the `run` body
doesn't forward it -- the original check only ever diffed `usage` against
the script's own flags and never read `run` at all (Codex F290/c, live). A
separate `unforwarded_flags()` diffs `usage` against the literal `--flag`
tokens actually present in `run`, so a declared-but-silently-dropped flag is
now its own failure category, distinct from "never declared."

**Discovery, not an allow-list of task names.** `discover_wrapper_tasks()`
finds every task whose `run` body invokes a Python script by a literal path
(`python3 <path>.py`, or an interpreter reached by a path such as
`backend/.venv/bin/python <path>.py`) -- the set of tasks a `wrapper-parity`
mise task *should* be checking, read from the doc itself rather than
hand-maintained. A `python3 $SOME_VAR ...` invocation (e.g. this repo's own
`wrapper-parity` task calling itself via `$PARITY`) is deliberately not a
literal path and is not matched -- this is what keeps discovery from finding
and looping back into its own invocation. When a `run` body invokes more
than one script (a precondition script, then the real one), the *last*
literal invocation is registered, not the first -- the precondition-then-
real-script order is what this repo's own tasks actually use.

**`--discover --expect` (preclose-inspection, live-reproduced x4 across
three repos).** A first version of `--discover` always exited 0, including
when `discover_wrapper_tasks()` itself raised `ParityError` -- and every
consumer wired it as the LEFT side of a pipe (`--discover | while read ...`)
under `sh -c -o errexit`, which has no `pipefail`. A pipeline's exit status
is its *last* command's, so a broken `--discover` (malformed mise.toml,
moved repo, a script whose invocation no longer matches the pattern) was
silently swallowed and the whole check reported nothing checked, exit 0 --
exactly the "reports clean while structurally unable to see the drift"
failure this issue exists to fix, reintroduced one layer up. `--expect
<comma-list>` names the task set a consumer's `wrapper-parity` task actually
relies on discovery for; if any named task isn't found with a resolvable
script, `--discover` now exits 2 and reports which ones, loudly, instead of
silently printing fewer lines. Consumers must also capture `--discover`'s
own output via command substitution (`X=$(... --discover --expect ...)`),
never a raw pipe -- `set -e` catches a failing assignment in POSIX sh even
without `pipefail`, which is what actually closes the swallowed-failure gap.

**Comments in `run` do not count as forwarding (preclose-inspection,
live-reproduced).** `run_forwarded_flags()` strips shell comment lines
before matching -- otherwise a flag merely *mentioned* in an explanatory
`# harmonic-forge#290: --milestone was missing...` comment reads as
forwarded, which is precisely backwards for a check whose whole point is
guarding those two flags.
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

# A literal `python3 <path>.py` (or `<interpreter-path>/python3 <path>.py`,
# e.g. `backend/.venv/bin/python scripts/x.py`) invocation inside a task's
# `run` body -- NOT a shell-variable invocation (`python3 $PARITY`), which
# has no `.py` suffix in the literal token and is deliberately excluded
# (see module docstring, harmonic-forge#368).
_SCRIPT_INVOCATION = re.compile(
    r"(?:^|[\s;&|])(?:[\w./~-]*?/)?python3?\s+(~?[\w./-]+\.py)\b"
)

# A whole-line shell comment (optional leading whitespace, then `#`, to end
# of line) -- stripped before flag-matching a `run` body (harmonic-forge#368
# preclose finding: a flag merely mentioned in an explanatory comment must
# not count as forwarded). Doesn't attempt to distinguish an inline `#`
# after real code from a `#` inside a quoted string -- an accepted heuristic
# limit matching the rest of this file's regex-based flag matching; every
# comment in this repo's own `run` bodies is a whole line.
_COMMENT_LINE = re.compile(r"(?m)^[ \t]*#.*$")


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


def _load_tasks(mise_toml: Path) -> dict:
    try:
        data = tomllib.loads(mise_toml.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ParityError(f"could not read {mise_toml}: {exc}") from exc
    tasks = data.get("tasks")
    if not isinstance(tasks, dict):
        raise ParityError(f"no [tasks] table in {mise_toml}")
    return tasks


def _task_entry(mise_toml: Path, task: str) -> dict:
    tasks = _load_tasks(mise_toml)
    if task not in tasks:
        raise ParityError(f"no [tasks.{task}] in {mise_toml}")
    entry = tasks[task]
    return entry if isinstance(entry, dict) else {}


def wrapper_flags(mise_toml: Path, task: str) -> set[str]:
    """Long flags a mise task's `usage` block declares."""
    usage = _task_entry(mise_toml, task).get("usage", "")
    return set(_MISE_FLAG.findall(usage)) if isinstance(usage, str) else set()


def run_forwarded_flags(mise_toml: Path, task: str) -> set[str]:
    """Long flags literally present anywhere in the task's `run` body,
    outside comment lines (harmonic-forge#368 preclose finding)."""
    run = _task_entry(mise_toml, task).get("run", "")
    if not isinstance(run, str):
        return set()
    return set(_LONG_FLAG.findall(_COMMENT_LINE.sub("", run)))


def unforwarded_flags(mise_toml: Path, task: str, allow_missing: set[str]) -> list[str]:
    """Flags `usage` declares that `run` never actually forwards to the
    script (harmonic-forge#368 item 3) -- declared-but-dropped is a
    distinct failure from never-declared, since `usage`-vs-script alone
    reports OK for it."""
    allow = {normalize(f) for f in allow_missing}
    declared = wrapper_flags(mise_toml, task)
    forwarded = run_forwarded_flags(mise_toml, task)
    return sorted(declared - forwarded - allow)


def discover_wrapper_tasks(mise_toml: Path) -> list[tuple[str, Path]]:
    """(task_name, resolved_script_path) for every task whose `run` body
    invokes a Python script by a literal path (harmonic-forge#368 AC1) --
    the set of tasks this check should cover, read from the doc itself
    rather than a hand-maintained list of task names. The LAST literal
    `.py` invocation per task is registered, not the first: a `run` body
    that invokes a precondition script before the real one (this repo's
    own `containers-up`) would otherwise register the precondition
    (preclose-inspection, live-reproduced). Comment lines are stripped
    first, matching `run_forwarded_flags()` -- a `.py` path mentioned only
    in a comment must not be discovered as the invocation. A resolved path
    that doesn't exist on disk is skipped rather than raising -- discovery
    must not fail the whole run over one unrelated task's shell snippet
    that happens to match the pattern without naming a real script (a
    consumer that needs a specific task to resolve should pass it to
    `--discover --expect`, which fails loudly if it doesn't)."""
    tasks = _load_tasks(mise_toml)
    repo_root = mise_toml.resolve().parent
    found: list[tuple[str, Path]] = []
    for name, entry in tasks.items():
        run = entry.get("run", "") if isinstance(entry, dict) else ""
        if not isinstance(run, str):
            continue
        matches = list(_SCRIPT_INVOCATION.finditer(_COMMENT_LINE.sub("", run)))
        if not matches:
            continue
        raw = matches[-1].group(1)
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (repo_root / path).resolve()
        if path.exists():
            found.append((name, path))
    return found


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
    ap.add_argument("--task",
                    help="mise task name, e.g. gh-new-issue. Required unless --discover.")
    ap.add_argument("--script", type=Path,
                    help="Path to the wrapped script. Required unless --discover.")
    ap.add_argument("--allow-missing", default="",
                    help="Comma-separated flags deliberately not exposed OR not forwarded, "
                         "with or without leading dashes, e.g. 'repo,project-owner'. Each one "
                         "is a recorded decision; an undeclared gap is the failure this check "
                         "exists for.")
    ap.add_argument("--discover", action="store_true",
                    help="List every (task, script) pair --mise-toml's run bodies invoke by a "
                         "literal path, one per line as 'task<TAB>script', instead of checking "
                         "a single --task (harmonic-forge#368 AC1).")
    ap.add_argument("--expect", default="",
                    help="With --discover: comma-separated task names that MUST be found with "
                         "a resolvable script, or this exits 2 and reports which are missing "
                         "(harmonic-forge#368 preclose finding). Without this, a broken or "
                         "empty discovery silently exits 0 having found nothing -- exactly the "
                         "'reports clean while structurally unable to see the drift' failure "
                         "this file exists to catch, one layer up. A consumer's wrapper-parity "
                         "task should always pass its own curated task list here.")
    args = ap.parse_args()

    if args.discover:
        try:
            pairs = discover_wrapper_tasks(args.mise_toml)
        except ParityError as exc:
            print(f"[wrapper-parity] ERROR: {exc}", file=sys.stderr)
            return 2
        expect = {t.strip() for t in args.expect.split(",") if t.strip()}
        found_names = {name for name, _ in pairs}
        missing_expected = sorted(expect - found_names)
        if missing_expected:
            print(
                f"[wrapper-parity] ERROR: --expect named tasks not discovered with a "
                f"resolvable script: {', '.join(missing_expected)}. A consumer relying on "
                f"--discover for these will silently stop checking them if this isn't fixed "
                f"(moved/renamed script, mise.toml unreadable, invocation no longer a literal "
                f".py path).",
                file=sys.stderr,
            )
            return 2
        for name, path in pairs:
            print(f"{name}\t{path}")
        return 0

    if not args.task or not args.script:
        print("[wrapper-parity] ERROR: --task and --script are required unless --discover",
              file=sys.stderr)
        return 2

    allow = {f.strip() for f in args.allow_missing.split(",") if f.strip()}
    try:
        missing = check(args.mise_toml, args.task, args.script, allow)
        unforwarded = unforwarded_flags(args.mise_toml, args.task, allow)
    except ParityError as exc:
        # The check itself broke. Exit 2 so a caller can tell "could not
        # check" from "checked, found drift" -- conflating them is how a
        # silently-broken check reads as a passing one.
        print(f"[wrapper-parity] ERROR: {exc}", file=sys.stderr)
        return 2

    problems = []
    if missing:
        problems.append(f"does not expose: {', '.join(missing)}")
    if unforwarded:
        problems.append(f"declares but never forwards to the script: {', '.join(unforwarded)}")

    if problems:
        print(
            f"[wrapper-parity] {args.task} " + "; ".join(problems) + "\n"
            f"  script: {args.script}\n"
            f"  Add each missing flag to the task's usage block and pass it through; "
            f"forward each unforwarded flag in the run body; "
            f"or declare it in --allow-missing with a reason.",
            file=sys.stderr,
        )
        return 1

    print(f"[wrapper-parity] {args.task}: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
