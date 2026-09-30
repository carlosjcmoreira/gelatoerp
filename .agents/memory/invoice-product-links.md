---
name: Invoice product links
description: Rules for associating invoice-line snapshots with the Compras master catalogue.
---

Invoice-line product associations are independent of the historical description, quantity, unit, unit price, and material association. Suggestions use exact normalized descriptions among active articles whose direct supplier ID matches the invoice's confirmed canonical supplier. Historical origins do not establish current supplier identity. Explicit links and article creation require confirmed invoice identity; links also require a matching direct article supplier. Commercial history reads immutable invoice-line snapshots and only confirmed eligible invoice states.

**Why:** Invoice text is accounting evidence and supplier identity can be ambiguous; rewriting it or trusting legacy origins, fuzzy matches, or cross-supplier matches would corrupt historical costs.

**How to apply:** Keep association changes append-only audited, preserve the line snapshot on product rename, and treat unconfirmed identity, supplier conflicts, or ambiguity as no-auto-link states.