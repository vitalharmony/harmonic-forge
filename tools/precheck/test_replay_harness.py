#!/usr/bin/env python3
"""Tests for replay_harness.py (harmonic-forge#1289).

Every failure log, overlay, store, sample and layer module is synthetic and
lives in a temp folder; nothing reads the operator's real store or sample.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import replay_harness as rh  # noqa: E402

DESIGN = HERE / "layersets" / "design-v1.json"
COMMAND_MARK = "uniquecommandmark"
PROBE_MARK = "uniqueprobemark"


def rec(ts: str, tool: str | None = None, klass: str | None = None) -> dict:
    return {"ts": ts, "caught_by": "self", "rule_existed": "none", "expected": "x", "happened": "y",
            "lane": "1", "issue": 7, "session": None, "turn": None, "tool": tool, "class": klass,
            "probe": PROBE_MARK, "class_list_version": 1 if klass else None}


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = self.tmp / "store"
        self.store.mkdir()
        self.memory = self.store / "protocol-failures.jsonl"
        self.overlays = self.store / "failure-overlays"
        self.overlays.mkdir()
        self.db = self.store / "failure-store.sqlite"
        self.sample = self.store / "never-refuse" / "never-refuse-sample-v1.jsonl"
        self.records: list[dict] = []
        self.overlay: list[dict] = []
        # The sample size is pinned at 10,000; the fixture sample is 4 rows.
        size = mock.patch.object(rh.never_refuse_sample, "SIZE", 4)
        size.start()
        self.addCleanup(size.stop)
        self.write_sample([f"ls /srv/{COMMAND_MARK}{i}" for i in range(4)])

    # fixture builders --------------------------------------------------

    def slip(self, effective: str, own: tuple[str | None, str | None] = (None, None)) -> None:
        """A record whose effective key comes from the overlay."""
        line = len(self.records)
        self.records.append(rec(f"2026-11-01T00:00:{line:02d}Z", *own))
        self.overlay.append({"log_line": line, "key_v1": effective})

    def unkeyed(self, own: str) -> None:
        tool, klass = own.split("|")
        self.records.append(rec(f"2026-11-01T00:01:{len(self.records):02d}Z", tool, klass))

    def write_store(self) -> None:
        self.memory.write_text("".join(json.dumps(r) + "\n" for r in self.records), encoding="utf-8")
        (self.overlays / "replay-overlay-v1.jsonl").write_text(
            "".join(json.dumps(o) + "\n" for o in self.overlay), encoding="utf-8")

    def write_sample(self, commands: list[str], size: int | None = None) -> None:
        self.sample.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"n": i, "id": f"t{i}", "ts": None, "session": None, "sidechain": False,
                 "version": "2.0.0", "cwd": "/srv/work", "class": "read", "command": c}
                for i, c in enumerate(commands, 1)]
        data = "".join(json.dumps(r) + "\n" for r in rows).encode("utf-8")
        self.sample.write_bytes(data)
        self.sample.with_name(self.sample.name + ".manifest.json").write_text(
            json.dumps({"version": 1, "size": len(rows) if size is None else size,
                        "sha256": hashlib.sha256(data).hexdigest()}),
            encoding="utf-8")

    def layer_set(self, layers: list[dict], existing: dict | None = None,
                  accepted: list[str] | None = None, raw: bool = False) -> Path:
        """Each layer without a `bar` gets a sample bar, which is
        not-measurable with no module, so tests about other fields still load;
        `raw=True` writes the layers exactly as given."""
        if not raw:
            layers = [layer if "bar" in layer else {**layer, "bar": {"sample_max_refusals": 0}}
                      for layer in layers]
        path = self.tmp / f"layers-{len(list(self.tmp.glob('layers-*')))}.json"
        path.write_text(json.dumps({"version": 1, "layers": layers, "existing": existing or {},
                                    "accepted_uncovered": accepted or []}), encoding="utf-8")
        return path

    def module(self, body: str) -> str:
        path = self.tmp / f"layer_{len(list(self.tmp.glob('layer_*.py')))}.py"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def run_score(self, layers: Path, *extra: str) -> tuple[int, str, str]:
        self.write_store()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = rh.main(["score", "--layers", str(layers), "--sample", str(self.sample),
                            "--db", str(self.db), "--memory-log", str(self.memory),
                            "--testplan-log", str(self.store / "testplan.jsonl"),
                            "--overlays", str(self.overlays), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def report(self, layers: Path, *extra: str) -> tuple[int, dict]:
        code, out, err = self.run_score(layers, "--json", *extra)
        self.assertIn(code, (0, 4), err)
        return code, json.loads(out)


# --------------------------------------------------------------------------
# AC1 decision 1: SQLite, no Neo4j.

class Replay(Harness):
    def test_replay_reads_failure_keys_overlaid(self):
        self.slip("git-push|shell")
        self.slip("gh-api|args")
        self.unkeyed("git-push|shell")
        layers = self.layer_set([{"name": "lint", "claims": ["*|shell"], "module": None}])
        _code, report = self.report(layers)
        replay = report["replay"]
        self.assertEqual(replay["total"], 2)
        self.assertEqual(replay["keyed_not_overlaid"], 1)
        self.assertEqual(replay["owners"], {"lint": 1, "UNCOVERED": 1})

    def test_no_neo4j_import(self):
        for name in ("replay_harness.py", "never_refuse_sample.py"):
            text = (HERE / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertIsNone(re.search(r"^\s*(import|from)\s+(neo4j|socket|urllib|http|requests)\b",
                                            text, re.M))

    def test_no_subprocess_call(self):
        for name in ("replay_harness.py", "never_refuse_sample.py"):
            text = (HERE / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertIsNone(re.search(r"^\s*(import|from)\s+subprocess\b|subprocess\.\w+\(|os\.system\(|os\.popen\(",
                                            text, re.M))

    def test_store_inside_forge_refuses(self):
        self.slip("gh-api|args")
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None}])
        self.db = HERE / "failure-store.sqlite"
        code, _out, err = self.run_score(layers)
        self.assertEqual(code, 2)
        self.assertFalse(self.db.exists())
        self.assertIn("inside a harmonic-forge checkout", err)


# --------------------------------------------------------------------------
# AC1 decision 2: the live environment.

class LiveEnvironment(Harness):
    def test_layer_receives_live_env_names(self):
        self.slip("gh-api|args")
        module = self.module("def refuses(command, cwd, env_names):\n"
                             "    return None if 'PRECHECK_FIXTURE_VAR' in env_names else 'unset'\n")
        layers = self.layer_set([{"name": "lint", "claims": ["*|shell"], "module": module,
                                  "bar": {"sample_max_refusals": 0}}])
        with mock.patch.dict(os.environ, {"PRECHECK_FIXTURE_VAR": "1"}):
            code, report = self.report(layers)
        self.assertEqual(report["layers"]["lint"]["refusals"], 0)
        self.assertEqual(code, 0)


# --------------------------------------------------------------------------
# AC1 decisions 3-5.

class Claims(Harness):
    def test_tool_class_claim_does_not_claim_another_tool(self):
        self.slip("git-push|args")
        self.slip("gh-pr-merge|args")
        layers = self.layer_set([{"name": "gate", "claims": ["git-push|args"], "module": None}])
        _code, report = self.report(layers)
        self.assertEqual(report["replay"]["owners"], {"gate": 1, "UNCOVERED": 1})

    def test_accepted_uncovered_is_reported_apart(self):
        self.slip("bash-string|exists")
        self.slip("gh-api|policy")
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None}],
                                existing={"policy": "existing deny hooks"}, accepted=["exists"])
        _code, report = self.report(layers)
        self.assertEqual(report["replay"]["owners"],
                         {"accepted-uncovered:exists": 1, "existing deny hooks": 1})

    def test_repeat_uses_effective_key(self):
        self.slip("gh-api|args", own=("git-push", "cwd"))
        self.slip("gh-api|args", own=("gh-pr-merge", "env"))
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None}])
        _code, report = self.report(layers)
        self.assertEqual(report["replay"]["repeat_owned"], 1)

    def test_repeat_unowned_excludes_claimed(self):
        for key in ("gh-api|args", "gh-api|args", "gh-api|policy", "gh-api|policy"):
            self.slip(key)
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None}],
                                existing={"policy": "existing deny hooks"})
        _code, report = self.report(layers)
        self.assertEqual((report["replay"]["repeat_owned"], report["replay"]["repeat_unowned"]), (1, 1))

    def test_multi_claimed_is_reported(self):
        self.slip("gh-api|args")
        layers = self.layer_set([{"name": "a", "claims": ["*|args"], "module": None},
                                 {"name": "b", "claims": ["gh-api|args"], "module": None}])
        _code, report = self.report(layers)
        self.assertEqual(report["replay"]["multi_claimed"], 1)

    def test_malformed_layer_set_refuses(self):
        self.slip("gh-api|args")
        for claim in ("args", "*|no-such-class", "|args", "a|b|c"):
            with self.subTest(claim=claim):
                layers = self.layer_set([{"name": "x", "claims": [claim], "module": None}])
                self.assertEqual(self.run_score(layers)[0], 2)


# --------------------------------------------------------------------------
# AC2: the pass bar.

class Bars(Harness):
    def test_full_share_bar_holds(self):
        self.slip("gh-api|args")
        self.slip("git-push|args")
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None,
                                  "bar": {"replay": {"classes": ["args"], "min_share": 1.0}}}])
        code, report = self.report(layers)
        self.assertEqual(report["bars"], [{"bar": "gate.replay", "status": "holds", "claimed": 2, "total": 2}])
        self.assertEqual(code, 0)

    def test_layer_exception_fails_sample_bar(self):
        self.slip("gh-api|args")
        module = self.module("def refuses(command, cwd, env_names):\n    raise RuntimeError('boom')\n")
        layers = self.layer_set([{"name": "lint", "claims": ["*|shell"], "module": module,
                                  "bar": {"sample_max_refusals": 0}}])
        code, report = self.report(layers)
        self.assertEqual(report["layers"]["lint"]["errors"], 4)
        self.assertEqual(report["bars"][0]["status"], "fails")
        self.assertEqual(code, 4)

    def test_strict_fails_unmeasurable_bar(self):
        self.slip("gh-api|args")
        layers = self.layer_set([{"name": "lint", "claims": ["*|shell"], "module": None,
                                  "bar": {"sample_max_refusals": 0}}])
        self.assertEqual(self.report(layers)[0], 0)
        self.assertEqual(self.report(layers, "--strict")[0], 4)

    def test_replay_bar_with_nothing_to_score_fails(self):
        # No slips at all: the empty-overlays case. Never a pass.
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None,
                                  "bar": {"replay": {"classes": ["args"], "min_share": 1.0}}}])
        code, out, _err = self.run_score(layers)
        self.assertEqual(code, 4)
        self.assertIn("gate.replay 0/0 fails", out)
        self.assertNotIn("verdict: PASS", out)

    def test_unmeasured_bar_never_prints_bare_pass(self):
        self.slip("gh-api|args")
        layers = self.layer_set([{"name": "lint", "claims": ["*|shell"], "module": None,
                                  "bar": {"sample_max_refusals": 0}}])
        code, out, _err = self.run_score(layers)
        self.assertEqual(code, 0)
        self.assertIn("verdict: PASS with 1 bar(s) not measured:", out)

    def test_failed_bar_exits_4(self):
        self.slip("gh-api|args")
        self.slip("gh-api|cwd")
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None,
                                  "bar": {"replay": {"classes": ["args", "cwd"], "min_share": 1.0}}}])
        code, out, _err = self.run_score(layers)
        self.assertEqual(code, 4)
        self.assertIn("gate.replay 1/2 fails", out)

    def test_first_claiming_layer_owns(self):
        self.slip("gh-api|args")
        layers = self.layer_set([{"name": "first", "claims": ["*|args"], "module": None},
                                 {"name": "second", "claims": ["*|args"], "module": None}])
        _code, report = self.report(layers)
        self.assertEqual(report["replay"]["owners"], {"first": 1})

    def test_control_all_refuses_every_row(self):
        self.slip("gh-api|args")
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None}])
        _code, report = self.report(layers, "--control")
        self.assertEqual(report["controls"]["all"]["refusals"], 4)
        self.assertEqual(report["controls"]["null"]["refusals"], 0)
        self.assertEqual(report["sample_rows"], 4)

    def test_design_v1_loads(self):
        layer_set = rh.load_layer_set(DESIGN)
        self.assertEqual([layer["name"] for layer in layer_set["layers"]], ["lint", "gate", "provenance"])
        self.assertEqual(layer_set["accepted_uncovered"], ["exists", "local-check"])
        # Decision 3 is not met yet: the gate's wildcard claims are labeled declared.
        self.assertEqual([layer["claims_basis"] for layer in layer_set["layers"]],
                         ["validated", "declared", "validated"])

    def test_declared_claims_label_the_replay_bar(self):
        self.slip("gh-api|receipt")
        layers = self.layer_set([{"name": "gate", "claims": ["*|receipt"], "claims_basis": "declared",
                                  "module": None,
                                  "bar": {"replay": {"classes": ["receipt"], "min_share": 1.0}}}])
        code, out, _err = self.run_score(layers)
        self.assertEqual(code, 0)
        self.assertIn("gate.replay (declared claims) 1/1 holds", out)

    def test_s16_misspelled_bar_key_refuses(self):
        # Survivor 16: a misspelled bar key loaded with zero bars and scored PASS.
        self.slip("gh-api|args")
        for layer in ({"name": "gate", "claims": ["*|args"], "module": None,
                       "bars": {"replay": {"classes": ["args"], "min_share": 1.0}}},
                      {"name": "gate", "claims": ["*|args"], "module": None,
                       "bar": {"replay": {"classes": ["args"], "min_share": 1.0}, "sample_max_refusal": 0}},
                      {"name": "gate", "claims": ["*|args"], "module": None,
                       "bar": {"replay": {"classes": ["args"], "min_share": 1.0, "min_shares": 0}}}):
            with self.subTest(layer=layer):
                code, _out, err = self.run_score(self.layer_set([layer], raw=True), "--strict")
                self.assertEqual(code, 2, err)
                self.assertIn("unknown key", err)

    def test_layer_with_no_bar_refuses(self):
        self.slip("gh-api|args")
        for bar in ({}, None):
            layer = {"name": "gate", "claims": ["*|args"], "module": None}
            if bar is not None:
                layer["bar"] = bar
            with self.subTest(bar=bar):
                code, _out, err = self.run_score(self.layer_set([layer], raw=True), "--strict")
                self.assertEqual(code, 2, err)
                self.assertIn("needs a bar", err)

    def test_bar_with_only_null_values_refuses(self):
        self.slip("gh-api|args")
        layer = {"name": "gate", "claims": ["*|args"], "module": None,
                 "bar": {"replay": None, "sample_max_refusals": None}}
        code, _out, err = self.run_score(self.layer_set([layer], raw=True), "--strict")
        self.assertEqual(code, 2, err)
        self.assertIn("sets neither", err)

    def test_layer_set_with_no_layers_refuses(self):
        self.slip("gh-api|args")
        code, _out, err = self.run_score(self.layer_set([], raw=True), "--strict")
        self.assertEqual(code, 2, err)
        self.assertIn("no layers", err)

    def test_unknown_top_level_key_refuses(self):
        path = self.tmp / "layers-top.json"
        path.write_text(json.dumps({"version": 1, "layers": [], "acepted_uncovered": []}), encoding="utf-8")
        with self.assertRaises(rh.Refused) as caught:
            rh.load_layer_set(path)
        self.assertIn("unknown key", str(caught.exception))

    def test_unknown_claims_basis_refuses(self):
        layers = self.layer_set([{"name": "gate", "claims": ["*|receipt"], "claims_basis": "assumed",
                                  "module": None}])
        code, _out, err = self.run_score(layers)
        self.assertEqual(code, 2, err)


# --------------------------------------------------------------------------
# AC3 and the frozen sample.

class NoData(Harness):
    def test_report_never_prints_command_or_probe_text(self):
        self.slip("gh-api|args")
        module = self.module("def refuses(command, cwd, env_names):\n    return 'refused by fixture'\n")
        layers = self.layer_set([{"name": "lint", "claims": ["*|shell"], "module": module,
                                  "bar": {"sample_max_refusals": 0}}])
        for extra in ((), ("--json",)):
            with self.subTest(extra=extra):
                code, out, err = self.run_score(layers, "--control", *extra)
                self.assertEqual(code, 4)
                for mark in (COMMAND_MARK, PROBE_MARK):
                    self.assertNotIn(mark, out + err)

    def test_edited_sample_refuses(self):
        self.slip("gh-api|args")
        with self.sample.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"n": 5, "command": "ls"}) + "\n")
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None}])
        code, _out, err = self.run_score(layers)
        self.assertEqual(code, 2)
        self.assertIn("sha256", err)

    def test_s17_sample_not_exactly_the_pinned_size_refuses(self):
        # Survivor 17's score side: a sample of any other size refuses,
        # whether its manifest agrees with it or not.
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None}])
        for commands, size in ((["ls a", "ls b", "ls c"], None), (["ls a", "ls b", "ls c"], 4), ([], None)):
            with self.subTest(rows=len(commands), size=size):
                self.slip("gh-api|args")
                self.write_sample(commands, size)
                code, _out, err = self.run_score(layers)
                self.assertEqual(code, 2, err)
                self.assertIn("a sample is exactly 4 rows", err)

    def test_missing_sample_refuses(self):
        self.slip("gh-api|args")
        self.sample.unlink()
        self.sample.with_name(self.sample.name + ".manifest.json").unlink()
        layers = self.layer_set([{"name": "gate", "claims": ["*|args"], "module": None}])
        code, _out, err = self.run_score(layers)
        self.assertEqual(code, 2)
        self.assertIn("derive", err)


if __name__ == "__main__":
    unittest.main()
