---
name: Daily weighing state
description: Durable rules for missing, draft, confirmed, and justified end-of-day store weighings.
---

Daily EOD weighing status is a store-and-business-date state with precedence:
confirmed, justified, draft, then missing. Use stable store identity, not editable
display names. Operational “previous day” windows use the Europe/Lisbon business
date and end on the last closed day.

Justifications are immutable audited exceptions for closed days when no weighing
should occur. They never create synthetic stock. Once a day is justified, every
path that could create or move final weighing stock into that store/day must
reject the write under the same per-store/day transaction lock.

**Why:** Missing calendar dates used to disappear from Production’s latest-data
view, and independent draft, stock, and exception writes could otherwise produce
contradictory states or hide an exception behind later stock.

**How to apply:** Any new EOD import, edit, bulk operation, or confirmation path
must participate in the shared status model, stable store identity, Lisbon date
semantics, and justification lock/check before writing final stock.