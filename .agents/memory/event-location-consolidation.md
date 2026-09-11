---
name: Event location consolidation
description: Rules for turning event venue data into reusable operational locations.
---

Consolidate event locations automatically only when the normalized name and full
address match exactly. Preserve the original venue text and address on every
historical event and occurrence. Potential partial matches require a manager to
confirm the association, and that confirmation must be auditable.

**Why:** Similar venue names and incomplete or inconsistent addresses can refer
to different places. Rewriting historic records would make operational history
unreliable.

**How to apply:** New location matching and migration backfills may create a
link for an exact normalized name-and-address identity. Do not add fuzzy or
name-only merging; surface those cases for explicit review instead.