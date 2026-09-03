# Lane 3 Test Gate — Frontend UI Golden Path (Greg's variant)

For UI-only tickets where the change is presentational/interaction and does
not touch business logic, data contracts, or backend behavior.

| Metric | Threshold | Enforced by |
|---|---|---|
| Visual regression | 0 unreviewed diffs | Playwright screenshot comparison |
| Component smoke tests | 100% pass | Render + key interaction per component |
| Unit test requirement | Not required for pure UI | Waived for UI-only tickets |
| Test spec approval | HITL (Tech Lead) | Required before test execution begins |

## Rules

<!-- R-0074 -->
1. This path applies only when the ticket is genuinely UI-only. If the
   change touches a data contract, a service, or business logic alongside
   the UI, use the standard `testing-gate.md` path instead — don't waive
   coverage on a ticket that isn't actually UI-only.
<!-- /R-0074 -->
<!-- R-0075 -->
2. Visual regression comparison runs via Playwright screenshot diffing.
   A diff that hasn't been explicitly reviewed and accepted blocks the gate
   — it does not auto-pass on "looks fine."
<!-- /R-0075 -->
<!-- R-0076 -->
3. Smoke tests must actually render the component and exercise its key
   interaction (click, input, toggle) — a test that only imports the module
   without rendering it does not count.
<!-- /R-0076 -->
<!-- R-0077 -->
4. Same HITL and live-verification standard as the standard path: no
   "Evidence type: Source" pass claims.
<!-- /R-0077 -->
<!-- R-0078 -->
5. **A `net::ERR_*` blocker report must include the entry URL, the full
   redirect chain, and the response status of every hop — never just the
   bare error string and originally-requested URL.** Playwright attributes
   a `net::ERR_*` failure to the URL first navigated to, even when the
   actual failing hop is several redirects downstream — quoting only that
   URL is not diagnostic and can send Lane 1 to the wrong layer entirely.
   Real incident (hrse#500, 2026-07-29): two consecutive Lane 3 FAILs were
   both attributed to a structurally implausible "network reachability"
   theory; a sticky-wicket review found the actual refused connection was
   a different port entirely, reached via a redirect hop triggered by a
   reused browser context carrying an existing SSO cookie — invisible in
   the bare error string. Capture `page.url()` at failure and the
   `redirectedFrom()` chain, not just the exception text.
<!-- /R-0078 -->
<!-- R-0079 -->
6. Gate specs for a page served by a live third-party dependency (e.g. an
   IdP-hosted login page) must name the exact entry URL and require
   browser-context hygiene (fresh non-persistent context, no
   `storageState` reuse) rather than leaving Lane 3 to construct its own
   entry point — context reuse can silently change page behavior (e.g. an
   OIDC endpoint skipping its login form because of an existing session
   cookie), the same root cause named in Rule 5's incident.
<!-- /R-0079 -->

**Open item:** the visual-regression tooling itself (Playwright config,
baseline screenshots) is not yet set up on every project using this variant
— confirm it exists on a given project before relying on this gate for that
project's tickets.
