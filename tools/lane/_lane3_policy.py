"""Whether this process can trust the installed Lane 3 claude policy (harmonic-forge#1156).

`_cli_launch.sh` refuses to launch Lane 3 claude unless the registry's policy
file is owned by the required uid (0) as this process sees it
(harmonic-forge#878). Inside the te-runner's `bwrap` sandbox a user namespace
maps root to 65534, so the root-owned file reads as `nobody` and every launch
is refused. Tests that need a launched Lane 3 claude session skip there, with
the reason named; on the host they run unchanged. The trust check itself is
never touched, and an absent policy is trusted, as the launcher launches
without one.
"""
from __future__ import annotations

import functools
import os
import re
import unittest
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent / "_agent_registry.sh"
_PATH_RE = re.compile(r'\[claude:3\]="([^"]*)"')
_OWNER_RE = re.compile(r'AGENT_POLICY_REQUIRED_OWNER=\(\s*\[claude\]="([^"]*)"', re.S)


def _registry() -> tuple[str, int]:
    text = REGISTRY.read_text(encoding="utf-8")
    path = _PATH_RE.search(text)
    owner = _OWNER_RE.search(text)
    return (path.group(1) if path else "", int(owner.group(1)) if owner and owner.group(1) else 0)


def overflow_uid() -> int:
    """The uid a user namespace shows for an unmapped owner (root, here)."""
    try:
        return int(Path("/proc/sys/kernel/overflowuid").read_text().strip())
    except (OSError, ValueError):
        return 65534


def in_user_namespace() -> bool:
    """True when this process's uid map is not the host identity map
    (`0 0 4294967295`): a user namespace, where an unmapped root reads as the
    overflow uid. An unreadable map is no evidence of one."""
    try:
        rows = Path("/proc/self/uid_map").read_text().split("\n")
    except OSError:
        return False
    return [r.split() for r in rows if r.strip()] != [["0", "0", "4294967295"]]


def lane3_policy_untrusted(lstat=os.lstat, overflow=None, userns=None) -> str | None:
    """The reason a Lane 3 claude launch cannot pass the trust check here, or
    None. Only a user-namespace remap skips: this process is in a user
    namespace AND the owner reads as the overflow uid. Any other ownership
    drift or read error is a real defect, so the tests run and fail loudly
    (preclose passes 1 and 2, sticky-wicket PATCH)."""
    path, owner = _registry()
    if not path:
        return None
    try:
        uid = lstat(path).st_uid
    except FileNotFoundError:
        return None
    except OSError:
        return None  # not a remap: the tests run and fail loudly (sticky-wicket PATCH)
    in_userns = in_user_namespace() if userns is None else userns
    if in_userns and uid != owner and uid == (overflow_uid() if overflow is None else overflow):
        return (f"Lane 3 policy {path} reads as uid {uid} here, not {owner} (a user "
                "namespace?); the launch trust check (harmonic-forge#878) cannot pass")
    return None


def needs_trusted_lane3_policy(test):
    """Skip one test, naming why, where a Lane 3 claude launch is refused."""
    @functools.wraps(test)
    def wrapper(self, *args, **kwargs):
        reason = lane3_policy_untrusted()
        if reason:
            raise unittest.SkipTest(reason)
        return test(self, *args, **kwargs)
    return wrapper
