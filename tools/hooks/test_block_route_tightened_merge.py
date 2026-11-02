#!/usr/bin/env python3
"""Tests for block_route_tightened_merge.py (harmonic-forge#1243, semantic routing A4a).

GitHub is a fake `_gh` (no network); the route store is a temp directory
(`HARMONIC_FORGE_TELEMETRY_STORE`), never the live one. One class per
acceptance criterion; the kill-check targets are named in each docstring.
Run: `python3 -m unittest tools.hooks.test_block_route_tightened_merge`."""
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import block_route_tightened_merge as hook  # noqa: E402
import block_missing_preclose_inspection as pre  # noqa: E402
import gates  # noqa: E402
import route_store  # noqa: E402

HRSE = "vitalharmony/hrse"
FORGE = "vitalharmony/harmonic-forge"
HEAD = "c" * 40
OTHER = "d" * 40


def gate_body(verdict: str, sha: str) -> str:
    """A Lane 3 gate report as l1-post writes it (test_carry_forward's fixture)."""
    prefix = (f"## Lane 3 Gate Results — {verdict}\n\n**Verdict:** {verdict}\n"
              f"Head-SHA: `{sha}`\nCI: green\n\nTCs ran.")
    digest = hashlib.sha256(prefix.encode()).hexdigest()
    return (f"{prefix}\n\n<!-- l1-post v1; kind=gate-result; posted-by=LANE3; sha={sha}; "
            f"body-sha256={digest}; -->\n")


def comment(cid: int, body: str) -> dict:
    return {"id": cid, "body": body, "created_at": "2026-11-01T00:00:00Z",
            "updated_at": "2026-11-01T00:00:00Z"}


class FakeGitHub:
    """Answers the hook's reads for one PR; `down` names reads that fail."""

    def __init__(self, repo, pr="9", *, title="Change (no suffix)", ref="feat/2600-x",
                 files=("backend/app/x.py",), labels=("tooling-exception",), head=HEAD,
                 comments=(), changed=None, delay=0.0):
        self.repo, self.pr, self.title, self.ref = repo, pr, title, ref
        self.files, self.labels, self.head = list(files), list(labels), head
        self.comments, self.changed = list(comments), changed
        self.down: set[str] = set()
        self.delay = delay
        self.calls: list[tuple] = []

    def __call__(self, *args, cwd=None, repo=None):
        self.calls.append(args)
        time.sleep(self.delay)
        joined = " ".join(args)
        if args[:2] == ("pr", "view"):
            what = "ref" if "headRefName" in args else "head"
            return None if what in self.down else (self.ref if what == "ref" else self.head)
        if "/pulls/" in joined and joined.endswith("{filename, previous_filename}"):
            return None if "files" in self.down else "\n".join(
                json.dumps({"filename": f, "previous_filename": None}) for f in self.files)
        if "/pulls/" in joined:
            changed = len(self.files) if self.changed is None else self.changed
            return None if "pr" in self.down else json.dumps(
                {"title": self.title, "changed_files": changed})
        if joined.endswith("/comments --jq .[]"):
            return None if "comments" in self.down else "\n".join(
                json.dumps(c) for c in self.comments)
        if ".labels[].name" in args:
            return None if "labels" in self.down else "\n".join(self.labels)
        raise AssertionError(f"unexpected gh call {args}")


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.store = Path(tmp.name)

    def seed(self, key, path, gate_id, checkpoint, decider="evaluator"):
        route_store.record(key, gates.Route(path, gate_id, "seed"), decider=decider,
                           checkpoint=checkpoint)

    def run_hook(self, gh, command=None, receipt_ok=False):
        command = command or f"gh pr merge {gh.pr} --repo {gh.repo} --squash --match-head-commit {HEAD}"
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        out = io.StringIO()
        with mock.patch.object(pre, "_gh", gh), \
                mock.patch.object(pre, "_preclose_receipt_ok", return_value=receipt_ok) as ok, \
                mock.patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(out):
            hook.main()
        self.receipt_calls = ok.call_args_list
        return json.loads(out.getvalue() or "{}")

    def denied(self, result) -> bool:
        deny = (result.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
        if deny:
            # A crash reaches the catch-all deny; no case may pass on one.
            self.assertNotIn("the route recheck failed", result.get("systemMessage", ""))
        return deny

    def premerge(self, key):
        return [e for e in route_store.load(key) if e["attrs"]["checkpoint"] == "pre-merge"]


class RefuseStricterDiff(_Case):
    """AC1. Kill check: the comparison always reads "not stricter"."""

    def test_stricter_diff_is_refused_and_recorded(self):
        key = f"{HRSE}#2600"
        self.seed(key, 2, "te-label", "handoff")
        result = self.run_hook(FakeGitHub(HRSE))
        self.assertTrue(self.denied(result))
        self.assertIn("hrse-product", result["systemMessage"])
        events = self.premerge(key)
        self.assertEqual([(e["attrs"]["path"], e["attrs"]["gate_id"]) for e in events],
                         [(1, "hrse-product")])
        self.assertEqual(events[0]["attrs"]["decider"], "pre-merge-recheck")

    def test_not_stricter_allows_and_records_nothing(self):
        key = f"{FORGE}#2600"
        self.seed(key, 2, "lane-machinery", "handoff")
        result = self.run_hook(FakeGitHub(FORGE, files=["tools/hooks/x.py"]))
        self.assertFalse(self.denied(result))
        self.assertEqual(self.premerge(key), [])


class RefusalPersists(_Case):
    """AC2. Kill check: the baseline uses `evaluated_path(K)` (no pre-merge filter)."""

    def test_retry_still_refused_with_no_second_event(self):
        key = f"{HRSE}#2600"
        self.seed(key, 2, "te-label", "handoff")
        for _ in range(2):
            self.assertTrue(self.denied(self.run_hook(FakeGitHub(HRSE))))
        self.assertEqual(len(self.premerge(key)), 1)

    def test_in_force_fallback_without_handoff(self):
        key = f"{HRSE}#2600"
        self.seed(key, 2, "te-label", "filing")
        for _ in range(2):
            self.assertTrue(self.denied(self.run_hook(FakeGitHub(HRSE))))
        self.assertEqual(len(self.premerge(key)), 1)

    def test_a_looser_override_does_not_lower_the_evaluated_baseline(self):
        events = [{"attrs": {"path": 2, "gate_id": "te-label", "checkpoint": "filing",
                             "decider": "evaluator"}},
                  {"attrs": {"path": 3, "gate_id": "runner", "checkpoint": "override",
                             "decider": route_store.OVERRIDE}}]
        # The override floor is looser than the evaluated route: the evaluated route holds.
        self.assertEqual(hook.baseline(events)["attrs"]["path"], 2)


class PassAtExactHead(_Case):
    """AC3. Kill checks: drop the PASS lookup; ignore the PASS's SHA."""

    def setUp(self):
        super().setUp()
        self.key = f"{HRSE}#2600"
        self.seed(self.key, 2, "te-label", "handoff")

    def test_pass_at_head_clears(self):
        gh = FakeGitHub(HRSE, comments=[comment(10, gate_body("PASS", HEAD))])
        self.assertFalse(self.denied(self.run_hook(gh)))

    def test_pass_at_other_sha_is_refused(self):
        gh = FakeGitHub(HRSE, comments=[comment(10, gate_body("PASS", OTHER))])
        self.assertTrue(self.denied(self.run_hook(gh)))

    def test_pass_at_a_prefix_of_the_head_is_refused(self):
        gh = FakeGitHub(HRSE, comments=[comment(10, gate_body("PASS", HEAD[:12]))])
        self.assertTrue(self.denied(self.run_hook(gh)))

    def test_newer_fail_supersedes_pass(self):
        gh = FakeGitHub(HRSE, comments=[comment(10, gate_body("PASS", HEAD)),
                                        comment(11, gate_body("FAIL", HEAD))])
        self.assertTrue(self.denied(self.run_hook(gh)))

    def test_cleared_merge_must_bind_the_head(self):
        gh = FakeGitHub(HRSE, comments=[comment(10, gate_body("PASS", HEAD))])
        result = self.run_hook(gh, command=f"gh pr merge 9 --repo {HRSE} --squash")
        self.assertTrue(self.denied(result))
        self.assertIn("--match-head-commit", result["systemMessage"])


class PrecloseCoversHead(_Case):
    """AC4 (synthetic path-3 events, pitch change 2). Kill check: skip `_preclose_receipt_ok`."""

    def setUp(self):
        super().setUp()
        self.key = f"{FORGE}#2600"
        self.seed(self.key, 3, "runner", "override", decider=route_store.OVERRIDE)
        self.gh = FakeGitHub(FORGE, files=["tools/hooks/x.py"],
                             labels=["tooling-exception", pre.PRECLOSE_LABEL])

    def test_receipt_at_head_clears(self):
        self.assertFalse(self.denied(self.run_hook(self.gh, receipt_ok=True)))
        self.assertEqual(self.receipt_calls[0].args, (FORGE, "2600", HEAD, "9"))

    def test_receipt_at_other_head_is_refused(self):
        result = self.run_hook(self.gh, receipt_ok=False)
        self.assertTrue(self.denied(result))
        self.assertIn("lane-machinery", result["systemMessage"])

    def test_pr_in_another_repo_gets_no_replay(self):
        gh = FakeGitHub(HRSE, title="Wire it (harmonic-forge#2600)", files=["tools/hooks/x.py"],
                        labels=["tooling-exception", pre.PRECLOSE_LABEL])
        self.run_hook(gh, receipt_ok=True)
        self.assertEqual(self.receipt_calls[0].args, (FORGE, "2600", HEAD, None))

    def test_label_missing_is_refused(self):
        self.gh.labels = ["tooling-exception"]
        self.assertTrue(self.denied(self.run_hook(self.gh, receipt_ok=True)))


class NoRouteRecordsAndAllows(_Case):
    """AC5. Kill check: deny when `prior == []`."""

    def test_first_event_recorded_and_allowed_once(self):
        key = f"{HRSE}#2600"
        first = self.run_hook(FakeGitHub(HRSE))
        self.assertFalse(self.denied(first))
        self.assertIn(f"no route recorded for {key}; recorded 1/hrse-product at pre-merge",
                      first["systemMessage"])
        second = self.run_hook(FakeGitHub(HRSE))
        self.assertFalse(self.denied(second))
        self.assertEqual(len(self.premerge(key)), 1)

    def test_first_recorded_event_is_the_baseline(self):
        key = f"{HRSE}#2600"
        first = self.run_hook(FakeGitHub(HRSE, files=["docs/x.md"]))
        self.assertFalse(self.denied(first))
        self.assertEqual(self.premerge(key)[0]["attrs"]["path"], 2)
        result = self.run_hook(FakeGitHub(HRSE))
        self.assertTrue(self.denied(result))
        self.assertIn("hrse-product", result["systemMessage"])


class RunnerExemption(_Case):
    """AC6 (operator ruling "1 yes"). Kill check: delete the exemption branch."""

    def setUp(self):
        super().setUp()
        self.key = f"{FORGE}#2600"
        self.seed(self.key, 3, "runner", "override", decider=route_store.OVERRIDE)

    def test_te_label_only_is_exempt_and_records_nothing(self):
        result = self.run_hook(FakeGitHub(FORGE, files=["docs/x.md"]))
        self.assertFalse(self.denied(result))
        self.assertIn("runner (path 3) merge exempt", result["systemMessage"])
        self.assertEqual(self.premerge(self.key), [])

    def test_genuine_tightening_is_enforced(self):
        key = f"{HRSE}#2600"
        self.seed(key, 3, "runner", "override", decider=route_store.OVERRIDE)
        self.assertTrue(self.denied(self.run_hook(FakeGitHub(HRSE))))


class FailDirections(_Case):
    """AC7. Kill check: catch the store `RuntimeError` and allow."""

    def test_corrupt_store_denies(self):
        part = self.store / "events" / "2026" / "11" / "route"
        part.mkdir(parents=True)
        (part / "bad.jsonl").write_text("{not json\n")
        result = self.run_hook(FakeGitHub(HRSE))
        self.assertTrue(self.denied(result))
        self.assertIn("route store", result["systemMessage"])

    def test_files_unreadable_denies(self):
        gh = FakeGitHub(HRSE)
        gh.down.add("files")
        self.assertTrue(self.denied(self.run_hook(gh)))

    def test_truncated_file_list_denies(self):
        self.assertTrue(self.denied(self.run_hook(FakeGitHub(HRSE, changed=3001))))

    def test_pr_unreadable_denies(self):
        gh = FakeGitHub(HRSE)
        gh.down.add("pr")
        self.assertTrue(self.denied(self.run_hook(gh)))

    def test_unlinked_pr_allows_with_warning(self):
        result = self.run_hook(FakeGitHub(HRSE, ref="main"))
        self.assertFalse(self.denied(result))
        self.assertEqual(result["systemMessage"], "route recheck skipped: PR #9 maps to no issue")

    def test_link_unreadable_denies(self):
        gh = FakeGitHub(HRSE)
        gh.down.add("ref")
        self.assertTrue(self.denied(self.run_hook(gh)))

    def test_comments_unreadable_denies(self):
        self.seed(f"{HRSE}#2600", 2, "te-label", "handoff")
        gh = FakeGitHub(HRSE)
        gh.down.add("comments")
        self.assertTrue(self.denied(self.run_hook(gh)))

    def test_non_merge_commands_pass_untouched(self):
        gh = FakeGitHub(HRSE)
        self.assertEqual(self.run_hook(gh, command="gh issue view 9"), {})
        self.assertEqual(gh.calls, [])


class Deadline(_Case):
    """Pitch change 3: slow reads deny before the registration timeout."""

    def test_slow_reads_deny(self):
        gh = FakeGitHub(HRSE, delay=0.2)
        with mock.patch.object(hook, "DEADLINE_SECONDS", 0.5), \
                mock.patch.object(hook, "READ_CEILING", 0.2):
            result = self.run_hook(gh)
        self.assertTrue(self.denied(result))
        self.assertIn("ran out of time", result["systemMessage"])
        self.assertEqual(self.premerge(f"{HRSE}#2600"), [])


class TitleMapping(_Case):
    """Pitch change 5 (operator ruling "1 A"): the title's `(slug#N)` wins over the branch."""

    def test_title_suffix_names_the_issue_in_another_repo(self):
        gh = FakeGitHub(HRSE, title="Register the hook (harmonic-forge#1243)", ref="feat/1243-x",
                        files=[".claude/settings.json"])
        result = self.run_hook(gh)
        self.assertIn(f"no route recorded for {FORGE}#1243", result["systemMessage"])
        self.assertEqual(route_store.load(f"{HRSE}#1243"), [])
        self.assertEqual(len(route_store.load(f"{FORGE}#1243")), 1)
        self.assertFalse(any(args[:2] == ("pr", "view") and "headRefName" in args
                             for args in gh.calls))

    def test_no_suffix_falls_back_to_the_branch(self):
        gh = FakeGitHub(HRSE, title="Register the hook", ref="feat/1243-x",
                        files=[".claude/settings.json"])
        result = self.run_hook(gh)
        self.assertIn(f"no route recorded for {HRSE}#1243", result["systemMessage"])


if __name__ == "__main__":
    unittest.main()
