import logging
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_app.auth import perm_required
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_invoices, get_invoice, create_invoice, update_invoice, delete_invoice,
    get_suppliers,
    get_invoices_with_payments,
    propose_invoice_payment, confirm_invoice_payment, mark_payment_executed,
    suggest_payment_date,
    get_invoice_payments,
    get_vat_periods, get_vat_period, upsert_vat_period, compute_vat_period,
    VAT_RATES,
    get_weekly_liquidity,
    get_all_stores,
)

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
    status_filter = request.args.get('status', '')
    invoices = [dict(r) for r in get_invoices_with_payments(status=status_filter or None)]
    today = date.today()
    # Overdue is a virtual status: scheduled invoices whose due_date is in the past.
    # Applied as display-only — DB status is NOT mutated, keeping filter semantics consistent.
    for inv in invoices:
        due = inv.get('due_date')
        if due and inv.get('status') == 'scheduled' and due < today:
            inv['_display_status'] = 'overdue'
        else:
            inv['_display_status'] = inv.get('status', '')
    stores = get_all_stores()
    suppliers = get_suppliers()
    return render_template('pagamentos/faturas.html',
                           invoices=invoices,
                           status_filter=status_filter,
                           status_map=STATUS_MAP,
                           statuses=INVOICE_STATUSES,
                           stores=stores,
                           suppliers=suppliers,
                           today=today)


@pagamentos_bp.route('/faturas/nova', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def nova_fatura():
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
        notes = request.form.get('notes', '').strip() or None
        document_type = request.form.get('document_type', 'fatura')
        if document_type not in ('fatura', 'nota_credito'):
            document_type = 'fatura'

        if not supplier_name:
            flash('Nome do fornecedor é obrigatório.', 'warning')
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
            'onedrive_subfolder': None,
            'onedrive_path': None,
            'onedrive_web_url': None,
            'pdf_filename': None,
            'pdf_data': None,
            'status': 'pending_review',
            'ocr_confidence': None,
            'ocr_raw': None,
            'created_by': _get_username(),
            'notes': notes,
            'document_type': document_type,
        })

        # Auto-propose a liquidity-aware payment date
        if due_date:
            suggested_date, is_fallback, _ = suggest_payment_date(
                invoice_id, amount_eur, due_date=due_date
            )
            propose_invoice_payment(invoice_id, suggested_date, amount_eur)
            if is_fallback:
                flash(f'Fatura registada. Data de pagamento sugerida = vencimento ({due_date.strftime("%d/%m/%Y")}) — sem semana com liquidez suficiente nas próximas 8 semanas.', 'warning')
            else:
                flash(f'Fatura de {supplier_name} registada. Data de pagamento proposta: {suggested_date.strftime("%d/%m/%Y")}.', 'success')
        else:
            flash(f'Fatura de {supplier_name} registada!', 'success')

        return redirect(url_for('pagamentos.faturas'))

    stores = get_all_stores()
    suppliers = get_suppliers()
    return render_template('pagamentos/nova_fatura.html',
                           stores=stores, suppliers=suppliers, today=str(date.today()))


@pagamentos_bp.route('/faturas/<int:invoice_id>', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def detalhe_fatura(invoice_id):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('pagamentos.faturas'))

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'edit':
            supplier_name = request.form.get('supplier_name', '').strip()
            supplier_nif = request.form.get('supplier_nif', '').strip() or None
            invoice_number = request.form.get('invoice_number', '').strip() or None
            amount_str = request.form.get('amount_eur', '').replace(',', '.')
            vat_str = request.form.get('vat_amount_eur', '').replace(',', '.') or '0'
            issue_date_str = request.form.get('issue_date', '')
            due_date_str = request.form.get('due_date', '')
            store_id = request.form.get('store_id') or None
            categoria = request.form.get('categoria', '').strip() or None
            notes = request.form.get('notes', '').strip() or None
            try:
                amount_eur = float(amount_str)
                vat_amount_eur = float(vat_str)
            except (ValueError, TypeError):
                flash('Valor inválido.', 'warning')
                return redirect(url_for('pagamentos.detalhe_fatura', invoice_id=invoice_id))
            issue_date = None
            due_date = None
            try:
                if issue_date_str:
                    issue_date = datetime.strptime(issue_date_str, '%Y-%m-%d').date()
                if due_date_str:
                    due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass
            update_invoice(invoice_id, {
                'supplier_name': supplier_name,
                'supplier_nif': supplier_nif,
                'invoice_number': invoice_number,
                'amount_eur': amount_eur,
                'vat_amount_eur': vat_amount_eur,
                'issue_date': issue_date,
                'due_date': due_date,
                'store_id': int(store_id) if store_id else None,
                'category': categoria,
                'notes': notes,
            })
            flash('Fatura actualizada.', 'success')

        elif action == 'propose_payment':
            proposed_date_str = request.form.get('proposed_date', '')
            amount_str = request.form.get('payment_amount', '').replace(',', '.')
            try:
                proposed_date = datetime.strptime(proposed_date_str, '%Y-%m-%d').date()
                amount_eur = float(amount_str) if amount_str else float(inv.get('amount_eur') or 0)
            except (ValueError, TypeError):
                flash('Data ou valor inválido.', 'warning')
                return redirect(url_for('pagamentos.detalhe_fatura', invoice_id=invoice_id))
            propose_invoice_payment(invoice_id, proposed_date, amount_eur)
            flash('Proposta de pagamento registada.', 'success')

        elif action == 'suggest_date':
            amount_eur = float(inv.get('amount_eur') or 0)
            due_date = inv.get('due_date')
            suggested_date, is_fallback, _ = suggest_payment_date(
                invoice_id, amount_eur, due_date=due_date
            )
            propose_invoice_payment(invoice_id, suggested_date, amount_eur)
            if is_fallback:
                flash(f'Sem semana com liquidez suficiente — data proposta = vencimento ({suggested_date.strftime("%d/%m/%Y")}).', 'warning')
            else:
                flash(f'Data sugerida pela liquidez projectada: {suggested_date.strftime("%d/%m/%Y")}.', 'success')

        elif action == 'confirm_payment':
            confirmed_date_str = request.form.get('confirmed_date', '')
            amount_str = request.form.get('payment_amount', '').replace(',', '.')
            notes = request.form.get('payment_notes', '').strip() or None
            try:
                confirmed_date = datetime.strptime(confirmed_date_str, '%Y-%m-%d').date()
                amount_eur = float(amount_str) if amount_str else float(inv['amount_eur'])
            except (ValueError, TypeError):
                flash('Data ou valor inválido.', 'warning')
                return redirect(url_for('pagamentos.detalhe_fatura', invoice_id=invoice_id))
            confirm_invoice_payment(invoice_id, confirmed_date, amount_eur, _get_username(), notes)
            flash('Data de pagamento confirmada.', 'success')

        elif action == 'mark_paid':
            paid_date_str = request.form.get('paid_date', str(date.today()))
            try:
                paid_date = datetime.strptime(paid_date_str, '%Y-%m-%d').date()
            except ValueError:
                paid_date = date.today()
            mark_payment_executed(invoice_id, paid_date, _get_username())
            flash('Fatura marcada como paga.', 'success')

        elif action == 'cancel':
            update_invoice(invoice_id, {'status': 'cancelled'})
            flash('Fatura cancelada.', 'success')
            return redirect(url_for('pagamentos.faturas'))

        elif action == 'delete':
            delete_invoice(invoice_id)
            flash('Fatura eliminada.', 'success')
            return redirect(url_for('pagamentos.faturas'))

        return redirect(url_for('pagamentos.detalhe_fatura', invoice_id=invoice_id))

    # Fetch payment scheduling info separately (get_invoice doesn't join invoice_payments)
    payment_list = get_invoice_payments()
    payment_info = next((p for p in payment_list if p['invoice_id'] == invoice_id), None)

    # Compute liquidity suggestion for display
    amount_eur = float(inv.get('amount_eur') or 0)
    due_date = inv.get('due_date')
    suggested_date, is_fallback, weekly = suggest_payment_date(
        invoice_id, amount_eur, due_date=due_date
    )

    stores = get_all_stores()
    return render_template('pagamentos/detalhe_fatura.html',
                           inv=inv, payment_info=payment_info,
                           suggested_date=suggested_date,
                           is_fallback=is_fallback,
                           weekly=weekly,
                           stores=stores, status_map=STATUS_MAP,
                           today=str(date.today()))


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
    weekly = get_weekly_liquidity(weeks=8)
    running_balance = 0
    for w in weekly:
        running_balance += w['balance']
        w['running_balance'] = round(running_balance, 2)
    return render_template('pagamentos/liquidez.html', weekly=weekly)
