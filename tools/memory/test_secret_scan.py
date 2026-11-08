#!/usr/bin/env python3
"""Tests for the --probe check in secret_scan.py (harmonic-forge#1288, AC5).

Every token-shaped value is built by concatenation, so no literal token sits in
this source for a scanner or a reader to trip over.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import secret_scan as s  # noqa: E402

ALNUM = "A1b2C3d4E5f6G7h8"

#: A valid HS256 header (`{"alg":"HS256","typ":"JWT"}`), split so no JWT sits here.
JWT_HEADER = "ey" + "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"

KW, AT, URL = "credential-keyword", "at-sign", "url-scheme"

#: Every pass-1 and pass-2 secret survivor, the REFORGE verdict's own strings
#: and the operator's extras, mapped to the content kinds that refuse each one.
SURVIVORS: dict[str, set[str]] = {
    # Pass 1, #2.
    "export DB_PASS" + "WORD='$3cr3tHunter2'": {KW},
    "PASS" + "WORD=$'literal'": {KW},
    "postgresql://app:" + "$ecretPw@db/x": {AT, URL},
    # Pass 1, #3.
    "headers={'Author" + "ization': 'Bearer " + ALNUM + "'}": {KW},
    "Author" + 'ization: Bearer "literal"': {KW},
    '{"pass' + 'word": "x"}': {KW},
    # Pass 1, #4.
    JWT_HEADER + ".e30." + "sig" + ALNUM: {"jwt"},
    "curl https://alice" + "@localhost/x": {AT, URL},
    "mail person" + "@evilnoreply.github.com": {AT},
    # Pass 2, #1.
    "export DB_PASS" + "WORD=${PREFIX}hunter2": {KW},
    'PASS' + 'WORD="$P"hunter2': {KW},
    "TOK" + "EN=$(cat f)hunter2": {KW},
    'pass' + 'word=""hunter2': {KW},
    'curl -H "Author' + 'ization: Bearer ${TOKEN}hunter2"': {KW},
    'curl -u "$USER":hunter2 https://x': {URL},
    # Pass 2, #2.
    "mvn deploy -Dpass" + "word=hunter2": {KW},
    "java -Djavax.net.ssl.keyStorePass" + "word=hunter2 -jar x.jar": {KW},
    "curl -dpass" + "word=hunter2": {KW},
    "app --spring.datasource.pass" + "word=hunter2": {KW},
    "tool --access" + "Token=hunter2": {KW},
    "tool --github" + "Token hunter2": {KW},
    # Pass 2, #3.
    'curl -d "{\\"pass' + 'word\\":\\"hunter2\\"}" https://x': {KW, URL},
    # Pass 2, #5.
    "PASS" + "WORD=Bearer hunter2": {KW},
    # The REFORGE verdict.
    "TOK" + "EN=$(echo hunter2)": {KW},
    "export DB_PASS" + "WORD=$(printf hunter2)": {KW},
    "echo hunter2 | gh auth login --with-" + "token": {KW},
}

#: The operator's extras with no keyword, no `@` or `://`, no vendor shape and
#: under 32 characters: explicitly OUT OF SCOPE under the rewritten AC5. They
#: are listed so the residual is named, not discovered.
OUT_OF_SCOPE = ["mysql -phunter2", "sshpass -p hunter2", "DB_PASS=hunter2", "pw=hunter2"]

#: kind -> values that must be found.
HITS = {
    "credential-keyword": ["GH_" + "TOKEN=x", "my api" + "Key", "a pass" + "wd file", "PWD", "client_sec" + "ret",
                           "private-" + "key", "AUTH_" + "KEY", "api-" + "key"],
    "at-sign": ["x" + "@y", "@"],
    "url-scheme": ["ftp:" + "//x", "://"],
    "github-token": ["gh" + "p_" + ALNUM * 3, "github" + "_pat_" + ALNUM * 2],
    "anthropic-key": ["sk-" + "ant-" + "api03-" + ALNUM * 2],
    "openai-key": ["sk-" + "proj-" + ALNUM * 2, "sk-" + ALNUM * 2],
    "aws-access-key": ["AK" + "IA" + "ABCDEFGHIJ234567"],
    "slack-token": ["xo" + "xb-" + "1234567890-" + ALNUM, "hooks.slack" + ".com/services/T0/B0/" + ALNUM],
    "jwt": ["ey" + "J" + ALNUM + ".ey" + "J" + ALNUM + "." + ALNUM, JWT_HEADER + ".e30." + "sig" + ALNUM],
    "private-key": ["-----BEGIN " + "RSA PRIVATE KEY-----", "-----BEGIN " + "PRIVATE KEY-----"],
    "opaque-token": ["AI" + "za" + ALNUM * 2 + "xQ9", "Q" + ALNUM * 2 + "k9Z0pPzz"],
}

#: Probes that must stay clean.
CLEAN = [
    "re-read the issue's AC before claiming",
    "ls the target dir before writing",
    "git -C the worktree status before the commit",
    "gh api the issue state before posting the close",
    "0123456789abcdef0123456789abcdef01234567",
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "promptId 3f2c9a1e-5b6d-4c7e-8f90-123456789abc",
    "",
]


class Findings(unittest.TestCase):
    def test_every_survivor_refuses_by_its_named_rule(self):
        for value, kinds in SURVIVORS.items():
            with self.subTest(value=value[:16]):
                found = set(s.probe_findings(value))
                self.assertTrue(found, "a survivor must refuse")
                self.assertTrue(kinds <= found, f"expected {kinds}, got {found}")

    def test_out_of_scope_strings_have_no_mark(self):
        """The named residual: no keyword, @, :// or shape, under 32 chars."""
        for value in OUT_OF_SCOPE:
            with self.subTest(value=value):
                self.assertLess(len(value), 32)
                self.assertEqual(s.findings(value), [])

    def test_every_kind_is_found(self):
        for kind, values in HITS.items():
            for value in values:
                with self.subTest(kind=kind, value=value[:10]):
                    self.assertIn(kind, s.findings(value))

    def test_keyword_is_case_insensitive_substring(self):
        for value in ("TOKEN", "Token", "xtokenx", "accessToken", "SecretS", "x.password.y"):
            with self.subTest(value=value):
                self.assertIn("credential-keyword", s.findings(value))

    def test_clean_text_has_no_findings(self):
        for text in CLEAN:
            with self.subTest(text=text):
                self.assertEqual(s.probe_findings(text), [])

    def test_none_is_clean(self):
        self.assertEqual(s.probe_findings(None), [])

    def test_length_cap_is_200(self):
        self.assertEqual(s.MAX_PROBE, 200)
        self.assertEqual(s.probe_findings("x" * 200), [])
        self.assertEqual(s.probe_findings("x" * 201), ["over-length"])

    def test_one_line_only(self):
        for value in ("a\nb", "a\rb", "a\n", "a b"):
            with self.subTest(value=value):
                self.assertIn("multi-line", s.probe_findings(value))

    def test_findings_are_kind_names_never_the_text(self):
        known = set(s.KINDS) | set(s.FORM_KINDS)
        for value in [*SURVIVORS, *(v for vs in HITS.values() for v in vs)]:
            for kind in s.probe_findings(value):
                self.assertIn(kind, known)
                self.assertNotIn(kind, value)

    def test_each_kind_reported_once(self):
        token = "gh" + "p_" + ALNUM * 3
        found = s.findings(f"{token} and {token}")
        self.assertEqual(len(found), len(set(found)))
        self.assertIn("github-token", found)

    def test_every_kind_has_a_hit(self):
        self.assertEqual(set(HITS), set(s.KINDS))

    def test_no_exemption_machinery_remains(self):
        source = Path(s.__file__).read_text(encoding="utf-8")
        for gone in ("_ASSIGN", "_FLAG", "_BASIC", "is_exempt", "_unquote", "noreply", "_EXPANSION"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, source)

    def test_the_source_reaches_no_network(self):
        source = Path(s.__file__).read_text(encoding="utf-8")
        for forbidden in ("requests", "urllib", "httpx", "socket", "subprocess"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
