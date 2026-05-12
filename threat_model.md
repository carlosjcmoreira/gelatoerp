# Threat Model

## Project Overview

Nivà Porto App is a Flask/PostgreSQL operations platform for the Nivà gelaterias in Porto. It handles production, sales, stock movements, invoices, payments, cash-flow forecasting, event/CRM workflows, and an internal AI assistant. The production app runs as a single Gunicorn-served Flask application with server-rendered Jinja templates and authenticated staff users.

This threat model is scoped to production-relevant behavior only. Mockup/sandbox code is out of scope unless production reachability is demonstrated. Replit-managed TLS is assumed in production.

## Assets

- **User accounts and sessions** — staff identities, permission flags, store assignments, and session cookies/tokens. Compromise allows impersonation and access to operational and financial functions.
- **Operational business data** — production plans, stock levels, transfers, store sales, and KPI data. Unauthorized changes can corrupt fulfillment, inventory, and profitability reporting.
- **Financial records** — invoices, payments, cash-flow forecasts, credit tracking, and related supplier/customer data. Exposure or tampering can cause direct financial harm.
- **CRM and event data** — leads, client names, email addresses, phone numbers, quotes, event notes, and payment state. This includes personal data and commercially sensitive pipeline information.
- **Uploaded documents and OCR outputs** — supplier invoices and extracted structured data. These documents may contain PII, tax identifiers, bank/payment details, and contract terms.
- **Application secrets and integrations** — `DATABASE_URL`, Flask secret material, Replit AI integration credentials, and optional Microsoft Graph credentials. Compromise would expose the database or third-party services.

## Trust Boundaries

- **Browser to Flask application** — all form fields, query params, and client-side navigation are untrusted. The server must authenticate and authorize every protected request.
- **Flask application to PostgreSQL** — the app has broad read/write access to business-critical tables. Injection or authorization failures at the app layer can expose or modify the full dataset.
- **Authenticated user to privileged user boundary** — the app uses per-user boolean permission flags (for example `acesso_gestor`, `acesso_financeiro`, `acesso_eventos`). Visibility decisions in the UI are not sufficient; privileged operations must be enforced server-side.
- **Flask application to external services** — OCR/AI calls and Microsoft Graph/Google Sheets integrations cross from trusted server code into third-party APIs and remote data stores.
- **Production to dev-only boundary** — any debug, bootstrap, or convenience flows (including auto-login style helpers) must be treated as unsafe unless explicitly proven unreachable in production.

## Scan Anchors

- **Production entry points:** `flask_app/app.py`, `flask_app/routes/`, `flask_app/services/`, `db/`.
- **Highest-risk areas:** auth/session handling (`flask_app/auth.py`, `flask_app/routes/auth.py`, `db/auth.py`), permissioned business routes in `flask_app/routes/`, CRM/events (`flask_app/routes/eventos.py`, `db/eventos.py`), finance/invoice flows (`flask_app/routes/compras.py`, `flask_app/routes/faturas.py`, `db/faturas.py`), and AI/integration code (`flask_app/routes/agente.py`, `flask_app/services/agente_ia.py`, OCR/OneDrive helpers).
- **Public surface:** `/login`, `/healthcheck`, and any explicitly unauthenticated routes registered from `flask_app/app.py`.
- **Authenticated surface:** nearly all business modules under `flask_app/routes/`.
- **Privileged surface:** gestor, financeiro, eventos, administrative data mutation routes, and any route relying on permission flags.
- **Usually ignore unless proven production-reachable:** mockup/sandbox helpers and development convenience flows controlled only by environment configuration.

## Threat Categories

### Spoofing

The application uses Flask session cookies and also stores server-side session records in PostgreSQL. Protected routes must require a currently valid server-side session tied to an active user account on every request; trusting only serialized user data in the client cookie is not sufficient. Any development or auto-login mechanism must be disabled or made unreachable in production.

### Tampering

Authenticated users can submit many business-critical mutations: transfers, production updates, invoice edits, event/CRM changes, and payment tracking. The server must enforce permission checks and business rules server-side for every mutation route. User-controlled inputs that reach SQL, file handling, OCR pipelines, or external integrations must be validated and constrained.

### Information Disclosure

The system stores staff, supplier, and customer information, including event leads and financial records. Routes must return only data the authenticated user is entitled to see. Error handling, logs, AI/OCR prompts, and integration responses must not leak secrets, credentials, or sensitive records beyond their intended audience.

### Denial of Service

Production availability depends on a single Flask process tier performing DB work plus potentially expensive OCR/AI operations. Upload sizes, external API calls, and expensive report/generation endpoints must remain bounded by size limits, timeouts, and authorization checks so ordinary users cannot trigger disproportionate load.

### Elevation of Privilege

The application relies heavily on boolean permission flags and module-specific access controls. Every privileged route must enforce the relevant permission on the server, not just hide links in navigation. Session revocation, account deactivation, and permission changes must take effect immediately for existing sessions so former or downgraded users cannot retain access.
