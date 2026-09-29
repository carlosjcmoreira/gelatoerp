from flask import Blueprint, render_template, request, session, redirect, url_for, flash, abort
from flask_app.auth import perm_required
import sys, os
import json
import math
import re
import unicodedata
from datetime import date, timedelta
import pandas as pd
import plotly.graph_objects as go
import plotly

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    calculate_kpi_annual, calculate_kpi_by_day, get_target_by_month,
    get_consumo_gelado_mensal,
    get_vendas_produto_mensal,
)
from db.connection import db_connection
from db.pastelaria import (
    get_precos_caixa_kg_historico, add_preco_caixa_kg, delete_preco_caixa_kg,
    get_volume_por_produto,
)
from db.doseamento import (
    get_dose_product_configuration_queue, set_product_dose,
    confirm_historical_dose_preview, create_historical_dose_preview,
    get_historical_dose_preview,
    get_doseamento_period, get_historical_dose_coverage,
    get_vendas_ao_peso_sem_peso_calculavel,
    get_dose_alias_coverage_alerts,
)

eurokg_bp = Blueprint('eurokg', __name__)


MENU_ITEMS = [
    {'id': 'dashboard', 'icon': '📊', 'label': 'Dashboard Euro/kg', 'description': 'Compare kg teóricos calculáveis com o consumo operacional dos últimos 30 dias e veja os avisos de dados.', 'url_endpoint': 'eurokg.dashboard'},
    {'id': 'resumo', 'icon': '📅', 'label': 'Resumo Mensal', 'description': 'Acompanhe por mês os kg teóricos conhecidos, o consumo operacional e os artigos em falta.', 'url_endpoint': 'eurokg.resumo_mensal'},
    {'id': 'consumo', 'icon': '🧮', 'label': 'Consumo Teórico', 'description': 'Consulte e configure as doses usadas para converter vendas em kg teóricos.', 'url_endpoint': 'eurokg.consumo_teorico'},
    {'id': 'vendas', 'icon': '💶', 'label': 'Vendas por Produto', 'description': 'Veja unidades e valor vendido por produto e por mês.', 'url_endpoint': 'eurokg.vendas_produto'},
    {'id': 'pesagens', 'icon': '⚖️', 'label': 'Pesagens', 'description': 'Consulte as pesagens que sustentam o cálculo do consumo real.', 'url_endpoint': 'eurokg.pesagens'},
    {'id': 'diagnostico', 'icon': '🔍', 'label': 'Diagnóstico de Vendas', 'description': 'Detete dias sem vendas importadas ou com valores anormalmente baixos.', 'url_endpoint': 'eurokg.diagnostico_vendas', 'gestor_only': True},
    {'id': 'volume', 'icon': '📦', 'label': 'Volume por Produto', 'description': 'Analise unidades, vendas e kg estimados por produto no período escolhido.', 'url_endpoint': 'eurokg.volume_produtos', 'gestor_only': True},
    {'id': 'config_preco', 'icon': '⚙️', 'label': 'Preço/kg Caixas Loja', 'description': 'Configure o histórico de preço/kg usado para estimar caixas vendidas.', 'url_endpoint': 'eurokg.config_preco_caixa', 'gestor_only': True},
]

MESES_PT_ABREV = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                  'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']

_DASHBOARD_ISSUE_GUIDANCE = {
    'insufficient_coverage': (
        'Cobertura física abaixo do período completo',
        'A cobertura conta dias cobertos por intervalos físicos válidos; no Global Porto é o mínimo entre lojas e sabores relevantes.',
    ),
    'no_usable_intervals': (
        'Sem intervalos físicos válidos',
        'Registe pesagens comparáveis e confirme os movimentos entre elas.',
    ),
    'invalid_real_consumption': (
        'Consumo real inválido',
        'Reveja pesagens, produção, transferências, quebras e respetivas datas.',
    ),
    'unmapped_products': (
        'Há produtos vendidos sem dose histórica associada',
        'Associe uma dose válida à data da venda antes de comparar o consumo teórico.',
    ),
    'weight_products': (
        'Há vendas ao peso sem quantidade em kg',
        'Complete o peso vendido para esses produtos; não é estimado automaticamente.',
    ),
    'no_sales': (
        'Sem vendas importadas no período',
        'Confirme a importação das vendas e a loja selecionada.',
    ),
    'insufficient_snapshots': (
        'Faltam pesagens comparáveis',
        'Registe duas pesagens do mesmo sabor para formar um intervalo.',
    ),
    'no_intervals_in_period': (
        'Sem intervalos completos neste período',
        'Confirme as datas das pesagens e amplie o período se necessário.',
    ),
    'negative_stock_residual': (
        'Os movimentos não conciliam com as pesagens',
        'Reveja quantidades e datas de produção, transferências, quebras e stock.',
    ),
    'manual_only_production': (
        'Há produção manual de controlo sem produção física importada',
        'Importe a produção física correta. O registo manual não substitui a balança.',
    ),
    'duplicate_snapshot': (
        'Existem pesagens duplicadas',
        'Confirme qual registo representa a pesagem correta e corrija a duplicação.',
    ),
    'unassigned_transfer_origin': (
        'Origem de transferência não identificada',
        'Associe a transferência à loja de origem antes de validar o intervalo.',
    ),
    'unknown_transfer_destination': (
        'Destino de transferência não identificado',
        'Confirme o destino físico ou indique que se trata de um destino externo.',
    ),
    'unknown_transfer_status': (
        'Estado de transferência não reconhecido',
        'Corrija o estado para que o movimento físico seja interpretado corretamente.',
    ),
    'missing_transfer_confirmation_date': (
        'Transferência confirmada sem data de confirmação',
        'Registe a data em que a transferência foi efetivamente confirmada.',
    ),
    'ambiguous_transfer_overlap': (
        'Há movimentos de transferência sobrepostos',
        'Confirme a origem e evite contar o mesmo movimento em mais de uma fonte.',
    ),
    'invalid_snapshot_quantity': (
        'Pesagem com quantidade inválida',
        'Corrija a quantidade da pesagem.',
    ),
    'invalid_production_quantity': (
        'Produção com quantidade inválida',
        'Corrija a quantidade da produção física.',
    ),
    'invalid_transfer_quantity': (
        'Transferência com quantidade inválida',
        'Corrija a quantidade transferida.',
    ),
    'invalid_receipt_quantity': (
        'Receção com quantidade inválida',
        'Corrija a quantidade recebida.',
    ),
    'invalid_breakage_quantity': (
        'Quebra com quantidade inválida',
        'Corrija a quantidade da quebra.',
    ),
    'unresolved_snapshot_identity': (
        'Loja ou sabor de uma pesagem não identificado',
        'Associe a pesagem a uma loja e a um sabor reconhecidos.',
    ),
    'unresolved_production_identity': (
        'Loja ou sabor de uma produção não identificado',
        'Associe a produção a uma loja física e a um sabor reconhecidos.',
    ),
    'unresolved_transfer_identity': (
        'Loja ou sabor de uma transferência não identificado',
        'Confirme a loja física e o sabor associados à transferência.',
    ),
    'unresolved_receipt_identity': (
        'Loja ou sabor de uma receção não identificado',
        'Confirme a loja física e o sabor associados à receção.',
    ),
    'unresolved_breakage_identity': (
        'Loja ou sabor de uma quebra não identificado',
        'Confirme a loja física e o sabor associados à quebra.',
    ),
    'invalid_snapshot_order': (
        'A ordem temporal das pesagens é inválida',
        'Confirme as datas e horas das pesagens.',
    ),
}

_DASHBOARD_FLAG_GUIDANCE = {
    'long_interval': (
        'Intervalo superior a 3 dias',
        'A leitura é menos representativa; registe pesagens com maior frequência.',
    ),
    'inferred_transfer_origin': (
        'Origem da transferência inferida',
        'Confirme a loja de origem para tornar o movimento auditável.',
    ),
    'legacy_transfer_source': (
        'Movimento recuperado do registo histórico',
        'Confirme o movimento na fonte histórica, se necessário.',
    ),
    'receipt_transfer_source': (
        'Saída recuperada do registo de receções',
        'Confirme que a saída não está duplicada noutra fonte.',
    ),
}


def _dashboard_guidance(issue, *, is_flag=False):
    key = issue
    if issue.startswith('rotation_flag:'):
        key = issue.split(':', 1)[1]
        is_flag = True
    mapping = _DASHBOARD_FLAG_GUIDANCE if is_flag else _DASHBOARD_ISSUE_GUIDANCE
    title, action = mapping.get(
        key,
        ('Outro problema de qualidade dos dados',
         'Verifique os registos associados a este período.'),
    )
    return {'title': title, 'action': action}


def _prepare_dashboard_quality(doseamento):
    """Replace internal data-quality keys with readable dashboard guidance."""
    doseamento['issue_guidance'] = [
        _dashboard_guidance(issue)
        for issue in doseamento.get('issues', [])
    ]
    for interval in doseamento.get('interval_diagnostics', []):
        interval['issue_guidance'] = [
            _dashboard_guidance(issue)
            for issue in interval.get('issues', [])
        ]
        interval['flag_guidance'] = [
            _dashboard_guidance(flag, is_flag=True)
            for flag in interval.get('flags', [])
        ]
    for gap in doseamento.get('coverage_gaps', []):
        gap['issue_guidance'] = [
            _dashboard_guidance(issue)
            for issue in gap.get('issues', [])
        ]
    return doseamento


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _known_theoretical_kg(doseamento):
    """Return the mapped subtotal even when audit quality is incomplete."""
    store_rows = doseamento.get('stores')
    if store_rows is not None:
        values = [
            _finite_float(row.get('mapped_theoretical_kg'))
            for row in store_rows
        ]
        known_values = [value for value in values if value is not None]
        return sum(known_values) if known_values else None

    value = _finite_float(doseamento.get('mapped_theoretical_kg'))
    if value is not None:
        return value
    if doseamento.get('status') == 'reliable':
        return _finite_float(doseamento.get('theoretical_kg'))
    return None


def _missing_product_details(doseamento):
    """Flatten store-scoped dose gaps into concise, actionable rows."""
    store_rows = doseamento.get('stores')
    if store_rows is None:
        store_rows = [doseamento]

    missing = []
    for store_row in store_rows:
        store_name = store_row.get('loja') or store_row.get('name') or ''
        detail_sets = (
            ('Sem dose histórica', 'unmapped_product_details',
             'unmapped_products'),
            ('Sem kg vendido', 'weighted_product_details',
             'weighted_products'),
        )
        for reason, detail_key, names_key in detail_sets:
            details = store_row.get(detail_key) or []
            if not details:
                details = [
                    {'product': product, 'quantity': None, 'revenue': None}
                    for product in store_row.get(names_key, [])
                ]
            for detail in details:
                missing.append({
                    'store': store_name,
                    'product': detail.get('product', ''),
                    'reason': reason,
                    'quantity': _finite_float(detail.get('quantity')),
                    'revenue': _finite_float(detail.get('revenue')),
                })
    return sorted(
        missing,
        key=lambda item: (
            str(item['store']).casefold(),
            str(item['product']).casefold(),
            item['reason'],
        ),
    )


def _prepare_operational_estimate(
    doseamento, operational_kg, operational_revenue
):
    """Combine known theoretical kg with the legacy operational series.

    Audit status remains available separately; it no longer gates these
    indicative values.
    """
    theoretical_kg = _known_theoretical_kg(doseamento)
    operational_kg = _finite_float(operational_kg)
    operational_revenue = _finite_float(operational_revenue)
    missing_products = _missing_product_details(doseamento)
    variance_kg = (
        operational_kg - theoretical_kg
        if operational_kg is not None and theoretical_kg is not None
        else None
    )
    yield_pct = (
        theoretical_kg * 100 / operational_kg
        if theoretical_kg is not None and operational_kg is not None
        and operational_kg > 0 else None
    )
    revenue_per_kg = (
        operational_revenue / operational_kg
        if operational_revenue is not None and operational_kg is not None
        and operational_kg > 0 else None
    )
    missing_sales_eur = sum(
        item['revenue'] or 0 for item in missing_products
    )

    audit_issues = set(doseamento.get('issues', [])) - {
        'unmapped_products', 'weight_products', 'no_sales',
    }
    audit_status = doseamento.get('status')
    if 'no_sales' in doseamento.get('issues', []) and not missing_products:
        audit_note = 'Sem vendas importadas neste período.'
    elif audit_status == 'invalid' or audit_issues:
        audit_note = 'Auditoria física incompleta ou inválida; ver Pesagens.'
    elif audit_status == 'incomplete' and not missing_products:
        audit_note = 'Auditoria física incompleta; ver Pesagens.'
    else:
        audit_note = None

    return {
        'theoretical_kg': theoretical_kg,
        'operational_kg': operational_kg,
        'variance_kg': variance_kg,
        'yield_pct': yield_pct,
        'revenue_per_kg': revenue_per_kg,
        'missing_products': missing_products,
        'missing_products_count': len(missing_products),
        'missing_sales_eur': missing_sales_eur,
        'is_partial': bool(missing_products),
        'theoretical_status': (
            'Parcial' if missing_products
            else 'Calculado' if theoretical_kg is not None
            else 'Sem dados'
        ),
        'audit_status_label': {
            'reliable': 'Fiável',
            'incomplete': 'Incompleta',
            'invalid': 'Inválida',
        }.get(audit_status, 'Desconhecida'),
        'audit_note': audit_note,
    }


def _parse_product_dose_batch(form):
    """Parse a batch while allowing incomplete rows to remain unchanged."""
    product_ids = form.getlist('product_id')
    dose_types = form.getlist('tipo_dose')
    grams_values = form.getlist('gramas')
    effective_values = form.getlist('data_efetiva')
    if not product_ids:
        raise ValueError('Não há artigos para guardar.')
    if not effective_values:
        effective_values = [None] * len(product_ids)
    if not (
        len(product_ids) == len(dose_types) == len(grams_values)
        == len(effective_values)
    ):
        raise ValueError('A configuração dos artigos está incompleta.')

    changes = []
    skipped_empty = 0
    invalid_rows = []
    seen_ids = set()
    for product_id_raw, dose_type_raw, grams_raw, effective_raw in zip(
        product_ids, dose_types, grams_values, effective_values
    ):
        try:
            product_id = int(product_id_raw)
        except (TypeError, ValueError):
            invalid_rows.append('Artigo faturado inválido.')
            continue
        if product_id in seen_ids:
            invalid_rows.append(
                f'Artigo {product_id}: aparece mais do que uma vez.'
            )
            continue
        seen_ids.add(product_id)

        dose_type = str(dose_type_raw or 'fixa').strip()
        if dose_type not in {'fixa', 'peso'}:
            invalid_rows.append(
                f'Artigo {product_id}: tipo de venda inválido.'
            )
            continue
        if dose_type == 'peso':
            grams = None
        else:
            grams_text = str(grams_raw or '').strip()
            if not grams_text:
                skipped_empty += 1
                continue
            grams_text = grams_text.replace(',', '.')
            try:
                grams = float(grams_text)
            except (TypeError, ValueError):
                invalid_rows.append(
                    f'Artigo {product_id}: indique um valor de gramas válido.'
                )
                continue
            if not math.isfinite(grams) or grams <= 0:
                invalid_rows.append(
                    f'Artigo {product_id}: indique gramas superiores a zero.'
                )
                continue
        effective_from = None
        effective_text = str(effective_raw or '').strip()
        if effective_text:
            try:
                effective_from = date.fromisoformat(effective_text)
            except ValueError:
                invalid_rows.append(
                    f'Artigo {product_id}: indique uma data de entrada em vigor válida.'
                )
                continue
            if effective_from > date.today():
                invalid_rows.append(
                    f'Artigo {product_id}: a data de entrada em vigor não pode ser futura.'
                )
                continue
        changes.append((product_id, grams, dose_type, effective_from))
    return changes, skipped_empty, invalid_rows


def _store_context(user):
    """Resolve an authorized store and the dynamic dashboard switcher."""
    from db.auth import get_store_by_id, get_vendas_module_stores
    stores = get_vendas_module_stores()
    is_gestor = user.get('acesso_gestor', False)
    requested = request.args.get('loja')
    if is_gestor:
        selected = requested or 'Global Porto'
        names = {store['name'] for store in stores}
        if selected != 'Global Porto' and selected not in names:
            selected = 'Global Porto'
        return selected, None if selected == 'Global Porto' else selected, stores
    allowed_ids = user.get('vendas_store_ids') or []
    allowed = [store for store in stores if store['id'] in allowed_ids]
    fallback = get_store_by_id(user.get('loja_id')) if not allowed else None
    if fallback and fallback.get('name'):
        allowed = [fallback]
    names = {store['name'] for store in allowed}
    if not names:
        abort(403)
    selected = requested if requested in names else (
        allowed[0]['name'] if allowed else None
    )
    return selected, selected, allowed


def _consumo_family_parts(product_name):
    """Return a conservative display family based on the first name token."""
    compact_name = ' '.join(str(product_name or '').split())
    if not compact_name:
        return '', ''
    display_family = compact_name.split(' ', 1)[0].strip('.,;:()[]{}')
    normalized = unicodedata.normalize('NFKD', display_family.casefold())
    family_key = ''.join(
        character for character in normalized
        if not unicodedata.combining(character)
    )
    family_key = re.sub(r'^[^\w]+|[^\w/+-]+$', '', family_key)
    preferred_labels = {
        'biscoito': 'Biscoito',
        'caixa': 'Caixa',
        'nivotto': 'Nivotto',
        'palito': 'Palito',
    }
    return family_key, preferred_labels.get(family_key, display_family)


def _consumo_rows_incomplete(rows):
    if rows.empty:
        return False
    if 'consumo_incompleto' in rows:
        return bool(rows['consumo_incompleto'].fillna(False).any())
    # Keep the view defensive for older callers that do not provide the
    # source flag; the database query always does.
    return bool(rows['consumo_kg'].isna().any())


def _build_consumo_teorico_view(consumo_df):
    """Build the theoretical-consumption matrix with provisional zeroes.

    Missing consumption remains marked as incomplete in the source data and in
    the historical coverage view.  Only the displayed kg matrix treats those
    missing contributions as zero so known values can still be summed.
    """
    if consumo_df.empty:
        return [], [], [], []
    from datetime import datetime
    meses = sorted(consumo_df['mes'].unique())
    meses_labels = [
        datetime.strptime(mes, '%Y-%m').strftime('%b %Y')
        for mes in meses
    ]
    product_items = []
    totals_qty = []
    totals_kg = []

    for mes in meses:
        month_rows = consumo_df[consumo_df['mes'] == mes]
        totals_qty.append(int(month_rows['quantidade_vendida'].sum()))
        totals_kg.append(round(
            float(month_rows['consumo_kg'].fillna(0).sum()), 2
        ))
    for produto in sorted(consumo_df['produto'].unique()):
        product_rows = consumo_df[consumo_df['produto'] == produto]
        item = {
            'kind': 'product',
            'produto': produto,
            'months': [],
        }
        for mes in meses:
            rows = product_rows[product_rows['mes'] == mes]
            qty = int(rows['quantidade_vendida'].sum()) if not rows.empty else 0
            kg = (
                round(float(rows['consumo_kg'].fillna(0).sum()), 2)
                if not rows.empty else 0
            )
            item['months'].append({
                'qty': qty,
                'kg': kg,
                'incomplete': _consumo_rows_incomplete(rows),
            })
        item['incomplete'] = any(
            month['incomplete'] for month in item['months']
        )
        family_key, family_label = _consumo_family_parts(produto)
        item['family_key'] = family_key
        item['family_label'] = family_label
        product_items.append(item)

    family_products = {}
    for item in product_items:
        family_products.setdefault(item['family_key'], []).append(item)

    consumo_data = []
    for family_key, products in sorted(
        family_products.items(),
        key=lambda value: (
            value[1][0]['family_label'].casefold(),
            value[0],
        ),
    ):
        products.sort(key=lambda item: item['produto'].casefold())
        if not family_key or len(products) == 1:
            consumo_data.extend(products)
            continue
        group_months = []
        for month_index in range(len(meses)):
            child_months = [
                product['months'][month_index] for product in products
            ]
            group_months.append({
                'qty': sum(month['qty'] for month in child_months),
                'kg': round(sum(month['kg'] for month in child_months), 2),
                'incomplete': any(
                    month['incomplete'] for month in child_months
                ),
            })
        consumo_data.append({
            'kind': 'group',
            'family': products[0]['family_label'],
            'product_count': len(products),
            'months': group_months,
            'products': products,
            'incomplete': any(
                product['incomplete'] for product in products
            ),
        })

    group_number = 0
    for item in consumo_data:
        if item['kind'] == 'group':
            group_number += 1
            item['group_id'] = f'consumo-family-{group_number}'
    return consumo_data, meses_labels, totals_qty, totals_kg


def _consumo_incomplete_totals(consumo_df):
    """Return the source incomplete flag for each displayed month."""
    if consumo_df.empty:
        return []
    incomplete = []
    for mes in sorted(consumo_df['mes'].unique()):
        incomplete.append(
            _consumo_rows_incomplete(consumo_df[consumo_df['mes'] == mes])
        )
    return incomplete


def _build_tabs(loja_filter, is_gestor, active):
    from db.tiles import get_tile_icons, get_tile_labels
    icons = get_tile_icons('eurokg')
    labels = get_tile_labels('eurokg')
    tabs = [
        {'id': 'dashboard', 'label': labels.get('dashboard') or 'Dashboard', 'icon': icons.get('dashboard') or '📊', 'url': url_for('eurokg.dashboard', loja=loja_filter)},
        {'id': 'resumo', 'label': labels.get('resumo') or 'Resumo Mensal', 'icon': icons.get('resumo') or '📅', 'url': url_for('eurokg.resumo_mensal', loja=loja_filter)},
        {'id': 'vendas', 'label': labels.get('vendas') or 'Vendas por Produto', 'icon': icons.get('vendas') or '💶', 'url': url_for('eurokg.vendas_produto', loja=loja_filter)},
        {'id': 'pesagens', 'label': labels.get('pesagens') or 'Pesagens', 'icon': icons.get('pesagens') or '⚖️', 'url': url_for('eurokg.pesagens')},
    ]
    if is_gestor:
        tabs.append({'id': 'consumo', 'label': labels.get('consumo') or 'Consumo Teórico', 'icon': icons.get('consumo') or '🍦', 'url': url_for('eurokg.consumo_teorico', loja=loja_filter)})
    return tabs


@eurokg_bp.route('/')
@perm_required('acesso_eurokg')
def index():
    from db.tiles import get_tile_visibility, get_tile_labels, get_tile_icons, get_module_labels
    user = session.get('user', {})
    is_gestor = user.get('acesso_gestor', False)
    visibility = get_tile_visibility('eurokg')
    labels = get_tile_labels('eurokg')
    icons = get_tile_icons('eurokg')
    custom_mod = get_module_labels().get('eurokg')
    items = [
        {'icon': icons.get(m['id']) or m['icon'], 'label': labels.get(m['id']) or m['label'], 'description': m['description'], 'url': url_for(m['url_endpoint'])}
        for m in MENU_ITEMS
        if (not m.get('gestor_only') or is_gestor) and visibility.get(m['id'], True)
    ]
    return render_template('components/section_menu.html', items=items,
                           menu_title=f'📊 {custom_mod}' if custom_mod else '📊 Euro/kg')


@eurokg_bp.route('/dashboard')
@perm_required('acesso_eurokg')
def dashboard():
    user = session.get('user', {})
    is_gestor = user.get('acesso_gestor', False)

    loja_filter, loja_db, store_filters = _store_context(user)

    today = date.today()
    year_start = date(today.year, 1, 1)
    month_start = today.replace(day=1)
    week_start = today - timedelta(days=today.weekday())

    annual_data = calculate_kpi_annual(today.year, loja_db)

    total_vendas_ytd = 0
    total_consumo_ytd = 0
    for m in range(1, today.month + 1):
        kpi_m = annual_data[m]
        total_vendas_ytd += kpi_m['vendas']
        total_consumo_ytd += kpi_m['consumo']
    kpi_ytd = total_vendas_ytd / total_consumo_ytd if total_consumo_ytd > 0 else 0

    kpi_month_data = annual_data[today.month]
    kpi_month = kpi_month_data['kpi']

    kpi_week_df = calculate_kpi_by_day(loja_db, week_start, today)
    if not kpi_week_df.empty:
        vendas_week = kpi_week_df['vendas'].sum()
        consumo_week = kpi_week_df['consumo_kg'].sum()
        kpi_week = vendas_week / consumo_week if consumo_week > 0 else 0
    else:
        kpi_week = 0

    target_atual = get_target_by_month(today.month)

    meses_pt = {
        1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril',
        5: 'Maio', 6: 'Junho', 7: 'Julho', 8: 'Agosto',
        9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'
    }
    month_name = meses_pt[today.month]

    data_inicio_30 = today - timedelta(days=29)
    intended_stores = [store['name'] for store in store_filters]
    doseamento_30 = get_doseamento_period(
        data_inicio_30, today, loja_db, intended_stores
    )
    kpi_df = calculate_kpi_by_day(loja_db, data_inicio_30, today)
    operational_kg_30 = (
        _finite_float(pd.to_numeric(
            kpi_df['consumo_kg'], errors='coerce'
        ).sum(min_count=1))
        if not kpi_df.empty and 'consumo_kg' in kpi_df else None
    )
    operational_revenue_30 = (
        _finite_float(pd.to_numeric(
            kpi_df['vendas'], errors='coerce'
        ).sum(min_count=1))
        if not kpi_df.empty and 'vendas' in kpi_df else None
    )
    estimate_30 = _prepare_operational_estimate(
        doseamento_30, operational_kg_30, operational_revenue_30
    )
    estimate_30.update({
        'data_inicio': data_inicio_30.isoformat(),
        'data_fim': today.isoformat(),
    })

    chart_json = None
    daily_details = []
    if not kpi_df.empty:
        kpi_df['data_str'] = pd.to_datetime(kpi_df['data']).dt.strftime('%d/%m')

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=kpi_df['data_str'].tolist(), y=kpi_df['kpi'].tolist(),
            mode='lines+markers', name='KPI Real',
            line=dict(color='#667eea', width=3), marker=dict(size=8),
            hovertemplate='%{x}<br>KPI: %{y:.2f} €/kg<extra></extra>'
        ))
        fig.update_layout(
            xaxis_title="Data", yaxis_title="€/kg", hovermode='x unified',
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=20, r=20, t=40, b=20),
            xaxis=dict(type='category', tickangle=-45)
        )
        fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='rgba(128,128,128,0.2)')
        fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='rgba(128,128,128,0.2)')
        chart_json = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

        if loja_db == 'Bolhão':
            detail_entrada_label = 'Transferências (kg)'
        elif loja_db == 'Matosinhos':
            detail_entrada_label = 'Prod - Transf (kg)'
        else:
            detail_entrada_label = 'Produção (kg)'

        for _, row in kpi_df.iterrows():
            d = row['data']
            if hasattr(d, 'strftime'):
                d_str = d.strftime('%d/%m/%Y')
            else:
                d_str = pd.to_datetime(d).strftime('%d/%m/%Y')
            daily_details.append({
                'data': d_str,
                'vendas': round(row['vendas'], 2),
                'stock_ini': round(row['stock_ini_kg'], 3),
                'entrada': round(row['entrada_kg'], 3),
                'stock_final': round(row['stock_final_kg'], 3),
                'quebras': round(row['quebras_kg'], 3),
                'consumo': round(row['consumo_kg'], 3),
                'kpi': round(row['kpi'], 2),
            })
    else:
        detail_entrada_label = 'Produção (kg)'

    tabs = _build_tabs(loja_filter, is_gestor, 'dashboard')

    return render_template('eurokg/dashboard.html',
        active_tab='dashboard', tabs=tabs,
        is_gestor=is_gestor, loja_filter=loja_filter,
        kpi_ytd=kpi_ytd, kpi_month=kpi_month, kpi_week=kpi_week,
        month_name=month_name, year=today.year,
        chart_json=chart_json, daily_details=daily_details,
        detail_entrada_label=detail_entrada_label,
        doseamento=doseamento_30, estimate=estimate_30,
        store_filters=store_filters)


@eurokg_bp.route('/resumo')
@perm_required('acesso_eurokg')
def resumo_mensal():
    user = session.get('user', {})
    is_gestor = user.get('acesso_gestor', False)

    loja_filter, loja_db, store_filters = _store_context(user)

    current_year = date.today().year
    meses_nomes = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                   'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']

    if loja_db == 'Bolhão':
        entrada_label = 'Transferências (kg)'
    elif loja_db == 'Matosinhos':
        entrada_label = 'Prod - Transf (kg)'
    else:
        entrada_label = 'Produção (kg)'

    annual_resumo = calculate_kpi_annual(current_year, loja_db)
    resumo_rows = []
    total_consumo = 0
    total_vendas = 0
    total_entrada = 0
    total_quebras = 0

    for m in range(1, 13):
        kpi_data = annual_resumo[m]
        consumo_kg = kpi_data['consumo']
        vendas_eur = kpi_data['vendas']
        euro_kg = kpi_data['kpi'] if consumo_kg > 0 else None
        target = get_target_by_month(m)
        entrada_val = kpi_data.get('entrada', kpi_data['producao'])

        total_consumo += consumo_kg
        total_vendas += vendas_eur
        total_entrada += entrada_val
        total_quebras += kpi_data['quebras']

        resumo_row = {
            'mes': meses_nomes[m - 1],
            'stock_ini': round(kpi_data['stock_ini'], 2) if kpi_data['stock_ini'] > 0 else 0,
            'entrada': round(entrada_val, 2) if entrada_val > 0 else 0,
            'stock_final': round(kpi_data['stock_final'], 2) if kpi_data['stock_final'] > 0 else 0,
            'quebras': round(kpi_data['quebras'], 2) if kpi_data['quebras'] > 0 else 0,
            'consumo': round(consumo_kg, 2),
            'vendas': round(vendas_eur, 2),
            'euro_kg': round(euro_kg, 2),
        }
        if m <= date.today().month:
            import calendar
            period_start = date(current_year, m, 1)
            period_end = min(
                date(
                    current_year, m,
                    calendar.monthrange(current_year, m)[1],
                ),
                date.today(),
            )
            dose = get_doseamento_period(
                period_start, period_end, loja_db,
                [store['name'] for store in store_filters],
            )
            estimate = _prepare_operational_estimate(
                dose, consumo_kg, vendas_eur
            )
            resumo_row.update({
                'data_inicio': period_start.isoformat(),
                'data_fim': period_end.isoformat(),
                'theoretical_kg': (
                    round(estimate['theoretical_kg'], 2)
                    if estimate['theoretical_kg'] is not None else None
                ),
                'variance_kg': (
                    round(estimate['variance_kg'], 2)
                    if estimate['variance_kg'] is not None else None
                ),
                'yield_pct': (
                    round(estimate['yield_pct'], 1)
                    if estimate['yield_pct'] is not None else None
                ),
                'status': estimate['theoretical_status'],
                'audit_status_label': estimate['audit_status_label'],
                'audit_note': estimate['audit_note'],
                'missing_products': estimate['missing_products'],
                'missing_sales_eur': round(
                    estimate['missing_sales_eur'], 2
                ),
            })
        resumo_rows.append(resumo_row)

    total_euro_kg = (
        total_vendas / total_consumo if total_consumo > 0 else None
    )

    tabs = _build_tabs(loja_filter, is_gestor, 'resumo')

    return render_template('eurokg/resumo_mensal.html',
        active_tab='resumo', tabs=tabs,
        is_gestor=is_gestor, loja_filter=loja_filter,
        current_year=current_year, entrada_label=entrada_label,
        resumo_rows=resumo_rows,
        total_entrada=round(total_entrada, 2),
        total_quebras=round(total_quebras, 2),
        total_consumo=round(total_consumo, 2),
        total_vendas=round(total_vendas, 2),
        total_euro_kg=(
            round(total_euro_kg, 2)
            if total_euro_kg is not None else None
        ),
        store_filters=store_filters)


@eurokg_bp.route('/consumo')
@perm_required('acesso_eurokg')
def consumo_teorico():
    user = session.get('user', {})
    is_gestor = user.get('acesso_gestor', False)

    loja_filter, loja_db, store_filters = _store_context(user)

    today = date.today()
    default_start = date(today.year, 1, 1)
    try:
        data_inicio = date.fromisoformat(
            request.args.get('data_inicio', default_start.isoformat())
        )
        data_fim = date.fromisoformat(
            request.args.get('data_fim', today.isoformat())
        )
        if data_inicio > data_fim:
            raise ValueError
    except (TypeError, ValueError):
        flash('Escolha um período de consulta válido.', 'warning')
        data_inicio, data_fim = default_start, today

    consumo_df = get_consumo_gelado_mensal(
        loja_db, data_inicio=data_inicio, data_fim=data_fim
    )

    consumo_data, meses_labels, totals_qty, totals_kg = (
        _build_consumo_teorico_view(consumo_df)
    )
    totals_incomplete = _consumo_incomplete_totals(consumo_df)

    dose_coverage, dose_import_audits = get_historical_dose_coverage(
        loja_db, data_inicio=data_inicio, data_fim=data_fim
    )
    actor = session.get("user", {}).get("username", "sistema")
    dose_import_preview = get_historical_dose_preview(
        session.get("dose_import_preview_id"), actor
    )
    dose_products, _unused_rules = (
        get_dose_product_configuration_queue() if is_gestor else ([], [])
    )
    vendas_peso_sem_peso = (
        get_vendas_ao_peso_sem_peso_calculavel(
            data_inicio=data_inicio, data_fim=data_fim, loja=loja_db
        )
        if is_gestor else []
    )
    dose_alias_alerts = (
        get_dose_alias_coverage_alerts(
            data_inicio=data_inicio, data_fim=data_fim, loja=loja_db
        )
        if is_gestor else []
    )

    tabs = _build_tabs(loja_filter, is_gestor, 'consumo')

    return render_template('eurokg/consumo_teorico.html',
        active_tab='consumo', tabs=tabs,
        is_gestor=is_gestor, loja_filter=loja_filter,
        consumo_data=consumo_data, meses_labels=meses_labels,
        totals_qty=totals_qty, totals_kg=totals_kg,
        totals_incomplete=totals_incomplete,
        store_filters=store_filters,
        data_inicio=data_inicio.isoformat(), data_fim=data_fim.isoformat(),
        dose_coverage=dose_coverage, dose_import_audits=dose_import_audits,
        dose_import_preview=dose_import_preview,
        dose_products=dose_products,
        vendas_peso_sem_peso=vendas_peso_sem_peso,
        dose_alias_alerts=dose_alias_alerts)


@eurokg_bp.route('/vendas-produto')
@perm_required('acesso_eurokg')
def vendas_produto():
    user = session.get('user', {})
    is_gestor = user.get('acesso_gestor', False)

    loja_filter = request.args.get('loja', 'Global Porto' if is_gestor else None)
    if loja_filter == 'Global Porto':
        loja_db = None
    elif loja_filter:
        loja_db = loja_filter
    else:
        loja_db = None

    today = date.today()
    year = request.args.get('year', today.year, type=int)
    available_years = list(range(today.year, today.year - 4, -1))

    table = get_vendas_produto_mensal(year, loja_db)

    tabs = _build_tabs(loja_filter, is_gestor, 'vendas')

    return render_template('eurokg/vendas_produto.html',
        active_tab='vendas', tabs=tabs,
        is_gestor=is_gestor, loja_filter=loja_filter,
        year=year, available_years=available_years,
        table=table,
        meses_abrev=MESES_PT_ABREV,
        current_month=today.month,
    )


@eurokg_bp.route('/consumo/configurar-produto', methods=['POST'])
@perm_required('acesso_gestor')
def configurar_produto_dose():
    loja_filter = request.form.get('loja_filter', 'Global Porto')
    try:
        changes, skipped_empty, invalid_rows = _parse_product_dose_batch(
            request.form
        )
        actor = session.get('user', {}).get('username', 'sistema')
        if changes:
            # Keep valid rows in one transaction so a real database failure
            # cannot leave a partially applied batch behind.
            with db_connection() as conn:
                try:
                    for product_id, grams, dose_type, effective_from in changes:
                        kwargs = {'source': 'Euro/kg', 'conn': conn}
                        if effective_from is not None:
                            kwargs['effective_from'] = effective_from
                        set_product_dose(
                            product_id, grams, dose_type, actor, **kwargs
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            flash(
                f'{len(changes)} configuração(ões) atualizada(s). '
                'A primeira configuração cobre as vendas já registadas; '
                'as alterações usam a data de entrada em vigor indicada.',
                'success',
            )
        elif not invalid_rows:
            flash(
                'Nenhuma configuração foi alterada. Os campos vazios '
                'mantêm-se sem alteração.',
                'info',
            )
        if skipped_empty:
            flash(
                f'{skipped_empty} linha(s) vazia(s) ficou/ficaram sem alteração.',
                'info',
            )
        if invalid_rows:
            flash(
                'Não foi possível guardar algumas linhas: '
                + ' '.join(invalid_rows),
                'warning',
            )
    except (TypeError, ValueError) as exc:
        flash(str(exc) or 'Indique um valor de gramas válido.', 'error')
    return redirect(url_for('eurokg.consumo_teorico', loja=loja_filter))


@eurokg_bp.route('/consumo/doses-historicas/preview', methods=['POST'])
@perm_required('acesso_gestor')
def preview_doses_historicas():
    upload = request.files.get("dose_csv")
    loja_filter = request.form.get("loja_filter", "Global Porto")
    if not upload or not upload.filename:
        flash("Selecione um ficheiro CSV.", "warning")
    else:
        try:
            actor = session.get("user", {}).get("username", "sistema")
            preview = create_historical_dose_preview(
                upload.read(), actor, upload.filename
            )
            session["dose_import_preview_id"] = preview["id"]
            session.modified = True
            flash(f"Pré-visualização pronta: {len(preview['rows'])} dose(s).", "success")
        except (UnicodeDecodeError, ValueError) as exc:
            session.pop("dose_import_preview_id", None)
            flash(str(exc), "error")
    return redirect(url_for('eurokg.consumo_teorico', loja=loja_filter))


@eurokg_bp.route('/consumo/doses-historicas/import', methods=['POST'])
@perm_required('acesso_gestor')
def confirmar_doses_historicas():
    loja_filter = request.form.get("loja_filter", "Global Porto")
    preview_id = request.form.get("preview_id")
    if not preview_id or preview_id != session.get("dose_import_preview_id"):
        flash("A pré-visualização expirou. Volte a selecionar o CSV.", "warning")
    else:
        try:
            actor = session.get("user", {}).get("username", "sistema")
            preview = get_historical_dose_preview(preview_id, actor)
            if not preview:
                raise ValueError("A pré-visualização expirou ou já foi utilizada.")
            confirm_historical_dose_preview(
                preview_id, actor,
            )
            session.pop("dose_import_preview_id", None)
            flash(f"{len(preview['rows'])} dose(s) históricas importadas.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
    return redirect(url_for('eurokg.consumo_teorico', loja=loja_filter))


@eurokg_bp.route('/pesagens')
@perm_required('acesso_eurokg')
def pesagens():
    import calendar
    from collections import defaultdict
    from db.pastelaria import get_stock_gelado_df

    today = date.today()
    first_of_month = today.replace(day=1)
    last_of_month = today.replace(day=calendar.monthrange(today.year, today.month)[1])

    lojas_raw = request.args.getlist('loja')
    lojas = [l for l in lojas_raw if l in ('Bolhão', 'Matosinhos')]
    if not lojas:
        lojas = ['Bolhão', 'Matosinhos']

    de_str = request.args.get('de', str(first_of_month))
    ate_str = request.args.get('ate', str(last_of_month))

    try:
        de_date = date.fromisoformat(de_str)
    except (ValueError, TypeError):
        de_date = first_of_month
    try:
        ate_date = date.fromisoformat(ate_str)
    except (ValueError, TypeError):
        ate_date = last_of_month

    if ate_date < de_date:
        ate_date = de_date

    single_day = (de_date == ate_date)

    all_rows = []
    for loja in lojas:
        rows = get_stock_gelado_df(loja=loja, data_inicio=de_date, data_fim=ate_date)
        all_rows.extend(rows)
    all_rows.sort(key=lambda r: (str(r['data']), r['loja'], r.get('sabor') or ''))

    TIPO_LABELS = {'inicio': 'Início', 'fim': 'Fim'}

    if single_day:
        by_loja = defaultdict(list)
        total_by_loja = defaultdict(float)
        grand_total = 0.0
        for row in all_rows:
            entry = {
                'sabor': row.get('sabor') or '—',
                'tipo': TIPO_LABELS.get(row.get('tipo', ''), row.get('tipo', '')),
                'quantidade_kg': round(float(row.get('quantidade_kg') or 0), 3),
            }
            by_loja[row['loja']].append(entry)
            total_by_loja[row['loja']] += entry['quantidade_kg']
            grand_total += entry['quantidade_kg']
        detail_data = {
            'by_loja': dict(by_loja),
            'totals': {k: round(v, 3) for k, v in total_by_loja.items()},
            'grand_total': round(grand_total, 3),
        }
        summary_data = None
    else:
        by_date = defaultdict(lambda: defaultdict(float))
        for row in all_rows:
            d = str(row['data'])
            by_date[d][row['loja']] += float(row.get('quantidade_kg') or 0)

        # Enumerate every calendar day in the range (zero-fill missing days)
        summary_rows = []
        total_by_loja = defaultdict(float)
        grand_total = 0.0
        current = de_date
        while current <= ate_date:
            d = str(current)
            row_totals = by_date.get(d, {})
            row_entry = {'data': d}
            row_sum = 0.0
            for loja in lojas:
                kg = round(row_totals.get(loja, 0.0), 3)
                row_entry[loja] = kg
                total_by_loja[loja] += kg
                row_sum += kg
            row_entry['total'] = round(row_sum, 3)
            grand_total += row_sum
            summary_rows.append(row_entry)
            current += timedelta(days=1)

        summary_data = {
            'rows': summary_rows,
            'totals': {k: round(v, 3) for k, v in total_by_loja.items()},
            'grand_total': round(grand_total, 3),
        }
        detail_data = None

    has_data = len(all_rows) > 0

    return render_template('eurokg/pesagens.html',
        lojas=lojas,
        de_date=str(de_date),
        ate_date=str(ate_date),
        single_day=single_day,
        has_data=has_data,
        detail_data=detail_data,
        summary_data=summary_data,
    )


@eurokg_bp.route('/diagnostico-vendas')
@perm_required('acesso_gestor')
def diagnostico_vendas():
    from db.pastelaria import get_vendas_diarias_diagnostico

    today = date.today()
    first_of_year = date(today.year, 1, 1)

    de_str = request.args.get('de', str(first_of_year))
    ate_str = request.args.get('ate', str(today))

    try:
        de_date = date.fromisoformat(de_str)
    except (ValueError, TypeError):
        de_date = first_of_year
    try:
        ate_date = date.fromisoformat(ate_str)
    except (ValueError, TypeError):
        ate_date = today

    if ate_date < de_date:
        ate_date = de_date

    all_lojas = ['Bolhão', 'Matosinhos']
    lojas_param = request.args.getlist('loja')
    lojas = [l for l in lojas_param if l in all_lojas] or all_lojas

    raw_rows = get_vendas_diarias_diagnostico(de_date, ate_date)
    # Keep only rows for selected lojas (avoids KeyError when user deselects a loja)
    raw_rows = [r for r in raw_rows if r['loja'] in lojas]

    # Index by (data, loja)
    by_day_loja = {}
    for r in raw_rows:
        key = (str(r['data']), r['loja'])
        by_day_loja[key] = r

    # Compute per-loja average (excluding zero days)
    loja_values = {l: [] for l in lojas}
    for r in raw_rows:
        if r['total_euros'] > 0:
            loja_values[r['loja']].append(r['total_euros'])
    loja_avg = {}
    for loja, vals in loja_values.items():
        loja_avg[loja] = sum(vals) / len(vals) if vals else 0

    # Build day-by-day table
    THRESHOLD_RATIO = 0.35  # flag if < 35% of loja average
    table_rows = []
    current = de_date
    while current <= ate_date:
        d = str(current)
        row_data = {'data': d, 'lojas': {}}
        any_loja_has_data = False
        any_alert = False

        for loja in lojas:
            entry = by_day_loja.get((d, loja))
            if entry:
                total = entry['total_euros']
                n = entry['n_produtos']
                avg = loja_avg.get(loja, 0)
                if total == 0:
                    status = 'zero'
                elif avg > 0 and total < avg * THRESHOLD_RATIO:
                    status = 'baixo'
                else:
                    status = 'ok'
                any_loja_has_data = True
                if status in ('zero', 'baixo'):
                    any_alert = True
            else:
                total = None
                n = 0
                status = 'ausente'
                # Only flag as alert if at least one other loja has data that day
                # (some stores may genuinely be closed)
                if any(by_day_loja.get((d, l2)) for l2 in lojas if l2 != loja):
                    any_alert = True
            row_data['lojas'][loja] = {'total': total, 'n': n, 'status': status}

        row_data['any_alert'] = any_alert
        row_data['any_loja_has_data'] = any_loja_has_data
        table_rows.append(row_data)
        current += timedelta(days=1)

    n_alerts = sum(1 for r in table_rows if r['any_alert'])
    n_ausente = sum(
        1 for r in table_rows
        for loja in lojas
        if r['lojas'][loja]['status'] == 'ausente'
           and r['any_loja_has_data']
    )

    return render_template('eurokg/diagnostico_vendas.html',
        de_date=str(de_date),
        ate_date=str(ate_date),
        lojas=lojas,
        table_rows=table_rows,
        loja_avg=loja_avg,
        n_alerts=n_alerts,
        n_ausente=n_ausente,
    )


@eurokg_bp.route('/volume-produtos')
@perm_required('acesso_gestor')
def volume_produtos():
    today = date.today()
    first_of_month = today.replace(day=1)
    de_str = request.args.get('de', str(first_of_month))
    ate_str = request.args.get('ate', str(today))
    loja = request.args.get('loja', '')
    try:
        de_date = date.fromisoformat(de_str)
    except ValueError:
        de_date = first_of_month
    try:
        ate_date = date.fromisoformat(ate_str)
    except ValueError:
        ate_date = today

    loja_db = loja if loja else None
    rows = get_volume_por_produto(de_date, ate_date, loja_db)

    total_euros = sum(float(r['valor_euros']) for r in rows)
    total_unidades = sum(int(r['unidades']) for r in rows)
    total_caixa_kg = sum(
        float(r['kg_estimado']) for r in rows
        if r['caixa_loja'] and r['kg_estimado'] is not None
    )
    missing_price_products = [
        r['produto'] for r in rows
        if r['caixa_loja'] and r['kg_estimado'] is None
    ]
    return render_template('eurokg/volume_produtos.html',
        de_date=str(de_date),
        ate_date=str(ate_date),
        loja=loja,
        rows=rows,
        total_euros=total_euros,
        total_unidades=total_unidades,
        total_caixa_kg=total_caixa_kg,
        missing_price_products=missing_price_products,
    )


@eurokg_bp.route('/config-preco-caixa', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def config_preco_caixa():
    username = session.get('user', {}).get('username', 'system')
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'add':
            data_inicio_str = request.form.get('data_inicio', '').strip()
            preco_kg_str = request.form.get('preco_kg', '').strip()
            if not data_inicio_str or not preco_kg_str:
                flash('Preencha a data de início e o preço €/kg.', 'danger')
            else:
                try:
                    data_inicio = date.fromisoformat(data_inicio_str)
                    preco_kg = float(preco_kg_str.replace(',', '.'))
                    if preco_kg <= 0:
                        raise ValueError('Preço deve ser positivo')
                    add_preco_caixa_kg(data_inicio, preco_kg)
                    flash(f'Período adicionado: {data_inicio_str} → {preco_kg:.2f} €/kg.', 'success')
                except ValueError as e:
                    flash(f'Dados inválidos: {e}', 'danger')
        elif action == 'delete':
            record_id = request.form.get('id', '')
            if record_id:
                deleted = delete_preco_caixa_kg(int(record_id))
                if deleted:
                    flash('Período removido.', 'success')
                else:
                    flash('Não é possível apagar o registo base (mais antigo).', 'warning')
        return redirect(url_for('eurokg.config_preco_caixa'))

    historico = get_precos_caixa_kg_historico()
    min_id = min((r['id'] for r in historico), default=None)
    return render_template('eurokg/config_preco_caixa.html',
        historico=historico,
        min_id=min_id,
    )
