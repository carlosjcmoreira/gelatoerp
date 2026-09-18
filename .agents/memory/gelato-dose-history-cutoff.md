---
name: Gelato dose history cutoff
description: Historical dose rules and the evidence boundary for theoretical gelato consumption.
---

The first dose configured for a stable sales product is treated as the dose used since the earliest recorded sale of that product. Only later changes require an explicit effective date; earlier periods then retain the previous dose.

**Why:** Store operators confirmed that these weighing rules have been in use since each store opened; leaving the first configuration unknown would incorrectly make ordinary historical sales look unrecoverable. Reapplying a later changed value to old sales would still fabricate history.

**How to apply:** For a product with no association history, start the first rule at its earliest recorded sale (or today when it has no sales). For an existing product history, require the new version's effective date and preserve non-overlapping prior intervals. Historical CSV imports remain the path for separately evidenced old changes. Missing rules remain unknown, never zero.