---
name: Urgent store requests
description: Durable rules for exceptional store purchasing requests.
---

Urgent store requests are submitted exception documents, not amendments to a submitted weekly order and not stock-transfer movements. Repeated identical content for the same store is one request, and every lifecycle change is audited.

**Why:** Urgent demand must remain measurable as an exception by store, article, reason, and target date without rewriting the weekly plan or creating inventory side effects.

**How to apply:** Keep the deterministic dedupe boundary and immutable article/origin snapshots. Resolve the Moedas catalogue category to the typed internal Matosinhos origin; never create or infer a supplier for it.