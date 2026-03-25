import sys
import os
from datetime import date, datetime
from flask import Blueprint, render_template, url_for, request, redirect, flash, session
from flask_app.auth import perm_required
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import get_contas_por_fornecedor, mark_payment_executed

financeiro_bp = Blueprint('financeiro', __name__)

FINANCEIRO_MODULES = {
    'faturas':    {'label': 'Faturas',              'icon': '🧾', 'active': True,  'url_func': 'faturas.index'},
    'fornecedores': {'label': 'Fornecedores',       'icon': '🏢', 'active': True,  'url_func': 'faturas.fornecedores'},
    'credito':    {'label': 'Crédito',              'icon': '💳', 'active': True,  'url': '/financeiro/credito/'},
    'pagamentos': {'label': 'Pagamentos',           'icon': '💸', 'active': True,  'url': '/financeiro/pagamentos/'},
    'iva':        {'label': 'IVA',                  'icon': '📋', 'active': True,  'url': '/financeiro/pagamentos/iva'},
    'liquidez':   {'label': 'Liquidez',             'icon': '📈', 'active': True,  'url': '/financeiro/pagamentos/liquidez'},
    'cashflow':      {'label': 'Cash Flow',           'icon': '📊', 'active': True,  'url': '/financeiro/cashflow/'},
    'salarios':      {'label': 'Salários',            'icon': '👥', 'active': True,  'url_func': 'cashflow.salarios'},
    'debitos':       {'label': 'Débitos Diretos',     'icon': '🔄', 'active': True,  'url_func': 'cashflow.debitos'},
    'meteorologia':  {'label': 'Meteorologia',        'icon': '🌤️', 'active': True,  'url_func': 'meteorologia.index'},
    'previsao':      {'label': 'Previsão de Vendas',  'icon': '🔮', 'active': True,  'url_func': 'forecast.index'},
    'modelo':        {'label': 'Modelo de Previsão', 'icon': '📊', 'active': True,  'url_func': 'forecast.modelo'},
}

PAYMENT_METHODS = [
    ('transferencia', 'Transferência Bancária'),
    ('cheque', 'Cheque'),
    ('debito_direto', 'Débito Direto'),
    ('numerario', 'Numerário'),
    ('outro', 'Outro'),
]


def _get_username():
    return session.get('user', {}).get('username', 'sistema')


@financeiro_bp.route('/')
@perm_required('acesso_gestor')
def index():
    modules = []
    for m in FINANCEIRO_MODULES.values():
        entry = dict(m)
        if entry['active'] and entry.get('url_func'):
            entry['url'] = url_for(entry['url_func'])
        elif not entry.get('url'):
            entry['url'] = None
        modules.append(entry)
    return render_template('financeiro/index.html', modules=modules)


@financeiro_bp.route('/contas-fornecedor')
def contas_fornecedor():
    return redirect(url_for('faturas.index', view='fornecedor'))


@financeiro_bp.route('/contas-fornecedor/liquidar', methods=['POST'])
@perm_required('acesso_gestor')
def liquidar_fornecedor():
    ids = request.form.getlist('invoice_ids', type=int)
    paid_date_str = request.form.get('paid_date', str(date.today()))
    payment_method = request.form.get('payment_method', 'transferencia')
    try:
        paid_date = datetime.strptime(paid_date_str, '%Y-%m-%d').date()
    except ValueError:
        paid_date = date.today()

    if not ids:
        flash('Nenhum documento selecionado.', 'warning')
        return redirect(url_for('faturas.index', view='fornecedor'))

    username = _get_username()
    method_label = dict(PAYMENT_METHODS).get(payment_method, payment_method)
    payment_note = f'Liquidação em lote via {method_label} ({paid_date.strftime("%d/%m/%Y")})'
    ok = 0
    fail = 0
    for inv_id in ids:
        try:
            mark_payment_executed(inv_id, paid_date, username, notes=payment_note)
            ok += 1
        except Exception:
            fail += 1

    if fail:
        flash(f'{ok} documento(s) liquidado(s). {fail} falharam.', 'warning')
    else:
        flash(f'{ok} documento(s) liquidado(s) via {method_label} em {paid_date.strftime("%d/%m/%Y")}.', 'success')

    return redirect(url_for('faturas.index', view='fornecedor'))
