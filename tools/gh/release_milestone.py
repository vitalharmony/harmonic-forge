"""The one definition of a repo's current release milestone (harmonic-forge#1382
cluster verdict C1).

R-0404 in `l1_post`, the parking-milestone test in `gh_issue` and the belt's
`@current-release` filter in `watch_lane_posts` all call `current_release`, so
the filing gate, the handoff gate and the lane queue can never disagree about
which milestone is current. Pure: no I/O.
"""
from __future__ import annotations

import re

#: A release title is a bare dotted number (`0.3`, `3.10`, `3.2.1`). `v3.0`,
#: `3.0-rc` and `3.0 hotfix` are never a release (harmonic-forge#1033).
RELEASE_TITLE = re.compile(r"^\d+(?:\.\d+)*$")


def is_release_title(title: str) -> bool:
    return bool(RELEASE_TITLE.match(title or ""))


def current_release(titles: list[str]) -> str | None:
    """The open milestone with the lowest version-shaped title, compared
    numerically (`3.9` < `3.10`, `3.2.1` < `3.3`); None when no open title is
    version-shaped. Turnover is closing the milestone."""
    releases = [t for t in titles if is_release_title(t)]
    if not releases:
        return None
    return min(releases, key=lambda t: tuple(int(part) for part in t.split(".")))
