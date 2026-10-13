#!/usr/bin/env python3
"""AC1-AC3 and AC8's parse (harmonic-forge#895): IMC on a BATCH line.

Every state path -- the BATCH store and the IMC state dir -- is a temp dir,
and every label read is a fake. Nothing here can reach the live store or gh.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "gh"))
import batch_auth as ba  # noqa: E402
import expand_lane_shorthand as m  # noqa: E402
import imc_waiver  # noqa: E402

RECORD_FIELDS = {"key", "repo", "issue", "granted_at", "session_id", "line", "hook_version"}


class LabelReader:
    def __init__(self, labels: dict[tuple[str, int], list[str] | None] | None = None,
                 default: list[str] | None = ("tooling-exception",)) -> None:
        self.labels = labels or {}
        self.default = list(default) if default is not None else None
        self.calls: list[tuple[str, int]] = []

    def __call__(self, repo: str, number: int):
        self.calls.append((repo, number))
        labels = self.labels.get((repo, number), self.default)
        return (labels, "") if labels is not None else (None, "the label read timed out after 5s")


class ImcFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "imc"
        self.batch = self.tmp / "batch-authorized.json"
        for target, attr, value in ((imc_waiver, "IMC_STATE_DIR", self.state),
                                    (ba, "STATE_PATH", self.batch)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"LANE": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)

    def imc(self, prompt: str, lane: str | None = "1", reader: LabelReader | None = None) -> str:
        return m.authorize_imc(prompt, "sess-42", lane, label_reader=reader or LabelReader())

    def records(self) -> list[str]:
        return sorted(p.name for p in self.state.glob("*.json")) if self.state.exists() else []

    def run_main(self, prompt: str, reader: LabelReader | None = None) -> str:
        out = io.StringIO()
        payload = {"prompt": prompt, "session_id": "sess-main"}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(m, "read_issue_labels", reader or LabelReader()), \
                mock.patch.object(m, "fetch_issue_context", return_value="ctx"), \
                contextlib.redirect_stdout(out):
            m.main()
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else ""


class Ac1RecordTests(ImcFixture):
    def test_batch_imc_writes_record_with_every_field_and_batch_grant_unchanged(self) -> None:
        context = self.run_main("BATCH F892 IMC F892")
        record = json.loads((self.state / "F892.json").read_text())
        self.assertEqual(set(record), RECORD_FIELDS)
        self.assertEqual(record["key"], "F892")
        self.assertEqual(record["repo"], "vitalharmony/harmonic-forge")
        self.assertEqual(record["issue"], 892)
        self.assertEqual(record["session_id"], "sess-main")
        self.assertEqual(record["line"], "BATCH F892 IMC F892")
        self.assertEqual(record["hook_version"], imc_waiver.HOOK_VERSION)
        self.assertIn("IMC: waiver hook record written for F892", context)
        self.assertIn("BATCH: authorized F892", context)
        state = ba._load(self.batch)
        self.assertEqual([t["action"] for t in state["F892"]["targets"]],
                         ["gh pr merge", "gh pr merge"])

    def test_keys_after_imc_only(self) -> None:
        receipt = self.imc("BATCH F874 IMC F892, F893 — implement in that order")
        self.assertIn("F892, F893", receipt)
        self.assertEqual(self.records(), ["F892.json", "F893.json"])

    def test_a_prompt_without_imc_returns_nothing(self) -> None:
        self.assertEqual(self.imc("BATCH F892"), "")
        self.assertEqual(self.imc("just text"), "")


class Ac2RefusalTests(ImcFixture):
    def assertRefusedWithGrant(self, prompt: str, reason: str, lane: str | None = "1",
                               reader: LabelReader | None = None,
                               batch_expected: bool = True) -> None:
        with mock.patch.dict(os.environ, {"LANE": lane} if lane is not None else {}, clear=False):
            if lane is None:
                os.environ.pop("LANE", None)
            context = self.run_main(prompt, reader)
        self.assertEqual(self.records(), [], prompt)
        self.assertIn("IMC REFUSED", context)
        self.assertIn(reason, context)
        if batch_expected:
            self.assertIn("F892", ba._load(self.batch), "the BATCH grant must be unaffected")

    def test_imc_without_batch_first(self) -> None:
        self.assertRefusedWithGrant("BATCH F892\nIMC F892", "IMC needs BATCH first")

    def test_imc_mid_line_not_starting_with_batch(self) -> None:
        context = self.run_main("please IMC F892 now")
        self.assertIn("IMC needs BATCH first", context)
        self.assertEqual(self.records(), [])

    def test_imc_in_fence(self) -> None:
        self.assertRefusedWithGrant("BATCH F892\n```\nBATCH F892 IMC F892\n```",
                                    "fenced block or blockquote")

    def test_imc_in_blockquote(self) -> None:
        self.assertRefusedWithGrant("BATCH F892\n> BATCH F892 IMC F892", "fenced block or blockquote")

    def test_lowercase_imc(self) -> None:
        self.assertRefusedWithGrant("BATCH F892 imc F892", "must be uppercase")

    def test_provenance_refusal(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk-py"}):
            context = self.run_main("BATCH F892 IMC F892")
        self.assertEqual(self.records(), [])
        self.assertIn("IMC REFUSED for F892", context)
        self.assertIn("CLAUDE_CODE_ENTRYPOINT", context)

    def test_lane_not_one(self) -> None:
        for lane in (None, "2", "3"):
            with self.subTest(lane=lane):
                self.assertRefusedWithGrant("BATCH F892 IMC F892", f"LANE is {lane!r}", lane=lane)

    def test_issue_without_label(self) -> None:
        self.assertRefusedWithGrant("BATCH F892 IMC F892", "does not carry `tooling-exception`",
                                    reader=LabelReader(default=["enhancement"]))

    def test_failed_label_read(self) -> None:
        self.assertRefusedWithGrant("BATCH F892 IMC F892", "timed out",
                                    reader=LabelReader(default=None))

    def test_cross_account_prefix(self) -> None:
        context = self.run_main("BATCH K12 IMC K12")
        self.assertEqual(self.records(), [])
        self.assertIn("cross-account", context)
        self.assertIn("K12", ba._load(self.batch))

    def test_one_refusal_does_not_block_another_key(self) -> None:
        reader = LabelReader({("vitalharmony/harmonic-forge", 893): ["bug"]})
        receipt = self.imc("BATCH F892 IMC F892 F893", reader=reader)
        self.assertEqual(self.records(), ["F892.json"])
        self.assertIn("IMC REFUSED for F893", receipt)


class Ac3LabelReadTests(ImcFixture):
    def test_five_keys_two_repos_five_reads_no_extra_rereads(self) -> None:
        reader = LabelReader()
        fetches: list = []
        out = io.StringIO()
        prompt = "BATCH IMC F892, F893, F894, H101, H102"
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt}))), \
                mock.patch.object(m, "read_issue_labels", reader), \
                mock.patch.object(m, "fetch_issue_context",
                                  side_effect=lambda r, n: fetches.append((r, n)) or "ctx"), \
                contextlib.redirect_stdout(out):
            m.main()
        self.assertEqual(len(reader.calls), 5)
        self.assertEqual({r for r, _ in reader.calls},
                         {"vitalharmony/harmonic-forge", "vitalharmony/hrse"})
        # The R-0212 re-read is what it was before IMC: capped, never extended.
        self.assertEqual(len(fetches), m._MAX_LIVE_FETCHES)
        self.assertEqual(len(self.records()), 5)

    def test_shim_refusal_is_no_waiver(self) -> None:
        import subprocess  # noqa: PLC0415

        done = subprocess.CompletedProcess([], 3, "", "gh-shim: refused: below budget floor\n")
        with mock.patch.object(m.subprocess, "run", return_value=done):
            labels, why = m.read_issue_labels("vitalharmony/harmonic-forge", 892)
        self.assertIsNone(labels)
        self.assertIn("budget floor", why)
        reader = lambda repo, n: (labels, why)  # noqa: E731
        receipt = self.imc("BATCH F892 IMC F892", reader=reader)
        self.assertIn("IMC REFUSED for F892", receipt)
        self.assertEqual(self.records(), [])

    def test_label_read_shape(self) -> None:
        import subprocess  # noqa: PLC0415

        done = subprocess.CompletedProcess([], 0, json.dumps(
            {"labels": [{"name": "tooling-exception"}]}), "")
        with mock.patch.object(m.subprocess, "run", return_value=done) as run:
            labels, _ = m.read_issue_labels("vitalharmony/harmonic-forge", 892)
        self.assertEqual(labels, ["tooling-exception"])
        self.assertEqual(run.call_args.args[0][:7],
                         ["gh", "issue", "view", "892", "--repo", "vitalharmony/harmonic-forge",
                          "--json"])


class Ac8DocTests(unittest.TestCase):
    def test_parse_named_directives_returns_imc_from_real_doc(self) -> None:
        directives = m.parse_named_directives(m.DOC_PATH.read_text(encoding="utf-8"))
        self.assertIn("IMC", directives)
        self.assertIn("R-0229", directives["IMC"])
        self.assertIn("mints no authority", directives["IMC"])


class Ac7DerivationTests(unittest.TestCase):
    """AC7: an IMC branch with a title suffix and no `link_pr` record resolves
    through `decide()`'s derivation. `batch_auth.py` does not change."""

    def test_imc_merge_derives_without_link_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "batch-authorized.json"
            ba.authorize(["F892"], actions=["gh pr merge", "gh pr merge"], state_path=state)
            carriers = ("fix/892-imc-waiver", "IMC waiver (harmonic-forge#892)")
            with mock.patch.object(ba, "_pr_carriers", lambda repo, number: carriers):
                verdict = ba.decide("gh pr merge 1000 --repo vitalharmony/harmonic-forge --squash",
                                    state_path=state)
        self.assertEqual(verdict[0], "allow", verdict)


if __name__ == "__main__":
    unittest.main()
