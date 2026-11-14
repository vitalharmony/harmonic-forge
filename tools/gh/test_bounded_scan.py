"""harmonic-forge#1363: the SCAN grant, bounded_scan.py and the shim's admission.

Fake gh runners and temporary stores only: nothing reads the network, the real
grant store, the real snapshot cache or the real receipts file."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))
import bounded_scan as bs  # noqa: E402
import scan_grant  # noqa: E402

REPO = "vitalharmony/hrse"
SESSION = "sess-test"


def _load_shim():
    loader = importlib.machinery.SourceFileLoader("gh_shim_under_test", str(HERE / "gh_shim"))
    spec = importlib.util.spec_from_loader("gh_shim_under_test", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def page_output(status: int, items=None, etag='W/"e1"', link=""):
    head = f"HTTP/2.0 {status} {'OK' if status == 200 else 'Not Modified'}\nEtag: {etag}\n"
    if link:
        head += f"Link: {link}\n"
    body = json.dumps(items) if items is not None else ""
    return head + "\n" + body


class Runner:
    """Answers each page of a list call from `pages`; records the argv."""

    def __init__(self, pages: dict[int, tuple[int, list | None, str]]):
        self.pages = pages
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.envs.append(kwargs.get("env") or {})
        page = int(argv[-1].split("&page=")[1].split("&")[0])
        status, items, link = self.pages[page]
        exit_code = 0 if status == 200 else 1
        return subprocess.CompletedProcess(argv, exit_code, page_output(status, items, link=link),
                                           "gh: HTTP 304" if status == 304 else "")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        scan_grant.SCAN_STATE_DIR = self.tmp / "grants"
        self.env = mock.patch.dict(os.environ, {
            "CLAUDE_CODE_SESSION_ID": SESSION,
            bs.SNAPSHOT_ROOT_ENV: str(self.tmp / "snap"),
            bs.RECEIPTS_ENV: str(self.tmp / "receipts.jsonl")})
        self.env.start()
        self.budgets = mock.patch.multiple(bs.gh_scan_patterns,
                                           budget=mock.Mock(return_value=(4000, 0.0)),
                                           graphql_budget=mock.Mock(return_value=(4900, 0.0)))
        self.budgets.start()

    def tearDown(self):
        self.budgets.stop()
        self.env.stop()
        scan_grant.SCAN_STATE_DIR = None

    def grant(self, letter="H", scope="open-issues", session=SESSION):
        scan_grant.write(letter, scope, session, f"SCAN {letter} {scope}: test", "test")

    def args(self, **kw):
        base = {"repo": "H", "scope": "open-issues", "fields": None, "max_pages": 10,
                "dry_run": False}
        base.update(kw)
        return argparse.Namespace(**base)

    def run_scan(self, runner, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = bs.run(self.args(**kw), runner=runner)
        return code, out.getvalue(), err.getvalue()

    def receipts(self):
        path = self.tmp / "receipts.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


class Grammar(unittest.TestCase):
    def test_16_a_known_letter_with_an_unknown_scope_is_refused(self):
        self.assertIsInstance(scan_grant.parse("SCAN H everything: x"), str)

    def test_12_a_cross_account_letter_is_refused(self):
        self.assertIn("another account", scan_grant.parse("SCAN K open-issues: x"))

    def test_11_malformed_lines_are_refused(self):
        for line in ("scan H open-issues: x", "SCAN H open-issues:", "  SCAN H open-issues: x",
                     "SCAN H open-issues x"):
            self.assertIsInstance(scan_grant.parse(line), str, line)

    def test_a_valid_line_parses(self):
        self.assertEqual(scan_grant.parse("SCAN H milestone=8: release"),
                         ("H", "milestone=8", "release"))


class Scan(Base):
    def test_1_no_grant_is_refused_naming_the_line(self):
        code, _, err = self.run_scan(Runner({}))
        self.assertEqual(code, 2)
        self.assertIn("SCAN H open-issues: <reason>", err)
        self.assertEqual(self.receipts()[-1]["outcome"], "refused")

    def test_2_a_grant_for_another_scope_is_refused(self):
        self.grant(scope="open-prs")
        self.assertEqual(self.run_scan(Runner({}))[0], 2)

    def test_3_a_grant_for_another_repo_is_refused(self):
        self.grant(letter="F")
        self.assertEqual(self.run_scan(Runner({}))[0], 2)

    def test_15_a_grant_from_another_session_is_refused(self):
        self.grant(session="sess-other")
        self.assertEqual(self.run_scan(Runner({}))[0], 2)

    def test_4_the_floor_refuses_with_both_budgets_and_the_estimate(self):
        self.grant()
        bs.gh_scan_patterns.budget.return_value = (1005, 0.0)
        code, _, err = self.run_scan(Runner({}))
        self.assertEqual(code, 2)
        for text in ("1005", "10 page(s)", "4900"):
            self.assertIn(text, err)

    def test_an_unknown_rest_budget_refuses(self):
        self.grant()
        bs.gh_scan_patterns.budget.return_value = None
        self.assertEqual(self.run_scan(Runner({}))[0], 2)

    def test_a_scan_writes_a_snapshot_and_drops_pull_requests(self):
        self.grant()
        runner = Runner({1: (200, [{"number": 1}, {"number": 2, "pull_request": {}}],
                             '<x?page=2>; rel="next"'),
                         2: (200, [{"number": 3}], "")})
        code, out, _ = self.run_scan(runner)
        self.assertEqual(code, 0)
        snap = json.loads(Path(out.strip()).read_text())
        self.assertEqual([i["number"] for p in snap["pages"] for i in p["items"]], [1, 3])
        self.assertEqual(self.receipts()[-1]["pages_fetched"], 2)
        self.assertTrue(all(e.get("GH_SHIM_CALLER") == "bounded_scan" for e in runner.envs))

    def test_5_a_fresh_snapshot_makes_no_call(self):
        self.grant()
        self.run_scan(Runner({1: (200, [{"number": 1}], "")}))
        runner = Runner({})
        code, _, _ = self.run_scan(runner)
        self.assertEqual((code, runner.calls), (0, []))
        self.assertEqual(self.receipts()[-1]["outcome"], "served-from-snapshot")

    def test_6_a_stale_snapshot_reuses_304_pages(self):
        self.grant()
        self.run_scan(Runner({1: (200, [{"number": 1}], "")}))
        path = bs.snapshot_path(REPO, "open-issues")
        snap = json.loads(path.read_text())
        snap["fetched_at"] = 0
        path.write_text(json.dumps(snap))
        runner = Runner({1: (304, None, "")})
        code, out, _ = self.run_scan(runner)
        self.assertEqual(code, 0)
        self.assertIn("If-None-Match: W/\"e1\"", " ".join(runner.calls[0]))
        self.assertEqual(self.receipts()[-1]["pages_304"], 1)
        self.assertEqual(json.loads(Path(out.strip()).read_text())["pages"][0]["items"],
                         [{"number": 1}])

    def test_17_a_304_with_gh_exit_1_is_unchanged_not_an_error(self):
        self.test_6_a_stale_snapshot_reuses_304_pages()
        self.assertEqual(self.receipts()[-1]["outcome"], "ok")

    def test_18_max_pages_truncates_and_a_shrunk_list_drops_its_tail(self):
        self.grant()
        code, out, _ = self.run_scan(Runner({1: (200, [{"number": 1}], '<x>; rel="next"'),
                                             2: (200, [{"number": 2}], "")}), max_pages=1)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(Path(out.strip()).read_text())["truncated"])
        self.assertTrue(self.receipts()[-1]["truncated"])

    def test_3b_other_fields_never_get_a_cached_projection(self):
        """Preclose pass 1, survivor 3: a snapshot made with --fields number is not
        served to a run asking for number,title."""
        self.grant()
        self.run_scan(Runner({1: (200, [{"number": 1, "title": "a"}], "")}), fields="number")
        runner = Runner({1: (200, [{"number": 1, "title": "a"}], "")})
        code, out, _ = self.run_scan(runner, fields="number,title")
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.calls), 1)
        items = json.loads(Path(out.strip()).read_text())["pages"][0]["items"]
        self.assertEqual(items, [{"number": 1, "title": "a"}])

    def test_4b_a_leftover_temporary_file_never_breaks_a_run(self):
        """Preclose pass 1, survivor 4: the temporary name is per run, so another
        run's temporary file (or its absence) cannot crash this one."""
        self.grant()
        path = bs.snapshot_path(REPO, "open-issues")
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / (path.name + ".tmp")).write_text("half-written")
        code, _, _ = self.run_scan(Runner({1: (200, [{"number": 1}], "")}))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text())["pages"][0]["items"], [{"number": 1}])
        self.assertEqual([p.name for p in path.parent.glob("*.tmp") if p.name != path.name + ".tmp"], [])

    def test_7_every_run_writes_a_receipt(self):
        self.run_scan(Runner({}))
        self.grant()
        self.run_scan(Runner({1: (200, [], "")}))
        self.assertEqual([r["outcome"] for r in self.receipts()], ["refused", "ok"])

    def test_a_failed_page_leaves_the_snapshot_as_it_was(self):
        self.grant()
        code, _, _ = self.run_scan(Runner({1: (500, None, "")}))
        self.assertEqual(code, 1)
        self.assertFalse(bs.snapshot_path(REPO, "open-issues").exists())


class Shim(Base):
    PATH = "repos/vitalharmony/hrse/issues?state=open&per_page=100&page=1"

    def setUp(self):
        super().setUp()
        self.shim = _load_shim()

    def test_8_the_marker_without_a_grant_is_not_admitted(self):
        self.assertFalse(self.shim._bounded_scan_admitted(["api", "-i", self.PATH]))

    def test_9_the_marker_with_a_grant_for_that_repo_is_admitted(self):
        self.grant()
        self.assertTrue(self.shim._bounded_scan_admitted(["api", "-i", self.PATH]))
        self.assertTrue(self.shim._bounded_scan_admitted(
            ["api", "-i", "-H", 'If-None-Match: W/"x"', self.PATH]))

    def test_10_a_grant_for_another_repo_is_not_admitted(self):
        self.grant(letter="F")
        self.assertFalse(self.shim._bounded_scan_admitted(["api", "-i", self.PATH]))

    def test_14_other_shapes_are_never_admitted(self):
        self.grant()
        for argv in (["issue", "list", "-R", REPO], ["api", "graphql", "-f", "query=x"],
                     ["api", "-X", "POST", self.PATH], ["api", self.PATH, "--paginate"]):
            self.assertFalse(self.shim._bounded_scan_admitted(argv), argv)

    def test_15b_no_session_is_not_admitted(self):
        self.grant()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertFalse(self.shim._bounded_scan_admitted(["api", "-i", self.PATH]))

    def test_13_the_touch_file_is_honored_before_the_marker_check(self):
        """Preclose pass 1, survivor 6: run the shim. With a fresh touch file and the
        bounded_scan marker but no grant, the real gh is exec'd, not refused."""
        execs = []
        with mock.patch.object(self.shim.os, "execv", side_effect=lambda p, a: execs.append(a)), \
                mock.patch.object(sys, "argv", ["gh", "api", "-i",
                                                "repos/vitalharmony/hrse/issues?state=all"]), \
                mock.patch.dict(os.environ, {"GH_SHIM_CALLER": "bounded_scan"}), \
                mock.patch("gh_scan_patterns.consume_override", return_value=True):
            self.shim.main()
        self.assertEqual(len(execs), 1)

    def test_1b_a_grant_admits_only_its_own_scope_path(self):
        """Preclose pass 1, survivor 1: a label=foo grant never admits state=all,
        an unlabelled list or pulls of the same repo."""
        self.grant(scope="label=foo")
        granted = bs.list_path(REPO, "label=foo", 3)
        self.assertTrue(self.shim._bounded_scan_admitted(["api", "-i", granted]))
        for path in ("repos/vitalharmony/hrse/issues?state=all&per_page=100&page=1",
                     bs.list_path(REPO, "open-issues", 1), bs.list_path(REPO, "open-prs", 1),
                     granted + "&state=closed"):
            self.assertFalse(self.shim._bounded_scan_admitted(["api", "-i", path]), path)


if __name__ == "__main__":
    unittest.main()


class AuthorizeScan(unittest.TestCase):
    """Preclose pass 1, survivor 5: the hook's SCAN line handling, driven through
    expand_lane_shorthand.authorize_scan itself, not the grant store."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        scan_grant.SCAN_STATE_DIR = self.tmp / "grants"
        import expand_lane_shorthand  # noqa: PLC0415
        self.hook = expand_lane_shorthand

    def tearDown(self):
        scan_grant.SCAN_STATE_DIR = None

    def granted(self, session=SESSION):
        return scan_grant.live(REPO, "open-issues", session) is not None

    def test_a_first_line_scan_writes_one_grant_for_this_session(self):
        receipt = self.hook.authorize_scan("SCAN H open-issues: tune-up", SESSION)
        self.assertIn("may be scanned", receipt)
        self.assertTrue(self.granted())
        self.assertFalse(self.granted("sess-other"))

    def test_quoted_fenced_second_line_and_lowercase_write_nothing(self):
        for prompt in ("> SCAN H open-issues: quoted", "```\nSCAN H open-issues: fenced\n```",
                       "hello\nSCAN H open-issues: second", "scan H open-issues: lower",
                       "  SCAN H open-issues: indented"):
            receipt = self.hook.authorize_scan(prompt, SESSION)
            self.assertIn("REFUSED", receipt, prompt)
            self.assertFalse(self.granted(), prompt)

    def test_a_provenance_refusal_writes_nothing(self):
        with mock.patch.object(self.hook, "_provenance_refusal", return_value="not operator-typed"):
            receipt = self.hook.authorize_scan("SCAN H open-issues: x", SESSION)
        self.assertIn("not operator-typed", receipt)
        self.assertFalse(self.granted())

    def test_no_session_writes_nothing(self):
        self.assertIn("no session id", self.hook.authorize_scan("SCAN H open-issues: x", None))
        self.assertFalse(self.granted())

    def test_a_prompt_without_scan_returns_nothing(self):
        self.assertEqual(self.hook.authorize_scan("BATCH H1 please", SESSION), "")
