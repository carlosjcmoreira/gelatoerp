---
name: Gelato dose history cutoff
description: Historical dose rules and the evidence boundary for theoretical gelato consumption.
---

Do not apply the current grams-per-product rules retroactively. The first versioned rules become effective on the date the history model is introduced; earlier theoretical consumption remains unknown unless a dated rule is explicitly backfilled from reliable evidence.

**Why:** Assigning today's grams to old sales creates precise-looking but fabricated dose adherence and silently rewrites history whenever current configuration changes.

**How to apply:** Any import or backfill of old dose rules must include an evidenced effective date and preserve non-overlapping versions. Enforce that invariant in the database, not only application code. Preview historical changes server-side and reject confirmation if affected history changed. Missing historical rules must produce incomplete/unknown metrics, never zero.