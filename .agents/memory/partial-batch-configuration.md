---
name: Euro/kg dose configuration
description: Euro/kg dose queues distinguish pending state from missing grams and preserve valid work in partial batch saves.
---

For operator-facing batch forms, treat each row independently: blank rows mean “leave unchanged”, valid rows are saved, and invalid rows are reported without discarding valid rows. Keep one transaction for the valid changes so real database failures still roll back together.

**Why:** A Euro/kg dose form previously rejected a whole batch because one fixed-dose row was empty, causing the user to lose substantial data-entry time.

**How to apply:** Use row-level parsing and feedback for bulk configuration screens. Reserve whole-form rejection for structural tampering such as mismatched field arrays or duplicate identities.

Determine whether an article is pending from its explicit configuration state, not from whether its grams value is empty. A configured “Ao peso” article has no fixed-grams value.

**Why:** Grams absence alone cannot distinguish an unconfigured fixed-dose article from a valid configured weighing rule.

**How to apply:** Use the explicit pending state for queue groups, counts, and warning focus; keep blank grams valid for configured weighing rules.