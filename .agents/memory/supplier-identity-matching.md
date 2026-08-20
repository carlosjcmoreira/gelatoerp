---
name: Supplier identity matching
description: Safety rules for resolving supplier identities across OCR imports, aliases, and legacy NIF formats.
---

Supplier identity must be NIF-first when a NIF is present: `PT`-prefixed and digits-only forms refer to the same identity, and lookups/inserts must be serialized on that normalized key. Do not fall back to a matching name when the supplied NIF does not agree.

**Why:** Historical imports contain both NIF formats and OCR can shorten legal company names. Name-only fallback or raw-NIF uniqueness can silently create duplicate suppliers or attach an invoice to the wrong legal entity.

**How to apply:** Preserve canonical supplier names on imports. Match no-NIF variants only through an exact canonical name or an alias created by a human-confirmed merge. Surface fuzzy/short-name matches for review, never merge them automatically.