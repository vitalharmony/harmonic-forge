#!/usr/bin/env python3
"""harmonic-forge#1036: a patch-identical rebase carries the Lane 3 PASS forward.

Git runs for real against a temporary bare origin plus a clone (the
`tools/lane/test_lane1_rebase.py` pattern); the GitHub reads (comments,
labels, CI) are stubbed. Run: `python3 tools/gh/test_carry_forward.py`."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "lane"))

import _carry_forward as cf  # noqa: E402
import gate_ci  # noqa: E402
import lane1_rebase  # noqa: E402
import watch_lane_posts as watch  # noqa: E402

L1_POST = _HERE / "l1_post.py"
REPO = "o/r"
ISSUE = 7
BRANCH = "feat/7-thing"
PASS_ID = 100

APP = "def f():\n    return 1\n"
PKG = '{\n  "name": "fe",\n  "version": "%s"\n}\n'
LOCK = ('{\n  "name": "fe",\n  "version": "%s",\n  "lockfileVersion": 3,\n  "requires": true,\n'
        '  "packages": {\n    "": {\n      "name": "fe",\n      "version": "%s"\n    },\n'
        '    "node_modules/x": {\n      "version": "%s"\n    }\n  }\n}\n')
NOTES = 'settings\n"version": "a"\n'


def sh(cwd, *args):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if proc.returncode:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc.stdout.strip()


def gate_body(verdict, sha, *, marker=True, footer_kind="gate-result", poster="LANE3"):
    prefix = (f"## Lane 3 Gate Results — {verdict}\n\n**Verdict:** {verdict}\n"
              f"Head-SHA: `{sha}`\nCI: green\n\nTCs ran.")
    digest = hashlib.sha256(prefix.encode()).hexdigest()
    sha_field = f" body-sha256={digest};" if marker else ""
    return f"{prefix}\n\n<!-- l1-post v1; kind={footer_kind}; posted-by={poster}; sha={sha};{sha_field} -->\n"


def cf_body(sha, from_sha, pass_id=PASS_ID, *, digest=None):
    prefix = f"## Gate carry-forward — F7\n\n**Target:** {sha}\n**Carried:** PASS {pass_id}\n**Next:** merge"
    digest = digest or hashlib.sha256(prefix.encode()).hexdigest()
    return (f"{prefix}\n\n<!-- l1-post v1; kind=carry-forward; sha={sha}; from-sha={from_sha}; "
            f"pass={pass_id}; body-sha256={digest}; checks=body-validation -->\n")


def fake_ci(mode="green"):
    """A `gate_ci` runner: `verify` is the one required check."""
    def run(cmd):
        target = cmd[2] if cmd[1] == "api" and cmd[2] != "--paginate" else cmd[3]
        if target.endswith("/protection"):
            return 0, '["verify"]'
        if "/statuses" in target:
            # harmonic-forge#1283: no commit statuses, so a missing `verify`
            # job still reads `absent`, not `unknown`.
            return 0, ""
        if "actions/runs?head_sha=" in target:
            if mode == "absent":
                return 0, ""
            return 0, json.dumps({"id": 1, "name": "ci", "status": "completed",
                                  "conclusion": "success", "started_at": "t"})
        if target.endswith("filter=latest"):
            job = {"green": {"name": "verify", "status": "completed", "conclusion": "success"},
                   "pending": {"name": "verify", "status": "in_progress", "conclusion": None},
                   "red": {"name": "verify", "status": "completed", "conclusion": "failure"},
                   "other": {"name": "lint", "status": "completed", "conclusion": "success"}}[mode]
            return 0, json.dumps({**job, "completed_at": "t", "started_at": "t"})
        return 1, f"unexpected {cmd}"
    return run


def stub_git(slug="https://github.com/o/r.git"):
    def git(*args, cwd=None):
        if args[:3] == ("remote", "get-url", "origin"):
            return subprocess.CompletedProcess(args, 0, slug + "\n", "")
        return cf._git(*args, cwd=cwd)
    return git


class _Fixture(unittest.TestCase):
    """main: base. Branch: the code change plus a version bump (from_sha).
    main then moves (its own version bump). The rebase is rebuilt by hand on
    main's tip, as `lane1-rebase` with F1035 would leave it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)],
                       check=True, capture_output=True)
        sh(self.repo, "checkout", "-q", "-b", "main")
        self.base = self.commit({"app.py": APP, "frontend/package.json": PKG % "1.0.0",
                                 "frontend/package-lock.json": LOCK % ("1.0.0", "1.0.0", "9.9.9"),
                                 "other.txt": "x\n", "notes.txt": NOTES, "design.toml": "gen 1\n"},
                                "base")
        sh(self.repo, "push", "-q", "origin", "main")
        self.from_sha = self.branch_on(self.base, {
            "app.py": APP.replace("1", "2"), "frontend/package.json": PKG % "1.0.1",
            "frontend/package-lock.json": LOCK % ("1.0.1", "1.0.1", "9.9.9")})
        sh(self.repo, "push", "-q", "origin", f"{self.from_sha}:refs/heads/{BRANCH}")
        sh(self.repo, "checkout", "-q", "main")
        self.main = self.commit({"other.txt": "y\n", "frontend/package.json": PKG % "1.0.1",
                                 "frontend/package-lock.json": LOCK % ("1.0.1", "1.0.1", "9.9.9"),
                                 "design.toml": "gen 2\n"}, "main moves")
        sh(self.repo, "push", "-q", "origin", "main")
        self.labels = {"bug"}
        self.comments = [{"id": 50, "body": "## L2D\n\n<!-- l1-post v1; kind=completion; posted-by=LANE2 -->"}]
        self.set_pass(gate_body("PASS", self.from_sha))

    def commit(self, files, msg):
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            sh(self.repo, "add", name)
        sh(self.repo, "commit", "-q", "-m", msg)
        return sh(self.repo, "rev-parse", "HEAD")

    def branch_on(self, base, files, *commits):
        sh(self.repo, "checkout", "-q", "--detach", base)
        sha = self.commit(files, "feature")
        for extra in commits:
            sha = self.commit(extra, "more")
        return sha

    def rebase(self, files=None, *extra_commits, push=True):
        """The rebased branch on main's tip; pushed as origin/<branch>."""
        files = files or {"app.py": APP.replace("1", "2"), "frontend/package.json": PKG % "1.0.2"}
        sha = self.branch_on(self.main, files, *extra_commits)
        if push:
            sh(self.repo, "push", "-q", "-f", "origin", f"{sha}:refs/heads/{BRANCH}")
        sh(self.repo, "checkout", "-q", "main")
        return sha

    def set_pass(self, body, created="2026-10-17T10:00:00Z", updated="2026-10-17T10:00:00Z"):
        comment = {"id": PASS_ID, "body": body}
        if created is not None:
            comment["created_at"] = created
        if updated is not None:
            comment["updated_at"] = updated
        self.comments = [c for c in self.comments if c["id"] != PASS_ID] + [comment]

    def validate(self, sha, *, ci="green", excluded=frozenset(), git=None, from_sha=None, labels=None):
        return cf.validate(REPO, ISSUE, PASS_ID, from_sha or self.from_sha, sha, BRANCH,
                           fake_ci(ci), git or stub_git(), cwd=self.repo, comments=self.comments,
                           labels=self.labels if labels is None else labels, excluded=set(excluded))

    def assertRefused(self, refusals, needle, wait=False):
        self.assertTrue(any(needle in r for r in refusals), f"{needle!r} not in {refusals}")
        if wait:
            self.assertTrue(all(r.wait for r in refusals), refusals)


class CarryForwardValidateTests(_Fixture):
    def test_hrse2405_shape_version_only_difference_is_accepted(self):
        self.assertEqual(self.validate(self.rebase()), [])

    def test_one_changed_code_line_is_refused(self):
        sha = self.rebase({"app.py": APP.replace("1", "3"), "frontend/package.json": PKG % "1.0.2"})
        self.assertRefused(self.validate(sha), "is not the gated change")

    def test_fail_result_is_refused(self):
        self.set_pass(gate_body("FAIL", self.from_sha))
        self.assertRefused(self.validate(self.rebase()), "not PASS")

    def test_blocked_result_is_refused(self):
        self.set_pass(gate_body("BLOCKED", self.from_sha))
        self.assertRefused(self.validate(self.rebase()), "not PASS")

    def test_newer_gate_report_is_refused(self):
        self.comments.append({"id": PASS_ID + 5, "body": gate_body("FAIL", self.from_sha)})
        self.assertRefused(self.validate(self.rebase()), "newer gate report")

    def test_pending_ci_is_a_wait_refusal(self):
        self.assertRefused(self.validate(self.rebase(), ci="pending"), "is pending", wait=True)

    def test_absent_ci_is_a_wait_refusal(self):
        self.assertRefused(self.validate(self.rebase(), ci="absent"), "is absent", wait=True)

    def test_a_bare_404_repo_waits_with_no_second_read_and_says_a_retry_will_not_help(self):
        """harmonic-forge#1263, ruling A as narrowed: `check_ci` also runs right
        before a merge, so a green `verify` on such a repo is still no all-clear."""
        import test_gate_ci as tg
        inner = tg.fake_gh(checks=[tg.check("verify")])
        calls = []

        def run(cmd):
            calls.append(cmd)
            if any(a.endswith("/protection") for a in cmd):
                return 1, "gh: Not Found (HTTP 404)\n"
            if cmd[-2:] == ["--jq", ".protected"]:
                return 0, "false\n"
            return inner(cmd)
        refusals = cf.check_ci(REPO, "a" * 40, run=run)
        self.assertEqual(len(refusals), 1)
        self.assertTrue(refusals[0].wait)
        self.assertIn("required checks", refusals[0])
        self.assertIn("ready-for-l3", refusals[0])
        self.assertEqual(len(calls), 1)

    def test_unreadable_required_checks_is_a_retry_refusal(self):
        """F1036 epoch 2 survivor 2: no fallback to the observed runs."""
        with mock.patch.object(gate_ci, "required_checks", return_value=None):
            self.assertRefused(self.validate(self.rebase()), "required checks", wait=True)

    def test_unreadable_comments_is_a_retry_refusal(self):
        """F1036 epoch 2 survivor 1: a failed read is a retry, never 'not covered'."""
        with mock.patch.object(watch, "_fetch_all_comments", return_value=None):
            self.assertRefused(cf.validate(REPO, ISSUE, PASS_ID, self.from_sha, self.rebase(), BRANCH,
                                           fake_ci(), stub_git(), cwd=self.repo, labels=self.labels,
                                           excluded=set()), "(retry)", wait=True)
            self.assertEqual(cf.check(REPO, ISSUE, BRANCH, fake_ci(), stub_git(), cwd=self.repo,
                                      labels=self.labels, excluded=set())[0], cf.WAIT_EXIT)

    def test_red_ci_is_a_substantive_refusal(self):
        refusals = self.validate(self.rebase(), ci="red")
        self.assertRefused(refusals, "is red")
        self.assertFalse(any(r.wait for r in refusals))

    def test_4cb68327_shape_required_verify_absent_is_refused(self):
        """Other workflows green, the required `verify` never ran."""
        self.assertRefused(self.validate(self.rebase(), ci="other"), "has not run its required check(s): verify")

    def test_an_empty_required_set_requires_verify(self):
        """harmonic-forge#1103 reforge fix 1: unprotected (set()) -> verify."""
        import test_gate_ci as tg
        with mock.patch.object(gate_ci, "required_checks", return_value=set()):
            self.assertTrue(cf.check_ci(REPO, "a" * 40, run=tg.fake_gh(checks=[tg.check("lint")])))
            self.assertEqual(cf.check_ci(REPO, "a" * 40, run=tg.fake_gh(
                checks=[tg.check("lint"), tg.check("verify")])), [])

    def test_ci_conclusion_is_called_with_required(self):
        with mock.patch.object(gate_ci, "required_checks", return_value={"verify"}) as req, \
                mock.patch.object(gate_ci, "_read_check_runs", return_value=([], None)), \
                mock.patch.object(gate_ci, "_status_records", return_value=([], None)), \
                mock.patch.object(gate_ci, "_judge_runs", return_value=("green", "verify:success")) as ci:
            self.assertEqual(cf.check_ci(REPO, "a" * 40, run="R"), [])
        req.assert_called_once_with(REPO, run="R")
        self.assertEqual(ci.call_args.args[2], {"verify"})

    def test_edited_pass_body_is_refused(self):
        self.set_pass(gate_body("PASS", self.from_sha).replace("TCs ran.", "TCs ran, edited."))
        self.assertRefused(self.validate(self.rebase()), "edited after it was posted")

    def test_body_sha_on_its_own_footer_line_is_refused(self):
        """Fix 1: a marker the verifier cannot find (no DOTALL there) is absent."""
        body = gate_body("PASS", self.from_sha).replace(" body-sha256=", "\nbody-sha256=")
        self.set_pass(body.replace("TCs ran.", "TCs ran, edited."))
        self.assertRefused(self.validate(self.rebase()), "no body-sha256 marker")

    def test_pass_edited_after_posting_is_refused(self):
        """Fix 6: the unkeyed hash is recomputable; GitHub's updated_at is not."""
        self.set_pass(gate_body("PASS", self.from_sha), updated="2026-10-17T10:05:00Z")
        self.assertRefused(self.validate(self.rebase()), "updated_at 2026-10-17T10:05:00Z is after")

    def test_pass_with_equal_timestamps_is_accepted(self):
        self.set_pass(gate_body("PASS", self.from_sha), updated="2026-10-17T10:00:00Z")
        self.assertEqual(self.validate(self.rebase()), [])

    def test_pass_without_timestamps_is_refused(self):
        self.set_pass(gate_body("PASS", self.from_sha), created=None, updated=None)
        self.assertRefused(self.validate(self.rebase()), "no readable created_at/updated_at")

    def test_same_replacement_in_another_function_is_refused(self):
        """Fix 5: the same replacement in another function is a different tree (sticky REFORGE)."""
        block = "    x = 0\n    x = 0\n    x = 0\n    return 1\n    pass\n    pass\n    pass\n"
        mod = f"def f():\n{block}\n\ndef g():\n{block}"
        sh(self.repo, "checkout", "-q", "main")
        start = self.commit({"mod.py": mod}, "mod")
        sh(self.repo, "push", "-q", "origin", "main")
        in_f = mod.replace("return 1", "return 2", 1)
        in_g = "return 2".join(mod.rsplit("return 1", 1))
        self.from_sha = self.branch_on(start, {"mod.py": in_f})
        sh(self.repo, "checkout", "-q", "main")
        self.main = self.commit({"other.txt": "z\n"}, "main moves again")
        sh(self.repo, "push", "-q", "origin", "main")
        self.set_pass(gate_body("PASS", self.from_sha))
        self.assertEqual(self.validate(self.rebase({"mod.py": in_f})), [])
        self.assertRefused(self.validate(self.rebase({"mod.py": in_g})), "is not the gated change")

    def test_markerless_pass_is_refused(self):
        self.set_pass(gate_body("PASS", self.from_sha, marker=False))
        self.assertRefused(self.validate(self.rebase()), "no body-sha256 marker")

    def test_pass_on_another_sha_is_refused(self):
        self.set_pass(gate_body("PASS", "0123456789abcdef0123456789abcdef01234567"))
        self.assertRefused(self.validate(self.rebase()), "not --from-sha")

    def test_pass_not_posted_by_lane3_is_refused(self):
        """F1036 epoch 2 survivor 1: a gate-report-shaped PASS from another
        lane, or with no poster, is never a Lane 3 PASS to carry."""
        for poster in ("LANE2", "LANE1", "LANE-unset"):
            with self.subTest(poster=poster):
                self.set_pass(gate_body("PASS", self.from_sha, poster=poster))
                self.assertRefused(self.validate(self.rebase()), "not posted by Lane 3")

    def test_pass_comment_not_on_issue_is_refused(self):
        self.comments = [c for c in self.comments if c["id"] != PASS_ID]
        self.assertRefused(self.validate(self.rebase()), "is not a comment on this issue")

    def test_non_gate_report_is_refused(self):
        self.set_pass("## Some discussion\n\nPASS\n")
        self.assertRefused(self.validate(self.rebase()), "is not a Lane 3 gate report")

    def test_stale_sha_not_the_branch_head_is_refused(self):
        stale = self.rebase()
        self.rebase({"app.py": APP.replace("1", "2"), "frontend/package.json": PKG % "1.0.3"})
        self.assertRefused(self.validate(stale), "is not origin/feat/7-thing's head")

    def test_sha_not_containing_main_is_refused(self):
        sh(self.repo, "push", "-q", "-f", "origin", f"{self.from_sha}:refs/heads/{BRANCH}")
        self.assertRefused(self.validate(self.from_sha), "does not contain origin/main")

    def test_tooling_exception_is_refused(self):
        self.labels = {"tooling-exception"}
        self.assertRefused(self.validate(self.rebase()), "tooling-exception")

    def test_indentation_only_python_change_is_refused(self):
        sha = self.rebase({"app.py": APP.replace("    return 1", "     return 2"),
                           "frontend/package.json": PKG % "1.0.2"})
        self.assertRefused(self.validate(sha), "is not the gated change")

    def test_version_change_in_a_non_package_json_file_is_refused(self):
        self.from_sha = self.branch_on(self.base, {
            "app.py": APP.replace("1", "2"), "notes.txt": NOTES.replace('"a"', '"b"')})
        sh(self.repo, "checkout", "-q", "main")
        self.set_pass(gate_body("PASS", self.from_sha))
        sha = self.rebase({"app.py": APP.replace("1", "2"), "notes.txt": NOTES.replace('"a"', '"c"')})
        self.assertRefused(self.validate(sha), "is not the gated change")

    def test_lock_dependency_version_change_is_refused(self):
        sha = self.rebase({"app.py": APP.replace("1", "2"), "frontend/package.json": PKG % "1.0.2",
                           "frontend/package-lock.json": LOCK % ("1.0.2", "1.0.2", "9.9.8")})
        self.assertRefused(self.validate(sha), "is not the gated change")

    def test_wrong_checkout_repo_is_refused(self):
        refusals = self.validate(self.rebase(), git=stub_git("https://github.com/o/other.git"))
        self.assertRefused(refusals, "not o/r")

    def test_binary_change_is_refused(self):
        sha = self.branch_on(self.main, {"app.py": APP.replace("1", "2"),
                                         "frontend/package.json": PKG % "1.0.2"})
        (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02")
        sh(self.repo, "add", "blob.bin")
        sh(self.repo, "commit", "-q", "-m", "bin")
        sha = sh(self.repo, "rev-parse", "HEAD")
        sh(self.repo, "push", "-q", "-f", "origin", f"{sha}:refs/heads/{BRANCH}")
        sh(self.repo, "checkout", "-q", "main")
        self.assertRefused(self.validate(sha), "is not the gated change")

    def test_every_refusal_is_named_not_only_the_first(self):
        self.set_pass(gate_body("FAIL", self.from_sha))
        self.labels = {"tooling-exception"}
        refusals = self.validate(self.rebase(), ci="absent")
        for needle in ("not PASS", "is absent", "tooling-exception"):
            self.assertRefused(refusals, needle)
        self.assertFalse(all(r.wait for r in refusals))

    def test_excluded_none_reads_the_declared_generated_files(self):
        with mock.patch.object(lane1_rebase, "regenerate_config",
                               return_value=({"design.toml": "gen"}, None)) as cfg:
            refusals = cf.validate(REPO, ISSUE, PASS_ID, self.from_sha, self.rebase(), BRANCH,
                                   fake_ci(), stub_git(), cwd=self.repo, comments=self.comments,
                                   labels=self.labels)
        cfg.assert_called_once()
        self.assertEqual(refusals, [])


class DeclaredGeneratedRebaseTests(_Fixture):
    """Fix 2: the REBASED diff touches a declared generated file (plus a
    regeneration commit), read through declared_generated(), not `excluded=`."""

    def run_declared(self, app):
        sha = self.rebase({"app.py": app, "frontend/package.json": PKG % "1.0.2",
                           "design.toml": "gen 2 plus branch\n"},
                          {"design.toml": "gen 3 regenerated\n"})
        with mock.patch.object(lane1_rebase, "regenerate_config",
                               return_value=({"design.toml": "gen"}, None)):
            return cf.validate(REPO, ISSUE, PASS_ID, self.from_sha, sha, BRANCH, fake_ci(),
                               stub_git(), cwd=self.repo, comments=self.comments, labels=self.labels)

    def test_rebased_diff_touching_declared_file_is_refused(self):
        """Gap 5, option A (operator, 2026-10-19): a declared generated file that
        differs refuses and goes back through Lane 3; it is never filtered out."""
        self.assertRefused(self.run_declared(APP.replace("1", "2")), "is not the gated change")

    def test_rebased_diff_touching_declared_file_plus_code_line_is_refused(self):
        self.assertRefused(self.run_declared(APP.replace("1", "3")), "is not the gated change")


class RegenerationAmendmentTests(_Fixture):
    """6229366936's regeneration amendment, superseded for generated files by
    gap 5 option A: a regeneration commit refuses; so does one more code line."""

    def regen_rebase(self, app):
        return self.rebase(
            {"app.py": app, "frontend/package.json": PKG % "1.0.2",
             "frontend/package-lock.json": LOCK % ("1.0.2", "1.0.2", "9.9.9")},
            {"design.toml": "gen 3 regenerated\n"})

    def test_regeneration_commit_is_refused_even_when_declared(self):
        """Gap 5, option A: the regeneration commit changes a declared file, so
        the head is not the gated tree; the lock's version keys alone would pass."""
        sha = self.regen_rebase(APP.replace("1", "2"))
        self.assertRefused(self.validate(sha, excluded={"design.toml"}), "is not the gated change")

    def test_regeneration_without_declaration_is_refused(self):
        sha = self.regen_rebase(APP.replace("1", "2"))
        self.assertRefused(self.validate(sha), "is not the gated change")

    def test_regeneration_plus_one_code_line_is_refused(self):
        sha = self.regen_rebase(APP.replace("1", "3"))
        self.assertRefused(self.validate(sha, excluded={"design.toml"}), "is not the gated change")


class MergeTimeCheckTests(_Fixture):
    """Operator ruling 6219929298 Q1: the merge step re-runs every check
    against the newest carry-forward footer's own fields."""

    def check(self, **kw):
        return cf.check(REPO, ISSUE, BRANCH, fake_ci(kw.pop("ci", "green")), stub_git(),
                        cwd=self.repo, comments=self.comments, labels=self.labels,
                        excluded=set(), **kw)

    def test_hrse2405_shape_passes(self):
        sha = self.rebase()
        self.comments.append({"id": PASS_ID + 1, "body": cf_body(sha, self.from_sha)})
        self.assertEqual(self.check(), (0, []))

    def test_hand_typed_footer_not_patch_identical_is_refused(self):
        sha = self.rebase({"app.py": APP.replace("1", "3"), "frontend/package.json": PKG % "1.0.2"})
        self.comments.append({"id": PASS_ID + 1, "body": cf_body(sha, self.from_sha)})
        code, refusals = self.check()
        self.assertEqual(code, cf.REFUSE_EXIT)
        self.assertRefused(refusals, "is not the gated change")

    def test_head_moved_after_carry_forward_is_refused(self):
        carried = self.rebase()
        self.comments.append({"id": PASS_ID + 1, "body": cf_body(carried, self.from_sha)})
        self.rebase({"app.py": APP.replace("1", "2"), "frontend/package.json": PKG % "1.0.4"})
        code, refusals = self.check()
        self.assertEqual(code, cf.REFUSE_EXIT)
        self.assertRefused(refusals, "is not origin/feat/7-thing's head")

    def test_main_moved_after_carry_forward_exits_base(self):
        """Fix 3: a stale base with patch-identical code is BASE_EXIT, not REFUSE_EXIT."""
        carried = self.rebase()
        self.comments.append({"id": PASS_ID + 1, "body": cf_body(carried, self.from_sha)})
        self.main = self.commit({"other.txt": "w\n"}, "main moves after the carry-forward")
        sh(self.repo, "push", "-q", "origin", "main")
        code, refusals = self.check()
        self.assertEqual(code, cf.BASE_EXIT)
        self.assertEqual(cf.BASE_EXIT, 4)
        self.assertRefused(refusals, "does not contain origin/main")

    def test_main_moved_and_code_changed_is_refused(self):
        carried = self.rebase({"app.py": APP.replace("1", "3"), "frontend/package.json": PKG % "1.0.2"})
        self.comments.append({"id": PASS_ID + 1, "body": cf_body(carried, self.from_sha)})
        self.main = self.commit({"other.txt": "w\n"}, "main moves after the carry-forward")
        sh(self.repo, "push", "-q", "origin", "main")
        self.assertEqual(self.check()[0], cf.REFUSE_EXIT)

    def test_pending_ci_exits_wait(self):
        sha = self.rebase()
        self.comments.append({"id": PASS_ID + 1, "body": cf_body(sha, self.from_sha)})
        self.assertEqual(self.check(ci="pending")[0], cf.WAIT_EXIT)

    def test_no_carry_forward_exits_usage(self):
        self.assertEqual(self.check()[0], cf.USAGE_EXIT)

    def test_newer_pass_supersedes_the_carry_forward(self):
        """F1036 preclose survivor 3: a gate report newer than the newest
        carry-forward gates the head directly, so no carry-forward governs it."""
        sha = self.rebase()
        self.comments.append({"id": PASS_ID + 1, "body": cf_body(sha, self.from_sha)})
        self.comments.append({"id": PASS_ID + 2, "body": gate_body("PASS", "d" * 40)})
        code, refusals = self.check()
        self.assertEqual(code, cf.USAGE_EXIT)
        self.assertIn("newer gate report supersedes it", refusals[0])

    def test_a_quoted_carry_forward_footer_is_not_read(self):
        sha = self.rebase()
        quoted = "\n".join("> " + line for line in cf_body(sha, self.from_sha).splitlines())
        self.comments.append({"id": PASS_ID + 1, "body": "## note\n\n" + quoted})
        self.assertEqual(self.check()[0], cf.USAGE_EXIT)


class LaneOneOnlyAndArgsTests(unittest.TestCase):
    """l1_post.py argument shape for --kind carry-forward (before any git/GitHub read)."""

    def run_l1(self, extra, lane):
        env = {k: v for k, v in os.environ.items() if k != "LANE"}
        if lane is not None:
            env["LANE"] = lane
        return subprocess.run([sys.executable, str(L1_POST), "--issue", "1", "--sha", "HEAD",
                               "--branch", "feat/1-x", "--file", "/nonexistent", *extra],
                              capture_output=True, text=True, env=env)

    CF = ["--kind", "carry-forward", "--from-sha", "a" * 40, "--pass-comment", "5"]

    def test_lane_2_is_refused(self):
        result = self.run_l1(self.CF, "2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LANE is '2'", result.stderr)

    def test_lane_unset_is_refused(self):
        result = self.run_l1(self.CF, None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LANE is None", result.stderr)

    def test_from_sha_on_kind_ae_is_refused(self):
        result = self.run_l1(["--kind", "ae", "--from-sha", "a" * 40], "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("valid only for --kind carry-forward", result.stderr)

    def test_ack_no_pr_required_with_carry_forward_is_refused(self):
        result = self.run_l1(self.CF + ["--ack-no-pr-required", "because"], "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--ack-no-pr-required is not valid with --kind carry-forward", result.stderr)

    def test_carry_forward_requires_from_sha_and_pass_comment(self):
        result = self.run_l1(["--kind", "carry-forward"], "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --from-sha and --pass-comment", result.stderr)

    def test_carry_forward_is_a_kind_with_its_lead_fields(self):
        sys.path.insert(0, str(_HERE))
        import l1_post  # noqa: PLC0415
        self.assertEqual(l1_post.LEAD_FIELDS["carry-forward"], ("Target", "Carried", "Next"))

    def test_ack_overlap_with_carry_forward_is_accepted(self):
        """harmonic-forge#1297 AC1 (CLI form): the refusal text is absent. Exit
        code is not asserted -- `--file /nonexistent` exits non-zero later."""
        result = self.run_l1(self.CF + ["--ack-overlap", "H2531 rebases after this merges"], "1")
        self.assertNotIn("--ack-overlap is not valid", result.stderr)

    def test_the_other_refused_flags_are_still_refused(self):
        """harmonic-forge#1297 AC3: every other refused flag still refuses."""
        cases = {"--ack-no-pr-required": ["--ack-no-pr-required", "because"],
                 "--prod-run": ["--prod-run", "count-label=Person"],
                 "--mutates-live": ["--mutates-live"],
                 "--spec-comment": ["--spec-comment", "9"],
                 "--cross-repo": ["--cross-repo"]}
        for flag, extra in cases.items():
            with self.subTest(flag=flag):
                result = self.run_l1(self.CF + extra, "1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{flag} is not valid with --kind carry-forward", result.stderr)


class CarryForwardOverlapTests(unittest.TestCase):
    """harmonic-forge#1297: a carry-forward may acknowledge a sibling-worktree
    overlap. The real `world_checks` runs; only its git/GitHub reads are patched."""

    SIBLING = "feat/9-sibling"
    REASON = "H9 rebases onto this after it merges"
    TARGET, FROM = "b" * 40, "a" * 40
    BODY = (f"## Gate carry-forward — F7\n\n**Target:** {'b' * 40}\n"
            f"**Carried:** PASS {PASS_ID}\n**Next:** merge\n")

    def setUp(self):
        import l1_post  # noqa: PLC0415
        self.l1 = l1_post

    def args(self, **overrides):
        import argparse  # noqa: PLC0415
        values = {"kind": "carry-forward", "from_sha": self.FROM, "pass_comment": PASS_ID,
                  "ack_no_pr_required": None, "ack_overlap": None, "prod_run": None,
                  "mutates_live": False, "spec_comment": None, "cross_repo": False}
        values.update(overrides)
        return argparse.Namespace(**values)

    def post(self, ack_overlap):
        l1 = self.l1
        captured = {}

        def fake_run(*cmd, **_kwargs):
            out = f"{self.TARGET}\trefs/heads/{BRANCH}\n" if "ls-remote" in cmd else "c" * 40 + "\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")

        def fake_comment_body(repo, issue, body):
            captured["body"] = body
            return f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-1", 1

        with mock.patch.object(l1, "issue_is_open"), \
                mock.patch.object(l1, "run", fake_run), \
                mock.patch.object(l1, "changed_files", return_value={"tools/x.py"}), \
                mock.patch.object(l1, "active_worktree_branches", return_value={BRANCH, "main", self.SIBLING}), \
                mock.patch.object(l1, "sibling_overlaps", return_value=[(self.SIBLING, {"tools/x.py"})]), \
                mock.patch.object(l1, "comment_body", fake_comment_body), \
                mock.patch.object(l1, "write_receipt"), \
                mock.patch.object(l1, "_record_attrib"), \
                mock.patch.object(l1, "_tasklist_owed", None), \
                mock.patch.object(l1, "validate_sticky_wicket"):
            l1.post_kind(REPO, ISSUE, "carry-forward", self.BODY, self.TARGET, BRANCH,
                         ack_overlap=ack_overlap, carry_forward=(self.FROM, PASS_ID))
        return captured["body"]

    def test_ack_overlap_passes_argument_validation(self):
        """AC1: a non-empty --ack-overlap with --from-sha/--pass-comment does not fail."""
        with mock.patch.dict(os.environ, {"LANE": "1"}):
            self.l1.validate_carry_forward_args(self.args(ack_overlap=self.REASON))

    def test_acknowledged_overlap_posts_with_the_override_section(self):
        """AC1: the posted body names the sibling and the reason."""
        body = self.post(self.REASON)
        self.assertIn("### Sibling-overlap override (operator-acknowledged)", body)
        self.assertIn(self.SIBLING, body)
        self.assertIn(self.REASON, body)
        self.assertIn("kind=carry-forward;", body)

    def test_unacknowledged_overlap_still_refuses(self):
        """AC2: without --ack-overlap an overlapping sibling refuses the post."""
        with self.assertRaises(SystemExit) as raised:
            self.post(None)
        self.assertIn(f"active sibling branch {self.SIBLING!r} overlaps target files", str(raised.exception))

    def test_override_is_covered_by_the_carry_forward_digest(self):
        """AC4: the body-sha256 covers the override, so lane1-rebase trusts it."""
        body = self.post(self.REASON)
        self.assertTrue(lane1_rebase._carry_forward_intact({"body": body}))
        self.assertEqual(lane1_rebase._carry_forward_fields(body)["sha"], self.TARGET)


class PostingGuardWiringTests(unittest.TestCase):
    """F1036 preclose survivor 1: `l1_post.main` runs `_carry_forward.validate`
    and refuses before `post_kind` when it returns a refusal."""

    def test_a_validate_refusal_stops_the_post(self):
        import l1_post  # noqa: PLC0415
        argv = ["l1_post.py", "--kind", "carry-forward", "--issue", "7", "--sha", "b" * 40,
                "--branch", "feat/7-thing", "--file", "/x", "--from-sha", "a" * 40,
                "--pass-comment", str(PASS_ID)]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"LANE": "1"}), \
                mock.patch.object(l1_post, "resolve_repo", return_value=REPO), \
                mock.patch.object(l1_post, "apply_project_identity"), \
                mock.patch.object(l1_post, "resolve_sha", return_value="b" * 40), \
                mock.patch.object(l1_post, "regular_body", return_value="body"), \
                mock.patch.object(l1_post, "_collect"), \
                mock.patch.object(cf, "validate", return_value=[cf.Refusal("not the gated change")]) as validate, \
                mock.patch.object(l1_post, "post_kind") as post_kind:
            with self.assertRaises(SystemExit) as raised:
                l1_post.main()
        self.assertNotEqual(raised.exception.code, 0)
        validate.assert_called_once()
        post_kind.assert_not_called()


class BeltRoutingTests(unittest.TestCase):
    def test_carry_forward_is_owned_by_lane_1_and_queues_nothing(self):
        self.assertEqual(watch.KIND_OWNER["carry-forward"], "l1")
        for lane, kinds in watch.QUEUE_KINDS.items():
            self.assertNotIn("carry-forward", kinds, lane)
        self.assertTrue(watch._owed_to("l1", "carry-forward"))
        self.assertFalse(watch._owed_to("l2", "carry-forward"))
        self.assertFalse(watch._owed_to("l3", "carry-forward"))
        self.assertEqual(watch._classify(cf_body("a" * 40, "b" * 40)), ("l1", "carry-forward"))


class SecondRebaseTests(unittest.TestCase):
    """Assumption 4: after a carry-forward, lane1-rebase sees the branch as
    passed on the carried head, so a second rebase is accepted."""

    L2D = "## L2D\n\n<!-- l1-post v1; kind=completion; posted-by=LANE2 -->"
    GATED, FIRST, SECOND = "1" * 40, "2" * 40, "3" * 40

    def marker(self, *bodies):
        w = lane1_rebase._watch()
        with mock.patch.object(w, "_fetch_all_comments", return_value=[{"body": b} for b in bodies]), \
                mock.patch.object(lane1_rebase, "repo_slug", return_value="o/r"):
            return lane1_rebase.newest_lane_marker("/x", 7)

    def test_carry_forward_moves_the_pass_to_its_sha(self):
        self.assertEqual(self.marker(self.L2D, gate_body("PASS", self.GATED),
                                     cf_body(self.FIRST, self.GATED)), f"l3.pass:{self.FIRST}")

    def test_second_carry_forward_chains_from_the_gated_sha(self):
        self.assertEqual(self.marker(self.L2D, gate_body("PASS", self.GATED),
                                     cf_body(self.FIRST, self.GATED), cf_body(self.SECOND, self.GATED)),
                         f"l3.pass:{self.SECOND}")

    def test_carry_forward_from_another_sha_does_not_move_the_pass(self):
        self.assertEqual(self.marker(self.L2D, gate_body("PASS", self.GATED),
                                     cf_body(self.FIRST, "9" * 40)), f"l3.pass:{self.GATED}")

    def test_edited_carry_forward_does_not_move_the_pass(self):
        """F1036 preclose survivor 2: a carry-forward whose body-sha256 does
        not match its body is not trusted."""
        self.assertEqual(self.marker(self.L2D, gate_body("PASS", self.GATED),
                                     cf_body(self.FIRST, self.GATED, digest="0" * 64)),
                         f"l3.pass:{self.GATED}")

    def test_a_newer_pass_resets_the_chain(self):
        """F1036 epoch 2 survivor 2: a carry-forward from the OLD gated SHA after
        a newer PASS does not move the newer PASS."""
        newer = "4" * 40
        self.assertEqual(self.marker(self.L2D, gate_body("PASS", self.GATED),
                                     cf_body(self.FIRST, self.GATED), gate_body("PASS", newer),
                                     cf_body(self.SECOND, self.GATED)), f"l3.pass:{newer}")

    def test_carry_forward_after_a_fail_does_not_pass(self):
        self.assertEqual(self.marker(self.L2D, gate_body("FAIL", self.GATED),
                                     cf_body(self.FIRST, self.GATED)), "l3.gate-result")

    def test_preconditions_accept_the_carried_head(self):
        bodies = [self.L2D, gate_body("PASS", self.GATED), cf_body(self.FIRST, self.GATED)]
        w = lane1_rebase._watch()
        with mock.patch.object(w, "_fetch_all_comments", return_value=[{"body": b} for b in bodies]), \
                mock.patch.object(lane1_rebase, "repo_slug", return_value="o/r"), \
                mock.patch.object(lane1_rebase, "issue_labels", return_value={"bug"}), \
                mock.patch.object(lane1_rebase, "lane3_open_on", return_value=None):
            self.assertEqual(lane1_rebase.check_preconditions("/x", 7, "feat/7-thing", self.FIRST),
                             self.FIRST)



# harmonic-forge#1115: these tests drive l1_post, not the dry-run preflight
# gate (test_l1_post_dry_run.py owns that). With no session id the gate does
# not apply, so the outcome cannot depend on the caller's
# CLAUDE_CODE_SESSION_ID (set in any Claude session and in kill_check runs).
# Child processes inherit the stripped environment.
import os as _os  # noqa: E402
from unittest import mock as _session_mock  # noqa: E402

_NO_SESSION_PATCH = _session_mock.patch.dict(_os.environ)


def setUpModule():  # noqa: N802
    _NO_SESSION_PATCH.start()
    _os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


def tearDownModule():  # noqa: N802
    _NO_SESSION_PATCH.stop()


if __name__ == "__main__":
    unittest.main()
