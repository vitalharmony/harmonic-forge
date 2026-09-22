"""Shared write-tier parsing for a Lane 1 sweep body (hrse#1549).

Extracted so `l1_post.py` (which posts a sweep) and `check_lane3_ready.py`
(which gates on one) can never silently diverge on what counts as a valid
tier line -- both import from here rather than each carrying its own copy
of the regex and the error text.
"""
import re

# hrse#1359: tier R is read-only (least dangerous, no AE needed if the sweep
# itself carries the HITL go-ahead); P is the production-write tier (most
# dangerous, always needs an AE). "Ceiling" resolution (below) takes the
# most permissive -- i.e. highest-ranked -- tier mentioned anywhere in the
# sweep, not the first match, so a stray higher-tier mention later in the
# body can never be silently missed.
TIER_RANK = {"R": 0, "W": 1, "P": 2}
WRITE_TIER_RE = re.compile(r"write\s*tier[:\s]+([RWP])\b", re.I)

#: hrse#1549: bitten three times in one session (hrse#1530 omitted the tier
#: line entirely; hrse#1531 and hrse#1437 both wrote descriptive prose like
#: "Write tier: mixed, per-TC as stated in the spec" -- matches no [RWP]
#: letter, so the parser finds nothing and this is indistinguishable from no
#: tier line at all). A memory note existed after the first two and was not
#: consulted before the third. The fix is structural: reject a malformed
#: sweep at post time, not later at the gate.
NO_TIER_MESSAGE = (
    'does not declare a write tier (e.g. "Write tier R throughout") -- '
    "silence is never read as tier R; state the tier explicitly"
)


def parse_write_tier(body: str) -> str | None:
    """hrse#1359 AC5: the ceiling (most permissive tier mentioned), not the
    first match -- a sweep declaring "R" near the top with a stray "write
    tier P" mention later in a TC line must resolve to P, not R. `findall`
    scans every mention; `max` by rank picks the most permissive."""
    matches = WRITE_TIER_RE.findall(body)
    if not matches:
        return None
    return max((m.upper() for m in matches), key=TIER_RANK.__getitem__)
