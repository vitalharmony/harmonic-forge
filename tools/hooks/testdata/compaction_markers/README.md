# Frozen compaction markers (harmonic-forge#480)

A **point-in-time capture**, not a live read. These are the five markers that
existed under `<tmpdir>/harmonic-forge-compaction-gate/` on 2026-09-06, copied
here because that directory is on a clock: it lives in `/tmp`, a TTL prunes
it, and a marker is rewritten whenever the same session compacts again.
`a58a3627`'s marker had already been overwritten once between the filing of
harmonic-forge#480 and its planning pass, which is what made a reference-in-
place unusable and this directory necessary.

`<session>.json` is the marker verbatim.

`<session>.tool_calls.json` is what a marker alone cannot supply: the
post-boundary `tool_use` records that mention a corpus path, extracted from
each session's transcript. The transcripts are 21-80 MB each and cannot be
vendored; these are 64 KB for all five. A marker records that a compaction
happened, never what the session did next, so a detector over tool calls has
nothing to run against without them.

Only `ab5817ae` has records -- six of them, and they are the most valuable
rows here because three are the false-positive shapes the detector had to
learn to reject:

| action | shape | detected as a reload? |
|---|---|---|
| 2 | `wc -l` on four corpus files | no -- counts, returns no content |
| 3 | `sed -n '1,240p' rules/universal-lane1.md` | **yes** |
| 4 | `cat rules/universal-claude.md` | **yes** |
| 20 | `python3 - <<PY` heredoc naming a corpus path | no -- a write |
| 29 | `cat >> tests.py <<EOF` naming a corpus path | no -- `cat` as a writer |
| 38 | `python3 - <<PY` heredoc naming a corpus path | no -- a write |

`ab5817ae` is the session that planned harmonic-forge#480, which is why its
reload is verifiable against a transcript rather than inferred.
