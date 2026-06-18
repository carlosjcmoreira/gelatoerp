import logging
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_invoices_with_payments,
    get_suppliers,
    create_invoice,
    propose_invoice_payment,
    suggest_payment_date,
    get_vat_periods, get_vat_period, upsert_vat_period, compute_vat_period,
    VAT_RATES,
    get_weekly_liquidity,
    get_all_stores,
    get_cost_centers,
    get_cost_categories_tree,
)
from db.faturas import ONEDRIVE_SUBFOLDERS, update_invoice_onedrive
from db.credito import get_payment_methods_config
from db.cashflow import get_saldo_inicial_tesouraria, set_saldo_inicial_tesouraria
from db.pagamentos import get_overdue_unscheduled_invoices, set_tesouraria_manual

logger = logging.getLogger(__name__)

pagamentos_bp = Blueprint('pagamentos', __name__)

INVOICE_STATUSES = [
    ('pending_review', 'Por Confirmar', 'warning'),
    ('scheduled', 'Agendada', 'info'),
    ('paid', 'Paga', 'success'),
    ('overdue', 'Em Atraso', 'danger'),
    ('cancelled', 'Cancelada', 'secondary'),
]

STATUS_MAP = {s[0]: (s[1], s[2]) for s in INVOICE_STATUSES}

VAT_STATUS_MAP = {
    'estimated': ('Estimado', 'secondary'),
    'declared': ('Declarado', 'info'),
    'paid': ('Pago', 'success'),
}

MESES_PT = ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
             'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']


def _get_username():
    return session.get('user', {}).get('username', 'sistema')


@pagamentos_bp.route('/')
@perm_required('acesso_gestor')
def index():
    invoices_pending = [dict(r) for r in get_invoices_with_payments(status='pending_review')]
    invoices_scheduled = [dict(r) for r in get_invoices_with_payments(status='scheduled')]
    total_pending = sum(float(i.get('amount_eur') or 0) for i in invoices_pending)
    total_scheduled = sum(float(i.get('amount_eur') or 0) for i in invoices_scheduled)

    vat_periods = list(get_vat_periods(limit=3))
    next_vat = None
    for p in reversed(vat_periods):
        if p['status'] in ('estimated', 'declared'):
            next_vat = p
            break

    weekly = get_weekly_liquidity(weeks=4)

    return render_template('pagamentos/index.html',
                           invoices_pending=invoices_pending,
                           invoices_scheduled=invoices_scheduled,
                           total_pending=total_pending,
                           total_scheduled=total_scheduled,
                           next_vat=next_vat,
                           weekly=weekly,
                           status_map=STATUS_MAP,
                           meses=MESES_PT,
                           today=date.today())


@pagamentos_bp.route('/faturas')
@perm_required('acesso_gestor')
def faturas():
    """Backward-compat redirect — canonical list is at /financeiro/faturas/."""
    status = request.args.get('status', '')
    target = url_for('faturas.index')
    if status:
        target += f'?status={status}'
    return redirect(target, code=302)


@pagamentos_bp.route('/faturas/nova', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def nova_fatura():
    """Backward-compat redirect — canonical form is now at /financeiro/faturas/registar."""
    if request.method == 'GET':
        return redirect(url_for('faturas.registar'), code=302)
    if request.method == 'POST':
        supplier_name = request.form.get('supplier_name', '').strip()
        supplier_nif = request.form.get('supplier_nif', '').strip() or None
        invoice_number = request.form.get('invoice_number', '').strip() or None
        amount_str = request.form.get('amount_eur', '').replace(',', '.')
        vat_str = request.form.get('vat_amount_eur', '').replace(',', '.') or '0'
        issue_date_str = request.form.get('issue_date', '')
        due_date_str = request.form.get('due_date', '')
        store_id = request.form.get('store_id') or None
        categoria = request.form.get('categoria', '').strip() or None
        notes_raw = request.form.get('notes', '').strip() or ''
        payment_method = request.form.get('payment_method', '').strip() or None
        document_type = request.form.get('document_type', 'fatura')
        from db.faturas import DOCUMENT_TYPE_LABELS as _DTL
        if document_type not in _DTL:
            document_type = 'fatura'
        onedrive_subfolder = request.form.get('onedrive_subfolder', '').strip() or None
        centro_custo_raw = request.form.get('centro_custo_id', '').strip()
        centro_custo_id = int(centro_custo_raw) if centro_custo_raw else None
        categoria_custo_raw = request.form.get('categoria_custo_id', '').strip()
        categoria_custo_id = int(categoria_custo_raw) if categoria_custo_raw else None

        if not supplier_name and document_type == 'fatura':
            flash('Nome do fornecedor é obrigatório para faturas.', 'warning')
            return redirect(url_for('pagamentos.nova_fatura'))
        try:
            amount_eur = float(amount_str)
        except (ValueError, TypeError):
            flash('Valor inválido.', 'warning')
            return redirect(url_for('pagamentos.nova_fatura'))

        try:
            vat_amount_eur = float(vat_str)
        except (ValueError, TypeError):
            vat_amount_eur = 0.0

        issue_date = None
        due_date = None
        try:
            if issue_date_str:
                issue_date = datetime.strptime(issue_date_str, '%Y-%m-%d').date()
            if due_date_str:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
        except ValueError:
            pass

        _ALLOWED_MANUAL_EXTS = {'pdf', 'jpg', 'jpeg', 'png', 'heic', 'heif', 'webp'}
        pdf_file = request.files.get('pdf_file')
        pdf_data = None
        pdf_filename = None
        if pdf_file and pdf_file.filename:
            ext = pdf_file.filename.rsplit('.', 1)[-1].lower() if '.' in pdf_file.filename else ''
            if ext not in _ALLOWED_MANUAL_EXTS:
                flash(f'Tipo de ficheiro não suportado (.{ext}). Usa PDF ou imagem.', 'warning')
                return redirect(url_for('pagamentos.nova_fatura'))
            pdf_data = pdf_file.read()
            pdf_filename = pdf_file.filename

        notes_parts = []
        if payment_method:
            notes_parts.append(f'Método: {payment_method}')
        if notes_raw:
            notes_parts.append(notes_raw)
        notes = ' | '.join(notes_parts) or None

        store_id_int = int(store_id) if store_id else None
        invoice_id = create_invoice({
            'supplier_id': None,
            'supplier_name': supplier_name,
            'supplier_nif': supplier_nif,
            'invoice_number': invoice_number,
            'amount_eur': amount_eur,
            'vat_amount_eur': vat_amount_eur,
            'issue_date': issue_date,
            'due_date': due_date,
            'store_id': store_id_int,
            'category': categoria,
            'onedrive_subfolder': onedrive_subfolder,
            'onedrive_path': None,
            'onedrive_web_url': None,
            'pdf_filename': pdf_filename,
            'pdf_data': pdf_data,
            'status': 'pending_review',
            'ocr_confidence': None,
            'ocr_raw': None,
            'created_by': _get_username(),
            'notes': notes,
            'document_type': document_type,
            'centro_custo_id': centro_custo_id,
            'categoria_custo_id': categoria_custo_id,
        })

        # Archive to OneDrive if a file and subfolder were provided
        if pdf_data and onedrive_subfolder:
            try:
                from flask_app.onedrive_archive import upload_invoice_pdf
                result = upload_invoice_pdf(pdf_data, pdf_filename or 'fatura.pdf',
                                            onedrive_subfolder, issue_date)
                if result.get('onedrive_path'):
                    update_invoice_onedrive(invoice_id, result['onedrive_path'],
                                            onedrive_subfolder,
                                            onedrive_web_url=result.get('web_url'))
                elif result.get('warning'):
                    flash(f'Documento registado. Aviso OneDrive: {result["warning"]}', 'warning')
            except Exception as exc:
                logger.warning('OneDrive upload failed for manual invoice %s: %s', invoice_id, exc)
                flash(f'Documento registado. Erro ao arquivar no OneDrive: {exc}', 'warning')

        # Auto-propose a liquidity-aware payment date
        from db.faturas import DOCUMENT_TYPE_LABELS as _DTL2
        _doc_label = _DTL2.get(document_type, 'Documento')
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

    stores = get_all_stores()
    suppliers = get_suppliers()
    cost_centers = get_cost_centers(ativo_only=True)
    cost_categories_tree = get_cost_categories_tree()
    payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]
    return render_template('pagamentos/nova_fatura.html',
                           stores=stores, suppliers=suppliers, today=str(date.today()),
                           subfolders=ONEDRIVE_SUBFOLDERS,
                           cost_centers=cost_centers,
                           cost_categories_tree=cost_categories_tree,
                           payment_methods=payment_methods)


@pagamentos_bp.route('/faturas/<int:invoice_id>', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def detalhe_fatura(invoice_id):
    """Backward-compat redirect — canonical detail is at /financeiro/faturas/<id>."""
    return redirect(url_for('faturas.detail', invoice_id=invoice_id), code=302)


@pagamentos_bp.route('/iva')
@perm_required('acesso_gestor')
def iva():
    today = date.today()
    periods = get_vat_periods(limit=24)

    warning_days = 15
    alerts = []
    for p in periods:
        if p['status'] in ('estimated', 'declared') and p['declaration_date']:
            days_left = (p['declaration_date'] - today).days
            if 0 <= days_left <= warning_days:
                alerts.append({
                    'type': 'declaration',
                    'period': p,
                    'days_left': days_left,
                    'label': f"Declaração de IVA {MESES_PT[p['month']]} {p['year']} em {days_left} dia(s)",
                })
        if p['status'] == 'declared' and p['payment_date']:
            days_left = (p['payment_date'] - today).days
            if 0 <= days_left <= warning_days:
                alerts.append({
                    'type': 'payment',
                    'period': p,
                    'days_left': days_left,
                    'label': f"Pagamento de IVA {MESES_PT[p['month']]} {p['year']} em {days_left} dia(s)",
                })

    current_month = today.month
    current_year = today.year
    prev_month = current_month - 1 if current_month > 1 else 12
    prev_year = current_year if current_month > 1 else current_year - 1

    existing = get_vat_period(prev_year, prev_month)
    if not existing:
        computed = compute_vat_period(prev_year, prev_month)
        upsert_vat_period(
            prev_year, prev_month,
            vat_collected_estimated=computed['vat_collected_estimated'],
            vat_deductible_estimated=computed['vat_deductible_estimated'],
            vat_due_estimated=computed['vat_due_estimated'],
        )
        periods = get_vat_periods(limit=24)

    return render_template('pagamentos/iva.html',
                           periods=periods,
                           alerts=alerts,
                           status_map=VAT_STATUS_MAP,
                           meses=MESES_PT,
                           vat_rates=VAT_RATES,
                           today=today)


@pagamentos_bp.route('/iva/<int:year>/<int:month>', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def iva_periodo(year, month):
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'recalculate':
            rate_pos_str = request.form.get('rate_pos', '').replace(',', '.')
            rate_events_str = request.form.get('rate_events', '').replace(',', '.')
            try:
                rate_pos = float(rate_pos_str) / 100 if rate_pos_str else None
                rate_events = float(rate_events_str) / 100 if rate_events_str else None
            except (ValueError, TypeError):
                rate_pos = rate_events = None
            computed = compute_vat_period(year, month, rate_pos=rate_pos, rate_events=rate_events)
            upsert_vat_period(year, month,
                              vat_collected_estimated=computed['vat_collected_estimated'],
                              vat_deductible_estimated=computed['vat_deductible_estimated'],
                              vat_due_estimated=computed['vat_due_estimated'])
            flash('IVA estimado recalculado.', 'success')

        elif action == 'declare':
            decl_date_str = request.form.get('declaration_submitted_at', str(date.today()))
            try:
                decl_date = datetime.strptime(decl_date_str, '%Y-%m-%d').date()
            except ValueError:
                decl_date = date.today()
            vat_collected_str = request.form.get('vat_collected_eur', '').replace(',', '.')
            vat_deductible_str = request.form.get('vat_deductible_eur', '').replace(',', '.')
            try:
                vat_collected = float(vat_collected_str)
                vat_deductible = float(vat_deductible_str)
                vat_due = max(0, vat_collected - vat_deductible)
            except (ValueError, TypeError):
                flash('Valores inválidos.', 'warning')
                return redirect(url_for('pagamentos.iva_periodo', year=year, month=month))
            upsert_vat_period(year, month,
                              vat_collected_eur=vat_collected,
                              vat_deductible_eur=vat_deductible,
                              vat_due_eur=vat_due,
                              status='declared',
                              declaration_submitted_at=decl_date)
            flash(f'Declaração de IVA {MESES_PT[month]} {year} registada.', 'success')

        elif action == 'pay':
            pay_date_str = request.form.get('payment_executed_at', str(date.today()))
            try:
                pay_date = datetime.strptime(pay_date_str, '%Y-%m-%d').date()
            except ValueError:
                pay_date = date.today()
            upsert_vat_period(year, month, status='paid', payment_executed_at=pay_date)
            flash(f'Pagamento de IVA {MESES_PT[month]} {year} registado.', 'success')

        return redirect(url_for('pagamentos.iva_periodo', year=year, month=month))

    period = get_vat_period(year, month)
    if not period:
        computed = compute_vat_period(year, month)
        period = upsert_vat_period(year, month,
                                   vat_collected_estimated=computed['vat_collected_estimated'],
                                   vat_deductible_estimated=computed['vat_deductible_estimated'],
                                   vat_due_estimated=computed['vat_due_estimated'])

    today = date.today()
    return render_template('pagamentos/iva_periodo.html',
                           period=period, year=year, month=month,
                           mes_nome=MESES_PT[month],
                           status_map=VAT_STATUS_MAP,
                           vat_rates=VAT_RATES,
                           today=str(today))


@pagamentos_bp.route('/liquidez')
@perm_required('acesso_gestor')
def liquidez():
    saldo_inicial = get_saldo_inicial_tesouraria()
    overdue_info = get_overdue_unscheduled_invoices()

    weekly = get_weekly_liquidity(weeks=6)
    running_balance = saldo_inicial
    for w in weekly:
        running_balance += w['balance']
        w['running_balance'] = round(running_balance, 2)

    all_cats: dict = {}
    for w in weekly:
        for cat in w['outflows_by_category']:
            cid = cat['category_id']
            if cid not in all_cats or cat['amount'] > 0:
                all_cats[cid] = cat['category_name']

    categories = sorted(
        all_cats.items(),
        key=lambda x: (x[0] is None, (x[1] or '').lower()),
    )
    categories = [(cid, cname) for cid, cname in categories
                  if any(
                      any(c['category_id'] == cid and c['amount'] > 0
                          for c in w['outflows_by_category'])
                      for w in weekly
                  )]

    items_by_cat: dict = {}
    items_meta_by_cat: dict = {}
    for w in weekly:
        for cat in w['outflows_by_category']:
            cid = cat['category_id']
            if cid not in items_by_cat:
                items_by_cat[cid] = {}
                items_meta_by_cat[cid] = {}
            for item in cat['items']:
                ref = item['reference']
                key = f"{item['description']} ({ref})" if ref else item['description']
                if key not in items_by_cat[cid]:
                    items_by_cat[cid][key] = {}
                items_by_cat[cid][key][w['week']] = item['amount']
                if key not in items_meta_by_cat[cid]:
                    items_meta_by_cat[cid][key] = {
                        'source': item.get('source', 'scheduled'),
                        'invoice_id': item.get('invoice_id'),
                        'type': item.get('type', 'fatura'),
                    }

    vat_items_by_week: dict = {}
    for w in weekly:
        for item in w['outflows_vat_items']:
            key = item['description']
            if key not in vat_items_by_week:
                vat_items_by_week[key] = {}
            vat_items_by_week[key][w['week']] = item['amount']

    cat_totals: dict = {}
    for cid, _ in categories:
        cat_totals[cid] = sum(
            next((c['amount'] for c in w['outflows_by_category'] if c['category_id'] == cid), 0.0)
            for w in weekly
        )

    vat_total = sum(w['outflows_vat'] for w in weekly)
    total_out_all = sum(w['total_out'] for w in weekly)
    total_in_all = sum(w['total_in'] for w in weekly)
    pos_total = sum(w['inflows_pos'] for w in weekly)
    events_total = sum(w['inflows_events'] for w in weekly)
    eventos_manual_total = round(sum(w['inflows_eventos_manual'] for w in weekly), 2)
    b2b_manual_total = round(sum(w['inflows_b2b_manual'] for w in weekly), 2)

    pos_daily_totals = [0.0] * 7
    for w in weekly:
        for i, day in enumerate(w.get('pos_daily', [])):
            if i < 7:
                pos_daily_totals[i] += day['amount']
    pos_daily_totals = [round(v, 2) for v in pos_daily_totals]
    pos_dow_labels = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']

    item_totals_by_cat: dict = {}
    for cid, items in items_by_cat.items():
        item_totals_by_cat[cid] = {k: sum(v.values()) for k, v in items.items()}

    vat_item_totals = {k: sum(v.values()) for k, v in vat_items_by_week.items()}

    cost_categories_tree = get_cost_categories_tree()

    return render_template(
        'pagamentos/liquidez.html',
        weekly=weekly,
        saldo_inicial=saldo_inicial,
        overdue_info=overdue_info,
        categories=categories,
        items_by_cat=items_by_cat,
        items_meta_by_cat=items_meta_by_cat,
        vat_items_by_week=vat_items_by_week,
        cat_totals=cat_totals,
        item_totals_by_cat=item_totals_by_cat,
        vat_item_totals=vat_item_totals,
        vat_total=round(vat_total, 2),
        total_out_all=round(total_out_all, 2),
        total_in_all=round(total_in_all, 2),
        pos_total=round(pos_total, 2),
        events_total=round(events_total, 2),
        eventos_manual_total=eventos_manual_total,
        b2b_manual_total=b2b_manual_total,
        pos_daily_totals=pos_daily_totals,
        pos_dow_labels=pos_dow_labels,
        cost_categories_tree=cost_categories_tree,
    )


@pagamentos_bp.route('/liquidez/saldo-inicial', methods=['POST'])
@perm_required('acesso_gestor')
def set_saldo_inicial():
    try:
        valor = float(request.form.get('valor', 0) or 0)
    except (ValueError, TypeError):
        valor = 0.0
    set_saldo_inicial_tesouraria(valor)
    return redirect(url_for('pagamentos.liquidez'))


@pagamentos_bp.route('/liquidez/manual', methods=['POST'])
@perm_required('acesso_gestor')
def save_manual_entry():
    data = request.get_json(silent=True) or {}
    tipo = (data.get('tipo') or '').strip().lower()
    if tipo not in ('eventos', 'b2b'):
        return jsonify({'ok': False, 'error': 'tipo inválido'}), 400
    try:
        semana_str = data.get('semana_inicio', '')
        semana_inicio = date.fromisoformat(semana_str)
        valor = float(data.get('valor', 0) or 0)
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    set_tesouraria_manual(tipo, semana_inicio, valor)
    return jsonify({'ok': True, 'valor': round(valor, 2)})
