from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from psycopg2.extras import RealDictCursor

from db.connection import db_connection, db_retry


__all__ = [
    'calculate_gelato_stock_rotation',
    'get_gelato_stock_rotation',
]


_VALID_PRODUCTION_TYPES = {'producao', 'balança'}
_REJECTED_TRANSFER_STATUSES = {'cancelada', 'cancelado', 'rejeitada', 'rejeitado'}
_CONFIRMED_TRANSFER_STATUSES = {'confirmada', 'confirmado'}
_ZERO_TOLERANCE_KG = Decimal('0.05')


def _quantity(value):
    if value is None:
        return None
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not quantity.is_finite() or quantity < 0:
        return None
    return quantity


def _date_only(value):
    return value.date() if isinstance(value, datetime) else value


def _normalise(value):
    return ' '.join(str(value or '').strip().casefold().split())


def _unique_identity_map(pairs):
    candidates = defaultdict(set)
    for alias, canonical in pairs:
        if alias and canonical:
            candidates[_normalise(alias)].add(canonical)
    return {
        alias: next(iter(values))
        for alias, values in candidates.items()
        if len(values) == 1
    }


def _resolver(stores, aliases, recipes):
    store_by_id = {row['id']: row['name'] for row in stores}
    store_pairs = [(row['name'], row['name']) for row in stores]
    for row in aliases:
        canonical = store_by_id.get(row.get('store_id'))
        store_pairs.extend([
            (row.get('alias_name'), canonical),
            (row.get('alias_code'), canonical),
            (row.get('store_pos_code'), canonical),
        ])
    store_by_name = _unique_identity_map(store_pairs)

    flavor_pairs = []
    for row in recipes:
        canonical = (row.get('nome_corrente') or row.get('nome') or '').strip()
        flavor_pairs.extend([
            (row.get('nome'), canonical),
            (row.get('nome_corrente'), canonical),
        ])
    flavor_by_name = _unique_identity_map(flavor_pairs)

    def resolve_store(row, name_key='loja', id_key='store_id'):
        if row.get(id_key) in store_by_id:
            return store_by_id[row[id_key]]
        return store_by_name.get(_normalise(row.get(name_key)))

    def resolve_flavor(value):
        return flavor_by_name.get(_normalise(value))

    return resolve_store, resolve_flavor


def _inside_interval(moment, start, end, snapshot_type):
    if snapshot_type == 'inicio':
        return start <= moment < end
    return start < moment <= end


def _sum_interval(rows, start, end, snapshot_type):
    return sum(
        (row['quantity'] for row in rows
         if _inside_interval(row['date'], start, end, snapshot_type)),
        Decimal('0'),
    )


def _has_interval_row(rows, start, end, snapshot_type):
    return any(
        _inside_interval(row['date'], start, end, snapshot_type)
        for row in rows
    )


def _interval_flags(rows, start, end, snapshot_type):
    return {
        flag
        for row in rows
        if _inside_interval(row['date'], start, end, snapshot_type)
        for flag in row.get('flags', ())
    }


def _interval_issues(rows, start, end, snapshot_type):
    return {
        row['issue']
        for row in rows
        if _inside_interval(row['date'], start, end, snapshot_type)
    }


def _interval_within_period(start, end, snapshot_type, first_day, last_day):
    if snapshot_type == 'inicio':
        return start >= first_day and end <= last_day + timedelta(days=1)
    return start >= first_day - timedelta(days=1) and end <= last_day


def calculate_gelato_stock_rotation(
    *,
    data_inicio,
    data_fim,
    stores,
    aliases,
    recipes,
    stock_rows,
    production_rows,
    transfer_order_rows,
    legacy_transfer_rows,
    receipt_rows,
    breakage_rows,
):
    """Calculate auditable stock-conservation intervals by flavor and store.

    ``inicio`` snapshots cover movements in ``[start, end)``; ``fim`` snapshots
    cover movements in ``(start, end]``.  This prevents a movement on a weighing
    date from being counted on both adjacent intervals.
    """
    if not isinstance(data_inicio, date) or not isinstance(data_fim, date):
        raise TypeError('data_inicio e data_fim devem ser datas.')
    if data_inicio > data_fim:
        raise ValueError('A data inicial não pode ser posterior à data final.')

    active_stores = [row for row in stores if row.get('is_active', True)]
    resolve_store, resolve_flavor = _resolver(active_stores, aliases, recipes)
    store_by_name = {row['name']: row for row in active_stores}
    production_store_names = {
        row['name'] for row in active_stores
        if _normalise(row.get('store_type')) == 'producao'
        or _normalise(row['name']) == 'matosinhos'
    }
    unresolved = Counter()
    movement_issues = defaultdict(list)
    flavors = set()
    unresolved_evidence = []

    def record_unresolved_identity(row, source, store, flavor):
        if flavor:
            flavors.add(flavor)
        unresolved_evidence.append({
            'date': row['data'],
            'issue': f'unresolved_{source}_identity',
            'store': store,
            'flavor': flavor,
        })

    snapshots = defaultdict(lambda: defaultdict(dict))
    for row in stock_rows:
        store = resolve_store(row)
        flavor = resolve_flavor(row.get('sabor'))
        if not store:
            unresolved['stock_store'] += 1
        if not flavor:
            unresolved['stock_flavor'] += 1
        if not store or not flavor:
            record_unresolved_identity(row, 'snapshot', store, flavor)
            continue
        preferred_type = 'inicio' if store in production_store_names else 'fim'
        if row.get('tipo') != preferred_type:
            continue
        key = (store, flavor)
        existing = snapshots[key][row['data']].get(preferred_type)
        if existing is not None:
            unresolved['duplicate_snapshot'] += 1
            snapshots[key][row['data']]['duplicate'] = True
            continue
        quantity = _quantity(row.get('quantidade_kg'))
        if quantity is None:
            unresolved['stock_quantity'] += 1
            movement_issues[key].append({
                'date': row['data'],
                'issue': 'invalid_snapshot_quantity',
            })
            continue
        snapshots[key][row['data']][preferred_type] = quantity
        flavors.add(flavor)

    grouped_production = defaultdict(lambda: defaultdict(Decimal))
    manual_production = defaultdict(list)
    for row in production_rows:
        store = resolve_store(row)
        flavor = resolve_flavor(row.get('sabor'))
        if not store:
            unresolved['production_store'] += 1
        if not flavor:
            unresolved['production_flavor'] += 1
        if not store or not flavor:
            record_unresolved_identity(row, 'production', store, flavor)
            continue
        tipo = row.get('tipo')
        quantity = _quantity(row.get('quantidade_kg'))
        if quantity is None:
            unresolved['production_quantity'] += 1
            movement_issues[(store, flavor)].append({
                'date': row['data'],
                'issue': 'invalid_production_quantity',
            })
            continue
        movement = {'date': row['data'], 'quantity': quantity}
        if tipo == 'manual':
            manual_production[(store, flavor)].append(movement)
            continue
        if tipo not in _VALID_PRODUCTION_TYPES:
            unresolved['production_type'] += 1
            continue
        grouped_production[(store, flavor, row['data'])][tipo] += movement[
            'quantity'
        ]
        flavors.add(flavor)

    production = defaultdict(list)
    for (store, flavor, movement_date), by_type in grouped_production.items():
        selected_type = 'balança' if 'balança' in by_type else 'producao'
        production[(store, flavor)].append({
            'date': movement_date,
            'quantity': by_type[selected_type],
            'source': selected_type,
        })

    inbound = defaultdict(list)
    outbound = defaultdict(list)
    order_keys = set()
    overlap_keys = set()
    transfer_totals = defaultdict(lambda: defaultdict(Decimal))
    outbound_order_keys = set()

    def add_transfer(row, *, legacy=False):
        destination = resolve_store(
            row, name_key='loja_destino', id_key='store_id'
        )
        movement_date = row['data']
        status = _normalise(row.get('status'))
        if not legacy and status == 'pendente':
            unresolved['pending_transfer_ignored'] += 1
            return

        flags = set()
        if legacy:
            flags.add('legacy_transfer_source')
        origin = None
        if row.get('loja_origem'):
            origin = resolve_store(
                row, name_key='loja_origem', id_key='origin_store_id'
            )
        elif destination and destination not in production_store_names:
            if len(production_store_names) == 1:
                origin = next(iter(production_store_names))
                flags.add('inferred_transfer_origin')

        inbound_date = (
            movement_date if legacy
            else _date_only(row.get('confirmado_em'))
        )
        flavor = resolve_flavor(row.get('sabor'))
        if not flavor:
            unresolved['transfer_flavor'] += 1
            if not destination and row.get('destino_tipo') != 'b2b':
                unresolved['transfer_destination'] += 1
            if legacy or status in _CONFIRMED_TRANSFER_STATUSES:
                source_candidates = (
                    [origin] if origin
                    else [
                        candidate for candidate in store_by_name
                        if candidate != destination
                    ]
                )
                for candidate in source_candidates:
                    record_unresolved_identity(
                        {'data': movement_date},
                        'transfer',
                        candidate,
                        None,
                    )
                if destination:
                    record_unresolved_identity(
                        {'data': inbound_date or movement_date},
                        'transfer',
                        destination,
                        None,
                    )
            else:
                record_unresolved_identity(
                    {'data': movement_date}, 'transfer', None, None
                )
            return

        quantity = _quantity(
            row.get('quantidade') if 'quantidade' in row
            else row.get('quantidade_kg')
        )

        affected_keys = set()
        if destination:
            affected_keys.add((destination, flavor))
        if origin:
            affected_keys.add((origin, flavor))
        else:
            affected_keys.update(
                (candidate, flavor)
                for candidate in store_by_name
                if candidate != destination
            )

        issue = None
        if not destination and row.get('destino_tipo') != 'b2b':
            unresolved['transfer_destination'] += 1
            issue = 'unknown_transfer_destination'
        elif quantity is None:
            unresolved['transfer_quantity'] += 1
            issue = 'invalid_transfer_quantity'
        elif not legacy and status not in _CONFIRMED_TRANSFER_STATUSES:
            unresolved['transfer_status'] += 1
            issue = 'unknown_transfer_status'
        elif not legacy and destination and inbound_date is None:
            unresolved['transfer_confirmation_date'] += 1
            issue = 'missing_transfer_confirmation_date'
        if issue:
            for affected_key in affected_keys:
                movement_issues[affected_key].append({
                    'date': movement_date,
                    'issue': issue,
                })
            return

        movement = {
            'date': movement_date,
            'quantity': quantity,
            'flags': flags,
        }
        if destination:
            inbound[(destination, flavor)].append({
                **movement,
                'date': inbound_date,
            })
        if origin:
            outbound[(origin, flavor)].append(movement)
            outbound_order_keys.add((
                origin,
                flavor,
                movement_date,
                quantity.quantize(Decimal('0.01')),
            ))
        else:
            unresolved['transfer_origin'] += 1
            for affected_key in affected_keys:
                movement_issues[affected_key].append({
                    'date': movement_date,
                    'issue': 'unassigned_transfer_origin',
                })

    for row in transfer_order_rows:
        flavor = resolve_flavor(row.get('sabor'))
        destination = resolve_store(
            row, name_key='loja_destino', id_key='store_id'
        )
        if flavor and destination:
            key = (row['data'], flavor, destination)
            order_keys.add(key)
            quantity = _quantity(row.get('quantidade'))
            if quantity is not None:
                transfer_totals[key]['orders'] += quantity
        if _normalise(row.get('status')) in _REJECTED_TRANSFER_STATUSES:
            continue
        add_transfer(row)
        if flavor:
            flavors.add(flavor)

    for row in legacy_transfer_rows:
        flavor = resolve_flavor(row.get('sabor'))
        destination = resolve_store(
            row, name_key='loja_destino', id_key='store_id'
        )
        if flavor and destination:
            key = (row['data'], flavor, destination)
            quantity = _quantity(row.get('quantidade_kg'))
            if key in order_keys:
                overlap_keys.add(key)
                if quantity is None:
                    add_transfer(row, legacy=True)
                else:
                    transfer_totals[key]['legacy'] += quantity
                continue
        add_transfer(row, legacy=True)
        if flavor:
            flavors.add(flavor)

    for row in receipt_rows:
        store = resolve_store(row)
        flavor = resolve_flavor(row.get('sabor'))
        if not store:
            unresolved['receipt_store'] += 1
        if not flavor:
            unresolved['receipt_flavor'] += 1
        if not store or not flavor:
            record_unresolved_identity(row, 'receipt', store, flavor)
            continue
        signed_value = row.get('quantidade')
        try:
            signed_quantity = Decimal(str(signed_value))
        except (InvalidOperation, TypeError, ValueError):
            signed_quantity = None
        if signed_quantity is None or not signed_quantity.is_finite():
            unresolved['receipt_quantity'] += 1
            movement_issues[(store, flavor)].append({
                'date': row['data'],
                'issue': 'invalid_receipt_quantity',
            })
            continue
        quantity = abs(signed_quantity)
        lote = row.get('lote') or ''
        movement = {
            'date': row['data'],
            'quantity': quantity,
            'flags': set(),
        }
        if signed_quantity > 0 and lote and lote != 'transferencia_saida':
            inbound[(store, flavor)].append(movement)
        elif signed_quantity < 0 and lote == 'transferencia_saida':
            key = (
                store,
                flavor,
                row['data'],
                quantity.quantize(Decimal('0.01')),
            )
            if key not in outbound_order_keys:
                movement['flags'].add('receipt_transfer_source')
                outbound[(store, flavor)].append(movement)
        flavors.add(flavor)

    breakages = defaultdict(list)
    for row in breakage_rows:
        store = resolve_store(row)
        flavor = resolve_flavor(row.get('sabor'))
        if not store:
            unresolved['breakage_store'] += 1
        if not flavor:
            unresolved['breakage_flavor'] += 1
        if not store or not flavor:
            record_unresolved_identity(row, 'breakage', store, flavor)
            continue
        quantity = _quantity(row.get('quantidade_kg'))
        if quantity is None:
            unresolved['breakage_quantity'] += 1
            movement_issues[(store, flavor)].append({
                'date': row['data'],
                'issue': 'invalid_breakage_quantity',
            })
            continue
        breakages[(store, flavor)].append({
            'date': row['data'],
            'quantity': quantity,
        })
        flavors.add(flavor)

    for movement_date, flavor, destination in overlap_keys:
        totals = transfer_totals[(movement_date, flavor, destination)]
        if totals['orders'] == totals['legacy']:
            continue
        unresolved['ambiguous_transfer_overlap'] += 1
        affected = {(destination, flavor)}
        affected.update(
            (candidate, flavor)
            for candidate in production_store_names
            if candidate != destination
        )
        for affected_key in affected:
            movement_issues[affected_key].append({
                'date': movement_date,
                'issue': 'ambiguous_transfer_overlap',
            })

    for evidence in unresolved_evidence:
        candidate_stores = (
            [evidence['store']] if evidence['store']
            else list(store_by_name)
        )
        candidate_flavors = (
            [evidence['flavor']] if evidence['flavor']
            else list(flavors)
        )
        for candidate_store in candidate_stores:
            for candidate_flavor in candidate_flavors:
                movement_issues[(candidate_store, candidate_flavor)].append({
                    'date': evidence['date'],
                    'issue': evidence['issue'],
                })

    cells = {}
    all_intervals = []
    requested_days = (data_fim - data_inicio).days + 1
    for store in store_by_name:
        for flavor in sorted(flavors):
            key = (store, flavor)
            dated = snapshots.get(key, {})
            snapshot_type = (
                'inicio' if store in production_store_names else 'fim'
            )
            points = []
            for moment in sorted(dated):
                payload = dated[moment]
                if snapshot_type in payload:
                    points.append((
                        moment,
                        payload[snapshot_type],
                        bool(payload.get('duplicate')),
                    ))

            valid = []
            excluded = []
            for previous, current in zip(points, points[1:]):
                start, opening, start_duplicate = previous
                end, closing, end_duplicate = current
                if not _interval_within_period(
                    start,
                    end,
                    snapshot_type,
                    data_inicio,
                    data_fim,
                ):
                    continue
                days = (end - start).days
                issues = set()
                flags = _interval_flags(
                    inbound.get(key, []), start, end, snapshot_type
                ) | _interval_flags(
                    outbound.get(key, []), start, end, snapshot_type
                )
                if days <= 0:
                    issues.add('invalid_snapshot_order')
                if start_duplicate or end_duplicate:
                    issues.add('duplicate_snapshot')
                issues.update(_interval_issues(
                    movement_issues.get(key, []),
                    start,
                    end,
                    snapshot_type,
                ))
                if days > 3:
                    flags.add('long_interval')

                produced = _sum_interval(
                    production.get(key, []), start, end, snapshot_type
                )
                received = _sum_interval(
                    inbound.get(key, []), start, end, snapshot_type
                )
                sent = _sum_interval(
                    outbound.get(key, []), start, end, snapshot_type
                )
                broken = _sum_interval(
                    breakages.get(key, []), start, end, snapshot_type
                )
                if store in production_store_names and _has_interval_row(
                    manual_production.get(key, []),
                    start,
                    end,
                    snapshot_type,
                ) and produced == 0:
                    issues.add('manual_only_production')
                consumption = opening + produced + received - sent - broken - closing
                if consumption < -_ZERO_TOLERANCE_KG:
                    issues.add('negative_stock_residual')
                elif consumption < 0:
                    consumption = Decimal('0')

                interval = {
                    'store': store,
                    'sabor': flavor,
                    'snapshot_type': snapshot_type,
                    'start_date': start,
                    'end_date': end,
                    'days': days,
                    'opening_kg': float(opening),
                    'production_kg': float(produced),
                    'inbound_kg': float(received),
                    'outbound_kg': float(sent),
                    'breakage_kg': float(broken),
                    'closing_kg': float(closing),
                    'consumption_kg': (
                        float(consumption) if not issues else None
                    ),
                    '_consumption_decimal': (
                        consumption if not issues else None
                    ),
                    'issues': sorted(issues),
                    'flags': sorted(flags),
                    'usable': not issues,
                }
                all_intervals.append(interval)
                (valid if interval['usable'] else excluded).append(interval)

            observed_days = sum(row['days'] for row in valid)
            total_consumption = sum(
                (row['_consumption_decimal'] for row in valid),
                Decimal('0'),
            )
            average = (
                total_consumption / observed_days if observed_days else None
            )
            flags = sorted({
                flag for interval in valid for flag in interval['flags']
            })
            coverage = min(1, observed_days / requested_days)
            cell_issues = {
                issue for interval in excluded
                for issue in interval['issues']
            }
            if len(points) < 2:
                cell_issues.add('insufficient_snapshots')
            elif not valid and not excluded:
                cell_issues.add('no_intervals_in_period')
            if not valid:
                confidence = 'none'
            elif excluded or flags or coverage < 0.5:
                confidence = 'low'
            elif coverage < 0.8:
                confidence = 'medium'
            else:
                confidence = 'high'
            cells[key] = {
                'snapshot_count': len(points),
                'activity_count': sum(len(source.get(key, [])) for source in (
                    production, manual_production, inbound, outbound, breakages,
                )),
                'average_daily_kg': (
                    round(float(average), 3) if average is not None else None
                ),
                'total_consumption_kg': (
                    round(float(total_consumption), 3) if valid else None
                ),
                'days_observed': observed_days,
                'valid_intervals': len(valid),
                'excluded_intervals': len(excluded),
                'coverage_pct': round(coverage * 100, 1),
                'confidence': confidence,
                'flags': flags,
                'issues': sorted(cell_issues),
                'first_observation': (
                    min(row['start_date'] for row in valid) if valid else None
                ),
                'last_observation': (
                    max(row['end_date'] for row in valid) if valid else None
                ),
            }

    rows = []
    for interval in all_intervals:
        interval.pop('_consumption_decimal', None)
    for flavor in sorted(flavors):
        rows.append({
            'sabor': flavor,
            'stores': {
                store: cells[(store, flavor)]
                for store in store_by_name
            },
        })

    confidence_by_store = {}
    for store in store_by_name:
        counts = Counter(cells[(store, flavor)]['confidence'] for flavor in flavors)
        confidence_by_store[store] = {
            level: counts.get(level, 0)
            for level in ('high', 'medium', 'low', 'none')
        }

    return {
        'data_inicio': data_inicio,
        'data_fim': data_fim,
        'stores': [
            {
                'id': row['id'],
                'name': row['name'],
                'is_production': row['name'] in production_store_names,
            }
            for row in active_stores
        ],
        'rows': rows,
        'intervals': all_intervals,
        'coverage': {
            'cells_total': len(cells),
            'cells_with_value': sum(
                1 for value in cells.values()
                if value['average_daily_kg'] is not None
            ),
            'by_store': confidence_by_store,
        },
        'unresolved': dict(sorted(unresolved.items())),
    }


@db_retry
def get_gelato_stock_rotation(data_inicio, data_fim):
    if not isinstance(data_inicio, date) or not isinstance(data_fim, date):
        raise TypeError('data_inicio e data_fim devem ser datas.')
    if data_inicio > data_fim:
        raise ValueError('A data inicial não pode ser posterior à data final.')
    if (data_fim - data_inicio).days > 366:
        raise ValueError('O período de rotação não pode exceder 367 dias.')

    baseline = data_inicio - timedelta(days=366)
    load_until = data_fim + timedelta(days=1)
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("""
            SELECT id, name, store_type, is_active, receives_transfers,
                   requires_eod_weighing
            FROM stores
            WHERE is_active = TRUE
            ORDER BY id
        """)
        stores = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT s.id AS store_id, sa.alias_name, sa.alias_code,
                   s.pos_store_code AS store_pos_code
            FROM stores s
            LEFT JOIN store_aliases sa ON sa.store_id = s.id
            WHERE s.is_active = TRUE
        """)
        aliases = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT nome, nome_corrente
            FROM receitas_gelado
            WHERE nome IS NOT NULL OR nome_corrente IS NOT NULL
        """)
        recipes = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT data, loja, store_id, sabor, quantidade_kg, tipo
            FROM stock_gelado
            WHERE data >= %s AND data <= %s
              AND is_active = TRUE
            ORDER BY data, id
        """, (baseline, load_until))
        stock_rows = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT data, loja, store_id, sabor, quantidade_kg, tipo
            FROM producao
            WHERE data >= %s AND data <= %s
              AND tipo IN ('producao', 'balança', 'manual')
            ORDER BY data, id
        """, (baseline, load_until))
        production_rows = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT data, sabor, quantidade, loja_destino, loja_origem,
                   status, confirmado_em, destino_tipo, NULL::integer AS store_id,
                   NULL::integer AS origin_store_id
            FROM ordens_transferencia
            WHERE (
                    (data >= %s AND data <= %s)
                 OR (confirmado_em::date >= %s AND confirmado_em::date <= %s)
                  )
              AND area_origem = 'Gelado'
            ORDER BY data, id
        """, (baseline, load_until, baseline, load_until))
        transfer_order_rows = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT data, sabor, quantidade_kg, loja_destino, store_id
            FROM transferencias
            WHERE data >= %s AND data <= %s
            ORDER BY data, id
        """, (baseline, load_until))
        legacy_transfer_rows = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT data, loja, store_id, sabor, quantidade, lote
            FROM rececao_mercadoria
            WHERE data >= %s AND data <= %s
              AND tipo_produto = 'gelado'
              AND (
                    (quantidade > 0 AND lote IS NOT NULL AND lote != '')
                 OR (quantidade < 0 AND lote = 'transferencia_saida')
              )
            ORDER BY data, id
        """, (baseline, load_until))
        receipt_rows = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT data, loja, store_id, sabor, quantidade_kg
            FROM quebras
            WHERE data >= %s AND data <= %s
            ORDER BY data, id
        """, (baseline, load_until))
        breakage_rows = [dict(row) for row in cursor.fetchall()]

    return calculate_gelato_stock_rotation(
        data_inicio=data_inicio,
        data_fim=data_fim,
        stores=stores,
        aliases=aliases,
        recipes=recipes,
        stock_rows=stock_rows,
        production_rows=production_rows,
        transfer_order_rows=transfer_order_rows,
        legacy_transfer_rows=legacy_transfer_rows,
        receipt_rows=receipt_rows,
        breakage_rows=breakage_rows,
    )