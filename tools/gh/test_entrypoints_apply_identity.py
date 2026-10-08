#!/usr/bin/env python3
"""Every lane entrypoint acts as its project's registered account (harmonic-forge#804).

The per-module tests stub `apply_project_identity` out so their fake repos keep
working, which means none of them notices if the call is deleted. This is the one
place that asserts the wiring itself, source-level, for every entrypoint: the call
is in `main`, and it comes before the first GitHub-touching call there.

Mutation-checked in the #804 preclose: deleting the call from ten of the twelve
entrypoints left the whole suite green before this file existed.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: file -> a call in `main` that reaches GitHub, which the identity call must precede.
#: None means the entrypoint has no single such marker; the call must still be present.
#: `preclose_check.py` is deliberately absent: it makes no GitHub call, so a live identity
#: probe there only makes the pre-close gate network-dependent (harmonic-forge#804 preclose).
ENTRYPOINTS = {
    "l1_post.py": "resolve_sha(args.sha",
    "post_lane_discussion.py": None,
    "post_lane1_issue.py": None,
    "gh_issue.py": "fetch_milestones(args.repo)",
    "post_comment.py": "post_comment(args.repo",
    "l2_post.py": "snapshot(args.repo",
    "check_lane3_ready.py": "fetch_comments(repo",
    "fetch_lane1_context.py": "fetch_issue_body(args.repo",
    "gate_ci.py": "check_gate_result(",
    "lane_transition_report.py": "api(",
    "belt_report.py": "audit(",
}


def _main_source(filename: str) -> str:
    tree = ast.parse((HERE / filename).read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    return ast.unparse(main)


class EveryEntrypointAppliesIdentity(unittest.TestCase):
    def test_main_calls_apply_project_identity(self) -> None:
        for filename in ENTRYPOINTS:
            with self.subTest(entrypoint=filename):
                self.assertIn("apply_project_identity(", _main_source(filename),
                              f"{filename}: main() never applies the repo's identity")

    def test_it_comes_before_the_first_github_call(self) -> None:
        for filename, marker in ENTRYPOINTS.items():
            if marker is None:
                continue
            with self.subTest(entrypoint=filename):
                body = _main_source(filename)
                self.assertIn(marker, body, f"{filename}: marker {marker!r} not found; "
                                            "update ENTRYPOINTS, this check would be vacuous")
                self.assertLess(body.index("apply_project_identity("), body.index(marker),
                                f"{filename}: identity is applied AFTER {marker!r}")

    def test_the_two_tools_that_reach_it_through_l1_post_import_it_from_there(self) -> None:
        """post_lane_discussion / post_lane1_issue take it from `l1_post`; if that
        re-export goes, they import nothing and the call above would NameError."""
        for filename in ("post_lane_discussion.py", "post_lane1_issue.py"):
            with self.subTest(entrypoint=filename):
                self.assertIn("apply_project_identity",
                              (HERE / filename).read_text(encoding="utf-8").split("def main")[0])

    def test_no_entrypoint_still_hardcodes_an_account(self) -> None:
        """lane_transition_report used to shell `gh-as vitalharmony` for every read,
        which would override a per-project slot."""
        for filename in ENTRYPOINTS:
            with self.subTest(entrypoint=filename):
                text = (HERE / filename).read_text(encoding="utf-8")
                self.assertNotIn('"gh-as", "vitalharmony"', text)


if __name__ == "__main__":
    unittest.main()
