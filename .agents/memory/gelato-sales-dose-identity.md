---
name: Gelato sales dose identity
description: Stable identity and lifecycle rules for mapping sales products to versioned gelato doses.
---

Gelato sales products must use stable product identities and explicit, non-overlapping associations to versioned dose rules. A product may enter the KPI only while it has a currently effective association. Renames change presentation, not historical meaning. Text matching is allowed only to suggest or migrate an association that a manager can verify.

**Why:** Substring matching made overlapping names ambiguous and allowed product or rule renames to silently alter historical theoretical consumption.

**How to apply:** Every sales write must persist the stable configured-product ID. KPI and weight calculations must resolve the dated rule ID, never infer it from the imported label. New or currently unmapped gelato products stay inactive in the configuration queue; reassociation preserves prior dated rows, may return to a previously used rule, and replaces all scheduled associations from its effective date onward. Deactivation closes the current association, cancels future associations, and retains earlier history; reactivation always returns to the explicit-association queue. Every transaction that creates, rebinds, or replaces a weight-rule association must backfill NULL sale weights only within that association's dated interval. Backdated rule imports must distinguish associations spanning the effective date from later-starting associations, rebinding the latter without changing their dates.