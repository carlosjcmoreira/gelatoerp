---
name: Portal brand default invariants
description: Consistency rule for the public Events portal brand and store activation lifecycle.
---

A public default portal brand must belong to an active store. Brand-default assignment and store deactivation must serialize by locking the same store row before evaluating or changing the default-brand state.

**Why:** Without a shared lock order, concurrent brand assignment and store deactivation can create an inactive default and expose the generic fallback despite an intended active brand.

**How to apply:** Preserve the store-row lock before modifying the public default or deactivating a store. Treat fallback to another configured active brand as recovery for legacy inconsistent data, not as a substitute for the write-time invariant.

The Eventos portal is configured as one public form for this instance. Its editor must not expose store selection or multi-brand terminology, even though the persisted active configuration retains an internal store association for lifecycle safety and historical request attribution.

**Why:** Existing requests need to retain the public identity used at submission, while the current product has one customer-facing form to maintain.

**How to apply:** Update the active public configuration through the singleton workflow; do not remove or repurpose historic brand associations on submitted requests.