---
name: Multi-worker migration startup
description: Why the Gunicorn application is preloaded before worker creation.
---

Preload migrations through one Gunicorn owner, but never let forked workers inherit live PostgreSQL sockets.

**Why:** Starting several workers at once ran overlapping `ALTER TABLE` migrations and caused PostgreSQL schema-lock deadlocks. Conversely, inheriting the pool created during preload made workers share PostgreSQL sockets, causing SSL/EOF corruption.

**How to apply:** Preserve a single migration owner before scaling workers. Reset database connections at the fork boundary so each worker creates independent sockets.