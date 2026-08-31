---
name: Supplier identity matching
description: Safety rules for resolving supplier identities across OCR imports, aliases, and legacy NIF formats.
---

Supplier identity must be NIF-first when a NIF is present: `PT`-prefixed and digits-only forms refer to the same identity, and lookups/inserts must be serialized on that normalized key. Do not fall back to a matching name when the supplied NIF does not agree. Invalid Portuguese NIFs must never drive automatic OCR association.

**Why:** Historical imports contain both NIF formats and OCR can shorten legal company names. Name-only fallback or raw-NIF uniqueness can silently create duplicate suppliers or attach an invoice to the wrong legal entity.

**How to apply:** Preserve the canonical supplier ID, legal name, and NIF together. Match no-NIF variants only through an exact canonical name or a human-confirmed alias. Surface fuzzy/short-name matches for review, never merge automatically. Any conflict or supplier change must stop before database or external side effects and proceed only after explicit human confirmation.