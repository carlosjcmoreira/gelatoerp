"""M3: Motor de Cash Flow a 13 Semanas.

Agrega todas as fontes de entradas e saídas e projecta o saldo semana a semana.
Fontes:
  - Saídas: crédito/leasings (M1), juros caucionada (M1), débitos directos (fixed),
            salários impostos (~dia 15), salários líquido (~dia 28-5),
            faturas agendadas (M0), IVA (M0 vat_periods)
  - Entradas: vendas forecast (baseado em histórico recente), recebimentos de eventos (M-Eventos)
"""
from datetime import date, timedelta
import calendar as cal_mod
import logging
from psycopg2.extras import RealDictCursor
from db.connection import db_connection
from db.forecast import get_consolidated_forecasts

logger = logging.getLogger(__name__)

SEMANAS = 13

# ── Schema / Migrations ────────────────────────────────────────────────────────

def run_migrations_cashflow():
    """Idempotent migrations for M3 cashflow tables.
    Uses an advisory lock to serialise concurrent worker executions."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202604)")
            acquired = cursor.fetchone()[0]
            if not acquired:
                return

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cashflow_config (
                    key VARCHAR(100) PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            defaults = [
                ('alert_threshold_eur', '2000'),
                ('salarios_impostos_dia', '15'),
                ('salarios_liquido_dia', '28'),
                ('salarios_impostos_eur', '0'),
                ('salarios_liquido_eur', '0'),
                ('debitos_directos', '[]'),
            ]
            for k, v in defaults:
                cursor.execute("""
                    INSERT INTO cashflow_config (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO NOTHING
                """, (k, v))

            conn.commit()
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(202604)")
                conn.commit()
            except Exception:
                pass


# ── Config helpers ─────────────────────────────────────────────────────────────

def get_cashflow_config() -> dict:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT key, value FROM cashflow_config")
        rows = cursor.fetchall()
    cfg = {r['key']: r['value'] for r in rows}
    return cfg


def set_cashflow_config(key: str, value: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO cashflow_config (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (key, value))
        conn.commit()


# ── Core aggregation engine ────────────────────────────────────────────────────

def _week_label(week_start: date) -> str:
    return f"Sem {week_start.strftime('%d/%m')}"


def build_cashflow_13weeks(weeks: int = SEMANAS) -> dict:
    """
    Builds the 13-week cash flow projection.

    Returns:
        {
          'weeks': [week_dict, ...],
          'alerts': [alert_dict, ...],
          'config': cfg_dict,
          'overdraft_total_plafond': float,
          'overdraft_utilizado': float,
        }

    week_dict keys:
        week_num, week_start, week_end, label,
        entries: [entry_dict, ...],
        total_in, total_out,
        saldo_semana, saldo_acumulado,
        usa_caucionada: bool,
        risco_real: bool,
        is_critical: bool,  (saldo_acumulado < threshold)
    """
    import json

    today = date.today()
    monday = today - timedelta(days=today.weekday())

    cfg = get_cashflow_config()
    threshold = float(cfg.get('alert_threshold_eur', 2000))
    sal_imp_dia = int(cfg.get('salarios_impostos_dia', 15))
    sal_liq_dia = int(cfg.get('salarios_liquido_dia', 28))
    sal_imp_eur = float(cfg.get('salarios_impostos_eur', 0))
    sal_liq_eur = float(cfg.get('salarios_liquido_eur', 0))

    try:
        debitos_directos = json.loads(cfg.get('debitos_directos', '[]'))
    except Exception:
        debitos_directos = []

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # ── Crédito: contratos activos (excluindo overdraft) ─────────────────
        cursor.execute("""
            SELECT id, tipo, label, banco, prestacao_mensal, dia_debito, plafond, tan, estado
            FROM credit_contracts
            WHERE estado = 'ativo' AND tipo != 'overdraft'
              AND prestacao_mensal IS NOT NULL AND dia_debito IS NOT NULL
        """)
        contracts = cursor.fetchall()

        # ── Overdraft: saldo mais recente POR contrato ────────────────────────
        cursor.execute("""
            SELECT DISTINCT ON (b.contrato_id)
                b.saldo, b.utilizacao_calculada, b.custo_juros_estimado,
                c.plafond, c.label AS label, c.id AS contrato_id
            FROM bank_balance_entries b
            JOIN credit_contracts c ON c.id = b.contrato_id
            WHERE c.estado = 'ativo' AND c.tipo = 'overdraft'
            ORDER BY b.contrato_id, b.data DESC
        """)
        overdraft_balances = cursor.fetchall()

        overdraft_total_plafond = sum(float(ob['plafond'] or 0) for ob in overdraft_balances)
        overdraft_utilizado = sum(float(ob['utilizacao_calculada'] or 0) for ob in overdraft_balances)
        overdraft_disponivel = max(0, overdraft_total_plafond - overdraft_utilizado)

        # Monthly interest estimate from latest overdraft balances (spread across 4 weeks)
        custo_juros_mensal = sum(float(ob['custo_juros_estimado'] or 0) * 12 for ob in overdraft_balances)
        custo_juros_semanal = custo_juros_mensal / 52 if custo_juros_mensal else 0

        # ── IVA: períodos activos ─────────────────────────────────────────────
        cursor.execute("""
            SELECT year, month, payment_date, declaration_date,
                   vat_due_eur, vat_due_estimated, status,
                   declaration_submitted_at
            FROM vat_periods
            WHERE status IN ('estimated', 'declared')
              AND payment_date >= %s
            ORDER BY payment_date
        """, (monday,))
        vat_periods = cursor.fetchall()

        # ── Faturas agendadas / por decidir ──────────────────────────────────
        cursor.execute("""
            SELECT i.id, i.supplier_name, i.amount_eur, i.due_date, i.status,
                   ip.proposed_date, ip.confirmed_date, ip.amount_eur AS payment_amount,
                   ip.status AS payment_status
            FROM invoices i
            LEFT JOIN invoice_payments ip ON ip.invoice_id = i.id
            WHERE i.status IN ('scheduled', 'pending_review')
              AND i.due_date IS NOT NULL
              AND i.due_date >= %s
        """, (monday,))
        invoices = cursor.fetchall()

        # ── Eventos: recebimentos pendentes ──────────────────────────────────
        cursor.execute("""
            SELECT id, event_name, client_name, invoice_amount_eur,
                   expected_payment_date, payment_status, event_date
            FROM events
            WHERE status = 'won'
              AND payment_status = 'pending'
              AND expected_payment_date IS NOT NULL
              AND expected_payment_date >= %s
        """, (monday,))
        eventos_recebimentos = cursor.fetchall()

        # ── Vendas forecast: média diária últimas 4 semanas × 7 ──────────────
        cursor.execute("""
            SELECT COALESCE(AVG(valor_euros), 0)
            FROM vendas
            WHERE data >= %s
        """, (today - timedelta(days=28),))
        avg_daily_sales = float(cursor.fetchone()['coalesce'] or 0)
        weekly_sales_forecast = avg_daily_sales * 7

        # ── Vendas forecast mais refinado: por semana do ano (YoY) ───────────
        # Se temos histórico do mesmo período do ano passado, usa-o
        last_year_start = monday - timedelta(weeks=52)
        last_year_end = last_year_start + timedelta(weeks=weeks)
        cursor.execute("""
            SELECT
                DATE_TRUNC('week', data)::DATE AS week_start,
                SUM(valor_euros) AS total
            FROM vendas
            WHERE data BETWEEN %s AND %s
            GROUP BY 1
            ORDER BY 1
        """, (last_year_start, last_year_end))
        yoy_rows = {r['week_start']: float(r['total'] or 0) for r in cursor.fetchall()}

    # ── Previsão meteorológica: só para Semana 1 (próximos 7 dias) ───────────
    # get_consolidated_forecasts usa a sua própria conexão DB — chamar fora do
    # bloco with db_connection() para evitar conflito de transacção.
    # Resultado: dict {monday: {total, banda_min, banda_max}} ou {} se vazio/erro.
    meteo_week1: dict = {}
    try:
        forecast_end = monday + timedelta(days=6)
        consolidated = get_consolidated_forecasts(from_date=monday, to_date=forecast_end)
        for row in consolidated:
            d = row['data']
            ws = d - timedelta(days=d.weekday())
            if ws not in meteo_week1:
                meteo_week1[ws] = {'total': 0.0, 'banda_min': 0.0, 'banda_max': 0.0}
            meteo_week1[ws]['total'] += float(row['previsao_eur'] or 0)
            meteo_week1[ws]['banda_min'] += float(row['banda_min'] or 0)
            meteo_week1[ws]['banda_max'] += float(row['banda_max'] or 0)
    except Exception as _e:
        logger.warning('Não foi possível obter previsão meteorológica para cashflow: %s', _e)

    # ── Build weeks ───────────────────────────────────────────────────────────
    result_weeks = []
    saldo_acumulado = 0.0

    for w in range(weeks):
        week_start = monday + timedelta(weeks=w)
        week_end = week_start + timedelta(days=6)
        entries = []

        # ── SAÍDAS ───────────────────────────────────────────────────────────

        # 1) Leasings / Créditos (certa, dia fixo)
        for c in contracts:
            dia = int(c['dia_debito'])
            # Check if this payment day falls within this week
            for m_offset in range(2):  # Check current and next month
                check_month = week_start.month + m_offset
                check_year = week_start.year
                if check_month > 12:
                    check_month -= 12
                    check_year += 1
                last_day = cal_mod.monthrange(check_year, check_month)[1]
                actual_day = min(dia, last_day)
                payment_date = date(check_year, check_month, actual_day)
                if week_start <= payment_date <= week_end:
                    entries.append({
                        'tipo': 'saida',
                        'categoria': 'credito',
                        'label': c['label'],
                        'valor_eur': float(c['prestacao_mensal']),
                        'certeza': 'certa',
                        'data_prevista': payment_date,
                        'fonte': 'credito_contracts',
                        'fonte_ref': str(c['id']),
                    })
                    break

        # 2) Juros caucionada (estimada, mensal — spread semanal)
        if custo_juros_semanal > 0:
            entries.append({
                'tipo': 'saida',
                'categoria': 'juros_caucionada',
                'label': 'Juros Conta Caucionada (est.)',
                'valor_eur': round(custo_juros_semanal, 2),
                'certeza': 'estimada',
                'data_prevista': week_start,
                'fonte': 'bank_balance',
                'fonte_ref': None,
            })

        # 3) Débitos directos (certa, dia fixo)
        for dd in debitos_directos:
            dia = int(dd.get('dia_debito', 0))
            valor = float(dd.get('valor_eur', 0))
            if not dia or not valor:
                continue
            for m_offset in range(2):
                check_month = week_start.month + m_offset
                check_year = week_start.year
                if check_month > 12:
                    check_month -= 12
                    check_year += 1
                last_day = cal_mod.monthrange(check_year, check_month)[1]
                actual_day = min(dia, last_day)
                payment_date = date(check_year, check_month, actual_day)
                if week_start <= payment_date <= week_end:
                    entries.append({
                        'tipo': 'saida',
                        'categoria': 'debito_directo',
                        'label': dd.get('label', 'Débito Directo'),
                        'valor_eur': valor,
                        'certeza': 'certa',
                        'data_prevista': payment_date,
                        'fonte': 'config',
                        'fonte_ref': None,
                    })
                    break

        # 4) Salários impostos (~dia 15, certa)
        if sal_imp_eur > 0:
            for m_offset in range(2):
                check_month = week_start.month + m_offset
                check_year = week_start.year
                if check_month > 12:
                    check_month -= 12
                    check_year += 1
                payment_date = date(check_year, check_month, sal_imp_dia)
                if week_start <= payment_date <= week_end:
                    entries.append({
                        'tipo': 'saida',
                        'categoria': 'salarios_impostos',
                        'label': 'Salários + Impostos (SS/IRS)',
                        'valor_eur': sal_imp_eur,
                        'certeza': 'certa',
                        'data_prevista': payment_date,
                        'fonte': 'config',
                        'fonte_ref': None,
                    })
                    break

        # 5) Salários líquido (~dia 28-5, certa)
        if sal_liq_eur > 0:
            for m_offset in range(2):
                check_month = week_start.month + m_offset
                check_year = week_start.year
                if check_month > 12:
                    check_month -= 12
                    check_year += 1
                payment_date = date(check_year, check_month, sal_liq_dia)
                if week_start <= payment_date <= week_end:
                    entries.append({
                        'tipo': 'saida',
                        'categoria': 'salarios_liquido',
                        'label': 'Salários Líquido',
                        'valor_eur': sal_liq_eur,
                        'certeza': 'certa',
                        'data_prevista': payment_date,
                        'fonte': 'config',
                        'fonte_ref': None,
                    })
                    break

        # 6) Faturas agendadas (muito provável / confirmada)
        for inv in invoices:
            # Use confirmed_date > proposed_date > due_date as payment date
            pay_date = inv.get('confirmed_date') or inv.get('proposed_date') or inv.get('due_date')
            if not pay_date:
                continue
            if week_start <= pay_date <= week_end:
                valor = float(inv.get('payment_amount') or inv.get('amount_eur') or 0)
                is_overdue = inv.get('due_date') and inv['due_date'] < today
                certeza = 'certa' if inv.get('payment_status') == 'confirmed' else 'muito_provavel'
                entries.append({
                    'tipo': 'saida',
                    'categoria': 'fatura',
                    'label': inv.get('supplier_name', 'Fornecedor'),
                    'valor_eur': valor,
                    'certeza': certeza,
                    'data_prevista': pay_date,
                    'fonte': 'invoices',
                    'fonte_ref': str(inv['id']),
                    'is_overdue': is_overdue,
                    'due_date': inv.get('due_date'),
                })

        # 7) IVA (estimada → confirmada, dia 25 do M+2)
        for vp in vat_periods:
            pay_date = vp.get('payment_date')
            if not pay_date:
                continue
            if week_start <= pay_date <= week_end:
                valor = float(vp.get('vat_due_eur') or vp.get('vat_due_estimated') or 0)
                certeza = 'confirmada' if vp['status'] == 'declared' else 'estimada'
                mes_str = f"{vp['month']:02d}/{vp['year']}"
                entries.append({
                    'tipo': 'saida',
                    'categoria': 'iva',
                    'label': f'IVA {mes_str}',
                    'valor_eur': valor,
                    'certeza': certeza,
                    'data_prevista': pay_date,
                    'fonte': 'vat_periods',
                    'fonte_ref': f"{vp['year']}-{vp['month']}",
                    'declarado': vp.get('declaration_submitted_at') is not None,
                })

        # ── ENTRADAS ─────────────────────────────────────────────────────────

        # 8) Vendas forecast (estimada)
        # Semana 1 (w==0): previsão meteorológica se disponível, senão rolling avg
        # Semanas 2-13 (w>0): YoY se disponível, senão rolling avg
        yoy_key = week_start - timedelta(weeks=52)
        banda_min = None
        banda_max = None
        if w == 0 and monday in meteo_week1:
            # Previsão meteorológica disponível para Semana 1
            # Usamos independentemente do total (inclui total == 0)
            _m = meteo_week1[monday]
            sales_est = _m['total']
            sales_label = 'Vendas (previsão meteorológica)'
            banda_min = round(_m['banda_min'], 2)
            banda_max = round(_m['banda_max'], 2)
        elif w == 0:
            # Semana 1 sem previsão meteo → rolling average (não YoY)
            sales_est = weekly_sales_forecast
            sales_label = 'Vendas (média 4 semanas)'
        elif yoy_key in yoy_rows and yoy_rows[yoy_key] > 0:
            sales_est = yoy_rows[yoy_key]
            sales_label = 'Vendas (YoY estimado)'
        else:
            sales_est = weekly_sales_forecast
            sales_label = 'Vendas (média 4 semanas)'
        if sales_est > 0:
            _entry = {
                'tipo': 'entrada',
                'categoria': 'vendas',
                'label': sales_label,
                'valor_eur': round(sales_est, 2),
                'certeza': 'estimada',
                'data_prevista': week_start,
                'fonte': 'vendas',
                'fonte_ref': None,
            }
            if banda_min is not None:
                _entry['banda_min'] = banda_min
                _entry['banda_max'] = banda_max
            entries.append(_entry)

        # 9) Recebimentos de eventos (muito provável)
        for ev in eventos_recebimentos:
            pay_date = ev.get('expected_payment_date')
            if not pay_date:
                continue
            if week_start <= pay_date <= week_end:
                valor = float(ev.get('invoice_amount_eur') or 0)
                nome = ev.get('event_name') or ev.get('client_name') or 'Evento'
                entries.append({
                    'tipo': 'entrada',
                    'categoria': 'evento',
                    'label': f'Recebimento: {nome}',
                    'valor_eur': valor,
                    'certeza': 'muito_provavel',
                    'data_prevista': pay_date,
                    'fonte': 'events',
                    'fonte_ref': str(ev['id']),
                })

        # ── Totals ────────────────────────────────────────────────────────────
        total_in = sum(e['valor_eur'] for e in entries if e['tipo'] == 'entrada')
        total_out = sum(e['valor_eur'] for e in entries if e['tipo'] == 'saida')
        saldo_semana = total_in - total_out
        saldo_acumulado += saldo_semana

        usa_caucionada = saldo_acumulado < 0 and abs(saldo_acumulado) <= overdraft_disponivel
        risco_real = saldo_acumulado < 0 and abs(saldo_acumulado) > overdraft_disponivel
        is_critical = saldo_acumulado < threshold

        result_weeks.append({
            'week_num': w + 1,
            'week_start': week_start,
            'week_end': week_end,
            'label': _week_label(week_start),
            'entries': entries,
            'total_in': round(total_in, 2),
            'total_out': round(total_out, 2),
            'saldo_semana': round(saldo_semana, 2),
            'saldo_acumulado': round(saldo_acumulado, 2),
            'usa_caucionada': usa_caucionada,
            'risco_real': risco_real,
            'is_critical': is_critical,
        })

    # ── Alerts ────────────────────────────────────────────────────────────────
    alerts = _build_alerts(result_weeks, threshold, today, overdraft_disponivel, vat_periods, invoices)

    return {
        'weeks': result_weeks,
        'alerts': alerts,
        'config': cfg,
        'threshold': threshold,
        'overdraft_total_plafond': overdraft_total_plafond,
        'overdraft_utilizado': overdraft_utilizado,
        'overdraft_disponivel': overdraft_disponivel,
        'today': today,
    }


def _build_alerts(weeks, threshold, today, overdraft_disponivel, vat_periods, invoices) -> list:
    """Build the full alert list per PRD specification."""
    alerts = []

    for w in weeks:
        # 🔴 CRÍTICO: saldo negativo mesmo com caucionada
        if w['risco_real']:
            alerts.append({
                'nivel': 'critico',
                'icon': '🔴',
                'tipo': 'saldo_negativo_critico',
                'label': f"Saldo negativo sem cobertura caucionada: {w['saldo_acumulado']:+,.0f}€ na semana {w['label']}",
                'semana': w['week_num'],
                'week_start': w['week_start'],
            })

        # 🟡 ATENÇÃO: saldo abaixo do threshold
        if w['is_critical'] and not w['risco_real']:
            alerts.append({
                'nivel': 'atencao',
                'icon': '🟡',
                'tipo': 'saldo_abaixo_threshold',
                'label': f"Saldo acumulado abaixo de {threshold:,.0f}€ na semana {w['label']}: {w['saldo_acumulado']:+,.0f}€",
                'semana': w['week_num'],
                'week_start': w['week_start'],
            })

        # 🟡 ATENÇÃO: sobreposição de saídas grandes (> 2 saídas cegas no mesmo dia)
        saidas_por_data = {}
        for e in w['entries']:
            if e['tipo'] == 'saida' and e.get('certeza') in ('certa', 'confirmada'):
                d = str(e.get('data_prevista', ''))
                saidas_por_data.setdefault(d, []).append(e)
        for d, saidas in saidas_por_data.items():
            if len(saidas) >= 2 and sum(s['valor_eur'] for s in saidas) > threshold:
                alerts.append({
                    'nivel': 'atencao',
                    'icon': '🟡',
                    'tipo': 'sobreposicao_saidas',
                    'label': f"Sobreposição de {len(saidas)} saídas em {d} (sem. {w['label']}): {sum(s['valor_eur'] for s in saidas):,.0f}€",
                    'semana': w['week_num'],
                    'week_start': w['week_start'],
                })
                break

    # 🔴 CRÍTICO: fatura vencida > 3 dias
    for inv in invoices:
        due = inv.get('due_date')
        if due and due < today and (today - due).days > 3 and inv.get('status') == 'scheduled':
            alerts.append({
                'nivel': 'critico',
                'icon': '🔴',
                'tipo': 'fatura_vencida',
                'label': f"Fatura vencida há {(today - due).days} dias: {inv.get('supplier_name', '?')} — {float(inv.get('amount_eur') or 0):,.0f}€",
                'semana': None,
                'week_start': None,
                'invoice_id': inv['id'],
            })

    # 🟡 ATENÇÃO: IVA não declarado com payment_date nos próximos 30 dias
    for vp in vat_periods:
        decl_date = vp.get('declaration_date')
        if vp['status'] == 'estimated' and decl_date:
            days_left = (decl_date - today).days
            if 0 <= days_left <= 30:
                from db.pagamentos import VAT_RATES
                mes_str = f"{vp['month']:02d}/{vp['year']}"
                alerts.append({
                    'nivel': 'atencao',
                    'icon': '🟡',
                    'tipo': 'iva_nao_declarado',
                    'label': f"IVA {mes_str} não declarado — prazo em {days_left} dia(s) ({decl_date.strftime('%d/%m/%Y')})",
                    'semana': None,
                    'week_start': None,
                    'vat_year': vp['year'],
                    'vat_month': vp['month'],
                })

    # 🟡 ATENÇÃO: faturas por decidir (pending_review)
    faturas_por_decidir = [i for i in invoices if i.get('status') == 'pending_review']
    if faturas_por_decidir:
        alerts.append({
            'nivel': 'atencao',
            'icon': '🟡',
            'tipo': 'faturas_por_decidir',
            'label': f"{len(faturas_por_decidir)} fatura(s) pendentes de revisão/agendamento",
            'semana': None,
            'week_start': None,
        })

    # Sort: critico first, then atencao
    alerts.sort(key=lambda a: (0 if a['nivel'] == 'critico' else 1, a.get('semana') or 99))
    return alerts
