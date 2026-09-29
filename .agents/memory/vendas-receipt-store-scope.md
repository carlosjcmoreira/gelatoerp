---
name: Vendas receipt store scope
description: Store-context and whole-batch invariants for Vendas transfer receipt actions.
---

State-changing receipt actions must bind to the selected active store, not a display-name fallback or any store in a user's broader assignment. Reject malformed, foreign, mixed-batch, or partial-pending ID lists as a whole before recording acceptance or a problem report.

**Why:** A fallback store context can expose receipts outside the user's valid selection, while per-order processing can partially record a forged or incomplete batch.

**How to apply:** Resolve an active authorized store for every write, lock and validate all submitted batch members in one transaction, and keep acceptance/problem reports idempotent and proof-only.