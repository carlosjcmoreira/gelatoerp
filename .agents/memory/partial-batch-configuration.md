---
name: Partial batch configuration
description: Batch configuration forms must preserve valid work when some rows are blank or invalid.
---

For operator-facing batch forms, treat each row independently: blank rows mean “leave unchanged”, valid rows are saved, and invalid rows are reported without discarding valid rows. Keep one transaction for the valid changes so real database failures still roll back together.

**Why:** A Euro/kg dose form previously rejected a whole batch because one fixed-dose row was empty, causing the user to lose substantial data-entry time.

**How to apply:** Use row-level parsing and feedback for bulk configuration screens. Reserve whole-form rejection for structural tampering such as mismatched field arrays or duplicate identities.