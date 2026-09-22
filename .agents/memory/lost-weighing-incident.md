---
name: Lost weighing incident
description: Evidence and interpretation of the September 2026 Bolhão end-of-day weighing loss.
---

Treat the 21 September 2026 Bolhão incident as a submission failure, not as
committed weighing rows being deleted. The page loaded and the entries were
still shown as pending, but no weighing POST reached production that night or
the next morning, and no final stock rows existed for that date.

**Why:** Production health failures started later than the pending-list
screenshot. Attributing the loss to those failures would hide the actual risk:
manual drafts previously existed only in one browser and could disappear
without ever reaching the server.

**How to apply:** When investigating similar reports, distinguish server-side
draft, confirmation attempt, confirmed receipt, and final stock rows. Do not
infer that final rows disappeared merely because an operator remembers entering
values in the browser.