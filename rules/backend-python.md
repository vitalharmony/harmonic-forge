---
paths:
  - "**/*.py"
---

# Backend Python — Universal Hard Constraints

Applies to any Python backend across Vital Harmony projects. Project-specific
addenda (e.g. a Cypher/Neo4j-specific rule file) live in the project's own
`.claude/rules/` and layer on top of this file — they are not part of the
platform sync since they don't apply universally.

## Type Hints

<!-- R-0060 -->
Every function carries an explicit return type hint.
<!-- /R-0060 -->

## Pydantic Payloads

<!-- R-0061 -->
All inbound write/update payloads use explicit Pydantic models with
`model_config = ConfigDict(extra="forbid")`. No `dict`, `Any`, or open-ended
types at API boundaries.
<!-- /R-0061 -->

**Correct:**
```python
class UpdateProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    location: str | None = None
```

## Query Injection Safety

<!-- R-0062 -->
Any query language with injection risk (SQL, Cypher, etc.) must be
parameterized. f-strings/concatenation to build query bodies are forbidden.
<!-- /R-0062 -->

**Correct:**
```python
records = session.run(
    "MATCH (p:Person) WHERE elementId(p) = $person_id RETURN p",
    person_id=person_id,
).data()
```

**Forbidden:**
```python
session.run(f"MATCH (p:Person) WHERE elementId(p) = '{person_id}' RETURN p")
```

## Single-Instance Resources

<!-- R-0063 -->
Any driver/connection-pool singleton (DB driver, cache client) is
instantiated exactly once, in one designated module, at process lifespan.
Never instantiate a second one elsewhere — inject/depend on the singleton.
<!-- /R-0063 -->

## LLM Gateway

<!-- R-0064 -->
If the project makes LLM calls, all of them route through one designated
gateway module (`complete()`-style entrypoint). No other module constructs a
provider client directly or hardcodes a model ID. Structured output: use the
gateway's schema-validated entrypoint if one exists rather than hand-rolling
`json.loads(strip_fences(...))` call sites — that pattern is fragile and
silently returns empty results on malformed output.
<!-- /R-0064 -->

## Layering

<!-- R-0065 -->
- **Routers/controllers** are thin HTTP layers — validation in/out, no
  business logic.
- **Services** hold business logic.
- Reusable queries live in a dedicated queries module, not inline in
  services or routers.
<!-- /R-0065 -->

## Observability

<!-- R-0066 -->
Use the structured logger (`logging.getLogger(__name__)` or project
equivalent) — never `print()` in production paths.
<!-- /R-0066 -->

## Async Safety

<!-- R-0067 -->
Never call blocking I/O inside an `async def` without offloading it to a
thread pool. Sync DB drivers, PDF/image libraries, and `requests` all block
the event loop — one slow call starves every concurrent request.
<!-- /R-0067 -->

**Correct:**
```python
from starlette.concurrency import run_in_threadpool

result = await run_in_threadpool(session.run, query, **params)
```

## Pre-Submit Checklist

- [ ] Every query call uses parameterized inputs (no f-strings)
- [ ] No new singleton-resource instantiation outside its designated module
- [ ] No new raw `json.loads(strip_fences(...))` call sites for LLM output
- [ ] Every `async def` calling blocking I/O wraps it in a thread-pool offload
