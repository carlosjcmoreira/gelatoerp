---
name: Pastelaria Sunday count consistency
description: Concurrency rule for Sunday counter-stock grids and individual history mutations.
---

Sunday grid reads and all mutations that can change Sunday counter-stock history must use the same date-scoped transaction lock. The grid version must represent the effective latest row for every store/product cell, not only the maximum row ID.

**Why:** A value read separately from its version, or deletion of a latest cell that does not own the global maximum ID, can let stale grid data overwrite a newer physical count.

**How to apply:** Any new bulk, individual, correction, import, or deletion path for Sunday Pastelaria counts must participate in the date lock and must invalidate the effective-cell version checked by bulk saves.