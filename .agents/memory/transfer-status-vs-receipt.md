---
name: Transfer execution status versus receipt
description: Keep transfer completion separate from the destination's optional receipt confirmation.
---

`ordens_transferencia.status` represents execution: pending orders are active, while confirmed or rejected orders belong in history. `rececao_estado` is separate receipt evidence; a completed order stays in history when the store has not checked it yet, reports a problem, or an administrator regularizes the receipt.

**Why:** Transfer execution can create the physical stock movement before the destination acknowledges it. Treating receipt acknowledgement as execution state hides completed transfers or leaves them incorrectly active.

**How to apply:** Filter active/history lists by execution status. Use receipt state only to describe the destination's follow-up; never use it to reopen an order or gate its history placement.