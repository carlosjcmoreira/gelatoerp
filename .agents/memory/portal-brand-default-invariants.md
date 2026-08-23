---
name: Portal brand default invariants
description: Consistency rule for the public Events portal brand and store activation lifecycle.
---

A public default portal brand must belong to an active store. Brand-default assignment and store deactivation must serialize by locking the same store row before evaluating or changing the default-brand state.

**Why:** Without a shared lock order, concurrent brand assignment and store deactivation can create an inactive default and expose the generic fallback despite an intended active brand.

**How to apply:** Preserve the store-row lock before modifying the public default or deactivating a store. Treat fallback to another configured active brand as recovery for legacy inconsistent data, not as a substitute for the write-time invariant.