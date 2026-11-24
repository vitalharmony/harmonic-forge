"""The exact `owner/repo` a lane checkout's origin names (harmonic-forge#1507).

`_gh_config_dir.sh` chooses which gh credential a lane session inherits from
this key, so it accepts only shapes whose host is unambiguously github.com and
returns None for anything else -- the launcher then refuses:

- `https://github.com/<owner>/<repo>[.git]`
- `git@github.com:<owner>/<repo>[.git]`
- `ssh://git@github.com/<owner>/<repo>[.git]`
- an absolute local path ending `/github.com/<owner>/<repo>.git` (the offline
  bare repos the launcher tests use; a path has no host to impersonate).

`.git` is stripped once, and a key that `normalize_repo` would change again
(a second `.git`, `.`/`..`, case) is refused rather than looked up under a
different repo's entry (preclose pass 1).
"""
from __future__ import annotations

import re

from manifest_protocol import ManifestError, normalize_repo

_PART = r"[A-Za-z0-9_.-]+"
_SHAPES = (
    re.compile(rf"https://github\.com/({_PART})/({_PART})/?"),
    re.compile(rf"git@github\.com:({_PART})/({_PART})/?"),
    re.compile(rf"ssh://git@github\.com/({_PART})/({_PART})/?"),
    re.compile(rf"/(?:[^\0]*/)?github\.com/({_PART})/({_PART}\.git)"),
)


def parse_origin(url: str) -> str | None:
    """`owner/repo`, lowercased, for an accepted origin shape; else None."""
    for shape in _SHAPES:
        match = shape.fullmatch(url)
        if match is None:
            continue
        owner, repo = match.groups()
        if repo.endswith(".git"):
            repo = repo[: -len(".git")]
        key = f"{owner}/{repo}"
        try:
            if normalize_repo(key) != key.lower():
                return None
        except ManifestError:
            return None
        return key.lower()
    return None
