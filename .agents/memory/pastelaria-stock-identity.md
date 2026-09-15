---
name: Pastelaria stock identity
description: Stable identity and conservative legacy matching rules for Pastelaria stock calculations.
---

Pastelaria records that contribute to stock calculations must be grouped by stable product identity, while their stored text remains a historical snapshot. Unresolved legacy text stays unresolved permanently; do not retry label matching on later startups or infer identity from a current rendered label.

**Why:** Catalogue labels can be renamed or reused. Repeated or read-time text matching can attach old counts to a different product and silently rewrite stock history.

**How to apply:** New catalogue-backed counts, movements, plans, and production-stock rows persist the catalogue ID. Historical backfill is one-time and only accepts a unique exact rendered-label match. Aggregations join by identity first and render labels afterward; raw history retains unmatched text.