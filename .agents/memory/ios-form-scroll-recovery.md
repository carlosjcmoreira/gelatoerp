---
name: iOS form scroll recovery
description: Constraints for keyboard-related focus and submit visibility adjustments in long mobile forms.
---

Global keyboard recovery must never move a long form to a distant submit button when an intermediate field loses focus. Submit recovery is appropriate only after the final visible editable field, and delayed work must be cancelled whenever focus moves elsewhere.

**Why:** On iPhone, each quantity change in a long Pastelaria count grid triggered a focusout handler that jumped directly to the save button. Fixed navigation and changing visual viewport height made the displacement especially confusing.

**How to apply:** Exclude readonly and non-data controls when identifying the final field, use the visual viewport for clipping, and pass any required movement through nested scroll containers before scrolling the window. Preserve explicit opt-outs.