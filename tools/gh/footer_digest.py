"""One check of an `l1-post` footer's own `body-sha256` (harmonic-forge#1358).

`l1_post` and `post_lane_discussion` hash the rstripped body (plus the
footer's `prod-run=` token, harmonic-forge#878) and append the footer last.
So a footer belongs to the text above it exactly when its own digest matches
that text. Shared by `watch_lane_posts._own_marker` (which footer is the
comment's own) and `check_lane3_ready.verify_body_sha256` (was the comment
edited), so the digest contract has one implementation.
"""
from __future__ import annotations

import hashlib
import re

import _prod_run

_BODY_SHA = re.compile(r"\bbody-sha256=([0-9a-f]{64})\b", re.I)


def has_digest(marker: str) -> bool:
    """Does `marker` carry a `body-sha256` at all?"""
    return _BODY_SHA.search(marker) is not None


def marker_verifies(body: str, start: int, marker: str) -> bool:
    """Does `marker`, found at `start` in `body`, carry a digest of the text
    before it? True when the marker carries no `body-sha256`: absence is not a
    mismatch (`discussion` footers carry none)."""
    recorded = _BODY_SHA.search(marker)
    if recorded is None:
        return True
    inner = marker.split(";", 1)[1].rsplit("-->", 1)[0] if ";" in marker else ""
    field = _prod_run._FIELD.search(inner)
    token = f"prod-run={field.group(1)}" if field else None
    covered = _prod_run.covered_text(body[:start], token)
    return hashlib.sha256(covered.encode()).hexdigest() == recorded.group(1).lower()
