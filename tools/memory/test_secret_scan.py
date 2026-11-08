#!/usr/bin/env python3
"""Tests for secret_scan.py (harmonic-forge#1288, AC5).

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

#: kind -> secret-shaped values that must be found. Includes the sticky-wicket
#: verdict's "must be caught" list (harmonic-forge#1288, pass-1 cluster).
HITS = {
    "github-token": ["gh" + "p_" + ALNUM * 3, "github" + "_pat_" + ALNUM * 2],
    "anthropic-key": ["sk-" + "ant-" + "api03-" + ALNUM * 2],
    "openai-key": ["sk-" + "proj-" + ALNUM * 2, "sk-" + ALNUM * 2],
    "aws-access-key": ["AK" + "IA" + "ABCDEFGHIJ234567"],
    "slack-token": ["xo" + "xb-" + "1234567890-" + ALNUM, "https://hooks.slack" + ".com/services/T0/B0/" + ALNUM],
    "jwt": ["ey" + "J" + ALNUM + ".ey" + "J" + ALNUM + "." + ALNUM,
            JWT_HEADER + ".e30." + "sig" + ALNUM],
    "private-key": ["-----BEGIN " + "RSA PRIVATE KEY-----", "-----BEGIN " + "PRIVATE KEY-----"],
    "authorization-header": [
        "curl -H 'Authorization: Bearer " + ALNUM + "'", "Authorization: token " + ALNUM,
        "authorization:" + ALNUM, "headers={'Author" + "ization': 'Bearer " + ALNUM + "'}",
        "Author" + 'ization: Bearer "literal"', '{"Author' + 'ization": "token X"}',
        "curl -H 'Authorization: Bearer $3cr'"],
    "credential-assignment": [
        "GH_" + "TOKEN=" + ALNUM, "export DB_PASS" + "WORD='" + ALNUM + "'",
        "my_api_" + "key = " + ALNUM, "CLIENT_SEC" + "RET=\"x\"",
        # The narrowed `$` exemption: single quotes, `$'...'`, `$<digit>` and a
        # partial expansion are literals.
        "export DB_PASS" + "WORD='$3cr3tHunter2'", "PASS" + "WORD=$'literal'",
        'export DB_PASS' + 'WORD="$3cr"', "DB_PASS" + "WORD=$ecretPw", 'TOKEN="$NAME-suffix"',
        # Any key position and separator.
        '{"pass' + 'word": "x"}', '{"api_' + 'key": "abc123"}', "pass" + "word: hunter2",
        "api_" + "key := x", 'sec' + 'ret => "x"', 'conf["client_' + 'secret"]="x"',
        "--tok" + "en abc123def", "--pass" + "word=x", "--api-" + "key x", "--gh-tok" + "en=" + ALNUM],
    "basic-auth": ["curl -u user:" + "hunter2 https://x", "curl --user=deploy:" + "pw1 https://x",
                   "curl -u 'u:$P' https://x"],
    "url-credentials": ["https://user:" + ALNUM + "@example.org/x", "https://" + ALNUM * 2 + "@github.com/a/b",
                        "postgresql://app:" + "$ecretPw@db/x", "curl https://alice" + "@localhost/x",
                        "https://user:" + "$PW-x@h/"],
    "email-address": ["mail someone" + "@example.com now", "mail person" + "@evilnoreply.github.com",
                      "x" + "@noreply.github.com.evil.com", "noreplyx" + "@example.com"],
    "opaque-token": ["AI" + "za" + ALNUM * 2 + "xQ9", "Q" + ALNUM * 2 + "k9Z0pPzz"],
}

#: Text that must stay clean: the existing list plus the verdict's "must stay
#: clean" list.
CLEAN = [
    "python3 tools/gh/l1_post.py --repo vitalharmony/harmonic-forge --issue 1288",
    'GH_TOKEN="$TOKEN" gh api repos/a/b',
    'GH_TOKEN="$GH_TOKEN"',
    "GH_TOKEN=$(gh auth token) gh pr view 1",
    "export API_KEY=${API_KEY}",
    "curl -H 'Authorization: Bearer $TOKEN' https://x",
    'curl -H "Authorization: token ${GH}" https://x',
    '--token "$TOKEN"',
    '{"password": "${PW}"}',
    "https://user:$PASSWORD@example.org/x",
    "https://x-access-token:$GH_TOKEN@github.com/a/b",
    "ssh://git@github.com/org/repo",
    "git clone git@github.com:vitalharmony/harmonic-forge.git",
    "Co-Authored-By: Claude <noreply@anthropic.com>",
    "12345+someone@users.noreply.github.com",
    "no-reply@example.com",
    "the token was stale; re-read it",
    "password reset flow failed",
    "python3 scripts/get_test_token.py",
    "l1_post.py --token-file path",
    "0123456789abcdef0123456789abcdef01234567",
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "promptId 3f2c9a1e-5b6d-4c7e-8f90-123456789abc",
    "body-sha256=" + "0a" * 32,
    "mkdir -p x",
    "git log -p",
    "git push -u origin main",
    "curl -u user https://x",
    'curl -u "$USER:$PASS" https://x',
    "Authorization: Bearer",
    "if token == x",
    "",
]


class Findings(unittest.TestCase):
    def test_every_kind_is_found(self):
        for kind, values in HITS.items():
            for value in values:
                with self.subTest(kind=kind, value=value[:10]):
                    self.assertIn(kind, s.findings(value))

    def test_clean_text_has_no_findings(self):
        for text in CLEAN:
            with self.subTest(text=text):
                self.assertEqual(s.findings(text), [])

    def test_none_is_clean(self):
        self.assertEqual(s.findings(None), [])

    def test_findings_are_kind_names_never_the_text(self):
        known = set(s.KINDS)
        for values in HITS.values():
            for value in values:
                for kind in s.findings(value):
                    self.assertIn(kind, known)
                    self.assertNotIn(kind, value)

    def test_each_kind_reported_once(self):
        token = "gh" + "p_" + ALNUM * 3
        self.assertEqual(s.findings(f"{token} and {token}"), ["github-token"])

    def test_every_kind_has_a_hit(self):
        self.assertEqual(set(HITS), set(s.KINDS))

    def test_backstop_skips_text_another_kind_explains(self):
        self.assertEqual(s.findings("GH_" + "TOKEN=" + ALNUM * 3), ["credential-assignment"])

    def test_exemption_is_whole_value_and_quote_aware(self):
        cases = [("$TOKEN", "none", True), ("$TOKEN", "double", True), ("${pw}", "none", True),
                 ("$(gh auth token)", "double", True), ("$TOKEN", "single", False),
                 ("$TOKEN", "ansi", False), ("$ecretPw", "none", False), ("$3cr", "none", False),
                 ("$TOKEN-x", "none", False), ("x$TOKEN", "double", False)]
        for value, quoting, exempt in cases:
            with self.subTest(value=value, quoting=quoting):
                self.assertIs(s.is_exempt(value, quoting), exempt)

    def test_the_source_reaches_no_network(self):
        source = Path(s.__file__).read_text(encoding="utf-8")
        for forbidden in ("requests", "urllib", "httpx", "socket", "subprocess"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
