---
name: Shared Events portal access
description: Product decision and safeguards for customer access to Events request history.
---

The customer Events portal intentionally uses a single configurable code per active public brand together with the customer email, rather than a separate code per request. A successful sign-in exposes the full request history for that email.

**Why:** The product priority is a simpler return-customer experience. The reduced exclusivity was explicitly accepted in exchange for avoiding a code per request.

**How to apply:** Keep the configured code stored only as a password hash and never prefill or expose it in HTML. Preserve the 30-minute session expiry, CSRF checks, login rate limiting, and access audit records. A direct post-submission link may remain scoped to its new request until the customer starts a normal history session.