---
name: Map provider rate limits
description: Concurrency rule for batch operations that call shared map providers.
---

Batch geocoding must use a cross-worker lock as well as an interval between
provider requests. A per-process or per-request delay is not sufficient.

**Why:** Multiple Gunicorn workers or managers can start batches concurrently.
Without shared serialization, individually compliant batches can collectively
exceed the external provider's request limit and duplicate lookups.

**How to apply:** Any bulk or scheduled map lookup must share the same
database-backed lock. Keep batches small and safely resumable, and do not hold
row transactions open during network requests.