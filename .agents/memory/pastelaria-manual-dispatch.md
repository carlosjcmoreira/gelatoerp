---
name: Pastelaria manual dispatch
description: Operational rules for weekly pastry planning, cake configurations, and transfers.
---

Pastelaria transfer orders must not be blocked, truncated, or deducted from digital production stock. Digital stock is reference-only for this workflow.

**Why:** The pastry team may produce and dispatch physically without first recording digital production. Requiring a digital balance prevents legitimate transfers and creates false stock movements.

**How to apply:** Keep the legacy production history intact, but treat weekly planning and transfer creation as the operational source for dispatch. A configured cake is identified by its persisted size/flavours/cover combination and may only be transferred when that identity exists in the selected date's plan or another persisted stock record.