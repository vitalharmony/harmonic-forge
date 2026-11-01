"""harmonic-forge#1188: LLM usage capture -- schema (AC2), Codex harvester
(AC3), idempotent re-harvest (AC5), xAI adapter (AC6), sidecar join (AC7),
sidecar writers never fail their action (AC8), token-weighted unassigned share
(AC9), audit evidence (AC10), unvalidated attention (AC11), report (AC12),
the Claude Code transcript harvester (AC4), and the 1188 amendments: `effort`
on every event, belt pickups as their own sidecar line, xAI cost_in_nano_usd,
and the stated Claude blind spot."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))
import attrib  # noqa: E402
import emit  # noqa: E402
import llm_usage  # noqa: E402
import llm_usage_report  # noqa: E402

FIX = HERE / "fixtures" / "llm_usage"
ROLLOUT = "rollout-2026-10-26T10-00-00-{}.jsonl"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {emit.STORE_ENV: str(self.tmp / "store"),
               attrib.ATTRIB_DIR_ENV: str(self.tmp / "attrib"),
               llm_usage.STATE_ENV: str(self.tmp / "state" / "wm.json"),
               llm_usage.ATTENTION_ENV: str(self.tmp / "attention")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.codex = self.tmp / "codex"
        self.codex.mkdir()

    def add_rollout(self, name: str) -> Path:
        dest = self.codex / ROLLOUT.format(name)
        shutil.copy(FIX / ROLLOUT.format(name), dest)
        return dest

    def usage(self) -> list[dict]:
        return llm_usage_report.load("llm-usage", self.tmp / "store")


class Schema(Base):
    def _event(self, **attrs):
        base = {"provider": "xai", "model": "m", "input_uncached": 1, "cached_read": 0, "output": 1}
        base.update(attrs)
        return {"ts": "2026-10-26T10:00:00Z", "source": "llm-usage", "event_type": "llm-usage.call",
                "subject_id": "xai:r1",
                "attrs": {**{k: v for k, v in base.items() if v is not None}, "effort": None}}

    def test_llm_usage_source_accepted(self):
        self.assertIn("llm-usage", emit.SCHEMA["columns"]["source"])
        self.assertIn("operator-attention", emit.SCHEMA["columns"]["source"])
        emit.validate(self._event())

    def test_llm_usage_missing_attr_refused(self):
        for key in emit.LLM_USAGE_REQUIRED:
            with self.subTest(key=key), self.assertRaises(emit.SchemaError):
                emit.validate(self._event(**{key: None}))
        self.assertEqual(len(emit.emit([self._event(output=None)])["rejected"]), 1)

    def test_effort_key_required_null_allowed(self):
        event = self._event()
        emit.validate(event)  # effort: None is accepted
        del event["attrs"]["effort"]
        with self.assertRaises(emit.SchemaError):
            emit.validate(event)


class Codex(Base):
    def test_codex_one_event_per_token_count(self):
        self.add_rollout("two-events")
        llm_usage.harvest_codex(self.codex)
        rows = sorted(self.usage(), key=lambda e: e["ts"])
        self.assertEqual(len(rows), 2)
        second = rows[1]["attrs"]  # last_token_usage, never the cumulative total
        self.assertEqual((second["input_uncached"], second["cached_read"], second["cache_write"],
                          second["output"], second["reasoning_out"]), (24182 - 12832, 12832, 5, 122, 10))
        self.assertEqual(second["plan_used_pct_5h"], 29.0)
        self.assertEqual(second["plan_used_pct_7d"], 14.0)
        self.assertEqual(second["model"], "gpt-6-astra")

    def test_codex_repeated_identical_event_one_row(self):
        self.add_rollout("repeated")
        llm_usage.harvest_codex(self.codex)
        self.assertEqual(len(self.usage()), 1)

    def test_codex_null_info_skipped(self):
        self.add_rollout("null-info")
        result = llm_usage.harvest_codex(self.codex)
        self.assertEqual(len(self.usage()), 1)
        self.assertEqual(result["unknown_lines"], 1)  # the malformed line, counted

    def test_codex_input_uncached_mapping(self):
        self.add_rollout("repeated")
        llm_usage.harvest_codex(self.codex)
        attrs = self.usage()[0]["attrs"]
        self.assertEqual(attrs["input_uncached"], 20607 - 7168)
        self.assertEqual(attrs["cached_read"], 7168)
        self.assertEqual(attrs["output"], 78)

    def test_reharvest_adds_zero(self):
        self.add_rollout("two-events")
        self.assertEqual(llm_usage.harvest_codex(self.codex)["written"], 2)
        self.assertEqual(llm_usage.harvest_codex(self.codex)["written"], 0)  # watermark
        os.remove(os.environ[llm_usage.STATE_ENV])  # lost watermark: the event_id dedupe holds
        again = llm_usage.harvest_codex(self.codex)
        self.assertEqual((again["written"], again["duplicate"]), (0, 2))
        self.assertEqual(len(self.usage()), 2)

    def test_reharvest_resumes_after_appended_lines(self):
        path = self.add_rollout("two-events")
        lines = path.read_text().splitlines(keepends=True)
        path.write_text("".join(lines[:3]))
        self.assertEqual(llm_usage.harvest_codex(self.codex)["written"], 1)
        with path.open("a") as handle:
            handle.write(lines[3])
        self.assertEqual(llm_usage.harvest_codex(self.codex)["written"], 1)
        self.assertEqual(len(self.usage()), 2)

    def test_codex_effort_from_turn_context(self):
        path = self.add_rollout("two-events")
        lines = path.read_text().splitlines(keepends=True)
        ctx = '{"type":"turn_context","timestamp":"2026-10-26T10:00:00Z","payload":{"effort":"medium"}}\n'
        path.write_text(lines[0] + ctx + "".join(lines[1:]))
        llm_usage.harvest_codex(self.codex)
        self.assertEqual({e["attrs"]["effort"] for e in self.usage()}, {"medium"})


class Claude(Base):
    """AC4: one event per request, the highest-output line, never summed."""

    def setUp(self):
        super().setUp()
        self.claude = self.tmp / "claude" / "proj"
        self.claude.mkdir(parents=True)
        self.path = self.claude / "s.jsonl"
        shutil.copy(FIX / "claude-session.jsonl", self.path)

    def by_req(self):
        return {e["attrs"]["request_id"]: e for e in self.usage()}

    def test_one_event_per_request_highest_output(self):
        llm_usage.harvest_claude(self.tmp / "claude", settle_s=0)
        rows = self.by_req()
        self.assertEqual(set(rows), {"req_A", "msg_B", "req_C"})  # msg_B: no requestId
        self.assertEqual(rows["req_A"]["attrs"]["output"], 40)  # the max, not 5+40+22
        self.assertEqual(rows["req_C"]["attrs"]["output"], 30)

    def test_mapping_no_codex_subtraction(self):
        llm_usage.harvest_claude(self.tmp / "claude", settle_s=0)
        a = self.by_req()["req_A"]["attrs"]
        self.assertEqual((a["input_uncached"], a["cached_read"], a["cache_write"],
                          a["cache_write_1h"], a["reasoning_out"]), (12, 9000, 300, 200, 7))
        self.assertEqual(a["provider"], "anthropic-claude-code")

    def test_effort_carried_and_null_kept(self):
        llm_usage.harvest_claude(self.tmp / "claude", settle_s=0)
        rows = self.by_req()
        self.assertEqual(rows["req_A"]["attrs"]["effort"], "high")
        self.assertIn("effort", rows["req_C"]["attrs"])
        self.assertIsNone(rows["req_C"]["attrs"]["effort"])

    def test_one_hour_cache_priced_at_its_own_rate(self):
        five_only = llm_usage.est_cost("claude-opus-5-5", 0, 0, 300, 0)
        split = llm_usage.est_cost("claude-opus-5-5", 0, 0, 300, 0, cache_write_1h=200)
        self.assertAlmostEqual(five_only, 300 * 5.0 / 1e6)
        self.assertAlmostEqual(split, (100 * 5.0 + 200 * 4.0 * 2.0) / 1e6)

    def test_reharvest_adds_zero(self):
        root = self.tmp / "claude"
        self.assertEqual(llm_usage.harvest_claude(root, settle_s=0)["written"], 3)
        self.assertEqual(llm_usage.harvest_claude(root, settle_s=0)["written"], 0)
        os.remove(os.environ[llm_usage.STATE_ENV])
        again = llm_usage.harvest_claude(root, settle_s=0)
        self.assertEqual((again["written"], again["duplicate"]), (0, 3))

    def test_straddling_request_emitted_once_complete(self):
        """AC5: a request whose lines straddle a run's stop point."""
        root = self.tmp / "claude"
        lines = self.path.read_text().splitlines(keepends=True)
        self.path.write_text("".join(lines[:3]))  # req_A's first two lines only
        first = llm_usage.harvest_claude(root, settle_s=3600)  # live file: newest group held
        self.assertEqual((first["written"], first["held"]), (0, 1))
        with self.path.open("a") as handle:
            handle.write("".join(lines[3:]))
        llm_usage.harvest_claude(root, settle_s=3600)  # req_A complete; req_C now held
        llm_usage.harvest_claude(root, settle_s=0)  # idle: req_C emitted
        rows = self.by_req()
        self.assertEqual(rows["req_A"]["attrs"]["output"], 40)
        self.assertEqual(len(self.usage()), 3)

    def test_unicode_line_separator_inside_a_record_is_kept(self):
        """Preclose pass 1: U+2028 inside a JSONL record never splits it."""
        lines = self.path.read_text().splitlines(keepends=True)
        rec = json.loads(lines[1])
        rec["message"]["content"] = [{"type": "text", "text": "a\u2028b\u0085c"}]
        lines[1] = json.dumps(rec, ensure_ascii=False) + "\n"
        self.path.write_text("".join(lines))
        result = llm_usage.harvest_claude(self.tmp / "claude", settle_s=0)
        self.assertEqual((result["written"], result["unknown_lines"]), (3, 0))

    def test_holdback_offset_survives_invalid_utf8_and_crlf(self):
        """Preclose pass 1: the held group's offset comes from raw bytes."""
        lines = self.path.read_bytes().split(b"\n")
        noisy = [lines[0] + b"\xff\xfe", b"", lines[1] + b"\r"] + lines[2:]
        self.path.write_bytes(b"\n".join(noisy))
        root = self.tmp / "claude"
        llm_usage.harvest_claude(root, settle_s=3600)  # req_C held
        llm_usage.harvest_claude(root, settle_s=0)
        self.assertEqual(self.by_req()["req_C"]["attrs"]["output"], 30)
        self.assertEqual(len(self.usage()), 3)

    def summary_req_a(self):
        s = llm_usage_report.summarize(self.usage(), [])
        return s

    def test_late_lines_counted_once_at_fullest(self):
        """Operator ruling B (reforge): a request read before and after its later
        lines arrive is stored twice and counted once, at its highest output."""
        root = self.tmp / "claude"
        lines = self.path.read_text().splitlines(keepends=True)
        self.path.write_text("".join(lines[:2]))  # req_A at output 5
        llm_usage.harvest_claude(root, settle_s=0)
        with self.path.open("a") as handle:
            handle.write("".join(lines[2:]))
        llm_usage.harvest_claude(root, settle_s=0)
        reads = [e for e in self.usage() if e["attrs"]["request_id"] == "req_A"]
        self.assertEqual(sorted(e["attrs"]["output"] for e in reads), [5, 40])
        kept = [e for e in llm_usage_report.latest_reads(self.usage())
                if e["attrs"]["request_id"] == "req_A"]
        self.assertEqual([e["attrs"]["output"] for e in kept], [40])
        self.assertEqual(sum(r["output"] for r in self.summary_req_a()["lane"].values()), 40 + 9 + 30)

    def test_identical_reread_still_dedupes_in_the_store(self):
        root = self.tmp / "claude"
        llm_usage.harvest_claude(root, settle_s=0)
        os.remove(os.environ[llm_usage.STATE_ENV])
        again = llm_usage.harvest_claude(root, settle_s=0)
        self.assertEqual((again["written"], again["duplicate"]), (0, 3))

    def test_resolved_fuller_read_wins_over_unresolved_partial(self):
        """The case ruling A's move could not do atomically: the store keeps
        both rows untouched, and the report counts the fuller, resolved one."""
        root = self.tmp / "claude"
        lines = self.path.read_text().splitlines(keepends=True)
        self.path.write_text("".join(lines[:2]))
        llm_usage.harvest_claude(root, settle_s=0)
        attrib.append("cccc2222-0000-0000-0000-000000000001", "vitalharmony/harmonic-forge#77", "2",
                      None, "shorthand:Fix", ts="2026-10-31T09:00:00Z")
        with self.path.open("a") as handle:
            handle.write("".join(lines[2:]))
        llm_usage.harvest_claude(root, settle_s=0)
        kept = [e for e in llm_usage_report.latest_reads(self.usage())
                if e["attrs"]["request_id"] == "req_A"]
        self.assertEqual([(e["issue"], e["attrs"]["output"]) for e in kept], [(77, 40)])

    def test_emit_failure_is_contained_and_reread(self):
        """Preclose pass 2: an OSError from emit never aborts the harvest, and
        the file is re-read on the next run."""
        root = self.tmp / "claude"
        with mock.patch.object(llm_usage.emit, "emit", side_effect=OSError("disk")):
            result = llm_usage.harvest_claude(root, settle_s=0)
        self.assertEqual(result["emit_failed"], 1)
        self.assertEqual(self.usage(), [])
        self.assertEqual(llm_usage.harvest_claude(root, settle_s=0)["written"], 3)

    def test_fuller_reread_same_ts_is_stored(self):
        """Reforge pass 1: a fuller read with the same first-line ts is a
        distinct read, stored, and the report keeps it."""
        root = self.tmp / "claude"
        lines = self.path.read_text().splitlines(keepends=True)
        self.path.write_text("".join(lines[:2]))  # req_A at 5
        llm_usage.harvest_claude(root, settle_s=0)
        os.remove(os.environ[llm_usage.STATE_ENV])  # re-read from offset 0
        self.path.write_text("".join(lines))  # req_A complete, same first line
        llm_usage.harvest_claude(root, settle_s=0)
        reads = sorted(e["attrs"]["output"] for e in self.usage() if e["attrs"]["request_id"] == "req_A")
        self.assertEqual(reads, [5, 40])
        kept = [e for e in llm_usage_report.latest_reads(self.usage())
                if e["attrs"]["request_id"] == "req_A"]
        self.assertEqual([e["attrs"]["output"] for e in kept], [40])

    def test_tie_keeps_the_attributed_read_in_either_order(self):
        def read(issue):
            return {"ts": "2026-10-31T10:00:01Z", "issue": issue,
                    "attrs": {"provider": "anthropic-claude-code", "request_id": "r", "output": 7}}
        for order in ((read(None), read(1188)), (read(1188), read(None))):
            with self.subTest(first=order[0]["issue"]):
                (kept,) = llm_usage_report.latest_reads(list(order))
                self.assertEqual(kept["issue"], 1188)

    def test_reread_differing_only_in_input_is_stored(self):
        """Reforge pass 2: any usage field makes a read distinct."""
        root = self.tmp / "claude"
        llm_usage.harvest_claude(root, settle_s=0)
        os.remove(os.environ[llm_usage.STATE_ENV])
        text = self.path.read_text().replace('"input_tokens": 12', '"input_tokens": 120')
        self.path.write_text(text)
        again = llm_usage.harvest_claude(root, settle_s=0)
        self.assertEqual(again["written"], 3)

    def test_joins_sidecar_by_session(self):
        attrib.append("cccc2222-0000-0000-0000-000000000001", "vitalharmony/harmonic-forge#77", "2",
                      None, "shorthand:Fix", ts="2026-10-31T09:00:00Z")
        llm_usage.harvest_claude(self.tmp / "claude", settle_s=0)
        self.assertEqual({(e["issue"], e["attrs"]["attribution"]) for e in self.usage()},
                         {(77, "explicit")})


class HarvestContainment(Base):
    """Reforge pass 1: a store failure in one harvester never stops the others
    and never advances that harvester's marks."""

    def test_codex_emit_failure_is_contained(self):
        self.add_rollout("two-events")
        with mock.patch.object(llm_usage.emit, "emit", side_effect=OSError("disk")):
            result = llm_usage.harvest_codex(self.codex)
        self.assertEqual(result["emit_failed"], 1)
        self.assertEqual(llm_usage.harvest_codex(self.codex)["written"], 2)

    def test_attention_emit_failure_keeps_marks(self):
        att = self.tmp / "attention"
        att.mkdir()
        (att / "s1.jsonl").write_text(json.dumps({"ts": "2026-10-31T10:00:00Z", "session_id": "s1",
                                                  "gap_s": 30, "lead_bytes": 100}) + "\n")
        with mock.patch.object(llm_usage.emit, "emit", side_effect=OSError("disk")):
            self.assertEqual(llm_usage.harvest_attention(att)["emit_failed"], 1)
        self.assertEqual(llm_usage.harvest_attention(att)["written"], 1)

    def test_main_runs_every_harvester_after_one_fails(self):
        calls = []
        with mock.patch.object(llm_usage, "harvest_codex", side_effect=OSError("disk")), \
                mock.patch.object(llm_usage, "harvest_claude", side_effect=lambda *a, **k: calls.append("claude") or {}), \
                mock.patch.object(llm_usage, "harvest_attention", side_effect=lambda *a, **k: calls.append("attention") or {}), \
                redirect_stdout(io.StringIO()) as out:
            rc = llm_usage.main(["harvest", "--codex-root", str(self.codex),
                                 "--claude-root", str(self.codex)])
        self.assertEqual((rc, calls), (0, ["claude", "attention"]))
        self.assertIn("OSError", out.getvalue())


class TeRunAttribution(unittest.TestCase):
    def test_sidecar_is_written_before_the_agent_runs(self):
        """Reforge pass 1: a harvest during a te-run already finds its line."""
        src = (HERE.parent / "runner" / "te_run.py").read_text()
        body = src[src.index("    session_id = str(uuid.uuid4())"):]
        body = body[:body.index('    result = parsed["result"]')]
        self.assertLess(body.index("record_attrib(session_id"), body.index("run_agent("))
        self.assertEqual(body.count("record_attrib("), 1)


class AttentionIdentity(Base):
    """Reforge pass 2: an attention prompt is counted once."""

    def test_compare_counts_a_reharvested_prompt_once(self):
        timings = self.tmp / "t.csv"
        timings.write_text("date,minutes\n2026-10-31,3\n")
        one = {"ts": "2026-10-31T10:00:00Z", "subject_id": "s1:2026-10-31T10:00:00.1Z", "issue": None,
               "attrs": {"minutes": 3.0}}
        two = dict(one, issue=1188)
        (row,) = llm_usage_report.attention_compare([one, two], timings)
        self.assertEqual(row["formula_min"], 3.0)

    def test_hook_records_subsecond_prompt_times(self):
        import expand_lane_shorthand as hook
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            hook.record_attention("hello", {"session_id": "sub-s", "transcript_path": ""})
            hook.record_attention("again", {"session_id": "sub-s", "transcript_path": ""})
        lines = [json.loads(l) for l in (self.tmp / "attention" / "sub-s.jsonl").read_text().splitlines()]
        self.assertEqual(len({l["ts"] for l in lines}), 2)
        self.assertRegex(lines[0]["ts"], r"\.\d{6}Z$")


class Xai(Base):
    def test_xai_chat_shape(self):
        usage = llm_usage.normalize_xai(json.loads((FIX / "xai_chat.json").read_text()))
        self.assertEqual((usage["input_uncached"], usage["cached_read"], usage["output"],
                          usage["reasoning_out"]), (400, 800, 300, 120))

    def test_xai_keeps_cost_in_nano_usd_and_caller_effort(self):
        usage = llm_usage.normalize_xai(json.loads((FIX / "xai_responses.json").read_text()),
                                        effort="low")
        event = llm_usage.to_event(usage, "2026-10-26T10:00:00Z", "xai:resp-xai-1")
        self.assertEqual(event["attrs"]["cost_in_nano_usd"], 1234567)
        self.assertEqual(event["attrs"]["effort"], "low")
        chat = llm_usage.normalize_xai(json.loads((FIX / "xai_chat.json").read_text()))
        self.assertIsNone(chat["cost_in_nano_usd"])  # absent stays absent

    def test_xai_responses_shape(self):
        usage = llm_usage.normalize_xai(json.loads((FIX / "xai_responses.json").read_text()))
        self.assertEqual((usage["input_uncached"], usage["cached_read"], usage["output"],
                          usage["reasoning_out"]), (500, 1500, 400, 90))
        event = llm_usage.to_event(usage, "2026-10-26T10:00:00Z", "xai:resp-xai-1")
        emit.validate(event)


class Join(Base):
    SID = "aaaa1111-0000-0000-0000-000000000001"

    def test_explicit_when_sidecar_at_or_before(self):
        attrib.append(self.SID, "vitalharmony/harmonic-forge#123", "1", "run-1", "cross-family",
                      ts="2026-10-26T10:00:00Z")
        attrib.append(self.SID, "vitalharmony/harmonic-forge#999", "1", None, "later",
                      ts="2026-10-26T11:00:00Z")
        self.add_rollout("two-events")
        llm_usage.harvest_codex(self.codex)
        for e in self.usage():
            self.assertEqual((e["repo"], e["issue"]), ("vitalharmony/harmonic-forge", 123))
            self.assertEqual(e["attrs"]["attribution"], "explicit")
            self.assertEqual(e["attrs"]["run_id"], "run-1")

    def test_unassigned_when_no_sidecar(self):
        # Another session's line must never be inherited.
        attrib.append("other-session", "vitalharmony/harmonic-forge#123", "1", None, "x",
                      ts="2026-10-26T09:00:00Z")
        # This session's only line is after the call: not "at or before".
        attrib.append("aaaa1111-0000-0000-0000-000000000002", "vitalharmony/harmonic-forge#5", "1",
                      None, "x", ts="2026-10-26T12:00:00Z")
        self.add_rollout("repeated")
        llm_usage.harvest_codex(self.codex)
        (event,) = self.usage()
        self.assertIsNone(event["issue"])
        self.assertEqual(event["attrs"]["attribution"], "unassigned")


class Writers(Base):
    def test_attrib_append_never_raises(self):
        blocker = self.tmp / "file"
        blocker.write_text("")
        with mock.patch.dict(os.environ, {attrib.ATTRIB_DIR_ENV: str(blocker)}):
            self.assertFalse(attrib.append("s1", "vitalharmony/harmonic-forge#1", "1", None, "t"))
        with mock.patch.object(attrib.json, "dumps", side_effect=RuntimeError("boom")):
            self.assertFalse(attrib.append("s1", "vitalharmony/harmonic-forge#1", "1", None, "t"))
        self.assertFalse(attrib.append("../escape", "vitalharmony/harmonic-forge#1", "1", None, "t"))

    def test_attrib_resolves_prefix_key(self):
        self.assertTrue(attrib.append("s1", "F123", "1", None, "t"))
        self.assertEqual(attrib.read("s1")[0]["issue"], "vitalharmony/harmonic-forge#123")

    def _run_hook(self, prompt: str, entrypoint: str = "cli") -> str:
        import expand_lane_shorthand as hook
        stdin = io.StringIO(json.dumps({"prompt": prompt, "session_id": "hook-session",
                                        "transcript_path": str(self.tmp / "t.jsonl")}))
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), redirect_stdout(out), \
                mock.patch.object(hook, "fetch_issue_context", return_value="ctx"), \
                mock.patch.object(hook, "authorize_batch", return_value=""), \
                mock.patch.object(hook, "authorize_imc", return_value=""), \
                mock.patch.object(hook, "authorize_force", return_value=""), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": entrypoint}):
            hook.main()
        return out.getvalue()

    def test_hook_writes_belt_line(self):
        """1188 amendment 1: belt-driven work is booked to the belt event's
        issue, not the previous trigger's."""
        self._run_hook("Implement F123")
        self._run_hook("<task-notification>\n<event>vitalharmony/hrse#18 queued-for-l1 "
                       "kind=spec owes=spec-review</event>\n</task-notification>")
        lines = attrib.read("hook-session")
        self.assertEqual([(l["issue"], l["trigger"]) for l in lines],
                         [("vitalharmony/harmonic-forge#123", "shorthand:Implement"),
                          ("vitalharmony/hrse#18", "belt:queued")])
        self.assertEqual(lines[-1]["lane"], "1")

    def test_hook_attributes_only_a_first_line_trigger(self):
        """Preclose pass 1: quoted, pasted or fenced trigger text never attributes."""
        for prompt in ("Please explain this example; do not implement it:\n```\nImplement F999\n```",
                       "Here is the issue body:\nFix H18 is what it says",
                       "<task-notification>Fix H18 in a Monitor line</task-notification>"):
            with self.subTest(prompt=prompt[:30]):
                self._run_hook(prompt)
        self.assertEqual(attrib.read("hook-session"), [])
        self._run_hook("\n  Implement F123\nmore text")
        self.assertEqual([l["issue"] for l in attrib.read("hook-session")],
                         ["vitalharmony/harmonic-forge#123"])

    def test_headless_prompt_never_attributes(self):
        """Preclose pass 2: an sdk-* session has no operator at its prompt."""
        self._run_hook("Implement F123", entrypoint="sdk-cli")
        self.assertEqual(attrib.read("hook-session"), [])

    def test_queued_line_after_the_notification_closes_is_ignored(self):
        self._run_hook("<task-notification>nothing</task-notification>\n"
                       "vitalharmony/hrse#999 queued-for-l1 kind=spec")
        self.assertEqual(attrib.read("hook-session"), [])

    def test_trigger_beside_a_notification_is_ignored(self):
        self._run_hook("Implement F123\n<task-notification>x</task-notification>")
        self.assertEqual(attrib.read("hook-session"), [])

    def test_belt_line_outside_a_notification_is_ignored(self):
        self._run_hook("pasted: vitalharmony/hrse#18 queued-for-l1 kind=spec")
        self.assertEqual(attrib.read("hook-session"), [])

    def test_hook_ignores_belt_text_that_is_not_a_queued_line(self):
        self._run_hook("<task-notification>vitalharmony/hrse#18 l3 — gate-result</task-notification>")
        self.assertEqual(attrib.read("hook-session"), [])

    def test_hook_writes_trigger_line(self):
        self._run_hook("Implement F123")
        (line,) = attrib.read("hook-session")
        self.assertEqual(line["issue"], "vitalharmony/harmonic-forge#123")
        self.assertEqual(line["trigger"], "shorthand:Implement")

    def test_hook_ignores_issue_in_prose(self):
        self._run_hook("please look at F123 when you can")
        self.assertEqual(attrib.read("hook-session"), [])

    def test_hook_survives_attrib_failure(self):
        blocker = self.tmp / "file"
        blocker.write_text("")
        with mock.patch.dict(os.environ, {attrib.ATTRIB_DIR_ENV: str(blocker)}), \
                mock.patch.object(attrib.json, "dumps", side_effect=RuntimeError("boom")):
            self.assertFalse(attrib.append("hook-session", "F1", "1", None, "t"))
        with mock.patch.dict(os.environ, {attrib.ATTRIB_DIR_ENV: str(blocker)}):
            output = self._run_hook("Implement F123")
        self.assertIn("hookSpecificOutput", output)
        # The guard itself (preclose pass 1): the hook's own call raises.
        import expand_lane_shorthand as hook
        for name in ("record_attribution", "record_attention"):
            with self.subTest(raising=name), \
                    mock.patch.object(hook, name, side_effect=RuntimeError("boom")):
                self.assertIn("hookSpecificOutput", self._run_hook("Implement F123"))

    def test_hook_records_attention(self):
        self._run_hook("Implement F123")
        lines = (self.tmp / "attention" / "hook-session.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self._run_hook("<task-notification>belt</task-notification>")
        lines = (self.tmp / "attention" / "hook-session.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)  # a belt wakeup is not attention
        self._run_hook("This session is being continued from a previous conversation")
        lines = (self.tmp / "attention" / "hook-session.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)  # the post-compaction prompt is not attention

    def test_headless_session_records_no_attention(self):
        self._run_hook("Implement F123", entrypoint="sdk-cli")
        self.assertFalse((self.tmp / "attention" / "hook-session.jsonl").exists())

    def test_hook_line_carries_transcript_evidence(self):
        attrib_line_count = len(attrib.read("hook-session"))
        self._run_hook("Implement F123")
        line = attrib.read("hook-session")[attrib_line_count]
        self.assertIn("transcript_path", line)

    def test_posters_record_after_success_and_survive(self):
        sys.path.insert(0, str(HERE.parent / "gh"))
        import l2_post
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "poster-s"}):
            l2_post._record_attrib("vitalharmony/harmonic-forge", 7, "completion", "2",
                                   "https://github.com/vitalharmony/harmonic-forge/issues/7#c1")
            with mock.patch.object(attrib, "append", side_effect=RuntimeError("boom")):
                l2_post._record_attrib("vitalharmony/harmonic-forge", 7, "completion", "2")
        (line,) = attrib.read("poster-s")
        self.assertEqual((line["issue"], line["lane"], line["trigger"]),
                         ("vitalharmony/harmonic-forge#7", "2", "post:completion"))
        self.assertTrue(line["comment_url"].endswith("#c1"))  # AC10 evidence outside the sidecar

    def test_attrib_cli_inherits_parent_issue(self):
        attrib.append("parent-s", "vitalharmony/harmonic-forge#42", "1", None, "t",
                      ts="2026-10-26T09:00:00Z")
        attrib.main(["append", "--session-id", "thread-1", "--thread-id", "thread-1",
                     "--parent-session-id", "parent-s", "--ts", "2026-10-26T10:00:00Z",
                     "--trigger", "cross-family:verify"])
        (line,) = attrib.read("thread-1")
        self.assertEqual(line["issue"], "vitalharmony/harmonic-forge#42")


class BlindSpot(Base):
    def test_report_states_the_claude_blind_spot(self):
        text = llm_usage_report.render(llm_usage_report.summarize([], []))
        self.assertIn(llm_usage_report.CLAUDE_BLIND_SPOT, text)


class Report(Base):
    def _ev(self, issue, lane, uncached, cached, output, cost=0.5, attribution="explicit"):
        return {"issue": issue, "repo": "vitalharmony/harmonic-forge" if issue else None,
                "attrs": {"lane": lane, "input_uncached": uncached, "cached_read": cached,
                          "cache_write": 0, "output": output, "est_cost_usd": cost,
                          "attribution": attribution, "session_id": "s1"}}

    def test_report_separates_cached(self):
        summary = llm_usage_report.summarize([self._ev(1, "1", 100, 900, 10),
                                              self._ev(1, "1", 50, 50, 5, cost=-1)], [])
        row = summary["issue"]["vitalharmony/harmonic-forge#1"]
        self.assertEqual((row["input_uncached"], row["cached_read"], row["output"]), (150, 950, 15))
        self.assertEqual((row["est_cost_usd"], row["unpriced_calls"]), (0.5, 1))
        self.assertIn("cached_read", llm_usage_report.render(summary).splitlines()[1])

    def test_unassigned_is_its_own_row(self):
        summary = llm_usage_report.summarize([self._ev(None, None, 10, 0, 1, attribution="unassigned")], [])
        self.assertIn("unassigned", summary["issue"])
        self.assertIn("unassigned", summary["lane"])

    def test_unassigned_share_is_token_weighted(self):
        # 1 unassigned call of 900 tokens, 3 assigned calls of 100 tokens each.
        events = [self._ev(None, None, 900, 0, 0, attribution="unassigned")]
        events += [self._ev(1, "1", 100, 0, 0) for _ in range(3)]
        self.assertEqual(llm_usage_report.summarize(events, [])["unassigned_share"], 0.75)

    def test_audit_sample_includes_evidence(self):
        attrib.append("s1", "vitalharmony/harmonic-forge#1", "1", None, "shorthand:Implement",
                      ts="2026-10-26T09:00:00Z")
        ev = self._ev(1, "1", 1, 0, 1)
        ev["attrs"].update(attrib_trigger="shorthand:Implement", attrib_ts="2026-10-26T09:00:00Z")
        rows = llm_usage_report.audit_sample([ev, self._ev(None, None, 1, 0, 1, attribution="unassigned")],
                                             50, seed=1)
        self.assertEqual(len(rows), 1)
        (row,) = rows
        self.assertEqual(row["joined_issue"], "vitalharmony/harmonic-forge#1")
        self.assertEqual(row["evidence_trigger"], "shorthand:Implement")
        self.assertEqual(row["session_sidecar"][0]["trigger"], "shorthand:Implement")

    def test_attention_unvalidated_excluded_from_report(self):
        attention = llm_usage.attention_minutes(120, 4096)
        self.assertEqual(attention, 3.0)  # 2 min + 0.5 per 2 KB, capped
        (self.tmp / "attention").mkdir()
        (self.tmp / "attention" / "s9.jsonl").write_text(json.dumps(
            {"ts": "2026-10-26T10:00:00Z", "session_id": "s9", "gap_s": 120, "lead_bytes": 4096}) + "\n")
        llm_usage.harvest_attention(self.tmp / "attention")
        (event,) = llm_usage_report.load("operator-attention", self.tmp / "store")
        self.assertIs(event["validated"], False)
        summary = llm_usage_report.summarize([], [event])
        self.assertTrue(all(r["attention_min"] == 0 for r in summary["issue"].values()))

    def test_attention_compare(self):
        timings = self.tmp / "t.csv"
        timings.write_text("date,minutes\n2026-10-26,6\n")
        event = {"ts": "2026-10-26T10:00:00Z", "validated": False, "attrs": {"minutes": 3.0}}
        (row,) = llm_usage_report.attention_compare([event], timings)
        self.assertEqual((row["formula_min"], row["ratio"]), (3.0, 0.5))


if __name__ == "__main__":
    unittest.main()
