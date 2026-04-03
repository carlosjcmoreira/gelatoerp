import sys, os
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_app.auth import perm_required
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_cost_categories, get_cost_categories_tree,
    create_cost_category, update_cost_category, toggle_cost_category,
)

categorias_custo_bp = Blueprint('categorias_custo', __name__)


@categorias_custo_bp.route('/', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def index():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'create':
            name = request.form.get('name', '').strip()
            parent_id_raw = request.form.get('parent_id', '').strip()
            parent_id = int(parent_id_raw) if parent_id_raw else None
            if not name:
                flash('Nome da categoria é obrigatório.', 'warning')
            else:
                try:
                    create_cost_category(name, parent_id)
                    flash(f'Categoria "{name}" criada.', 'success')
                except Exception as exc:
                    flash(f'Erro ao criar categoria: {exc}', 'danger')
        elif action == 'edit':
            cat_id = int(request.form.get('cat_id', 0))
            name = request.form.get('name', '').strip()
            parent_id_raw = request.form.get('parent_id', '').strip()
            parent_id = int(parent_id_raw) if parent_id_raw else None
            if not name:
                flash('Nome é obrigatório.', 'warning')
            else:
                try:
                    update_cost_category(cat_id, name, parent_id)
                    flash('Categoria actualizada.', 'success')
                except Exception as exc:
                    flash(f'Erro ao actualizar: {exc}', 'danger')
        elif action == 'toggle':
            cat_id = int(request.form.get('cat_id', 0))
            ativo = request.form.get('ativo', '0') == '1'
            try:
                toggle_cost_category(cat_id, ativo)
                estado = 'activada' if ativo else 'desactivada'
                flash(f'Categoria {estado}.', 'success')
            except Exception as exc:
                flash(f'Erro: {exc}', 'danger')
        return redirect(url_for('categorias_custo.index'))

    tree = get_cost_categories_tree(ativo_only=False)
    flat = get_cost_categories()
    top_level = [c for c in flat if c['parent_id'] is None and c['ativo']]
    return render_template('financeiro/categorias_custo/index.html',
                           tree=tree, top_level=top_level)
