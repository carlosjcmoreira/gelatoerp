from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import database as db
import pandas as pd
from database import (
    get_produtos_pastelaria, get_coberturas,
    add_contagem_stock, delete_contagem_stock, get_contagem_stock_df,
    get_ultimo_stock_balcao,
    upsert_plano_area, marcar_produto_no_plano, remover_produto_do_plano,
    get_plano_do_dia_area, get_plano_produto,
    update_producao_real_area, update_plano_status_area,
    upsert_stock_producao_area, get_stock_producao_area_all,
    get_stock_producao_area, reduzir_stock_producao_area,
    criar_ordem_transferencia,
    add_quebra_area, get_quebras_df_area, delete_quebra_area,
    get_active_venda_stores,
    get_or_create_pending_batch,
)
from datetime import date

pastelaria_bp = Blueprint('pastelaria', __name__)

AREA = 'pastelaria'

TABS = [
    {'id': 'stock_balcao', 'label': 'Visão de Stock', 'icon': '📦', 'endpoint': 'pastelaria.stock_balcao'},
    {'id': 'planear', 'label': 'Planear Produção', 'icon': '📋', 'endpoint': 'pastelaria.planear'},
    {'id': 'produzir', 'label': 'Produzir', 'icon': '▶️', 'endpoint': 'pastelaria.produzir'},
    {'id': 'transferir', 'label': 'Transferir para Loja', 'icon': '🔄', 'endpoint': 'pastelaria.transferir'},
    {'id': 'quebra', 'label': 'Registar Quebra', 'icon': '⚠️', 'endpoint': 'pastelaria.registar_quebra'},
    {'id': 'gerir_produtos', 'label': 'Gerir Produtos', 'icon': '🍡', 'endpoint': 'pastelaria.gerir_produtos'},
]

def _tabs_with_urls():
    from db.tiles import get_tile_visibility
    visibility = get_tile_visibility('pastelaria')
    return [
        {'id': t['id'], 'label': t['label'], 'icon': t['icon'], 'url': url_for(t['endpoint'])}
        for t in TABS
        if visibility.get(t['id'], True)
    ]


def _parse_int(val_str, default=0):
    if not val_str:
        return default
    s = str(val_str).strip()
    try:
        return int(s)
    except ValueError:
        return default


@pastelaria_bp.route('/')
@perm_required('acesso_pastelaria')
def index():
    from db.tiles import get_tile_visibility
    visibility = get_tile_visibility('pastelaria')
    items = [
        {'icon': t['icon'], 'label': t['label'], 'url': url_for(t['endpoint'])}
        for t in TABS
        if visibility.get(t['id'], True)
    ]
    return render_template('components/section_menu.html', items=items,
                           menu_title='🍰 Produção Pastelaria')


@pastelaria_bp.route('/stock-balcao', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def stock_balcao():
    produtos_stock = get_produtos_pastelaria() or []

    msg = None
    msg_type = None

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'registar':
            data_contagem = request.form.get('data_contagem', '')
            loja = request.form.get('loja', 'Matosinhos')
            produto = request.form.get('produto', '')
            quantidade = _parse_int(request.form.get('quantidade', '0'))

            if produto:
                add_contagem_stock(date.fromisoformat(data_contagem), loja, produto, quantidade, AREA)
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

    ultimo_stock = get_ultimo_stock_balcao(AREA)

    stock_matrix = {}
    for s in ultimo_stock:
        prod = s['produto']
        if prod not in stock_matrix:
            stock_matrix[prod] = {'produto': prod, 'Bolhão': 0, 'Matosinhos': 0, 'data_bolhao': None, 'data_matosinhos': None}
        stock_matrix[prod][s['loja']] = s['quantidade']
        stock_matrix[prod][f"data_{s['loja'].lower()}"] = s['data']
    stock_matrix_list = sorted(stock_matrix.values(), key=lambda x: x['produto'])

    contagens = get_contagem_stock_df(AREA)
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

    return render_template('pastelaria/stock_balcao.html',
                           active_tab='stock_balcao',
                           tabs=_tabs_with_urls(),
                           produtos=produtos_stock,
                           ultimo_stock=ultimo_stock,
                           stock_matrix=stock_matrix_list,
                           contagens=contagens_list,
                           today=date.today().isoformat(),
                           msg=msg,
                           msg_type=msg_type)


@pastelaria_bp.route('/planear', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def planear():
    produtos = get_produtos_pastelaria()
    today = date.today()
    msg = None
    msg_type = None

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'save_plano':
            count = 0
            for produto in produtos:
                est_bol = _parse_int(request.form.get(f'est_bol_{produto}', '0'))
                est_mat = _parse_int(request.form.get(f'est_mat_{produto}', '0'))
                est_total = est_bol + est_mat
                if est_total > 0:
                    upsert_plano_area(AREA, today, produto, est_total,
                                      estimada_bolhao=est_bol, estimada_matosinhos=est_mat)
                    marcar_produto_no_plano(AREA, today, produto)
                    count += 1
            if count > 0:
                msg = f'Plano guardado com {count} produto(s)!'
                msg_type = 'success'
            else:
                msg = 'Nenhuma quantidade definida.'
                msg_type = 'warning'

        elif action == 'remover_produto':
            produto = request.form.get('produto', '')
            if produto:
                remover_produto_do_plano(AREA, today, produto)
                msg = f'{produto} removido do plano.'
                msg_type = 'success'

    plano_atual = get_plano_do_dia_area(AREA, today)
    plano_dict = {p['produto']: p for p in plano_atual}

    ultimo_stock = get_ultimo_stock_balcao(AREA)
    stock_bolhao_map = {}
    stock_mat_map = {}
    for s in ultimo_stock:
        if s['loja'] == 'Bolhão':
            stock_bolhao_map[s['produto']] = s['quantidade']
        elif s['loja'] == 'Matosinhos':
            stock_mat_map[s['produto']] = s['quantidade']

    produtos_data = []
    for p in produtos:
        plano = plano_dict.get(p)
        produtos_data.append({
            'nome': p,
            'estimado': plano['estimado'] if plano else 0,
            'estimado_bolhao': plano['estimado_bolhao'] if plano else 0,
            'estimado_matosinhos': plano['estimado_matosinhos'] if plano else 0,
            'no_plano': True if plano else False,
            'stock_bolhao': stock_bolhao_map.get(p, 0),
            'stock_matosinhos': stock_mat_map.get(p, 0),
        })

    return render_template('pastelaria/planear.html',
                           active_tab='planear',
                           tabs=_tabs_with_urls(),
                           produtos=produtos_data,
                           plano=plano_atual,
                           today=today.isoformat(),
                           msg=msg,
                           msg_type=msg_type)


@pastelaria_bp.route('/produzir', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def produzir():
    data_str = request.args.get('data', str(date.today()))
    try:
        data_plano = date.fromisoformat(data_str)
    except ValueError:
        data_plano = date.today()

    if request.method == 'POST':
        data_str = request.form.get('data', str(date.today()))
        try:
            data_plano = date.fromisoformat(data_str)
        except ValueError:
            data_plano = date.today()

        entradas = get_plano_do_dia_area(AREA, data_plano)
        registos = 0
        for e in entradas:
            produto = e['produto']
            real = _parse_int(request.form.get(f'real_{produto}', '0'))
            if real > 0:
                prev_real = e['real'] if e['real'] is not None else 0
                delta = real - prev_real
                update_producao_real_area(AREA, data_plano, produto, real)
                registos += 1

                if delta != 0:
                    upsert_stock_producao_area(AREA, data_plano, produto, delta)

        if registos > 0:
            flash(f"{registos} produto(s) com produção registada.", "success")
        else:
            flash("Nenhuma alteração.", "info")
        return redirect(url_for('pastelaria.produzir', data=str(data_plano)))

    entradas = get_plano_do_dia_area(AREA, data_plano)
    for e in entradas:
        e['has_real'] = e['real'] is not None and e['real'] > 0

    n_total = len(entradas)
    n_done = sum(1 for e in entradas if e['has_real'])

    return render_template('pastelaria/produzir.html',
                           active_tab='produzir',
                           tabs=_tabs_with_urls(),
                           entradas=entradas,
                           data_plano=str(data_plano),
                           n_total=n_total,
                           n_done=n_done)


@pastelaria_bp.route('/produzir/status', methods=['POST'])
@perm_required('acesso_pastelaria')
def produzir_status():
    data_str = request.form.get('data', str(date.today()))
    produto = request.form.get('produto', '')
    status = request.form.get('status', 'pendente')
    nota = request.form.get('nota', '')
    try:
        data_plano = date.fromisoformat(data_str)
    except ValueError:
        data_plano = date.today()
    if produto and status in ('pendente', 'em_curso', 'concluido'):
        update_plano_status_area(AREA, data_plano, produto, status, nota)
        return jsonify({'ok': True, 'status': status, 'nota': nota})
    return jsonify({'ok': False}), 400


@pastelaria_bp.route('/gerir-produtos')
@perm_required('acesso_pastelaria')
def gerir_produtos():
    return render_template('pastelaria/gerir_produtos.html',
                           active_tab='gerir_produtos',
                           tabs=_tabs_with_urls())


@pastelaria_bp.route('/transferir', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
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
            return redirect(url_for('pastelaria.transferir'))
        batch_id = get_or_create_pending_batch(today, 'Pastelaria', loja_destino)
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
            qty = _parse_int(qty_str)
            if qty <= 0:
                continue
            stock_disponivel = get_stock_producao_area(AREA, today, produto)
            if qty > stock_disponivel:
                qty = stock_disponivel
            if qty > 0:
                reduced = reduzir_stock_producao_area(AREA, today, produto, qty)
                if reduced:
                    criar_ordem_transferencia(today, 'Pastelaria', produto, qty, 'und', loja_destino, criado_por=username, data_prevista=data_prevista, batch_id=batch_id)
                    ordens_count += 1
        if ordens_count > 0:
            flash(f"{ordens_count} ordem(ns) de transferência criada(s)!", "success")
        else:
            flash("Nenhuma transferência registada. Verifique as quantidades.", "info")
        return redirect(url_for('pastelaria.transferir'))

    stock_prod = get_stock_producao_area_all(AREA, today)
    lojas_venda = get_active_venda_stores()

    ultimo_stock = get_ultimo_stock_balcao(AREA)
    balcao_map = {}
    for s in ultimo_stock:
        key = s['produto']
        if key not in balcao_map:
            balcao_map[key] = {}
        balcao_map[key][s['loja']] = {'quantidade': s['quantidade'], 'data': s['data']}

    prod_map = {sp['produto']: sp['quantidade'] for sp in stock_prod}
    all_produtos = sorted(set(list(prod_map.keys()) + list(balcao_map.keys())))

    cards = []
    for produto in all_produtos:
        balcao = balcao_map.get(produto, {})
        balcao_mat = balcao.get('Matosinhos', {})
        balcao_bol = balcao.get('Bolhão', {})
        data_mat = balcao_mat.get('data')
        data_bol = balcao_bol.get('data')
        cards.append({
            'produto': produto,
            'stock_prod': prod_map.get(produto, 0),
            'balcao_matosinhos': balcao_mat.get('quantidade', 0),
            'balcao_bolhao': balcao_bol.get('quantidade', 0),
            'data_balcao_matosinhos': data_mat.strftime('%d/%m') if data_mat else '-',
            'data_balcao_bolhao': data_bol.strftime('%d/%m') if data_bol else '-',
        })

    cards_transferivel = [c for c in cards if c['stock_prod'] > 0]

    return render_template('pastelaria/transferir.html',
                           active_tab='transferir',
                           tabs=_tabs_with_urls(),
                           cards=cards,
                           cards_transferivel=cards_transferivel,
                           lojas_venda=lojas_venda,
                           today=str(today))


def _parse_decimal(val_str, default=0.0):
    if not val_str:
        return default
    s = str(val_str).replace(',', '.').strip()
    try:
        return float(s)
    except ValueError:
        return default


@pastelaria_bp.route('/coberturas', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def coberturas():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'add_cobertura':
            nome = request.form.get('nova_cobertura', '').strip()
            if nome:
                success = db.add_cobertura(nome)
                flash(f"Cobertura '{nome}' adicionada!" if success else 'Cobertura já existe.', 'success' if success else 'warning')
            else:
                flash('Por favor, insira um nome.', 'warning')
        elif action == 'delete_cobertura':
            cid = int(request.form.get('cobertura_id'))
            db.delete_cobertura(cid)
            flash('Cobertura eliminada!', 'success')
        return redirect(url_for('pastelaria.coberturas'))
    return render_template('pastelaria/coberturas.html',
                           coberturas=db.get_all_coberturas())


@pastelaria_bp.route('/tipologias', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def tipologias():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'add_tipologia':
            nome = request.form.get('nova_tipologia', '').strip()
            if nome:
                success = db.add_tipologia_pastelaria(nome)
                flash(f"Tipologia '{nome}' adicionada!" if success else 'Tipologia já existe.', 'success' if success else 'warning')
            else:
                flash('Por favor, insira um nome.', 'warning')
        elif action == 'delete_tipologia':
            tid = int(request.form.get('tipologia_id'))
            db.delete_tipologia_pastelaria(tid)
            flash('Tipologia eliminada!', 'success')
        return redirect(url_for('pastelaria.tipologias'))
    return render_template('pastelaria/tipologias.html',
                           tipologias=db.get_all_tipologias_pastelaria())


@pastelaria_bp.route('/produtos', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def produtos():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'add_produto_past':
            tip = request.form.get('novo_tipologia', '')
            sabor = request.form.get('novo_sabor_past', '')
            cob = request.form.get('novo_cob_past', '')
            if tip:
                success = db.add_produto_pastelaria(tip, sabor, cob)
                flash('Produto adicionado!' if success else 'Produto já existe.', 'success' if success else 'warning')
            else:
                flash('Por favor, selecione uma tipologia.', 'warning')
        elif action == 'delete_bulk_produtos_past':
            ids_str = request.form.getlist('produto_ids')
            ids = [int(i) for i in ids_str if i.isdigit()]
            if ids:
                deleted = db.delete_produtos_pastelaria_bulk(ids)
                flash(f'{deleted} produto(s) eliminado(s)!', 'success')
            else:
                flash('Nenhum produto selecionado.', 'warning')
        return redirect(url_for('pastelaria.produtos'))
    return render_template('pastelaria/lista_produtos.html',
                           produtos_past=db.get_all_produtos_pastelaria(),
                           tipologias_list=[t['nome'] for t in db.get_all_tipologias_pastelaria()],
                           sabores_list=db.get_sabores_list(),
                           coberturas_list=[c['nome'] for c in db.get_all_coberturas()])
@pastelaria_bp.route('/gelado-tipologia', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def gelado_tipologia():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'save_gelado_tip':
            tipologias = db.get_all_tipologias_pastelaria()
            for tip in tipologias:
                nome = tip['nome']
                qtd_new = _parse_decimal(request.form.get(f'gelado_tip_qtd_{nome}'))
                db.update_gelado_peso_by_tipologia(nome, qtd_new)
            flash('Alterações guardadas!', 'success')
        return redirect(url_for('pastelaria.gelado_tipologia'))
    
    tipologias = db.get_all_tipologias_pastelaria()
    for tip in tipologias:
        peso = db.get_gelado_peso_by_tipologia(tip['nome'])
        tip['quantidade_gelado_g'] = peso or 0
    
    return render_template('pastelaria/gelado_tipologia.html',
                           tipologias=tipologias,
                           back_url=url_for('pastelaria.gerir_produtos'))


@pastelaria_bp.route('/registar-quebra', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def registar_quebra():
    if request.method == 'POST':
        data_quebra = date.fromisoformat(request.form['data'])
        quantidade = float(request.form.get('quantidade', '0').replace(',', '.'))
        produto = request.form.get('produto', '')
        lote = request.form.get('lote', '')
        motivo = request.form.get('motivo', '')
        if quantidade > 0 and produto:
            add_quebra_area(data_quebra, "Matosinhos", quantidade, AREA, produto, lote if lote else None, motivo)
            lote_text = f" (Lote: {lote})" if lote else ""
            flash(f"Quebra de {quantidade} de {produto}{lote_text} registada com sucesso!", "success")
        else:
            flash("Por favor, preencha os campos obrigatórios: Data, Quantidade e Produto.", "error")
        return redirect(url_for('pastelaria.registar_quebra'))

    produtos = get_produtos_pastelaria() or []
    quebras_df = get_quebras_df_area("Matosinhos", AREA)
    quebras = []
    if not quebras_df.empty:
        for _, r in quebras_df.iterrows():
            produto = r.get('sabor', '').replace(f'{AREA}:', '')
            quebras.append({
                'id': r.get('id'),
                'data': pd.to_datetime(r.get('data')).strftime('%d/%m/%Y') if r.get('data') else '',
                'produto': produto,
                'lote': r.get('lote', '-'),
                'quantidade': r.get('quantidade_kg'),
                'motivo': r.get('motivo', '-')
            })

    return render_template('pastelaria/registar_quebra.html',
                           active_tab='quebra', tabs=_tabs_with_urls(),
                           produtos=[p for p in produtos], quebras=quebras, today=str(date.today()))


@pastelaria_bp.route('/eliminar-quebra', methods=['POST'])
@perm_required('acesso_pastelaria')
def eliminar_quebra():
    id_to_delete = int(request.form['id'])
    delete_quebra_area(id_to_delete)
    flash("Registo eliminado!", "success")
    return redirect(url_for('pastelaria.registar_quebra'))
