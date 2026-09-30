---
name: Table-specific layout classes
description: Avoid layout CSS shared between table rows and non-table components.
---

Use element-specific classes for layout-changing styles. A class that sets `display: flex` or `display: grid` on a non-table component should not also be applied to a `<tr>`; give the table row its own class instead of overriding table display rules globally.

**Why:** A shared flex class intended for the draggable production order caused the transfer-history row to stop behaving like a table row, collapsing its data beneath the full-width header. A table-specific class fixes the table without changing the production layout.

**How to apply:** Before reusing a presentation class on a table row, inspect all its selectors and usages. Keep the flex/grid class on its original component and use a distinct class for table rows.