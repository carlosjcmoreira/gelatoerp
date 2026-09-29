---
name: Confeitaria audited stock boundary
description: Cutover constraints between the immutable Confeitaria stock ledger and legacy production stock.
---

Keep the Confeitaria movement ledger separate from legacy production totals. Never infer an opening balance from legacy totals or match old stock by product name. Before each product's first audited transfer, require an operator-entered current physical count that accounts for legacy transfers since the opening balance.

**Why:** Existing transfers can debit legacy stock after an operator confirms an opening balance. Treating that stale opening as current would overstate available stock; a fresh physical count establishes a safe per-product cutover without importing ambiguous legacy totals.

**How to apply:** Record the count as an immutable reconciliation against a fresh audited-balance snapshot. After cutover, read transfer availability only from the ledger and keep production credits, transfer debits, destination receipts, and eligible cancellation reversals transactional.