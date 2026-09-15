---
name: Pastelaria access boundary
description: Authorization policy for features inside the Pastelaria module.
---

Anyone authorized for the Pastelaria module must have access to every function inside that module. Do not reserve individual Pastelaria pages or actions for Gestão, Administração, Produção, or another role.

**Why:** The user explicitly decided that the module permission is the complete authorization boundary and that role-based differences inside Pastelaria are unnecessary.

**How to apply:** Protect new Pastelaria features with the module permission, while keeping data validation, auditing, concurrency controls, and history protection independent of the user's role.