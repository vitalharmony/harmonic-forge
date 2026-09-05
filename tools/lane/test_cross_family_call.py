#!/usr/bin/env python3
"""Tests for cross_family_call.sh's `verify` posture (harmonic-forge#448).

None of these tests invokes a sibling CLI. That is a hard requirement, not a
convenience: a real `verify` call spawns a Codex process that costs an API
round-trip and whose output is nondeterministic, so a suite that made one
would be slow, flaky, and — worse — would quietly stop being a test of *this
script* and become a test of the model's cooperation.

Two techniques keep everything covered without a spawn:

  * The argument guards (`--posture verify` validation, the `--cwd`
    requirement, the Codex-only target check) all `exit 2` BEFORE the dispatch
    loop is reached, so invoking the script directly exercises them for real.
  * `emit_envelope` is sourced out of the script and called in isolation, so
    the normalization logic is tested against handcrafted reports — including
    the malformed and adversarial shapes a live run would almost never produce
    on demand.

Test-case numbering follows the Implementation Spec's "Test Cases (for Lane
3)" list where one applies.
"""

from __future__ import annotations

import re
import shutil
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "cross_family_call.sh"


def make_stub_path(tmpdir: str) -> str:
    """A PATH whose `codex`/`claude`/`gemini` are inert stubs.

    Required, not tidiness: a `--posture` that clears every guard falls
    through to the dispatch loop and really does exec the sibling CLI. Without
    this, `test_existing_postures_still_accepted` alone spent ~59s making live
    API calls — a unit suite silently billing an account and depending on a
    network round-trip to pass. The stubs emit a valid envelope-shaped reply
    so dispatch completes normally and the guard assertions stay meaningful.
    """
    stub_dir = Path(tmpdir) / "stubbin"
    stub_dir.mkdir(exist_ok=True)
    for name in ("codex", "claude", "gemini"):
        stub = stub_dir / name
        stub.write_text(
            '#!/usr/bin/env bash\n'
            'echo \'{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"{\\"summary\\":\\"stub\\",\\"findings\\":[]}"}}\'\n'
        )
        stub.chmod(0o755)
    return f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def run_script(*args: str, path: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def emit_envelope(family: str, posture: str, exit_code: int, native_text: str) -> dict:
    """Call the script's own `emit_envelope` with a controlled native payload.

    Sourcing stops at the dispatch loop by running with no arguments under a
    guard: the script would `exit 2` on argument validation, so instead the
    function is extracted by sourcing a trimmed copy that stops before the
    argument parser.
    """
    source = SCRIPT.read_text()
    # Everything from `emit_envelope()` to the dispatch comment is the unit
    # under test; the preamble is skipped so argument validation never runs.
    start = source.index("emit_envelope() {")
    end = source.index("# --- dispatch ---")
    body = "set -euo pipefail\n" + source[start:end]

    with tempfile.TemporaryDirectory() as tmp:
        native = Path(tmp) / "native"
        native.write_text(native_text)
        harness = Path(tmp) / "harness.sh"
        harness.write_text(
            body + f'\nemit_envelope {family} {posture} {exit_code} "{native}"\n'
        )
        proc = subprocess.run(
            ["bash", str(harness)], capture_output=True, text=True, check=True
        )
    return json.loads(proc.stdout)


def codex_native(report: object) -> str:
    """A Codex `--json` stream whose final agent_message carries `report`."""
    return json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message",
                     "text": json.dumps(report)},
        }
    ) + "\n"


class TestVerifyPostureGuards(unittest.TestCase):
    """The guards that run before any sibling CLI is spawned."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.brief = Path(self.tmp.name) / "brief.md"
        self.brief.write_text("a cold brief\n")
        # Every test in this class may reach dispatch; none may spawn a real
        # sibling CLI. See make_stub_path.
        self.path = make_stub_path(self.tmp.name)

    def test_verify_is_an_accepted_posture(self):
        """It must fail for a *reason*, not for being an unknown posture."""
        result = run_script("--caller", "claude", "--families", "2",
                            "--posture", "verify", "--brief", str(self.brief),
                            path=self.path)
        self.assertNotIn("--posture must be", result.stderr)

    def test_verify_requires_cwd(self):
        result = run_script("--caller", "claude", "--families", "2",
                            "--posture", "verify", "--brief", str(self.brief))
        self.assertEqual(result.returncode, 2)
        self.assertIn("--cwd PATH is required", result.stderr)
        self.assertIn("verify posture", result.stderr)

    def test_verify_rejects_nonexistent_cwd(self):
        result = run_script("--caller", "claude", "--families", "2",
                            "--posture", "verify", "--brief", str(self.brief),
                            "--cwd", "/nonexistent/path/xyz")
        self.assertEqual(result.returncode, 2)

    def test_verify_permits_claude_caller_two_families(self):
        """`--caller claude --families 2` resolves to codex alone — the one
        combination `verify` supports. It must get past every guard."""
        result = run_script("--caller", "claude", "--families", "2",
                            "--posture", "verify", "--brief", str(self.brief),
                            "--cwd", self.tmp.name, path=self.path)
        self.assertNotIn("Codex-only", result.stderr)

    def test_verify_rejects_three_families(self):
        """TC: `--families 3` adds gemini, which has no verify implementation.
        It must exit non-zero rather than silently hand Gemini a posture whose
        guarantees live only in the Codex branch."""
        result = run_script("--caller", "claude", "--families", "3",
                            "--posture", "verify", "--brief", str(self.brief),
                            "--cwd", self.tmp.name)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Codex-only", result.stderr)
        self.assertIn("gemini", result.stderr)

    def test_verify_rejects_codex_caller(self):
        """A codex caller's primary sibling is claude, whose `invoke_claude`
        takes no posture argument at all."""
        result = run_script("--caller", "codex", "--families", "2",
                            "--posture", "verify", "--brief", str(self.brief),
                            "--cwd", self.tmp.name)
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude", result.stderr)

    def test_verify_rejects_gemini_caller(self):
        result = run_script("--caller", "gemini", "--families", "2",
                            "--posture", "verify", "--brief", str(self.brief),
                            "--cwd", self.tmp.name)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Codex-only", result.stderr)

    def test_existing_postures_still_accepted(self):
        """#448 must not narrow the two postures that already worked."""
        for posture in ("read-only", "probe"):
            with self.subTest(posture=posture):
                result = run_script("--caller", "claude", "--families", "2",
                                    "--posture", posture, "--brief",
                                    str(self.brief), "--cwd", self.tmp.name,
                                    path=self.path)
                self.assertNotIn("--posture must be", result.stderr)
                self.assertNotIn("Codex-only", result.stderr)


class TestVerifyEnvelopeNormalization(unittest.TestCase):
    """`emit_envelope`'s per-assumption verdict handling.

    Every normalization is a DOWNGRADE. The suite asserts that property
    directly (`test_normalization_never_upgrades`) as well as case by case,
    because "a malformed report can only come out weaker" is the actual
    security claim — a single case passing does not establish it.
    """

    def test_confirmed_with_evidence_survives(self):
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "checked", "findings": [],
            "assumptions": [{"assumption": "a", "verdict": "confirmed",
                             "evidence": "$ grep -c foo bar\n3"}],
        }))
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["report"]["assumptions"][0]["verdict"], "confirmed")

    def test_refuted_with_evidence_survives(self):
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "checked", "findings": [],
            "assumptions": [{"assumption": "a", "verdict": "refuted",
                             "evidence": "actual output"}],
        }))
        self.assertEqual(env["report"]["assumptions"][0]["verdict"], "refuted")

    def test_confirmed_without_evidence_downgrades(self):
        """The core anti-confabulation check: a verdict asserted with no
        executed evidence is exactly the failure this issue exists to catch."""
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "checked", "findings": [],
            "assumptions": [{"assumption": "a", "verdict": "confirmed",
                             "evidence": ""}],
        }))
        self.assertEqual(env["report"]["assumptions"][0]["verdict"], "uncheckable")

    def test_confirmed_with_missing_evidence_key_downgrades(self):
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "checked", "findings": [],
            "assumptions": [{"assumption": "a", "verdict": "confirmed"}],
        }))
        self.assertEqual(env["report"]["assumptions"][0]["verdict"], "uncheckable")

    def test_confirmed_with_whitespace_only_evidence_downgrades(self):
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "checked", "findings": [],
            "assumptions": [{"assumption": "a", "verdict": "confirmed",
                             "evidence": "   \n\t  "}],
        }))
        self.assertEqual(env["report"]["assumptions"][0]["verdict"], "uncheckable")

    def test_unknown_verdict_token_downgrades(self):
        """The verdict set is closed. An invented token like `likely` must not
        pass through to a consumer that only knows three values."""
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "checked", "findings": [],
            "assumptions": [{"assumption": "a", "verdict": "likely",
                             "evidence": "some real output"}],
        }))
        self.assertEqual(env["report"]["assumptions"][0]["verdict"], "uncheckable")

    def test_uncheckable_without_evidence_is_legitimate(self):
        """`uncheckable` is the one verdict that needs no evidence — demanding
        it would push the model back toward inventing some."""
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "checked", "findings": [],
            "assumptions": [{"assumption": "a", "verdict": "uncheckable",
                             "evidence": ""}],
        }))
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["report"]["assumptions"][0]["verdict"], "uncheckable")

    def test_missing_assumptions_key_is_invalid_report(self):
        """A verify pass that returned no verdicts produced nothing, even if
        it is a perfectly well-formed base report."""
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "checked", "findings": [],
        }))
        self.assertEqual(env["status"], "invalid-report")
        self.assertIsNone(env["report"])

    def test_non_array_assumptions_is_invalid_report(self):
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "checked", "findings": [], "assumptions": "several",
        }))
        self.assertEqual(env["status"], "invalid-report")

    def test_empty_assumptions_array_is_valid(self):
        """Distinct from a missing key: an explicit empty list is a reviewer
        saying "the brief asserted nothing", which is a real answer."""
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "nothing asserted", "findings": [], "assumptions": [],
        }))
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["report"]["assumptions"], [])

    def test_mixed_verdicts_each_handled_independently(self):
        env = emit_envelope("codex", "verify", 0, codex_native({
            "summary": "checked", "findings": [],
            "assumptions": [
                {"assumption": "a", "verdict": "confirmed", "evidence": "out"},
                {"assumption": "b", "verdict": "refuted", "evidence": ""},
                {"assumption": "c", "verdict": "uncheckable", "evidence": ""},
                {"assumption": "d", "verdict": "nonsense", "evidence": "out"},
            ],
        }))
        self.assertEqual(
            [a["verdict"] for a in env["report"]["assumptions"]],
            ["confirmed", "uncheckable", "uncheckable", "uncheckable"],
        )

    def test_normalization_never_upgrades(self):
        """The security property stated as a property, not as N examples: for
        every verdict/evidence combination, the result is never stronger than
        what the model claimed."""
        strength = {"uncheckable": 0, "confirmed": 1, "refuted": 1}
        for verdict in ("confirmed", "refuted", "uncheckable", "bogus"):
            for evidence in ("", "   ", "real output"):
                with self.subTest(verdict=verdict, evidence=repr(evidence)):
                    env = emit_envelope("codex", "verify", 0, codex_native({
                        "summary": "s", "findings": [],
                        "assumptions": [{"assumption": "a", "verdict": verdict,
                                         "evidence": evidence}],
                    }))
                    got = env["report"]["assumptions"][0]["verdict"]
                    self.assertIn(got, strength)
                    self.assertLessEqual(
                        strength[got], strength.get(verdict, 0),
                        f"{verdict!r}+{evidence!r} was upgraded to {got!r}",
                    )

    def test_assumptions_untouched_for_non_verify_postures(self):
        """The extension is scoped to `verify`. A `read-only` report carrying
        an `assumptions` key must pass through byte-for-byte — #448 changes no
        behavior for the two existing postures."""
        env = emit_envelope("codex", "read-only", 0, codex_native({
            "summary": "checked", "findings": [],
            "assumptions": [{"assumption": "a", "verdict": "confirmed",
                             "evidence": ""}],
        }))
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["report"]["assumptions"][0]["verdict"], "confirmed")

    def test_process_error_still_short_circuits_under_verify(self):
        env = emit_envelope("codex", "verify", 1, "")
        self.assertEqual(env["status"], "process-error")
        self.assertIsNone(env["report"])

    def test_invalid_base_report_is_not_reclassified(self):
        """A reply that was never valid JSON stays `invalid-report`; the
        verify branch must not run on it and must not mask it."""
        env = emit_envelope("codex", "verify", 0, codex_native("not an object"))
        self.assertEqual(env["status"], "invalid-report")


class TestVerifyContractCarriesReadOnlyBoundary(unittest.TestCase):
    """The reviewer's gh-mutation boundary is prose only, so the prose must
    actually be there (harmonic-forge#448, operator decision 2026-09-03).

    This exists because the boundary was once claimed in a completion report
    while no such instruction was present in either contract — the same
    confabulation class the whole issue targets. A test is the only thing that
    makes "the brief tells it not to" a checkable statement instead of a
    remembered one.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text()
        start = cls.source.index("read -r -d '' VERIFY_CONTRACT")
        cls.contract = cls.source[start:cls.source.index("\nEOF", start)]

    def test_verify_contract_forbids_mutation(self):
        self.assertIn("READ-ONLY reviewer", self.contract)
        self.assertIn("Do not mutate anything", self.contract)

    def test_verify_contract_names_the_specific_write_commands(self):
        """A generic "don't mutate" is easy to reason past; the named
        commands are the ones that actually matter here."""
        for command in ("gh issue close", "gh pr merge", "gh issue comment"):
            with self.subTest(command=command):
                self.assertIn(command, self.contract)

    def test_verify_contract_still_permits_reads(self):
        """The reviewer's whole purpose is running read commands — an
        over-broad prohibition would make every assumption uncheckable."""
        self.assertIn("gh issue view", self.contract)
        self.assertIn("use them freely", self.contract)

    def test_mutation_requiring_assumption_routes_to_uncheckable(self):
        self.assertIn("uncheckable", self.contract.split("READ-ONLY reviewer")[1])

    def test_boundary_is_helper_appended_not_caller_supplied(self):
        """It must ship in the contract the helper appends, so a brief that
        never thinks to include it still gets it."""
        self.assertIn('"$VERIFY_CONTRACT"', self.source)


class TestAC7FrozenReplay(unittest.TestCase):
    """AC7 — replay against hrse#1530's real handoff (Implementation Spec).

    The spec supersedes the original handoff's framing here, and the
    distinction matters. The expected evidence is NOT "zero real contacts":
    hrse#1530's own later correction puts the true scope at 27 test fixtures,
    2 gate fixtures and 1 real contact (Crystal Alvarez, added deliberately,
    carrying no `status`). What is asserted is the verdict token `refuted`
    **as to magnitude** plus the presence of executed evidence — against a
    frozen query and its dated result, never a live re-count against a
    population that grows every time someone adds a fixture.
    """

    @classmethod
    def setUpClass(cls) -> None:
        fixture = Path(__file__).resolve().parent / "testdata" / "f448_h1530_replay.json"
        cls.fixture = json.loads(fixture.read_text())

    def test_replay_yields_refuted_with_evidence(self):
        env = emit_envelope(
            "codex", "verify", 0, codex_native(self.fixture["reviewer_report"])
        )
        self.assertEqual(env["status"], "ok")
        assumption = env["report"]["assumptions"][0]
        self.assertEqual(assumption["verdict"], "refuted")
        self.assertTrue(assumption["evidence"].strip(),
                        "a refuted verdict must carry executed evidence")

    def test_replay_evidence_cites_the_frozen_dated_result(self):
        """The evidence must be the recorded run, not a re-derivation."""
        evidence = self.fixture["reviewer_report"]["assumptions"][0]["evidence"]
        self.assertIn(self.fixture["frozen_query_date"], evidence)
        self.assertIn(str(self.fixture["frozen_result"]["total_with_status"]), evidence)

    def test_fixture_records_the_corrected_scope_not_zero_real_contacts(self):
        """Guards against the superseded framing creeping back in: the
        corrected scope has exactly one real contact, not none."""
        result = self.fixture["frozen_result"]
        self.assertEqual(result["real_contacts"], 1)
        self.assertEqual(result["real_contact_names"], ["Crystal Alvarez"])
        self.assertEqual(
            result["test_fixtures"] + result["gate_fixtures"] + result["real_contacts"],
            result["total_with_status"],
        )

    def test_same_replay_without_evidence_would_not_pass_as_refuted(self):
        """The replay only passes because evidence is attached — strip it and
        the same report downgrades. Without this, the test above could pass
        for a report that merely asserted `refuted`."""
        report = json.loads(json.dumps(self.fixture["reviewer_report"]))
        report["assumptions"][0]["evidence"] = ""
        env = emit_envelope("codex", "verify", 0, codex_native(report))
        self.assertEqual(env["report"]["assumptions"][0]["verdict"], "uncheckable")


class TestAC2GeminiAuthMechanism(unittest.TestCase):
    """harmonic-forge#462: `invoke_gemini` must not unset the API keys, and
    must force `gemini-api-key` auth via a throwaway HOME rather than the
    operator's real `~/.gemini/settings.json` (which pins the discontinued
    `oauth-personal` tier)."""

    def test_no_unconditional_key_unset_remains(self) -> None:
        source = SCRIPT.read_text()
        self.assertNotIn("-u GOOGLE_API_KEY", source)
        self.assertNotIn("-u GEMINI_API_KEY", source)

    def test_gemini_invocation_gets_its_own_throwaway_home_selecting_the_key_auth_type(self) -> None:
        """A stub `gemini` records the HOME it was actually invoked with and
        that HOME's settings.json content, so this proves the mechanism --
        not just that the source text mentions it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stub_dir = Path(tmpdir) / "stubbin"
            stub_dir.mkdir()
            capture = Path(tmpdir) / "capture.json"
            for name in ("codex", "claude"):
                stub = stub_dir / name
                stub.write_text(
                    '#!/usr/bin/env bash\n'
                    'echo \'{"type":"item.completed","item":{"type":"agent_message",'
                    '"text":"{\\"summary\\":\\"stub\\",\\"findings\\":[]}"}}\'\n'
                )
                stub.chmod(0o755)
            gemini_stub = stub_dir / "gemini"
            gemini_stub.write_text(
                '#!/usr/bin/env bash\n'
                f'settings=$(cat "$HOME/.gemini/settings.json" 2>/dev/null || echo "MISSING")\n'
                f'printf \'{{"home":"%s","settings":%s,"gemini_key":"%s","google_key":"%s"}}\' '
                f'"$HOME" "$settings" "${{GEMINI_API_KEY:-UNSET}}" "${{GOOGLE_API_KEY:-UNSET}}" '
                f'> "{capture}"\n'
                'echo \'{"summary": "stub"}\'\n'
            )
            gemini_stub.chmod(0o755)
            brief = Path(tmpdir) / "brief.md"
            brief.write_text("cold brief\n")
            cwd = Path(tmpdir) / "scratch"
            cwd.mkdir()

            # preclose-inspection finding: never capture a REAL secret into a
            # file, even one meant to be discarded -- both keys are
            # overridden to dummy values before the script ever runs, so
            # whatever the stub writes to disk is a fixture, not a live
            # credential. `GEMINI_CLI_HOME` is also stripped from the test's
            # own environment so a real one (if ever set on a dev machine)
            # can't mask the fix's own `-u GEMINI_CLI_HOME` from mattering.
            env = dict(os.environ)
            env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
            env["GEMINI_API_KEY"] = "test-gemini-key"
            env["GOOGLE_API_KEY"] = "test-google-key"
            env.pop("GEMINI_CLI_HOME", None)
            real_home = env.get("HOME", "")
            subprocess.run(
                ["bash", str(SCRIPT), "--caller", "claude", "--families", "3",
                 "--posture", "read-only", "--brief", str(brief), "--cwd", str(cwd)],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env,
            )

            self.assertTrue(capture.exists(), "gemini stub never ran")
            captured = json.loads(capture.read_text())
            self.assertNotEqual(captured["home"], real_home,
                                 "gemini must run under a throwaway HOME, not the operator's real one")
            self.assertEqual(
                captured["settings"],
                {"security": {"auth": {"selectedType": "gemini-api-key"}}},
            )
            self.assertEqual(captured["gemini_key"], "test-gemini-key",
                              "GEMINI_API_KEY must reach the gemini process, not be unset")
            self.assertEqual(captured["google_key"], "test-google-key",
                              "GOOGLE_API_KEY must also reach the gemini process, not be unset")

    def test_a_real_gemini_cli_home_does_not_defeat_the_throwaway_home(self) -> None:
        """preclose-inspection finding: Gemini's own config-root resolution
        checks GEMINI_CLI_HOME before HOME, so `env HOME=...` alone does not
        force the throwaway settings if the caller's environment happens to
        export GEMINI_CLI_HOME -- it must be explicitly unset."""
        with tempfile.TemporaryDirectory() as tmpdir:
            stub_dir = Path(tmpdir) / "stubbin"
            stub_dir.mkdir()
            capture = Path(tmpdir) / "capture.json"
            for name in ("codex", "claude"):
                stub = stub_dir / name
                stub.write_text(
                    '#!/usr/bin/env bash\n'
                    'echo \'{"type":"item.completed","item":{"type":"agent_message",'
                    '"text":"{\\"summary\\":\\"stub\\",\\"findings\\":[]}"}}\'\n'
                )
                stub.chmod(0o755)
            gemini_stub = stub_dir / "gemini"
            # Mirrors the installed CLI's own precedence: GEMINI_CLI_HOME
            # wins over HOME if the fix fails to strip it.
            gemini_stub.write_text(
                '#!/usr/bin/env bash\n'
                'resolved_home="${GEMINI_CLI_HOME:-$HOME}"\n'
                f'printf \'{{"resolved_home":"%s","gemini_cli_home_seen":"%s"}}\' '
                '"$resolved_home" "${GEMINI_CLI_HOME:-UNSET}" '
                f'> "{capture}"\n'
                'echo \'{"summary": "stub"}\'\n'
            )
            gemini_stub.chmod(0o755)
            brief = Path(tmpdir) / "brief.md"
            brief.write_text("cold brief\n")
            cwd = Path(tmpdir) / "scratch"
            cwd.mkdir()

            fake_real_gemini_cli_home = str(Path(tmpdir) / "operators-real-gemini-home")
            env = dict(os.environ)
            env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
            env["GEMINI_CLI_HOME"] = fake_real_gemini_cli_home
            subprocess.run(
                ["bash", str(SCRIPT), "--caller", "claude", "--families", "3",
                 "--posture", "read-only", "--brief", str(brief), "--cwd", str(cwd)],
                capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env,
            )

            self.assertTrue(capture.exists(), "gemini stub never ran")
            captured = json.loads(capture.read_text())
            self.assertEqual(captured["gemini_cli_home_seen"], "UNSET",
                              "GEMINI_CLI_HOME must be stripped before invoking gemini")
            self.assertNotEqual(captured["resolved_home"], fake_real_gemini_cli_home,
                                 "a caller-set GEMINI_CLI_HOME must not survive into the gemini process")



class TestOversizedNativeStream(unittest.TestCase):
    """harmonic-forge#466 — payloads reach `jq` through files, never argv.

    Linux caps a *single* argument at `MAX_ARG_STRLEN` (32 pages = 131,072
    bytes), independently of `ARG_MAX` (2,097,152 on this machine), so the
    total-argument budget was never the constraint and raising it would not
    have helped. Reproduced before the fix at exit 126 with
    `/usr/bin/jq: Argument list too long` and an empty envelope.

    The failure scaled with review quality: a `read-only` review of a real
    codebase carries every tool call and reasoning item in its native stream,
    not just the final message. The observed run's stream was 754,034 bytes,
    5.75x the cap — the helper broke precisely when it was doing its job.
    """

    #: Comfortably past both the per-argument cap and `ARG_MAX`, so a fix that
    #: merely trimmed the payload under 128 KB would still fail here.
    TARGET_BYTES = 2 * 1024 * 1024

    def _big_codex_stream(self, report: object) -> str:
        """A Codex JSONL stream over `TARGET_BYTES` ending in a valid report.

        Bulked with `reasoning` items rather than one enormous message,
        because that is the real shape: the stream is long because the model
        did a lot of work, not because any single item is huge.
        """
        filler = "x" * 300
        lines = []
        size = 0
        while size < self.TARGET_BYTES:
            line = json.dumps({
                "type": "item.completed",
                "item": {"id": f"i{len(lines)}", "type": "reasoning", "text": filler},
            })
            lines.append(line)
            size += len(line) + 1
        lines.append(codex_native(report).strip())
        return "\n".join(lines) + "\n"

    def test_a_two_megabyte_stream_produces_a_well_formed_envelope(self):
        """TC1. Asserts the envelope's SHAPE, not merely a zero exit — the
        AC is explicit about that, because an exit code alone would pass
        against an empty stdout."""
        stream = self._big_codex_stream({"findings": [{"title": "a finding"}]})
        self.assertGreater(len(stream.encode()), self.TARGET_BYTES)
        env = emit_envelope("codex", "read-only", 0, stream)
        self.assertEqual(
            sorted(env),
            ["exit_code", "family", "native", "posture", "report", "status"])
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["report"], {"findings": [{"title": "a finding"}]})

    def test_the_oversized_native_stream_survives_intact(self):
        """Not merely present: every item of it. A fix that truncated the
        stream to fit would satisfy a shape check and lose the evidence the
        envelope exists to carry."""
        stream = self._big_codex_stream({"findings": []})
        expected_items = len(stream.strip().splitlines())
        env = emit_envelope("codex", "read-only", 0, stream)
        self.assertIsInstance(env["native"], list)
        self.assertEqual(len(env["native"]), expected_items)
        self.assertEqual(env["native"][-1]["item"]["type"], "agent_message")

    def test_an_oversized_report_also_survives(self):
        """`report_json` had the identical exposure. The handoff marks "the
        report never exceeds the cap" as ASSERTED and says not to rely on it,
        so it is tested rather than assumed."""
        big_report = {"findings": [{"title": "f", "detail": "y" * 200_000}]}
        env = emit_envelope("codex", "read-only", 0, codex_native(big_report))
        self.assertEqual(env["status"], "ok")
        self.assertEqual(len(env["report"]["findings"][0]["detail"]), 200_000)

    def test_a_small_stream_is_unchanged(self):
        """TC2 — no behavioural change on the path that already worked."""
        env = emit_envelope("codex", "read-only", 0,
                            codex_native({"findings": [{"title": "small"}]}))
        self.assertEqual(env["status"], "ok")
        self.assertEqual(env["family"], "codex")
        self.assertEqual(env["exit_code"], 0)
        self.assertEqual(env["report"], {"findings": [{"title": "small"}]})

    def test_a_single_object_family_still_yields_the_object_not_an_array(self):
        """`--slurpfile` reads a file into an ARRAY, so the single-object
        families need the `[0]` index. Without it `native` would silently
        become a one-element list — valid JSON, wrong shape, and invisible to
        any test that only checked the envelope's keys."""
        env = emit_envelope("claude", "read-only", 0,
                            json.dumps({"result": json.dumps({"findings": []})}))
        self.assertIsInstance(env["native"], dict)
        self.assertIn("result", env["native"])

    def test_a_malformed_native_stream_still_produces_an_envelope(self):
        """The `|| null` fallback the Codex branch already had, now on all
        three. Losing the envelope entirely is the worse failure: the caller
        learns nothing, which is exactly this issue's complaint."""
        env = emit_envelope("gemini", "read-only", 0, "this is not json at all\n")
        self.assertIsNone(env["native"])
        self.assertEqual(env["status"], "invalid-report")

    def test_no_unbounded_value_is_passed_as_an_argv_element(self):
        """TC3, asserted structurally against the script's own text.

        The two variables that carried unbounded payloads must not exist at
        all any more — a `grep` for them is the whole check the AC asks for,
        and it stays true against future edits in a way a behavioural test
        on today's payload sizes would not.
        """
        source = SCRIPT.read_text()
        for name in ("native_json", "report_json"):
            self.assertNotIn(name, source,
                             f"${name} still exists; it is the argv exposure this issue removed")
        self.assertIn("--slurpfile", source)


class TestVerifyNormalizationSurvivesTheFileMove(unittest.TestCase):
    """harmonic-forge#466 TC4/TC5 — the behaviour that must NOT change.

    `TestVerifyVerdictNormalization` above already covers these paths and
    still passes untouched; these restate the two the handoff calls out by
    name, so a future reader sees them tied to this issue rather than having
    to infer the coverage.
    """

    def test_an_assumption_with_empty_evidence_still_downgrades(self):
        env = emit_envelope("codex", "verify", 0, codex_native({
            "findings": [],
            "assumptions": [{"claim": "c", "verdict": "confirmed", "evidence": "   "}],
        }))
        self.assertEqual(env["report"]["assumptions"][0]["verdict"], "uncheckable")

    def test_an_invalid_report_still_yields_invalid_report_and_a_null_report(self):
        env = emit_envelope("codex", "read-only", 0,
                            codex_native("not an object with findings"))
        self.assertEqual(env["status"], "invalid-report")
        self.assertIsNone(env["report"])



def parse_envelopes(stdout: str) -> list[dict]:
    """Decode the run's concatenated JSON envelopes.

    Not `splitlines()`: `jq` pretty-prints the success envelope across many
    lines while the failure envelope is one line, so a line-oriented parse
    reads `}` as a document. A streaming `raw_decode` handles both shapes and
    would keep working if either formatting changed.
    """
    decoder = json.JSONDecoder()
    out, index = [], 0
    text = stdout.strip()
    while index < len(text):
        value, index = decoder.raw_decode(text, index)
        out.append(value)
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
    return out


def make_failing_envelope_path(tmpdir: str, families=("codex", "claude", "gemini")) -> str:
    """A PATH whose sibling CLIs work but whose `jq` fails on the ENVELOPE step.

    Forcing the failure through `jq -n` rather than through an oversized
    payload is deliberate: harmonic-forge#466 fixed the oversize case, and a
    test that reproduced the failure only via E2BIG would stop exercising this
    issue's fix the moment that one landed. This makes the two independently
    verifiable, which TC6 asks for explicitly.

    Every other `jq` invocation delegates to the real binary, so the run
    reaches the envelope step normally and fails only there.
    """
    stub_dir = Path(tmpdir) / "failbin"
    stub_dir.mkdir(exist_ok=True)
    real_jq = shutil.which("jq")
    assert real_jq, "jq must be installed for these tests"
    (stub_dir / "jq").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-n" ]; then\n'
        '  echo "stub jq: refusing the envelope step" >&2\n'
        "  exit 1\n"
        "fi\n"
        f'exec {real_jq} "$@"\n'
    )
    (stub_dir / "jq").chmod(0o755)
    for name in ("codex", "claude", "gemini"):
        stub = stub_dir / name
        if name in families:
            stub.write_text(
                "#!/usr/bin/env bash\n"
                "echo '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\","
                "\"text\":\"{\\\"summary\\\":\\\"stub\\\",\\\"findings\\\":[]}\"}}'\n"
            )
        else:
            stub.write_text("#!/usr/bin/env bash\necho notjson\n")
        stub.chmod(0o755)
    return f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}"


class TestEnvelopeFailureIsLoud(unittest.TestCase):
    """harmonic-forge#467 — a lost review must never read as a clean pass.

    The incident: a `read-only` cross-family review printed a `jq` error and
    the calling harness reported *"Background command completed (exit code
    0)"*. A caller trusting that would have reported a successful red-team
    pass with zero findings — the worst possible outcome for a tool whose only
    purpose is an independent adversarial verdict. Silence read as "clean".
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.brief = Path(self.tmp.name) / "brief.md"
        self.brief.write_text("a cold brief\n")
        self.preserve = Path(self.tmp.name) / "preserved"
        self.path = make_failing_envelope_path(self.tmp.name)

    def _run(self, families="2", path=None):
        env_dir = str(self.preserve)
        os.environ["CROSS_FAMILY_PRESERVE_DIR"] = env_dir
        self.addCleanup(os.environ.pop, "CROSS_FAMILY_PRESERVE_DIR", None)
        return run_script("--caller", "claude", "--families", families,
                          "--posture", "read-only", "--brief", str(self.brief),
                          path=path or self.path)

    def test_a_failed_envelope_exits_non_zero(self):
        """TC1. The code itself, not merely that some output appeared."""
        self.assertNotEqual(self._run().returncode, 0)

    def test_the_diagnostic_names_the_family_the_posture_and_the_failure(self):
        """TC2. "Something went wrong" is not actionable; the raw shell error
        alone does not say which of three families produced it."""
        err = self._run().stderr
        self.assertIn("FAILED to build the result envelope", err)
        self.assertIn("read-only", err)
        self.assertRegex(err, r"family:\s+(codex|claude|gemini)")
        self.assertIn("envelope construction exited", err)

    def test_the_underlying_error_is_carried_not_replaced(self):
        """The named diagnostic is added to the raw error, never instead of
        it — the raw text is what identifies the actual cause next time."""
        self.assertIn("stub jq: refusing the envelope step", self._run().stderr)

    def test_the_native_output_is_preserved_at_a_printed_path(self):
        """TC3, the half that matters most. In the real incident the verdict
        survived only because a `mktemp` file happened to; the path was never
        printed and recovery was luck. Reading the printed path must yield the
        reviewer's actual content."""
        err = self._run().stderr
        match = re.search(r"native output preserved at: (\S+)", err)
        self.assertIsNotNone(match, f"no preserved path in stderr:\n{err}")
        preserved = Path(match.group(1))
        self.assertTrue(preserved.is_file(), f"{preserved} does not exist")
        self.assertIn("agent_message", preserved.read_text())

    def test_stdout_still_carries_a_row_for_the_failing_family(self):
        """AC4's first half: a family that failed must not simply vanish from
        the output. A consumer reading only stdout would otherwise see two
        envelopes where three were requested and have nothing to notice."""
        env = parse_envelopes(self._run().stdout)[-1]
        self.assertEqual(env["status"], "envelope-error")
        self.assertIsNone(env["report"])
        self.assertIn("native_preserved_at", env)

    def test_a_successful_run_is_unchanged(self):
        """TC5. Exit 0, no diagnostic, and the same envelope as today —
        asserted against the ORIGINAL stub PATH, whose jq is the real one."""
        good = make_stub_path(self.tmp.name)
        result = self._run(path=good)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("FAILED to build the result envelope", result.stderr)
        env = parse_envelopes(result.stdout)[-1]
        self.assertEqual(env["status"], "ok")
        self.assertEqual(sorted(env),
                         ["exit_code", "family", "native", "posture", "report", "status"])

    def test_one_family_failing_does_not_abort_the_others(self):
        """TC4. Before this, `set -e` killed the loop at the first failure:
        later families never ran, earlier ones had already printed, and
        nothing in the output said a family was missing."""
        result = run_script("--caller", "claude", "--families", "3",
                            "--posture", "read-only", "--brief", str(self.brief),
                            path=self.path)
        self.assertNotEqual(result.returncode, 0)
        envelopes = parse_envelopes(result.stdout)
        # `--families 3` resolves to TWO targets: the caller is excluded from
        # its own review. Asserted against the resolved target list rather
        # than the flag's number, which is the same distinction
        # harmonic-forge#448 draws for the verify guard.
        self.assertEqual(len(envelopes), 2, "a failing family dropped the whole run")
        self.assertTrue(all(e["status"] == "envelope-error" for e in envelopes))
        self.assertEqual(len({e["family"] for e in envelopes}), 2)


if __name__ == "__main__":
    unittest.main()
