---
name: Weekly store orders
description: Durable boundary between weekly purchasing plans and stock movements.
---

Weekly store orders are planning documents owned by a stable store ID, not stock-transfer orders. Their Sunday planning cycle and Monday delivery date are explicit, and the submitted article/origin/supplier snapshot remains immutable evidence even when catalogue data later changes. Dispatches and store receipt confirmations are a separate audited ledger that keeps requested, sent, and received quantities distinct without creating inventory movements.

**Why:** Reusing stock-transfer rows would materialize inventory and would not preserve the exact request or delivery evidence when quantities, supplier links, or catalogue activity change.

**How to apply:** Keep weekly status transitions, amendments, and consolidations in the weekly-order model. Link dispatches and receipts back to submitted request lines, but never treat them as transfers or stock updates. Use canonical article and purchasing-origin IDs for current validation, while retaining product, unit, official supplier, and origin snapshots in submitted versions.