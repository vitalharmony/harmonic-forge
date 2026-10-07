"""The constraints map half of `ci_plan.py` (harmonic-forge#918): which tests
name the files a branch changes. Split out to keep both under R-0006's cap.

Every path here is relative to the repository ROOT, and every git call runs at
that root: `git grep` is scoped to its cwd subtree, so a run from `frontend/`
would otherwise map a `backend/` file to nothing and still succeed.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

STOPLIST = {"index", "app", "main", "utils", "__init__", "init", "test", "tests"}
#: Data and prose files: their bare stem ("projects", "settings") is an English
#: word, so only the full basename ("projects.toml") is searched for them.
DATA_SUFFIXES = {".toml", ".json", ".md", ".yaml", ".yml", ".txt", ".cfg", ".ini", ".lock"}
TEST_PATHSPECS = [":(glob)**/tests/**", ":(glob)**/test/**", ":(glob)**/*_test.*",
                  ":(glob)**/*Test.*", ":(glob)**/test_*"]
SETUP_NAMES = ("conftest.py", "flutter_test_config.dart", "TestCase.php", "setup.ts",
               "setupTests.ts", "vitest.setup.ts")
#: Weak (bare-word) matches shown per file; strong matches are never truncated.
MAX_WEAK_HITS = 8


def git(cwd: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def git_grep(root: Path, needle: str) -> str:
    """Matches as `git grep` prints them. Exit 1 is 'no match' and returns ''; any
    other failure raises, so a broken search is never read as an empty map."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "grep", "-n", "-w", "-F", "-e", needle, "--",
             *TEST_PATHSPECS], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git grep could not run: {exc}") from exc
    if done.returncode == 1:
        return ""
    if done.returncode != 0:
        raise RuntimeError(f"git grep failed ({done.returncode}): {done.stderr.strip()[:200]}")
    return done.stdout


def repo_root(cwd: Path) -> Path:
    top = git(cwd, "rev-parse", "--show-toplevel")
    if not top or not top.strip():
        raise RuntimeError(f"{cwd} is not inside a git checkout")
    return Path(top.strip())


def changed_files(root: Path) -> list[str]:
    """Tracked changes against the merge base with origin/main (committed or
    not), plus new untracked files that are not git-ignored."""
    base = git(root, "merge-base", "origin/main", "HEAD")
    if not base:
        raise RuntimeError("cannot find the merge base with origin/main (run `git fetch`)")
    tracked = git(root, "diff", "--name-only", base.strip())
    untracked = git(root, "ls-files", "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        raise RuntimeError("git could not list the changed files")
    return [line for line in (tracked + untracked).splitlines() if line]


def root_relative(arg: str, cwd: Path, root: Path) -> str:
    """A path argument as the root-relative form every other path uses: one
    given relative to the invoking directory is converted, one already
    root-relative is kept."""
    candidate = (cwd / arg).resolve()
    try:
        if candidate.exists():
            return str(candidate.relative_to(root.resolve()))
    except ValueError:
        pass
    return arg


def is_test(path: str) -> bool:
    name = Path(path).name
    return bool(re.search(r"(^|/)tests?/", path) or name.startswith("test_")
                or re.search(r"(_test|Test)\.", name))


def needles(path: str) -> list[tuple[str, bool]]:
    """`(needle, strong)`: strong needles name the file (path tail, basename,
    dotted module) and rank above a bare-stem match. The stoplist and the data
    suffixes gate ONLY the bare stem: `app.main` is a precise needle even though
    `main` is not."""
    pure = Path(path)
    found: list[tuple[str, bool]] = [(pure.name, True)]
    if len(pure.parts) >= 2:
        found.append(("/".join(pure.parts[-2:]), True))
    if pure.suffix == ".py":
        found += [(".".join(pure.with_suffix("").parts[-n:]), True)
                  for n in (2, 3) if len(pure.parts) >= n]
    if pure.suffix not in DATA_SUFFIXES and pure.stem.lower() not in STOPLIST:
        found.append((pure.stem, False))
    return found


def constraints(root: Path, files: list[str]) -> list[str]:
    lines: list[str] = []
    for path in files:
        if is_test(path):
            continue
        hits: dict[str, tuple[int, str]] = {}
        for needle, strong in needles(path):
            for row in git_grep(root, needle).splitlines():
                name, _, rest = row.partition(":")
                score = 2 if strong else 1
                if name not in hits or hits[name][0] < score:
                    hits[name] = (score, rest.strip()[:110])
        if not hits:
            continue
        ranked = sorted(hits.items(), key=lambda item: (-item[1][0], item[0]))
        strong = [item for item in ranked if item[1][0] == 2]
        weak = [item for item in ranked if item[1][0] == 1]
        shown = strong + weak[:MAX_WEAK_HITS]
        lines.append(f"  {path}")
        for name, (_, text) in shown:
            lines.append(f"    {name}:{text}")
        if len(weak) > MAX_WEAK_HITS:
            lines.append(f"    ... and {len(weak) - MAX_WEAK_HITS} more weaker (bare-word) "
                         "test file(s) not shown")
        for setup in setup_files(root, dict(shown)):
            lines.append(f"    setup: {setup}")
    return lines


def setup_files(root: Path, test_files: dict) -> list[str]:
    found: list[str] = []
    for name in test_files:
        directory = Path(name).parent
        while True:
            for setup in SETUP_NAMES:
                candidate = directory / setup
                if (root / candidate).is_file() and str(candidate) not in found:
                    found.append(str(candidate))
            if directory == Path("."):
                break
            directory = directory.parent
    return found
