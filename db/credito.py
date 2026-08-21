import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
import json

def get_credit_contracts(estado=None):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        base_q = """
            SELECT cc.*, cat.name AS categoria_custo_nome
            FROM credit_contracts cc
            LEFT JOIN cost_categories cat ON cat.id = cc.categoria_custo_id
        """
        if estado:
            cursor.execute(base_q + " WHERE cc.estado = %s ORDER BY cc.label", (estado,))
        else:
            cursor.execute(base_q + " ORDER BY cc.estado, cc.label")
        return cursor.fetchall()


def get_credit_contract(contract_id: int):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT cc.*, cat.name AS categoria_custo_nome
            FROM credit_contracts cc
            LEFT JOIN cost_categories cat ON cat.id = cc.categoria_custo_id
            WHERE cc.id = %s
        """, (contract_id,))
        return cursor.fetchone()


def upsert_credit_contract(data: dict, contract_id: int = None):
    fields = ['tipo', 'label', 'banco', 'loja_associada', 'capital_inicial',
              'saldo_divida', 'tan', 'prestacao_mensal', 'dia_debito',
              'data_inicio', 'data_fim', 'plafond', 'estado', 'notas',
              'categoria_custo_id']

    def _v(k):
        v = data.get(k)
        if v == '' or v is None:
            return None
        return v

    values = {k: _v(k) for k in fields}

    with db_connection() as conn:
        cursor = conn.cursor()
        if contract_id:
            set_clause = ', '.join(f"{k} = %s" for k in fields)
            set_clause += ', updated_at = NOW()'
            cursor.execute(
                f"UPDATE credit_contracts SET {set_clause} WHERE id = %s",
                [values[k] for k in fields] + [contract_id]
            )
        else:
            cols = ', '.join(fields)
            placeholders = ', '.join(['%s'] * len(fields))
            cursor.execute(
                f"INSERT INTO credit_contracts ({cols}) VALUES ({placeholders}) RETURNING id",
                [values[k] for k in fields]
            )
            contract_id = cursor.fetchone()[0]
        conn.commit()
    return contract_id


def delete_credit_contract(contract_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM credit_contracts WHERE id = %s", (contract_id,))
        conn.commit()


def deactivate_credit_contract(contract_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE credit_contracts SET estado = 'inativo', updated_at = NOW() WHERE id = %s",
            (contract_id,)
        )
        conn.commit()


def get_bank_balance_entries(contrato_id: int = None, limit: int = 52):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if contrato_id:
            cursor.execute(
                """SELECT b.*, c.label as contrato_label, c.plafond, c.tan
                   FROM bank_balance_entries b
                   LEFT JOIN credit_contracts c ON b.contrato_id = c.id
                   WHERE b.contrato_id = %s
                   ORDER BY b.data DESC LIMIT %s""",
                (contrato_id, limit)
            )
        else:
            cursor.execute(
                """SELECT b.*, c.label as contrato_label, c.plafond, c.tan
                   FROM bank_balance_entries b
                   LEFT JOIN credit_contracts c ON b.contrato_id = c.id
                   ORDER BY b.data DESC LIMIT %s""",
                (limit,)
            )
        return cursor.fetchall()


def insert_bank_balance_entry(data: dict):
    saldo = float(data['saldo'])
    plafond = data.get('plafond')
    tan = data.get('tan')
    utilizacao = None
    custo_juros = None
    if plafond is not None and plafond != '':
        plafond_f = float(plafond)
        utilizacao = max(0.0, plafond_f - saldo)
        if tan is not None and tan != '':
            tan_f = float(tan)
            custo_juros = round(utilizacao * (tan_f / 100) / 365 * 30, 2)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO bank_balance_entries
               (data, loja, saldo, contrato_id, utilizacao_calculada, custo_juros_estimado, notas)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (data['data'], data.get('loja'), saldo,
             data.get('contrato_id') or None,
             utilizacao, custo_juros, data.get('notas'))
        )
        entry_id = cursor.fetchone()[0]
        conn.commit()
    return entry_id


def delete_bank_balance_entry(entry_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bank_balance_entries WHERE id = %s", (entry_id,))
        conn.commit()


def get_payment_revisions(contract_id: int):
    """Returns all payment revisions for a contract, newest first."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """SELECT * FROM contract_payment_revisions
               WHERE contract_id = %s
               ORDER BY data_inicio DESC, criado_em DESC""",
            (contract_id,)
        )
        return cursor.fetchall()


def add_payment_revision(contract_id: int, data: dict):
    """Insert a new payment revision row and return its id."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO contract_payment_revisions
               (contract_id, data_inicio, prestacao, tan, euribor, spread, notas)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                contract_id,
                data['data_inicio'],
                data['prestacao'],
                data.get('tan') or None,
                data.get('euribor') or None,
                data.get('spread') or None,
                data.get('notas') or None,
            )
        )
        rev_id = cursor.fetchone()[0]
        conn.commit()
    return rev_id


def get_current_prestacao(contract) -> float:
    """Return the prestacao from the most recent revision, or prestacao_mensal fallback."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT prestacao FROM contract_payment_revisions
               WHERE contract_id = %s
               ORDER BY data_inicio DESC, criado_em DESC
               LIMIT 1""",
            (contract['id'],)
        )
        row = cursor.fetchone()
    if row:
        return float(row[0])
    return float(contract['prestacao_mensal'] or 0)


def get_credit_dashboard():
    """Returns aggregated dashboard data for active credit contracts."""
    from datetime import date, timedelta
    today = date.today()
    in_90 = today + timedelta(days=90)
    in_30 = today + timedelta(days=30)

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute(
            "SELECT * FROM credit_contracts WHERE estado = 'ativo' ORDER BY label"
        )
        contracts = cursor.fetchall()

        # Latest balance entry PER overdraft contract (handles multiple overdraft contracts)
        cursor.execute(
            """SELECT DISTINCT ON (b.contrato_id)
                  b.*, c.label as contrato_label, c.plafond, c.tan
               FROM bank_balance_entries b
               JOIN credit_contracts c ON b.contrato_id = c.id
               WHERE c.estado = 'ativo' AND c.tipo = 'overdraft'
               ORDER BY b.contrato_id, b.data DESC"""
        )
        latest_balances = cursor.fetchall()

    # Fetch latest revision per contract in one query
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """SELECT DISTINCT ON (contract_id)
                  contract_id, prestacao
               FROM contract_payment_revisions
               ORDER BY contract_id, data_inicio DESC, criado_em DESC"""
        )
        _rev_rows = cursor.fetchall()
    _latest_prestacao = {r['contract_id']: float(r['prestacao']) for r in _rev_rows}

    def _prestacao(c):
        return _latest_prestacao.get(c['id'], float(c['prestacao_mensal'] or 0))

    for c in contracts:
        c['prestacao_atual'] = _prestacao(c)

    saldo_total = sum(float(c['saldo_divida'] or 0) for c in contracts)
    custo_mensal_fixo = sum(
        _prestacao(c)
        for c in contracts if c['tipo'] != 'overdraft'
    )

    proximas_saidas = []
    for c in contracts:
        if c['tipo'] == 'overdraft':
            continue
        prestacao_val = _prestacao(c)
        if c['dia_debito'] and prestacao_val:
            dia = int(c['dia_debito'])
            mes = today.month
            ano = today.year
            try:
                prox = date(ano, mes, dia)
                if prox < today:
                    mes += 1
                    if mes > 12:
                        mes = 1
                        ano += 1
                    prox = date(ano, mes, dia)
            except ValueError:
                import calendar
                last_day = calendar.monthrange(ano, mes)[1]
                prox = date(ano, mes, last_day)
            proximas_saidas.append({
                'data': prox,
                'valor': prestacao_val,
                'label': c['label'],
                'tipo': c['tipo'],
                'certa': True,
            })

    for bal in latest_balances:
        if bal['custo_juros_estimado'] is not None:
            proximas_saidas.append({
                'data': today,
                'valor': float(bal['custo_juros_estimado']),
                'label': bal['contrato_label'],
                'tipo': 'overdraft',
                'certa': False,
            })

    latest_balance = latest_balances[0] if latest_balances else None

    proximas_saidas.sort(key=lambda x: x['data'])
    proximas_saidas_30 = [s for s in proximas_saidas if s['data'] <= in_30]

    a_vencer_90 = [
        c for c in contracts
        if c['data_fim'] and today <= c['data_fim'] <= in_90
    ]

    overdraft_sem_dados = [
        c for c in contracts
        if c['tipo'] == 'overdraft' and (not c['tan'] or not c['plafond'])
    ]

    return {
        'contracts': contracts,
        'saldo_total': saldo_total,
        'custo_mensal_fixo': custo_mensal_fixo,
        'proximas_saidas': proximas_saidas,
        'proximas_saidas_30': proximas_saidas_30,
        'a_vencer_90': a_vencer_90,
        'overdraft_sem_dados': overdraft_sem_dados,
        'latest_balance': latest_balance,
    }


def get_prestacoes_calendar(weeks: int = 13):
    """Returns upcoming payment events for the next N weeks."""
    from datetime import date, timedelta
    today = date.today()
    end_date = today + timedelta(weeks=weeks)

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT * FROM credit_contracts WHERE estado = 'ativo' ORDER BY label"
        )
        contracts = cursor.fetchall()

        # Latest balance PER overdraft contract (handles multiple)
        cursor.execute(
            """SELECT DISTINCT ON (b.contrato_id)
                  b.*, c.label as contrato_label, c.plafond, c.tan
               FROM bank_balance_entries b
               JOIN credit_contracts c ON b.contrato_id = c.id
               WHERE c.estado = 'ativo' AND c.tipo = 'overdraft'
               ORDER BY b.contrato_id, b.data DESC"""
        )
        latest_balances = cursor.fetchall()

    # Fetch latest revision per contract in one query
    with db_connection() as conn:
        _cal_cursor = conn.cursor(cursor_factory=RealDictCursor)
        _cal_cursor.execute(
            """SELECT DISTINCT ON (contract_id)
                  contract_id, prestacao
               FROM contract_payment_revisions
               ORDER BY contract_id, data_inicio DESC, criado_em DESC"""
        )
        _cal_rev_rows = _cal_cursor.fetchall()
    _cal_latest = {r['contract_id']: float(r['prestacao']) for r in _cal_rev_rows}

    def _cal_prestacao(c):
        return _cal_latest.get(c['id'], float(c['prestacao_mensal'] or 0))

    events = []
    import calendar as cal_mod

    for c in contracts:
        if c['tipo'] == 'overdraft':
            continue
        prestacao_val = _cal_prestacao(c)
        if not c['dia_debito'] or not prestacao_val:
            continue
        dia = int(c['dia_debito'])
        cur = date(today.year, today.month, 1)
        while cur <= end_date:
            last_day = cal_mod.monthrange(cur.year, cur.month)[1]
            actual_day = min(dia, last_day)
            evt_date = date(cur.year, cur.month, actual_day)
            if today <= evt_date <= end_date:
                events.append({
                    'data': evt_date,
                    'valor': prestacao_val,
                    'label': c['label'],
                    'banco': c['banco'],
                    'tipo': c['tipo'],
                    'certa': True,
                })
            if cur.month == 12:
                cur = date(cur.year + 1, 1, 1)
            else:
                cur = date(cur.year, cur.month + 1, 1)

    for bal in latest_balances:
        if bal['custo_juros_estimado'] is not None:
            events.append({
                'data': today,
                'valor': float(bal['custo_juros_estimado']),
                'label': bal['contrato_label'],
                'banco': None,
                'tipo': 'overdraft',
                'certa': False,
            })

    events.sort(key=lambda x: x['data'])
    return events


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIRMING BANCÁRIO
# ═══════════════════════════════════════════════════════════════════════════════

def get_confirming_contracts(estado='ativo'):
    """Return all confirming-type credit contracts with current plafond utilization."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """SELECT c.*,
                      COALESCE(SUM(cp.montante) FILTER (WHERE cp.estado IN ('scheduled','confirmed','paid')), 0) AS utilizado,
                      GREATEST(0, c.plafond - COALESCE(SUM(cp.montante) FILTER (WHERE cp.estado IN ('scheduled','confirmed','paid')), 0)) AS disponivel
               FROM credit_contracts c
               LEFT JOIN confirming_parcelas cp ON cp.confirming_contract_id = c.id
               WHERE c.tipo = 'confirming' AND c.estado = %s
               GROUP BY c.id
               ORDER BY c.label""",
            (estado,)
        )
        return cursor.fetchall()


def get_confirming_dashboard():
    """Dashboard data for all active confirming contracts (plafond utilizado/disponível + parcelas)."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT * FROM credit_contracts WHERE tipo = 'confirming' AND estado = 'ativo' ORDER BY label"
        )
        contracts = cursor.fetchall()

        cursor.execute(
            """SELECT cp.*, i.supplier_name, s.name AS supplier_legal_name,
                      COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) AS supplier_display_name,
                      i.invoice_number, i.amount_eur AS invoice_amount,
                      i.due_date, c.label AS contract_label
               FROM confirming_parcelas cp
               JOIN invoices i ON i.id = cp.invoice_id
               LEFT JOIN suppliers s ON s.id = i.supplier_id
               JOIN credit_contracts c ON c.id = cp.confirming_contract_id
               WHERE cp.estado IN ('scheduled', 'confirmed', 'paid')
               ORDER BY cp.data_pagamento ASC"""
        )
        parcelas_abertas = cursor.fetchall()

        cursor.execute(
            """SELECT cp.*, i.supplier_name, s.name AS supplier_legal_name,
                      COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) AS supplier_display_name,
                      i.invoice_number, i.amount_eur AS invoice_amount,
                      i.due_date, c.label AS contract_label
               FROM confirming_parcelas cp
               JOIN invoices i ON i.id = cp.invoice_id
               LEFT JOIN suppliers s ON s.id = i.supplier_id
               JOIN credit_contracts c ON c.id = cp.confirming_contract_id
               ORDER BY cp.data_pagamento DESC
               LIMIT 100"""
        )
        todas_parcelas = cursor.fetchall()

    utilizado_map = get_confirming_utilizacao_por_contrato()

    contracts_info = []
    for c in contracts:
        plafond = float(c['plafond'] or 0)
        util_data = utilizado_map.get(c['id'], {})
        utilizado = float(util_data.get('utilizado') or 0)
        disponivel = max(0, plafond - utilizado)
        contracts_info.append({
            **dict(c),
            'plafond': plafond,
            'utilizado': utilizado,
            'disponivel': disponivel,
            'pct_utilizado': round(utilizado / plafond * 100, 1) if plafond else 0,
        })

    return {
        'contracts': contracts_info,
        'parcelas_abertas': parcelas_abertas,
        'todas_parcelas': todas_parcelas,
    }


def get_confirming_utilizacao_por_contrato(confirming_contract_id=None):
    """Return utilization amounts per confirming contract.
    Utilizado = sum of montante for parcelas with estado IN ('scheduled', 'confirmed', 'paid')
    (active parcelas that are using the plafond but not yet settled).
    """
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if confirming_contract_id:
            cursor.execute(
                """SELECT confirming_contract_id,
                          SUM(montante) AS utilizado,
                          COUNT(*) AS n_parcelas
                   FROM confirming_parcelas
                   WHERE estado IN ('scheduled', 'confirmed', 'paid')
                     AND confirming_contract_id = %s
                   GROUP BY confirming_contract_id""",
                (confirming_contract_id,)
            )
        else:
            cursor.execute(
                """SELECT confirming_contract_id,
                          SUM(montante) AS utilizado,
                          COUNT(*) AS n_parcelas
                   FROM confirming_parcelas
                   WHERE estado IN ('scheduled', 'confirmed', 'paid')
                   GROUP BY confirming_contract_id"""
            )
        rows = cursor.fetchall()
    return {r['confirming_contract_id']: dict(r) for r in rows}


def create_confirming_parcela(invoice_id, confirming_contract_id, montante, data_pagamento,
                               notas=None, estado='confirmed'):
    """Create a single confirming parcela for an invoice."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO confirming_parcelas
               (invoice_id, confirming_contract_id, montante, data_pagamento, estado, notas)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (invoice_id, confirming_contract_id, montante, data_pagamento, estado, notas)
        )
        parcela_id = cursor.fetchone()[0]
        conn.commit()
    return parcela_id


def create_confirming_parcelas_batch(invoice_id, confirming_contract_id, parcelas, estado='confirmed'):
    """Batch insert confirming parcelas for an invoice.
    `parcelas` is a list of dicts with keys: montante, data_pagamento, notas (optional).
    Replaces any existing open parcelas for this invoice+contract before inserting.
    Returns list of inserted ids.
    """
    if not parcelas:
        return []
    with db_connection() as conn:
        cursor = conn.cursor()
        # Remove existing open parcelas for this invoice+contract (confirmed/paid) to avoid duplicates
        cursor.execute(
            """DELETE FROM confirming_parcelas
               WHERE invoice_id = %s AND confirming_contract_id = %s
                 AND estado IN ('scheduled', 'confirmed', 'paid')""",
            (invoice_id, confirming_contract_id)
        )
        ids = []
        for p in parcelas:
            cursor.execute(
                """INSERT INTO confirming_parcelas
                   (invoice_id, confirming_contract_id, montante, data_pagamento, estado, notas)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (invoice_id, confirming_contract_id,
                 p['montante'], p['data_pagamento'],
                 estado, p.get('notas'))
            )
            ids.append(cursor.fetchone()[0])
        conn.commit()
    return ids


def delete_confirming_parcelas_for_invoice(invoice_id, confirming_contract_id=None):
    """Remove open parcelas for an invoice (optionally scoped to a contract)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        if confirming_contract_id:
            cursor.execute(
                """DELETE FROM confirming_parcelas
                   WHERE invoice_id = %s AND confirming_contract_id = %s
                     AND estado IN ('scheduled', 'confirmed', 'paid')""",
                (invoice_id, confirming_contract_id)
            )
        else:
            cursor.execute(
                "DELETE FROM confirming_parcelas WHERE invoice_id = %s AND estado IN ('scheduled', 'confirmed', 'paid')",
                (invoice_id,)
            )
        conn.commit()


def get_confirming_parcelas(confirming_contract_id=None, invoice_id=None, estado=None):
    """Get confirming parcelas with optional filters."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        where = []
        params = []
        if confirming_contract_id:
            where.append("cp.confirming_contract_id = %s")
            params.append(confirming_contract_id)
        if invoice_id:
            where.append("cp.invoice_id = %s")
            params.append(invoice_id)
        if estado:
            where.append("cp.estado = %s")
            params.append(estado)
        where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
        cursor.execute(
            f"""SELECT cp.*, i.supplier_name, s.name AS supplier_legal_name,
                       COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) AS supplier_display_name,
                       i.invoice_number, i.amount_eur AS invoice_amount,
                       i.due_date, c.label AS contract_label
                FROM confirming_parcelas cp
                JOIN invoices i ON i.id = cp.invoice_id
                LEFT JOIN suppliers s ON s.id = i.supplier_id
                JOIN credit_contracts c ON c.id = cp.confirming_contract_id
                {where_sql}
                ORDER BY cp.data_pagamento DESC""",
            params
        )
        return cursor.fetchall()


def update_confirming_parcela_estado(parcela_id, estado):
    """Update the estado of a confirming parcela."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE confirming_parcelas SET estado = %s, updated_at = NOW() WHERE id = %s",
            (estado, parcela_id)
        )
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# MÉTODOS DE PAGAMENTO CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

def get_payment_methods_config():
    """Return all configured payment methods."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM payment_methods_config ORDER BY metodo")
        return cursor.fetchall()


def upsert_payment_method_config(metodo, label, ativo, taxa_percentagem=None, prazo_dias=None, notas=None):
    """Insert or update a payment method config entry."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO payment_methods_config (metodo, label, ativo, taxa_percentagem, prazo_dias, notas, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (metodo) DO UPDATE
                   SET label = EXCLUDED.label,
                       ativo = EXCLUDED.ativo,
                       taxa_percentagem = EXCLUDED.taxa_percentagem,
                       prazo_dias = EXCLUDED.prazo_dias,
                       notas = EXCLUDED.notas,
                       updated_at = NOW()""",
            (metodo, label, ativo, taxa_percentagem or None, prazo_dias or None, notas or None)
        )
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# EVENTOS / CRM
# ═══════════════════════════════════════════════════════════════════════════════

EVENT_STATUSES = ['lead', 'contacted', 'proposal_sent', 'negotiating', 'won', 'lost', 'cancelled']

# ── artigos_evento ─────────────────────────────────────────────────────────────

