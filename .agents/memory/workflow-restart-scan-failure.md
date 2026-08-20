---
name: Workflow restart scanner failure
description: Environment-level failure that can prevent workflow restart/validation before the application command runs.
---

When workflow restart or validation fails because Ripgrep cannot find temporary paths below `.local/skills`, treat it as an environment scanner failure rather than an application startup failure.

**Why:** The error is raised before the configured command runs, so retrying the same workflow does not test the application and may leave the web server stopped.

**How to apply:** Verify syntax, focused tests, and template rendering independently. Attempt the configured restart once, capture the exact blocker, and use a validation skip reason only when the platform cannot list its configured checks.