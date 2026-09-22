"""Shared database operations for explicit sales-product dose associations."""

from collections import defaultdict


def resolve_product_alias(product, aliases):
    """Resolve exact rename aliases; invalid or ambiguous chains stay unmapped."""
    targets = defaultdict(dict)
    for old_name, new_name in aliases:
        old_key = str(old_name or "")
        new_key = str(new_name or "")
        if old_key and new_key:
            targets[old_key][new_key] = new_key
    current_key = str(product or "")
    current_label = current_key
    visited = set()
    used_alias = False
    while current_key in targets:
        if current_key in visited or len(targets[current_key]) != 1:
            return None, "invalid_alias"
        visited.add(current_key)
        next_key, next_label = next(iter(targets[current_key].items()))
        if next_key == current_key or next_key in visited:
            return None, "invalid_alias"
        current_key, current_label = next_key, next_label
        used_alias = True
    return current_label, "alias" if used_alias else "original"


def get_product_family_ids(cursor, product_id):
    """Return one canonical product and all exact aliases pointing to it.

    The canonical name must resolve without using an alias and must identify one
    configuration row.  Ambiguous or invalid identity data is rejected instead
    of choosing a product by name.
    """
    cursor.execute("""
        SELECT id, produto
        FROM produtos_vendas_config
        WHERE id=%s
    """, (product_id,))
    product = cursor.fetchone()
    if not product:
        raise ValueError("Produto de vendas inexistente.")
    product_name = product["produto"] if isinstance(product, dict) else product[1]

    cursor.execute("""
        SELECT nome_antigo, nome_atual
        FROM produtos_vendas_aliases
        ORDER BY nome_antigo
    """)
    aliases = [
        (
            row["nome_antigo"] if isinstance(row, dict) else row[0],
            row["nome_atual"] if isinstance(row, dict) else row[1],
        )
        for row in cursor.fetchall()
    ]
    canonical_name, status = resolve_product_alias(product_name, aliases)
    if status != "original" or canonical_name is None:
        raise ValueError(
            "A configuração de dose deve usar um produto canónico válido."
        )

    cursor.execute("""
        SELECT id, produto
        FROM produtos_vendas_config
        ORDER BY id
    """)
    configs = cursor.fetchall()
    canonical_ids = [
        row["id"] if isinstance(row, dict) else row[0]
        for row in configs
        if (row["produto"] if isinstance(row, dict) else row[1])
        == canonical_name
    ]
    if len(canonical_ids) != 1 or canonical_ids[0] != product_id:
        raise ValueError(
            "A identidade canónica do produto é ambígua."
        )

    family_ids = []
    for row in configs:
        config_id = row["id"] if isinstance(row, dict) else row[0]
        config_name = row["produto"] if isinstance(row, dict) else row[1]
        resolved_name, _alias_status = resolve_product_alias(
            config_name, aliases
        )
        if resolved_name == canonical_name:
            family_ids.append(config_id)
    return family_ids


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