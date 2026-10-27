"""harmonic-forge#1188: LLM usage capture -- schema (AC2), Codex harvester
(AC3), idempotent re-harvest (AC5), xAI adapter (AC6), sidecar join (AC7),
sidecar writers never fail their action (AC8), token-weighted unassigned share
(AC9), audit evidence (AC10), unvalidated attention (AC11), report (AC12).
The Claude Code harvester (AC4) waits on AC1 and has no tests yet."""
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
                "subject_id": "xai:r1", "attrs": {k: v for k, v in base.items() if v is not None}}

    def test_llm_usage_source_accepted(self):
        self.assertIn("llm-usage", emit.SCHEMA["columns"]["source"])
        self.assertIn("operator-attention", emit.SCHEMA["columns"]["source"])
        emit.validate(self._event())

    def test_llm_usage_missing_attr_refused(self):
        for key in emit.LLM_USAGE_REQUIRED:
            with self.subTest(key=key), self.assertRaises(emit.SchemaError):
                emit.validate(self._event(**{key: None}))
        self.assertEqual(len(emit.emit([self._event(output=None)])["rejected"]), 1)


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

    def test_claude_harvester_is_a_stub_until_ac1(self):
        with self.assertRaises(NotImplementedError):
            llm_usage.harvest_claude()


class Xai(Base):
    def test_xai_chat_shape(self):
        usage = llm_usage.normalize_xai(json.loads((FIX / "xai_chat.json").read_text()))
        self.assertEqual((usage["input_uncached"], usage["cached_read"], usage["output"],
                          usage["reasoning_out"]), (400, 800, 300, 120))

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
