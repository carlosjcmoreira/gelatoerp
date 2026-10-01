---
name: Cancellable form loading
description: Avoid leaving the global loading overlay active after a form submission is prevented by a confirmation prompt.
---

For forms whose submit handler can cancel navigation, opt out of the global submit-loading listener and start loading only after the local confirmation succeeds.

**Why:** The global document submit listener runs before a later page-level handler can prevent submission; a delayed overlay can remain visible after the user rejects the confirmation.

**How to apply:** Keep the opt-out local to cancellable forms, and make their accepted submit path explicitly start the normal loading state. Native validation should still prevent submit and leave no overlay.