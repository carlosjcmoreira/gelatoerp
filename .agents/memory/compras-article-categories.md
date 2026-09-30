---
name: Compras article-use categories
description: Rules for classifying purchasing catalogue articles for store-facing selection.
---

Store-facing article categories describe how an article is used. Initial categories come from explicit reviewed product identities/names; never infer them from official supplier, legacy supplier label, or operational origin. Every unknown or ambiguous article remains available under “Por classificar”.

For catalogue review, classification is manual; do not automatically reclassify existing rows. Saving edits across articles must be atomic, and official-supplier/origin confirmation stays separate from ordinary catalogue edits.

**Why:** Supplier/origin values answer procurement and routing questions, not store-use questions. A category backfill must not overwrite a human choice or make a legacy article unavailable. Category-only edits must also avoid changing the shared human-modified timestamp used by catalogue imports to protect other fields. Manual batch review should not partially save if one row fails validation or implicitly confirm a supplier.

**How to apply:** When adding or evolving category metadata, use explicit product-name mappings only for initial unset-value bootstrap; preserve later manual assignments and supplier/origin snapshots, keep the uncategorized choice selectable, audit human category edits separately from procurement identity changes, and commit a submitted review batch all-or-nothing.