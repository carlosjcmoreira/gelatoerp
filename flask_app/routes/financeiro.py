import sys
import os
from datetime import date, datetime
from flask import Blueprint, render_template, url_for, request, redirect, flash, session
from flask_app.auth import perm_required
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import get_contas_por_fornecedor, mark_payment_executed

financeiro_bp = Blueprint('financeiro', __name__)

FINANCEIRO_GROUPS = [
    {
        'label': 'Gestão de Pagamentos',
        'modules': [
            {'key': 'faturas',    'label': 'Faturas',          'icon': '🧾', 'active': True,  'url': '/financeiro/faturas/'},
            {'key': 'pagamentos', 'label': 'Pagamentos',        'icon': '💸', 'active': True,  'url': '/financeiro/pagamentos/'},
            {'key': 'credito',    'label': 'Crédito',           'icon': '💳', 'active': True,  'url': '/financeiro/credito/'},
            {'key': 'iva',        'label': 'IVA',               'icon': '📋', 'active': True,  'url': '/financeiro/pagamentos/iva'},
            {'key': 'liquidez',   'label': 'Liquidez',          'icon': '📈', 'active': True,  'url': '/financeiro/pagamentos/liquidez'},
        ],
    },
    {
        'label': 'Planeamento & Previsão',
        'modules': [
            {'key': 'cashflow',      'label': 'Cash Flow',           'icon': '📊', 'active': True,  'url_func': 'cashflow.index'},
            {'key': 'salarios',      'label': 'Salários',            'icon': '👥', 'active': True,  'url_func': 'cashflow.salarios'},
            {'key': 'debitos',       'label': 'Débitos Diretos',     'icon': '🔄', 'active': True,  'url_func': 'cashflow.debitos'},
            {'key': 'previsao',      'label': 'Previsão de Vendas',  'icon': '🔮', 'active': True,  'url_func': 'forecast.index'},
            {'key': 'modelo',        'label': 'Modelo de Previsão',  'icon': '📉', 'active': True,  'url_func': 'forecast.modelo'},
            {'key': 'meteorologia',  'label': 'Meteorologia',        'icon': '🌤️', 'active': True,  'url_func': 'meteorologia.index'},
        ],
    },
]

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
    groups = []
    for g in FINANCEIRO_GROUPS:
        resolved = []
        for m in g['modules']:
            entry = dict(m)
            if entry['active'] and entry.get('url_func'):
                entry['url'] = url_for(entry['url_func'])
            elif not entry.get('url'):
                entry['url'] = None
            resolved.append(entry)
        groups.append({'label': g['label'], 'modules': resolved})
    return render_template('financeiro/index.html', groups=groups)


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
