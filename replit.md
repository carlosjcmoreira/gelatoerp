# Nivà Porto App

Gerencia a rentabilidade das gelatarias Nivà no Porto, incluindo produção, vendas, compras, logística e finanças, com suporte a mobile-first e dark mode.

## Run & Operate

```bash
python flask_app/app.py
```

**Env Vars:**
- `DATABASE_URL`: PostgreSQL connection string.
- `AI_INTEGRATIONS_OPENAI_BASE_URL`, `AI_INTEGRATIONS_OPENAI_API_KEY`: For Replit AI Integrations (Anthropic Vision/Claude).
- `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`: (Optional) For Microsoft Graph API (OneDrive).

**Deployment:**
- Dev: `python flask_app/app.py` on port 5000.
- Prod: `gunicorn --bind=0.0.0.0:5000 --preload --timeout 120 flask_app.app:create_app()`

## Stack

- **Backend:** Flask (Python), Jinja2, gunicorn 25.1.0
- **Frontend:** Bootstrap 5, vanilla JS
- **Charts:** Plotly (client-side rendering)
- **Auth:** Flask sessions (secure cookies)
- **DB:** PostgreSQL (`psycopg2`)
- **Cache:** In-process TTL cache (`db/cache.py`)
- **OCR:** Anthropic Vision (claude-haiku-4-5) via Replit AI Integrations
- **AI Agent:** Anthropic (claude-sonnet-4-5) via Replit AI Integrations
- **Cloud Storage:** Microsoft Graph API (OneDrive)

## Where things live

- **App Entry:** `flask_app/app.py`
- **Routes:** `flask_app/routes/`
- **Templates:** `flask_app/templates/` (Base: `base.html`)
- **Static Assets:** `flask_app/static/`
- **Database Access:** `db/` package (source-of-truth: `db/schema.py` for DB schema)
- **Business Logic/Services:** `flask_app/services/`
- **Gunicorn Config:** `gunicorn.conf.py`
- **OCR Logic:** `flask_app/ocr_invoice.py`
- **OneDrive Archiving:** `flask_app/onedrive_archive.py`

## Architecture decisions

- **Mobile-First Design:** Implemented with Bootstrap 5 and custom CSS for optimal mobile experience.
- **Granular Permissions:** User access is controlled by boolean flags in the `users` table, allowing fine-grained control over module visibility.
- **In-Process Caching:** `db/cache.py` provides an efficient, dependency-free caching mechanism with TTL for frequently accessed data.
- **Agentic AI Integration:** An 8-tool AI agent leverages Anthropic's Claude for complex operations and conversational interaction.
- **OCR for Invoice Processing:** Anthropic Vision automates invoice data extraction with confidence scores, reducing manual entry.
- **Consolidated Transfer Workflow:** All stock movements (gelado, pastelaria, confeitaria, compras) are managed through a unified "Order of Transfer" system with `pendente` status and `data_prevista`.

## Product

- **Profitability Control:** Tracks Euro/kg KPI for gelaterias.
- **Production Management:** Oversees ice cream, pastry, and confectionery production.
- **Sales & Stock Management:** Records daily sales, manages stock levels, and handles transfers between stores.
- **Financial Management:** Modules for invoices (OCR, payments, IVA), credit responsibilities, and 13-week cash flow forecasting.
- **Sales Forecasting:** 5-step algorithm with meteorological adjustments and YoY factors for accurate predictions.
- **User & Permissions Management:** Admin-controlled user creation, password changes, and access rights.
- **AI-Powered Assistance:** Scoopy Agent for chat and operational assistance, AI for contract data extraction.
- **Task Management:** A module for managing recurring and ad-hoc tasks with status tracking.
- **Events CRM:** Pipeline management for customer events, lead tracking (Google Sheets integration), and quotation generation.

## User preferences

_Populate as you build_

## Gotchas

- **"Your Start application artifact encountered an error"**: This is a **TRANSIENT** error during `restart_workflow` in Replit preview. It's not a code error and resolves automatically. **Do NOT debug immediately after a workflow restart.**
- **Stock Calculations:** `Euro/kg` KPI calculation relies heavily on accurate `transferencias` data.
- **IVA Logic:** Currently estimates IVA based on sales, awaiting confirmation for specific tax rates (6%/13%/23%).
- **Tile Visibility:** New tiles are visible by default unless explicitly configured as hidden in `tile_config`.

## Pointers

- [Flask Documentation](https://flask.palletsprojects.com/)
- [Bootstrap 5 Documentation](https://getbootstrap.com/docs/5.3/getting-started/introduction/)
- [Plotly.js Documentation](https://plotly.com/javascript/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [Replit AI Integrations](https://docs.replit.com/ai/integrations)
- [Microsoft Graph API Documentation](https://learn.microsoft.com/en-us/graph/overview)
- [Gunicorn Documentation](https://gunicorn.org/)