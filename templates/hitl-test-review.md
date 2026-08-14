<!-- Tech Lead approval checklist for a Lane 3 test spec. Complete before Lane 3
     executes any test against Lane 2's implementation. -->

## HITL Test Spec Review: [Issue #N — Short Title]

- [ ] **Each test case is labelled `TC<n>`** (`TC1`, `TC2`, …). Lane 1's
      gate-readiness sweep is cross-checked against these identifiers, so
      that it accounts for exactly the cases in the approved spec — no more,
      no fewer. A plain numbered list under a `### Test cases` heading is
      accepted as a fallback (hrse#875), but the explicit identifier is
      preferred: it lets a later gate report cite a case unambiguously.
- [ ] Test cases cover all acceptance criteria stated in the issue
- [ ] No test is trivially easy (e.g. `assert True`, a test that can't fail)
- [ ] Edge cases are represented (empty input, boundary values, error paths)
- [ ] Coverage scope is appropriate to the ticket type (standard vs. UI
      golden path — see `rules/testing-gate.md` / `rules/frontend-ui-golden-path.md`)
- [ ] Test spec was written from the issue alone — confirm Lane 3 did not
      cite Lane 2's implementation as the basis for any test case
- [ ] For each check in this spec, the evidence artifact that will prove it
      is pre-declared (command+output, log excerpt, screenshot/recording,
      before/after query output) — approving this spec is approving what
      counts as proof, not just what gets tested
- [ ] **Does this spec include executing a data-modifying script's
      write/apply path against real data?** If yes, say so explicitly
      here and confirm the HITL understands approval of this spec *is*
      approval to execute it — Lane 3 is the only lane authorized to run
      that action (see `3-lane-protocol.md`, Lane 3 section). If no,
      leave this unchecked; Lane 2's implementation must already be
      dry-run/fixture-verified only.
  - Data-modifying execution in this spec? Yes ☐ / No ☐

**Approved:** _______  **Date:** _______

**Notes / requested changes before approval:**
