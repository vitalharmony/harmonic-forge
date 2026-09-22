#!/usr/bin/env python3
"""harmonic-forge#568 AC1/AC2 — `resolve_sha`'s failure message must name
which repo was searched, and `--repo`'s independence from the attested
repo must be unchanged.

Live-verified against real git repos in temp directories, not mocked --
matching `test_l1_post_base_currency.py`'s own precedent for this module.
"""
import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path, remote: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.test")
    _git(path, "config", "user.name", "Test")
    # Content must be UNIQUE per repo -- two repos with byte-identical
    # commit content (same file, message, author/committer identity) hash
    # to the SAME sha (git's object model is content-addressed), which
    # would make the "sha exists in repo B but not repo A" scenario this
    # file tests unconstructible.
    (path / "README.md").write_text(str(path), encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", f"initial commit for {path}")
    if remote:
        _git(path, "remote", "add", "origin", remote)


class DescribeRepoTests(unittest.TestCase):
    def test_names_the_remote_when_one_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo, remote="https://github.com/vitalharmony/harmonic-forge.git")
            description = L._describe_repo(repo)
            self.assertIn("vitalharmony/harmonic-forge", description)
            self.assertIn(str(repo), description)

    def test_names_the_cwd_when_no_remote_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo, remote=None)
            description = L._describe_repo(repo)
            self.assertIn(str(repo), description)
            self.assertIn("no git remote", description)


class ResolveShaDiagnosticTests(unittest.TestCase):
    def test_ac1_an_unresolvable_sha_names_which_repo_was_searched(self) -> None:
        """The defect this issue exists to fix: `fatal: Needed a single
        revision` alone reads as "the SHA is wrong," with no way to tell
        that apart from "you are standing in the wrong repo entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = Path(tmp) / "repo-a"
            repo_b = Path(tmp) / "repo-b"
            _init_repo(repo_a, remote="https://github.com/vitalharmony/harmonic-forge.git")
            _init_repo(repo_b, remote="https://github.com/vitalharmony/hrse.git")
            # A real commit that exists in repo_b, resolved against repo_a --
            # the exact "standing in the wrong repo" shape from the issue.
            sha_in_b = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_b,
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            with self.assertRaises(SystemExit) as ctx:
                L.resolve_sha(sha_in_b, cwd=repo_a)
            message = str(ctx.exception)
            self.assertIn(sha_in_b, message)
            self.assertIn("vitalharmony/harmonic-forge", message,
                          "must name the repo actually searched (repo_a), "
                          "not silently say only that the SHA is bad")

    def test_ac2_a_forge_repo_flag_with_an_hrse_sha_still_resolves(self) -> None:
        """AC2: --repo's independence from the attested repo must be
        UNCHANGED. This is the a private-repo incident regression this issue's own
        opening section protects -- a forge issue's handoff routinely
        attests an hrse commit, and `resolve_sha` must keep resolving
        against cwd, never against --repo."""
        with tempfile.TemporaryDirectory() as tmp:
            hrse_repo = Path(tmp) / "hrse"
            _init_repo(hrse_repo, remote="https://github.com/vitalharmony/hrse.git")
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=hrse_repo,
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            # --repo is irrelevant to resolve_sha's own signature -- it takes
            # only a value and a cwd. Standing in the hrse repo (cwd),
            # attesting its own commit, must resolve regardless of what a
            # caller's --repo says.
            resolved = L.resolve_sha(sha, cwd=hrse_repo)
            self.assertEqual(resolved, sha)

    def test_ac2_no_resolve_sha_call_site_derives_cwd_from_repo(self) -> None:
        """The regression this AC actually protects lives at the CALL
        SITES, not in `resolve_sha`'s own signature (preclose-inspection
        finding: the test above concedes '--repo is irrelevant to
        resolve_sha's own signature,' which means it cannot fail even if a
        future edit reintroduces a private-repo incident by passing a `cwd` DERIVED
        from `args.repo` at a call site). Static, AST-based: every
        `resolve_sha(...)` call in the module is inspected, and any `cwd=`
        keyword argument whose own source text mentions `repo` (case-
        insensitive) fails this test. A `cwd=` argument that is `scratch`,
        a bare `cwd` passed through, or absent (defaulting to the process
        cwd) all pass; `cwd=Path(args.repo)` or `cwd=repo_from_flag` would
        not."""
        source = Path(L.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls_checked = 0
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "resolve_sha"):
                continue
            calls_checked += 1
            for kw in node.keywords:
                if kw.arg != "cwd":
                    continue
                snippet = ast.unparse(kw.value)
                self.assertNotIn(
                    "repo", snippet.lower(),
                    f"resolve_sha() call at line {node.lineno} passes "
                    f"cwd={snippet!r}, which appears derived from --repo -- "
                    "this is the a private-repo incident regression AC2 exists to "
                    "prevent."
                )
        self.assertGreaterEqual(calls_checked, 4,
                                "expected at least the 4 known resolve_sha() "
                                "call sites -- if this drops, a call site "
                                "may have been refactored out from under "
                                "this guard without updating it")

    def test_an_unresolvable_sha_still_includes_gits_own_error(self) -> None:
        """The new diagnostic is additive -- it must not swallow the
        original git error text a reader might already recognize."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            with self.assertRaises(SystemExit) as ctx:
                L.resolve_sha("0" * 40, cwd=repo)
            self.assertIn("Needed a single revision", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
