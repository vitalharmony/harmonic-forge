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
appear, and a changed symlink whose target lands in a denied path (or leaves
the repo) is a hit. Output is JSON `{"hits": [path...]}`.

Edit rules are rendered without Write twins: Claude Code consults `Edit(path)`
for both tools and ignores a `Write(path)` rule (code.claude.com/docs/en/
permissions, "Read and Edit", fetched 2026-10-19).
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
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
DENY_BASH = ("gh pr merge", "gh issue close", "gh issue edit", "gh label", "gh api",
             "git push", "git remote")
ALLOW = ("Read", "Glob", "Grep", "Edit", "Write", "Bash(git add:*)", "Bash(git commit:*)",
         "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
         "Bash(python3:*)", "Bash(mise run:*)")


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
        _old_mode, new_mode, _old, new_sha, _status = meta.lstrip(":").split(" ")
        if denied(path, patterns):
            hits.append(path)
        elif new_mode == "120000":
            target = git("cat-file", "-p", new_sha)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            if target.startswith("/") or resolved.startswith("..") or denied(resolved, patterns):
                hits.append(path)
    return sorted(set(hits))


def render_settings() -> str:
    deny = [f"Bash({cmd}:*)" for cmd in DENY_BASH] + [f"Edit({g})" for g in globs()]
    settings = {"permissions": {"allow": list(ALLOW), "deny": deny}}
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
                else sorted({p for p in args.path if denied(p)}))
    except (DenyError, OSError, ValueError) as exc:
        print(f"deny-check: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"hits": hits}))
    return 3 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
