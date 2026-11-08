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

#: kind -> secret-shaped values that must be found.
HITS = {
    "github-token": ["gh" + "p_" + ALNUM * 3, "github" + "_pat_" + ALNUM * 2],
    "anthropic-key": ["sk-" + "ant-" + "api03-" + ALNUM * 2],
    "openai-key": ["sk-" + "proj-" + ALNUM * 2, "sk-" + ALNUM * 2],
    "aws-access-key": ["AK" + "IA" + "ABCDEFGHIJ234567"],
    "slack-token": ["xo" + "xb-" + "1234567890-" + ALNUM, "https://hooks.slack" + ".com/services/T0/B0/" + ALNUM],
    "jwt": ["ey" + "J" + ALNUM + ".ey" + "J" + ALNUM + "." + ALNUM],
    "private-key": ["-----BEGIN " + "RSA PRIVATE KEY-----", "-----BEGIN " + "PRIVATE KEY-----"],
    "authorization-header": ["curl -H 'Authorization: Bearer " + ALNUM + "'",
                             "Authorization: token " + ALNUM, "authorization:" + ALNUM],
    "credential-assignment": ["GH_" + "TOKEN=" + ALNUM, "export DB_PASS" + "WORD='" + ALNUM + "'",
                              "my_api_" + "key = " + ALNUM, "CLIENT_SEC" + "RET=\"x\""],
    "url-credentials": ["https://user:" + ALNUM + "@example.org/x", "https://" + ALNUM * 2 + "@github.com/a/b"],
    "email-address": ["mail someone" + "@example.com now"],
}

CLEAN = [
    "python3 tools/gh/l1_post.py --repo vitalharmony/harmonic-forge --issue 1288",
    'GH_TOKEN="$TOKEN" gh api repos/a/b',
    "GH_TOKEN=$(gh auth token) gh pr view 1",
    "export API_KEY=${API_KEY}",
    "curl -H 'Authorization: Bearer $TOKEN' https://x",
    'curl -H "Authorization: token ${GH}" https://x',
    "https://user:$PASSWORD@example.org/x",
    "git clone git@github.com:vitalharmony/harmonic-forge.git",
    "Co-Authored-By: Claude <noreply@anthropic.com>",
    "12345+someone@users.noreply.github.com",
    "no-reply@example.com",
    "the token was stale; re-read it",
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
        known = {kind for kind, _p in s.PATTERNS} | {"email-address"}
        for values in HITS.values():
            for value in values:
                for kind in s.findings(value):
                    self.assertIn(kind, known)
                    self.assertNotIn(kind, value)

    def test_each_kind_reported_once(self):
        token = "gh" + "p_" + ALNUM * 3
        self.assertEqual(s.findings(f"{token} and {token}"), ["github-token"])

    def test_the_source_reaches_no_network(self):
        source = Path(s.__file__).read_text(encoding="utf-8")
        for forbidden in ("requests", "urllib", "httpx", "socket", "subprocess"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
