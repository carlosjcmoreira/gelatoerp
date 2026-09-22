---
name: Weighing audit and recovery
description: Durable integrity rules for weighing history, deletion, restoration, and reporting.
---

Every weighing mutation must create an immutable audit event in the same
transaction as the business change. Confirmed batch receipts remain immutable
snapshots even if their stock rows are later edited or deactivated.

Deletion means reversible deactivation, never physical removal. Operational
stock, Production calculations, forecasts, dashboards, and daily weighing
status must use active rows only. Audit and investigation views deliberately
include inactive evidence. Restoring a row requires an authorized actor, a
reason, the store/day lock, and active-row uniqueness.

**Why:** A missing weighing cannot be investigated if the current-state table
is the only evidence. Physical deletion also makes it impossible to distinguish
an unconfirmed browser draft from a confirmed batch that was later changed.

**How to apply:** New stock write paths must carry actor, origin, stable store
identity, before/after values, and batch identity where applicable. New reads
of weighing stock must explicitly choose active operational data or full audit
evidence; never rely on an unqualified query.