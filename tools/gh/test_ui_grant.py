#!/usr/bin/env python3
"""harmonic-forge#1274 (reforge, comment 6699562919): the UI grant's glob
meaning, its pending store, `record` (plan grants on the issue, merge grants on
the one matching PR), and `l1_post.py`'s mandatory `**UI change:**`
declaration with its brand-path cross-check (AC3, AC6, AC7).

The store and GitHub are faked; nothing reaches ~/.claude/state/ui-grant/ or
the network.

Kill checks (each a single-line guard):
- `record` posting a merge grant with zero matching PRs (`if digest and
  digest.startswith(prefix):` -> `if True:`) fails
  `test_merge_record_refuses_zero_matching_prs`; with two
  (`if len(candidates) == 1:` -> `if candidates:`) fails
  `test_merge_record_refuses_two_matching_prs`;
- reading a user-token status as the digest (`if status.get("login") ==
  STATUS_CREATOR:` -> `if True:`) fails `test_a_user_token_status_is_not_read_as_the_digest`;
- `find_record` dropping its author test fails
  `test_only_the_approvers_exact_record_counts`;
- the title/branch disagreement guard (`if branch_match and int(branch_match.group(1)) != number:`)
  removed fails `test_a_pr_whose_title_and_branch_disagree_refuses`;
- `l1_post` accepting a handoff with no `**UI change:**` line (the `fail` on an
  empty value) fails `test_main_refuses_a_handoff_with_no_ui_change_line`
  (through `main()`), as does deleting `validate_ui_brand` from main()'s steps;
- the `none` cross-check removed (`if named:` -> `if False:`) fails
  `test_none_naming_a_brand_path_is_refused`;
- the plan-record test (`if ui_grant.find_record(thread, key, "plan") is None:`)
  removed fails `test_a_declared_change_needs_a_recorded_plan_grant`.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))

import ui_grant as G  # noqa: E402

HRSE, FORGE, ISSUE, KEY = "vitalharmony/hrse", "vitalharmony/harmonic-forge", 2615, "H2615"
DIGEST = "ab" * 32
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
PREFIXES = {HRSE: "H", FORGE: "F", "kenekted/kenekted-platform": "K"}


def _done(stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, stderr)


def _lines(items) -> str:
    return "".join(json.dumps(i) + "\n" for i in items)


class GlobMeaning(unittest.TestCase):
    """Step 0: `matches()` agrees with `git ls-files -- ':(glob)<pattern>'`."""

    FILES = ("frontend/src/index.css", "frontend/src/token-pairings.toml",
             "frontend/src/components/ui/button.tsx", "frontend/src/components/ui/deep/x.tsx",
             "frontend/src/components/uix.tsx", "frontend/src/assets/logo.svg",
             "frontend/src/assets/a/b/c.png", "frontend/public/favicon.svg",
             "frontend/public/favicon.svg.bak", "frontend/e2e/visual/screens.spec.ts",
             "frontend/e2e/visual/screens.spec.ts-snapshots/home-linux.png",
             "frontend/e2e/visual/top.png", "extension/static/icons/i16.png",
             "frontend/src/a1.css", "frontend/src/ab.css", "png/x.png", "x.png")
    PATTERNS = ("frontend/src/index.css", "frontend/src/components/ui/**",
                "frontend/src/assets/**", "frontend/public/favicon.svg",
                "frontend/e2e/visual/**/*.png", "extension/static/icons/**",
                "frontend/src/components", "frontend/src/*.css", "**/*.png", "frontend/src/a?.css",
                "frontend/src/a[0-9].css", "frontend/src/a[!0-9].css", "frontend/**/c.png",
                "frontend/e2e/visual/*.png", "frontend/s**/index.css")

    def test_matches_agree_with_git_ls_files(self):
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(["git", "init", "-q", root], check=True)
            for name in self.FILES:
                path = Path(root, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x")
            subprocess.run(["git", "-C", root, "add", "-A"], check=True)
            for pattern in self.PATTERNS:
                with self.subTest(pattern=pattern):
                    out = subprocess.run(["git", "-C", root, "ls-files", "--", f":(glob){pattern}"],
                                         capture_output=True, text=True, check=True).stdout
                    want = sorted(out.split())
                    got = sorted(f for f in self.FILES if G.matches(f, [pattern]))
                    self.assertEqual(got, want)


class FakeGitHub:
    """`gh api` for the record path: opt-in reads, open PRs, statuses, comments,
    and the post/refetch pair `imc_waiver.post_and_confirm` makes."""

    def __init__(self, prs=None, statuses=None, opted=(HRSE,), fail_refetch=False, contents_error=False):
        self.prs = prs or []  # [{number, title, ref, sha}] on hrse
        self.statuses = statuses or {}  # sha -> [{login, description}]
        self.opted, self.fail_refetch, self.contents_error = opted, fail_refetch, contents_error
        self.posts: list[tuple[str, str]] = []

    def __call__(self, argv, **_kw):
        path = next((a for a in argv[2:] if a.startswith("repos/")), "")
        if "--method" in argv:
            body = Path(argv[argv.index("-F") + 1].split("@", 1)[1]).read_text()
            self.posts.append((path, body))
            return _done(f"https://github.com/{path.split('/issues/')[0][6:]}/issues/"
                         f"{path.split('/')[4]}#issuecomment-{len(self.posts)}")
        if "/issues/comments/" in path:
            return _done("" if self.fail_refetch else self.posts[-1][1])
        if "/contents/" in path:
            if self.contents_error:
                return _done("", 1, "gh: Server Error (HTTP 502)")
            repo = "/".join(path.split("/")[1:3])
            return _done("abc\n") if repo in self.opted else _done("", 1, "gh: Not Found (HTTP 404)")
        if "/pulls?" in path:
            return _done(_lines(self.prs) if path.startswith(f"repos/{HRSE}/") else "")
        if "/statuses?" in path:
            return _done(_lines(self.statuses.get(path.split("/")[4], [])))
        if path.endswith("/comments?per_page=100"):
            return _done(_lines([{"login": "vitalharmony", "body": "**UI change:** the dark logo is "
                                  "replaced; the old mark goes away", "html_url": "u"}]))
        raise AssertionError(f"unexpected gh call {argv}")


def actions_status(digest: str = DIGEST, login: str = "github-actions[bot]") -> dict:
    return {"login": login, "description": f"needs operator line UI <KEY> {digest[:16]}: <reason>; "
                                           f"digest {digest}"}


class _Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(G, "UI_STATE_DIR", Path(tmp.name)),
                        mock.patch.dict(os.environ, {"LANE": "1"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        import batch_auth  # noqa: PLC0415
        prefixes = mock.patch.object(batch_auth, "REPO_PREFIXES", PREFIXES)
        prefixes.start()
        self.addCleanup(prefixes.stop)

    def pending(self, mode="merge", digest=DIGEST[:16], key=KEY, issue=ISSUE, now=NOW, repo=HRSE):
        return G.write_pending(key, repo, issue, mode, digest if mode == "merge" else None,
                               "logo restored", "s1", f"UI {key}: logo restored", now)


class RecordPlan(_Store):
    """AC3: a plan grant's public record is posted on the issue and refetched."""

    def test_plan_record_posts_on_the_issue(self):
        self.pending("plan")
        gh = FakeGitHub()
        lines = G.record(KEY, now=NOW, runner=gh)
        self.assertEqual(len(gh.posts), 1)
        path, body = gh.posts[0]
        self.assertEqual(path, f"repos/{HRSE}/issues/{ISSUE}/comments")
        self.assertEqual(body.split("\n", 1)[0], "## UI plan grant — H2615")
        self.assertIn("**UI change:** the dark logo is replaced; the old mark goes away", body)
        grant = G.read_grant(KEY)
        self.assertIsNone(grant["plan"])
        self.assertEqual(grant["recorded"][0]["mode"], "plan")
        self.assertIn("recorded on vitalharmony/hrse#2615", lines[0])
        self.assertIsNotNone(G.find_record([{"login": "vitalharmony", "body": body}], KEY, "plan"))

    def test_a_failed_refetch_leaves_it_pending(self):
        self.pending("plan")
        with self.assertRaises(SystemExit):
            G.record(KEY, now=NOW, runner=FakeGitHub(fail_refetch=True))
        self.assertEqual(G.read_grant(KEY)["plan"]["status"], "pending")

    def test_record_needs_lane_one(self):
        self.pending("plan")
        with mock.patch.dict(os.environ, {"LANE": "2"}), self.assertRaisesRegex(SystemExit, "LANE"):
            G.record(KEY, now=NOW, runner=FakeGitHub())

    def test_an_expired_plan_never_blocks_a_merge_record(self):
        """Pass-2 survivor 1: modes are independent; an expired entry is dropped."""
        self.pending("plan", now=NOW - timedelta(days=G.TTL_DAYS + 1))
        self.pending("merge")
        gh = FakeGitHub(prs=[{"number": 2620, "title": "Logo (hrse#2615)", "ref": "fix/2615-logo",
                              "sha": "c" * 40}], statuses={"c" * 40: [actions_status()]})
        lines = G.record(KEY, now=NOW, runner=gh)
        self.assertIn("plan: DROPPED, expired", lines[0])
        self.assertIn("merge: recorded on vitalharmony/hrse#2620", lines[1])
        self.assertIsNone(G.read_grant(KEY)["plan"])


class RecordMerge(_Store):
    """Reforge step 5: a merge grant goes on the single open PR naming the key
    whose head digest starts with the typed prefix."""

    PR = {"number": 2620, "title": "Restore the logo (hrse#2615)", "ref": "fix/2615-logo", "sha": "c" * 40}

    def test_merge_record_posts_on_the_one_matching_pr(self):
        self.pending()
        other = {"number": 2621, "title": "Unrelated (hrse#2600)", "ref": "fix/2600-x", "sha": "d" * 40}
        gh = FakeGitHub(prs=[other, self.PR], statuses={"c" * 40: [actions_status()],
                                                        "d" * 40: [actions_status()]})
        lines = G.record(KEY, now=NOW, runner=gh)
        self.assertEqual([p for p, _ in gh.posts], [f"repos/{HRSE}/issues/2620/comments"])
        body = gh.posts[0][1]
        self.assertEqual(body.split("\n", 1)[0], "## UI grant — H2615")
        # The exact marker HRSE2's scripts/ui_approval.py reads; change both together.
        self.assertRegex(body, re.compile(
            r"(?m)^<!-- ui-grant key=H2615 mode=merge granted_at=\S* digest=" + DIGEST + r" -->$"))
        self.assertIn("recorded on vitalharmony/hrse#2620 for digest " + DIGEST, lines[0])
        self.assertEqual(G.read_grant(KEY)["recorded"][0]["head_sha"], "c" * 40)

    def test_merge_record_refuses_zero_matching_prs(self):
        self.pending(digest="cd" * 8)
        gh = FakeGitHub(prs=[self.PR], statuses={"c" * 40: [actions_status()]})
        lines = G.record(KEY, now=NOW, runner=gh)
        self.assertIn("merge: REFUSED, nothing posted: no open PR names H2615", lines[0])
        self.assertIn("vitalharmony/hrse#2620", lines[0])
        self.assertEqual(gh.posts, [])
        self.assertEqual(G.read_grant(KEY)["merge"]["status"], "pending")

    def test_merge_record_refuses_two_matching_prs(self):
        self.pending()
        twin = {**self.PR, "number": 2622, "sha": "e" * 40}
        gh = FakeGitHub(prs=[self.PR, twin], statuses={"c" * 40: [actions_status()],
                                                       "e" * 40: [actions_status()]})
        lines = G.record(KEY, now=NOW, runner=gh)
        self.assertIn("REFUSED, nothing posted: 2 open PRs name H2615", lines[0])
        self.assertEqual(gh.posts, [])

    def test_a_pr_whose_title_and_branch_disagree_refuses(self):
        """Pass-2 survivor 12's guard, kept for the record's PR mapping."""
        self.pending()
        wrong = {**self.PR, "title": "Logo (hrse#2615)", "ref": "feat/2600-other"}
        gh = FakeGitHub(prs=[wrong], statuses={"c" * 40: [actions_status()]})
        lines = G.record(KEY, now=NOW, runner=gh)
        self.assertIn("REFUSED", lines[0])
        self.assertIn("names #2615 but its branch", lines[0])
        self.assertEqual(gh.posts, [])

    def test_a_forge_key_maps_by_title_across_repos(self):
        """F1274's own shape: an hrse PR on branch feat/1274-... titled
        `(harmonic-forge#1274)` is F1274's, never H1274's."""
        pr = {"number": 2630, "title": "UI approval (harmonic-forge#1274)", "ref": "feat/1274-ui",
              "sha": "c" * 40}
        self.pending(key="F1274", issue=1274, repo=FORGE)
        gh = FakeGitHub(prs=[pr], statuses={"c" * 40: [actions_status()]})
        self.assertIn("recorded on vitalharmony/hrse#2630", G.record("F1274", now=NOW, runner=gh)[0])
        self.pending(key="H1274", issue=1274)
        gh = FakeGitHub(prs=[pr], statuses={"c" * 40: [actions_status()]})
        self.assertIn("REFUSED", G.record("H1274", now=NOW, runner=gh)[0])

    def test_a_user_token_status_is_not_read_as_the_digest(self):
        self.pending()
        gh = FakeGitHub(prs=[self.PR], statuses={"c" * 40: [actions_status(login="vitalharmony")]})
        lines = G.record(KEY, now=NOW, runner=gh)
        self.assertIn("digest not reported", lines[0])
        self.assertEqual(gh.posts, [])

    def test_an_unreadable_opt_in_read_refuses(self):
        self.pending()
        lines = G.record(KEY, now=NOW, runner=FakeGitHub(prs=[self.PR], contents_error=True))
        self.assertIn("REFUSED", lines[0])
        self.assertIn("HTTP 502", lines[0])

    def test_cli_exit_code_follows_refusals(self):
        self.pending(digest="cd" * 8)
        with mock.patch.object(G.subprocess, "run", FakeGitHub(prs=[self.PR], statuses={
                "c" * 40: [actions_status()]})), mock.patch.object(G.imc_waiver, "_now", return_value=NOW):
            self.assertEqual(G.main(["record", "--key", KEY]), 1)
        self.assertEqual(G.main(["status", "--key", KEY]), 0)


class FindRecord(unittest.TestCase):
    def test_only_the_approvers_exact_record_counts(self):
        grant = {"key": KEY, "repo": HRSE, "issue": ISSUE,
                 "plan": {"reason": "r", "granted_at": "t", "session_id": "s", "digest": None}}
        body = G.comment_body(grant, "plan", "x")
        self.assertIsNotNone(G.find_record([{"login": "vitalharmony", "body": body}], KEY, "plan"))
        for comment in ({"login": "kytxii", "body": body},
                        {"login": "vitalharmony", "body": "> " + body},
                        {"login": "vitalharmony", "body": body.replace("mode=plan", "mode=merge")},
                        {"login": "vitalharmony", "body": body.replace("H2615", "H2614")}):
            with self.subTest(comment=comment["body"][:30]):
                self.assertIsNone(G.find_record([comment], KEY, "plan"))


class BrandHandoff(unittest.TestCase):
    """AC6/AC7 reforged: a mandatory `**UI change:**` declaration that fails
    closed; the brand-path scanner is a cross-check behind `none`."""

    #: HRSE2's real `brand` list (frontend/ui-version.toml).
    BRAND = ["frontend/src/assets/**", "frontend/public/favicon.svg", "frontend/src/index.css",
             "frontend/src/token-pairings.toml", "frontend/src/components/ui/**",
             "extension/static/icons/**"]

    def body(self, row: str = "| frontend/src/pages/Home.tsx | 3 | Modify |",
             ui_change: str | None = "none") -> str:
        lead = f"**UI change:** {ui_change}\n" if ui_change is not None else ""
        return (f"## Handoff: hrse#2615\n\n**Scope:** logo\n**Next:** Lane 2\n{lead}\n"
                f"### Affected Files\n| File | Lines | Change |\n|---|---|---|\n{row}\n\n### Issue\nx\n")

    def check(self, body: str, comments=None, repo: str = HRSE):
        import l1_post  # noqa: PLC0415
        return l1_post.validate_ui_brand(body, repo, ISSUE, globs=self.BRAND,
                                         comments=[] if comments is None else comments)

    def refused(self, body: str, comments=None) -> str:
        with self.assertRaises(SystemExit) as caught:
            self.check(body, comments)
        return str(caught.exception)

    def test_a_handoff_with_no_ui_change_line_is_refused(self):
        self.assertIn("no filled `**UI change:**` lead line", self.refused(self.body(ui_change=None)))

    def test_placeholder_values_are_refused(self):
        for value in ("{what is replaced}", "<what is replaced, and what goes away>"):
            with self.subTest(value=value):
                self.assertIn("no filled", self.refused(self.body(ui_change=value)))

    def test_none_with_no_brand_path_passes(self):
        for value in ("none", "None.", "`none`", "**none**"):
            with self.subTest(value=value):
                self.assertIsNone(self.check(self.body(ui_change=value)))

    def test_none_naming_a_brand_path_is_refused(self):
        for row in ("| frontend/src/assets/cymagraph-small-dark.svg | 1-9 | Replace |",
                    "| frontend/src/index.css:40-52 | 40-52 | Modify |",
                    "| `frontend/src/index.css:40–52` | x | Modify |",
                    "| frontend/src/assets/ | dir | Add a mark |",
                    "| ./frontend/src | dir | Restyle |",
                    "| HRSE2 `frontend/public/favicon.svg` | whole | Replace |",
                    "| vitalharmony/hrse frontend/src/components/ui/button.tsx | 3 | Modify |",
                    "| frontend/src/assets/*.svg | all | Replace |",
                    "| extension/static/** | all | Replace |"):
            with self.subTest(row=row):
                self.assertIn("says `**UI change:** none` but names brand path",
                              self.refused(self.body(row)))

    def test_the_real_hrse_2614_handoff_with_none_is_refused(self):
        body = (HERE / "fixtures" / "hrse-2614-handoff.md").read_text().replace(
            "**Threat model:** good-faith\n", "**Threat model:** good-faith\n**UI change:** none\n", 1)
        reason = self.refused(body)
        for path in ("frontend/public/favicon.svg", "frontend/src/assets/cymagraph-small-dark.svg",
                     "frontend/src/index.css"):
            self.assertIn(path, reason)
        self.assertNotIn("scripts/generate_chladni_mark.py", reason)

    def test_the_template_lead_declares_none(self):
        template = (HERE.parents[1] / "templates" / "lane1-handoff.md").read_text()
        template = template[template.index("## Handoff:"):]  # a post starts at its heading
        import l1_post  # noqa: PLC0415
        match = G.UI_CHANGE_RE.search(l1_post.lead_region(template))
        self.assertIsNotNone(match)
        self.assertEqual(match.group("text").strip(), "none")

    def test_a_declared_change_needs_a_recorded_plan_grant(self):
        body = self.body("| frontend/src/assets/logo.svg | 1 | Replace |",
                         ui_change="the dark logo is replaced; the old mark goes away")
        self.assertIn("no recorded UI plan grant", self.refused(body, comments=[]))
        grant = {"key": KEY, "repo": HRSE, "issue": ISSUE,
                 "plan": {"reason": "r", "granted_at": "t", "session_id": "s", "digest": None}}
        record = G.comment_body(grant, "plan", "x")
        self.assertIn("no recorded UI plan grant",
                      self.refused(body, comments=[{"login": "kytxii", "body": record}]))
        self.assertIsNone(self.check(body, comments=[{"login": "vitalharmony", "body": record}]))

    def test_a_forge_handoff_naming_hrse2_brand_paths_is_refused(self):
        import l1_post  # noqa: PLC0415
        import batch_auth  # noqa: PLC0415
        toml = 'version = 1\ndigest = "x"\ncorpus = ["frontend/src/assets/**"]\nbrand = ["frontend/src/assets/**"]\n'
        reads: list[str] = []

        def fake_run(*args, **_kw):
            reads.append(args[2])
            if args[2].startswith("repos/vitalharmony/hrse/contents/"):
                return _done(toml)
            return _done("", 1, "gh: Not Found (HTTP 404)")

        with mock.patch.object(l1_post, "run", fake_run), \
                mock.patch.object(batch_auth, "REPO_PREFIXES", PREFIXES):
            for row in ("| HRSE2 frontend/src/assets/x.svg | new | Add |",
                        "| frontend/src/assets/x.svg | new | Add |"):
                with self.subTest(row=row), self.assertRaisesRegex(
                        SystemExit, r"names brand path.*frontend/src/assets/x\.svg \(vitalharmony/hrse\)"):
                    l1_post.validate_ui_brand(self.body(row), FORGE, 1274)
        self.assertFalse(any(r.startswith("repos/kenekted/") for r in reads))  # owner-bound

    def test_an_unreadable_version_file_refuses(self):
        import l1_post  # noqa: PLC0415
        result = _done("", 1, "gh: Server Error (HTTP 502)")
        with mock.patch.object(l1_post, "run", return_value=result), \
                self.assertRaisesRegex(SystemExit, "cannot read frontend/ui-version.toml"):
            l1_post.validate_ui_brand(self.body("| frontend/src/assets/x.svg | 1 | Add |"), HRSE, ISSUE)

    def test_a_repo_without_a_version_file_has_no_brand_paths(self):
        import l1_post  # noqa: PLC0415
        result = _done("", 1, "gh: Not Found (HTTP 404)")
        with mock.patch.object(l1_post, "run", return_value=result):
            self.assertEqual(l1_post.brand_globs(HRSE), [])
            self.assertIsNone(l1_post.validate_ui_brand(
                self.body("| frontend/src/assets/x.svg | 1 | Add |"), HRSE, ISSUE))

    def test_a_repo_outside_the_owner_is_unaffected(self):
        self.assertIsNone(self.check(self.body(ui_change=None), repo="kenekted/kenekted-platform"))


def _harness():
    import test_l1_post_dry_run as dry  # noqa: PLC0415
    return dry


class MainWiring(_harness().Harness):
    """Pass-2 survivor 11: the declaration is enforced through `l1_post.main()`,
    not only by calling `validate_ui_brand` directly."""

    QUIET = ("validate_handoff", "validate_plan_first_declared", "validate_plan_first_spec",
             "validate_board_fields_set", "validate_milestone_set",
             "validate_tooling_exception_labelled", "validate_pitch_inspected",
             "validate_te_lead_fields", "validate_lead")

    def run_handoff(self, text: str):
        dry = _harness()
        with contextlib.ExitStack() as stack:
            for name in self.QUIET:
                stack.enter_context(mock.patch.object(dry.L, name))
            stack.enter_context(mock.patch.object(dry.L, "issue_labels", return_value=set()))
            stack.enter_context(mock.patch.object(dry.L, "brand_globs", return_value=[]))
            argv = ("--repo", dry.REPO, "--issue", dry.ISSUE, "--kind", "handoff", "--sha", dry.SHA_A,
                    "--branch", "main", "--file", self.body(text), "--plan-first", "false")
            return self.run_main(*argv, "--dry-run")

    def test_main_refuses_a_handoff_with_no_ui_change_line(self):
        code, _, _ = self.run_handoff("## Handoff: F1115\n\n**Scope:** x\n**Next:** Lane 2\n")
        self.assertIsNotNone(code)
        self.assertIn("no filled `**UI change:**` lead line", code)

    def test_main_accepts_the_same_handoff_declaring_none(self):
        code, _, err = self.run_handoff("## Handoff: F1115\n\n**Scope:** x\n**Next:** Lane 2\n"
                                        "**UI change:** none\n")
        self.assertIsNone(code, err)


if __name__ == "__main__":
    unittest.main()
