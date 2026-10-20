"""Tests for the headless preclose driver (harmonic-forge#1059)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preclose_driver  # noqa: E402

LENSES = preclose_driver.preclose_check.LENSES
REAL_FIND_RECEIPT = preclose_driver.preclose_check.find_receipt
PLANNED = {"status": "planned", "reviewed_sha": "a" * 40, "refuters": 5}
# AC2's list, restated: the tests never take it from the code under test.
FORBIDDEN = ("--force", "--reforge", "--allow-dirty", "--not-triggered", "--cluster-verdict",
             "--allow-repo-mismatch")
ISSUE_BODY = ("Intro.\n\n## Problem\n\nPR body and rationale live elsewhere.\n\n"
              "## Acceptance criteria\n\n1. The widget returns 2.\n2. It never returns 3.\n\n"
              "## Notes\n\nnot an AC\n")

FAKE_PRECLOSE = textwrap.dedent("""\
    import json, os, sys
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps(["preclose", *sys.argv[1:]]) + "\\n")
    lenses = json.loads(os.environ["FAKE_LENSES"])
    if "--complete" in sys.argv:
        if os.environ.get("FAKE_CORRUPT"):
            open(os.environ["FAKE_CORRUPT"], "w").write("{not json")
        if os.environ.get("FAKE_COMPLETE_RC"):
            print("refused", file=sys.stderr); sys.exit(int(os.environ["FAKE_COMPLETE_RC"]))
        print("preclose-check: recorded pass 1 of at most 2")
        print("  " + os.environ.get("FAKE_PROVENANCE",
              "Red-team provenance: cross-family (codex) -- 1/1 confirmed"))
    else:
        # Lenses print even on a refusal, so only the exit code can stop the pass.
        print("preclose-check plan\\n  lenses:")
        for lens in lenses:
            print("    - " + lens)
        print("  arm:      manual")
        if os.environ.get("FAKE_PLAN_RC"):
            print("no passing kill-check receipt", file=sys.stderr)
            sys.exit(int(os.environ["FAKE_PLAN_RC"]))
""")

# Mirrors cross_family_call.sh's usage(): every flag it requires, nothing else.
FAKE_CROSS = textwrap.dedent("""\
    #!/usr/bin/env bash
    set -eu
    python3 - "$@" <<'PY'
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps(["cross", *args]) + "\\n")
    known = {"--caller", "--families", "--posture", "--brief", "--cwd", "--out"}
    pairs = dict(zip(args[0::2], args[1::2]))
    assert len(args) % 2 == 0 and set(pairs) <= known, args
    assert pairs["--caller"] in ("claude", "codex", "gemini"), args
    assert pairs["--families"] in ("2", "3"), args
    assert pairs["--posture"] in ("read-only", "probe", "verify"), args
    assert os.path.isfile(pairs["--brief"]) and os.path.isdir(pairs["--cwd"]), args
    if os.environ.get("FAKE_CROSS_NO_ENVELOPE"):
        sys.exit(1)
    open(pairs["--out"], "w").write('{"status": "ok"}\\n')
    PY
""")

FAKE_CLAUDE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    prompt = sys.stdin.read()
    with open(os.environ["FAKE_LOG"], "a") as log:
        tree = open("widget.py").read() if os.path.isfile("widget.py") else None
        log.write(json.dumps(["claude", *sys.argv[1:], {"cwd": os.getcwd(), "prompt": prompt,
                                                         "tree": tree}]) + "\\n")
    result = os.environ.get("FAKE_RESULT", "[]")
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 1,
             "cache_read_input_tokens": 4}
    if os.environ.get("FAKE_NO_USAGE"):
        usage = None
    envelope = {"is_error": False, "result": "", "usage": usage,
                "structured_output": {"findings": json.loads(result)}}
    if "FAKE_STRUCTURED" in os.environ:
        raw = os.environ["FAKE_STRUCTURED"]
        if raw == "absent":
            del envelope["structured_output"]
        else:
            envelope["structured_output"] = json.loads(raw)
    print(json.dumps(envelope))
""")

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps(["gh", *sys.argv[1:]]) + "\\n")
    print(os.environ["FAKE_BODY"])
""")


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout



STUB_BWRAP = Path(__file__).resolve().parent / "testdata" / "stub_bwrap.py"


def setUpModule():
    # The unit tests run the boundary's argv through a stand-in (harmonic-forge#1098);
    # real confinement has its own tests.
    os.environ["TE_RUNNER_BWRAP"] = str(STUB_BWRAP)
    os.environ["HARMONIC_FORGE_TESTING"] = "1"

class Driver(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        src = self.tmp / "src"
        src.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=src)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
           "-m", "base", cwd=src)
        (src / "widget.py").write_text("def widget():\n    return 2\n")
        sh("git", "add", ".", cwd=src)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
           "-m", "head: secret rationale in the commit message", cwd=src)
        self.src, self.head = src, sh("git", "rev-parse", "HEAD", cwd=src).strip()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, text in (("claude", FAKE_CLAUDE), ("gh", FAKE_GH)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        (self.tmp / "preclose.py").write_text(FAKE_PRECLOSE)
        (self.tmp / "cross.sh").write_text(FAKE_CROSS)
        self.log = self.tmp / "log.jsonl"
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "FAKE_LOG": str(self.log), "FAKE_BODY": ISSUE_BODY,
                    "FAKE_LENSES": json.dumps(list(LENSES))}
        self.enter(mock.patch.object(preclose_driver, "PRECLOSE", self.tmp / "preclose.py"))
        self.enter(mock.patch.object(preclose_driver, "CROSS_FAMILY", self.tmp / "cross.sh"))
        self.enter(mock.patch.object(preclose_driver.preclose_check, "find_receipt",
                                     return_value=None))
        self.enter(mock.patch.object(preclose_driver, "runner_env", lambda: self.env))
        self.argvs: list[list[str]] = []
        real_call = preclose_driver.call

        def recording(argv, **kw):
            self.argvs.append(list(argv))
            return real_call(argv, **kw)
        self.enter(mock.patch.object(preclose_driver, "call", recording))

    def enter(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def drive(self, **env) -> int:
        self.env.update(env)
        out = self.tmp / "out"
        return preclose_driver.main(["run", "--repo", "vitalharmony/harmonic-forge", "--issue", "7",
                                     "--base", "main~1", "--head", self.head,
                                     "--source-repo", str(self.src), "--tier", "deep",
                                     "--out", str(out)])

    def logged(self, kind: str) -> list[list]:
        if not self.log.exists():
            return []
        return [row[1:] for row in map(json.loads, self.log.read_text().splitlines())
                if row[0] == kind]

    def test_clean_pass_records_cross_family_and_writes_outputs(self):
        self.assertEqual(self.drive(), 0)
        out = self.tmp / "out"
        fact = json.loads((out / "preclose.json").read_text())
        self.assertEqual(fact["survivors"], 0)
        self.assertTrue(fact["provenance"].startswith("Red-team provenance: cross-family ("))
        self.assertEqual(json.loads((out / "findings.json").read_text()), [])
        self.assertTrue((out / "envelope.jsonl").is_file())
        self.assertIn("Surviving: 0", (out / "summary.md").read_text())
        complete = [a for a in self.logged("preclose") if "--complete" in a]
        self.assertEqual(len(complete), 1)
        for flag in ("--cross-family", "--own-model", "--findings", "--envelope",
                     "--cross-family-ms", "--panel-tokens", "--panel-ms"):
            self.assertIn(flag, complete[0])
        self.assertEqual(complete[0][complete[0].index("--panel-tokens") + 1], str(20 * 5))

    def test_one_refuter_per_planned_lens(self):
        self.assertEqual(self.drive(FAKE_LENSES=json.dumps(list(LENSES[:3]))), 0)
        refuters = self.logged("claude")
        self.assertEqual(len(refuters), 3)
        self.assertEqual(sorted(r[-1]["prompt"].splitlines()[0] for r in refuters),
                         sorted(f"Lens: {lens}" for lens in LENSES[:3]))

    def test_refuter_argv_and_cwd(self):
        self.drive()
        argv = self.logged("claude")[0]
        self.assertNotIn("--agent", argv)
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        item = schema["properties"]["findings"]["items"]
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(set(item["required"]), {"anchor", "scenario", "mechanism"})
        for pair in (["--permission-prompts", "none"],
                     ["--permission-mode", "dontAsk"]):
            i = argv.index(pair[0])
            self.assertEqual(argv[i:i + 2], pair)
        self.assertIn("--max-budget-usd", argv)
        cwd = Path(argv[-1]["cwd"])
        self.assertNotEqual(cwd.resolve(), self.src.resolve())
        self.assertNotIn(str(self.src.resolve()), str(cwd.resolve()))
        # Positively: the cwd held the head's tree, inside the driver's scratch dir.
        self.assertEqual(argv[-1]["tree"], "def widget():\n    return 2\n")
        self.assertTrue(cwd.parent.name.startswith("preclose-driver-"), cwd)

    def test_cross_family_argv_matches_real_usage(self):
        self.drive()
        argv = self.logged("cross")[0]
        pairs = dict(zip(argv[0::2], argv[1::2]))
        self.assertEqual((pairs["--caller"], pairs["--families"], pairs["--posture"]),
                         ("claude", "2", "verify"))
        self.assertEqual(set(pairs), {"--caller", "--families", "--posture", "--brief", "--cwd",
                                      "--out"})
        usage = (Path(preclose_driver.__file__).parent.parent / "lane" /
                 "cross_family_call.sh").read_text()
        for flag in pairs:
            self.assertIn(f"{flag})", usage, f"{flag} is not in the real script's parser")

    def test_no_forbidden_flag_in_any_argv(self):
        self.drive(FAKE_RESULT=json.dumps([{"anchor": "widget.py:2", "scenario": "s",
                                            "mechanism": "m"}]))
        self.assertTrue(self.argvs)
        for argv in self.argvs:
            for flag in FORBIDDEN:
                self.assertFalse([a for a in argv if isinstance(a, str) and a.split("=")[0] == flag],
                                 argv)

    def test_call_refuses_a_forbidden_flag(self):
        for flag in FORBIDDEN:
            with self.assertRaises(preclose_driver.DriverError):
                preclose_driver.call(["python3", "preclose_check.py", flag])

    def test_nothing_comments_on_or_edits_an_issue(self):
        self.drive()
        for argv in self.argvs + self.logged("gh"):
            text = " ".join(map(str, argv))
            for forbidden in ("issue comment", "issue edit", "pr comment", "label", "issue close"):
                self.assertNotIn(forbidden, text)

    def test_two_passes_held_refuses_before_plan(self):
        # The shape preclose_check writes (pass_history), read through the real
        # preclose_passes; only the store lookup is stubbed.
        receipt = {"status": "complete", "reviewed_sha": "b" * 40,
                   "pass_history": [{"sha": "a" * 40, "surviving": 1, "epoch": 0},
                                    {"sha": "b" * 40, "surviving": 1, "epoch": 0}]}
        with mock.patch.object(preclose_driver.preclose_check, "find_receipt",
                               return_value=receipt):
            self.assertEqual(self.drive(), 2)
        self.assertEqual(self.logged("preclose"), [])
        self.assertEqual(self.logged("claude"), [])

    def test_one_pass_held_still_runs(self):
        receipt = {"status": "complete", "reviewed_sha": "a" * 40,
                   "pass_history": [{"sha": "a" * 40, "surviving": 1, "epoch": 0}]}
        with mock.patch.object(preclose_driver.preclose_check, "find_receipt",
                               return_value=receipt):
            self.assertEqual(self.drive(), 0)

    def test_unparseable_refuter_output_does_not_complete(self):
        for structured in ("absent", "null", "[]", '{"findings": {}}', '{"other": []}',
                           '{"findings": [{"anchor": "x:1"}]}', '{"findings": [1]}'):
            self.log.unlink(missing_ok=True)
            self.assertEqual(self.drive(FAKE_STRUCTURED=structured), 2, structured)
            self.assertFalse([a for a in self.logged("preclose") if "--complete" in a],
                             structured)

    def test_findings_are_never_dismissed(self):
        found = [{"anchor": "widget.py:2", "scenario": "returns 3", "mechanism": "m",
                  "dismissed": "refuter tried to dismiss its own finding"}]
        self.assertEqual(self.drive(FAKE_RESULT=json.dumps(found)), 0)
        written = json.loads((self.tmp / "out" / "findings.json").read_text())
        self.assertEqual(len(written), len(LENSES))
        self.assertTrue(all("dismissed" not in f for f in written))
        fact = json.loads((self.tmp / "out" / "preclose.json").read_text())
        self.assertEqual(fact["survivors"], len(LENSES))

    def test_brief_holds_criteria_diff_and_lens_only(self):
        self.drive()
        prompt = self.logged("claude")[0][-1]["prompt"]
        self.assertIn("1. The widget returns 2.", prompt)
        self.assertIn("+    return 2", prompt)
        for leaked in ("secret rationale", "PR body and rationale", "not an AC"):
            self.assertNotIn(leaked, prompt)

    def test_plan_refusal_is_not_a_pass(self):
        self.assertEqual(self.drive(FAKE_PLAN_RC="1"), 2)
        self.assertEqual(self.logged("claude"), [])

    def test_missing_envelope_is_not_a_pass(self):
        self.assertEqual(self.drive(FAKE_CROSS_NO_ENVELOPE="1"), 2)
        self.assertFalse([a for a in self.logged("preclose") if "--complete" in a])

    def test_non_empty_out_is_refused(self):
        out = self.tmp / "out"
        out.mkdir()
        (out / "envelope.jsonl").write_text('{"status": "ok"}\n')
        self.assertEqual(self.drive(), 2)
        self.assertEqual(self.logged("preclose"), [])

    def test_failed_run_leaves_no_output(self):
        self.assertEqual(self.drive(FAKE_CROSS_NO_ENVELOPE="1"), 2)
        out = self.tmp / "out"
        self.assertFalse(out.exists() and any(out.iterdir()))

    def test_envelope_handed_to_complete_is_this_runs(self):
        self.drive()
        complete = [a for a in self.logged("preclose") if "--complete" in a][0]
        cross = self.logged("cross")[0]
        envelope = complete[complete.index("--envelope") + 1]
        self.assertEqual(envelope, cross[cross.index("--out") + 1])
        self.assertNotEqual(Path(envelope).parent, self.tmp / "out")

    def test_failure_after_complete_exits_recorded_incomplete(self):
        # --complete exited 0 but the receipt, still readable, does not show
        # the pass yet: the exit code alone must make this exit 4.
        with mock.patch.object(preclose_driver.preclose_check, "find_receipt",
                               return_value=PLANNED), \
                mock.patch.object(preclose_driver.shutil, "copy2", side_effect=OSError("ENOSPC")):
            self.assertEqual(self.drive(), preclose_driver.RECORDED_INCOMPLETE)
        self.assertEqual(len([a for a in self.logged("preclose") if "--complete" in a]), 1)

    def test_receipt_gaining_a_pass_exits_4_even_when_complete_failed(self):
        one = {"status": "complete", "reviewed_sha": "a" * 40,
               "pass_history": [{"sha": "a" * 40, "surviving": 0, "epoch": 0}]}
        with mock.patch.object(preclose_driver.preclose_check, "find_receipt",
                               side_effect=[PLANNED, PLANNED, one]):
            self.assertEqual(self.drive(FAKE_COMPLETE_RC="1"), preclose_driver.RECORDED_INCOMPLETE)

    def test_failure_before_complete_exits_two(self):
        with mock.patch.object(preclose_driver.preclose_check, "find_receipt",
                               side_effect=[None, None, None]):
            self.assertEqual(self.drive(FAKE_CROSS_NO_ENVELOPE="1"), 2)

    def test_unreadable_real_receipt_after_complete_started_exits_4(self):
        # The real reader: preclose_check.read_receipt returns None for a file
        # it cannot parse, it never raises.
        store = self.tmp / "receipts"
        store.mkdir()
        pc = preclose_driver.preclose_check
        with mock.patch.object(pc, "find_receipt", REAL_FIND_RECEIPT), \
                mock.patch.object(pc, "receipt_dir", return_value=store), \
                mock.patch.object(pc, "legacy_receipt_dir", return_value=self.tmp / "legacy"):
            path = pc.receipt_path("vitalharmony/harmonic-forge", 7)
            path.write_text(json.dumps(PLANNED))
            self.assertEqual(self.drive(FAKE_CORRUPT=str(path), FAKE_COMPLETE_RC="1"),
                             preclose_driver.RECORDED_INCOMPLETE)

    def test_unreadable_receipt_after_failure_is_not_retryable(self):
        with mock.patch.object(preclose_driver.preclose_check, "find_receipt",
                               side_effect=[None, None, ValueError("corrupt")]):
            self.assertEqual(self.drive(FAKE_CROSS_NO_ENVELOPE="1"),
                             preclose_driver.RECORDED_INCOMPLETE)

    def test_complete_refusal_exits_nonzero(self):
        # A real plan leaves a planned receipt, so --complete's refusal reads
        # back as "no new pass": retryable.
        with mock.patch.object(preclose_driver.preclose_check, "find_receipt",
                               return_value=PLANNED):
            self.assertEqual(self.drive(FAKE_COMPLETE_RC="1"), 2)
        self.assertFalse((self.tmp / "out" / "preclose.json").exists())

    def test_missing_usage_records_cost_unavailable(self):
        self.assertEqual(self.drive(FAKE_NO_USAGE="1"), 0)
        complete = [a for a in self.logged("preclose") if "--complete" in a][0]
        self.assertIn("--cost-unavailable", complete)
        self.assertNotIn("--panel-tokens", complete)

    def test_no_acceptance_criteria_refuses(self):
        self.assertEqual(self.drive(FAKE_BODY="## Problem\n\nnone\n"), 2)
        self.assertEqual(self.logged("claude"), [])


class RunnerEnv(unittest.TestCase):
    def test_key_mode_and_token_strip(self):
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / "anthropic-key"
            key.write_text("sk-test\n")
            key.chmod(0o644)
            with mock.patch.object(preclose_driver, "RUNNER_KEY", key), \
                    mock.patch.dict(os.environ, {"GH_TOKEN": "t", "GITHUB_TOKEN": "u"}):
                with self.assertRaises(preclose_driver.DriverError):
                    preclose_driver.runner_env()
                key.chmod(0o600)
                env = preclose_driver.runner_env()
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-test")
        self.assertEqual(env["GH_CONFIG_DIR"], str(preclose_driver.RUNNER_GH))

    def test_missing_key_refuses(self):
        with mock.patch.object(preclose_driver, "RUNNER_KEY", Path("/nonexistent/key")):
            with self.assertRaises(preclose_driver.DriverError):
                preclose_driver.runner_env()


if __name__ == "__main__":
    unittest.main()
