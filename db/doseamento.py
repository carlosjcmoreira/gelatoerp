"""Store gelato dose adherence.

The calculation is deliberately kept separate from the database reader.  Apart
from making the rules auditable, this makes it possible to test the arithmetic
with exports of the two source systems.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
import uuid

from psycopg2.extras import RealDictCursor

from db.connection import db_connection
from db.dose_associations import (
    backfill_weight_sales,
    close_current_association,
    get_product_family,
    get_product_family_ids,
    product_alias_issue,
    resolve_product_alias,
)
from db.gelato_rotation import get_gelato_stock_rotation


def _decimal(value, default=Decimal("0")):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else default
    except (InvalidOperation, TypeError, ValueError):
        return default


def _day(value):
    return value.date() if isinstance(value, datetime) else value


def load_dose_sales_with_rules(data_inicio=None, data_fim=None, loja=None):
    """Load Euro/kg sales and attach one explicit dated rule after alias resolution."""
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = []
        params = []
        if data_inicio is not None:
            conditions.append("vd.data >= %s")
            params.append(data_inicio)
        if data_fim is not None:
            conditions.append("vd.data <= %s")
            params.append(data_fim)
        if loja:
            conditions.append("vd.loja = %s")
            params.append(loja)
        where = " AND ".join(conditions) if conditions else "TRUE"
        cur.execute(f"""
            SELECT vd.data, vd.loja, vd.produto, vd.quantidade,
                   vd.valor_euros, vd.peso_vendido_kg,
                   vd.produto_vendas_config_id
            FROM vendas_detalhe vd
            WHERE {where}
            ORDER BY vd.data, vd.produto
        """, params)
        sales = [dict(row) for row in cur.fetchall()]
        cur.execute("""
            SELECT nome_antigo, nome_atual
            FROM produtos_vendas_aliases
            ORDER BY nome_antigo
        """)
        aliases = [(row["nome_antigo"], row["nome_atual"]) for row in cur.fetchall()]
        cur.execute("""
            SELECT id, produto, gelado_kpi, dose_config_pendente
            FROM produtos_vendas_config
        """)
        configs = [dict(row) for row in cur.fetchall()]
        reference_conditions = []
        reference_params = []
        if data_fim is not None:
            reference_conditions.append("prd.valid_from <= %s")
            reference_params.append(data_fim)
            reference_conditions.append("hist.valid_from <= %s")
            reference_params.append(data_fim)
        if data_inicio is not None:
            reference_conditions.append(
                "(prd.valid_to IS NULL OR prd.valid_to >= %s)"
            )
            reference_params.append(data_inicio)
            reference_conditions.append(
                "(hist.valid_to IS NULL OR hist.valid_to >= %s)"
            )
            reference_params.append(data_inicio)
        reference_where = (
            "WHERE " + " AND ".join(reference_conditions)
            if reference_conditions else ""
        )
        cur.execute(f"""
            SELECT prd.produto_vendas_config_id, prd.valid_from AS assoc_from,
                   prd.valid_to AS assoc_to, hist.id, hist.artigo, hist.gramas,
                   hist.tipo_dose, hist.valid_from, hist.valid_to
            FROM produto_regra_dose_historico prd
            JOIN gramas_gelado_historico hist ON hist.id=prd.regra_dose_id
            {reference_where}
            ORDER BY prd.produto_vendas_config_id, prd.valid_from
        """, reference_params)
        associations = [dict(row) for row in cur.fetchall()]
        history_conditions = []
        history_params = []
        if data_fim is not None:
            history_conditions.append("valid_from <= %s")
            history_params.append(data_fim)
        if data_inicio is not None:
            history_conditions.append("(valid_to IS NULL OR valid_to >= %s)")
            history_params.append(data_inicio)
        history_where = (
            "WHERE " + " AND ".join(history_conditions)
            if history_conditions else ""
        )
        cur.execute(f"""
            SELECT id, artigo, gramas, tipo_dose, valid_from, valid_to
            FROM gramas_gelado_historico
            {history_where}
            ORDER BY artigo, valid_from
        """, history_params)
        history = [dict(row) for row in cur.fetchall()]

    config_candidates = defaultdict(list)
    for config in configs:
        config_candidates[config["produto"]].append(config)
    associations_by_product = defaultdict(list)
    for association in associations:
        associations_by_product[association["produto_vendas_config_id"]].append(
            association
        )
    associated_ids = set(associations_by_product)
    configs_by_id = {config["id"]: config for config in configs}
    eligible_ids = {
        config["id"] for config in configs
        if config.get("gelado_kpi") or config["id"] in associated_ids
    }
    eligible_sales = []
    for sale in sales:
        canonical, alias_status = resolve_product_alias(sale["produto"], aliases)
        sale["canonical_produto"] = canonical or sale["produto"]
        sale["alias_status"] = alias_status
        if alias_status == "original":
            candidates = [
                configs_by_id[sale["produto_vendas_config_id"]]
            ] if sale.get("produto_vendas_config_id") in configs_by_id else []
        else:
            candidates = (
                config_candidates.get(canonical, [])
                if canonical is not None else []
            )
        resolved_config = candidates[0] if len(candidates) == 1 else None
        source_config = configs_by_id.get(sale.get("produto_vendas_config_id"))
        if not (
            (resolved_config and resolved_config["id"] in eligible_ids)
            or (source_config and source_config["id"] in eligible_ids)
        ):
            continue
        eligible_sales.append(sale)
        if len(candidates) != 1:
            sale["regra_dose_id"] = None
            continue
        sale_day = _day(sale["data"])
        association_owner_id = candidates[0]["id"]
        dated = [
            row for row in associations_by_product[association_owner_id]
            if _day(row["assoc_from"]) <= sale_day
            and (row["assoc_to"] is None or _day(row["assoc_to"]) >= sale_day)
            and _day(row["valid_from"]) <= sale_day
            and (row["valid_to"] is None or _day(row["valid_to"]) >= sale_day)
        ]
        if not dated and alias_status == "alias" and source_config:
            # Explicit aliases route new configuration through the canonical ID,
            # while retaining dated associations that predate consolidation.
            dated = [
                row for row in associations_by_product[source_config["id"]]
                if _day(row["assoc_from"]) <= sale_day
                and (
                    row["assoc_to"] is None
                    or _day(row["assoc_to"]) >= sale_day
                )
                and _day(row["valid_from"]) <= sale_day
                and (
                    row["valid_to"] is None
                    or _day(row["valid_to"]) >= sale_day
                )
            ]
        if len(dated) == 1:
            sale["regra_dose_id"] = dated[0]["id"]
            sale["dose_rule"] = dated[0]
        else:
            sale["regra_dose_id"] = None
    return eligible_sales, history


def get_vendas_ao_peso_sem_peso_calculavel(
    data_inicio=None, data_fim=None, loja=None
):
    """Return weight-configured sales whose stored kg cannot be calculated.

    This is intentionally a read-only diagnostic.  The source upload remains
    the authority for the weight, so this function never copies quantity into
    ``peso_vendido_kg`` or updates a sale.
    """
    if data_inicio is not None and data_fim is not None and data_inicio > data_fim:
        raise ValueError("A data inicial não pode ser posterior à data final.")

    sales, _history = load_dose_sales_with_rules(
        data_inicio=data_inicio, data_fim=data_fim, loja=loja
    )
    incomplete = []
    for sale in sales:
        rule = _explicit_rule(sale, _history)
        if not rule or str(rule.get("tipo_dose", "")).casefold() not in {
            "peso", "weight"
        }:
            continue
        weight = sale.get("peso_vendido_kg")
        if weight is not None:
            try:
                if Decimal(str(weight)).is_finite():
                    continue
            except (InvalidOperation, TypeError, ValueError):
                pass
        incomplete.append({
            "data": _day(sale.get("data")),
            "loja": sale.get("loja"),
            "artigo": sale.get("produto"),
            "quantidade": sale.get("quantidade"),
        })
    return incomplete


def get_dose_alias_coverage_alerts(
    data_inicio=None, data_fim=None, loja=None
):
    """Return exact aliases that leave selected historical sales unmapped.

    This is a read-only manager diagnostic.  It deliberately uses the imported
    sale's stable configuration ID and exact alias names; no text similarity is
    used to decide whether an alias belongs to the Euro/kg scope.
    """
    if data_inicio is not None and data_fim is not None and data_inicio > data_fim:
        raise ValueError("A data inicial não pode ser posterior à data final.")

    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["a.nome_antigo = vd.produto"]
        params = []
        if data_inicio is not None:
            conditions.append("vd.data >= %s")
            params.append(data_inicio)
        if data_fim is not None:
            conditions.append("vd.data <= %s")
            params.append(data_fim)
        if loja:
            conditions.append("vd.loja = %s")
            params.append(loja)
        cur.execute(f"""
            SELECT a.nome_antigo AS alias_name,
                   COUNT(*) AS sales_count,
                   vd.produto_vendas_config_id AS source_config_id,
                   COALESCE(source.gelado_kpi, FALSE) AS source_selected,
                   EXISTS (
                       SELECT 1
                       FROM produto_regra_dose_historico source_prd
                       WHERE source_prd.produto_vendas_config_id =
                             vd.produto_vendas_config_id
                   ) AS source_has_dose_history
            FROM produtos_vendas_aliases a
            JOIN vendas_detalhe vd
              ON vd.produto = a.nome_antigo
            LEFT JOIN produtos_vendas_config source
              ON source.id = vd.produto_vendas_config_id
            WHERE {" AND ".join(conditions)}
            GROUP BY a.nome_antigo, vd.produto_vendas_config_id,
                     source.gelado_kpi
            ORDER BY a.nome_antigo, vd.produto_vendas_config_id
        """, params)
        sale_rows = [dict(row) for row in cur.fetchall()]

        cur.execute("""
            SELECT id, produto, gelado_kpi
            FROM produtos_vendas_config
        """)
        configs = [dict(row) for row in cur.fetchall()]
        cur.execute("""
            SELECT nome_antigo, nome_atual
            FROM produtos_vendas_aliases
            ORDER BY nome_antigo, nome_atual
        """)
        aliases = [
            (row["nome_antigo"], row["nome_atual"])
            for row in cur.fetchall()
        ]
        cur.execute("""
            SELECT DISTINCT produto_vendas_config_id
            FROM produto_regra_dose_historico
            WHERE produto_vendas_config_id IS NOT NULL
        """)
        historical_dose_ids = {
            row["produto_vendas_config_id"] for row in cur.fetchall()
        }

    config_by_name = defaultdict(list)
    config_by_id = {}
    for config in configs:
        config_by_name[config["produto"]].append(config)
        config_by_id[config["id"]] = config

    direct_targets = defaultdict(set)
    for old_name, new_name in aliases:
        direct_targets[old_name].add(new_name)

    alerts = {}
    for row in sale_rows:
        source_id = row["source_config_id"]
        source = config_by_id.get(source_id)
        source_in_scope = bool(
            row["source_selected"] or row["source_has_dose_history"]
            or source_id in historical_dose_ids
        )
        resolved_name, alias_status = resolve_product_alias(
            row["alias_name"], aliases
        )
        issue = product_alias_issue(row["alias_name"], aliases)
        target_configs = (
            config_by_name.get(resolved_name, [])
            if resolved_name is not None else []
        )
        target_in_scope = any(
            config.get("gelado_kpi")
            or config["id"] in historical_dose_ids
            for config in target_configs
        )
        if not source_in_scope and not target_in_scope:
            continue

        # A resolved alias is still blocked when its terminal identity is not
        # exactly one configured product.  More than one is ambiguous; no
        # product at all is an invalid historical alias.
        if issue is None and alias_status == "alias" and len(target_configs) != 1:
            issue = (
                "ambiguous_alias" if len(target_configs) > 1
                else "invalid_alias"
            )
        if issue is None:
            continue

        alert = alerts.setdefault(row["alias_name"], {
            "alias": row["alias_name"],
            "destinations": sorted(direct_targets[row["alias_name"]]),
            "issue": issue,
            "sales_count": 0,
        })
        alert["sales_count"] += int(row["sales_count"])

    return sorted(alerts.values(), key=lambda item: item["alias"])


def _matches(product, history, sale_date):
    """Migration-only textual suggestion; KPI calculations never call this."""
    product = str(product or "").casefold()
    candidates = [
        (len(str(row.get("artigo", "")).strip()), row)
        for row in history
        if str(row.get("artigo", "")).strip().casefold() in product
        and (row.get("valid_from") is None or _day(row["valid_from"]) <= sale_date)
        and (row.get("valid_to") is None or _day(row["valid_to"]) >= sale_date)
    ]
    if not candidates:
        return None
    longest = max(length for length, _ in candidates)
    winners = [row for length, row in candidates if length == longest]
    return winners[0] if len(winners) == 1 else None


def _explicit_rule(sale, history):
    rule = sale.get("dose_rule")
    sale_date = _day(sale.get("data"))
    if rule:
        candidate = rule
    else:
        rule_id = sale.get("regra_dose_id")
        if rule_id is None:
            return None
        candidate = next(
            (row for row in history if row.get("id") == rule_id), None
        )
        if candidate is None and len(history) == 1 and history[0].get("id") is None:
            candidate = history[0]
    if not candidate:
        return None
    if sale_date is not None:
        if (candidate.get("valid_from") is not None
                and _day(candidate["valid_from"]) > sale_date):
            return None
        if (candidate.get("valid_to") is not None
                and _day(candidate["valid_to"]) < sale_date):
            return None
    return candidate


def get_dose_product_configuration_queue():
    """Return only products explicitly selected for Euro/kg.

    This is deliberately a pure read. A missing dose is a configuration state,
    not a reason to silently undo the manager's product selection.
    """
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT pvc.id, pvc.produto,
                   (current_rule.regra_dose_id IS NULL)
                       AS dose_config_pendente,
                   current_rule.regra_dose_id AS current_rule_id,
                   current_rule.gramas,
                   current_rule.tipo_dose,
                   current_rule.valid_from,
                   EXISTS (
                       SELECT 1
                       FROM produto_regra_dose_historico prd_any
                       WHERE prd_any.produto_vendas_config_id=pvc.id
                   ) AS dose_history_exists,
                   first_sale.first_sale
            FROM produtos_vendas_config pvc
            LEFT JOIN LATERAL (
                SELECT prd.regra_dose_id, hist.gramas, hist.tipo_dose,
                       GREATEST(prd.valid_from, hist.valid_from) AS valid_from
                FROM produto_regra_dose_historico prd
                JOIN gramas_gelado_historico hist
                  ON hist.id=prd.regra_dose_id
                WHERE prd.produto_vendas_config_id=pvc.id
                  AND CURRENT_DATE BETWEEN prd.valid_from
                      AND COALESCE(prd.valid_to, 'infinity'::date)
                  AND CURRENT_DATE BETWEEN hist.valid_from
                      AND COALESCE(hist.valid_to, 'infinity'::date)
                ORDER BY prd.valid_from DESC LIMIT 1
            ) current_rule ON TRUE
            LEFT JOIN LATERAL (
                SELECT MIN(vd.data) AS first_sale
                FROM vendas_detalhe vd
                WHERE vd.produto_vendas_config_id=pvc.id
            ) first_sale ON TRUE
            WHERE pvc.gelado_kpi=TRUE
              AND NOT EXISTS (
                SELECT 1
                FROM produtos_vendas_aliases alias
                JOIN produtos_vendas_config canonical
                  ON canonical.produto=alias.nome_atual
                 AND canonical.gelado_kpi=TRUE
                WHERE alias.nome_antigo=pvc.produto
              )
            ORDER BY pvc.produto
        """)
        products = [dict(row) for row in cur.fetchall()]
        for product in products:
            try:
                family = get_product_family(cur, product["id"])
            except ValueError:
                # The queue is a read-only diagnostic.  Keep its own-ID date
                # when legacy/ambiguous identity data cannot be consolidated.
                product["canonical_product"] = {
                    "id": product["id"],
                    "produto": product["produto"],
                    "is_canonical": True,
                }
                product["aliases"] = []
                continue
            product["canonical_product"] = family["canonical_product"]
            product["aliases"] = family["aliases"]
            family_ids = [
                family["canonical_product"]["id"],
                *(alias["id"] for alias in family["aliases"]),
            ]
            cur.execute("""
                SELECT MIN(data) AS first_sale
                FROM vendas_detalhe
                WHERE produto_vendas_config_id=ANY(%s)
            """, (family_ids,))
            product["first_sale"] = cur.fetchone()["first_sale"]
    return products, []


def set_typology_dose(
    typology, grams, actor, source="Pastelaria", old_typology=None
):
    """Version one typology dose and rebind only explicitly related products."""
    typology = str(typology or "").strip()
    grams = _decimal(grams, None)
    if not typology or grams is None or grams < 0:
        raise ValueError("Indique uma tipologia e um peso válidos.")
    today = date.today()
    old_typology = str(old_typology or typology).strip()
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("dose-config-writes",),
            )
            if old_typology != typology:
                cur.execute("""
                    UPDATE gelado_por_tipologia
                    SET tipologia=%s, quantidade_gelado_g=%s
                    WHERE tipologia=%s
                """, (typology, grams, old_typology))
            cur.execute("""
                INSERT INTO gelado_por_tipologia (
                    tipologia, quantidade_gelado_g
                ) VALUES (%s, %s)
                ON CONFLICT (tipologia) DO UPDATE
                SET quantidade_gelado_g=EXCLUDED.quantidade_gelado_g
            """, (typology, grams))
            cur.execute("""
                SELECT DISTINCT pvc.id
                FROM produtos_vendas_config pvc
                LEFT JOIN produto_regra_dose_historico prd
                  ON prd.produto_vendas_config_id=pvc.id
                 AND CURRENT_DATE BETWEEN prd.valid_from
                     AND COALESCE(prd.valid_to, 'infinity'::date)
                LEFT JOIN gramas_gelado_historico hist
                  ON hist.id=prd.regra_dose_id
                 AND CURRENT_DATE BETWEEN hist.valid_from
                     AND COALESCE(hist.valid_to, 'infinity'::date)
                WHERE pvc.gelado_kpi=TRUE
                  AND (
                    pvc.produto=%s
                    OR LOWER(BTRIM(hist.artigo)) IN (
                        LOWER(BTRIM(%s)), LOWER(BTRIM(%s))
                    )
                  )
            """, (typology, typology, old_typology))
            product_ids = [row["id"] for row in cur.fetchall()]
            if product_ids:
                cur.execute("""
                    SELECT id FROM produtos_vendas_config
                    WHERE id=ANY(%s) FOR UPDATE
                """, (product_ids,))
                cur.fetchall()
            for product_id in product_ids:
                close_current_association(cur, product_id, today)

            cur.execute("""
                UPDATE gramas_gelado_historico
                SET valid_to=%s
                WHERE LOWER(BTRIM(artigo))=LOWER(BTRIM(%s))
                  AND valid_to IS NULL AND valid_from < %s
            """, (today - timedelta(days=1), typology, today))
            cur.execute("""
                DELETE FROM gramas_gelado_historico
                WHERE LOWER(BTRIM(artigo))=LOWER(BTRIM(%s))
                  AND valid_to IS NULL AND valid_from=%s
                  AND NOT EXISTS (
                    SELECT 1 FROM produto_regra_dose_historico prd
                    WHERE prd.regra_dose_id=gramas_gelado_historico.id
                )
            """, (typology, today))
            rule_id = None
            if grams > 0:
                cur.execute("""
                    INSERT INTO gramas_gelado_historico (
                        artigo, gramas, tipo_dose, valid_from,
                        created_by, evidence_reference
                    ) VALUES (%s, %s, 'fixa', %s, %s, %s)
                    RETURNING id
                """, (
                    typology, grams, today, actor,
                    f"Configuração por tipologia: {source}",
                ))
                rule_id = cur.fetchone()["id"]
                if product_ids:
                    cur.execute("""
                        INSERT INTO produto_regra_dose_historico (
                            produto_vendas_config_id, regra_dose_id,
                            valid_from, created_by
                        )
                        SELECT product_id, %s, %s, %s
                        FROM UNNEST(%s::integer[]) product_id
                    """, (rule_id, today, actor, product_ids))
                    cur.execute("""
                        UPDATE produtos_vendas_config
                        SET gelado_kpi=TRUE, dose_config_pendente=FALSE
                        WHERE id=ANY(%s)
                    """, (product_ids,))
            elif product_ids:
                cur.execute("""
                    UPDATE produtos_vendas_config
                    SET dose_config_pendente=TRUE
                    WHERE id=ANY(%s)
                """, (product_ids,))
            cur.execute("""
                INSERT INTO gramas_gelado (artigo, gramas)
                VALUES (%s, %s)
                ON CONFLICT (artigo) DO UPDATE SET gramas=EXCLUDED.gramas
            """, (typology, grams if grams > 0 else 0))
            conn.commit()
            return rule_id, len(product_ids)
        except Exception:
            conn.rollback()
            raise


def _set_product_dose_with_connection(
    conn, product_id, grams, dose_type, actor, source, effective_from=None
):
    """Apply one product dose without committing the surrounding transaction.

    A product's first dose is the rule for all sales already recorded for that
    canonical product and its exact rename aliases. Later changes are dated
    versions and preserve the previous rule before their effective date.
    """
    cur = conn.cursor(cursor_factory=RealDictCursor)
    today = date.today()
    if effective_from is not None and not isinstance(effective_from, date):
        try:
            effective_from = date.fromisoformat(str(effective_from))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Indique uma data de entrada em vigor válida."
            ) from exc
    if effective_from is not None and effective_from > today:
        raise ValueError(
            "A data de entrada em vigor não pode ser futura."
        )
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("dose-config-writes",),
    )
    cur.execute("""
        SELECT id, produto, gelado_kpi
        FROM produtos_vendas_config
        WHERE id=%s FOR UPDATE
    """, (product_id,))
    product = cur.fetchone()
    if not product:
        raise ValueError("Artigo faturado inexistente.")
    if not product["gelado_kpi"]:
        raise ValueError(
            "Selecione primeiro este artigo no tile Euro/kg do Gestor."
        )

    cur.execute("""
        SELECT prd.regra_dose_id, hist.artigo, hist.gramas,
               hist.tipo_dose
        FROM produto_regra_dose_historico prd
        JOIN gramas_gelado_historico hist
          ON hist.id=prd.regra_dose_id
        WHERE prd.produto_vendas_config_id=%s
          AND CURRENT_DATE BETWEEN prd.valid_from
              AND COALESCE(prd.valid_to, 'infinity'::date)
          AND CURRENT_DATE BETWEEN hist.valid_from
              AND COALESCE(hist.valid_to, 'infinity'::date)
        ORDER BY prd.valid_from DESC LIMIT 1
    """, (product_id,))
    current = cur.fetchone()

    cur.execute("""
        SELECT EXISTS (
            SELECT 1
            FROM produto_regra_dose_historico
            WHERE produto_vendas_config_id=%s
        ) AS has_history
    """, (product_id,))
    has_history = bool(cur.fetchone()["has_history"])

    if current and current["tipo_dose"] == dose_type and (
        dose_type == "peso" or _decimal(current["gramas"]) == grams
    ):
        cur.execute("""
            UPDATE produtos_vendas_config
            SET dose_config_pendente=FALSE
            WHERE id=%s
        """, (product_id,))
        return current["regra_dose_id"]

    article = product["produto"]

    if not has_history:
        family_ids = get_product_family_ids(cur, product_id)
        cur.execute("""
            SELECT MIN(data) AS first_sale
            FROM vendas_detalhe
            WHERE produto_vendas_config_id=ANY(%s)
        """, (family_ids,))
        first_sale = cur.fetchone()["first_sale"]
        effective_from = _day(first_sale) if first_sale is not None else today
    elif effective_from is None:
        raise ValueError(
            "Este artigo já tem uma dose configurada. "
            "Indique a data a partir da qual o novo valor vigora."
        )

    cur.execute("""
        SELECT MIN(valid_from) AS next_start
        FROM produto_regra_dose_historico
        WHERE produto_vendas_config_id=%s AND valid_from > %s
    """, (product_id, effective_from))
    next_association_start = cur.fetchone()["next_start"]

    cur.execute("""
        SELECT MIN(valid_from) AS next_start
        FROM gramas_gelado_historico
        WHERE LOWER(BTRIM(artigo))=LOWER(BTRIM(%s))
          AND valid_from > %s
    """, (article, effective_from))
    next_rule_start = cur.fetchone()["next_start"]

    next_starts = [
        value for value in (next_association_start, next_rule_start)
        if value is not None
    ]
    new_valid_to = (
        min(_day(value) for value in next_starts) - timedelta(days=1)
        if next_starts else None
    )

    # Keep already scheduled versions after the selected date. The exclusion
    # constraint below then guarantees that this new interval cannot overlap.
    cur.execute("""
        DELETE FROM produto_regra_dose_historico
        WHERE produto_vendas_config_id=%s
          AND valid_from=%s
    """, (product_id, effective_from))
    cur.execute("""
        UPDATE produto_regra_dose_historico
        SET valid_to=%s
        WHERE produto_vendas_config_id=%s
          AND valid_from < %s
          AND (valid_to IS NULL OR valid_to >= %s)
    """, (
        effective_from - timedelta(days=1), product_id,
        effective_from, effective_from,
    ))

    cur.execute("""
        UPDATE gramas_gelado_historico
        SET valid_to=%s
        WHERE LOWER(BTRIM(artigo))=LOWER(BTRIM(%s))
          AND valid_from < %s
          AND (valid_to IS NULL OR valid_to >= %s)
    """, (
        effective_from - timedelta(days=1), article,
        effective_from, effective_from,
    ))
    cur.execute("""
        DELETE FROM gramas_gelado_historico
        WHERE LOWER(BTRIM(artigo))=LOWER(BTRIM(%s))
          AND valid_from=%s
          AND NOT EXISTS (
              SELECT 1 FROM produto_regra_dose_historico prd
              WHERE prd.regra_dose_id=gramas_gelado_historico.id
          )
    """, (article, effective_from))
    cur.execute("""
        INSERT INTO gramas_gelado_historico (
            artigo, gramas, tipo_dose, valid_from, valid_to,
            created_by, evidence_reference
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        article, grams, dose_type, effective_from, new_valid_to, actor,
        f"Configuração direta: {source}",
    ))
    rule_id = cur.fetchone()["id"]
    cur.execute("""
        INSERT INTO produto_regra_dose_historico (
            produto_vendas_config_id, regra_dose_id,
            valid_from, valid_to, created_by
        ) VALUES (%s, %s, %s, %s, %s)
    """, (
        product_id, rule_id, effective_from, new_valid_to, actor,
    ))
    cur.execute("""
        INSERT INTO gramas_gelado (artigo, gramas)
        VALUES (%s, %s)
        ON CONFLICT (artigo) DO UPDATE SET gramas=EXCLUDED.gramas
    """, (article, grams if dose_type == "fixa" else 1))
    backfill_weight_sales(
        cur, product_ids=[product_id], rule_ids=[rule_id]
    )
    cur.execute("""
        UPDATE produtos_vendas_config
        SET gelado_kpi=TRUE, dose_config_pendente=FALSE
        WHERE id=%s
    """, (product_id,))
    return rule_id


def set_product_dose(
    product_id, grams, dose_type, actor, source="Euro/kg", conn=None,
    effective_from=None,
):
    """Set a product dose, optionally inside a caller-owned transaction.

    The first rule starts at the first recorded sale in the product's exact
    canonical alias family. A later replacement requires ``effective_from`` so
    the prior period is preserved.
    """
    dose_type = str(dose_type or "fixa")
    if dose_type not in {"fixa", "peso"}:
        raise ValueError("Tipo de venda inválido.")
    grams = None if dose_type == "peso" else _decimal(grams, None)
    if dose_type == "fixa" and (grams is None or grams <= 0):
        raise ValueError("Indique um valor de gramas superior a zero.")

    if conn is not None:
        return _set_product_dose_with_connection(
            conn, product_id, grams, dose_type, actor, source, effective_from
        )

    with db_connection() as owned_conn:
        try:
            rule_id = _set_product_dose_with_connection(
                owned_conn, product_id, grams, dose_type, actor, source,
                effective_from,
            )
            owned_conn.commit()
            return rule_id
        except Exception:
            owned_conn.rollback()
            raise


def configure_dose_product(product_id, rule_id, actor):
    """Activate a queued product using explicit IDs for every rule version."""
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute("""
                SELECT id FROM produtos_vendas_config
                WHERE id=%s FOR UPDATE
            """, (product_id,))
            if not cur.fetchone():
                raise ValueError("Produto de vendas inexistente.")
            cur.execute("""
                SELECT artigo, valid_from, valid_to
                FROM gramas_gelado_historico
                WHERE id=%s
            """, (rule_id,))
            selected = cur.fetchone()
            if not selected:
                raise ValueError("Regra de dose inexistente.")
            today = date.today()
            if (_day(selected["valid_from"]) > today
                    or (
                        selected["valid_to"] is not None
                        and _day(selected["valid_to"]) < today
                    )):
                raise ValueError("Escolha uma versão de dose atualmente vigente.")
            cur.execute("""
                SELECT id, valid_from, valid_to
                FROM gramas_gelado_historico
                WHERE LOWER(BTRIM(artigo)) = LOWER(BTRIM(%s))
                ORDER BY valid_from
            """, (selected["artigo"],))
            versions = cur.fetchall()
            cur.execute("""
                SELECT id, regra_dose_id, valid_from, valid_to
                FROM produto_regra_dose_historico
                WHERE produto_vendas_config_id=%s
                ORDER BY valid_from
            """, (product_id,))
            associations = cur.fetchall()
            has_association_history = bool(associations)
            versions_to_insert = versions
            if has_association_history:
                close_current_association(cur, product_id, today)
                versions_to_insert = []
                for version in versions:
                    version_start = _day(version["valid_from"])
                    version_end = (
                        _day(version["valid_to"])
                        if version["valid_to"] is not None else None
                    )
                    if version_end is not None and version_end < today:
                        continue
                    versions_to_insert.append({
                        "id": version["id"],
                        "valid_from": max(version_start, today),
                        "valid_to": version_end,
                    })
            for version in versions_to_insert:
                cur.execute("""
                    INSERT INTO produto_regra_dose_historico (
                        produto_vendas_config_id, regra_dose_id,
                        valid_from, valid_to, created_by
                    ) VALUES (%s, %s, %s, %s, %s)
                """, (
                    product_id, version["id"], version["valid_from"],
                    version["valid_to"], actor,
                ))
            backfill_weight_sales(cur, product_ids=[product_id])
            cur.execute("""
                UPDATE produtos_vendas_config
                SET gelado_kpi=TRUE, dose_config_pendente=FALSE
                WHERE id=%s
            """, (product_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def preview_historical_dose_csv(contents):
    """Validate and normalize a dated evidence CSV without writing anything."""
    if isinstance(contents, bytes):
        contents = contents.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(contents or ""))
    required = {"artigo", "tipo_dose", "gramas", "data_efetiva", "evidencia"}
    headers = {
        str(value or "").lstrip("\ufeff").strip().casefold()
        for value in (reader.fieldnames or [])
    }
    if not required.issubset(headers):
        raise ValueError(
            "O CSV deve ter as colunas artigo, tipo_dose, gramas, "
            "data_efetiva e evidencia."
        )
    rows = []
    seen = set()
    for line_number, raw in enumerate(reader, start=2):
        if len(rows) >= 200:
            raise ValueError("O CSV não pode ter mais de 200 doses.")
        normalized = {
            str(key or "").lstrip("\ufeff").strip().casefold(): str(value or "").strip()
            for key, value in raw.items()
        }
        artigo = normalized["artigo"]
        tipo = normalized["tipo_dose"].casefold()
        evidencia = normalized["evidencia"]
        try:
            effective = date.fromisoformat(normalized["data_efetiva"])
        except ValueError as exc:
            raise ValueError(f"Linha {line_number}: data efetiva inválida.") from exc
        if not artigo or not evidencia:
            raise ValueError(f"Linha {line_number}: artigo e evidência são obrigatórios.")
        if len(artigo) > 255 or len(evidencia) > 500:
            raise ValueError(
                f"Linha {line_number}: artigo ou evidência excede o tamanho permitido."
            )
        if effective > date.today():
            raise ValueError(f"Linha {line_number}: a data efetiva não pode ser futura.")
        if tipo not in ("fixa", "peso"):
            raise ValueError(f"Linha {line_number}: tipo_dose deve ser fixa ou peso.")
        grams = None
        if tipo == "fixa":
            grams = _decimal(normalized["gramas"].replace(",", "."), None)
            if grams is None or grams <= 0:
                raise ValueError(f"Linha {line_number}: gramas deve ser maior que zero.")
        key = (artigo.casefold(), effective)
        if key in seen:
            raise ValueError(
                f"Linha {line_number}: artigo e data efetiva repetidos no ficheiro."
            )
        seen.add(key)
        rows.append({
            "artigo": artigo,
            "tipo_dose": tipo,
            "gramas": str(grams) if grams is not None else None,
            "valid_from": effective.isoformat(),
            "evidence_reference": evidencia,
        })
    if not rows:
        raise ValueError("O CSV não contém doses.")
    return rows


def _history_snapshot(cur, rows, lock=False):
    articles = sorted({row["artigo"].strip().casefold() for row in rows})
    query = """
        SELECT id, artigo, gramas, tipo_dose, valid_from, valid_to,
               evidence_reference
        FROM gramas_gelado_historico
        WHERE LOWER(BTRIM(artigo)) = ANY(%s)
        ORDER BY LOWER(BTRIM(artigo)), valid_from, id
    """
    if lock:
        query += " FOR UPDATE"
    cur.execute(query, (articles,))
    snapshot = [dict(value) for value in cur.fetchall()]
    payload = json.dumps(snapshot, sort_keys=True, default=str, ensure_ascii=False)
    return snapshot, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _preview_interval_changes(rows, snapshot):
    changes = []
    for row in sorted(rows, key=lambda item: (
            item["artigo"].casefold(), item["valid_from"])):
        effective = date.fromisoformat(row["valid_from"])
        versions = [
            value for value in snapshot
            if value["artigo"].strip().casefold() == row["artigo"].strip().casefold()
        ]
        if any(_day(value["valid_from"]) == effective for value in versions):
            raise ValueError(
                f"Já existe uma versão de '{row['artigo']}' em {effective}."
            )
        snapshot.append({
            "id": None, "artigo": row["artigo"], "gramas": row["gramas"],
            "tipo_dose": row["tipo_dose"], "valid_from": effective,
            "valid_to": None, "evidence_reference": row["evidence_reference"],
            "_imported": True,
        })
    for row in sorted(rows, key=lambda item: (
            item["artigo"].casefold(), item["valid_from"])):
        effective = date.fromisoformat(row["valid_from"])
        versions = sorted([
            value for value in snapshot
            if value["artigo"].strip().casefold() == row["artigo"].strip().casefold()
        ], key=lambda value: _day(value["valid_from"]))
        index = next(
            i for i, value in enumerate(versions)
            if _day(value["valid_from"]) == effective and value.get("_imported")
        )
        previous = versions[index - 1] if index else None
        next_version = versions[index + 1] if index + 1 < len(versions) else None
        end = (
            _day(next_version["valid_from"]) - timedelta(days=1)
            if next_version else None
        )
        changes.append({
            **row,
            "valid_to": end.isoformat() if end else None,
            "shortens_previous": (
                _day(previous["valid_from"]).isoformat()
                if previous and (
                    previous.get("_imported")
                    or previous["valid_to"] is None
                    or _day(previous["valid_to"]) >= effective
                ) else None
            ),
            "shortens_imported_row": bool(previous and previous.get("_imported")),
        })
    return changes


def create_historical_dose_preview(contents, actor, source_name=None):
    """Persist a short-lived, actor-bound preview and its exact interval effects."""
    rows = preview_historical_dose_csv(contents)
    preview_id = str(uuid.uuid4())
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        snapshot, history_hash = _history_snapshot(cur, rows)
        changes = _preview_interval_changes(rows, list(snapshot))
        cur.execute("""
            DELETE FROM gramas_gelado_import_preview
            WHERE expires_at < NOW() OR consumed_at IS NOT NULL
        """)
        cur.execute("""
            INSERT INTO gramas_gelado_import_preview
                (id, created_by, expires_at, source_name, rows_json,
                 changes_json, history_hash)
            VALUES (%s, %s, NOW() + INTERVAL '30 minutes', %s,
                    %s::jsonb, %s::jsonb, %s)
        """, (
            preview_id, actor[:100], source_name,
            json.dumps(rows, ensure_ascii=False),
            json.dumps(changes, ensure_ascii=False), history_hash,
        ))
        conn.commit()
    return {"id": preview_id, "rows": rows, "changes": changes,
            "source_name": source_name}


def get_historical_dose_preview(preview_id, actor):
    if not preview_id:
        return None
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, source_name, rows_json, changes_json
            FROM gramas_gelado_import_preview
            WHERE id = %s AND created_by = %s AND consumed_at IS NULL
              AND expires_at > NOW()
        """, (preview_id, actor[:100]))
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": str(row["id"]), "source_name": row["source_name"],
        "rows": row["rows_json"], "changes": row["changes_json"],
    }


def confirm_historical_dose_preview(preview_id, actor):
    """Consume a preview once, rejecting it if affected history has changed."""
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute("""
                SELECT * FROM gramas_gelado_import_preview
                WHERE id = %s AND created_by = %s AND consumed_at IS NULL
                  AND expires_at > NOW()
                FOR UPDATE
            """, (preview_id, actor[:100]))
            preview = cur.fetchone()
            if not preview:
                raise ValueError("A pré-visualização expirou ou já foi utilizada.")
            rows = preview["rows_json"]
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("dose-config-writes",),
            )
            for article in sorted({row["artigo"].strip().casefold() for row in rows}):
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("dose-history:" + article,),
                )
            _snapshot, current_hash = _history_snapshot(cur, rows, lock=True)
            if current_hash != preview["history_hash"]:
                raise ValueError(
                    "As versões mudaram desde a pré-visualização. "
                    "Volte a pré-visualizar o ficheiro."
                )
            batch_id = _import_historical_doses_tx(
                cur, rows, actor[:100], preview["source_name"]
            )
            cur.execute("""
                UPDATE gramas_gelado_import_preview
                SET consumed_at = NOW() WHERE id = %s
            """, (preview_id,))
            conn.commit()
            return batch_id
        except Exception:
            conn.rollback()
            raise


def _import_historical_doses_tx(cur, normalized, actor, source_name):
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("dose-config-writes",),
    )
    batch_id = str(uuid.uuid4())
    audit_changes = []
    for row in sorted(normalized, key=lambda item: (
            item["artigo"].casefold(), item["valid_from"])):
        effective = date.fromisoformat(row["valid_from"])
        cur.execute("""
            SELECT id, valid_from, valid_to
            FROM gramas_gelado_historico
            WHERE LOWER(BTRIM(artigo)) = LOWER(BTRIM(%s))
            ORDER BY valid_from FOR UPDATE
        """, (row["artigo"],))
        versions = [dict(value) for value in cur.fetchall()]
        if any(_day(value["valid_from"]) == effective for value in versions):
            raise ValueError(
                f"Já existe uma versão de '{row['artigo']}' em {effective}."
            )
        next_version = next(
            (value for value in versions if _day(value["valid_from"]) > effective),
            None,
        )
        previous = next(
            (value for value in reversed(versions)
             if _day(value["valid_from"]) < effective), None,
        )
        valid_to = (
            _day(next_version["valid_from"]) - timedelta(days=1)
            if next_version else None
        )
        continuing_product_ids = []
        future_association_ids = []
        if previous and (
            previous["valid_to"] is None or _day(previous["valid_to"]) >= effective
        ):
            old_valid_to = previous["valid_to"]
            cur.execute("""
                SELECT id, produto_vendas_config_id, valid_from
                FROM produto_regra_dose_historico
                WHERE regra_dose_id=%s
                  AND (valid_to IS NULL OR valid_to >= %s)
            """, (previous["id"], effective))
            affected_associations = cur.fetchall()
            continuing_product_ids = [
                value["produto_vendas_config_id"]
                for value in affected_associations
                if _day(value["valid_from"]) < effective
            ]
            future_association_ids = [
                value["id"] for value in affected_associations
                if _day(value["valid_from"]) >= effective
            ]
            cur.execute(
                """UPDATE gramas_gelado_historico
                   SET valid_to = %s WHERE id = %s RETURNING id""",
                (effective - timedelta(days=1), previous["id"]),
            )
            cur.fetchone()
            cur.execute("""
                UPDATE produto_regra_dose_historico
                SET valid_to=%s
                WHERE regra_dose_id=%s
                  AND valid_from < %s
                  AND (valid_to IS NULL OR valid_to >= %s)
            """, (
                effective - timedelta(days=1), previous["id"],
                effective, effective,
            ))
            audit_changes.append({
                "action": "shorten", "row_id": previous["id"],
                "old_valid_to": (
                    _day(old_valid_to).isoformat() if old_valid_to else None
                ),
                "new_valid_to": (effective - timedelta(days=1)).isoformat(),
            })
        cur.execute("""
            INSERT INTO gramas_gelado_historico
                (artigo, gramas, tipo_dose, valid_from, valid_to,
                 created_by, evidence_reference, import_batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            row["artigo"], row["gramas"], row["tipo_dose"], effective,
            valid_to, actor, row["evidence_reference"], batch_id,
        ))
        inserted_id = cur.fetchone()["id"]
        if future_association_ids:
            cur.execute("""
                UPDATE produto_regra_dose_historico
                SET regra_dose_id=%s
                WHERE id=ANY(%s)
            """, (inserted_id, future_association_ids))
        for product_id in continuing_product_ids:
            cur.execute("""
                SELECT MIN(valid_from) AS next_start
                FROM produto_regra_dose_historico
                WHERE produto_vendas_config_id=%s AND valid_from>%s
            """, (product_id, effective))
            next_association = cur.fetchone()["next_start"]
            association_end = valid_to
            if next_association is not None:
                before_next = _day(next_association) - timedelta(days=1)
                association_end = min(
                    association_end, before_next
                ) if association_end is not None else before_next
            if association_end is not None and association_end < effective:
                continue
            cur.execute("""
                INSERT INTO produto_regra_dose_historico (
                    produto_vendas_config_id, regra_dose_id,
                    valid_from, valid_to, created_by
                ) VALUES (%s, %s, %s, %s, %s)
            """, (
                product_id, inserted_id, effective, association_end, actor,
            ))
        backfill_weight_sales(cur, rule_ids=[inserted_id])
        audit_changes.append({
            "action": "insert", "row_id": inserted_id,
            "artigo": row["artigo"], "valid_from": row["valid_from"],
            "valid_to": valid_to.isoformat() if valid_to else None,
            "tipo_dose": row["tipo_dose"], "gramas": row["gramas"],
            "evidence_reference": row["evidence_reference"],
        })
    cur.execute("""
        INSERT INTO gramas_gelado_import_audit
            (id, created_by, source_name, row_count, rows_json)
        VALUES (%s, %s, %s, %s, %s::jsonb)
    """, (
        batch_id, actor, source_name, len(normalized),
        json.dumps(
            {"inputs": normalized, "changes": audit_changes},
            ensure_ascii=False,
        ),
    ))
    return batch_id


def get_historical_dose_coverage(loja=None, data_inicio=None, data_fim=None):
    """Return monthly sales-rule evidence coverage and recent import audits."""
    sales, _history = load_dose_sales_with_rules(
        data_inicio=data_inicio, data_fim=data_fim, loja=loja
    )
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, created_by, created_at, source_name, row_count
            FROM gramas_gelado_import_audit
            ORDER BY created_at DESC LIMIT 20
        """)
        audits = [dict(row) for row in cur.fetchall()]
    months = defaultdict(lambda: {"total": 0, "covered": 0})
    for sale in sales:
        key = _day(sale["data"]).strftime("%Y-%m")
        months[key]["total"] += 1
        if sale.get("regra_dose_id") is not None:
            months[key]["covered"] += 1
    coverage = []
    for month, counts in sorted(months.items(), reverse=True):
        status = (
            "recovered" if counts["covered"] == counts["total"]
            else "partial" if counts["covered"] else "unknown"
        )
        coverage.append({"month": month, "status": status, **counts})
    return coverage, audits


def calculate_doseamento(sales, history, rotation, data_inicio=None,
                         data_fim=None, loja=None, store_names=None):
    """Pure doseamento calculation used by :func:`get_doseamento_period`."""
    allowed_stores = set(store_names or [])
    if loja:
        allowed_stores = {loja}
    stores = {}
    for store in rotation.get("stores", []):
        name = store.get("name", store.get("loja", store.get("store")))
        if name is not None and (not allowed_stores or name in allowed_stores):
            stores[name] = {"name": name, "id": store.get("id")}
    if loja and loja not in stores:
        stores[loja] = {"name": loja}
    buckets = defaultdict(lambda: {
        "theoretical": Decimal("0"), "revenue": Decimal("0"),
        "sales": Decimal("0"), "mapped_sales": Decimal("0"),
        "sale_rows": 0, "fixed_rows": 0,
        "weighted_rows": 0, "unmapped": [], "weighted_products": [],
    })
    for sale in sales:
        store = sale.get("loja", sale.get("store", loja))
        if loja and store != loja:
            continue
        if allowed_stores and store not in allowed_stores:
            continue
        product = sale.get("produto", sale.get("product", ""))
        bucket = buckets[store]
        bucket["sale_rows"] += 1
        quantity = _decimal(sale.get("quantidade"))
        bucket["sales"] += quantity
        bucket["revenue"] += _decimal(sale.get("valor_euros", sale.get("revenue")))
        row = _explicit_rule(sale, history)
        if row is None:
            bucket["unmapped"].append(product)
            continue
        tipo = str(row.get("tipo_dose", "fixa")).casefold()
        bucket["mapped_sales"] += quantity
        if tipo in ("weight", "peso"):
            weight = sale.get("peso_vendido_kg")
            if weight is None:
                bucket["weighted_products"].append(product)
                continue
            bucket["weighted_rows"] += 1
            bucket["theoretical"] += _decimal(weight)
            continue
        bucket["fixed_rows"] += 1
        bucket["theoretical"] += quantity * _decimal(row.get("gramas")) / 1000

    intervals = rotation.get("intervals", [])
    for interval in intervals:
        store = interval.get("store", interval.get("loja"))
        if loja and store != loja:
            continue
        if allowed_stores and store not in allowed_stores:
            continue
        stores.setdefault(store, {"name": store})
        bucket = buckets[store]
        if interval.get("usable", not interval.get("issues")):
            bucket.setdefault("real", Decimal("0"))
            bucket["real"] += _decimal(interval.get("consumption_kg"))
        bucket.setdefault("inventory", defaultdict(Decimal))
        for key in ("opening_kg", "production_kg", "inbound_kg", "outbound_kg",
                    "breakage_kg", "closing_kg"):
            bucket["inventory"][key] += _decimal(interval.get(key))
        bucket.setdefault("rotation_issues", set()).update(interval.get("issues", []))
        bucket.setdefault("rotation_flags", set()).update(interval.get("flags", []))

    def result(store, bucket):
        real = bucket.get("real", Decimal("0"))
        theoretical = bucket["theoretical"]
        sales = bucket["sales"]
        mapped = bucket["mapped_sales"]
        issues = set(bucket.get("rotation_issues", set()))
        issues.update(
            "rotation_flag:" + flag
            for flag in bucket.get("rotation_flags", set())
        )
        unmapped = sorted(set(bucket["unmapped"]))
        weighted = sorted(set(bucket["weighted_products"]))
        theoretical_value = (
            theoretical
            if bucket["fixed_rows"] or bucket["weighted_rows"] else None
        )
        if unmapped:
            issues.add("unmapped_products")
        if weighted:
            issues.add("weight_products")
        if not bucket["sale_rows"]:
            issues.add("no_sales")
        store_intervals = [
            i for i in intervals
            if i.get("store", i.get("loja")) == store
        ]
        usable_intervals = [
            i for i in store_intervals
            if i.get("usable", not i.get("issues"))
        ]
        if not usable_intervals:
            issues.add("no_usable_intervals")
        elif real <= 0:
            issues.add("invalid_real_consumption")
        covered_days = set()
        for interval in usable_intervals:
            start = _day(interval.get("start_date"))
            if start:
                is_end_snapshot = interval.get("snapshot_type") == "fim"
                for offset in range(int(interval.get("days", 0))):
                    current = start + timedelta(
                        days=offset + (1 if is_end_snapshot else 0)
                    )
                    if data_inicio and current < data_inicio:
                        continue
                    if data_fim and current > data_fim:
                        continue
                    covered_days.add(current)
        observed = len(covered_days)
        requested = ((data_fim - data_inicio).days + 1
                     if data_inicio and data_fim else observed)
        coverage = (Decimal(observed) * 100 / requested
                    if requested else Decimal("0"))
        relevant_cells = []
        for flavor_row in rotation.get("rows", []):
            cell = flavor_row.get("stores", {}).get(store)
            if cell and (
                cell.get("snapshot_count", 0) or
                cell.get("activity_count", 0) or
                cell.get("valid_intervals", 0) or
                cell.get("excluded_intervals", 0)
            ):
                relevant_cells.append(cell)
        if relevant_cells:
            coverage = min(
                coverage,
                min(_decimal(cell.get("coverage_pct")) for cell in relevant_cells),
            )
        if coverage < 100:
            issues.add("insufficient_coverage")
        invalid_rotation = bool(bucket.get("rotation_issues"))
        status = (
            "invalid"
            if invalid_rotation or "invalid_real_consumption" in issues
            else ("reliable" if not issues else "incomplete")
        )
        comparable = status == "reliable" and theoretical_value is not None
        variance = real - theoretical if comparable else None
        real_value = real if status == "reliable" else None
        inventory = dict(bucket.get("inventory", {}))
        return {
            "loja": store,
            "mapped_theoretical_kg": (
                float(theoretical_value)
                if theoretical_value is not None else None
            ),
            "theoretical_kg": float(theoretical) if comparable else None,
            "real_kg": float(real_value) if real_value is not None else None,
            "observed_real_kg": float(real) if usable_intervals else None,
            "variance_kg": float(variance) if variance is not None else None,
            "variance_pct": (
                float(variance * 100 / theoretical)
                if variance is not None and theoretical > 0 else None
            ),
            "yield_pct": (
                float(theoretical * 100 / real)
                if comparable and real > 0 else None
            ),
            "revenue": float(bucket["revenue"]),
            "revenue_per_kg": (
                float(bucket["revenue"] / real)
                if comparable and real > 0 else None
            ),
            "status": status, "coverage_pct": round(float(coverage), 1),
            "issues": sorted(issues), "unmapped_products": unmapped,
            "weighted_products": weighted,
            "mapped_sales_pct": float(mapped * 100 / sales) if sales > 0 else 0.0,
            "inventory": {key: float(inventory.get(key, 0))
                          for key in ("opening_kg", "production_kg", "inbound_kg",
                                      "outbound_kg", "breakage_kg", "closing_kg")},
        }

    rows = [result(name, buckets[name]) for name in sorted(
        set(stores) | set(buckets))]
    if loja:
        return rows[0] if rows else result(loja, buckets[loja])
    total = defaultdict(lambda: Decimal("0"))
    for row in rows:
        for key in ("theoretical_kg", "real_kg", "revenue"):
            total[key] += _decimal(row[key])
        total["mapped_theoretical_kg"] += _decimal(
            row.get("mapped_theoretical_kg")
        )
    aggregate = result("global", {
        "theoretical": total["theoretical_kg"], "real": total["real_kg"],
        "revenue": total["revenue"], "sales": sum(_decimal(b["sales"]) for b in buckets.values()),
        "mapped_sales": sum(_decimal(b["mapped_sales"]) for b in buckets.values()),
        "sale_rows": sum(b["sale_rows"] for b in buckets.values()),
        "fixed_rows": sum(b["fixed_rows"] for b in buckets.values()),
        "weighted_rows": sum(b["weighted_rows"] for b in buckets.values()),
        "unmapped": sum((b["unmapped"] for b in buckets.values()), []),
        "weighted_products": sum((b["weighted_products"] for b in buckets.values()), []),
        "rotation_issues": set().union(*(b.get("rotation_issues", set()) for b in buckets.values())),
        "rotation_flags": set().union(*(b.get("rotation_flags", set()) for b in buckets.values())),
        "inventory": defaultdict(Decimal),
    })
    for row in rows:
        for key, value in row["inventory"].items():
            aggregate["inventory"][key] = aggregate["inventory"].get(key, 0) + value
    aggregate["status"] = (
        "reliable" if rows and all(row["status"] == "reliable" for row in rows)
        else ("invalid" if not rows or any(row["status"] == "invalid" for row in rows)
              else "incomplete")
    )
    aggregate["issues"] = sorted(set(
        issue for row in rows for issue in row["issues"]
    ))
    aggregate["coverage_pct"] = min(
        (row["coverage_pct"] for row in rows), default=0.0
    )
    aggregate["observed_real_kg"] = sum(
        _decimal(row.get("observed_real_kg")) for row in rows
    )
    if aggregate["status"] == "reliable":
        theoretical = sum(_decimal(row["theoretical_kg"]) for row in rows)
        real = sum(_decimal(row["real_kg"]) for row in rows)
        variance = real - theoretical
        aggregate.update({
            "theoretical_kg": float(theoretical),
            "real_kg": float(real),
            "variance_kg": float(variance),
            "variance_pct": (
                float(variance * 100 / theoretical)
                if theoretical > 0 else None
            ),
            "yield_pct": (
                float(theoretical * 100 / real) if real > 0 else None
            ),
            "revenue_per_kg": (
                float(total["revenue"] / real) if real > 0 else None
            ),
        })
    else:
        aggregate.update({
            "mapped_theoretical_kg": float(total["mapped_theoretical_kg"]),
            "theoretical_kg": None,
            "real_kg": None,
            "variance_kg": None,
            "variance_pct": None,
            "yield_pct": None,
            "revenue_per_kg": None,
        })
    aggregate["inventory"] = {
        key: float(value) for key, value in aggregate["inventory"].items()
    }
    aggregate["inventory_breakdown"] = aggregate["inventory"]
    aggregate["stores"] = rows
    return aggregate


def get_doseamento_period(data_inicio, data_fim, loja=None, store_names=None):
    """Load auditable sales and stock sources and return template data."""
    if data_inicio > data_fim:
        raise ValueError("A data inicial não pode ser posterior à data final.")
    sales, history = load_dose_sales_with_rules(data_inicio, data_fim, loja)
    rotation = get_gelato_stock_rotation(data_inicio, data_fim)
    return calculate_doseamento(
        sales, history, rotation, data_inicio, data_fim, loja, store_names
    )


__all__ = [
    "get_doseamento_period", "calculate_doseamento",
    "get_vendas_ao_peso_sem_peso_calculavel",
    "preview_historical_dose_csv", "create_historical_dose_preview",
    "get_historical_dose_preview", "confirm_historical_dose_preview",
    "get_historical_dose_coverage",
    "get_dose_product_configuration_queue", "configure_dose_product",
]