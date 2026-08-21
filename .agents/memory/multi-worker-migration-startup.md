---
name: Multi-worker migration startup
description: Why the Gunicorn application is preloaded before worker creation.
---

Keep Gunicorn `preload_app` enabled while application startup runs database migrations and data corrections.

**Why:** Starting several workers at once ran overlapping `ALTER TABLE` migrations and caused PostgreSQL schema-lock deadlocks, preventing the app from booting.

**How to apply:** If startup migration behavior changes, preserve a single migration owner (preload or another cross-process lock) before scaling workers.