---
name: Supplier common names
description: Presentation-name rules for suppliers and linked invoice history.
---

Supplier common names are an optional presentation layer. A linked invoice must display the supplier's common name when configured, otherwise the canonical legal name; an unlinked invoice falls back to its stored supplier text.

**Why:** Teams need concise, recognisable supplier labels without losing legal identity, NIF matching, import behaviour, or historical OCR/audit values.

**How to apply:** Never use a common name for NIF resolution, aliases, canonicalisation, imports, or persistence fields representing the legal/OCR supplier text. Do not bulk-rewrite invoice supplier text when a common name changes; compute the display label from the linked supplier at read time and keep the legal name available as secondary detail.