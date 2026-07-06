---
name: VAT/IVA rate policy for Nivà Porto App
description: How real IVA (VAT) is derived vs estimated in this project's finance module — check before touching any VAT/IVA calculation code.
---

Never hardcode or assume Portuguese VAT/IVA rates (6%/13%/23%) for this project without explicit confirmation. The user explicitly rejected assuming rates and instead requires deriving real IVA from actual data sources whenever possible.

**Why:** The user's real sales export file ("Evolução de Vendas por Produto", the Ravagnan POS export) contains a per-line "Valor Total S/IVA" (value excluding VAT) column alongside "Valor Total" (value including VAT). Real per-line IVA = `Valor Total - Valor Total S/IVA`. A sampled real rate came out to ~13%, not the previously assumed 6% — confirming hardcoded assumptions were wrong.

**How to apply:**
- Sales rows imported with the S/IVA column populate `valor_sem_iva_euros` on `vendas_detalhe`; VAT calculations must sum real per-row IVA for those rows and only fall back to a configurable estimated rate (`vat_config` table, `get_vat_config()`/`update_vat_config()` in `db/pagamentos.py`) for rows lacking real data (e.g. historical imports, or event/catering revenue which has no per-line IVA source yet).
- `compute_vat_period()` returns `is_estimate` / `pos_base_real` / `pos_base_fallback` so the UI can show whether a period's IVA figure is fully real, partially estimated, or fully estimated — always surface this distinction to the user rather than presenting a blended number as fact.
- Historical `vendas_detalhe` rows imported before this feature have `valor_sem_iva_euros IS NULL` (no real IVA data) — full validation against historical accounting figures requires the user to re-upload the original "Evolução de Vendas por Produto" files for those months.
