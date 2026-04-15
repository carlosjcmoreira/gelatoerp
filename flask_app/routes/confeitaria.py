from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_app.auth import perm_required
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import pandas as pd
from database import (
    get_produtos_confeitaria,
    get_ultimo_stock_balcao,
    add_contagem_stock, delete_contagem_stock, get_contagem_stock_df,
    get_produtos_by_area,
    upsert_plano_area, marcar_produto_no_plano, remover_produto_do_plano,
    get_plano_do_dia_area, get_plano_produto,
    update_producao_real_area,
    upsert_stock_producao_area, get_stock_producao_area_all,
    get_stock_producao_area, reduzir_stock_producao_area,
    criar_ordem_transferencia,
    get_all_produtos_confeitaria, add_produto_confeitaria, delete_produto_confeitaria,
    add_quebra_area, get_quebras_df_area, delete_quebra_area,
    get_active_venda_stores,
    get_or_create_pending_batch,
)
from datetime import date

confeitaria_bp = Blueprint('confeitaria', __name__)

TABS = [
    {'id': 'stock_balcao', 'label': 'Visão de Stock', 'icon': '📦', 'endpoint': 'confeitaria.stock_balcao'},
    {'id': 'planear', 'label': 'Planear Produção', 'icon': '📋', 'endpoint': 'confeitaria.planear'},
    {'id': 'produzir', 'label': 'Produzir', 'icon': '▶️', 'endpoint': 'confeitaria.produzir'},
    {'id': 'transferir', 'label': 'Transferir para Loja', 'icon': '🔄', 'endpoint': 'confeitaria.transferir'},
    {'id': 'quebra', 'label': 'Registar Quebra', 'icon': '⚠️', 'endpoint': 'confeitaria.registar_quebra'},
    {'id': 'produtos', 'label': 'Produtos', 'icon': '🍪', 'endpoint': 'confeitaria.produtos'},
]

def _tabs_with_urls():
    return [{'id': t['id'], 'label': t['label'], 'icon': t['icon'], 'url': url_for(t['endpoint'])} for t in TABS]


@confeitaria_bp.route('/')
@perm_required('acesso_confeitaria')
def index():
    items = [{'icon': t['icon'], 'label': t['label'], 'url': url_for(t['endpoint'])} for t in TABS]
    return render_template('components/section_menu.html', items=items,
                           menu_title='🍪 Produção Confeitaria')


@confeitaria_bp.route('/stock-balcao', methods=['GET', 'POST'])
@perm_required('acesso_confeitaria')
def stock_balcao():
    produtos_stock = get_produtos_confeitaria() or []

    msg = None
    msg_type = None

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'registar':
            data_contagem = request.form.get('data_contagem', '')
            loja = request.form.get('loja', 'Matosinhos')
            produto = request.form.get('produto', '')
            quantidade = request.form.get('quantidade', '0')

            try:
                quantidade = int(quantidade)
            except ValueError:
                quantidade = 0

            if produto:
                add_contagem_stock(date.fromisoformat(data_contagem), loja, produto, quantidade, 'confeitaria')
                msg = f'Contagem de {quantidade}x {produto} registada!'
                msg_type = 'success'
            else:
                msg = 'Por favor, selecione um produto.'
                msg_type = 'warning'

        elif action == 'eliminar':
            id_del = request.form.get('id_delete', '0')
            try:
                delete_contagem_stock(int(id_del))
                msg = 'Contagem eliminada!'
                msg_type = 'success'
            except (ValueError, Exception):
                msg = 'Erro ao eliminar contagem.'
                msg_type = 'danger'

    stock_resumo = get_ultimo_stock_balcao('confeitaria')

    stock_matrix = {}
    for s in stock_resumo:
        prod = s['produto']
        if prod not in stock_matrix:
            stock_matrix[prod] = {'produto': prod, 'Bolhão': 0, 'Matosinhos': 0, 'data_bolhao': None, 'data_matosinhos': None}
        stock_matrix[prod][s['loja']] = s['quantidade']
        stock_matrix[prod][f"data_{s['loja'].lower()}"] = s['data']
    stock_matrix_list = sorted(stock_matrix.values(), key=lambda x: x['produto'])

    contagens = get_contagem_stock_df('confeitaria')
    contagens_list = []
    if not contagens.empty:
        for _, row in contagens.iterrows():
            contagens_list.append({
                'id': row['id'],
                'data': row['data'].strftime('%Y-%m-%d') if hasattr(row['data'], 'strftime') else str(row['data']),
                'loja': row['loja'],
                'produto': row['produto'],
                'quantidade': int(row['quantidade']),
            })

    return render_template('confeitaria/stock_balcao.html',
                           active_tab='stock_balcao',
                           tabs=_tabs_with_urls(),
                           produtos=produtos_stock,
                           stock_resumo=stock_resumo,
                           stock_matrix=stock_matrix_list,
                           contagens=contagens_list,
                           today=date.today().isoformat(),
                           msg=msg,
                           msg_type=msg_type)


@confeitaria_bp.route('/planear', methods=['GET', 'POST'])
@perm_required('acesso_confeitaria')
def planear():
    today = date.today()
    produtos = get_produtos_confeitaria()

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'save_plan':
            count = 0
            for produto in produtos:
                qty_str = request.form.get(f'qty_{produto}', '0')
                try:
                    qty = int(qty_str)
                except ValueError:
                    qty = 0
                if qty > 0:
                    upsert_plano_area('confeitaria', today, produto, qty)
                    marcar_produto_no_plano('confeitaria', today, produto)
                    count += 1
                else:
                    existing = get_plano_produto('confeitaria', today, produto)
                    if existing and existing.get('no_plano'):
                        pass
            if count > 0:
                flash(f'{count} produto(s) adicionado(s) ao plano!', 'success')
            else:
                flash('Nenhuma quantidade definida.', 'info')
            return redirect(url_for('confeitaria.planear'))

        elif action == 'remove_from_plan':
            produto = request.form.get('produto', '')
            if produto:
                remover_produto_do_plano('confeitaria', today, produto)
                flash(f'{produto} removido do plano.', 'success')
            return redirect(url_for('confeitaria.planear'))

    plano = get_plano_do_dia_area('confeitaria', today)
    plano_produtos = {p['produto'] for p in plano}

    produtos_grid = []
    for p in produtos:
        existing = get_plano_produto('confeitaria', today, p)
        produtos_grid.append({
            'nome': p,
            'no_plano': p in plano_produtos,
            'estimado': existing['estimado'] if existing else 0,
        })

    return render_template('confeitaria/planear.html',
                           active_tab='planear',
                           tabs=_tabs_with_urls(),
                           produtos=produtos_grid,
                           plano=plano,
                           today=today.isoformat())


@confeitaria_bp.route('/produzir', methods=['GET', 'POST'])
@perm_required('acesso_confeitaria')
def produzir():
    today = date.today()

    if request.method == 'POST':
        plano = get_plano_do_dia_area('confeitaria', today)
        registos = 0
        for entry in plano:
            produto = entry['produto']
            real_str = request.form.get(f'real_{produto}', '0')
            try:
                real = int(real_str)
            except ValueError:
                real = 0
            if real > 0:
                prev_real = entry['real'] if entry['real'] is not None else 0
                delta = real - prev_real
                update_producao_real_area('confeitaria', today, produto, real)
                registos += 1
                if delta != 0:
                    upsert_stock_producao_area('confeitaria', today, produto, delta)
        if registos > 0:
            flash(f'{registos} produto(s) registado(s)!', 'success')
        else:
            flash('Nenhuma alteração.', 'info')
        return redirect(url_for('confeitaria.produzir'))

    plano = get_plano_do_dia_area('confeitaria', today)
    for entry in plano:
        entry['has_real'] = entry['real'] is not None and entry['real'] > 0
        if entry['has_real']:
            entry['diferenca'] = entry['real'] - entry['estimado']
        else:
            entry['diferenca'] = None

    n_total = len(plano)
    n_done = sum(1 for e in plano if e['has_real'])

    return render_template('confeitaria/produzir.html',
                           active_tab='produzir',
                           tabs=_tabs_with_urls(),
                           plano=plano,
                           today=today.isoformat(),
                           n_total=n_total,
                           n_done=n_done)


@confeitaria_bp.route('/transferir', methods=['GET', 'POST'])
@perm_required('acesso_confeitaria')
def transferir():
    today = date.today()

    if request.method == 'POST':
        ordens_count = 0
        username = session.get('user', {}).get('username', '')
        data_prevista_str = request.form.get('data_prevista', '')
        data_prevista = None
        if data_prevista_str:
            try:
                from datetime import datetime
                data_prevista = datetime.strptime(data_prevista_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        loja_destino = request.form.get('loja_destino', '')
        active_store_names = {s['name'] for s in get_active_venda_stores()}
        if loja_destino not in active_store_names:
            flash('Loja de destino inválida.', 'error')
            return redirect(url_for('confeitaria.transferir'))
        batch_id = get_or_create_pending_batch(today, 'Confeitaria', loja_destino)
        import re as _re
        form_pairs = []
        for key in request.form:
            m = _re.match(r'^produto_(\d+)$', key)
            if m:
                n = int(m.group(1))
                form_pairs.append((n, request.form[key], request.form.get(f'qty_{n}', '')))
        for _, produto, qty_str in sorted(form_pairs, key=lambda x: x[0]):
            if not produto:
                continue
            try:
                qty = int(qty_str) if qty_str else 0
            except ValueError:
                qty = 0
            if qty <= 0:
                continue
            stock_disponivel = get_stock_producao_area('confeitaria', today, produto)
            if qty > stock_disponivel:
                qty = stock_disponivel
            if qty > 0:
                reduced = reduzir_stock_producao_area('confeitaria', today, produto, qty)
                if reduced:
                    criar_ordem_transferencia(today, 'Confeitaria', produto, qty, 'und', loja_destino, criado_por=username, data_prevista=data_prevista, batch_id=batch_id)
                    ordens_count += 1
        if ordens_count > 0:
            flash(f'{ordens_count} ordem(ns) de transferência criada(s)!', 'success')
        else:
            flash('Nenhuma transferência registada. Verifique as quantidades.', 'info')
        return redirect(url_for('confeitaria.transferir'))

    stock_prod = get_stock_producao_area_all('confeitaria', today)
    lojas_venda = get_active_venda_stores()

    stock_balcao = get_ultimo_stock_balcao('confeitaria')
    balcao_map = {}
    for s in stock_balcao:
        key = s['produto']
        if key not in balcao_map:
            balcao_map[key] = {}
        balcao_map[key][s['loja']] = {'quantidade': s['quantidade'], 'data': s['data']}

    cards = []
    for sp in stock_prod:
        produto = sp['produto']
        balcao_info = balcao_map.get(produto, {})
        balcao_mat = balcao_info.get('Matosinhos', {})
        balcao_bol = balcao_info.get('Bolhão', {})
        data_mat = balcao_mat.get('data')
        data_bol = balcao_bol.get('data')
        cards.append({
            'produto': produto,
            'stock_producao': sp['quantidade'],
            'balcao_matosinhos': balcao_mat.get('quantidade', 0),
            'balcao_bolhao': balcao_bol.get('quantidade', 0),
            'data_balcao_matosinhos': data_mat.strftime('%d/%m') if data_mat and hasattr(data_mat, 'strftime') else '-',
            'data_balcao_bolhao': data_bol.strftime('%d/%m') if data_bol and hasattr(data_bol, 'strftime') else '-',
        })

    return render_template('confeitaria/transferir.html',
                           active_tab='transferir',
                           tabs=_tabs_with_urls(),
                           cards=cards,
                           lojas_venda=lojas_venda,
                           today=today.isoformat())


@confeitaria_bp.route('/produtos', methods=['GET', 'POST'])
@perm_required('acesso_confeitaria')
def produtos():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'add_produto_conf':
            nome = request.form.get('novo_prod_conf', '').strip()
            if nome:
                success = add_produto_confeitaria(nome)
                flash(f"Produto '{nome}' adicionado!" if success else 'Produto já existe.', 'success' if success else 'warning')
            else:
                flash('Por favor, insira um nome.', 'warning')
        elif action == 'delete_produto_conf':
            pid = int(request.form.get('produto_conf_id'))
            delete_produto_confeitaria(pid)
            flash('Produto eliminado!', 'success')
        return redirect(url_for('confeitaria.produtos'))
    return render_template('confeitaria/produtos.html',
                           active_tab='produtos',
                           tabs=_tabs_with_urls(),
                           produtos_conf=get_all_produtos_confeitaria())
@confeitaria_bp.route('/registar-quebra', methods=['GET', 'POST'])
@perm_required('acesso_confeitaria')
def registar_quebra():
    if request.method == 'POST':
        data_quebra = date.fromisoformat(request.form['data'])
        quantidade = float(request.form.get('quantidade', '0').replace(',', '.'))
        produto = request.form.get('produto', '')
        lote = request.form.get('lote', '')
        motivo = request.form.get('motivo', '')
        if quantidade > 0 and produto:
            add_quebra_area(data_quebra, "Matosinhos", quantidade, "confeitaria", produto, lote if lote else None, motivo)
            lote_text = f" (Lote: {lote})" if lote else ""
            flash(f"Quebra de {quantidade} de {produto}{lote_text} registada com sucesso!", "success")
        else:
            flash("Por favor, preencha os campos obrigatórios: Data, Quantidade e Produto.", "error")
        return redirect(url_for('confeitaria.registar_quebra'))

    produtos = get_all_produtos_confeitaria() or []
    quebras_df = get_quebras_df_area("Matosinhos", "confeitaria")
    quebras = []
    if not quebras_df.empty:
        for _, r in quebras_df.iterrows():
            produto = r.get('sabor', '').replace('confeitaria:', '')
            quebras.append({
                'id': r.get('id'),
                'data': pd.to_datetime(r.get('data')).strftime('%d/%m/%Y') if r.get('data') else '',
                'produto': produto,
                'lote': r.get('lote', '-'),
                'quantidade': r.get('quantidade_kg'),
                'motivo': r.get('motivo', '-')
            })

    return render_template('confeitaria/registar_quebra.html',
                           active_tab='quebra', tabs=_tabs_with_urls(),
                           produtos=[p for p in produtos], quebras=quebras, today=str(date.today()))


@confeitaria_bp.route('/eliminar-quebra', methods=['POST'])
@perm_required('acesso_confeitaria')
def eliminar_quebra():
    id_to_delete = int(request.form['id'])
    delete_quebra_area(id_to_delete)
    flash("Registo eliminado!", "success")
    return redirect(url_for('confeitaria.registar_quebra'))
