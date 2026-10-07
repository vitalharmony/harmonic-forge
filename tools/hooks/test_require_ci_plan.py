"""harmonic-forge#918 -- the read-the-gate guard and the `ALLOW EDIT` override,
driven as real hook processes against a real (temp) git repo registered in a
temp manifest. The kill checks: allowing without a receipt, denying in Lane 3,
failing closed on an unresolvable cwd, and ignoring the grant each fail below."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lane"))
import require_ci_plan as guard  # noqa: E402
import grant_ci_plan_override as grant  # noqa: E402

GUARD, GRANT = HERE / "require_ci_plan.py", HERE / "grant_ci_plan_override.py"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "demo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "tooling/918-x")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                 "--allow-empty", "-m", "base")
        (self.repo / "src").mkdir()
        self.manifest = root / "projects.toml"
        self.manifest.write_text(textwrap.dedent(f"""
            [[project]]
            name = "demo"
            prefix = "D"
            path = "{self.repo}"
        """), encoding="utf-8")
        self.receipts = root / "receipts"
        self.outside = root / "elsewhere"
        self.outside.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def env(self, lane):
        env = {k: v for k, v in os.environ.items() if k != "LANE"}
        env.update({"FORGE_PROJECTS_MANIFEST": str(self.manifest),
                    "HARMONIC_FORGE_CI_PLAN_DIR": str(self.receipts),
                    "CLAUDE_CODE_ENTRYPOINT": "cli"})
        if lane is not None:
            env["LANE"] = lane
        return env

    def call(self, payload, lane="2"):
        done = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload),
                              env=self.env(lane), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr

    def edit(self, session="s1", path=None):
        return {"tool_name": "Edit", "session_id": session, "cwd": str(self.repo),
                "tool_input": {"file_path": str(path or self.repo / "src" / "a.py")}}

    def bash(self, command, session="s1"):
        return {"tool_name": "Bash", "session_id": session, "cwd": str(self.repo),
                "tool_input": {"command": command}}

    def receipt(self, session="s1", suffix=".json"):
        self.receipts.mkdir(exist_ok=True)
        (self.receipts / f"{session}{suffix}").write_text("{}\n", encoding="utf-8")


def denied(result):
    return bool(result and result["hookSpecificOutput"]["permissionDecision"] == "deny")


class GuardTests(Fixture):
    def test_lane2_edit_is_denied_with_the_exact_command(self):
        result, _ = self.call(self.edit())
        self.assertTrue(denied(result))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("python3 ~/harmonic-forge/tools/lane/ci_plan.py", reason)
        self.assertIn("ALLOW EDIT", reason)

    def test_a_receipt_allows_it_and_a_new_session_needs_its_own(self):
        self.receipt("s1")
        self.assertFalse(denied(self.call(self.edit("s1"))[0]))
        self.assertTrue(denied(self.call(self.edit("s2"))[0]))

    def test_write_notebook_and_gate_commands_are_gated(self):
        for tool in ("Write", "MultiEdit", "NotebookEdit"):
            payload = self.edit()
            payload["tool_name"] = tool
            self.assertTrue(denied(self.call(payload)[0]), tool)
        for command in ("mise run check", "mise r ci-check", "mise run check-full --fail-fast",
                        "cd backend && mise run check", "mise check"):
            self.assertTrue(denied(self.call(self.bash(command))[0]), command)

    def test_other_tools_and_commands_are_never_denied(self):
        for payload in ({"tool_name": "Read", "session_id": "s1", "tool_input": {}},
                        self.bash("git status"), self.bash("mise run restart"),
                        self.bash("echo mise run check"), self.bash("mise run check-steps")):
            self.assertFalse(denied(self.call(payload)[0]), payload)

    def test_lane3_and_no_lane_are_never_denied(self):
        for lane in ("3", None):
            self.assertFalse(denied(self.call(self.edit(), lane=lane)[0]), lane)

    def test_lane1_is_gated_only_on_a_tooling_branch(self):
        self.assertTrue(denied(self.call(self.edit(), lane="1")[0]))
        self.git("checkout", "-q", "-b", "feat/other")
        self.assertFalse(denied(self.call(self.edit(), lane="1")[0]))

    def test_no_session_id_is_never_denied(self):
        payload = self.edit()
        payload["session_id"] = ""
        self.assertFalse(denied(self.call(payload)[0]))

    def test_an_edit_outside_every_project_fails_open_with_one_line(self):
        result, err = self.call(self.edit(path=self.outside / "note.md"))
        self.assertFalse(denied(result))
        self.assertIn("require_ci_plan: allowed", err)

    def test_an_unresolvable_cwd_fails_open(self):
        payload = self.bash("mise run check")
        payload["cwd"] = str(self.outside)
        result, err = self.call(payload)
        self.assertFalse(denied(result))
        self.assertIn("allowed", err)

    def test_garbage_input_fails_open(self):
        done = subprocess.run([sys.executable, str(GUARD)], input="not json",
                              env=self.env("2"), capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_the_allow_edit_grant_lifts_the_guard(self):
        self.receipt("s1", ".allow")
        self.assertFalse(denied(self.call(self.edit("s1"))[0]))
        self.assertTrue(denied(self.call(self.edit("s2"))[0]))


class OverrideTests(Fixture):
    def submit(self, prompt, lane="2", session="s1"):
        done = subprocess.run(
            [sys.executable, str(GRANT)],
            input=json.dumps({"prompt": prompt, "session_id": session}),
            env=self.env(lane), capture_output=True, text=True)
        return json.loads(done.stdout) if done.stdout.strip() else {}

    def test_a_typed_allow_edit_line_records_a_grant_the_guard_honors(self):
        self.assertTrue(denied(self.call(self.edit())[0]))
        out = self.submit("ALLOW EDIT scratch spike")
        self.assertIn("granted", out["systemMessage"])
        self.assertTrue((self.receipts / "s1.allow").is_file())
        self.assertFalse(denied(self.call(self.edit())[0]))

    def test_the_grant_is_session_scoped(self):
        self.submit("ALLOW EDIT", session="s1")
        self.assertTrue(denied(self.call(self.edit("s2"))[0]))

    def test_lane3_and_prose_mentions_record_nothing(self):
        self.submit("ALLOW EDIT", lane="3")
        self.submit("please do not ALLOW EDIT yet")
        self.submit("> ALLOW EDIT quoted")
        self.assertFalse(self.receipts.exists())

    def test_a_bad_session_id_is_refused_out_loud(self):
        out = self.submit("ALLOW EDIT", session="../x")
        self.assertIn("REFUSED", out["systemMessage"])
        self.assertFalse(self.receipts.exists())


if __name__ == "__main__":
    unittest.main()
