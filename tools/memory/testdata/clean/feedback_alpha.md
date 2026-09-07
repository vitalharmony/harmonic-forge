---
name: feedback-alpha
description: The first fixture memory
metadata:
  type: feedback
first_seen: 2026-09-01
instances: 1
promoted: R-0006
---

Body text linking to [[project_beta]].

harmonic-forge#500: carries `promoted:` so it is immune to the calendar.
Without it, its absolute `first_seen:` crossed the 14-day threshold on
2026-09-15 and turned `mise run check` red with no code change — a time bomb
invisible in the diff that planted it. A `promoted:` file skips the age test,
the only date-independent way a permanent fixture stays green.
