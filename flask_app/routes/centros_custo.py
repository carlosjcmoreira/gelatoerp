import sys, os
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
