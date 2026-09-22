---
name: Purchase count snapshots
description: Durable boundary for physical counts of the purchasing catalogue.
---

Physical purchase counts are store-scoped evidence snapshots. Draft editing may change only the current draft; submission creates an immutable version with article and origin snapshots, and historical reads must not depend on the article remaining active.

**Why:** Counts support purchasing review but must not silently alter specialised stock, receipts, transfers, weekly orders, or calculated quantities to buy.

**How to apply:** Keep draft deduplication scoped to store/date/user, make repeated submission idempotent, validate active catalogue articles only when creating new lines, and preserve stable article IDs plus display snapshots in every submitted version.