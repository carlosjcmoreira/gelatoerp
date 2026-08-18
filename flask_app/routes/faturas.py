import os
import sys
import json
import logging
from datetime import date, datetime
from io import BytesIO

import psycopg2

from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, session, send_file, jsonify)
from flask_app.auth import perm_required, any_perm_required

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_invoices, get_invoice, get_invoice_pdf, create_invoice, update_invoice,
    delete_invoice, confirm_invoice_payment, mark_payment_executed,
    get_suppliers, get_supplier_by_id, get_supplier_by_nif, get_supplier_by_name,
    upsert_supplier, delete_supplier, merge_supplier,
    link_invoices_to_supplier_by_name, bulk_link_invoices_by_name,
    get_unlinked_supplier_names, get_suppliers_with_invoice_count,
    backfill_supplier_ids, normalise_supplier_names, rename_supplier_name_variant,
    get_stores_list, update_invoice_onedrive, suggest_onedrive_subfolder,
    get_contas_por_fornecedor, get_distinct_supplier_names,
    ONEDRIVE_SUBFOLDERS, INVOICE_CATEGORIES,
    DOCUMENT_TYPE_LABELS, PAYMENT_METHOD_LABELS, PAYMENT_TERMS_LABELS,
    calculate_due_date,
    get_confirming_contracts, create_confirming_parcela,
    create_confirming_parcelas_batch,
    get_payment_methods_config,
    get_invoice_linhas, upsert_invoice_linha, delete_invoice_linha,
    registar_entradas_stock_fatura, derive_local_from_store,
    list_materiais, LOCAIS_STOCK, UNIDADES_MATERIAIS,
    get_cost_centers, get_cost_categories_tree,
    get_invoices_with_payments, get_vat_periods,
    propose_invoice_payment, suggest_payment_date,
    get_invoice_centros_custo, add_invoice_centro_custo, remove_invoice_centro_custo,
)

import flask_app.services.faturas as faturas_svc
from flask_app.services import ServiceError
from db.faturas import (get_duplicate_supplier_suggestions, ignore_supplier_pair,
                        get_all_supplier_aliases, delete_supplier_alias, add_supplier_alias,
                        get_invoice_status_labels_map, get_invoice_audit_log,
                        get_invoice_deletion_log,
                        get_saved_views, save_view, delete_saved_view,
                        patch_supplier)
from flask_app.utils.finance import (
    parse_date as _parse_date,
    parse_float as _parse_float,
    find_duplicate_invoice_ids as _find_duplicate_invoice_ids,
)

logger = logging.getLogger(__name__)

faturas_bp = Blueprint('faturas', __name__)

ALLOWED_EXTENSIONS = {'pdf', 'xlsx', 'xls'}
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'heic', 'heif', 'webp'}


def _ext(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


def _panel_redirect(invoice_id: int):
    back = request.form.get('_return_url', '').strip()
    if back:
        return redirect(back)
    return redirect(url_for('faturas.detail', invoice_id=invoice_id))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@faturas_bp.route('/dashboard')
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — financial dashboard
def dashboard():
    from db.faturas import get_invoices_type_totals, get_pending_installments
    today = date.today()
    scheduled_docs = [dict(r) for r in get_invoices_with_payments(status='scheduled')]
    total_scheduled = sum(float(i.get('amount_eur') or 0) for i in scheduled_docs)
    # Next VAT period
    vat_periods = list(get_vat_periods(limit=4))
    next_vat = next(
        (p for p in vat_periods if not p.get('paid')),
        vat_periods[0] if vat_periods else None,
    )
    totals_by_type = get_invoices_type_totals()
    # Status counts & financial KPIs
    all_inv = get_invoices()
    overdue_count = sum(1 for i in all_inv if i['status'] in ('pending_review', 'scheduled') and i.get('due_date') and i['due_date'] < today)
    pending_count = sum(1 for i in all_inv if i['status'] == 'pending_review')
    scheduled_count = len(scheduled_docs)
    paid_count = sum(1 for i in all_inv if i['status'] == 'paid')
    # Total em aberto = pending_review + scheduled amounts
    total_em_aberto = sum(
        float(i.get('amount_eur') or 0) for i in all_inv
        if i['status'] in ('pending_review', 'scheduled')
    )
    # Saldo NCs pendentes (not paid)
    saldo_nc = totals_by_type.get('nota_credito', {}).get('total', 0.0)
    # Próximas 4 semanas de vencimentos
    from datetime import timedelta
    in_4w = today + timedelta(weeks=4)
    proximos_4_semanas = sorted(
        [i for i in all_inv if i['status'] == 'scheduled' and i.get('due_date') and i['due_date'] <= in_4w],
        key=lambda x: x['due_date']
    )
    pending_installments = get_pending_installments()
    return render_template(
        'financeiro/faturas/dashboard.html',
        today=today,
        scheduled_docs=scheduled_docs,
        total_scheduled=total_scheduled,
        next_vat=next_vat,
        totals_by_type=totals_by_type,
        document_type_labels=DOCUMENT_TYPE_LABELS,
        overdue_count=overdue_count,
        pending_count=pending_count,
        scheduled_count=scheduled_count,
        paid_count=paid_count,
        pending_installments=pending_installments,
        total_em_aberto=total_em_aberto,
        saldo_nc=saldo_nc,
        proximos_4_semanas=proximos_4_semanas,
        in_4w=in_4w,
    )


# ── Index ──────────────────────────────────────────────────────────────────────

@faturas_bp.route('/')
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — invoice listing
def index():
    from urllib.parse import urlencode
    from db.centros_custo import get_cost_categories as _gccat
    view = request.args.get('view', 'documento')
    today = date.today()

    confirming_contracts = get_confirming_contracts()
    payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]

    # Alert for permanently failed OneDrive uploads
    try:
        from db.faturas import count_onedrive_failed as _count_od_failed
        _od_failed_count = _count_od_failed()
        if _od_failed_count > 0:
            flash(
                f'⚠️ {_od_failed_count} fatura(s) com falha permanente no arquivo OneDrive. '
                'Verifique as faturas marcadas com ❌ e contacte o administrador se necessário.',
                'warning',
            )
    except Exception:
        pass

    if view == 'fornecedor':
        forn_status = request.args.get('forn_status', '').strip()
        _valid_forn_statuses = ('pending_review', 'scheduled', 'paid', 'cancelled', 'overdue', 'all', '')
        if forn_status not in _valid_forn_statuses:
            forn_status = ''

        # Default (empty) = show only active/pending invoices; 'all' = no filter
        # exclude_gov=True: government/tax entities (AT, SS) are not commercial suppliers;
        # exclude them from the grouped supplier view in both Financeiro and Compras contexts.
        _default_active = (forn_status == '')
        if _default_active:
            grupos = get_contas_por_fornecedor(status_filters=['pending_review', 'scheduled'], exclude_gov=True)
        elif forn_status == 'all':
            grupos = get_contas_por_fornecedor(status_filter=None, exclude_gov=True)
        else:
            grupos = get_contas_por_fornecedor(status_filter=forn_status, exclude_gov=True)

        # Fetch paid counts per supplier for badge (only when not already showing paid)
        forn_paid_counts = {}
        if _default_active and grupos:
            from db.faturas import get_paid_counts_by_supplier
            supplier_names = [g['supplier_name'] for g in grupos if g['supplier_name']]
            forn_paid_counts = get_paid_counts_by_supplier(supplier_names)

        for grupo in grupos:
            for inv in grupo.get('invoices', []):
                if inv['status'] in ('pending_review', 'scheduled') and inv.get('due_date') and inv['due_date'] < today:
                    inv['display_status'] = 'overdue'
                    inv['status_label'] = get_invoice_status_labels_map().get('overdue', 'Vencida')
                else:
                    inv['display_status'] = inv['status']
        return render_template(
            'financeiro/faturas/index.html',
            view='fornecedor',
            grupos=grupos,
            grupos_cc=[],
            grupos_cat=[],
            today=today,
            invoices=[],
            confirming_contracts=confirming_contracts,
            payment_methods=payment_methods,
            cost_centers=get_cost_centers(ativo_only=True),
            cost_categories_tree=[],
            cost_categories=_gccat(ativo_only=True),
            stores=get_stores_list(),
            centro_custo_filter=None,
            categoria_custo_filter=None,
            document_type_labels=DOCUMENT_TYPE_LABELS,
            forn_status=forn_status,
            forn_paid_counts=forn_paid_counts,
        )

    if view == 'centro_custo':
        # Group all non-draft invoices by centro_custo.
        # exclude_gov=True: AT/SS tax entities are not meaningful cost-centre entries.
        _valid_cc_statuses = ('pending_review', 'scheduled', 'paid', 'cancelled', 'overdue', 'all', '')
        cc_status = request.args.get('cc_status', '').strip()
        if cc_status not in _valid_cc_statuses:
            cc_status = ''
        all_invoices = get_invoices(exclude_gov=True)
        from collections import defaultdict
        grupos_cc = defaultdict(lambda: {'label': None, 'total': 0.0, 'count': 0, 'invoices': []})
        cc_map = {cc['id']: cc for cc in get_cost_centers(ativo_only=False)}
        for inv in all_invoices:
            is_overdue = inv['status'] in ('pending_review', 'scheduled') and inv.get('due_date') and inv['due_date'] < today
            if is_overdue:
                inv['display_status'] = 'overdue'
                inv['status_label'] = get_invoice_status_labels_map().get('overdue', 'Vencida')
            else:
                inv['display_status'] = inv['status']
            # Apply status filter
            if cc_status == '' and inv['status'] not in ('pending_review', 'scheduled'):
                continue
            elif cc_status == 'overdue' and not is_overdue:
                continue
            elif cc_status == 'pending_review' and inv['status'] != 'pending_review':
                continue
            elif cc_status == 'scheduled' and inv['status'] != 'scheduled':
                continue
            elif cc_status == 'paid' and inv['status'] != 'paid':
                continue
            elif cc_status == 'cancelled' and inv['status'] != 'cancelled':
                continue
            cc_id = inv.get('centro_custo_id')
            key = cc_id or 'sem_centro'
            if grupos_cc[key]['label'] is None:
                if cc_id and cc_id in cc_map:
                    cc = cc_map[cc_id]
                    grupos_cc[key]['label'] = f"{cc['code']} — {cc['name']}"
                else:
                    grupos_cc[key]['label'] = 'Sem centro de custo'
            grupos_cc[key]['invoices'].append(inv)
            grupos_cc[key]['total'] += float(inv.get('amount_eur') or 0)
            grupos_cc[key]['count'] += 1
        # Sort: sem_centro last, others alphabetically
        sorted_grupos = sorted(
            [{'key': k, **v} for k, v in grupos_cc.items()],
            key=lambda g: ('z' if g['key'] == 'sem_centro' else g['label'].lower())
        )
        _sem_cc_grp = next((g for g in sorted_grupos if g['key'] == 'sem_centro'), None)
        sem_cc_count = _sem_cc_grp['count'] if _sem_cc_grp else 0
        sem_cc_total = _sem_cc_grp['total'] if _sem_cc_grp else 0.0
        return render_template(
            'financeiro/faturas/index.html',
            view='centro_custo',
            grupos_cc=sorted_grupos,
            sem_cc_count=sem_cc_count,
            sem_cc_total=sem_cc_total,
            grupos_cat=[],
            invoices=[],
            today=today,
            confirming_contracts=confirming_contracts,
            payment_methods=payment_methods,
            cost_centers=get_cost_centers(ativo_only=True),
            cost_categories_tree=[],
            cost_categories=_gccat(ativo_only=True),
            stores=get_stores_list(),
            centro_custo_filter=None,
            categoria_custo_filter=None,
            document_type_labels=DOCUMENT_TYPE_LABELS,
            cc_status=cc_status,
        )

    if view == 'categoria_custo':
        from collections import defaultdict
        from db.centros_custo import get_cost_categories
        sem_categoria_only = request.args.get('sem_categoria') == '1'
        _valid_cat_statuses = ('pending_review', 'scheduled', 'paid', 'cancelled', 'overdue', 'all', '')
        cat_status = request.args.get('cat_status', '').strip()
        if cat_status not in _valid_cat_statuses:
            cat_status = ''
        # exclude_gov=True: AT/SS tax entities are not meaningful cost-category entries.
        all_invoices = get_invoices(exclude_gov=True)
        cat_map = {c['id']: c for c in get_cost_categories(ativo_only=False)}
        grupos_cat = defaultdict(lambda: {'label': None, 'total': 0.0, 'count': 0, 'invoices': []})
        for inv in all_invoices:
            cat_id = inv.get('categoria_custo_id')
            if sem_categoria_only and cat_id:
                continue
            is_overdue = inv['status'] in ('pending_review', 'scheduled') and inv.get('due_date') and inv['due_date'] < today
            if is_overdue:
                inv['display_status'] = 'overdue'
                inv['status_label'] = get_invoice_status_labels_map().get('overdue', 'Vencida')
            else:
                inv['display_status'] = inv['status']
            # Apply status filter
            if cat_status == '' and inv['status'] not in ('pending_review', 'scheduled'):
                continue
            elif cat_status == 'overdue' and not is_overdue:
                continue
            elif cat_status == 'pending_review' and inv['status'] != 'pending_review':
                continue
            elif cat_status == 'scheduled' and inv['status'] != 'scheduled':
                continue
            elif cat_status == 'paid' and inv['status'] != 'paid':
                continue
            elif cat_status == 'cancelled' and inv['status'] != 'cancelled':
                continue
            key = cat_id or 'sem_categoria'
            if grupos_cat[key]['label'] is None:
                if cat_id and cat_id in cat_map:
                    cat = cat_map[cat_id]
                    grupos_cat[key]['label'] = cat['name']
                else:
                    grupos_cat[key]['label'] = 'Sem categoria'
            grupos_cat[key]['invoices'].append(inv)
            grupos_cat[key]['total'] += float(inv.get('amount_eur') or 0)
            grupos_cat[key]['count'] += 1
        sorted_grupos_cat = sorted(
            [{'key': k, **v} for k, v in grupos_cat.items()],
            key=lambda g: ('z' if g['key'] == 'sem_categoria' else g['label'].lower())
        )
        return render_template(
            'financeiro/faturas/index.html',
            view='categoria_custo',
            grupos_cc=[],
            grupos_cat=sorted_grupos_cat,
            invoices=[],
            today=today,
            confirming_contracts=confirming_contracts,
            payment_methods=payment_methods,
            cost_centers=get_cost_centers(ativo_only=True),
            cost_categories_tree=[],
            cost_categories=_gccat(ativo_only=True),
            stores=get_stores_list(),
            centro_custo_filter=None,
            categoria_custo_filter=None,
            sem_categoria_only=sem_categoria_only,
            document_type_labels=DOCUMENT_TYPE_LABELS,
            cat_status=cat_status,
        )

    import calendar as _calendar

    _MONTH_PT = ['', 'Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']

    statuses_filter = [s for s in request.args.getlist('status') if s]
    show_all = request.args.get('all', '') == '1'
    order_by = request.args.get('order_by', 'due_date')
    order_dir = request.args.get('order_dir', 'asc')
    search = request.args.get('q', '').strip()
    centro_custo_raw = request.args.get('centro_custo_id', '')
    categoria_custo_raw = request.args.get('categoria_custo_id', '')
    centro_custo_filter = int(centro_custo_raw) if centro_custo_raw else None
    categoria_custo_filter = int(categoria_custo_raw) if categoria_custo_raw else None
    document_type_filter = request.args.get('document_type', '').strip()
    if document_type_filter not in DOCUMENT_TYPE_LABELS:
        document_type_filter = ''
    supplier_name_filter = request.args.get('supplier_name', '').strip()
    supplier_id_raw = request.args.get('supplier_id', '').strip()
    supplier_id_filter = int(supplier_id_raw) if supplier_id_raw.isdigit() else None
    # Resolve supplier name from supplier_id when no explicit name filter is given
    supplier_id_name = ''
    if supplier_id_filter and not supplier_name_filter:
        try:
            _sup = get_supplier_by_id(supplier_id_filter)
            if _sup:
                supplier_id_name = _sup.get('name', '')
        except Exception:
            supplier_id_filter = None

    sem_cc_filter = request.args.get('sem_cc', '') == '1'

    # Date filters
    date_field = request.args.get('date_field', 'due_date').strip()
    if date_field not in ('issue_date', 'due_date', 'paid_date'):
        date_field = 'due_date'
    date_from_raw = request.args.get('date_from', '').strip()
    date_to_raw = request.args.get('date_to', '').strip()
    month_raw = request.args.get('month', '').strip()  # YYYY-MM shortcut

    # month shortcut → expand to date_from/date_to (only when no explicit range given)
    if month_raw and not date_from_raw and not date_to_raw:
        try:
            _my, _mm = int(month_raw[:4]), int(month_raw[5:7])
            _, _last = _calendar.monthrange(_my, _mm)
            date_from_raw = f'{_my:04d}-{_mm:02d}-01'
            date_to_raw = f'{_my:04d}-{_mm:02d}-{_last:02d}'
        except (ValueError, IndexError):
            month_raw = ''

    date_from = _parse_date(date_from_raw)
    date_to = _parse_date(date_to_raw)

    # Month picker options: 12 past months (inclusive current) + 3 future
    _month_options = []
    for _i in range(-11, 4):
        _total = today.month - 1 + _i
        _y = today.year + _total // 12
        _m = _total % 12 + 1
        _val = f'{_y:04d}-{_m:02d}'
        _month_options.append({'value': _val, 'label': f'{_MONTH_PT[_m]} {_y}'})

    # Default filter: active when no status/all param set — exclude paid and draft
    # When sem_cc=1 is active we broaden the default to all non-draft, non-cancelled
    # statuses so the count in the P&L badge matches exactly what the user sees here.
    default_filter_active = not statuses_filter and not show_all
    if sem_cc_filter and not statuses_filter and not show_all:
        effective_statuses = ['pending_review', 'scheduled', 'paid']
    elif default_filter_active:
        effective_statuses = ['pending_review', 'scheduled', 'cancelled']
    elif show_all:
        effective_statuses = None
    else:
        effective_statuses = statuses_filter

    invoices = get_invoices(
        statuses=effective_statuses,
        no_status_filter=show_all,
        search=search or None,
        order_by=order_by,
        order_dir=order_dir,
        centro_custo_id=centro_custo_filter,
        categoria_custo_id=categoria_custo_filter,
        document_type=document_type_filter or None,
        supplier_name=supplier_name_filter or None,
        supplier_id=supplier_id_filter or None,
        date_from=date_from,
        date_to=date_to,
        date_field=date_field,
        sem_cc=sem_cc_filter or None,
    )

    # Category filter (in-memory — category is a free-text field on invoices)
    category_filter = request.args.get('category', '').strip()
    if category_filter:
        invoices = [i for i in invoices if i.get('category') and category_filter.lower() in i['category'].lower()]

    # Sem evidência filter (in-memory — has_pdf already computed by DB)
    sem_evidencia_filter = request.args.get('sem_evidencia', '') == '1'
    # Count before applying the filter; exclude draft/cancelled (same rule as dashboard alert)
    sem_evidencia_count = sum(
        1 for i in invoices
        if not i.get('has_pdf') and i.get('status') not in ('draft', 'cancelled')
    )
    if sem_evidencia_filter:
        invoices = [
            i for i in invoices
            if not i.get('has_pdf') and i.get('status') not in ('draft', 'cancelled')
        ]

    _labels_map = get_invoice_status_labels_map()
    for inv in invoices:
        if inv['status'] in ('pending_review', 'scheduled') and inv['due_date'] and inv['due_date'] < today:
            inv['display_status'] = 'overdue'
            inv['status_label'] = _labels_map.get('overdue', 'Vencida')
        else:
            inv['display_status'] = inv['status']

    # Duplicate detection: flag invoices where same supplier has same normalised number
    _dup_ids = _find_duplicate_invoice_ids(invoices)
    for inv in invoices:
        inv['has_duplicate'] = inv['id'] in _dup_ids

    # Distinct categories for filter panel
    all_categories = sorted({i['category'] for i in invoices if i.get('category')})

    # Build filter_qs preserving multi-select status for sort links
    _filter_params = []
    for s in (statuses_filter if not default_filter_active else []):
        _filter_params.append(('status', s))
    if show_all:
        _filter_params.append(('all', '1'))
    for k, v in [('q', search),
                 ('centro_custo_id', centro_custo_raw), ('categoria_custo_id', categoria_custo_raw),
                 ('document_type', document_type_filter), ('supplier_name', supplier_name_filter),
                 ('supplier_id', supplier_id_raw if supplier_id_filter else ''),
                 ('category', category_filter)]:
        if v:
            _filter_params.append((k, v))
    if sem_evidencia_filter:
        _filter_params.append(('sem_evidencia', '1'))
    if sem_cc_filter:
        _filter_params.append(('sem_cc', '1'))
    if date_from_raw:
        _filter_params.append(('date_from', date_from_raw))
    if date_to_raw:
        _filter_params.append(('date_to', date_to_raw))
    if month_raw:
        _filter_params.append(('month', month_raw))
    if date_field != 'due_date':
        _filter_params.append(('date_field', date_field))
    filter_qs = ('?' + urlencode(_filter_params)) if _filter_params else '?'

    # Build sem_evidencia toggle URLs from the fully-populated params list
    _params_no_ev = [p for p in _filter_params if p[0] != 'sem_evidencia']
    _params_with_ev = _params_no_ev + [('sem_evidencia', '1')]
    sem_ev_off_url = ('?' + urlencode(_params_no_ev)) if _params_no_ev else '?'
    sem_ev_on_url = '?' + urlencode(_params_with_ev)

    stores = get_stores_list()
    cost_centers = get_cost_centers(ativo_only=True)
    cost_categories_tree = get_cost_categories_tree()
    cost_categories = _gccat(ativo_only=True)
    all_supplier_names = get_distinct_supplier_names()

    # KPI dashboard — scheduled / next VAT
    scheduled_docs = [dict(r) for r in get_invoices_with_payments(status='scheduled')]
    total_scheduled = sum(float(i.get('amount_eur') or 0) for i in scheduled_docs)
    vat_periods = list(get_vat_periods(limit=4))
    next_vat = next(
        (p for p in reversed(vat_periods) if p['status'] in ('estimated', 'declared')),
        None
    )

    # Per-type totals — single GROUP BY query, no full-list fetch
    from db.faturas import get_invoices_type_totals, get_pending_installments
    totals_by_type = get_invoices_type_totals()

    # Pending installments (show above main table regardless of filters)
    try:
        pending_installments = get_pending_installments()
    except Exception:
        pending_installments = []

    # Base query string without document_type so badge links preserve other filters
    _type_badge_params = []
    for s in (statuses_filter if not default_filter_active else []):
        _type_badge_params.append(('status', s))
    if show_all:
        _type_badge_params.append(('all', '1'))
    for k, v in [('q', search),
                 ('centro_custo_id', centro_custo_raw), ('categoria_custo_id', categoria_custo_raw),
                 ('supplier_name', supplier_name_filter),
                 ('supplier_id', supplier_id_raw if supplier_id_filter else '')]:
        if v:
            _type_badge_params.append((k, v))
    if date_from_raw:
        _type_badge_params.append(('date_from', date_from_raw))
    if date_to_raw:
        _type_badge_params.append(('date_to', date_to_raw))
    if month_raw:
        _type_badge_params.append(('month', month_raw))
    if date_field != 'due_date':
        _type_badge_params.append(('date_field', date_field))
    type_badge_base_qs = urlencode(_type_badge_params)

    return render_template(
        'financeiro/faturas/index.html',
        view='documento',
        scope='financeiro',
        filter_action_url=url_for('faturas.index'),
        panel_base_url=url_for('faturas.invoice_panel', invoice_id=0),
        paid_date_url_tpl='/financeiro/faturas/SET_ID/set-paid-date',
        inv_api_prefix='/financeiro/faturas',
        invoices=invoices,
        statuses_filter=statuses_filter,
        show_all=show_all,
        default_filter_active=default_filter_active,
        order_by=order_by,
        order_dir=order_dir,
        search=search,
        stores=stores,
        today=today,
        filter_qs=filter_qs,
        confirming_contracts=confirming_contracts,
        payment_methods=payment_methods,
        cost_centers=cost_centers,
        cost_categories_tree=cost_categories_tree,
        cost_categories=cost_categories,
        centro_custo_filter=centro_custo_filter,
        categoria_custo_filter=categoria_custo_filter,
        document_type_filter=document_type_filter,
        document_type_labels=DOCUMENT_TYPE_LABELS,
        scheduled_docs=scheduled_docs,
        total_scheduled=total_scheduled,
        next_vat=next_vat,
        totals_by_type=totals_by_type,
        type_badge_base_qs=type_badge_base_qs,
        all_supplier_names=all_supplier_names,
        supplier_name_filter=supplier_name_filter,
        supplier_id_filter=supplier_id_filter,
        supplier_id_name=supplier_id_name,
        date_field=date_field,
        date_from_raw=date_from_raw,
        date_to_raw=date_to_raw,
        month_raw=month_raw,
        month_options=_month_options,
        pending_installments=pending_installments,
        category_filter=category_filter,
        all_categories=all_categories,
        sem_evidencia_filter=sem_evidencia_filter,
        sem_evidencia_count=sem_evidencia_count,
        sem_ev_on_url=sem_ev_on_url,
        sem_ev_off_url=sem_ev_off_url,
        saved_views=_get_user_saved_views(),
    )


def _get_user_saved_views() -> list:
    """Return saved views for the current session user. Safe — returns [] on any error."""
    try:
        user_id = session.get('user', {}).get('username', '')
        return get_saved_views(user_id) if user_id else []
    except Exception:
        return []


@faturas_bp.route('/views', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — save named filter view
def save_view_endpoint():
    """POST /financeiro/faturas/views — persist a named filter view."""
    user_id = session.get('user', {}).get('username', '')
    if not user_id:
        return jsonify({'ok': False, 'error': 'Não autenticado'}), 401
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400
    filters = data.get('filters') or {}
    try:
        view_id = save_view(user_id, name, filters)
        return jsonify({'ok': True, 'id': view_id, 'name': name})
    except Exception as exc:
        logger.error('save_view_endpoint failed: %s', exc)
        return jsonify({'ok': False, 'error': 'Erro ao guardar vista'}), 500


@faturas_bp.route('/views/<int:view_id>', methods=['DELETE'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — delete own saved view
def delete_view_endpoint(view_id):
    """DELETE /financeiro/faturas/views/<id> — remove a saved view owned by current user."""
    user_id = session.get('user', {}).get('username', '')
    if not user_id:
        return jsonify({'ok': False, 'error': 'Não autenticado'}), 401
    deleted = delete_saved_view(view_id, user_id)
    return jsonify({'ok': deleted})


def _generate_bulk_confirming_parcelas(amount, start_date, n, freq='none'):
    """Generate N equal-split confirming parcelas from start_date.
    freq='monthly' adds 1 month per parcela; otherwise all on start_date.
    """
    from dateutil.relativedelta import relativedelta
    n = max(1, int(n or 1))
    unit = round(float(amount or 0) / n, 2)
    remainder = round(float(amount or 0) - unit * n, 2)
    parcelas = []
    for i in range(n):
        if freq == 'monthly':
            try:
                pdate = start_date + relativedelta(months=i)
            except Exception:
                pdate = start_date
        else:
            pdate = start_date
        montante = unit + (remainder if i == n - 1 else 0)
        if montante > 0:
            parcelas.append({'montante': montante, 'data_pagamento': pdate})
    return parcelas


def _parse_confirming_parcelas():
    """Parse multi-parcela form data. Returns list of {montante, data_pagamento} or empty list."""
    montantes = request.form.getlist('parcela_montante[]')
    datas = request.form.getlist('parcela_data[]')
    parcelas = []
    for m, d in zip(montantes, datas):
        try:
            montante = float(m.replace(',', '.'))
        except (ValueError, AttributeError):
            continue
        data = _parse_date(d)
        if montante > 0 and data:
            parcelas.append({'montante': montante, 'data_pagamento': data})
    return parcelas


def _safe_return_url(raw: str) -> str:
    """Accept only relative same-origin URLs to prevent open redirects."""
    from urllib.parse import urlparse
    if raw:
        parsed = urlparse(raw)
        if not parsed.scheme and not parsed.netloc:
            return raw
    return url_for('faturas.index')


_BULK_ALLOWED_STATUSES = {'pending_review', 'scheduled', 'paid', 'cancelled'}
_BULK_DATE_REQUIRED = {'scheduled', 'paid'}
_NON_BULK_STATUSES = {'draft', 'overdue'}


def _get_bulk_allowed_statuses() -> set:
    """Return the set of statuses allowed in bulk status changes.

    Loads from DB (cached) and excludes non-actionable statuses; falls back to
    the hardcoded constant when the DB table is not yet available.
    """
    try:
        from db.faturas import get_invoice_status_configs
        rows = get_invoice_status_configs()
        if rows:
            return {r['key'] for r in rows if r['active'] and r['key'] not in _NON_BULK_STATUSES}
    except Exception:
        pass
    return _BULK_ALLOWED_STATUSES


@faturas_bp.route('/bulk', methods=['POST'])
@perm_required('acesso_gestor')  # intentionally restricted — bulk ops include status changes and deletion
def bulk_action():
    ids = request.form.getlist('ids', type=int)
    action = request.form.get('action', '')
    new_status = request.form.get('status', '')
    return_url = _safe_return_url(request.form.get('return_url', ''))

    if not ids:
        flash('Nenhuma fatura selecionada.', 'warning')
        return redirect(return_url)

    if action == 'change_status' and new_status:
        if new_status not in _get_bulk_allowed_statuses():
            flash('Estado inválido.', 'warning')
            return redirect(return_url)

        # Validate and parse bulk_date for states that require it
        bulk_date = _parse_date(request.form.get('bulk_date', ''))
        if new_status in _BULK_DATE_REQUIRED and not bulk_date:
            label = get_invoice_status_labels_map().get(new_status, new_status)
            flash(f'É obrigatório indicar a data ao alterar para «{label}».', 'warning')
            return redirect(return_url)

        # Guard: warn before re-paying already-paid invoices
        if new_status == 'paid' and request.form.get('force_duplicate', '0') != '1':
            from db.pagamentos import get_already_paid_invoices
            already_paid = get_already_paid_invoices(ids)
            if already_paid:
                refs = ', '.join(
                    f"{r['invoice_number'] or '(sem nº)'} — {r['supplier_name']}"
                    for r in already_paid
                )
                flash(
                    f'⚠️ {len(already_paid)} fatura(s) já estão pagas: {refs}. '
                    'Marque "Confirmar mesmo assim" no modal para processar na mesma.',
                    'warning',
                )
                return redirect(return_url)

        # Payment method (optional, for scheduled/paid)
        bulk_payment_method = request.form.get('bulk_payment_method', '').strip() or None
        bulk_confirming_id_raw = request.form.get('bulk_confirming_contract_id', '').strip()
        bulk_confirming_id = int(bulk_confirming_id_raw) if bulk_confirming_id_raw.isdigit() else None
        bulk_nparcelas_raw = request.form.get('bulk_nparcelas', '1')
        bulk_nparcelas = int(bulk_nparcelas_raw) if bulk_nparcelas_raw.isdigit() else 1
        bulk_freq = request.form.get('bulk_freq', 'none').strip()

        label = get_invoice_status_labels_map().get(new_status, new_status)
        current_user = session.get('user', {}).get('username', 'system')
        ok = 0
        fail = 0
        for inv_id in ids:
            try:
                if new_status == 'paid':
                    inv_data = get_invoice(inv_id)
                    mark_payment_executed(inv_id, bulk_date, current_user,
                                         payment_method=bulk_payment_method,
                                         confirming_contract_id=bulk_confirming_id)
                    if bulk_payment_method == 'confirming' and bulk_confirming_id and inv_data:
                        try:
                            amt = inv_data.get('amount_eur')
                            parcelas = _generate_bulk_confirming_parcelas(amt, bulk_date, bulk_nparcelas, bulk_freq)
                            create_confirming_parcelas_batch(inv_id, bulk_confirming_id, parcelas, estado='paid')
                        except Exception as pe:
                            logger.warning('bulk confirming paid inv=%s: %s', inv_id, pe)
                elif new_status == 'scheduled':
                    inv_data = get_invoice(inv_id)
                    amt = inv_data.get('amount_eur') if inv_data else None
                    confirm_invoice_payment(inv_id, bulk_date, amt, current_user,
                                           payment_method=bulk_payment_method,
                                           confirming_contract_id=bulk_confirming_id)
                    if bulk_payment_method == 'confirming' and bulk_confirming_id and inv_data:
                        try:
                            parcelas = _generate_bulk_confirming_parcelas(amt, bulk_date, bulk_nparcelas, bulk_freq)
                            create_confirming_parcelas_batch(inv_id, bulk_confirming_id, parcelas, estado='scheduled')
                        except Exception as pe:
                            logger.warning('bulk confirming scheduled inv=%s: %s', inv_id, pe)
                else:
                    update_invoice(inv_id, {'status': new_status}, changed_by=current_user)
                ok += 1
            except Exception as e:
                logger.error('bulk change_status id=%s: %s', inv_id, e, exc_info=True)
                fail += 1
        msg = f'{ok} fatura(s) alterada(s) para «{label}».'
        if fail:
            msg += f' {fail} falharam (ver logs).'
        flash(msg, 'success' if not fail else 'warning')
    elif action == 'delete':
        ok = 0
        fail = 0
        _del_user = session.get('user', {}).get('username', 'sistema')
        for inv_id in ids:
            try:
                delete_invoice(inv_id, deleted_by=_del_user)
                ok += 1
            except Exception as e:
                logger.error('bulk delete id=%s: %s', inv_id, e)
                fail += 1
        msg = f'{ok} fatura(s) eliminada(s).'
        if fail:
            msg += f' {fail} falharam (ver logs).'
        flash(msg, 'success' if not fail else 'warning')
    elif action == 'assign_field':
        _ASSIGN_ALLOWED = {'categoria_custo_id', 'centro_custo_id'}
        field = request.form.get('field', '').strip()
        if field not in _ASSIGN_ALLOWED:
            flash('Campo inválido.', 'warning')
            return redirect(return_url)
        raw_value = request.form.get('value', '').strip()
        value = int(raw_value) if raw_value.isdigit() else None
        current_user = session.get('user', {}).get('username', 'sistema')
        ok = 0
        fail = 0
        for inv_id in ids:
            try:
                update_invoice(inv_id, {field: value}, changed_by=current_user)
                ok += 1
            except Exception as e:
                logger.error('bulk assign_field id=%s field=%s: %s', inv_id, field, e)
                fail += 1
        _field_labels = {
            'categoria_custo_id': 'Categoria',
            'centro_custo_id': 'Centro de Custo',
        }
        msg = f'{ok} fatura(s) actualizadas ({_field_labels.get(field, field)}).'
        if fail:
            msg += f' {fail} falharam (ver logs).'
        flash(msg, 'success' if not fail else 'warning')
    else:
        flash('Acção inválida.', 'warning')

    return redirect(return_url)


# ── Registar Documento (unified entry: manual + OCR channels) ──────────────────

@faturas_bp.route('/registar', methods=['GET', 'POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — register new invoice (manual entry)
def registar():
    """Unified document registration: manual entry, PDF upload, or photo channel."""

    def _render_with_errors(field_errors, form_data):
        """Re-render the registration form with pre-filled values and inline field errors."""
        return render_template(
            'financeiro/faturas/registar.html',
            suppliers=get_suppliers(),
            cost_centers=get_cost_centers(ativo_only=True),
            cost_categories_tree=get_cost_categories_tree(),
            payment_methods=[m for m in get_payment_methods_config() if m.get('ativo')],
            subfolders=ONEDRIVE_SUBFOLDERS,
            today=str(date.today()),
            document_type_labels=DOCUMENT_TYPE_LABELS,
            payment_method_labels=PAYMENT_METHOD_LABELS,
            payment_terms_labels=PAYMENT_TERMS_LABELS,
            form_data=form_data,
            field_errors=field_errors,
        ), 422

    if request.method == 'POST':
        supplier_id_str = request.form.get('supplier_id', '').strip()
        supplier_id = int(supplier_id_str) if supplier_id_str else None
        supplier_name = request.form.get('supplier_name', '').strip()
        supplier_nif = request.form.get('supplier_nif', '').strip() or None
        invoice_number = request.form.get('invoice_number', '').strip() or None
        amount_str = request.form.get('amount_eur', '').replace(',', '.')
        vat_str = request.form.get('vat_amount_eur', '').replace(',', '.') or '0'
        issue_date_str = request.form.get('issue_date', '')
        due_date_str = request.form.get('due_date', '')
        notes_raw = request.form.get('notes', '').strip() or ''
        payment_method = request.form.get('payment_method', '').strip() or None
        document_type = request.form.get('document_type', 'fatura')
        if document_type not in DOCUMENT_TYPE_LABELS:
            document_type = 'fatura'
        onedrive_subfolder = request.form.get('onedrive_subfolder', '').strip() or None
        centro_custo_raw = request.form.get('centro_custo_id', '').strip()
        centro_custo_id = int(centro_custo_raw) if centro_custo_raw else None
        categoria_custo_raw = request.form.get('categoria_custo_id', '').strip()
        categoria_custo_id = int(categoria_custo_raw) if categoria_custo_raw else None

        _INVOICE_DOC_TYPES = {'fatura', 'nota_credito', 'nota_debito'}
        if not supplier_name and document_type in _INVOICE_DOC_TYPES:
            return _render_with_errors(
                {'supplier': 'Nome do fornecedor é obrigatório para este tipo de documento.'},
                request.form,
            )
        try:
            amount_eur = float(amount_str)
        except (ValueError, TypeError):
            return _render_with_errors(
                {'amount_eur': 'Valor inválido — introduz um número (ex: 12.50).'},
                request.form,
            )

        try:
            vat_amount_eur = float(vat_str)
        except (ValueError, TypeError):
            vat_amount_eur = 0.0

        issue_date = _parse_date(issue_date_str)
        due_date = _parse_date(due_date_str)

        _ALLOWED_MANUAL_EXTS = {'pdf', 'jpg', 'jpeg', 'png', 'heic', 'heif', 'webp'}
        pdf_file = request.files.get('pdf_file')
        pdf_data = None
        pdf_filename = None
        if pdf_file and pdf_file.filename:
            ext = _ext(pdf_file.filename)
            if ext not in _ALLOWED_MANUAL_EXTS:
                return _render_with_errors(
                    {'pdf_file': f'Tipo de ficheiro não suportado (.{ext}). Usa PDF ou imagem.'},
                    request.form,
                )
            pdf_data = pdf_file.read()
            pdf_filename = pdf_file.filename

        notes_parts = []
        if payment_method:
            notes_parts.append(f'Método: {payment_method}')
        if notes_raw:
            notes_parts.append(notes_raw)
        notes = ' | '.join(notes_parts) or None

        # Auto-lookup supplier by name if not selected from dropdown
        if not supplier_id and supplier_name:
            try:
                _matched = get_supplier_by_name(supplier_name)
                if _matched:
                    supplier_id = _matched['id']
            except Exception:
                pass

        # Enforce: invoice-type documents must resolve to a known supplier
        _INVOICE_DOC_TYPES = {'fatura', 'nota_credito', 'nota_debito'}
        if document_type in _INVOICE_DOC_TYPES and not supplier_id:
            return _render_with_errors(
                {'supplier': 'Seleciona um fornecedor da lista ou regista-o primeiro (botão ＋).'},
                request.form,
            )

        current_user = session.get('user', {}).get('username', 'sistema')
        invoice_id = create_invoice({
            'supplier_id': supplier_id,
            'supplier_name': supplier_name,
            'supplier_nif': supplier_nif,
            'invoice_number': invoice_number,
            'amount_eur': amount_eur,
            'vat_amount_eur': vat_amount_eur,
            'issue_date': issue_date,
            'due_date': due_date,
            'category': None,
            'onedrive_subfolder': onedrive_subfolder,
            'onedrive_path': None,
            'onedrive_web_url': None,
            'pdf_filename': pdf_filename,
            'pdf_data': pdf_data,
            'status': 'pending_review',
            'ocr_confidence': None,
            'ocr_raw': None,
            'created_by': current_user,
            'notes': notes,
            'document_type': document_type,
            'centro_custo_id': centro_custo_id,
            'categoria_custo_id': categoria_custo_id,
        })

        if pdf_data and onedrive_subfolder:
            try:
                from flask_app.onedrive_archive import upload_invoice_pdf
                result = upload_invoice_pdf(pdf_data, pdf_filename or 'documento.pdf',
                                            onedrive_subfolder, issue_date)
                if result.get('onedrive_path'):
                    update_invoice_onedrive(invoice_id, result['onedrive_path'],
                                            onedrive_subfolder,
                                            onedrive_web_url=result.get('web_url'))
                elif result.get('warning'):
                    flash(f'Documento registado. Aviso OneDrive: {result["warning"]}', 'warning')
            except Exception as exc:
                logger.warning('OneDrive upload failed for manual doc %s: %s', invoice_id, exc)
                flash(f'Documento registado. Erro ao arquivar no OneDrive: {exc}', 'warning')

        _doc_label = DOCUMENT_TYPE_LABELS.get(document_type, 'Documento')
        _entity = f' de {supplier_name}' if supplier_name else ''
        if due_date:
            try:
                suggested_date, is_fallback, _ = suggest_payment_date(
                    invoice_id, amount_eur, due_date=due_date
                )
                propose_invoice_payment(invoice_id, suggested_date, amount_eur)
                if is_fallback:
                    flash(f'{_doc_label} registado. Data de pagamento sugerida = vencimento ({due_date.strftime("%d/%m/%Y")}) — sem semana com liquidez suficiente nas próximas 8 semanas.', 'warning')
                else:
                    flash(f'{_doc_label}{_entity} registado. Data de pagamento proposta: {suggested_date.strftime("%d/%m/%Y")}.', 'success')
            except Exception as _pay_exc:
                logger.warning('suggest/propose payment failed for invoice %s: %s', invoice_id, _pay_exc)
                flash(f'{_doc_label}{_entity} registado. Não foi possível calcular data de pagamento automaticamente — agenda manualmente na fatura.', 'warning')
        else:
            flash(f'{_doc_label}{_entity} registado com sucesso!', 'success')

        return redirect(url_for('faturas.index'))

    suppliers = get_suppliers()
    cost_centers = get_cost_centers(ativo_only=True)
    cost_categories_tree = get_cost_categories_tree()
    payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]
    return render_template(
        'financeiro/faturas/registar.html',
        suppliers=suppliers,
        cost_centers=cost_centers,
        cost_categories_tree=cost_categories_tree,
        payment_methods=payment_methods,
        subfolders=ONEDRIVE_SUBFOLDERS,
        today=str(date.today()),
        document_type_labels=DOCUMENT_TYPE_LABELS,
        payment_method_labels=PAYMENT_METHOD_LABELS,
        payment_terms_labels=PAYMENT_TERMS_LABELS,
    )


# ── Upload & OCR ───────────────────────────────────────────────────────────────

@faturas_bp.route('/upload', methods=['GET', 'POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — upload PDF / OCR invoice
def upload():
    stores = get_stores_list()
    subfolders = ONEDRIVE_SUBFOLDERS
    categories = INVOICE_CATEGORIES
    suppliers = get_suppliers()

    if request.method == 'POST':
        channel = request.form.get('channel', 'email_upload')
        return_to = request.form.get('return_to', '').strip()

        # Store return_to context in session so review/save/cancel can use it
        if return_to:
            session['faturas_return_to'] = return_to
        else:
            session.pop('faturas_return_to', None)

        if channel == 'photo':
            file = request.files.get('photo_file')
            if not file or not file.filename:
                flash('Selecciona uma fotografia.', 'warning')
                return redirect(url_for('faturas.upload'))
            ext = _ext(file.filename)
            if ext not in ALLOWED_IMAGE_EXTENSIONS:
                flash('Tipo de ficheiro não suportado. Usa uma imagem (JPG, PNG, HEIC, etc.).', 'warning')
                return redirect(url_for('faturas.upload'))
            file_bytes = file.read()
            filename = file.filename
            username = session.get('user', {}).get('username', '')
            try:
                invoice_id = faturas_svc.create_draft_from_image(file_bytes, filename, username, source='photo')
            except ServiceError as e:
                flash(str(e), 'warning')
                return redirect(url_for('faturas.upload'))
            return redirect(url_for('faturas.review', invoice_id=invoice_id))

        # Default: email_upload channel
        file = request.files.get('pdf_file')
        if not file or not file.filename:
            flash('Selecciona um ficheiro PDF.', 'warning')
            return redirect(url_for('faturas.upload'))

        ext = _ext(file.filename)
        if ext not in ALLOWED_EXTENSIONS:
            flash('Tipo de ficheiro não suportado. Usa PDF ou Excel.', 'warning')
            return redirect(url_for('faturas.upload'))

        # Excel import path
        if ext in ('xlsx', 'xls'):
            return _handle_excel_import(file, ext)

        # PDF path: run OCR, create draft
        pdf_bytes = file.read()
        pdf_filename = file.filename
        username = session.get('user', {}).get('username', '')
        try:
            invoice_id = faturas_svc.create_draft_from_pdf(pdf_bytes, pdf_filename, username, source='email_upload')
        except ServiceError as e:
            flash(str(e), 'warning')
            return redirect(url_for('faturas.upload'))
        return redirect(url_for('faturas.review', invoice_id=invoice_id))

    return render_template(
        'financeiro/faturas/upload.html',
        stores=stores,
    )


# ── Review draft (GET) ─────────────────────────────────────────────────────────

@faturas_bp.route('/review/<int:invoice_id>')
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — review OCR draft before saving
def review(invoice_id):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'danger')
        return redirect(url_for('faturas.upload'))

    stores = get_stores_list()

    # Rebuild ocr-like dict from stored data for the template
    ocr_raw = inv.get('ocr_raw') or {}
    if isinstance(ocr_raw, str):
        try:
            ocr_raw = json.loads(ocr_raw)
        except (json.JSONDecodeError, TypeError):
            ocr_raw = {}

    ocr = {
        'supplier_name': inv.get('supplier_name'),
        'supplier_nif': inv.get('supplier_nif'),
        'invoice_number': inv.get('invoice_number'),
        'amount_eur': inv.get('amount_eur'),
        'vat_amount_eur': inv.get('vat_amount_eur'),
        'issue_date': inv.get('issue_date').isoformat() if inv.get('issue_date') else None,
        'due_date': inv.get('due_date').isoformat() if inv.get('due_date') else None,
        'ocr_confidence': inv.get('ocr_confidence'),
        'confidence': ocr_raw,
        'error': None,
    }

    # Resolve supplier: FK first (works even without NIF), then NIF, then name
    supplier = None
    if inv.get('supplier_id'):
        supplier = get_supplier_by_id(inv['supplier_id'])
    if not supplier and inv.get('supplier_nif'):
        supplier = get_supplier_by_nif(inv['supplier_nif'])
    if not supplier and inv.get('supplier_name'):
        supplier = get_supplier_by_name(inv['supplier_name'])

    return_to = session.get('faturas_return_to', '')

    return render_template(
        'financeiro/faturas/review.html',
        inv=inv,
        ocr=ocr,
        supplier=supplier,
        pdf_filename=inv.get('pdf_filename', 'fatura.pdf'),
        stores=stores,
        subfolders=ONEDRIVE_SUBFOLDERS,
        categories=INVOICE_CATEGORIES,
        suppliers=get_suppliers(),
        suggested_subfolder=inv.get('onedrive_subfolder'),
        document_type_labels=DOCUMENT_TYPE_LABELS,
        payment_method_labels=PAYMENT_METHOD_LABELS,
        payment_terms_labels=PAYMENT_TERMS_LABELS,
        return_to=return_to,
        cost_centers=get_cost_centers(ativo_only=True),
        cost_categories_tree=get_cost_categories_tree(ativo_only=True),
    )


# ── Save after review ──────────────────────────────────────────────────────────

@faturas_bp.route('/save', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — save reviewed draft
def save():
    invoice_id = request.form.get('invoice_id', '').strip()
    if not invoice_id:
        flash('ID de fatura em falta. Tenta de novo.', 'danger')
        return redirect(url_for('faturas.upload'))
    invoice_id = int(invoice_id)

    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'danger')
        return redirect(url_for('faturas.upload'))

    return_to = request.form.get('return_to', '').strip()

    form_data = {
        'supplier_name': request.form.get('supplier_name', ''),
        'supplier_nif': request.form.get('supplier_nif', ''),
        'invoice_number': request.form.get('invoice_number', ''),
        'amount_eur': request.form.get('amount_eur', ''),
        'vat_amount_eur': request.form.get('vat_amount_eur', ''),
        'issue_date': request.form.get('issue_date', ''),
        'due_date': request.form.get('due_date', ''),
        'category': request.form.get('category', ''),
        'onedrive_subfolder': request.form.get('onedrive_subfolder', ''),
        'notes': request.form.get('notes', ''),
        'document_type': request.form.get('document_type', 'fatura'),
        'supplier_payment_method': request.form.get('supplier_payment_method', ''),
        'supplier_payment_terms': request.form.get('supplier_payment_terms', ''),
        'supplier_iban': request.form.get('supplier_iban', ''),
        'is_new_supplier': request.form.get('is_new_supplier', ''),
        'existing_supplier_id': request.form.get('existing_supplier_id', ''),
        'centro_custo_id': request.form.get('centro_custo_id', ''),
        'categoria_custo_id': request.form.get('categoria_custo_id', ''),
    }
    try:
        _review_user = session.get('user', {}).get('username', 'sistema')
        result = faturas_svc.save_reviewed_invoice(invoice_id, form_data, changed_by=_review_user)
    except ServiceError as e:
        flash(str(e), 'danger')
        return redirect(url_for('faturas.review', invoice_id=invoice_id))

    if result.get('warning'):
        flash(f'Documento guardado. Aviso OneDrive: {result["warning"]}', 'warning')
    else:
        flash('Documento guardado com sucesso!', 'success')

    # Clear the return_to context from session after save
    session.pop('faturas_return_to', None)

    if return_to == 'pagamentos':
        return redirect(url_for('faturas.index'))

    return _panel_redirect(invoice_id)


# ── Cancel draft ───────────────────────────────────────────────────────────────

@faturas_bp.route('/cancel_draft/<int:invoice_id>')
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — discard OCR draft
def cancel_draft(invoice_id):
    inv = get_invoice(invoice_id)
    if inv and inv.get('status') == 'draft':
        _cancel_user = session.get('user', {}).get('username', 'sistema')
        delete_invoice(invoice_id, deleted_by=_cancel_user)
    return_to = session.pop('faturas_return_to', '')
    if return_to == 'pagamentos':
        return redirect(url_for('pagamentos.nova_fatura'))
    return redirect(url_for('faturas.upload'))


# ── Detail ─────────────────────────────────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>')
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — invoice detail page
def detail(invoice_id: int):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))
    subfolders = ONEDRIVE_SUBFOLDERS
    categories = INVOICE_CATEGORIES
    suppliers = get_suppliers()
    today = date.today()
    payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]
    confirming_contracts = get_confirming_contracts()
    linhas = get_invoice_linhas(invoice_id)
    materiais = list_materiais(apenas_ativos=True)
    cost_centers = get_cost_centers(ativo_only=True)
    _cc_for_stock = next((c for c in cost_centers if c['id'] == inv.get('centro_custo_id')), None) if inv.get('centro_custo_id') else None
    stock_local_derivado = derive_local_from_store(store_id=_cc_for_stock.get('store_id') if _cc_for_stock else None)
    cost_categories_tree = get_cost_categories_tree()
    inv_centros_custo = get_invoice_centros_custo(invoice_id)
    from db.faturas import get_invoice_installments
    installments = get_invoice_installments(invoice_id)
    return render_template(
        'financeiro/faturas/detail.html',
        inv=inv,
        subfolders=subfolders,
        categories=categories,
        suppliers=suppliers,
        document_type_labels=DOCUMENT_TYPE_LABELS,
        today=today,
        payment_methods=payment_methods,
        confirming_contracts=confirming_contracts,
        linhas=linhas,
        materiais=materiais,
        locais_stock=LOCAIS_STOCK,
        unidades_materiais=UNIDADES_MATERIAIS,
        stock_local_derivado=stock_local_derivado,
        cost_centers=cost_centers,
        cost_categories_tree=cost_categories_tree,
        inv_centros_custo=inv_centros_custo,
        installments=installments,
    )


# ── Invoice cost-center allocation management ────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>/centros-custo', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — add/remove cost-centre allocations
def gestao_centros_custo(invoice_id: int):
    """Add or remove a cost-center allocation for an invoice."""
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))

    action = request.form.get('action', '').strip()
    return_url = request.form.get('_return_url', '').strip() or url_for('faturas.detail', invoice_id=invoice_id)

    if action == 'add':
        cc_raw = request.form.get('centro_custo_id', '').strip()
        pct_raw = request.form.get('percentagem', '100').strip().replace(',', '.')
        if not cc_raw:
            flash('Seleciona um centro de custo.', 'warning')
            return redirect(return_url)
        try:
            cc_id = int(cc_raw)
            pct = float(pct_raw)
            if pct <= 0 or pct > 100:
                raise ValueError('pct out of range')
        except (ValueError, TypeError):
            flash('Dados inválidos.', 'warning')
            return redirect(return_url)
        try:
            add_invoice_centro_custo(invoice_id, cc_id, pct)
            flash('Centro de custo adicionado.', 'success')
        except Exception as exc:
            logger.error('add_invoice_centro_custo inv=%s cc=%s: %s', invoice_id, cc_id, exc)
            flash(f'Erro ao adicionar: {exc}', 'danger')

    elif action == 'remove':
        cc_raw = request.form.get('centro_custo_id', '').strip()
        if not cc_raw:
            flash('Centro de custo não especificado.', 'warning')
            return redirect(return_url)
        try:
            cc_id = int(cc_raw)
        except ValueError:
            flash('Dados inválidos.', 'warning')
            return redirect(return_url)
        try:
            removed = remove_invoice_centro_custo(invoice_id, cc_id)
            if removed:
                flash('Alocação removida.', 'success')
            else:
                flash('Alocação não encontrada.', 'warning')
        except Exception as exc:
            logger.error('remove_invoice_centro_custo inv=%s cc=%s: %s', invoice_id, cc_id, exc)
            flash(f'Erro ao remover: {exc}', 'danger')
    else:
        flash('Acção inválida.', 'warning')

    return redirect(return_url)


# ── Invoice panel (offcanvas fragment) ──────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>/panel')
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — panel fragment shown to financial/compras users
def invoice_panel(invoice_id: int):
    inv = get_invoice(invoice_id)
    if not inv:
        return '<p class="text-danger p-3">Fatura não encontrada.</p>', 404
    today = date.today()
    if inv['status'] in ('pending_review', 'scheduled') and inv.get('due_date') and inv['due_date'] < today:
        inv['display_status'] = 'overdue'
        inv['status_label'] = get_invoice_status_labels_map().get('overdue', 'Vencida')
    linhas = get_invoice_linhas(invoice_id)
    materiais = list_materiais(apenas_ativos=True)
    _inv_cc_id = inv.get('centro_custo_id')
    _cc_store_id = None
    if _inv_cc_id:
        _cc_list = get_cost_centers(ativo_only=False)
        _cc = next((c for c in _cc_list if c['id'] == _inv_cc_id), None)
        _cc_store_id = _cc.get('store_id') if _cc else None
    stock_local_derivado = derive_local_from_store(store_id=_cc_store_id)
    panel_return_url = _safe_return_url(request.args.get('return_url', ''))
    from db.contabilidade import (
        get_tickets_for_invoice,
        ACCOUNTING_STATUS_LABELS,
        ACCOUNTING_STATUS_BADGE,
    )
    cont_tickets = get_tickets_for_invoice(invoice_id)
    acc_status = inv.get('accounting_status') or 'por_contabilizar'
    inv['accounting_status_label'] = ACCOUNTING_STATUS_LABELS.get(acc_status, acc_status)
    inv['accounting_status_badge'] = ACCOUNTING_STATUS_BADGE.get(acc_status, 'bg-secondary')
    from db.pagamentos import get_invoice_payment_record
    inv_payment = get_invoice_payment_record(invoice_id)
    audit_log = get_invoice_audit_log(invoice_id)
    doc_mimetype = _mimetype_from_filename(inv.get('pdf_filename') or '') if inv.get('pdf_filename') else None
    return render_template(
        'financeiro/faturas/_panel.html',
        inv=inv,
        today=today,
        document_type_labels=DOCUMENT_TYPE_LABELS,
        categories=INVOICE_CATEGORIES,
        subfolders=ONEDRIVE_SUBFOLDERS,
        payment_methods=[m for m in get_payment_methods_config() if m.get('ativo')],
        confirming_contracts=get_confirming_contracts(),
        linhas=linhas,
        materiais=materiais,
        locais_stock=LOCAIS_STOCK,
        unidades_materiais=UNIDADES_MATERIAIS,
        stock_local_derivado=stock_local_derivado,
        suppliers=get_suppliers(),
        panel_return_url=panel_return_url,
        cont_tickets=cont_tickets,
        inv_payment=inv_payment,
        audit_log=audit_log,
        doc_mimetype=doc_mimetype,
        cost_centers=get_cost_centers(ativo_only=True),
    )


# ── Inline paid-date update ────────────────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>/set-paid-date', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — inline paid-date correction
def set_paid_date(invoice_id: int):
    from datetime import datetime as _dt
    inv = get_invoice(invoice_id)
    if not inv:
        return jsonify({'ok': False, 'error': 'Documento não encontrado'}), 404
    data = request.get_json(silent=True) or {}
    raw = (data.get('paid_date') or '').strip()
    paid_date = None
    if raw:
        try:
            paid_date = _dt.strptime(raw, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'ok': False, 'error': 'Data inválida'}), 400
    _actor = session.get('user', {}).get('username', 'sistema')
    try:
        update_invoice(invoice_id, {'paid_date': paid_date}, changed_by=_actor)
    except Exception as e:
        logger.error('set_paid_date error: %s', e)
        return jsonify({'ok': False, 'error': 'Erro interno'}), 500
    return jsonify({'ok': True, 'paid_date': paid_date.isoformat() if paid_date else ''})


# ── Invoice Linhas (line items) ────────────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>/linha', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — manage invoice line items
def linha(invoice_id: int):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))

    if inv.get('stock_registado_at'):
        flash('As linhas desta fatura não podem ser alteradas após o stock ter sido registado.', 'warning')
        return _panel_redirect(invoice_id)

    action = request.form.get('action', '')
    if action == 'delete':
        linha_id = request.form.get('linha_id', type=int)
        if linha_id:
            delete_invoice_linha(linha_id, invoice_id)
            flash('Linha eliminada.', 'success')
        return _panel_redirect(invoice_id)

    descricao = request.form.get('descricao', '').strip()
    if not descricao:
        flash('A descrição é obrigatória.', 'warning')
        return _panel_redirect(invoice_id)

    try:
        quantidade = float(request.form.get('quantidade', '').replace(',', '.'))
    except (ValueError, AttributeError):
        flash('Quantidade inválida.', 'warning')
        return _panel_redirect(invoice_id)

    unidade = request.form.get('unidade', 'un').strip()
    material_id_raw = request.form.get('material_id', '').strip()
    try:
        material_id = int(material_id_raw) if material_id_raw else None
    except ValueError:
        material_id = None

    preco_raw = request.form.get('preco_unitario', '').replace(',', '.').strip()
    preco_unitario = None
    if preco_raw:
        try:
            preco_unitario = float(preco_raw)
        except ValueError:
            flash('Preço unitário inválido — deve ser um número (ex: 1.50).', 'warning')
            return _panel_redirect(invoice_id)

    linha_id = request.form.get('linha_id', type=int)

    try:
        saved_id = upsert_invoice_linha(
            invoice_id=invoice_id,
            descricao=descricao,
            quantidade=quantidade,
            unidade=unidade,
            material_id=material_id,
            preco_unitario=preco_unitario,
            linha_id=linha_id,
        )
    except ValueError as e:
        flash(f'Erro ao guardar linha: {e}', 'warning')
        return _panel_redirect(invoice_id)
    except psycopg2.IntegrityError as e:
        logging.warning('IntegrityError saving invoice linha: %s', e)
        flash('Não foi possível guardar a linha (material inválido ou dados inconsistentes).', 'warning')
        return _panel_redirect(invoice_id)
    if saved_id is None:
        flash('Linha não encontrada ou sem permissão para editar.', 'warning')
        return _panel_redirect(invoice_id)
    flash('Linha guardada.', 'success')
    return _panel_redirect(invoice_id)


@faturas_bp.route('/<int:invoice_id>/registar-stock', methods=['POST'])
@perm_required('acesso_gestor')  # intentionally restricted — stock entries affect inventory; requires full gestor access
def registar_stock(invoice_id: int):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))

    if inv.get('stock_registado_at'):
        flash('O stock desta fatura já foi registado e não pode ser executado novamente.', 'warning')
        return _panel_redirect(invoice_id)

    # Derive local from invoice CC store; fall back to submitted value if unmapped
    _rs_cc_id = inv.get('centro_custo_id')
    _rs_store_id = None
    if _rs_cc_id:
        _rs_cc_list = get_cost_centers(ativo_only=False)
        _rs_cc = next((c for c in _rs_cc_list if c['id'] == _rs_cc_id), None)
        _rs_store_id = _rs_cc.get('store_id') if _rs_cc else None
    local = derive_local_from_store(store_id=_rs_store_id)
    if not local:
        local = request.form.get('local', '').strip()
    if local not in LOCAIS_STOCK:
        flash('Não foi possível determinar o local de stock. Seleciona um local e tenta de novo.', 'warning')
        return _panel_redirect(invoice_id)

    utilizador = session.get('user', {}).get('username', 'system')
    try:
        resultado = registar_entradas_stock_fatura(invoice_id, utilizador, local)
    except ValueError as e:
        flash(str(e), 'warning')
        return _panel_redirect(invoice_id)
    except Exception as e:
        logging.error('Erro ao registar stock para fatura %s: %s', invoice_id, e)
        flash('Ocorreu um erro inesperado ao registar o stock. Tenta novamente.', 'danger')
        return _panel_redirect(invoice_id)

    n = resultado['registadas']
    if n == 0:
        flash('Nenhuma linha com material associado por registar.', 'info')
    else:
        flash(f'{n} entrada(s) de stock registada(s) em {local}.', 'success')
    return _panel_redirect(invoice_id)


# ── Edit ───────────────────────────────────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>/edit', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — edit invoice fields
def edit(invoice_id: int):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))

    supplier_name = request.form.get('supplier_name', '').strip()
    supplier_nif = ''.join(c for c in request.form.get('supplier_nif', '') if c.isdigit())
    invoice_number = request.form.get('invoice_number', '').strip()
    amount_eur = _parse_float(request.form.get('amount_eur', ''))
    vat_amount_eur = _parse_float(request.form.get('vat_amount_eur', ''))
    issue_date = _parse_date(request.form.get('issue_date', ''))
    due_date = _parse_date(request.form.get('due_date', ''))
    category = request.form.get('category', '').strip()
    onedrive_subfolder = request.form.get('onedrive_subfolder', '').strip()
    notes = request.form.get('notes', '').strip()
    status = request.form.get('status', inv['status'])
    document_type = request.form.get('document_type', inv.get('document_type', 'fatura'))
    cc_raw = request.form.get('centro_custo_id', '').strip()
    centro_custo_id = int(cc_raw) if cc_raw.isdigit() else None
    cat_raw = request.form.get('categoria_custo_id', '').strip()
    categoria_custo_id = int(cat_raw) if cat_raw.isdigit() else None
    if document_type not in DOCUMENT_TYPE_LABELS:
        document_type = 'fatura'

    supplier_id = inv.get('supplier_id')

    # Accept supplier_id directly from the dropdown (closed-list selection)
    supplier_id_form = request.form.get('supplier_id', '').strip()
    if supplier_id_form.isdigit():
        supplier_id = int(supplier_id_form)
        # Always use the canonical name/nif from the suppliers table when a supplier_id
        # is provided — the hidden supplier_name field may still hold the old supplier's
        # name if the user just changed the dropdown, so we cannot rely on it.
        # NIF is also set unconditionally: if the new supplier has no NIF the old value
        # must be cleared, not kept, to avoid a mismatched name/NIF pair.
        try:
            _sup = get_supplier_by_id(supplier_id)
            if _sup:
                supplier_name = _sup['name']
                supplier_nif = _sup.get('nif') or ''
        except Exception:
            pass
    elif supplier_name and not supplier_id:
        # Fallback: try name-based lookup when no NIF is provided
        try:
            _matched = get_supplier_by_name(supplier_name)
            if _matched:
                supplier_id = _matched['id']
        except Exception:
            pass

    # Enforce: invoice-type docs in non-draft statuses must have supplier_id.
    # Draft edits are allowed so users can fill in supplier progressively.
    _INVOICE_EDIT_DOC_TYPES = {'fatura', 'nota_credito', 'nota_debito'}
    _NON_DRAFT_STATUSES = {'pending_review', 'scheduled', 'paid', 'overdue'}
    if (document_type in _INVOICE_EDIT_DOC_TYPES
            and status in _NON_DRAFT_STATUSES
            and not supplier_id):
        flash('Seleciona um fornecedor antes de guardar este tipo de documento em estado não rascunho.', 'warning')
        return _panel_redirect(invoice_id)

    _edit_user = session.get('user', {}).get('username', 'sistema')
    update_invoice(invoice_id, {
        'supplier_id': supplier_id,
        'supplier_name': supplier_name or None,
        'supplier_nif': supplier_nif or None,
        'invoice_number': invoice_number or None,
        'amount_eur': amount_eur,
        'vat_amount_eur': vat_amount_eur,
        'issue_date': issue_date,
        'due_date': due_date,
        'category': category or None,
        'onedrive_subfolder': onedrive_subfolder or None,
        'status': status,
        'notes': notes or None,
        'document_type': document_type,
        'centro_custo_id': centro_custo_id,
        'categoria_custo_id': categoria_custo_id,
    }, changed_by=_edit_user)

    flash('Documento actualizado.', 'success')
    return _panel_redirect(invoice_id)


# ── Quick actions ──────────────────────────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>/confirmar', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — schedule/confirm payment date
def confirmar(invoice_id: int):
    confirmed_date = _parse_date(request.form.get('confirmed_date', ''))
    if not confirmed_date:
        flash('É obrigatório indicar a data de confirmação.', 'warning')
        return _panel_redirect(invoice_id)
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))
    if inv.get('status') not in ('pending_review', 'scheduled', 'overdue'):
        flash('Estado da fatura não permite confirmação de data.', 'warning')
        return _panel_redirect(invoice_id)
    amount_eur = inv.get('amount_eur')
    current_user = session.get('user', {}).get('username', 'system')
    payment_method = request.form.get('payment_method', '').strip() or None
    confirming_id_raw = request.form.get('confirming_contract_id', '').strip()
    confirming_id = int(confirming_id_raw) if confirming_id_raw.isdigit() else None
    confirm_invoice_payment(invoice_id, confirmed_date, amount_eur, current_user,
                            payment_method=payment_method,
                            confirming_contract_id=confirming_id)
    if payment_method == 'confirming':
        if not confirming_id:
            flash('Selecione um contrato de confirming.', 'warning')
            return _panel_redirect(invoice_id)
        try:
            parcelas = _parse_confirming_parcelas()
            if parcelas:
                total = sum(p['montante'] for p in parcelas)
                if amount_eur and abs(total - float(amount_eur)) > 0.02:
                    flash(f'Total das parcelas ({total:.2f} €) difere do valor da fatura ({float(amount_eur):.2f} €).', 'warning')
                    return _panel_redirect(invoice_id)
                create_confirming_parcelas_batch(invoice_id, confirming_id, parcelas, estado='scheduled')
            else:
                create_confirming_parcela(invoice_id, confirming_id, float(amount_eur or 0), confirmed_date,
                                          estado='scheduled')
        except Exception as e:
            logger.warning('create_confirming_parcela invoice=%s: %s', invoice_id, e)
    flash('Data confirmada. Fatura agendada.', 'success')
    return _panel_redirect(invoice_id)


@faturas_bp.route('/<int:invoice_id>/pagar', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — mark invoice as paid
def pagar(invoice_id: int):
    current_user = session.get('user', {}).get('username', 'system')

    # ── Installment (parcelado) mode ────────────────────────────────────────
    if request.form.get('installment_mode') == '1':
        montantes = request.form.getlist('inst_montante[]')
        datas = request.form.getlist('inst_data[]')
        if len(montantes) < 2 or len(montantes) != len(datas):
            flash('Pagamento parcelado requer pelo menos 2 parcelas com valor e data.', 'warning')
            return _panel_redirect(invoice_id)
        installments = []
        total = 0.0
        for m_raw, d_raw in zip(montantes, datas):
            amount = _parse_float(m_raw)
            dt = _parse_date(d_raw)
            if amount is None or not dt:
                flash('Valor ou data inválidos nas parcelas.', 'warning')
                return _panel_redirect(invoice_id)
            total += amount
            installments.append({'amount_eur': amount, 'due_date': dt})
        inv = get_invoice(invoice_id)
        if inv and inv.get('amount_eur') is not None:
            inv_total = float(inv['amount_eur'])
            if abs(total - inv_total) > 0.02:
                flash(
                    f'Atenção: total das parcelas ({total:.2f} €) difere do valor da fatura '
                    f'({inv_total:.2f} €). Plano criado mesmo assim.',
                    'warning',
                )
        try:
            from db.faturas import create_invoice_installments
            create_invoice_installments(invoice_id, installments, current_user)
            flash(f'Plano parcelado criado: {len(installments)} parcelas (1ª já marcada como paga).', 'success')
        except Exception as e:
            logger.error('create_invoice_installments invoice=%s: %s', invoice_id, e)
            flash('Erro ao criar parcelas. Tente novamente.', 'danger')
        return _panel_redirect(invoice_id)

    # ── Normal (full) payment ───────────────────────────────────────────────
    # Block if invoice has an active installment plan with unpaid installments
    inv_check = get_invoice(invoice_id)
    if inv_check and inv_check.get('installment_total', 0) > 0:
        from db.faturas import get_invoice_installments
        unpaid = [i for i in get_invoice_installments(invoice_id) if i['status'] != 'paid']
        if unpaid:
            flash(
                f'Esta fatura tem {len(unpaid)} parcela(s) em aberto. '
                'Use os botões de pagamento individuais para cada parcela.',
                'warning',
            )
            return _panel_redirect(invoice_id)

    paid_date = _parse_date(request.form.get('paid_date', ''))
    if not paid_date:
        flash('É obrigatório indicar a data de pagamento.', 'warning')
        return _panel_redirect(invoice_id)
    payment_method = request.form.get('payment_method', '').strip() or None
    confirming_id_raw = request.form.get('confirming_contract_id', '').strip()
    confirming_id = int(confirming_id_raw) if confirming_id_raw.isdigit() else None
    mark_payment_executed(invoice_id, paid_date, current_user,
                          payment_method=payment_method,
                          confirming_contract_id=confirming_id)
    if payment_method == 'confirming':
        if not confirming_id:
            flash('Selecione um contrato de confirming.', 'warning')
            return _panel_redirect(invoice_id)
        inv = get_invoice(invoice_id)
        if inv:
            amount_eur = inv.get('amount_eur')
            try:
                parcelas = _parse_confirming_parcelas()
                if parcelas:
                    total = sum(p['montante'] for p in parcelas)
                    if amount_eur and abs(total - float(amount_eur)) > 0.02:
                        flash(f'Total das parcelas ({total:.2f} €) difere do valor da fatura ({float(amount_eur):.2f} €).', 'warning')
                        return _panel_redirect(invoice_id)
                    create_confirming_parcelas_batch(invoice_id, confirming_id, parcelas, estado='paid')
                else:
                    create_confirming_parcela(invoice_id, confirming_id, float(amount_eur or 0), paid_date,
                                              estado='paid')
            except Exception as e:
                logger.warning('create_confirming_parcela pagar invoice=%s: %s', invoice_id, e)
    flash('Fatura marcada como paga.', 'success')
    return _panel_redirect(invoice_id)


@faturas_bp.route('/<int:invoice_id>/installment/<int:installment_id>/pagar', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — pay individual installment
def installment_pagar(invoice_id: int, installment_id: int):
    paid_date = _parse_date(request.form.get('paid_date', ''))
    if not paid_date:
        flash('É obrigatório indicar a data de pagamento.', 'warning')
        return _panel_redirect(invoice_id)
    current_user = session.get('user', {}).get('username', 'system')
    try:
        from db.faturas import mark_installment_paid
        all_paid = mark_installment_paid(installment_id, invoice_id, paid_date, current_user)
        if all_paid:
            flash('Todas as parcelas pagas — fatura marcada como paga.', 'success')
        else:
            flash('Parcela marcada como paga.', 'success')
    except Exception as e:
        logger.error('mark_installment_paid inst=%s: %s', installment_id, e)
        flash('Erro ao marcar parcela como paga.', 'danger')
    return _panel_redirect(invoice_id)


@faturas_bp.route('/<int:invoice_id>/arquivar-onedrive', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — archive PDF to OneDrive
def arquivar_onedrive(invoice_id: int):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))

    pdf_data, pdf_filename = get_invoice_pdf(invoice_id)
    if not pdf_data:
        flash('PDF não disponível para arquivo.', 'warning')
        return _panel_redirect(invoice_id)

    subfolder = request.form.get('onedrive_subfolder') or inv.get('onedrive_subfolder') or 'Gestão'
    try:
        from flask_app.onedrive_archive import upload_invoice_pdf
        result = upload_invoice_pdf(pdf_data, pdf_filename or 'fatura.pdf', subfolder, inv.get('issue_date'))
        if result.get('onedrive_path'):
            update_invoice_onedrive(invoice_id, result['onedrive_path'], subfolder,
                                    onedrive_web_url=result.get('web_url'))
            flash(f'PDF arquivado no OneDrive: {result["onedrive_path"]}', 'success')
        else:
            flash(f'Erro: {result.get("warning", "Falha ao arquivar.")}', 'warning')
    except Exception as _od_exc:
        logger.warning('arquivar_onedrive failed for invoice %s: %s', invoice_id, _od_exc)
        flash('Não foi possível arquivar no OneDrive. Tenta novamente ou contacta o administrador.', 'warning')

    return _panel_redirect(invoice_id)


@faturas_bp.route('/<int:invoice_id>/eliminar', methods=['POST'])
@perm_required('acesso_gestor')  # intentionally restricted — deletion requires full gestor access
def eliminar(invoice_id: int):
    _del_user = session.get('user', {}).get('username', 'sistema')
    delete_invoice(invoice_id, deleted_by=_del_user)
    flash('Fatura eliminada.', 'success')
    return redirect(url_for('faturas.index'))


@faturas_bp.route('/<int:invoice_id>/set-categoria-custo', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro or acesso_compras
def set_categoria_custo(invoice_id: int):
    """Quick-assign categoria_custo_id for an invoice (used inline from the cash-flow map)."""
    data = request.get_json(silent=True) or {}
    raw = data.get('categoria_custo_id')
    try:
        cat_id = int(raw) if raw not in (None, '', 'null') else None
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'ID de categoria inválido'}), 400
    inv = get_invoice(invoice_id)
    if not inv:
        return jsonify({'ok': False, 'error': 'Fatura não encontrada'}), 404
    _actor = session.get('user', {}).get('username', 'sistema')
    try:
        update_invoice(invoice_id, {'categoria_custo_id': cat_id}, changed_by=_actor)
    except Exception as e:
        logger.error('set_categoria_custo error: %s', e)
        return jsonify({'ok': False, 'error': 'Erro interno'}), 500
    return jsonify({'ok': True, 'invoice_id': invoice_id, 'categoria_custo_id': cat_id})


@faturas_bp.route('/<int:invoice_id>/history')
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — audit timeline (read-only JSON)
def invoice_history(invoice_id: int):
    """Return the complete audit timeline for a document — including deleted ones.

    For live invoices returns the invoice_audit_log.
    For deleted invoices returns the prior_audit from invoice_deletion_log.
    Always returns JSON so managers can query the timeline programmatically.
    """
    live = get_invoice(invoice_id)
    if live:
        audit = get_invoice_audit_log(invoice_id)
        timeline = [
            {
                'campo_alterado': e['campo_alterado'],
                'valor_anterior': e['valor_anterior'],
                'valor_novo': e['valor_novo'],
                'alterado_por': e['alterado_por'],
                'alterado_em': e['alterado_em'].isoformat() if e['alterado_em'] else None,
            }
            for e in audit
        ]
        return jsonify({
            'invoice_id': invoice_id,
            'deleted': False,
            'supplier_name': live.get('supplier_name'),
            'invoice_number': live.get('invoice_number'),
            'timeline': timeline,
        })
    tombstone = get_invoice_deletion_log(invoice_id)
    if tombstone:
        return jsonify({
            'invoice_id': invoice_id,
            'deleted': True,
            'deleted_by': tombstone['deleted_by'],
            'deleted_at': tombstone['deleted_at'].isoformat() if tombstone['deleted_at'] else None,
            'supplier_name': tombstone['supplier_name'],
            'invoice_number': tombstone['invoice_number'],
            'amount_eur': str(tombstone['amount_eur']) if tombstone['amount_eur'] is not None else None,
            'status_at_deletion': tombstone['status_at_deletion'],
            'timeline': tombstone['prior_audit'],
        })
    return jsonify({'ok': False, 'error': 'Documento não encontrado'}), 404


# ── PDF download / attach ───────────────────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>/attach-pdf', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — attach PDF evidence
def attach_pdf(invoice_id: int):
    from db.faturas import save_invoice_pdf as _save_pdf
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return _panel_redirect(invoice_id)
    pdf_file = request.files.get('pdf_file')
    if not pdf_file or not pdf_file.filename:
        flash('Nenhum ficheiro seleccionado.', 'warning')
        return _panel_redirect(invoice_id)
    if _ext(pdf_file.filename) != 'pdf':
        flash('Apenas ficheiros PDF são aceites.', 'warning')
        return _panel_redirect(invoice_id)
    pdf_data = pdf_file.read()
    if not pdf_data.startswith(b'%PDF'):
        flash('Ficheiro não é um PDF válido.', 'warning')
        return _panel_redirect(invoice_id)
    _save_pdf(invoice_id, pdf_data, pdf_file.filename or 'fatura.pdf')
    flash('PDF anexado com sucesso.', 'success')
    return _panel_redirect(invoice_id)


@faturas_bp.route('/<int:invoice_id>/upload-chunk', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — chunked PDF upload
def upload_chunk(invoice_id: int):
    import re, shutil
    upload_id = request.form.get('upload_id', '')
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', upload_id):
        return jsonify({'ok': False, 'error': 'upload_id inválido'}), 400
    try:
        chunk_index = int(request.form.get('chunk_index', -1))
        total_chunks = int(request.form.get('total_chunks', 0))
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'Parâmetros inválidos'}), 400
    if chunk_index < 0 or total_chunks < 1 or chunk_index >= total_chunks:
        return jsonify({'ok': False, 'error': 'Índice de chunk inválido'}), 400
    chunk_file = request.files.get('chunk')
    if not chunk_file:
        return jsonify({'ok': False, 'error': 'Dados do chunk em falta'}), 400
    chunk_data = chunk_file.read()
    if len(chunk_data) > 4 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Chunk demasiado grande'}), 400
    chunk_dir = f'/tmp/pdf_upload_{upload_id}'
    os.makedirs(chunk_dir, exist_ok=True)
    chunk_path = os.path.join(chunk_dir, f'chunk_{chunk_index:06d}')
    with open(chunk_path, 'wb') as f:
        f.write(chunk_data)
    return jsonify({'ok': True, 'chunk': chunk_index})


@faturas_bp.route('/<int:invoice_id>/finalize-upload', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — finalize chunked upload
def finalize_upload(invoice_id: int):
    import re, glob, shutil
    from db.faturas import save_invoice_pdf as _save_pdf
    upload_id = request.form.get('upload_id', '')
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', upload_id):
        return jsonify({'ok': False, 'error': 'upload_id inválido'}), 400
    try:
        total_chunks = int(request.form.get('total_chunks', 0))
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'Parâmetros inválidos'}), 400
    filename = request.form.get('filename', 'fatura.pdf').strip()
    if _ext(filename) != 'pdf':
        filename = 'fatura.pdf'
    back = request.form.get('_return_url', '').strip() or url_for('faturas.index')
    chunk_dir = f'/tmp/pdf_upload_{upload_id}'
    chunk_files = sorted(glob.glob(os.path.join(chunk_dir, 'chunk_*')))
    if len(chunk_files) != total_chunks:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        return jsonify({'ok': False, 'error': f'Esperados {total_chunks} chunks, recebidos {len(chunk_files)}'}), 400
    pdf_data = b''
    for cf in chunk_files:
        with open(cf, 'rb') as f:
            pdf_data += f.read()
    shutil.rmtree(chunk_dir, ignore_errors=True)
    if not pdf_data.startswith(b'%PDF'):
        return jsonify({'ok': False, 'error': 'Ficheiro não é um PDF válido'}), 400
    if len(pdf_data) > 55 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Ficheiro demasiado grande (máx. 50 MB)'}), 400
    inv = get_invoice(invoice_id)
    if not inv:
        return jsonify({'ok': False, 'error': 'Fatura não encontrada'}), 404
    _save_pdf(invoice_id, pdf_data, filename or 'fatura.pdf')
    flash('PDF anexado com sucesso.', 'success')
    return jsonify({'ok': True, 'redirect': back})


def _mimetype_from_filename(filename: str) -> str:
    """Infer MIME type from filename extension alone (no bytes needed)."""
    ext = (filename or '').lower().rsplit('.', 1)[-1]
    return {
        'pdf': 'application/pdf',
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'png': 'image/png',
        'heic': 'image/heic', 'heif': 'image/heif',
        'webp': 'image/webp',
    }.get(ext, 'application/octet-stream')


def _detect_file_mimetype(data, filename: str) -> str:
    if isinstance(data, memoryview):
        data = bytes(data)
    if data.startswith(b'%PDF'):
        return 'application/pdf'
    if len(data) >= 2 and data[:2] == b'\xff\xd8':
        return 'image/jpeg'
    if len(data) >= 8 and data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    ext = (filename or '').lower().rsplit('.', 1)[-1]
    return {'pdf': 'application/pdf', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png'}.get(ext, 'application/octet-stream')


@faturas_bp.route('/<int:invoice_id>/pdf')
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — view/download PDF evidence
def download_pdf(invoice_id: int):
    pdf_data, pdf_filename = get_invoice_pdf(invoice_id)
    if not pdf_data:
        flash('PDF não disponível.', 'warning')
        return _panel_redirect(invoice_id)
    filename = pdf_filename or 'fatura.pdf'
    mimetype = _detect_file_mimetype(pdf_data, filename)
    as_attachment = request.args.get('dl') == '1'
    response = send_file(
        BytesIO(pdf_data),
        mimetype=mimetype,
        as_attachment=as_attachment,
        download_name=filename,
    )
    if not as_attachment:
        response.headers['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


# ── Suppliers management ───────────────────────────────────────────────────────

@faturas_bp.route('/fornecedores/<int:supplier_id>/info')
@any_perm_required('acesso_financeiro', 'acesso_compras')
def supplier_info(supplier_id: int):
    """Lightweight JSON endpoint: returns key supplier fields for JS pre-fill."""
    sup = get_supplier_by_id(supplier_id)
    if not sup:
        return jsonify({'error': 'not_found'}), 404
    return jsonify({
        'id': sup['id'],
        'name': sup['name'],
        'centro_custo_id': sup.get('centro_custo_id'),
        'payment_method': sup.get('payment_method'),
        'payment_terms': sup.get('payment_terms'),
    })


@faturas_bp.route('/fornecedores', methods=['GET', 'POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — supplier list/create/edit; destructive actions guarded below
def fornecedores():
    stores = get_stores_list()
    if request.method == 'POST':
        action = request.form.get('action', '')

        # Bulk maintenance actions remain restricted to gestor/admin — they affect all data
        _BULK_ADMIN_ACTIONS = {'backfill', 'normalise'}
        if action in _BULK_ADMIN_ACTIONS:
            _u = session.get('user', {})
            if not (_u.get('acesso_gestor') or _u.get('acesso_administrativo')):
                flash('Não tens permissão para realizar esta acção.', 'danger')
                return redirect(url_for('faturas.fornecedores'))
        # Supplier CRUD actions (delete, merge, rename) are allowed for
        # acesso_compras and acesso_financeiro — enforced by the route decorator.

        if action == 'save':
            name = request.form.get('name', '').strip()
            nif = ''.join(c for c in request.form.get('nif', '') if c.isdigit())
            store_id = request.form.get('store_id', '') or None
            if store_id:
                store_id = int(store_id)
            notes = request.form.get('notes', '').strip()
            payment_method = request.form.get('payment_method', '').strip() or None
            payment_terms = request.form.get('payment_terms', '').strip() or None
            iban = request.form.get('iban', '').strip() or None
            _cc_raw = request.form.get('centro_custo_id', '').strip()
            centro_custo_id = int(_cc_raw) if _cc_raw.isdigit() else None
            _ccat_raw = request.form.get('categoria_custo_id', '').strip()
            categoria_custo_id = int(_ccat_raw) if _ccat_raw.isdigit() else None
            supplier_id_raw = request.form.get('supplier_id', '').strip()
            if not name:
                flash('Nome do fornecedor é obrigatório.', 'warning')
            elif supplier_id_raw.isdigit():
                from db.faturas import update_supplier
                update_supplier(int(supplier_id_raw), name=name, nif=nif or None,
                                store_id=store_id,
                                notes=notes or None, payment_method=payment_method,
                                payment_terms=payment_terms, iban=iban,
                                centro_custo_id=centro_custo_id,
                                categoria_custo_id=categoria_custo_id)
                flash(f'Fornecedor "{name}" actualizado.', 'success')
            else:
                upsert_supplier(name=name, nif=nif or None,
                                store_id=store_id, notes=notes or None,
                                payment_method=payment_method,
                                payment_terms=payment_terms, iban=iban,
                                centro_custo_id=centro_custo_id,
                                categoria_custo_id=categoria_custo_id)
                flash(f'Fornecedor "{name}" criado.', 'success')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'delete':
            supplier_id = int(request.form.get('supplier_id', 0))
            ok = delete_supplier(supplier_id)
            if ok:
                flash('Fornecedor eliminado.', 'success')
            else:
                flash('Não é possível eliminar: fornecedor tem faturas associadas.', 'warning')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'merge':
            source_id_str = request.form.get('merge_source_id', '').strip()
            target_id_str = request.form.get('merge_target_id', '').strip()
            if not source_id_str or not target_id_str:
                flash('Seleciona fornecedor de origem e destino.', 'warning')
            else:
                try:
                    count = merge_supplier(int(source_id_str), int(target_id_str))
                    flash(f'Fusão concluída: {count} fatura(s) re-ligada(s).', 'success')
                except ValueError as exc:
                    flash(str(exc), 'warning')
                except Exception as exc:
                    flash(f'Erro ao fundir fornecedores: {exc}', 'danger')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'associate':
            supplier_name_raw = request.form.get('unlinked_name', '').strip()
            assoc_supplier_id_str = request.form.get('associate_supplier_id', '').strip()
            if supplier_name_raw and assoc_supplier_id_str:
                assoc_supplier_id = int(assoc_supplier_id_str)
                count = bulk_link_invoices_by_name(supplier_name_raw, assoc_supplier_id)
                flash(f'"{supplier_name_raw}" associado: {count} fatura(s) ligada(s).', 'success')
            else:
                flash('Selecciona um fornecedor para associar.', 'warning')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'backfill':
            result = backfill_supplier_ids()
            flash(
                f'Sincronização concluída: {result["suppliers_created"]} fornecedor(es) criado(s), '
                f'{result["invoices_linked"]} fatura(s) ligada(s).',
                'success'
            )
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'bulk_merge':
            pair_count = int(request.form.get('pair_count', 0))
            total_merged = 0
            total_invoices = 0
            errors = []
            for i in range(pair_count):
                if not request.form.get(f'sel_{i}'):
                    continue  # unchecked — skip
                src_str = request.form.get(f'src_{i}', '').strip()
                tgt_str = request.form.get(f'tgt_{i}', '').strip()
                if not src_str.isdigit() or not tgt_str.isdigit():
                    continue
                try:
                    n = merge_supplier(int(src_str), int(tgt_str))
                    total_invoices += n
                    total_merged += 1
                except Exception as exc:
                    errors.append(str(exc))
            if total_merged:
                flash(f'✅ {total_merged} par(es) fundido(s) — {total_invoices} fatura(s) re-ligada(s).', 'success')
            if errors:
                for err in errors[:3]:
                    flash(f'⚠ {err}', 'warning')
            if not total_merged and not errors:
                flash('Nenhum par seleccionado.', 'info')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'ignore_pair':
            id_a_raw = request.form.get('id_a', '').strip()
            id_b_raw = request.form.get('id_b', '').strip()
            if id_a_raw.isdigit() and id_b_raw.isdigit():
                ignore_supplier_pair(int(id_a_raw), int(id_b_raw))
                flash('Par ignorado — não voltará a aparecer nas sugestões.', 'info')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'normalise':
            count = normalise_supplier_names()
            if count:
                flash(f'Nomes normalizados: {count} fatura(s) actualizadas com o nome canónico do fornecedor.', 'success')
            else:
                flash('Todos os nomes já estão normalizados.', 'info')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'rename_variant':
            old_name = request.form.get('old_name', '').strip()
            new_name = request.form.get('new_name', '').strip()
            if not old_name or not new_name:
                flash('Nome original e novo nome são obrigatórios.', 'warning')
            elif old_name == new_name:
                flash('O nome novo é igual ao original.', 'warning')
            else:
                count = rename_supplier_name_variant(old_name, new_name)
                if count:
                    flash(f'"{old_name}" → "{new_name}": {count} fatura(s) renomeada(s).', 'success')
                else:
                    flash(f'Nenhuma fatura sem ligação encontrada com o nome "{old_name}".', 'info')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'delete_alias':
            alias_id_str = request.form.get('alias_id', '').strip()
            if alias_id_str.isdigit():
                ok = delete_supplier_alias(int(alias_id_str))
                if ok:
                    flash('Alias eliminado.', 'success')
                else:
                    flash('Alias não encontrado.', 'warning')
            else:
                flash('ID de alias inválido.', 'warning')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'add_alias':
            supplier_id_str = request.form.get('supplier_id', '').strip()
            alias_name = request.form.get('alias_name', '').strip()
            if not supplier_id_str.isdigit() or not alias_name:
                flash('Fornecedor e nome do alias são obrigatórios.', 'warning')
            else:
                ok = add_supplier_alias(int(supplier_id_str), alias_name)
                if ok:
                    flash(f'Alias "{alias_name}" adicionado.', 'success')
                else:
                    flash(f'Alias "{alias_name}" já existe ou não foi possível adicionar.', 'warning')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'bulk_edit':
            from flask import jsonify as _jsonify
            _u = session.get('user', {})
            if not (_u.get('acesso_gestor') or _u.get('acesso_administrativo')):
                return _jsonify({'ok': False, 'error': 'Sem permissão para edição em massa.'}), 403
            _BULK_EDIT_FIELDS = {'payment_method', 'payment_terms', 'centro_custo_id', 'categoria_custo_id'}
            field = request.form.get('field', '').strip()
            if field not in _BULK_EDIT_FIELDS:
                return _jsonify({'ok': False, 'error': 'Campo inválido'}), 400
            raw_value = request.form.get('value', '').strip()
            # Coerce integer fields; treat empty string as NULL
            if field in ('centro_custo_id', 'categoria_custo_id'):
                value = int(raw_value) if raw_value.isdigit() else None
            else:
                value = raw_value or None
            supplier_ids = [int(s) for s in request.form.getlist('supplier_ids[]') if s.isdigit()]
            if not supplier_ids:
                return _jsonify({'ok': False, 'error': 'Nenhum fornecedor seleccionado'}), 400
            updated = sum(1 for sid in supplier_ids if patch_supplier(sid, **{field: value}))
            return _jsonify({'ok': True, 'updated': updated})

    suppliers = get_suppliers_with_invoice_count()
    categories = INVOICE_CATEGORIES
    from db.centros_custo import get_cost_categories as _get_cost_cats
    cost_categories = _get_cost_cats(ativo_only=True)
    unlinked_names = get_unlinked_supplier_names()
    try:
        duplicate_pairs = get_duplicate_supplier_suggestions(suppliers)
    except Exception as _dup_exc:
        logger.warning('get_duplicate_supplier_suggestions failed: %s', _dup_exc)
        duplicate_pairs = []
    supplier_aliases = get_all_supplier_aliases()
    cost_centers = get_cost_centers(ativo_only=True)
    _usr = session.get('user', {})
    can_bulk_edit = bool(_usr.get('acesso_gestor') or _usr.get('acesso_administrativo'))
    return render_template(
        'financeiro/faturas/fornecedores.html',
        suppliers=suppliers,
        categories=categories,
        cost_categories=cost_categories,
        stores=stores,
        payment_method_labels=PAYMENT_METHOD_LABELS,
        payment_terms_labels=PAYMENT_TERMS_LABELS,
        unlinked_names=unlinked_names,
        duplicate_pairs=duplicate_pairs,
        supplier_aliases=supplier_aliases,
        cost_centers=cost_centers,
        can_bulk_edit=can_bulk_edit,
    )


@faturas_bp.route('/fornecedores/quick', methods=['POST'])
@any_perm_required('acesso_financeiro', 'acesso_compras')  # min: acesso_financeiro — quick supplier creation from panel modal
def fornecedores_quick():
    """AJAX endpoint: quick supplier creation from the registar form modal."""
    name = request.form.get('name', '').strip()
    nif = ''.join(c for c in request.form.get('nif', '') if c.isdigit()) or None
    if not name:
        return jsonify({'error': 'Nome do fornecedor é obrigatório.'}), 400
    payment_method = request.form.get('payment_method', '').strip() or None
    payment_terms = request.form.get('payment_terms', '').strip() or None
    iban = request.form.get('iban', '').strip() or None
    try:
        supplier_id = upsert_supplier(
            name=name, nif=nif,
            payment_method=payment_method,
            payment_terms=payment_terms,
            iban=iban,
        )
        return jsonify({
            'id': supplier_id,
            'name': name,
            'nif': nif or '',
            'payment_method': payment_method or '',
            'payment_terms': payment_terms or '',
        })
    except Exception as exc:
        logger.warning('fornecedores_quick error: %s', exc)
        return jsonify({'error': str(exc)}), 500


# ── Excel import ───────────────────────────────────────────────────────────────

def _handle_excel_import(file, ext='xlsx'):
    import openpyxl
    from io import BytesIO

    username = session.get('user', {}).get('username', '')
    file_bytes = file.read()
    wb_closeable = None

    try:
        if ext == 'xls':
            # xlrd (via pandas) supports legacy .xls; convert to uniform row tuples
            import pandas as pd
            df = pd.read_excel(BytesIO(file_bytes), dtype=object, keep_default_na=True)
            raw_headers = [str(c).strip().lower() for c in df.columns]

            def _pd_val(v):
                if v is None:
                    return None
                try:
                    if pd.isna(v):
                        return None
                except (TypeError, ValueError):
                    pass
                return v

            headers = raw_headers
            rows_list = [tuple(_pd_val(v) for v in row) for _, row in df.iterrows()]
            rows_iter = iter(rows_list)
        else:
            wb = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
            wb_closeable = wb
            ws = wb.active
            _rows = ws.iter_rows(values_only=True)
            try:
                header_row = next(_rows)
            except StopIteration:
                wb.close()
                flash('Ficheiro Excel vazio.', 'warning')
                return redirect(url_for('faturas.upload'))
            headers = [str(c or '').strip().lower() for c in header_row]
            rows_iter = _rows
    except Exception as e:
        flash(f'Erro ao ler Excel: {e}', 'warning')
        return redirect(url_for('faturas.upload'))

    # Map Excel column name → internal field key.
    # When the same internal key appears more than once (e.g. empresa + fornecedor),
    # the first column found wins.
    COL_MAP = {
        'fornecedor': 'supplier_name',
        'supplier_name': 'supplier_name',
        'empresa': 'supplier_name',
        'nome comercial': 'supplier_name_alt',   # fallback if supplier_name empty
        'nif': 'supplier_nif',
        'supplier_nif': 'supplier_nif',
        'fatura': 'invoice_number',
        'invoice_number': 'invoice_number',
        'número': 'invoice_number',
        'numero': 'invoice_number',
        'valor': 'amount_eur',
        'valor real': 'amount_eur',
        'amount_eur': 'amount_eur',
        'total': 'amount_eur',
        'iva': 'vat_amount_eur',
        'vat_amount_eur': 'vat_amount_eur',
        'data emissão': 'issue_date',
        'data emissao': 'issue_date',
        'issue_date': 'issue_date',
        'data': 'issue_date',
        'data vencimento': 'due_date',
        'vencimento': 'due_date',
        'due_date': 'due_date',
        'categoria': 'category',
        'category': 'category',
        'subpasta': 'onedrive_subfolder',
        'onedrive_subfolder': 'onedrive_subfolder',
        'estado': 'status',
        'status': 'status',
        'notas': 'notes',
        'notes': 'notes',
        'descrição': 'notes',
        'descricao': 'notes',
        'centro custo': 'centro_custo',
    }

    # Build column-index lookup: internal_key → column index (first match wins)
    col_idx = {}
    for i, h in enumerate(headers):
        key = COL_MAP.get(h)
        if key and key not in col_idx:
            col_idx[key] = i

    # Portuguese → internal status names
    STATUS_PT = {
        'pago': 'paid',
        'pendente': 'pending_review',
        'pendente revisão': 'pending_review',
        'pendente revisao': 'pending_review',
        'agendado': 'scheduled',
        'agendada': 'scheduled',
        'cancelado': 'cancelled',
        'cancelada': 'cancelled',
    }

    def _get(row, key):
        i = col_idx.get(key)
        return row[i] if i is not None and i < len(row) else None

    def _cell_str(v):
        if v is None:
            return ''
        if isinstance(v, float) and v == int(v):
            return str(int(v))
        return str(v).strip()

    def _cell_date(v):
        if v is None:
            return None
        if hasattr(v, 'date'):
            return v.date()
        return _parse_date(_cell_str(v))

    imported = 0
    errors = []
    consecutive_empty = 0

    for idx, row in enumerate(rows_iter, start=2):
        # Early-stop: break after 5 consecutive rows where all mapped columns are empty.
        # A single gap row (all-None) is skipped; 5 in a row means we've reached the
        # end of actual data (avoids iterating up to 1 048 560 trailing empty rows).
        mapped_vals = [row[i] for i in col_idx.values() if i < len(row)]
        if not any(v is not None for v in mapped_vals):
            consecutive_empty += 1
            if consecutive_empty >= 5:
                break
            continue
        consecutive_empty = 0

        try:
            supplier_name = _cell_str(_get(row, 'supplier_name'))
            if not supplier_name:
                supplier_name = _cell_str(_get(row, 'supplier_name_alt'))
            supplier_nif_raw = _cell_str(_get(row, 'supplier_nif'))
            supplier_nif = ''.join(c for c in supplier_nif_raw if c.isdigit())

            if not supplier_name:
                continue

            # Amount: determine document_type before taking abs value
            # Detect nota_credito: invoice_number contains "NC" OR raw amount is positive
            amount_raw = _get(row, 'amount_eur')
            amount_eur_raw = _parse_float(_cell_str(amount_raw))
            invoice_number_raw = _cell_str(_get(row, 'invoice_number'))
            nc_by_number = 'nc' in invoice_number_raw.lower()
            nc_by_amount = (amount_eur_raw is not None and amount_eur_raw > 0)
            document_type = 'nota_credito' if (nc_by_number or nc_by_amount) else 'fatura'
            amount_eur = abs(amount_eur_raw) if amount_eur_raw is not None else None

            vat_amount_eur = _parse_float(_cell_str(_get(row, 'vat_amount_eur')))

            issue_date = _cell_date(_get(row, 'issue_date'))
            due_date = _cell_date(_get(row, 'due_date'))

            # Status: try Portuguese map first, then direct match, else default
            status_raw = _cell_str(_get(row, 'status')).lower()
            status = STATUS_PT.get(status_raw, status_raw)
            if status not in get_invoice_status_labels_map():
                status = 'pending_review'

            # Notes: combine notes column + Centro Custo (if present)
            notes_parts = []
            notes_val = _cell_str(_get(row, 'notes'))
            if notes_val:
                notes_parts.append(notes_val)
            centro = _cell_str(_get(row, 'centro_custo'))
            if centro:
                notes_parts.append(f'Centro Custo: {centro}')
            notes = ' | '.join(notes_parts) or None

            supplier_id = None
            if supplier_nif and supplier_name:
                supplier_id = upsert_supplier(name=supplier_name, nif=supplier_nif)
            elif supplier_name and not supplier_nif:
                # NIF absent — try name-based lookup then upsert without NIF
                try:
                    _sup = get_supplier_by_name(supplier_name)
                    supplier_id = _sup['id'] if _sup else upsert_supplier(name=supplier_name)
                except Exception:
                    pass

            invoice_number = invoice_number_raw or None

            inv_data = {
                'supplier_id': supplier_id,
                'supplier_name': supplier_name or None,
                'supplier_nif': supplier_nif or None,
                'invoice_number': invoice_number,
                'amount_eur': amount_eur,
                'vat_amount_eur': vat_amount_eur,
                'issue_date': issue_date,
                'due_date': due_date,
                'category': _cell_str(_get(row, 'category')) or None,
                'onedrive_subfolder': _cell_str(_get(row, 'onedrive_subfolder')) or None,
                'onedrive_path': None,
                'onedrive_web_url': None,
                'pdf_filename': None,
                'pdf_data': None,
                'status': status,
                'ocr_confidence': None,
                'ocr_raw': None,
                'created_by': username,
                'notes': notes,
                'document_type': document_type,
            }
            create_invoice(inv_data)
            imported += 1
        except Exception as e:
            errors.append(f'Linha {idx}: {e}')

    if wb_closeable is not None:
        wb_closeable.close()

    if errors:
        flash(f'{imported} faturas importadas. Erros: {"; ".join(errors[:5])}', 'warning')
    else:
        flash(f'{imported} faturas importadas com sucesso!', 'success')
    return redirect(url_for('faturas.index'))
