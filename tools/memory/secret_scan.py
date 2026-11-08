#!/usr/bin/env python3
"""Pattern scan for secret-shaped text (harmonic-forge#1288).

`capture_failure.py` runs `--command` and `--probe` through `findings()` before
it writes anything. A hit refuses the whole capture rather than redacting: a
redaction that misses part of a secret persists the rest.

`findings()` returns kind names only, never the matched text, so a refusal can
say what it found without repeating it into a terminal or a transcript.

Local and pattern-only: no network, no external scanner. The scan errs toward
refusing. A value that is a shell expansion (`$VAR`, `${VAR}`, `$(...)`) is not
a literal secret, so assignments, Authorization headers and URL passwords whose
value starts with `$` pass. Noreply addresses (`noreply@…`, `no-reply@…`,
`…@users.noreply.github.com`) and the scp-style `git@host:` remote form are not
treated as email addresses.
"""
from __future__ import annotations

import re

_LITERAL = r"""(?!\$)[^\s'"]+"""
_SCHEME = r"(?:bearer|basic|token|digest)"

#: `(kind, pattern)`, in report order.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}|hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("authorization-header", re.compile(
        r"(?i)\bauthorization\s*:\s*(?:" + _SCHEME + r"\s+" + _LITERAL
        + r"|(?!" + _SCHEME + r"\b)" + _LITERAL + ")")),
    ("credential-assignment", re.compile(
        r"(?i)[A-Za-z0-9_-]*(?:token|secret|password|passwd|api[_-]?key)[A-Za-z0-9_-]*"
        r"\s*=\s*['\"]?" + _LITERAL)),
    ("url-credentials", re.compile(
        r"[A-Za-z][A-Za-z0-9+.-]*://(?:[^\s/@:]*:(?!\$)[^\s/@]+|[A-Za-z0-9_-]{20,})@")),
)

_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")


def _is_exempt_email(match: re.Match[str], text: str) -> bool:
    local, domain = match.group(1).lower(), match.group(2).lower()
    if local in ("noreply", "no-reply") or domain.endswith("noreply.github.com"):
        return True
    # `$PASSWORD@host` is an expansion in URL userinfo, not an address.
    if text[max(0, match.start() - 1):match.start()] == "$":
        return True
    # `git@github.com:org/repo` is a remote, not an address.
    return local == "git" and text[match.end():match.end() + 1] == ":"


def findings(text: str | None) -> list[str]:
    """The kinds of secret-shaped value in `text`, in report order, each once.

    Never the matched text itself."""
    if not text:
        return []
    kinds = [kind for kind, pattern in PATTERNS if pattern.search(text)]
    for match in _EMAIL.finditer(text):
        if not _is_exempt_email(match, text):
            kinds.append("email-address")
            break
    return kinds
