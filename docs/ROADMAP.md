# harmonic-forge roadmap

Forge's releases, one thesis each. Milestones own release membership; this
file owns what each release means. `drift_check.py` fails when an open
version-shaped milestone has no `## <version>` section here
(harmonic-forge#1382).

## Scheme

| Item | Rule |
|---|---|
| Versioning | SemVer `0.y.z`. A minor is a forge release and may break contracts; a patch is optional. |
| Milestones | Bare numbers (`0.3`, `0.4`) plus one parking milestone, `Later`. The lowest open number is the current release (R-0404). |
| Version source | The annotated tag `v0.y.0`. No `VERSION` file, no per-commit bump. |
| Coupling to CymaGraph | None. Forge's series is independent of hrse's (operator decision 184). |
| Cadence | A thesis per release, a soft box of 3 to 4 weeks. Unfinished issues move to the next milestone or `Later` at close. |
| Parking | `Later` holds real work not yet placed in a release. A filing into it owes no handoff and posts an R-0352 deferral (`--deferral-trigger`, `--deferral-owner`). It is reviewed at every release close. |

## 0.3 — precheck enforcement

The first tagged release (`v0.3.0`, above the hand-kept "Draft v0.2"). Thesis:
a gated step is prechecked mechanically before it runs, never tried and
failed (R-0419). Its first content is the precheck-enforcement issues
(F1291, F1292, F1293 and their follow-ups); everything else waits in `Later`
until the operator moves it.
