---
name: Pastelaria count submission audit
description: Rules for recording and interpreting Pastelaria count authorship and store selection across submissions and legacy rows.
---

Treat each saved count as an append-only submission with one batch identity. Stable store identity is authoritative; the submitted store name preserves what was displayed as context. Never infer the author of an old row. Link legacy rows to a store only when the exact saved name identifies one store unambiguously; leave ambiguous rows unlinked.

**Why:** Count history is evidence of who recorded a physical stock state and for which store. Guessing from a current name or grouping separate writes by timestamp can silently misattribute that evidence.

**How to apply:** Preserve batch boundaries, actor, timestamp, and stable store identity in any new Pastelaria count writer, history view, or legacy migration. Do not use fuzzy label matching to repair historic attribution.