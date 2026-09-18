---
name: Multi-worker migration startup
description: Why the Gunicorn application is preloaded before worker creation.
---

Preload migrations through one Gunicorn owner, but never let forked workers inherit live PostgreSQL sockets.

**Why:** Starting several workers at once ran overlapping `ALTER TABLE` migrations and caused PostgreSQL schema-lock deadlocks. Conversely, inheriting the pool created during preload made workers share PostgreSQL sockets, causing SSL/EOF corruption.

**How to apply:** Preserve a single migration owner before scaling workers. Reset database connections at the fork boundary so each worker creates independent sockets.

Only schema migrations belong in the synchronous preloaded application startup. Historical data corrections and recurring reconciliations must run after the first worker is ready.

**Why:** Production database latency made the combined startup exceed the Autoscale 60-second readiness limit twice, even though the build and schema migrations were valid.

**How to apply:** Keep schema changes under the single pre-fork owner. Start retryable data maintenance once from the first initialized worker so it cannot delay port binding or the root health check.