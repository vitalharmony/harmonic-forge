"""harmonic-forge#1112 AC9: the te-runner preflight runs every check and lists
every failure; a kill bundle whose base run is not a test failure aborts."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight  # noqa: E402
import te_run  # noqa: E402

STUB_BWRAP = Path(__file__).resolve().parent / "testdata" / "stub_bwrap.py"


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, message: str) -> None:
    sh("git", "add", "-A", cwd=repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message, cwd=repo)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {"TE_RUNNER_BWRAP": str(STUB_BWRAP),
                                               "HARMONIC_FORGE_TESTING": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)


class RunAll(Base):
    def test_every_failure_is_reported(self):
        def boom():
            raise RuntimeError("exploded")
        checks = [("a", lambda: ["a failed"]), ("b", lambda: ["b failed"]), ("c", boom),
                  ("d", lambda: []), ("e", lambda: ["e failed", "e again"])]
        record = preflight.run_all(checks, [("r", lambda: ["noted"])], self.tmp)
        self.assertEqual(record["failures"],
                         ["a failed", "b failed", "c: could not run: exploded",
                          "e failed", "e again"])
        self.assertEqual(record["reported"], ["noted"])
        self.assertEqual(json.loads((self.tmp / "preflight.json").read_text()), record)
        self.assertTrue(record["checks"]["d"]["ok"])


class KillBaselines(Base):
    def setUp(self):
        super().setUp()
        upstream = self.tmp / "upstream.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(upstream), cwd=self.tmp)
        self.src = self.tmp / "src"
        self.src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.src)
        (self.src / "widget.py").write_text("def widget():\n    return 1\n")
        commit(self.src, "base")
        sh("git", "remote", "add", "origin", str(upstream), cwd=self.src)
        sh("git", "push", "-q", "origin", "main", cwd=self.src)
        self.base = sh("git", "rev-parse", "HEAD", cwd=self.src)

    def bundle(self, test_body: str) -> Path:
        bundle = self.tmp / "bundle"
        shutil.rmtree(bundle, ignore_errors=True)
        (bundle / "files" / "tests").mkdir(parents=True)
        (bundle / "files" / "tests" / "__init__.py").write_text("")
        (bundle / "files" / "tests" / "test_widget.py").write_text(
            "import unittest\n" + test_body + "\n\nclass W(unittest.TestCase):\n"
            "    def test_two(self):\n        self.assertEqual(widget.widget(), 2)\n")
        (bundle / "checks.json").write_text(json.dumps(
            [{"ac": "AC1", "test": ["python3", "-m", "unittest", "discover", "-s", "tests",
                                    "-t", "."]}]))
        return bundle

    def baselines(self, bundle: Path) -> list[str]:
        with mock.patch.object(preflight.te_queue, "bundle_dir", return_value=bundle):
            return preflight.kill_baselines([7], self.src, self.base, self.tmp)

    def test_a_test_failure_on_the_base_passes(self):
        self.assertEqual(self.baselines(self.bundle("import widget")), [])

    def test_an_import_error_on_the_base_fails(self):
        [failure] = self.baselines(self.bundle("import widget\nimport not_there_yet"))
        self.assertIn("broken-test", failure)

    def test_broken_test_baseline_aborts(self):
        # The whole preflight, as `te_run.py run` calls it: every other check
        # passes, the kill bundle's base run is an import error, so it fails.
        bundle = self.bundle("import widget\nimport not_there_yet")
        state = {"items": [{"id": "F7", "status": "pending", "note": ""}]}
        with mock.patch.object(te_run, "SOURCE", self.src), \
                mock.patch.object(te_run, "BASE_CHECK_ROOT", self.tmp / "base-check"), \
                mock.patch.object(preflight.te_queue, "bundle_dir", return_value=bundle), \
                mock.patch.object(preflight, "permission_probe", return_value=[]), \
                mock.patch.object(preflight, "codex_parity", return_value=[]), \
                mock.patch.object(preflight, "rehearsal", return_value=[]), \
                mock.patch.object(preflight, "item_failures", return_value=[]), \
                mock.patch.object(te_run, "base_check",
                                  return_value={"sha": self.base, "failures": [], "ran": 0}):
            record = te_run.run_preflight(state, self.tmp / "run", {**os.environ},
                                          {**os.environ})
        self.assertTrue(record["failures"])
        self.assertTrue(any("broken-test" in f for f in record["failures"]), record)
        self.assertTrue((self.tmp / "run" / "preflight.json").is_file())


ISSUE_BODY = "## Acceptance criteria\n\n1. The widget returns 2.\n"

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"is_error": False, "result": "", "structured_output": {"findings": []}}))
"""

# A fixture reviewer: writes a real-shaped envelope whose one verdict is
# FAKE_VERDICT, so the real classifier labels it.
FAKE_CROSS = """#!/usr/bin/env bash
set -eu
python3 - "$@" <<'PY'
import json, os, sys
pairs = dict(zip(sys.argv[1::2], sys.argv[2::2]))
envelope = {"family": "codex", "caller_family": "claude", "posture": "verify", "status": "ok",
            "exit_code": 0, "verify_model": "gpt-6-sol",
            "report": {"summary": "s", "findings": [], "assumptions": [
                {"assumption": "VALUE is 2", "verdict": os.environ["FAKE_VERDICT"],
                 "evidence": "$ cat rehearsal.py\\nVALUE = 2"}]}}
open(pairs["--out"], "w").write(json.dumps(envelope) + "\\n")
PY
"""


class ItemChecks(Base):
    """AC12: the preflight calls the pipeline's own per-item preconditions."""

    def setUp(self):
        super().setUp()
        self.src = self.tmp / "src"
        self.src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.src)
        (self.src / "widget.py").write_text("def widget():\n    return 1\n")
        commit(self.src, "base")
        self.base = sh("git", "rev-parse", "HEAD", cwd=self.src)
        self.seams = {"first_failure": None, "bundle_pin": "a" * 64,
                      "snapshot_bundle": self.tmp / "snap", "body": ISSUE_BODY}

    def failures(self, **override) -> list[str]:
        seams = {**self.seams, **override}

        def seam(value):
            if isinstance(value, Exception):
                return {"side_effect": value}
            return {"return_value": value}
        with mock.patch.object(preflight.te_queue, "first_failure", **seam(seams["first_failure"])), \
                mock.patch.object(preflight.te_queue, "bundle_pin", **seam(seams["bundle_pin"])), \
                mock.patch.object(preflight.te_queue, "snapshot_bundle",
                                  **seam(seams["snapshot_bundle"])), \
                mock.patch.object(preflight.preclose_driver, "passes_held", return_value=0), \
                mock.patch.object(preflight.preclose_driver, "issue_body",
                                  return_value=seams["body"]):
            return preflight.item_failures(7, self.src, self.base, self.tmp / "item", {})

    def test_clean_item_has_no_failures(self):
        self.assertEqual(self.failures(), [])
        # The driver's export step ran: a git repository at the base.
        head = self.tmp / "item" / "driver" / "head"
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=head), self.base)

    def test_new_driver_error_is_reported(self):
        # A DriverError path added to the driver's read-only steps reaches the
        # preflight with no change to the preflight's code.
        def new_step(ctx):
            raise preflight.preclose_driver.DriverError("a brand-new refusal")
        steps = (*preflight.preclose_driver.READ_ONLY_STEPS, new_step)
        with mock.patch.object(preflight.preclose_driver, "READ_ONLY_STEPS", steps):
            failures = self.failures(body="## Problem\n\nno criteria\n")
        self.assertTrue(any("a brand-new refusal" in f for f in failures), failures)
        # ...beside the step that also failed: every refusal, never the first.
        self.assertTrue(any("acceptance criteria" in f for f in failures), failures)

    def test_kill_bundle_and_queue_refusals_are_both_listed(self):
        failures = self.failures(
            first_failure=("ineligible", "no tooling-exception label"),
            snapshot_bundle=preflight.te_queue.QueueError("the kill-check bundle no longer "
                                                          "matches the handoff's pin"))
        self.assertTrue(any("eligibility" in f and "tooling-exception" in f for f in failures),
                        failures)
        self.assertTrue(any("kill bundle" in f and "no longer matches" in f for f in failures),
                        failures)


class Rehearsal(Base):
    """AC15: one refuter and one cross-family verify call on a fixed fixture."""

    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "claude").write_text(FAKE_CLAUDE)
        (bin_dir / "claude").chmod(0o755)
        (self.tmp / "cross.sh").write_text(FAKE_CROSS)
        patcher = mock.patch.object(preflight.preclose_driver, "CROSS_FAMILY",
                                    self.tmp / "cross.sh")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def test_confirmed_known_true_assumption_passes(self):
        self.assertEqual(preflight.rehearsal(self.tmp, {**self.env, "FAKE_VERDICT": "confirmed"}),
                         [])

    def test_uncheckable_reviewer_fails_the_preflight(self):
        failures = preflight.rehearsal(self.tmp, {**self.env, "FAKE_VERDICT": "uncheckable"})
        self.assertTrue(any("cross-family rehearsal" in f and "in-family fallback" in f
                            for f in failures), failures)


class Pieces(unittest.TestCase):
    def test_parse_probe_lists_attempts_and_denials(self):
        lines = [{"type": "assistant", "message": {"content": [
                     {"type": "tool_use", "name": "Bash", "input": {"command": "mkdir -p x"}}]}},
                 {"type": "result", "permission_denials": [
                     {"tool_name": "Bash", "tool_input": {"command": "mkdir -p x"}}]}]
        attempted, denied, result = preflight.parse_probe("\n".join(map(json.dumps, lines)))
        self.assertEqual(attempted, ["mkdir -p x"])
        self.assertEqual(denied, ["mkdir -p x"])
        self.assertIsNotNone(result)

    def test_base_regression_is_reported_only_from_empty(self):
        self.assertTrue(preflight.base_regression({"sha": "b", "failures": ["x"]},
                                                  {"sha": "a", "failures": []}))
        self.assertEqual(preflight.base_regression({"sha": "b", "failures": ["x"]},
                                                   {"sha": "a", "failures": ["y"]}), [])
        self.assertEqual(preflight.base_regression({"sha": "b", "failures": ["x"]}, None), [])

    def test_probe_list_covers_the_ac5_commands(self):
        text = "\n".join(preflight.PROBE_COMMANDS)
        for cmd in ("mkdir", "rm ", "cp ", "mv ", "touch", "git checkout", "git switch",
                    "git restore", "git stash", "git mv", "git rm", "git reset", "git rev-parse",
                    "git branch", "mise tasks", "mise run check --help"):
            self.assertIn(cmd, text)


if __name__ == "__main__":
    unittest.main()
