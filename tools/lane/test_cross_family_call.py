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


if __name__ == "__main__":
    unittest.main()
