---
name: B2B transfer destinations
description: Rules for representing and processing external recipients in stock-transfer orders.
---

B2B recipients must be represented as typed external destinations with an explicit entity name, never by creating or reusing an internal store identity. Internal-store transfers retain their existing receipt-confirmation flow; B2B orders must not generate stock receipts at the external destination.

**Why:** Treating an external customer such as Sogrape as Bolhão (or as a synthetic store) corrupts destination history and can create stock in a location that does not exist. Store-originated B2B orders remain pending because the current effective-stock calculation uses pending outgoing orders to reserve stock until the next pesagem.

**How to apply:** Any transfer creation, history, logistics, filtering, confirmation, or future export must branch on the destination type. Show the external entity clearly, preserve internal store behavior, and never write a B2B receipt into store stock.