#!/usr/bin/env python3
"""Unit tests for deny_retired_belt_sweep.py (harmonic-forge#766).
Run: python3 tools/hooks/test_deny_retired_belt_sweep.py"""

import json
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import deny_retired_belt_sweep as m


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class DecisionTests(unittest.TestCase):
    def test_bare_invocation_is_denied(self):
        result = m.decision("python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py --sweep-for l1")
        self.assertTrue(_is_denied(result))

    def test_l3_value_is_also_denied(self):
        """The flag itself is the trigger, not a particular value -- both
        values were separately retired (harmonic-forge#640, #659), and the
        flag no longer exists in the parser at all."""
        result = m.decision("python3 tools/gh/watch_lane_posts.py --sweep-for l3 --account-repos vitalharmony")
        self.assertTrue(_is_denied(result))

    def test_chained_after_other_commands_is_still_caught(self):
        result = m.decision("cd /tmp && python3 tools/gh/watch_lane_posts.py --sweep-for l1 --repo o/r")
        self.assertTrue(_is_denied(result))

    def test_the_message_never_echoes_the_token_in_either_field(self):
        """The whole point: a refusal that quotes the command back re-teaches
        it -- the exact mechanism harmonic-forge#766 exists to stop. Both
        fields Claude Code can surface to the session are checked -- a prior
        round of this test checked only `permissionDecisionReason`, so it
        would have passed even if `systemMessage` echoed the token."""
        result = m.decision("python3 tools/gh/watch_lane_posts.py --sweep-for l1")
        for field in ("permissionDecisionReason",):
            self.assertNotIn("--sweep-for", result["hookSpecificOutput"][field])
        self.assertNotIn("--sweep-for", result["systemMessage"])
        self.assertIn("belt_plan.py", result["systemMessage"])

    def test_ordinary_belt_command_is_untouched(self):
        result = m.decision(
            "python3 tools/gh/watch_lane_posts.py --queue-for l1 --repo o/r --watch l2"
        )
        self.assertEqual(result, {})

    def test_unrelated_command_is_untouched(self):
        self.assertEqual(m.decision("git status"), {})

    def test_a_flag_that_merely_starts_with_the_same_prefix_is_untouched(self):
        """`\\b` word-boundary: `--sweep-forever` (hypothetical future flag)
        must not collide with the retired token."""
        result = m.decision("python3 tools/gh/watch_lane_posts.py --sweep-forever")
        self.assertEqual(result, {})

    def test_a_grep_for_the_token_is_not_denied(self):
        """AC2's own verification shape: a session confirming the flag is
        gone greps for it, including inside watch_lane_posts.py's own source
        (e.g. a `git log -S`/receipt read). This must not be denied -- the
        message never names what tripped, so a false positive here reads as
        broken tooling, not a diagnosable refusal."""
        result = m.decision('grep -rn "\\-\\-sweep-for" tools/gh/watch_lane_posts.py')
        self.assertEqual(result, {})

    def test_a_read_only_command_with_no_script_mention_is_untouched(self):
        result = m.decision('grep -rn "\\-\\-sweep-for" skills/')
        self.assertEqual(result, {})

    def test_chained_after_a_read_only_segment_is_still_denied(self):
        """Preclose fail-direction finding, round 2: the first version's
        read-only exemption matched anywhere in the WHOLE command, so a
        leading read-only segment exempted a later, unrelated segment that
        actually runs the script. Per-segment analysis closes this."""
        result = m.decision("ls && python3 tools/gh/watch_lane_posts.py --sweep-for l1")
        self.assertTrue(_is_denied(result))

    def test_git_log_dash_s_on_the_token_is_not_denied(self):
        result = m.decision('git log -S"--sweep-for" -- tools/gh/watch_lane_posts.py')
        self.assertEqual(result, {})

    def test_find_exec_is_denied_not_treated_as_read_only(self):
        """Preclose correctness finding, round 3: `find` runs arbitrary
        programs via `-exec`, so it must not be in the read-only-leading
        exemption at all -- a single segment leading with `find` must not
        exempt an actual execution of the retired mechanism."""
        result = m.decision(
            'find . -maxdepth 0 -exec python3 tools/gh/watch_lane_posts.py --sweep-for l1 \\;'
        )
        self.assertTrue(_is_denied(result))

    def test_awk_system_is_denied_not_treated_as_read_only(self):
        result = m.decision(
            'awk \'BEGIN{system("python3 tools/gh/watch_lane_posts.py --sweep-for l1")}\''
        )
        self.assertTrue(_is_denied(result))


class MainTests(unittest.TestCase):
    def _run(self, payload: dict) -> dict:
        with patch("sys.stdin", StringIO(json.dumps(payload))), \
             patch("sys.stdout", new_callable=StringIO) as out:
            m.main()
        return json.loads(out.getvalue())

    def test_unrelated_tool_passes_through(self):
        result = self._run({"tool_name": "Read", "tool_input": {"command": "--sweep-for l1"}})
        self.assertEqual(result, {})

    def test_bash_with_retired_flag_is_denied(self):
        result = self._run({"tool_name": "Bash",
                            "tool_input": {"command": "python3 watch_lane_posts.py --sweep-for l1"}})
        self.assertTrue(_is_denied(result))

    def test_monitor_with_retired_flag_is_denied(self):
        """Preclose silent-bypass finding: Monitor, not Bash, is the belt's
        actual arming channel (`CANONICAL_BELTS` builds a Monitor command),
        and it is the one enforce_belt_arming.py itself does not gate when
        `LANE` is unset -- a Bash-only backstop leaves the exact incident
        this issue exists to stop fully reachable."""
        result = self._run({"tool_name": "Monitor",
                            "tool_input": {"command": "python3 ~/harmonic-forge/tools/gh/"
                                           "watch_lane_posts.py --sweep-for l3 "
                                           "--account-repos vitalharmony --interval 300",
                                           "description": "sweep"}})
        self.assertTrue(_is_denied(result))

    def test_croncreate_with_retired_flag_in_prompt_is_denied(self):
        result = self._run({"tool_name": "CronCreate",
                            "tool_input": {"prompt": "python3 watch_lane_posts.py --sweep-for l1",
                                           "cron": "*/10 * * * *"}})
        self.assertTrue(_is_denied(result))

    def test_skill_with_retired_flag_in_args_is_denied(self):
        result = self._run({"tool_name": "Skill",
                            "tool_input": {"skill": "loop",
                                           "args": "run python3 watch_lane_posts.py --sweep-for l1"}})
        self.assertTrue(_is_denied(result))

    def test_ordinary_monitor_command_passes_through(self):
        result = self._run({"tool_name": "Monitor",
                            "tool_input": {"command": "python3 watch_lane_posts.py --queue-for l1",
                                           "description": "belt"}})
        self.assertEqual(result, {})

    def test_non_dict_tool_input_fails_open(self):
        result = self._run({"tool_name": "Bash", "tool_input": "not a dict"})
        self.assertEqual(result, {})

    def test_malformed_stdin_fails_open(self):
        with patch("sys.stdin", StringIO("not json")), \
             patch("sys.stdout", new_callable=StringIO) as out:
            m.main()
        self.assertEqual(json.loads(out.getvalue()), {})


if __name__ == "__main__":
    unittest.main()
