#!/usr/bin/env python3
"""The gh shim's close and merge gate, end to end through real bash
(harmonic-forge#1447).

Each test puts a `gh` symlink to the shim first on PATH and a fake "real" gh
second. The fake logs every argv it receives and answers the gate's reads
from a fixture. A refused spelling must exit non-zero with no close or merge
argv in the fake's log: only the gate's own reads reach it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHIM = HERE / "gh_shim"
sys.path.insert(0, str(HERE.parent / "hooks"))

import close_gate  # noqa: E402

FAKE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    argv = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a") as log:
        log.write(json.dumps(argv) + "\\n")
    fx = json.load(open(os.environ["FAKE_GH_FIXTURE"]))
    if argv[:2] == ["alias", "list"]:
        print(fx.get("aliases", "")); sys.exit(0)
    if fx.get("fail_reads") and argv[:1] == ["api"]:
        sys.exit(1)
    if argv[:1] == ["api"] and len(argv) > 1 and "--jq" in argv:
        path, jq = argv[1], argv[argv.index("--jq") + 1]
        if jq == ".labels[].name":
            num = path.rsplit("/", 1)[-1]
            owned = fx.get("repo_labels", {}).get("/".join(path.split("/")[1:3]) + "#" + num)
            print("\\n".join(owned if owned is not None else fx.get("labels", {}).get(num, []))); sys.exit(0)
    if argv[:2] == ["repo", "view"] and "repos" in fx:
        # gh's own resolver reads GIT_DIR, else the cwd (harmonic-forge#1498 reforge).
        key = os.path.abspath(os.environ.get("GIT_DIR") or os.getcwd())
        if key in fx["repos"]:
            print(fx["repos"][key]); sys.exit(0)
        sys.exit(1)
        if jq == ".body":
            print(fx.get("pr_body", "")); sys.exit(0)
        if jq == ".[].commit.message":
            print(fx.get("pr_commits", "")); sys.exit(0)
    if argv[:2] == ["pr", "view"]:
        print(json.dumps({"headRefOid": fx.get("head", "a" * 40),
                          "closingIssuesReferences": [{"number": n} for n in fx.get("closes", [])],
                          "body": fx.get("pr_body", "")}))
        sys.exit(0)
    sys.exit(0 if argv[:1] in (["issue"], ["pr"]) else 1)
''')

UNINSPECTED = {"labels": {"5": ["tooling-exception"]}, "aliases": "c:\tissue close\n"}
INSPECTED = {"labels": {"5": ["tooling-exception", "preclose-inspected"]}, "aliases": ""}


class ShimBashBypass(unittest.TestCase):
    """AC1: no shell spelling sends an uninspected close to the real gh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "shim").mkdir()
        (self.tmp / "real").mkdir()
        (self.tmp / "shim" / "gh").symlink_to(SHIM)
        fake = self.tmp / "real" / "gh"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.log = self.tmp / "log"
        self.log.write_text("")
        self.fixture = self.tmp / "fixture.json"
        self.work = self.tmp / "work"
        self.work.mkdir()

    def run_bash(self, script: str, fixture: dict, env: dict | None = None):
        self.fixture.write_text(json.dumps(fixture))
        full_env = {"PATH": f"{self.tmp / 'shim'}:{self.tmp / 'real'}:/usr/bin:/bin",
                    "HOME": str(self.tmp), "FAKE_GH_LOG": str(self.log),
                    "FAKE_GH_FIXTURE": str(self.fixture)}
        full_env.update(env or {})
        return subprocess.run(["bash", "-c", script], cwd=self.work, env=full_env,
                              capture_output=True, text=True, timeout=60)

    def sent(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def closes_sent(self) -> list[list[str]]:
        return [a for a in self.sent()
                if a[:2] == ["issue", "close"] or a[:2] == ["c", "5"]
                or (a[:1] == ["api"] and any(t.upper() in ("PATCH", "-XPATCH", "PUT") or "state=closed" in t.lower()
                                             or t.startswith("--input") for t in a))]

    SPELLINGS = (
        "exec -ca x gh issue close 5 -R o/r",
        "# if\ngh issue close 5 -R o/r",
        ">/dev/null gh issue close 5 -R o/r",
        "sh -c 'true; true; true; gh issue close 5 -R o/r'",
        "printf 'issue\\nclose\\n5\\n-R\\no/r\\n' | xargs gh",
        "f(){ gh \"$@\"; }; " + "true; " * 60 + "f issue close 5 -R o/r",
        "G=gh; $G issue close 5 -R o/r",
        "$(printf gh) issue close 5 -R o/r",
        "if true; then gh issue close 5 -R o/r; fi",
        "gh api -X PATCH repos/o/r/issues/5 -f state=closed",
        "gh api --method=patch repos/o/r/issues/5 -F state=closed",
        "gh api repos/o/r/issues/5 -fstate=closed",
        "gh api repos/o/r/issues/5 -f state=closed",
        "echo '{}' | gh api -X PATCH repos/o/r/issues/5 --input -",
        "gh c 5 -R o/r",
        # pass 1 survivors
        "echo closed > f; gh api -X PATCH repos/o/r/issues/5 -F state=@f",
        "gh api -X PATCH 'repos/{owner}/{repo}/issues/5' -f state=closed",
        "gh issue close -c x --duplicate-of 6 5 -R o/r",
        # sticky-wicket reforge: spellings the old substring/prefix classifier let through
        "gh api -iXPATCH repos/o/r/issues/5 -f state=closed",
        "gh api repos/o/r/issues/5 -iFstate=closed",
        "gh issue -R o/r close 5",
        "gh api -X PATCH repositories/123/issues/5 -f state=closed",
        "gh api -X PUT 'repos/{owner}/{repo}/pulls/7/merge'",
    )

    def test_every_spelling_of_an_uninspected_close_is_refused(self):
        for script in self.SPELLINGS:
            with self.subTest(script=script):
                self.log.write_text("")
                done = self.run_bash(script, UNINSPECTED)
                self.assertNotEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.closes_sent(), [], done.stderr)
                self.assertIn("harmonic-forge#1447", done.stderr)

    def test_every_alias_and_extension_root_is_refused(self):
        """Sticky-wicket epoch 2: the gate expands no alias. A config alias
        (`co`), a `!` alias, a `# --help` alias body and an extension name all
        refuse at the root; gh is never called, not even `gh alias list`."""
        cases = (("gh co", "co:\tpr checkout\n"),
                 ("gh co", "co:\tissue close 5 -R o/r\n"),
                 ("gh c 5", "c:\t!/usr/bin/gh issue close \"$1\"\n"),
                 ("gh c 5", "c:\t'!echo hi'\n"),
                 ("gh c 5", "c:\tissue close 5 -R o/r # --help\n"),
                 ("gh dash", ""),
                 ("gh poi 5", ""))
        for script, aliases in cases:
            with self.subTest(script=script, aliases=aliases):
                self.log.write_text("")
                done = self.run_bash(script, {**INSPECTED, "aliases": aliases})
                self.assertEqual(done.returncode, 4, done.stderr)
                self.assertEqual(self.sent(), [])
                self.assertIn("not a gh builtin", done.stderr)
                self.assertIn("harmonic-forge#1447", done.stderr)

    def test_a_method_override_header_on_a_get_is_refused(self):
        """Sticky-wicket epoch 2, P2(b), verbatim survivor."""
        self.log.write_text("")
        done = self.run_bash("echo '{}' > body.json; gh api -X GET -H 'X-HTTP-Method-Override: PATCH' "
                             "--input body.json repos/o/r/issues/5", INSPECTED)
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertEqual(self.sent(), [])

    def test_branch_merges_and_ref_writes_are_refused(self):
        """Reforge pass 1, survivor 2: moving a base branch to a PR head merges it."""
        for script in ("gh api repos/o/r/merges -f base=main -f head=feat",
                       "gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc",
                       "gh api -X POST repositories/1/merges -f base=main -f head=x",
                       "gh api -X DELETE repos/o/r/git/refs/heads/x"):
            with self.subTest(script=script):
                self.log.write_text("")
                done = self.run_bash(script, INSPECTED)
                self.assertEqual(done.returncode, 4, done.stderr)
                self.assertEqual(self.sent(), [])

    def test_graphql_spellings_are_refused(self):
        for script in ("gh api -X POST /graphql -f query='mutation { closeIssue(input:{issueId:\"x\"}) { clientMutationId } }'",
                       "echo '{}' > q.json; gh api graphql --input q.json"):
            with self.subTest(script=script):
                self.log.write_text("")
                done = self.run_bash(script, INSPECTED)
                self.assertEqual(done.returncode, 4, done.stderr)
                self.assertFalse(any("graphql" in " ".join(a) for a in self.sent()))

    def test_a_comment_post_is_not_a_close(self):
        """AC3 regression: a mutating issues API call that is not a close passes."""
        done = self.run_bash("gh api repos/o/r/issues/5/comments -f body=hi", UNINSPECTED)
        self.assertNotEqual(done.returncode, 4, done.stderr)
        self.assertIn(["api", "repos/o/r/issues/5/comments", "-f", "body=hi"], self.sent())

    def test_a_fresh_override_never_unlocks_an_uninspected_close(self):
        """The gate runs before consume_override, and the override is not spent."""
        override = self.tmp / ".cache" / "harmonic-forge" / "gh_scan_override"
        override.parent.mkdir(parents=True)
        override.write_text("")
        done = self.run_bash("gh issue close 5 -R o/r", UNINSPECTED)
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertEqual(self.closes_sent(), [])
        self.assertTrue(override.exists())

    def test_an_import_failure_refuses_a_close_and_never_touches_auth(self):
        """AC4 and the plan's no-import test: a shim copy with no hooks dir."""
        lonely = self.tmp / "lonely"
        lonely.mkdir()
        shutil.copy(SHIM, lonely / "gh")
        (lonely / "gh").chmod(0o755)
        for name in ("gh_scan_patterns.py",):
            shutil.copy(HERE / name, lonely / name)
        env = {"PATH": f"{lonely}:{self.tmp / 'real'}:/usr/bin:/bin"}
        done = self.run_bash("gh issue close 5 -R o/r", UNINSPECTED, env)
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertIn("internal error", done.stderr)
        self.assertEqual(self.closes_sent(), [])
        done = self.run_bash("gh auth token", UNINSPECTED, env)
        self.assertNotEqual(done.returncode, 4, done.stderr)

    def test_a_duplicate_close_binds_the_issue_not_the_duplicate_of_value(self):
        """Pass-1 finding: `--duplicate-of 6` is a value, so the target is #5,
        and the exact duplicate close is exempt (harmonic-forge#1433)."""
        done = self.run_bash("gh issue close --duplicate-of 6 5 -R o/r", UNINSPECTED)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(["api", "repos/o/r/issues/5", "--jq", ".labels[].name"], self.sent())

    def test_an_unknown_flag_on_a_gated_root_is_refused(self):
        """Required test 6: a token the pinned grammar does not know refuses."""
        for script in ("gh issue close 5 -R o/r --frobnicate", "gh api --frobnicate user",
                       "gh pr view --frobnicate 7", "gh frobnicate 5"):
            with self.subTest(script=script):
                self.log.write_text("")
                done = self.run_bash(script, {**INSPECTED, "aliases": ""})
                self.assertEqual(done.returncode, 4, done.stderr)
                self.assertEqual(self.sent(), [])

    def test_an_import_failure_refuses_every_gated_root_but_not_identity(self):
        """Required test 6: with no gate to import, api/issue/pr and unknown
        roots refuse; the identity calls and other builtins pass."""
        lonely = self.tmp / "lonely2"
        lonely.mkdir()
        shutil.copy(SHIM, lonely / "gh")
        (lonely / "gh").chmod(0o755)
        shutil.copy(HERE / "gh_scan_patterns.py", lonely / "gh_scan_patterns.py")
        env = {"PATH": f"{lonely}:{self.tmp / 'real'}:/usr/bin:/bin"}
        for script in ("gh api repos/o/r/issues/5", "gh issue view 5 -R o/r", "gh c 5"):
            with self.subTest(script=script):
                self.assertEqual(self.run_bash(script, UNINSPECTED, env).returncode, 4)
        for script in ("gh api user --jq .login", "gh auth token", "gh repo view o/r"):
            with self.subTest(script=script):
                self.assertNotEqual(self.run_bash(script, UNINSPECTED, env).returncode, 4)

    def test_an_inspected_close_goes_through(self):
        done = self.run_bash("gh issue close 5 -R o/r", INSPECTED)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(["issue", "close", "5", "-R", "o/r"], self.sent())

    def test_gh_repo_from_the_environment_is_gated(self):
        done = self.run_bash("gh issue close 5", UNINSPECTED, {"GH_REPO": "o/r"})
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertEqual(self.closes_sent(), [])

    def test_gh_repo_disagreeing_with_repo_refuses(self):
        done = self.run_bash("gh issue close 5 -R o/r", INSPECTED, {"GH_REPO": "o/other"})
        self.assertEqual(done.returncode, 4)
        self.assertIn("disagree", done.stderr)

    def test_an_unreadable_label_refuses(self):
        """AC4: strict reads fail closed and name the read."""
        done = self.run_bash("gh issue close 5 -R o/r", {**INSPECTED, "fail_reads": True})
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertIn("could not read", done.stderr)
        self.assertEqual(self.closes_sent(), [])

    def test_graphql_mutations_are_refused(self):
        done = self.run_bash("gh api graphql -f query='mutation { closeIssue(input:{issueId:\"x\"}) { clientMutationId } }'",
                             INSPECTED)
        self.assertEqual(done.returncode, 4)

    def test_a_read_passes_through_untouched(self):
        """AC3."""
        done = self.run_bash("gh issue view 5 -R o/r | grep close; true", UNINSPECTED)
        self.assertEqual(done.returncode, 0)
        self.assertIn(["issue", "view", "5", "-R", "o/r"], self.sent())

    def test_the_gate_reads_go_to_the_real_gh(self):
        """Kill check 8: the label read reaches the fake, not the shim again."""
        self.run_bash("gh issue close 5 -R o/r", UNINSPECTED)
        self.assertTrue(any(a[:2] == ["api", "repos/o/r/issues/5"] for a in self.sent()))


class DataMigrationGate(unittest.TestCase):
    """harmonic-forge#1498 reforge: the shim runs the data-migration label
    decision on the real argv, cwd and environment, so neither an expansion or
    runner (category A) nor a rebinding of the repo (category B) hides a close
    of a `data-migration` issue lacking `migration-executed`."""

    def fixture_for(self, labels):
        other = self.tmp / "other"
        other.mkdir(exist_ok=True)
        return {"aliases": "", "repo_labels": {"o/mig#849": labels, "o/safe#849": []},
                "repos": {str(self.work): "o/mig", str(other): "o/mig",
                          str(self.tmp / "safe"): "o/safe", str(other / ".git"): "o/mig"}}

    run_bash, sent, closes_sent = ShimBashBypass.run_bash, ShimBashBypass.sent, ShimBashBypass.closes_sent

    def setUp(self):
        ShimBashBypass.setUp(self)
        (self.tmp / "safe").mkdir()

    def migration_closes(self):
        return [a for a in self.closes_sent() if "849" in " ".join(a)]

    SPELLINGS = (
        "G=gh; $G issue close 849",
        "declare -x GH_REPO=o/mig; cd ../safe; gh issue close 849",
        "cd ../safe && cd ../other && gh issue close 849",
        "bash -c 'gh issue close 849'",
        "cd ../safe; GIT_DIR=../other/.git gh issue close 849",
        "gh api -X PATCH repos/o/mig/issues/849 -f state=closed",
    )

    def test_every_spelling_of_an_unexecuted_migration_close_is_refused(self):
        for script in self.SPELLINGS:
            with self.subTest(script=script):
                self.log.write_text("")
                done = self.run_bash(script, self.fixture_for(["data-migration"]))
                self.assertEqual(done.returncode, 4, done.stderr)
                self.assertIn("'data-migration'", done.stderr)
                self.assertIn("o/mig#849", done.stderr)
                self.assertEqual(self.migration_closes(), [], done.stderr)

    def test_an_executed_or_unlabelled_issue_closes(self):
        for labels in (["data-migration", "migration-executed"], ["data-migration", "migration-abandoned"], []):
            with self.subTest(labels=labels):
                self.log.write_text("")
                done = self.run_bash("G=gh; $G issue close 849", self.fixture_for(labels))
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(["issue", "close", "849"], self.sent())

    def test_a_bare_close_binds_where_gh_runs(self):
        """A rebinding to the safe repo is honored, not denied as undecided."""
        done = self.run_bash("cd ../safe && gh issue close 849", self.fixture_for(["data-migration"]))
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_failed_label_read_refuses(self):
        done = self.run_bash("gh issue close 849 -R o/mig", {**self.fixture_for(["data-migration"]),
                                                             "fail_reads": True})
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertIn("could not read", done.stderr)
        self.assertEqual(self.migration_closes(), [])

    def test_an_unresolvable_repo_refuses(self):
        fixture = {**self.fixture_for(["data-migration"]), "repos": {}}
        done = self.run_bash("G=gh; $G issue close 849", fixture)
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertEqual(self.migration_closes(), [])


class MergeGate(unittest.TestCase):
    """AC2: the shim's merge decision is the hook's own (shared code)."""

    def setUp(self):
        import block_missing_preclose_inspection as pre
        import block_undetermined_phase_close as ph
        self.pre, self.ph = pre, ph
        from unittest import mock
        self.mock = mock
        env = mock.patch.dict(os.environ, {"GH_REPO": "", "GH_HOST": ""})
        env.start()
        self.addCleanup(env.stop)
        for target, name, value in ((pre, "issues_closed_by_pr", ["5"]), (pre, "_pr_head_sha", "a" * 40),
                                    (pre, "_preclose_receipt_ok", True), (ph, "segment_verdict", None)):
            patcher = mock.patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def verdict(self, argv, labels):
        with self.mock.patch.object(self.pre, "labels_for", return_value=set(labels)):
            return close_gate.shim_verdict(argv, cwd="/tmp", real_gh="/bin/true")

    def test_an_uninspected_merge_is_refused_with_the_hooks_own_text(self):
        text = self.verdict(["pr", "merge", "7", "--repo", "o/r"], ["tooling-exception"])
        self.assertIn(self.pre._deny_message("o/r", "5", "7"), text)

    def test_an_inspected_bound_merge_goes_through(self):
        self.assertIsNone(self.verdict(["pr", "merge", "7", "--repo", "o/r", "--match-head-commit", "a" * 40],
                                       ["tooling-exception", "preclose-inspected"]))

    def test_a_merge_with_no_number_is_refused(self):
        self.assertIsNotNone(self.verdict(["pr", "merge", "--repo", "o/r"], ["tooling-exception"]))

    def test_shim_makes_no_batch_decision(self):
        import batch_auth
        with self.mock.patch.object(batch_auth, "decide", side_effect=AssertionError("BATCH decided")):
            self.assertIsNone(self.verdict(["pr", "merge", "7", "--repo", "o/r", "--match-head-commit", "a" * 40],
                                           ["tooling-exception", "preclose-inspected"]))


class Layering(unittest.TestCase):
    """AC5: a real gh reached by absolute path still meets the PreToolUse
    hook's target finder (program basename `gh`)."""

    def test_absolute_path_gh_is_still_a_target(self):
        import block_missing_preclose_inspection as pre
        self.assertEqual(pre.find_gated_targets("/usr/bin/gh issue close 5 -R o/r"),
                         [("o/r", "5", "issue")])


class CloseGateUnits(unittest.TestCase):
    def test_a_root_flag_followed_by_words_is_refused(self):
        """harmonic-forge#1447 post-verdict: gh dispatches the words after a
        root flag, so `gh --help issue close 5 --help=false` would close.
        A root flag is allowed only alone."""
        for argv in (["--help", "issue", "close", "5", "--help=false"],
                     ["-h", "pr", "merge", "7"], ["--version", "api", "-X", "PATCH", "x"],
                     ["--help", "version"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(close_gate.shim_verdict(argv, cwd=".", real_gh="/nonexistent/gh"))
        for argv in (["--help"], ["--version"], ["-h"]):
            with self.subTest(argv=argv):
                self.assertIsNone(close_gate.shim_verdict(argv, cwd=".", real_gh="/nonexistent/gh"))

    def test_the_shim_sends_a_root_flag_with_words_to_the_gate(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("gh_shim_mod", str(SHIM))
        spec = importlib.util.spec_from_loader("gh_shim_mod", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        self.assertTrue(mod._needs_gate(["--help", "issue", "close", "5"]))
        self.assertFalse(mod._needs_gate(["--help"]))

    def test_paginated_pages_read_as_one_list(self):
        import block_undetermined_phase_close as ph
        from unittest import mock
        with mock.patch.object(ph, "_gh", return_value='[{"a":1}]\n[{"a":2}]'):
            self.assertEqual(ph._gh_json("api", "x", "--paginate"), [{"a": 1}, {"a": 2}])

    def test_strict_reads_name_the_real_binary_explicitly(self):
        """Kill check 8: in strict mode both hooks run the shim's real gh, never
        `gh` through PATH (which would re-enter the shim)."""
        import subprocess as sp
        from unittest import mock
        import block_missing_preclose_inspection as pre
        import block_undetermined_phase_close as ph
        with tempfile.NamedTemporaryFile() as real:
            for hook in (pre, ph):
                seen = {}

                def fake_run(args, **kw):
                    seen.update(kw)
                    return sp.CompletedProcess(args, 0, stdout="ok", stderr="")
                with mock.patch.object(hook, "_run_gh", side_effect=fake_run), close_gate.strict(real.name):
                    self.assertEqual(hook._gh("api", "x"), "ok")
                with self.subTest(hook=hook.__name__):
                    self.assertEqual(seen.get("gh_bin"), real.name)

    def test_strict_mode_raises_only_inside_the_context(self):
        self.assertIsNone(close_gate.unread(["api"], "x"))
        with close_gate.strict("/bin/true"):
            with self.assertRaises(close_gate.GateReadError):
                close_gate.unread(["api"], "x")
        self.assertFalse(close_gate.STATE.strict)


if __name__ == "__main__":
    unittest.main()
