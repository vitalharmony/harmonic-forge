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


if __name__ == "__main__":
    unittest.main()
