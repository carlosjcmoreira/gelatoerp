---
name: Gelato stock rotation evidence
description: Durable evidence, boundary, and uncertainty rules for gelato consumption estimates.
---

Gelato consumption is a stock-conservation residual between comparable physical weighings. Opening and end-of-day weighings have different movement boundaries and must not be mixed.

**Why:** Mixing the two time conventions, treating control records as production, or filling missing movements with zero creates plausible but incorrect daily-consumption values.

**How to apply:** Count physical confirmations rather than commitments, reconcile overlapping movement evidence before counting, weight averages by observed days, and exclude intervals with negative residuals or unresolved movements. Unknown values remain unknown.