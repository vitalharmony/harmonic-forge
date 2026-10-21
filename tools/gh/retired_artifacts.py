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

_GEMINI_CLI_RETIRED = (
    "Gemini CLI retired (harmonic-forge#1113) -- it cannot authenticate by "
    "OAuth or API key; lanes and cross-family calls are Claude Code and Codex only"
)

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
    "L2P": "retired lane token (harmonic-forge#583) -- Lane 2 posts `L2S` (spec/plan "
           "done, ready for Lane 1 review) or `L2D` (done, implementation posted). The "
           "emitter was fixed; historical issue comments still carry it, so the belt "
           "marks rather than reproduces it (harmonic-forge#609)",
    ".devin/hooks.v1.json": "retired with the Devin scrub (harmonic-forge#317)",
    "scripts/gate_devin_exec.py": "retired with the Devin scrub (harmonic-forge#317)",
    # harmonic-forge#1113: the Gemini CLI as a lane agent and cross-family
    # family. Command-shaped and full-path keys only (never the bare word),
    # so HRSE2's Gemini model API is never flagged.
    # A key ending in "/" is a directory key: it also flags any span that
    # starts with it, so every file the deletion removed is covered.
    "tools/gemini/": _GEMINI_CLI_RETIRED,
    "tools/gemini/install-lane3-context-extension": _GEMINI_CLI_RETIRED,
    "tools/gemini/lane3-context/lane3_context_mcp.py": _GEMINI_CLI_RETIRED,
    "GEMINI.md": _GEMINI_CLI_RETIRED,
    "tools/lane/gemini-read-only-deny.toml": _GEMINI_CLI_RETIRED,
    "tools/lane/gemini-probe-deny.toml": _GEMINI_CLI_RETIRED,
    "tools/lane/policies/gemini-lane1.toml": _GEMINI_CLI_RETIRED,
    "tools/lane/policies/gemini-lane2.toml": _GEMINI_CLI_RETIRED,
    "tools/lane/policies/gemini-lane3.toml": _GEMINI_CLI_RETIRED,
    "tools/lane/policies/canary/run_canary.py": _GEMINI_CLI_RETIRED,
    "tools/lane/test_lane3_context_mcp.py": _GEMINI_CLI_RETIRED,
    "prompts/gemini-gem-harmonic-architect-co-pilot.md": _GEMINI_CLI_RETIRED,
    "prompts/gemini-gem-harmonic-forge.md": _GEMINI_CLI_RETIRED,
}

# Command-fragment keys (harmonic-forge#1113): matched as a contiguous run of
# whitespace-separated tokens anywhere inside a backtick span, so
# `lane3 --agent gemini` is flagged. A small explicit set -- never the bare
# word "gemini", which would flag HRSE2's Gemini model API.
RETIRED_COMMAND_FRAGMENTS: dict[str, str] = {
    "--agent gemini": _GEMINI_CLI_RETIRED,
    "--caller gemini": _GEMINI_CLI_RETIRED,
    "--families 3": _GEMINI_CLI_RETIRED,
    "LANE_CLI=gemini": _GEMINI_CLI_RETIRED,
}
