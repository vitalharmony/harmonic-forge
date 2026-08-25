"""Single canonical list of retired artifacts (harmonic-forge#379).

Backtick-quoted references to these names in a new issue body are almost
always a symptom of the same recurring pattern: someone quotes a script,
file, or field that already existed at filing time but no longer does,
and the issue reads as broken the next time anyone acts on it (hrse#760).

This is the one machine-readable home for that list. `gh_issue.py`'s
write-time warning imports it directly; CLAUDE.md's own prose (the
"Utility / Migration Scripts", "Issue Tracking", and versioning sections)
should point here rather than re-deriving or duplicating the list — a
second copy is the exact dual-source-of-truth failure hrse#839 was filed
for.

Keys are matched whole-string against a backtick-quoted span in an issue
body (see `find_retired_citations()` in `gh_issue.py`) -- never a bare
word, since the audit that produced this list found bare-word matching
responsible for its worst false positives (flagging "Estimate" in every
issue that correctly states one, flagging "Devin" in the issue whose
whole point was scrubbing Devin references).
"""

RETIRED_ARTIFACTS: dict[str, str] = {
    "board_sync.py": "retired (hrse#839) -- docs/PRIORITIES.md no longer drives the boards; board fields are edited on the board directly",
    "board_drift_check.py": "retired alongside board_sync.py (hrse#839) -- its Priority-field read has no input now that Priority is retired",
    "hrse_manager.py": "retired -- replaced by mise tasks (`mise run restart` etc., ADR-001)",
    "mini-audit.md": "retired 2026-07-10 -- pre-3-lane-protocol artifact; superseded by the standing-rule-violations-get-filed rule in universal-agent.md",
    "--backlog-sweep": "retired -- no replacement; backlog.md is a read-only historical record",
    "backlog.md": "read-only historical record -- new bugs/features are tracked as GitHub Issues",
    "backlog_archive.md": "read-only historical record",
    "Estimate": "retired board field (hrse#966) -- Tier is the sole model-routing signal now",
    "Priority": "retired board field (hrse#839) -- Status (Todo/In Progress/Done) carries the Kanban",
    "WorkEntry": "retired node label (BACKLOG-001/002, v2.3.85) -- work history is HAS_EXPERIENCE -> Experience",
    "HAS_WORK_ENTRY": "retired edge type (BACKLOG-001/002, v2.3.85) -- replaced by HAS_EXPERIENCE",
    ".devin/hooks.v1.json": "retired with the Devin scrub (harmonic-forge#317)",
    "scripts/gate_devin_exec.py": "retired with the Devin scrub (harmonic-forge#317)",
}
