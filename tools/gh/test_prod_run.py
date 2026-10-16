#!/usr/bin/env python3
"""harmonic-forge#878: the AE's production-run declaration (`_prod_run`)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _prod_run as p  # noqa: E402

SHA = "a" * 40
FIELD = f"prod-run=issue:1867,sha:{SHA},script:scripts/1-1891-x.py,apply:false"


def _ae(field: str = FIELD, prose: str = "") -> str:
    return (f"## AE — H1867\n\n{prose}\n\n<!-- l1-post v1; kind=ae; sha={SHA}; {field}; "
            f"body-sha256={'0' * 64}; checks=body-validation -->")


class ParseSpecTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(p.parse_spec("script=scripts/1-a.py"), {"script": "scripts/1-a.py", "apply": False})
        self.assertEqual(p.parse_spec("script=scripts/1-a.py,apply"), {"script": "scripts/1-a.py", "apply": True})
        self.assertEqual(p.parse_spec("count-label=Person"), {"count_label": "Person"})

    def test_refusals(self):
        for spec in ("", "script=scripts/a.py", "script=scripts/sub/1-a.py", "script=../1-a.py",
                     "script=scripts/1-a.py,apply,apply", "script=scripts/1-a.py,dry",
                     "count-label=", "count-label=A`B", "cypher=MATCH (n) RETURN n"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                p.parse_spec(spec)


class DeclaredTests(unittest.TestCase):
    def test_reads_the_trailing_footer(self):
        self.assertEqual(p.declared(_ae()), {"issue": 1867, "sha": SHA,
                                             "script": "scripts/1-1891-x.py", "apply": False})

    def test_round_trips_footer_field(self):
        for action in ({"script": "scripts/1-a.py", "apply": True}, {"count_label": "Task"}):
            body = _ae(p.footer_field(1867, SHA, action))
            self.assertEqual(p.action_of(p.declared(body)), action)

    def test_prose_never_satisfies_it(self):
        """The same token in the AE's prose, with a footer that carries none."""
        body = (f"## AE — H1867\n\n{FIELD}\n\n<!-- l1-post v1; kind=ae; sha={SHA}; "
                "checks=body-validation -->")
        self.assertIsNone(p.declared(body))

    def test_a_fenced_footer_never_satisfies_it(self):
        body = (f"## AE — H1867\n\n```\n<!-- l1-post v1; kind=ae; sha={SHA}; {FIELD}; -->\n```\n\n"
                f"<!-- l1-post v1; kind=ae; sha={SHA}; checks=x -->")
        self.assertIsNone(p.declared(body))
        # ...and a quoted footer at the very end, inside a fence, is no footer at all.
        self.assertIsNone(p.declared(f"x\n```\n<!-- l1-post v1; kind=ae; {FIELD}; -->\n```"))

    def test_malformed_fails_closed(self):
        for field in (f"prod-run=issue:1867,sha:{SHA[:12]},script:scripts/1-a.py,apply:false",
                      f"prod-run=issue:1867,sha:{SHA},script:scripts/1-a.py",
                      f"prod-run=issue:1867,sha:{SHA},script:scripts/1-a.py,apply:yes",
                      f"prod-run=issue:1867,sha:{SHA},script:scripts/1-a.py,apply:true,count-label:A",
                      f"prod-run=issue:x,sha:{SHA},count-label:A",
                      f"prod-run=issue:1867,issue:1,sha:{SHA},count-label:A"):
            with self.subTest(field=field):
                self.assertIsNone(p.declared(_ae(field)))

    def test_footer_field_needs_a_full_sha(self):
        with self.assertRaises(ValueError):
            p.footer_field(1, "abc1234", {"count_label": "A"})


class ScriptContractTests(unittest.TestCase):
    """harmonic-forge#1026: the runner's contract, checked statically."""
    GOOD = ("import argparse\nfrom _migration_env import ensure_env\n"
            "p = argparse.ArgumentParser()\np.add_argument('--apply', action='store_true')\nensure_env()\n")

    def v(self, src):
        return p.script_contract_violations(src)

    def test_the_2396_shape_passes(self):
        self.assertEqual(self.v(self.GOOD), [])

    def test_the_1896_shape_names_both_env_violations(self):
        src = "from dotenv import load_dotenv\nimport argparse\np=argparse.ArgumentParser()\np.add_argument('--apply', action='store_true')\nload_dotenv('x')\n"
        self.assertEqual(self.v(src), ["no ensure_env() call", "calls load_dotenv()"])

    def test_each_rule_alone(self):
        self.assertEqual(self.v(self.GOOD.replace("ensure_env()\n", "")), ["no ensure_env() call"])
        self.assertEqual(self.v(self.GOOD + "from dotenv import load_dotenv\nload_dotenv()\n"), ["calls load_dotenv()"])
        self.assertEqual(self.v(self.GOOD.replace("'--apply'", "'--execute'")), ["no --apply option"])
        self.assertEqual(self.v(self.GOOD + "p.add_argument('--owner', required=True)\n"), ["required argument --owner"])
        self.assertEqual(self.v(self.GOOD + "p.add_argument('target')\n"), ["required argument target"])

    def test_optional_positionals_pass(self):
        self.assertEqual(self.v(self.GOOD + "p.add_argument('t', nargs='?')\np.add_argument('u', nargs='*')\n"), [])

    def test_a_positional_with_only_a_default_is_required(self):
        # argparse: default= alone never makes a positional optional.
        self.assertEqual(self.v(self.GOOD + "p.add_argument('target', default='x')\n"), ["required argument target"])

    def test_constructs_the_no_argument_run_cannot_satisfy_are_refused(self):
        cases = {
            "g = p.add_mutually_exclusive_group(required=True)\ng.add_argument('--owner')\n": "add_mutually_exclusive_group",
            "s = p.add_subparsers(dest='cmd', required=True)\n": "add_subparsers",
            "REQ = True\np.add_argument('--owner', required=REQ)\n": "required argument --owner",
            "opts = {'required': True}\np.add_argument('--owner', **opts)\n": "not a literal declaration",
        }
        for extra, needle in cases.items():
            with self.subTest(extra=extra):
                self.assertTrue(any(needle in v for v in self.v(self.GOOD + extra)), self.v(self.GOOD + extra))

    def test_a_value_taking_apply_is_refused(self):
        for decl in ("'--apply', type=str", "'--apply', nargs=1", "'--apply'"):
            with self.subTest(decl=decl):
                src = self.GOOD.replace("'--apply', action='store_true'", decl)
                self.assertIn("--apply is not a literal action='store_true' flag", self.v(src))

    def test_apply_with_an_alias_passes(self):
        self.assertEqual(self.v(self.GOOD.replace("'--apply'", "'--apply', '--execute', dest='apply'")), [])

    def test_a_docstring_mention_is_not_a_call(self):
        src = '"""Use ensure_env() here."""\n' + self.GOOD.replace("ensure_env()\n", "# ensure_env()\n")
        self.assertIn("no ensure_env() call", self.v(src))

    def test_an_aliased_call_counts(self):
        src = self.GOOD.replace("import ensure_env", "import ensure_env as load_env").replace("ensure_env()\n", "load_env()\n")
        self.assertEqual(self.v(src), [])

    def test_a_syntax_error_is_a_violation(self):
        self.assertTrue(self.v("def (:\n")[0].startswith("not parseable Python"))


if __name__ == "__main__":
    unittest.main()
