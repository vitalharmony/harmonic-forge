---
paths:
  - "**/*.ts"
  - "**/*.tsx"
---

# Frontend TypeScript — Universal Hard Constraints

Applies to any TypeScript/React frontend across Vital Harmony projects.

## Type Safety

<!-- R-0068 -->
`any` is forbidden. Use `unknown` with type predicates, or an explicit
interface/type.
<!-- /R-0068 -->

**Correct:**
```typescript
catch (err: unknown) {
  if (err instanceof Error) setError(err.message)
}
```

**Forbidden:**
```typescript
const data: any = await fetch(...)   // raw fetch + any — both violations
```

## API Calls

<!-- R-0069 -->
All backend calls go through the project's one designated HTTP client
module. No raw `fetch()`/axios calls anywhere else in application code.
<!-- /R-0069 -->

## Graceful Degradation

<!-- R-0070 -->
Components must degrade to mock data or empty states when the backend is
offline — never crash or surface an unhandled error to the user.
<!-- /R-0070 -->

## Async Cleanup

<!-- R-0071 -->
`useEffect` hooks that fire an API call must cancel on unmount via
`AbortController`.
<!-- /R-0071 -->

**Correct:**
```typescript
useEffect(() => {
  const controller = new AbortController()
  apiClient.get<ProfileResponse>('/profiles/123', {
    signal: controller.signal,
  }).then(setProfile).catch(() => {})
  return () => controller.abort()
}, [id])
```

## Runtime Boundary Validation

<!-- R-0072 -->
TypeScript types API responses at compile time only — no runtime guarantee.
Validate required fields before writing a response into component props or
state; don't silently propagate `undefined`.
<!-- /R-0072 -->

## 300-Line Cap

<!-- R-0073 -->
If an edit would push any `.ts`/`.tsx` file over 300 lines, decompose into
smaller modules before proceeding.
<!-- /R-0073 -->
