---
name: Pastelaria priority-plan snapshots
description: Invariants for minimum stock configuration, Sunday completeness, and historical production plans.
---

Every active Pastelaria catalogue product must have an explicit minimum for every active participating store. An explicit zero is valid; an absent configuration is not zero and must block plan generation. Every eligible product/store pair must also have a real count for the selected Sunday.

**Why:** Treating missing configuration or counts as zero can silently omit production needs. Recalculating a previously printed plan can also change operational instructions after the fact.

**How to apply:** Calculate shortages per store, aggregate and rank them, then persist each generation as a new immutable version under a same-Sunday concurrency lock. Keep old versions readable even if catalogue products are later deleted. Made-to-order cake rows are outside the catalogue scope, while ordinary catalogue products with “Bolo” in their name remain eligible.