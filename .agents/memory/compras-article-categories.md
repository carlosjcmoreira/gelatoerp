---
name: Compras article-use categories
description: Rules for classifying purchasing catalogue articles for store-facing selection.
---

Store-facing article categories describe how an article is used. Initial categories come from explicit reviewed product identities/names; never infer them from official supplier, legacy supplier label, or operational origin. Every unknown or ambiguous article remains available under “Por classificar”.

**Why:** Supplier/origin values answer procurement and routing questions, not store-use questions. A category backfill must not overwrite a human choice or make a legacy article unavailable. Category-only edits must also avoid changing the shared human-modified timestamp used by catalogue imports to protect other fields.

**How to apply:** When adding or evolving category metadata, backfill only unset values from an explicit product-name mapping, preserve manual assignments and supplier/origin snapshots, keep the uncategorized choice selectable, and audit human category edits separately from procurement identity changes.