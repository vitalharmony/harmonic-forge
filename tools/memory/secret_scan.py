#!/usr/bin/env python3
"""Pattern scan for secret-shaped text (harmonic-forge#1288).

`capture_failure.py` runs `--command` and `--probe` through `findings()` before
it writes anything. A hit refuses the whole capture rather than redacting: a
redaction that misses part of a secret persists the rest.

`findings()` returns kind names only, never the matched text, so a refusal can
say what it found without repeating it into a terminal or a transcript.

Local and pattern-only: no network, no external scanner. The scan errs toward
refusing, because a refusal costs one re-run without the value.

Posture (the sticky-wicket PATCH verdict on the pass-1 cluster): a credential
keyword in any key position refuses, whatever the separator or quoting. The one
exemption is a value that is, as a whole, a single shell expansion (`$NAME`
with an upper-case NAME, `${name}`, `$(...)`), unquoted or double-quoted.
Single-quoted and `$'...'` values are never exempt, because the shell does not
expand them. `KINDS` is the closed list; the AC5 shape table on the issue
describes each kind's forms, exemptions and what is out of scope.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator

Span = tuple[int, int]

_KEYWORD = (r"(?:token|secret|password|passwd|pwd|api[_-]?key|auth[_-]?key|private[_-]?key"
            r"|client[_-]?secret|authorization)")
_SCHEMES = ("bearer", "basic", "token", "digest")
_SCHEME_OPT = r"(?:(?:bearer|basic|token|digest)\s+)?"
#: One value: `$'...'`, `'...'`, `"..."`, `$(...)`, `${...}`, or an unquoted run.
_VALUE = r"""(?P<value>\$'[^']*'?|'[^']*'?|"[^"]*"?|\$\([^)]*\)?|\$\{[^}]*\}?|[^\s'"`,;}\])@]+)"""

#: The whole-value expansions that are exempt. A bare `$NAME` must be upper
#: case: `$ecretPw` is far more likely a literal password than a variable.
_EXPANSION = re.compile(r"\$[A-Z_][A-Z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$\([^)]*\)")

#: A key: the keyword inside an identifier, optionally quoted, optionally
#: closing a subscript, then `=`, `:`, `:=` or `=>`. The identifier must start
#: at a boundary and not with `-`, so `--flag` forms are left to `_FLAG`.
_ASSIGN = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?P<q>['\"]?)(?P<key>(?:[A-Za-z0-9_][A-Za-z0-9_.-]*?)?" + _KEYWORD
    + r"[A-Za-z0-9_.-]*)(?P=q)\]?\s*(?:=>|:=|=(?!=)|:(?!:))\s*" + _SCHEME_OPT + _VALUE)

#: `--<flag>` whose last segment is the keyword, then `=` or whitespace and a
#: value that does not start with `-`. `--token-file path` is not a hit.
_FLAG = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])--(?P<key>(?:[A-Za-z0-9]+[-_])*" + _KEYWORD
    + r")(?![A-Za-z0-9_-])(?:=|\s+)(?!-)" + _SCHEME_OPT + _VALUE)

#: curl/basic auth: `-u` or `--user`, then `user:pass`.
_BASIC = re.compile(r"(?<![A-Za-z0-9_-])(?:-u|--user)(?![A-Za-z0-9_-])(?:=|\s*)" + _VALUE)

_URL = re.compile(r"\b(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<ui>[^\s/@'\"]+)@")
_SSH_SCHEMES = {"ssh", "git+ssh", "ssh+git"}

_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")

_RUN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")

#: Fixed-shape kinds: `(kind, pattern)`.
_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}|hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    # Anchored on the header alone: an empty-claims payload (`e30`) still hits.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")),
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
)

#: Every kind `findings()` can report, in report order.
KINDS: tuple[str, ...] = (*(kind for kind, _p in _SHAPES), "authorization-header",
                          "credential-assignment", "basic-auth", "url-credentials",
                          "email-address", "opaque-token")


def _unquote(raw: str) -> tuple[str, str]:
    """`(quoting, inner)`; quoting is `single`, `ansi`, `double` or `none`."""
    if raw.startswith("$'"):
        return "ansi", raw[2:-1] if raw.endswith("'") and len(raw) > 2 else raw[2:]
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        inner = raw[1:-1] if raw.endswith(quote) and len(raw) > 1 else raw[1:]
        return ("single" if quote == "'" else "double"), inner
    return "none", raw


def is_exempt(value: str, quoting: str) -> bool:
    """True only for a whole value that is one shell expansion, unquoted or
    double-quoted. The one exemption every keyword-anchored kind uses."""
    return quoting in ("none", "double") and bool(_EXPANSION.fullmatch(value))


def _literal_value(raw: str) -> bool:
    """Whether a captured value is a non-empty, non-exempt literal."""
    quoting, inner = _unquote(raw)
    if quoting in ("single", "double"):
        inner = re.sub(r"(?i)^(?:bearer|basic|token|digest)\s+", "", inner)
    if not inner.strip() or inner.lower() in _SCHEMES:
        return False
    return not is_exempt(inner, quoting)


def _keyword_hits(text: str) -> Iterator[tuple[str, Span]]:
    for pattern in (_ASSIGN, _FLAG):
        for match in pattern.finditer(text):
            if _literal_value(match.group("value")):
                kind = ("authorization-header" if "authorization" in match.group("key").lower()
                        else "credential-assignment")
                yield kind, match.span("value")


def _basic_hits(text: str) -> Iterator[tuple[str, Span]]:
    for match in _BASIC.finditer(text):
        quoting, inner = _unquote(match.group("value"))
        _user, colon, password = inner.partition(":")
        if colon and password and not is_exempt(password, quoting):
            yield "basic-auth", match.span("value")


def _url_hits(text: str) -> Iterator[tuple[str, Span]]:
    for match in _URL.finditer(text):
        user, colon, password = match.group("ui").partition(":")
        if colon:
            exempt = is_exempt(password, "none")
        else:
            exempt = (user == "git" and match.group("scheme").lower() in _SSH_SCHEMES) \
                or is_exempt(user, "none")
        if not exempt:
            yield "url-credentials", match.span("ui")


def _is_exempt_email(match: re.Match[str], text: str) -> bool:
    local, domain = match.group(1).lower(), match.group(2).lower()
    if local in ("noreply", "no-reply"):
        return True
    if domain == "users.noreply.github.com" or domain.endswith(".users.noreply.github.com"):
        return True
    # `$PASSWORD@host` is an expansion in URL userinfo, not an address; the URL
    # kind judges that userinfo on its own.
    if text[max(0, match.start() - 1):match.start()] == "$":
        return True
    # `scheme://user@host` is URL userinfo, which the URL kind judges.
    if text[max(0, match.start() - 3):match.start()] == "://":
        return True
    # `git@github.com:org/repo` is a remote, not an address.
    return local == "git" and text[match.end():match.end() + 1] == ":"


def _email_hits(text: str) -> Iterator[tuple[str, Span]]:
    for match in _EMAIL.finditer(text):
        if not _is_exempt_email(match, text):
            yield "email-address", match.span()


def _is_opaque(run: str) -> bool:
    return (any(c.isupper() for c in run) and any(c.islower() for c in run)
            and any(c.isdigit() for c in run) and not re.fullmatch(r"[0-9a-f]+", run)
            and not _UUID.fullmatch(run))


def findings(text: str | None) -> list[str]:
    """The kinds of secret-shaped value in `text`, in report order, each once.

    Never the matched text itself."""
    if not text:
        return []
    hits: list[tuple[str, Span]] = []
    for kind, pattern in _SHAPES:
        hits.extend((kind, m.span()) for m in pattern.finditer(text))
    finders: tuple[Callable[[str], Iterator[tuple[str, Span]]], ...] = (
        _keyword_hits, _basic_hits, _url_hits, _email_hits)
    for finder in finders:
        hits.extend(finder(text))
    # The backstop: a long mixed-case alphanumeric run no other kind explains.
    for match in _RUN.finditer(text):
        start, end = match.span()
        if _is_opaque(match.group()) and not any(s < end and start < e for _k, (s, e) in hits):
            hits.append(("opaque-token", match.span()))
    found = {kind for kind, _span in hits}
    return [kind for kind in KINDS if kind in found]
