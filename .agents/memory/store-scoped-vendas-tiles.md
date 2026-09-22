---
name: Store-scoped Vendas tiles
description: Durable rules for per-store navigation configuration in the shared Vendas module.
---

Vendas keeps one shared module identity, including its module label and icon. Child tile visibility, labels, and icons are scoped by stable `store_id`; store names are presentation only.

**Why:** The same operational module serves stores with different capabilities, so a global child-tile configuration either exposes unsupported actions or forces unrelated stores to share customizations.

**How to apply:** Use the canonical Vendas tab definitions and one shared capability filter for both runtime navigation and Gestão de Tiles. Keep global rows as migration/fallback data, and never use a store name as configuration identity.