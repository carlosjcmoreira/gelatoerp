"""Shared database operations for explicit sales-product dose associations."""


def close_current_association(cursor, product_id, effective_date):
    """End current mappings before effective_date, deleting same-day rows."""
    cursor.execute("""
        DELETE FROM produto_regra_dose_historico
        WHERE produto_vendas_config_id=%s
          AND valid_from >= %s
    """, (product_id, effective_date))
    cursor.execute("""
        UPDATE produto_regra_dose_historico
        SET valid_to=%s
        WHERE produto_vendas_config_id=%s
          AND valid_from < %s
          AND %s BETWEEN valid_from
              AND COALESCE(valid_to, 'infinity'::date)
    """, (
        effective_date.fromordinal(effective_date.toordinal() - 1),
        product_id, effective_date, effective_date,
    ))


def backfill_weight_sales(cursor, product_ids=None, rule_ids=None):
    """Copy imported kg quantities for sales covered by explicit weight rules."""
    filters = []
    params = []
    if product_ids:
        filters.append("prd.produto_vendas_config_id=ANY(%s)")
        params.append(list(product_ids))
    if rule_ids:
        filters.append("prd.regra_dose_id=ANY(%s)")
        params.append(list(rule_ids))
    scope = (" AND " + " AND ".join(filters)) if filters else ""
    cursor.execute("""
        UPDATE vendas_detalhe vd
        SET peso_vendido_kg=vd.quantidade
        FROM produto_regra_dose_historico prd
        JOIN gramas_gelado_historico hist
          ON hist.id=prd.regra_dose_id
        WHERE prd.produto_vendas_config_id=vd.produto_vendas_config_id
          AND vd.data BETWEEN prd.valid_from
              AND COALESCE(prd.valid_to, 'infinity'::date)
          AND hist.tipo_dose='peso'
          AND vd.peso_vendido_kg IS NULL
          AND vd.quantidade <> 0
    """ + scope, params)