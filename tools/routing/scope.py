"""The file scope a filing body declares (harmonic-forge#1232, semantic routing A2).

At filing time the only scope there is is the backticked spans in the issue
body. `analyze` visits **every** backticked span, never a list of accepted
shapes (sticky-wicket on harmonic-forge#1232: a pattern that lists shapes drops
whatever it did not list, silently):

- a span is split on whitespace and each token is normalized: a `:42` or
  `:42-60` suffix and a `#L5` anchor are stripped;
- a token that looks like a path (contains `/`, starts with `.`, or is a bare
  file name with an extension) and is made only of path characters is a path;
- a path-like token with anything else in it (`src/*.py`, `$HOME/x`) is
  **unparsed**, and `_route_at_filing` then files the issue at Path 1, the
  strictest route, so no spelling can ever lower the route.

Plain words (`mise`, `run`) are neither. `scope_for_filing` turns the paths
into A1's `Scope`: one repo half (the issue's), the filing's labels, no threat
model (a handoff declares that later), never runner-eligible.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gates import Scope  # noqa: E402

#: a span may wrap a line and has no length cap: a cap is another place a path can hide
_SPAN = re.compile(r"`([^`]+)`")
_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?|#L\d+(?:-L?\d+)?)$")
_SAFE = re.compile(r"^[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]*)*$")


_EXTENSION = re.compile(r"\.[A-Za-z0-9]+$")


def _pathlike(token: str) -> bool:
    """Anything that could name a file: a slash, a leading dot, or a trailing
    extension. Deliberately wider than `_SAFE`: a token that looks like a path
    but is not clean is reported as unparsed, never ignored."""
    return "/" in token or token.startswith(".") or bool(_EXTENSION.search(token))


def analyze(body: str) -> tuple[list[str], list[str]]:
    """(paths in order once each, unparsed path-like tokens in order once each)."""
    paths: list[str] = []
    unparsed: list[str] = []
    for span in _SPAN.finditer(body or ""):
        for raw in span.group(1).split():
            token = _SUFFIX.sub("", raw.strip(",;")).strip()
            if not token or not _pathlike(token):
                continue
            bucket = paths if _SAFE.match(token) else unparsed
            if token not in bucket:
                bucket.append(token)
    return paths, unparsed


def paths_from_body(body: str) -> list[str]:
    """Backticked file or directory paths in `body`, in order, once each."""
    return analyze(body)[0]


def unparsed_spans(body: str) -> list[str]:
    """Path-like backticked tokens that could not be read as a clean path."""
    return analyze(body)[1]


def scope_for_filing(repo: str, body: str, labels: list[str]) -> Scope:
    paths = paths_from_body(body)
    return Scope(repo=repo, halves={repo: paths} if paths else {}, labels=list(labels or []),
                 threat_model=None, runner_eligible=False)
