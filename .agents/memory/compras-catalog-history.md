---
name: Compras catalog history
description: Why catalogue imports keep older rows and how future dataset versions should treat them.
---

The validated Compras seed is additive and identity-based. Rows already present before the versioned dataset must remain available for historical references; a newer seed may add or enrich rows but must not delete or silently replace those identities.

**Why:** invoices and other historical records may still refer to older catalogue entries, and the catalogue's deactivation model is intentionally non-destructive.

**How to apply:** preserve existing rows during future imports, use the stable source identity for seed rows, and deactivate rather than delete when an item should no longer be offered.