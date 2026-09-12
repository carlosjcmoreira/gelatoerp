---
name: Pastelaria rotation evidence
description: Defines which evidence can support stock-rotation estimates and confidence labels.
---

Pastelaria rotation must use only genuine physical stock counts as interval endpoints. Transfer-generated stock rows are receipt movements, not stock snapshots, and confirmed transfers use their actual confirmation date.

**Why:** A receipt quantity is not the store's complete stock. Treating it as a count, or counting the same transfer as both a snapshot and an entry, materially corrupts the residual-consumption estimate.

**How to apply:** Keep count origin explicit. Estimate residual consumption as opening count plus known inbound movements minus closing count. Exclude negative residuals from KPIs/rankings, and keep confidence low unless all relevant movement classes can be proven complete; a regular 5–14 day interval may be called comparable, not more reliable.