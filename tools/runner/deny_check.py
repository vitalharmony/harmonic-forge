#!/usr/bin/env python3
"""The te-runner's deny-list checks (harmonic-forge#1053, G2 and G6).

`deny_manifest.json` is the one list of paths the unattended agent must not
change. It has two halves, kept equal by tests: `globs` render the agent's
`--settings` Edit deny rules (which catch accidents), and `regexes` feed the
harness-side check of the final diff (which does not trust the agent: a
`python3 -c` write bypasses `--settings`). The diff check also applies
`tools/gh/preclose_check.py`'s `HIGH_BLAST_PATTERNS` by import, never a copy.

    deny_check.py diff --base REF --head REF   # exit 0 clean, 3 on a hit, 2 on error
    deny_check.py paths PATH...                # same question for a path list
    deny_check.py render-settings [--out FILE] # te-runner.settings.json from the manifest
    deny_check.py boundary                     # the fork boundary: exit 0 only when it holds

`diff` uses `--no-renames`, so both sides of a rename and every deletion
appear, and any added or changed symlink is a hit, as is any added module or
package that shadows a stdlib or test-runner name (`shadows`, harmonic-forge#1112).
`paths` refuses (exit 2)
a spelling git never emits. Output is JSON `{"hits": [path...]}`.

Edit rules are rendered without Write twins: Claude Code consults `Edit(path)`
for both tools and ignores a `Write(path)` rule (code.claude.com/docs/en/
permissions, "Read and Edit", fetched 2026-10-19).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "deny_manifest.json"
SETTINGS = HERE / "te-runner.settings.json"
sys.path.insert(0, str(HERE.parent / "gh"))
from preclose_check import HIGH_BLAST_PATTERNS  # noqa: E402

UPSTREAM = "vitalharmony/harmonic-forge"
RUNNER_LOGIN = "vitalharmony-runner"
STRIPPED = ("GH_TOKEN", "GITHUB_TOKEN")
#: `bash`, `sh`, `env`, `eval` and `xargs` would carry a denied command past
#: this list, so they are denied by name, never allowed (harmonic-forge#1112 AC5).
DENY_BASH = ("gh pr merge", "gh issue close", "gh issue edit", "gh label", "gh api",
             "git push", "git remote", "bash", "sh", "env", "eval", "xargs")
ALLOW = ("Read", "Glob", "Grep", "Edit", "Write", "Bash(git add:*)", "Bash(git commit:*)",
         "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
         "Bash(python3:*)", "Bash(mise run:*)",
         "Bash(mkdir:*)", "Bash(rm:*)", "Bash(cp:*)", "Bash(mv:*)", "Bash(touch:*)",
         "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git restore:*)",
         "Bash(git stash:*)", "Bash(git mv:*)", "Bash(git rm:*)", "Bash(git reset:*)",
         "Bash(git rev-parse:*)", "Bash(git branch:*)", "Bash(mise tasks:*)")
#: Top-level module names an added file must never take, anywhere in the tree:
#: the oracle runs without `PYTHONSAFEPATH` (harmonic-forge#1112 AC2, pitch
#: change C), so a head-authored `unittest.py` would otherwise shadow the runner.
SHADOW_NAMES = frozenset(sys.stdlib_module_names) | {"pytest", "unittest", "_pytest", "conftest"}
MODULE_SUFFIXES = (".py", ".pyc", ".pyo", ".pyw", ".so", ".pyd")


class DenyError(Exception):
    """The check could not be made; the message says why."""


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def globs(data: dict | None = None) -> list[str]:
    return [g for group in (data or manifest())["groups"] for g in group["globs"]]


def regexes(data: dict | None = None) -> list[re.Pattern[str]]:
    own = [re.compile(r) for group in (data or manifest())["groups"] for r in group["regexes"]]
    return [pattern for pattern, _why in HIGH_BLAST_PATTERNS] + own


def denied(path: str, patterns: list[re.Pattern[str]] | None = None) -> bool:
    path = path.removeprefix("./")
    return any(p.search(path) for p in (patterns or regexes()))


def paths_hits(paths: list[str]) -> list[str]:
    """`paths` takes free-form text (a handoff's Affected Files), so it refuses
    any spelling git would never emit rather than guess at it, and matches
    every `/`-suffix of a well-formed path, so a prefixed spelling such as
    `harmonic-forge/rules/x.md` is still denied. Over-denying is the
    fail-closed direction."""
    patterns, hits = regexes(), []
    for raw in paths:
        path = raw.removeprefix("./")
        parts = path.split("/")
        if (not path or path.startswith("/") or "\\" in path
                or any(part in ("", ".", "..") for part in parts)):
            raise DenyError(f"not a canonical repo-relative path: {raw!r}")
        if any(denied("/".join(parts[i:]), patterns) for i in range(len(parts))):
            hits.append(raw)
    return sorted(set(hits))


def glob_regex(glob: str) -> re.Pattern[str] | None:
    """A repo-relative glob as a regex (gitignore-style `*`, `**`, `?`), for the
    tests that keep the two halves equal. Globs anchored outside the repo
    (`~/`, `//`) return None."""
    if not glob.startswith("./"):
        return None
    out, i, body = "^", 0, glob[2:]
    while i < len(body):
        if body.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif body.startswith("**", i):
            out, i = out + ".*", i + 2
        elif body[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif body[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(body[i]), i + 1
    return re.compile(out + "$")


def shadows(path: str) -> bool:
    """True when an added `path` is a module, or sits in a package directory,
    whose top-level name is a stdlib, `pytest`, `unittest`, `_pytest` or
    `conftest` name. Any directory of the path counts, not only the repo root
    or `tools/run_tests.py`'s `TEST_DIRS`: a test can put any directory on
    `sys.path`, so the narrower rule is the one that can be bypassed."""
    parts = path.removeprefix("./").split("/")
    name = parts[-1]
    if name.endswith(MODULE_SUFFIXES) and name.split(".", 1)[0] in SHADOW_NAMES:
        return True
    return any(part in SHADOW_NAMES for part in parts[:-1])


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode:
        raise DenyError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def diff_hits(base: str, head: str) -> list[str]:
    raw = git("diff", "--raw", "--no-renames", "-z", "--no-abbrev", base, head)
    fields = raw.split("\0")
    patterns, hits = regexes(), []
    for meta, path in zip(fields[0::2], fields[1::2]):
        if not meta:
            continue
        _old_mode, new_mode, _old, _new, status = meta.lstrip(":").split(" ")
        # Any added or changed symlink is a hit, wherever it points: the runner
        # has no use for one, and resolving targets is how it was bypassed.
        if denied(path, patterns) or new_mode == "120000" \
                or (status.startswith("A") and shadows(path)):
            hits.append(path)
    return sorted(set(hits))


#: The exact command the settings' own PreToolUse hook denies, so the preflight
#: probe proves the hooks enforce in the agent's own launch mode
#: (`te_run.launch_base`), not merely that they are present (harmonic-forge#1195).
SENTINEL_COMMAND = "python3 -c 'print(\"te-runner-hook-probe\")'"
#: Reads the hook's stdin JSON and denies SENTINEL_COMMAND, rebuilt from
#: chr() so the hook's own text never needs nested quoting.
SENTINEL_HOOK = (
    "python3 -c \"import json, sys; d = json.load(sys.stdin); "
    "c = (d.get('tool_input') or {}).get('command'); "
    "s = 'python3 -c ' + chr(39) + 'print(' + chr(34) + 'te-runner-hook-probe' + chr(34) + ')' "
    "+ chr(39); "
    "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
    "'permissionDecision': 'deny', 'permissionDecisionReason': 'te-runner-hook-probe sentinel'}"
    "})) if c == s else None\"")
#: G3's REQUIRED_HOOKS, each an inline no-op: the hooks are passed in
#: `--settings` explicitly, never left to project-settings discovery (#1195).
NOOP_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")


def hooks() -> dict:
    block: dict = {event: [{"hooks": [{"type": "command", "command": "true"}]}]
                   for event in NOOP_HOOK_EVENTS}
    block["PreToolUse"] = [{"matcher": "Bash",
                            "hooks": [{"type": "command", "command": SENTINEL_HOOK}]}]
    return block


def render_settings() -> str:
    regexes()  # the diff check's half must be valid before the settings half ships
    deny = [f"Bash({cmd}:*)" for cmd in DENY_BASH] + [f"Edit({g})" for g in globs()]
    settings = {"permissions": {"allow": list(ALLOW), "deny": deny}, "hooks": hooks()}
    return json.dumps(settings, indent=2) + "\n"


def runner_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED}
    env["GH_CONFIG_DIR"] = str(Path.home() / ".config" / "te-runner" / "gh")
    return env


def gh_json(*args: str) -> object:
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, env=runner_env())
    if result.returncode:
        raise DenyError(f"gh api {' '.join(args)} failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise DenyError(f"gh api {' '.join(args)}: unparseable output") from exc


def boundary() -> list[str]:
    """Reasons the fork boundary does not hold; empty means it holds."""
    user = gh_json("user")
    login = user.get("login") if isinstance(user, dict) else None
    repo = gh_json(f"repos/{UPSTREAM}")
    perms = repo.get("permissions") if isinstance(repo, dict) else None
    problems = []
    if login != RUNNER_LOGIN:
        problems.append(f"login is {login!r}, not {RUNNER_LOGIN}")
    if not isinstance(perms, dict):
        problems.append("no permissions object")
    else:
        for key in ("push", "admin"):
            if perms.get(key) is not False:
                problems.append(f"{key} is {perms.get(key)!r}, not false")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    diff = sub.add_parser("diff")
    diff.add_argument("--base", required=True)
    diff.add_argument("--head", required=True)
    sub.add_parser("paths").add_argument("path", nargs="+")
    sub.add_parser("render-settings").add_argument("--out")
    sub.add_parser("boundary")
    args = parser.parse_args(argv)
    try:
        if args.command == "render-settings":
            text = render_settings()
            Path(args.out).write_text(text) if args.out else sys.stdout.write(text)
            return 0
        if args.command == "boundary":
            problems = boundary()
            print(json.dumps({"holds": not problems, "problems": problems}))
            return 0 if not problems else 3
        hits = (diff_hits(args.base, args.head) if args.command == "diff"
                else paths_hits(args.path))
    # Anything raised (a malformed manifest's re.error or KeyError included)
    # means the check could not decide: exit 2, never Python's default 1.
    except Exception as exc:  # noqa: BLE001
        print(f"deny-check: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"hits": hits}))
    return 3 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
