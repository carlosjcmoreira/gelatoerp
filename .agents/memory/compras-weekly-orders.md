---
name: Weekly store orders
description: Durable boundary between weekly purchasing plans and stock movements.
---

Weekly store orders are planning documents owned by a stable store ID, not stock-transfer orders. Their Sunday planning cycle and Monday delivery date are explicit, and the submitted article/origin snapshot remains immutable evidence even when catalogue data later changes.

**Why:** Reusing stock-transfer rows would materialize inventory and would not preserve the exact request sent to Compras when quantities, supplier origins, or catalogue activity change.

**How to apply:** Keep weekly status transitions, amendments, and consolidations in the weekly-order model. Use canonical article and purchasing-origin IDs for current validation, but always retain product, unit, supplier, and origin snapshots in submitted versions.