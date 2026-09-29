---
name: Gelato sales dose identity
description: Stable identity and lifecycle rules for mapping sales products to versioned gelato doses.
---

Gelato sales products must use stable product identities and explicit, non-overlapping associations to versioned dose rules. Manager selection and dose completeness are separate states: a selected product without current grams stays selected and is shown as pending, but its uncovered sales remain unknown in the KPI. Renames change presentation, not historical meaning. Text matching is allowed only to suggest or migrate an association that a manager can verify.

**Why:** Substring matching made overlapping names ambiguous and allowed product or rule renames to silently alter historical theoretical consumption.

**How to apply:** Every sales write must persist the stable configured-product ID. KPI and weight calculations must resolve the dated rule ID, never infer it from the imported label. Configuration reads must never change manager selection. A selected product without a current rule stays visible as pending; assigning grams clears pending without rewriting earlier periods. Reassociation preserves prior dated rows, may return to a previously used rule, and replaces all scheduled associations from its effective date onward. Deactivation closes the current association, cancels future associations, and retains earlier history. Every transaction that creates, rebinds, replaces, or imports a dose must acquire the shared dose-write lock before article or row locks, then backfill NULL sale weights only within the association's dated interval. Backdated rule imports must distinguish associations spanning the effective date from later-starting associations, rebinding the latter without changing their dates.

Exact alias families share the canonical product's earliest sale only when the canonical identity is unique and every alias link resolves unambiguously. Historical backfills may move the canonical first association and its linked rule together, but must not create alias associations, rewrite sale IDs or quantities, or alter shared/conflicting rules.

**Why:** Alias labels can predate the canonical configuration ID; using only the canonical ID's first sale leaves valid historical sales with an artificial unknown-dose gap. Conservative identity checks prevent that repair from crossing an ambiguous rename or another product's rule.

**How to apply:** Resolve aliases by exact name and stable configured-product IDs. Use the family earliest sale for first-dose creation and an idempotent versioned migration for existing canonical first rules; leave ambiguous families for explicit review.

Warning-driven configuration links must use the eligible configured-product ID resolved for each sale. Alias sales target the canonical selected product when available; missing-weight warnings do not link into dose editing.

**Why:** Product labels can be historical aliases, while sales configured for weight may already have a valid dose and need a different correction. Label-based navigation can focus the wrong row or encourage an unnecessary dose change.

**How to apply:** Carry the resolved stable ID through warning aggregation and only show a focused configuration action to managers. Keep period and store context in the link; use a general diagnostic link when no eligible dose ID exists or only sale weight is missing.