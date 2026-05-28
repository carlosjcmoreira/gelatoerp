import sys, os
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_app.auth import perm_required
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_cost_centers, get_cost_center, create_cost_center,
    update_cost_center, toggle_cost_center,
)

centros_custo_bp = Blueprint('centros_custo', __name__)


@centros_custo_bp.route('/', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def index():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'create':
            code = request.form.get('code', '').strip().upper()
            name = request.form.get('name', '').strip()
            desc = request.form.get('description', '').strip() or None
            if not code or not name:
                flash('Código e nome são obrigatórios.', 'warning')
            else:
                try:
                    create_cost_center(code, name, desc)
                    flash(f'Centro de custo "{code} — {name}" criado.', 'success')
                except Exception as exc:
                    flash(f'Erro ao criar centro de custo: {exc}', 'danger')
        elif action == 'edit':
            cc_id = int(request.form.get('cc_id', 0))
            code = request.form.get('code', '').strip().upper()
            name = request.form.get('name', '').strip()
            desc = request.form.get('description', '').strip() or None
            if not code or not name:
                flash('Código e nome são obrigatórios.', 'warning')
            else:
                try:
                    update_cost_center(cc_id, code, name, desc)
                    flash('Centro de custo actualizado.', 'success')
                except Exception as exc:
                    flash(f'Erro ao actualizar: {exc}', 'danger')
        elif action == 'toggle':
            cc_id = int(request.form.get('cc_id', 0))
            ativo = request.form.get('ativo', '0') == '1'
            try:
                toggle_cost_center(cc_id, ativo)
                estado = 'activado' if ativo else 'desactivado'
                flash(f'Centro de custo {estado}.', 'success')
            except Exception as exc:
                flash(f'Erro: {exc}', 'danger')
        return redirect(url_for('centros_custo.index'))

    centros = get_cost_centers()
    return render_template('financeiro/centros_custo/index.html', centros=centros)


@centros_custo_bp.route('/relatorio')
@perm_required('acesso_gestor')
def relatorio():
    """Expense report grouped by cost center."""
    from db.centros_custo import get_despesas_por_centro_custo
    from db.faturas import INVOICE_STATUS_LABELS

    today = date.today()
    date_from_raw = request.args.get('date_from', '').strip()
    date_to_raw = request.args.get('date_to', '').strip()
    status_filter = [s for s in request.args.getlist('status') if s]

    def _parse(val):
        if not val:
            return None
        for fmt in ('%Y-%m-%d', '%d/%m/%Y'):
            try:
                from datetime import datetime
                return datetime.strptime(val, fmt).date()
            except ValueError:
                continue
        return None

    date_from = _parse(date_from_raw)
    date_to = _parse(date_to_raw)
    statuses = status_filter if status_filter else ['pending_review', 'scheduled', 'paid']

    grupos = get_despesas_por_centro_custo(
        date_from=date_from,
        date_to=date_to,
        statuses=statuses,
    )
    total_geral = sum(g['total_eur'] for g in grupos)

    return render_template(
        'financeiro/centros_custo/relatorio.html',
        grupos=grupos,
        total_geral=total_geral,
        today=today,
        date_from=date_from,
        date_to=date_to,
        date_from_raw=date_from_raw,
        date_to_raw=date_to_raw,
        statuses=statuses,
        status_labels=INVOICE_STATUS_LABELS,
    )
