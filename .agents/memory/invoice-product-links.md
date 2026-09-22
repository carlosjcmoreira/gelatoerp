---
name: Invoice product links
description: Rules for associating invoice-line snapshots with the Compras master catalogue.
---

Invoice-line product associations are independent of the historical description, quantity, unit, unit price, and material association. Automatic suggestions are only exact normalized descriptions inside an active external-supplier origin whose canonical supplier matches a confirmed invoice supplier; operational, unresolved, and conflicting origins require a human decision. Commercial history reads the immutable invoice-line snapshots and only confirmed eligible invoice states.

**Why:** Invoice text is accounting evidence and supplier identity can be ambiguous; rewriting it or trusting fuzzy/cross-supplier matches would corrupt historical costs.

**How to apply:** Keep association changes append-only audited, preserve the line snapshot on product rename, and treat supplier conflicts or ambiguity as no-auto-link states.