## Handoff: [Issue H2614 — Restore the product logo hrse#18 replaced and remove the watermark]

**Scope:** restore the three logo SVGs byte for byte from `dd3dfb584^`, remove the watermark and the content column added for it, keep H18's unrelated work, and refresh the visual baselines.
**Next:** Implement H2614
**Threat model:** good-faith

### Issue
- GitHub: https://github.com/vitalharmony/hrse/issues/2614
- Labels: bug, ui
- Acceptance criteria: the issue body.

### Lane 3 Gate Variant
UI golden path (`rules/frontend-ui-golden-path.md`) with HRSE2's lane3-gate skill.

### Affected Files
| File | Lines | Change type |
|---|---|---|
| frontend/public/favicon.svg | whole file | Restore from `dd3dfb584^` (sha256 begins `723c3b6ad7e7ddbd`) |
| frontend/src/assets/cymagraph-small-dark.svg | whole file | Restore from `dd3dfb584^` (sha256 begins `98c62205b09ad90d`) |
| frontend/src/assets/cymagraph-small-light.svg | whole file | Restore from `dd3dfb584^` (sha256 begins `d08212396fca4dbb`) |
| frontend/src/assets/cymagraph-watermark.svg | whole file | Delete |
| frontend/src/components/ResonanceWatermark.tsx | whole file | Delete |
| frontend/src/components/ResonanceWatermark.test.tsx | whole file | Delete |
| scripts/generate_chladni_mark.py | whole file | Delete |
| frontend/src/AppShell.tsx | 25, 217–220, 249–254, 272 | Modify: remove the import, the mount and both hrse#18 comments; put `bg-background` back on the root and `<main>`, drop `relative` from the root, and remove the inner column's `<div>` and its closing tag |
| frontend/src/index.css | 65–66 | Modify: remove the watermark comment and `--watermark-opacity` only; `--scrim` and `--color-scrim` stay |
| frontend/e2e/visual/screens.spec.ts-snapshots/ | PNGs | Refresh from a CI render |

### Root Cause / Entry Point
> "| frontend/public/favicon.svg | whole file | Replace — generated mark, cool slate only |" (Lane 1's H18 handoff, 6433222943)

The defect is in the handoff, not in Lane 2's work. It carried a replacement forward from a stale backlog spec without asking the operator whether to retire the logo.

### Scenario Trace
The operator opens the app and sees the discarded mark in the sidebar, the browser tab and behind the screen.
1. Sidebar: `frontend/src/components/Sidebar.tsx:7` imports `../assets/cymagraph-small-dark.svg`, rendered at `:154`. verified-live (`grep -n`, this session).
2. Tab: `frontend/index.html:11` links `/favicon.svg`. verified-live (`git grep -n`, this session).
3. Both files last changed in `dd3dfb584`. verified-live (`git log -- <path>`, this session).
4. Watermark: `frontend/src/AppShell.tsx:25` imports it and `:220` mounts it; `frontend/src/components/ResonanceWatermark.tsx:10` loads `cymagraph-watermark.svg`. verified-live (`grep -n`, this session).
5. The prior art: `git show dd3dfb584^:frontend/src/assets/cymagraph-small-dark.svg`, rendered with `rsvg-convert`, is the orange-center mark, and the operator confirmed on 2026-11-05 that it is his logo. verified-live.

### Design Alternatives Considered
none. The operator ruled both points: restore this exact logo, and remove the watermark rather than redraw it.

### Load-Bearing Assumptions
- `frontend/src/index.css:65-66` and `frontend/src/AppShell.tsx` are the only code references left once the deleted files go. **verified-live** (`git grep -n -E 'watermark-opacity|ResonanceWatermark|cymagraph-watermark|generate_chladni'`, this session).
- No commit after `dd3dfb584` changed `AppShell.tsx`, `index.css` or `token-pairings.toml`. **verified-live** (`git log dd3dfb584..HEAD -- <paths>` returned nothing, this session).

### Consumers and Equivalents
- Logo files: `git grep -n -E 'cymagraph-small|favicon\.svg' -- frontend scripts docs` returns `frontend/index.html:11` and `Sidebar.tsx:7`. `cymagraph-small-light.svg` has no importer and is restored anyway, as AC1 says.
- Watermark: the search under Load-Bearing Assumptions.
- `extension/static/icons/` on `feat/2529` comes from the discarded mark and changes under hrse#2529, not here.

### Live Probe
none. `git grep -n -E 'urlopen|subprocess|claude -p'` over the kept files adds no call: this handoff only removes and restores. `scripts/generate_chladni_mark.py` is deleted.

### Delegated Judgment Calls
none

### Pre-Flight Preconditions
none

### Implementation Spec
1. Branch `fix/2614-restore-logo` from `origin/main` in `~/Harmonic_Projects/.worktrees/hrse2-2614-impl`, then run `mise run worktree-provision`.
2. Restore the three SVGs: `git checkout dd3dfb584^ -- frontend/public/favicon.svg frontend/src/assets/cymagraph-small-dark.svg frontend/src/assets/cymagraph-small-light.svg`.
3. Delete the four files in the table with `git rm`.
4. Edit `AppShell.tsx` and `index.css` as the table says. Afterward the AppShell root and `<main>` class strings equal their `dd3dfb584^` values. `git diff dd3dfb584^ -- frontend/src/AppShell.tsx` must show nothing.
5. Keep `--scrim`, `--color-scrim`, `token-pairings.toml`, `dialog.tsx`, `alert-dialog.tsx`, `eslint-rules/`, `e2e/mocked/a11y*` and `.github/workflows/ci.yml` as they are.
6. Run `mise run check`, commit with `mise run commit`, and post L2D naming the SHA. Visual baselines are expected to fail in CI at this point; say so in the L2D.
7. Lane 1 pushes that SHA and dispatches `update-visual-baselines.yml`. Lane 1 then posts a rework naming the run; Lane 2 runs `mise run visual-baselines-fetch --run <id>` and commits only files under `frontend/e2e/visual/*-snapshots/`.

### Test Cases (for Lane 3)
1. `git show HEAD:<path> | sha256sum` for each of the three SVGs must begin with the hash in Affected Files.
2. `git grep -n -E 'watermark-opacity|ResonanceWatermark|cymagraph-watermark|generate_chladni'` at HEAD must return nothing.
3. On the gate stack, every AppShell screen must render no `[data-testid="resonance-watermark"]`, and the sidebar logo must be the orange-center mark.
4. Dialog overlays must still use `bg-scrim`, and `frontend/eslint-rules/design-system.test.js` must pass.
5. `a11y.spec.ts` must show no rise from its baseline on any screen. CI's `verify`, including `e2e-visual`, must pass.
6. The operator signs off on the sidebar logo and the browser-tab favicon. This case is AE by name.

### Read-Before-Edit Instruction
Read the cited lines and quote the root-cause line or condition before making any change.

### Ambiguity Gate
If any instruction above is unclear, Lane 2 must stop and escalate to the Tech Lead before making any change.

Route (snapshot): path 1 — gate hrse-product — hrse product code: frontend/public/favicon.svg

<!-- l1-post v1; kind=handoff; plan-first=false; sha=0724962ddce42e16b6c8cd048630271e2e852f65; body-sha256=acd27d90df2b707a12df9a88190934ce459e6a4fa14fe71827c2a2f6d4ae62a4; checks=body-validation,tier-set,issue-open,origin-branch-sha-match,active-worktree-overlap -->

