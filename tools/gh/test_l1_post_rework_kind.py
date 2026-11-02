#!/usr/bin/env python3
"""harmonic-forge#1247 — a Lane 1 rework records whether it corrects or only rebases.

`--kind rework` requires `--rework-kind correction|rebase`; the footer carries
`rework=<kind>;` directly after `sha=`; the thread extractor reads it back, and a
legacy footer reads `unclassified`, never a guess.
Run: `python3 -m unittest tools.gh.test_l1_post_rework_kind`."""
import importlib.util
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "telemetry"))
sys.path.insert(0, str(ROOT.parent.parent / "skills" / "sprint-plan" / "scripts"))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


l1_post = load("l1_post")
import eras  # noqa: E402
import lane_state  # noqa: E402

ARGV = ["l1_post.py", "--repo", "o/r", "--issue", "1", "--sha", "abc", "--branch", "br",
        "--file", "/dev/null"]


class ReworkKindFlag(unittest.TestCase):
    def _main(self, *extra):
        def no_network(*a, **k):
            raise AssertionError("a network or git call ran before the refusal")
        with mock.patch.object(sys, "argv", ARGV + list(extra)), \
             mock.patch.object(l1_post, "run", side_effect=no_network), \
             mock.patch.object(l1_post.subprocess, "run", side_effect=no_network):
            with self.assertRaises(SystemExit) as caught:
                l1_post.main()
        return str(caught.exception.code)

    def test_a_rework_without_a_kind_is_refused_before_any_call(self):
        self.assertIn("--rework-kind", self._main("--kind", "rework"))

    def test_a_kind_on_another_post_kind_is_refused(self):
        self.assertIn("valid only for --kind rework",
                      self._main("--kind", "handoff", "--plan-first", "false",
                                 "--rework-kind", "rebase"))


class ReworkFooter(unittest.TestCase):
    def _body(self, kind, **kwargs):
        captured = {}

        def fake_comment_body(repo, issue, body):
            captured["body"] = body
            return "https://example/1", 1

        with mock.patch.object(l1_post, "comment_body", fake_comment_body), \
             mock.patch.object(l1_post, "static_checks", lambda sha, branch: (
                 ["body-validation"], ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
                 {"result": "pass"})), \
             mock.patch.object(l1_post, "world_checks", lambda *a, **k: ([], [])), \
             mock.patch.object(l1_post, "require_open_pr", lambda *a, **k: (["pr-open"], [], [])), \
             mock.patch.object(l1_post, "pr_issue_marker", lambda *a, **k: "<!-- lane-pr-link v1 -->"), \
             mock.patch.object(l1_post, "write_receipt", lambda record: None), \
             mock.patch.object(l1_post, "validate_sticky_wicket"):
            l1_post.post_kind("o/r", 1, kind, "body", "abc", "br", **kwargs)
        return captured["body"]

    def test_the_field_sits_directly_after_sha_and_kind_still_reads(self):
        body = self._body("rework", rework_kind="correction")
        self.assertIn("sha=abc; rework=correction;", body)
        self.assertEqual(lane_state._FOOTER_KIND.search(body).group(1), "rework")

    def test_no_other_kind_carries_the_field(self):
        for kind in ("ready-for-l3", "sweep"):
            with self.subTest(kind=kind):
                self.assertNotIn("rework=", self._body(kind))


class ReworkExtractor(unittest.TestCase):
    def _kinds(self, footer: str) -> list:
        body = f"## Rework: H1 — fix the thing\n\n**Next:** Lane 2.\n\n{footer}\n"
        comment = {"id": 5, "body": body, "created_at": "2026-11-01T00:00:00Z",
                   "updated_at": "2026-11-01T00:00:00Z", "user": {"login": "x"}}
        events = eras.comment_events(comment, account="a", org="o", repo="o/r", issue=1)
        return [e["attrs"].get("rework_kind") for e in events
                if e["event_type"] == lane_state.KEY_REWORK]

    def test_the_footer_kind_is_read(self):
        footer = "<!-- l1-post v1; kind=rework; sha=abc; rework=correction; body-sha256=x; checks=y -->"
        self.assertEqual(self._kinds(footer), ["correction"])

    def test_a_legacy_footer_is_unclassified(self):
        footer = "<!-- l1-post v1; kind=rework; sha=abc; body-sha256=x; checks=y -->"
        self.assertEqual(self._kinds(footer), ["unclassified"])


if __name__ == "__main__":
    unittest.main()
