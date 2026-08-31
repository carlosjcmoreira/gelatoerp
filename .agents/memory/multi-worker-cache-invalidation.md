---
name: Multi-worker cache invalidation
description: Consistency rule for process-local caches of administrator-editable database configuration.
---

Administrator-editable database configuration may be cached in each Gunicorn
worker only when writes publish a shared generation and readers compare that
generation before accepting a local entry.

**Why:** Invalidating an in-memory dictionary affects only the worker that
handled the write. Other workers can otherwise serve obsolete labels, icons,
visibility, or status choices until a TTL expires.

**How to apply:** Reuse the shared exact-key or argument-prefix invalidation
mechanism for new mutable configuration caches, and invalidate both source
records and every derived cached view after a successful commit.