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
            print("\\n".join(fx.get("labels", {}).get(num, []))); sys.exit(0)
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
                or (a[:1] == ["api"] and any(t.upper() in ("PATCH", "-XPATCH") or "state=closed" in t.lower()
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
    )

    def test_every_spelling_of_an_uninspected_close_is_refused(self):
        for script in self.SPELLINGS:
            with self.subTest(script=script):
                self.log.write_text("")
                done = self.run_bash(script, UNINSPECTED)
                self.assertNotEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.closes_sent(), [], done.stderr)
                self.assertIn("harmonic-forge#1447", done.stderr)

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


class Layering(unittest.TestCase):
    """AC5: a real gh reached by absolute path still meets the PreToolUse
    hook's target finder (program basename `gh`)."""

    def test_absolute_path_gh_is_still_a_target(self):
        import block_missing_preclose_inspection as pre
        self.assertEqual(pre.find_gated_targets("/usr/bin/gh issue close 5 -R o/r"),
                         [("o/r", "5", "issue")])


class CloseGateUnits(unittest.TestCase):
    def test_rest_close_shapes(self):
        rest = close_gate._rest_close_target
        self.assertEqual(rest(["gh", "api", "-XPATCH", "repos/o/r/issues/5", "-f", "state=CLOSED"]),
                         ("o/r", "5", "issue"))
        self.assertEqual(rest(["gh", "api", "repos/o/r/issues/5", "--field=state=closed"]), ("o/r", "5", "issue"))
        self.assertIsNone(rest(["gh", "api", "-X", "GET", "repos/o/r/issues/5", "-f", "state=closed"]))
        self.assertIsNone(rest(["gh", "api", "repos/o/r/issues/5"]))
        self.assertIsNone(rest(["gh", "api", "-X", "PATCH", "repos/o/r/issues/5", "-f", "title=x"]))

    def test_cheap_classifier(self):
        self.assertFalse(close_gate.could_be_close_or_merge(["issue", "view", "5"]))
        self.assertTrue(close_gate.could_be_close_or_merge(["issue", "close", "5"]))
        self.assertTrue(close_gate.could_be_close_or_merge(["c", "5"]))

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
