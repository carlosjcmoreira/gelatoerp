from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_app.auth import perm_required
from datetime import date, datetime
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_artigos_administrativos, add_artigo_administrativo,
    update_artigo_administrativo, toggle_artigo_administrativo,
    delete_artigo_administrativo,
    criar_ordem_transferencia,
)

compras_bp = Blueprint('compras', __name__)

TABS = [
    {'id': 'artigos', 'label': 'Artigos de Fornecimento', 'icon': '📋', 'url_endpoint': 'compras.artigos'},
    {'id': 'criar_ordem', 'label': 'Criar Ordem de Transferência', 'icon': '📦', 'url_endpoint': 'compras.criar_ordem'},
]


@compras_bp.route('/')
@perm_required('acesso_administrativo')
def index():
    items = [{'icon': t['icon'], 'label': t['label'], 'url': url_for(t['url_endpoint'])} for t in TABS]
    return render_template('components/section_menu.html', items=items)


@compras_bp.route('/artigos', methods=['GET', 'POST'])
@perm_required('acesso_administrativo')
def artigos():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            fornecedor = request.form.get('fornecedor', '').strip()
            produto = request.form.get('produto', '').strip()
            if fornecedor and produto:
                success = add_artigo_administrativo(fornecedor, produto)
                if success:
                    flash(f'Artigo "{produto}" adicionado!', 'success')
                else:
                    flash('Artigo já existe.', 'warning')
            else:
                flash('Preencha fornecedor e produto.', 'warning')

        elif action == 'toggle':
            artigo_id = int(request.form.get('artigo_id', 0))
            ativo = request.form.get('ativo') == '1'
            toggle_artigo_administrativo(artigo_id, ativo)

        elif action == 'delete':
            artigo_id = int(request.form.get('artigo_id', 0))
            delete_artigo_administrativo(artigo_id)
            flash('Artigo eliminado!', 'success')

        return redirect(url_for('compras.artigos'))

    artigos_list = get_artigos_administrativos(apenas_ativos=False)
    fornecedores = sorted(set(a['fornecedor'] for a in artigos_list))
    return render_template('compras/artigos.html',
                           artigos=artigos_list, fornecedores=fornecedores)


@compras_bp.route('/criar-ordem', methods=['GET', 'POST'])
@perm_required('acesso_administrativo')
def criar_ordem():
    if request.method == 'POST':
        username = session.get('user', {}).get('username', '')
        loja_destino = 'Bolhão'
        data_prevista_str = request.form.get('data_prevista', '')
        data_prevista = None
        if data_prevista_str:
            try:
                data_prevista = datetime.strptime(data_prevista_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        ordens_count = 0
        idx = 0
        while True:
            produto = request.form.get(f'produto_{idx}')
            if produto is None:
                break
            qty_str = request.form.get(f'qty_{idx}', '')
            unidade = request.form.get(f'unidade_{idx}', 'und')
            idx += 1
            try:
                qty = float(qty_str.replace(',', '.'))
            except (ValueError, TypeError):
                qty = 0
            if qty <= 0:
                continue
            criar_ordem_transferencia(date.today(), 'Compras', produto, qty, unidade, loja_destino, criado_por=username, data_prevista=data_prevista)
            ordens_count += 1
        if ordens_count > 0:
            flash(f'{ordens_count} ordem(ns) de transferência criada(s)!', 'success')
        else:
            flash('Nenhuma ordem criada. Preencha as quantidades.', 'info')
        return redirect(url_for('compras.criar_ordem'))

    artigos_list = get_artigos_administrativos(apenas_ativos=True)
    fornecedores = sorted(set(a['fornecedor'] for a in artigos_list))
    artigos_by_fornecedor = {}
    for a in artigos_list:
        artigos_by_fornecedor.setdefault(a['fornecedor'], []).append(a)
    return render_template('compras/criar_ordem.html',
                           artigos=artigos_list, fornecedores=fornecedores,
                           artigos_by_fornecedor=artigos_by_fornecedor,
                           today=str(date.today()))
