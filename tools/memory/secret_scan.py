#!/usr/bin/env python3
"""The `--probe` check: a blunt scan for secret-shaped text (harmonic-forge#1288).

`capture_failure.py` runs `--probe` through `probe_findings()` before it writes
anything. A hit refuses the whole capture rather than redacting: a redaction
that misses part of a secret persists the rest. The command that failed is not
stored at all (operator ruling, 2026-11-08); it is recovered from the session
transcript through the record's `(session, turn)`.

The scan has no shell awareness and no exemptions (the sticky-wicket REFORGE
verdict). A probe is a short description, not a command, so nothing in it has
to be judged safe:

- one line, at most `MAX_PROBE` characters;
- no credential keyword (`KEYWORDS`) anywhere, case-insensitive, as a plain
  substring with no separator, quoting or position logic;
- no `@` and no `://`;
- no fixed vendor token shape, JWT header or PEM private-key header;
- no opaque token (the backstop: a 32+ character mixed-case run of base64
  characters, standard or URL-safe, with a digit, that is not hex or a UUID).

A secret with no keyword, no `@` or `://`, under 32 characters and no vendor
shape (`pw=hunter2`, `mysql -phunter2`) is out of scope: the irreducible
residual of any pattern scan, accepted under the good-faith threat model and
local storage with no remote.

Every function returns kind names only, never the matched text, so a refusal
can say what it found without repeating it into a terminal or a transcript.
Local and pattern-only: no network, no external scanner.
"""
from __future__ import annotations

import re

#: The longest probe accepted, in characters.
MAX_PROBE = 200

#: Credential keywords, matched as case-insensitive substrings anywhere.
KEYWORDS: tuple[str, ...] = ("token", "secret", "password", "passwd", "pwd", "api_key",
                             "api-key", "apikey", "auth_key", "auth-key", "authkey",
                             "private_key", "private-key", "privatekey", "authorization")

#: Standard and URL-safe base64 both: `/` and `+` never split a run, and
#: trailing `=` padding belongs to it.
_RUN = re.compile(r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/-]{32,}=*(?![A-Za-z0-9_+/=-])")
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")

#: Fixed-shape kinds: `(kind, pattern)`. Unanchored: a shape embedded after or
#: before a word character (`idAKIA…`, `xyzxoxb-…`) still hits.
_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-token", re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"sk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("slack-token", re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}|hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    # Anchored on the header alone: an empty-claims payload (`e30`) still hits.
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")),
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
)

#: Every content kind `findings()` can report, in report order.
KINDS: tuple[str, ...] = ("credential-keyword", "at-sign", "url-scheme",
                          *(kind for kind, _p in _SHAPES), "opaque-token")

#: The form kinds `probe_findings()` adds: more than one line, too long.
FORM_KINDS: tuple[str, ...] = ("multi-line", "over-length")


def _is_opaque(run: str) -> bool:
    return (any(c.isupper() for c in run) and any(c.islower() for c in run)
            and any(c.isdigit() for c in run) and not re.fullmatch(r"[0-9a-f]+", run)
            and not _UUID.fullmatch(run))


def findings(text: str | None) -> list[str]:
    """The content kinds in `text`, in report order, each once. Never the
    matched text itself."""
    if not text:
        return []
    lowered = text.lower()
    found = set()
    if any(keyword in lowered for keyword in KEYWORDS):
        found.add("credential-keyword")
    if "@" in text:
        found.add("at-sign")
    if "://" in text:
        found.add("url-scheme")
    found.update(kind for kind, pattern in _SHAPES if pattern.search(text))
    if any(_is_opaque(m.group()) for m in _RUN.finditer(text)):
        found.add("opaque-token")
    return [kind for kind in KINDS if kind in found]


def probe_findings(text: str | None) -> list[str]:
    """Every reason `text` is refused as a `--probe`: the form kinds (more
    than one line, longer than `MAX_PROBE`) and then the content kinds."""
    if not text:
        return []
    return form_findings(text) + findings(text)


def form_findings(text: str | None) -> list[str]:
    """The form kinds alone (more than one line, longer than `MAX_PROBE`).
    Checked on the raw value, so a blank probe that will be stored as null is
    still refused when it is too long or not one line."""
    if not text:
        return []
    form = []
    if text.splitlines() != [text]:
        form.append("multi-line")
    if len(text) > MAX_PROBE:
        form.append("over-length")
    return form
