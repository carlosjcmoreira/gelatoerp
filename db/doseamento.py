"""Store gelato dose adherence.

The calculation is deliberately kept separate from the database reader.  Apart
from making the rules auditable, this makes it possible to test the arithmetic
with exports of the two source systems.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from psycopg2.extras import RealDictCursor

from db.connection import db_connection
from db.gelato_rotation import get_gelato_stock_rotation


def _decimal(value, default=Decimal("0")):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else default
    except (InvalidOperation, TypeError, ValueError):
        return default


def _day(value):
    return value.date() if isinstance(value, datetime) else value


def _matches(product, history, sale_date):
    """Return the longest unambiguous effective history row."""
    product = str(product or "").casefold()
    candidates = []
    for row in history:
        pattern = str(row.get("artigo", "")).casefold()
        if not pattern or pattern not in product:
            continue
        start = row.get("valid_from")
        end = row.get("valid_to")
        if start is not None and _day(start) > sale_date:
            continue
        if end is not None and _day(end) < sale_date:
            continue
        candidates.append((len(pattern), row))
    if not candidates:
        return None
    longest = max(length for length, _ in candidates)
    winners = [row for length, row in candidates if length == longest]
    return winners[0] if len(winners) == 1 else None


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
        "weighted": 0, "unmapped": [], "weighted_products": [],
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
        row = _matches(product, history, _day(sale.get("data")))
        if row is None:
            bucket["unmapped"].append(product)
            continue
        tipo = str(row.get("tipo_dose", "fixa")).casefold()
        bucket["mapped_sales"] += quantity
        if tipo in ("weight", "peso"):
            bucket["weighted"] += 1
            bucket["weighted_products"].append(product)
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
        theoretical_value = theoretical if bucket["fixed_rows"] else None
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
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT vd.data, vd.loja, vd.produto, vd.quantidade, vd.valor_euros
            FROM vendas_detalhe vd
            JOIN produtos_vendas_config pvc ON pvc.produto = vd.produto
              AND pvc.gelado_kpi = TRUE
            WHERE vd.data >= %s AND vd.data <= %s
        """ + (" AND vd.loja = %s" if loja else ""), 
                   (data_inicio, data_fim, loja) if loja else (data_inicio, data_fim))
        sales = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT artigo, gramas, tipo_dose, valid_from, valid_to "
                    "FROM gramas_gelado_historico ORDER BY artigo")
        history = [dict(row) for row in cur.fetchall()]
    rotation = get_gelato_stock_rotation(data_inicio, data_fim)
    return calculate_doseamento(
        sales, history, rotation, data_inicio, data_fim, loja, store_names
    )


__all__ = ["get_doseamento_period", "calculate_doseamento"]