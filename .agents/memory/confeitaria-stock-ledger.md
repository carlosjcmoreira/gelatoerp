---
name: Confeitaria audited stock boundary
description: Cutover constraints between the immutable Confeitaria stock ledger and legacy production stock.
---

Keep the Confeitaria movement ledger separate from legacy production totals. Never infer an opening balance from legacy totals or match old stock by product name. Until production, transfers, and cancellations all write audited movements atomically, the new balance is not an authoritative transfer source.

**Why:** Existing transfers can debit legacy stock after an operator confirms an opening balance. Switching only the transfer read to the ledger would then overstate available stock. Product names are not stable identities.

**How to apply:** Before transfer cutover, reconcile or reconfirm any legacy movements after each opening balance. Switch the transfer validation and every production/transfer/cancellation write together, requiring a confirmed opening and sufficient active-product balance.