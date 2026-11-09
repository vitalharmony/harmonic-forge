"""harmonic-forge#785: `strip_invocation_prefix`'s #782 shell unwrap is opt-out.

Gate classifiers want `bash -c "<script>"` unwrapped to the command inside;
write guards must see the nested shell itself, or their nested-shell rules
never fire.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
from shell_parse import strip_invocation_prefix  # noqa: E402

NESTED = ["bash", "-c", "echo x > y"]


class UnwrapShellsTests(unittest.TestCase):
    def test_default_unwraps_a_nested_shell(self):
        self.assertEqual(strip_invocation_prefix(list(NESTED)), ["echo", "x", ">", "y"])

    def test_unwrap_shells_false_leaves_the_nested_shell_intact(self):
        self.assertEqual(strip_invocation_prefix(list(NESTED), unwrap_shells=False), NESTED)

    def test_unwrap_shells_false_still_strips_gh_as(self):
        self.assertEqual(
            strip_invocation_prefix(["gh-as", "vitalharmony", "gh", "pr", "view"], unwrap_shells=False),
            ["gh", "pr", "view"],
        )

    def test_dash_and_ksh_unwrap_like_bash(self):
        for shell in ("dash", "ksh"):
            with self.subTest(shell=shell):
                self.assertEqual(
                    strip_invocation_prefix([shell, "-c", "echo x > y"]),
                    ["echo", "x", ">", "y"],
                )

    def test_dash_ksh_unwrap_shells_false_leaves_them_intact(self):
        for shell in ("dash", "ksh"):
            with self.subTest(shell=shell):
                tokens = [shell, "-c", "echo x > y"]
                self.assertEqual(strip_invocation_prefix(list(tokens), unwrap_shells=False), tokens)


class TimeoutNiceStdbufTests(unittest.TestCase):
    """harmonic-forge#787: these three were missing from the strip set
    entirely, so a classifier/guard downstream saw `timeout`/`nice`/`stdbuf`
    as the invoked program rather than a wrapper -- allowing every one of
    them to hide a write or a `gh` mutation from every caller of this
    function (batch_auth/model_tier_gate/enforce_belt_arming/etc. included).
    """

    def test_timeout_bare(self):
        self.assertEqual(strip_invocation_prefix(["timeout", "5", "tee", "y"]), ["tee", "y"])

    def test_timeout_with_flags_and_long_duration(self):
        self.assertEqual(
            strip_invocation_prefix(["timeout", "-k", "10", "-s", "TERM", "30", "tee", "y"]),
            ["tee", "y"],
        )
        self.assertEqual(
            strip_invocation_prefix(["timeout", "--kill-after=10", "30", "tee", "y"]),
            ["tee", "y"],
        )

    def test_nice_bare(self):
        self.assertEqual(strip_invocation_prefix(["nice", "tee", "y"]), ["tee", "y"])

    def test_nice_with_adjustment_forms(self):
        self.assertEqual(strip_invocation_prefix(["nice", "-n", "10", "tee", "y"]), ["tee", "y"])
        self.assertEqual(strip_invocation_prefix(["nice", "-10", "tee", "y"]), ["tee", "y"])
        self.assertEqual(
            strip_invocation_prefix(["nice", "--adjustment=10", "tee", "y"]), ["tee", "y"]
        )

    def test_nice_attached_n_value_and_double_dash(self):
        """Preclose finding (4 of 5 refuters converged on this): `-n10` is a
        valid getopt attached-value form, and `--` is nice's own end-of-options
        marker. Both slipped past a single-token peek in an earlier draft."""
        self.assertEqual(strip_invocation_prefix(["nice", "-n10", "tee", "y"]), ["tee", "y"])
        self.assertEqual(strip_invocation_prefix(["nice", "-n-5", "tee", "y"]), ["tee", "y"])
        self.assertEqual(strip_invocation_prefix(["nice", "--", "tee", "y"]), ["tee", "y"])

    def test_stdbuf_combined_and_separate_flags(self):
        self.assertEqual(strip_invocation_prefix(["stdbuf", "-oL", "tee", "y"]), ["tee", "y"])
        self.assertEqual(
            strip_invocation_prefix(["stdbuf", "-o", "L", "-e", "L", "tee", "y"]), ["tee", "y"]
        )

    def test_stacked_prefixes_all_strip(self):
        self.assertEqual(
            strip_invocation_prefix(["nice", "timeout", "5", "env", "tee", "y"]), ["tee", "y"]
        )

    def test_timeout_wraps_nested_shell_and_still_unwraps(self):
        self.assertEqual(
            strip_invocation_prefix(["timeout", "5", "bash", "-c", "echo x > y"]),
            ["echo", "x", ">", "y"],
        )

    def test_negatives_unaffected(self):
        # A bare cat/tee with no prefix at all is untouched.
        self.assertEqual(strip_invocation_prefix(["cat", "/tmp/x"]), ["cat", "/tmp/x"])

    def test_wrapped_commands_own_flags_survive_intact(self):
        """test-honesty finding: a negative that never runs the new
        timeout/nice/stdbuf branches at all (e.g. a bare `cat`) stays green
        under any mutation of those branches and proves nothing about them.
        This one does exercise them: it fails if the option-stripping loop
        over-consumes into the WRAPPED command's own flags, not just the
        wrapper's."""
        self.assertEqual(
            strip_invocation_prefix(["timeout", "5", "tee", "-a", "/tmp/x"]),
            ["tee", "-a", "/tmp/x"],
        )
        self.assertEqual(
            strip_invocation_prefix(["nice", "-n5", "sed", "-i", "s/a/b/", "/tmp/x"]),
            ["sed", "-i", "s/a/b/", "/tmp/x"],
        )
        self.assertEqual(
            strip_invocation_prefix(["stdbuf", "-oL", "cp", "-r", "/tmp/a", "/tmp/x"]),
            ["cp", "-r", "/tmp/a", "/tmp/x"],
        )



# harmonic-forge#1289: `simple_commands` and `substitution_bodies`, additive.
from shell_parse import GroupMarker, SimpleCommand, simple_commands, substitution_bodies  # noqa: E402


def _programs(command: str) -> list[str]:
    return [item.argv[0] for item in simple_commands(command)
            if isinstance(item, SimpleCommand) and item.argv]


class SimpleCommandsTests(unittest.TestCase):
    def test_fd_dup_is_not_a_program(self):
        self.assertEqual(_programs("x 2>&1 | tee /tmp/a"), ["x", "tee"])
        first = simple_commands("x 2>&1 | tee /tmp/a")[0]
        self.assertEqual(first.argv, ["x"])
        self.assertEqual(first.redirects, [(">&", None)])

    def test_redirect_target_is_recorded(self):
        (item,) = simple_commands("cat a >/tmp/out 2>>/tmp/err")
        self.assertEqual(item.argv, ["cat", "a"])
        self.assertEqual(item.redirects, [(">", "/tmp/out"), (">>", "/tmp/err")])

    def test_merged_operators_split(self):
        self.assertEqual(simple_commands("a&&(b)"), [
            SimpleCommand(["a"], [], None, "&&"), GroupMarker("open"),
            SimpleCommand(["b"], [], "(", ")"), GroupMarker("close")])
        self.assertEqual(simple_commands("x);"), [SimpleCommand(["x"], [], None, ")"), GroupMarker("close")])
        self.assertEqual(simple_commands("diff <(ls a)"), [
            SimpleCommand(["diff"], [], None, "<("), GroupMarker("open"),
            SimpleCommand(["ls", "a"], [], "<(", ")"), GroupMarker("close")])

    def test_redirect_ampersand_to_file_is_a_target(self):
        (item,) = simple_commands("ls >& ~/f")
        self.assertEqual(item.redirects, [(">&", "~/f")])
        (dup,) = simple_commands("ls 1>&2")
        self.assertEqual(dup.redirects, [(">&", None)])

    def test_reserved_words_are_stripped(self):
        self.assertEqual(_programs("for f in a; do rm x; done"), ["for", "rm"])
        self.assertEqual(_programs("if test -f a; then cat a; fi"), ["test", "cat"])

    def test_heredoc_delimiter_is_not_a_program(self):
        self.assertEqual(_programs("cat <<EOF > /tmp/x\nhello world\nEOF\necho done"), ["cat", "echo"])
        self.assertEqual(_programs("cat <<-'END'\n\tbody\n\tEND\n"), ["cat"])

    def test_unparseable_raises(self):
        for text in ('echo "open', "cat >", "x > ; y"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                simple_commands(text)

    def test_substitution_bodies(self):
        self.assertEqual(substitution_bodies("a$(b $(c))d"), ["b $(c)"])
        self.assertEqual(substitution_bodies("`x y`z$(w)"), ["x y", "w"])
        self.assertEqual(substitution_bodies("plain"), [])
        for text in ("$(open", "`open"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                substitution_bodies(text)


if __name__ == "__main__":
    unittest.main()
