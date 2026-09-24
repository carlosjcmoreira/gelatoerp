---
name: Pastelaria transfer stock gate
description: Explicit, audited production balances and atomic transfer withdrawals for Pastelaria.
---

Pastelaria transfers require an explicitly confirmed production balance. Legacy digital totals are visible only as reference; they must not seed transferable stock automatically. Validate and withdraw every order line atomically, and restore a cancelled order at most once.

**Why:** The team explicitly replaced the previous manual-dispatch rule because transfers must not overdraw production stock, while incomplete historical records cannot establish a trustworthy opening balance.

**How to apply:** Keep balances and movements keyed by stable catalogue IDs or persisted configured-cake identity. Record opening balances, production, corrections, and transfer reversals with date, actor, and reason; receipts at destination remain movements, not physical counts.