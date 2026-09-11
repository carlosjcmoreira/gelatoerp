from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, abort
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
    get_plano_do_dia_area, get_plano_produto, get_plano_intervalo_area,
    update_producao_real_area, update_plano_status_area,
    upsert_stock_producao_area, get_stock_producao_area_all,
    get_stock_producao_area, reduzir_stock_producao_area,
    criar_ordem_transferencia,
    add_quebra_area, get_quebras_df_area, delete_quebra_area,
    get_active_venda_stores,
    get_or_create_pending_batch,
    get_reconciliacao_pastelaria,
)
from datetime import date, timedelta
from db.pastelaria import (
    get_pastelaria_stock_minimums,
    save_pastelaria_stock_minimums,
    get_pastelaria_priority_status,
    generate_pastelaria_priority_plan,
    get_pastelaria_priority_plan,
)

pastelaria_bp = Blueprint('pastelaria', __name__)

AREA = 'pastelaria'

TABS = [
    {'id': 'stock_balcao', 'label': 'Visão de Stock', 'icon': '📦', 'endpoint': 'pastelaria.stock_balcao'},
    {'id': 'planear', 'label': 'Planear Produção', 'icon': '📋', 'endpoint': 'pastelaria.planear'},
    {'id': 'transferir', 'label': 'Transferir para Loja', 'icon': '🔄', 'endpoint': 'pastelaria.transferir'},
    {'id': 'quebra', 'label': 'Registar Quebra', 'icon': '⚠️', 'endpoint': 'pastelaria.registar_quebra'},
    {'id': 'reconciliacao', 'label': 'Reconciliação', 'icon': '📊', 'endpoint': 'pastelaria.reconciliacao'},
    {'id': 'gerir_produtos', 'label': 'Gerir Produtos', 'icon': '🍡', 'endpoint': 'pastelaria.gerir_produtos'},
]


def _can_configure_stock_minimums(user):
    return bool(user and (
        user.get('acesso_gestor') or user.get('role') in {'admin', 'gestao'}
    ))


def _can_generate_priority_plan(user):
    return bool(user and (
        user.get('acesso_gestor')
        or user.get('role') in {'admin', 'gestao', 'producao'}
    ))


def _parse_stock_minimum_matrix(config, form):
    values = []
    for product in config['products']:
        for store in config['stores']:
            raw = form.get(f"min_{product['id']}_{store['id']}")
            if raw is None or not raw.strip().isdigit():
                raise ValueError(
                    'Preencha todos os stocks mínimos com números inteiros não negativos.'
                )
            values.append((product['id'], store['id'], int(raw)))
    return values

def _tabs_with_urls():
    from db.tiles import get_tile_visibility, get_tile_labels, get_tile_icons
    visibility = get_tile_visibility('pastelaria')
    labels = get_tile_labels('pastelaria')
    icons = get_tile_icons('pastelaria')
    return [
        {'id': t['id'], 'label': labels.get(t['id']) or t['label'], 'icon': icons.get(t['id']) or t['icon'], 'url': url_for(t['endpoint'])}
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


def _week_start(value):
    """Return the Monday for a date-like value, defaulting to this week."""
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError):
        parsed = date.today()
    return parsed - timedelta(days=parsed.weekday())


def _week_days(start):
    return [start + timedelta(days=offset) for offset in range(7)]


def validate_bolo_configuration(tamanho, sabores, cobertura, tamanhos, sabores_validos, coberturas):
    """Validate and canonicalise the configurable cake fields."""
    options = {str(option).strip().casefold(): str(option).strip() for option in tamanhos}
    tamanho_key = str(tamanho or '').strip().casefold()
    if not tamanho_key or tamanho_key not in options:
        return None, 'Selecione um tamanho de Bolo válido.'

    sabores_validos_map = {
        str(option).strip().casefold(): str(option).strip()
        for option in sabores_validos
    }
    escolhidos = [str(s).strip() for s in (sabores or []) if str(s).strip()]
    if not 1 <= len(escolhidos) <= 3:
        return None, 'O Bolo deve ter entre 1 e 3 sabores.'
    sabores_canonicos = []
    vistos = set()
    for sabor in escolhidos:
        key = sabor.casefold()
        if key not in sabores_validos_map:
            return None, f'Sabor de Bolo inválido: {sabor}.'
        if key in vistos:
            return None, 'Os sabores do Bolo não podem repetir-se.'
        vistos.add(key)
        sabores_canonicos.append(sabores_validos_map[key])

    coberturas_map = {
        str(option).strip().casefold(): str(option).strip()
        for option in coberturas
    }
    cobertura_text = str(cobertura or '').strip()
    if cobertura_text and cobertura_text.casefold() not in coberturas_map:
        return None, f'Cobertura de Bolo inválida: {cobertura_text}.'

    return {
        'tamanho': options[tamanho_key],
        'sabores': sabores_canonicos,
        'cobertura': coberturas_map.get(cobertura_text.casefold(), '') if cobertura_text else '',
    }, None


@pastelaria_bp.route('/')
@perm_required('acesso_pastelaria')
def index():
    from db.tiles import get_tile_visibility, get_tile_labels, get_tile_icons, get_module_labels
    visibility = get_tile_visibility('pastelaria')
    labels = get_tile_labels('pastelaria')
    icons = get_tile_icons('pastelaria')
    custom_mod = get_module_labels().get('pastelaria')
    items = [
        {'icon': icons.get(t['id']) or t['icon'], 'label': labels.get(t['id']) or t['label'], 'url': url_for(t['endpoint'])}
        for t in TABS
        if visibility.get(t['id'], True)
    ]
    return render_template('components/section_menu.html', items=items,
                           menu_title=f'🍰 {custom_mod}' if custom_mod else '🍰 Produção Pastelaria')


@pastelaria_bp.route('/stock-balcao', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def stock_balcao():
    produtos_stock = get_produtos_pastelaria() or []
    stock_config = get_pastelaria_stock_minimums()
    valid_store_names = {store['name'] for store in stock_config['stores']}

    msg = None
    msg_type = None

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'registar':
            data_contagem = request.form.get('data_contagem', '')
            loja = request.form.get('loja', 'Matosinhos')
            produto = request.form.get('produto', '')
            raw_quantity = request.form.get('quantidade', '')
            try:
                count_date = date.fromisoformat(data_contagem)
                if loja not in valid_store_names or produto not in produtos_stock:
                    raise ValueError
                if not raw_quantity.strip().isdigit():
                    raise ValueError
                quantidade = int(raw_quantity)
            except (TypeError, ValueError):
                msg, msg_type = 'Data, loja, produto ou quantidade inválidos.', 'warning'
            else:
                add_contagem_stock(count_date, loja, produto, quantidade, AREA)
                msg = f'Contagem de {quantidade}x {produto} registada!'
                msg_type = 'success'

        elif action == 'gerar_plano':
            if not _can_generate_priority_plan(session.get('user') or {}):
                abort(403)
            try:
                count_date = date.fromisoformat(request.form.get('data_plano', ''))
                plan_id = generate_pastelaria_priority_plan(
                    count_date, (session.get('user') or {}).get('username'),
                )
                return redirect(url_for('pastelaria.plano_prioridade', plan_id=plan_id))
            except (TypeError, ValueError) as exc:
                msg, msg_type = str(exc), 'warning'
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

    plan_date_value = request.args.get('data_plano')
    try:
        plan_date = date.fromisoformat(plan_date_value) if plan_date_value else (
            date.today() - timedelta(days=(date.today().weekday() + 1) % 7)
        )
        priority_status = get_pastelaria_priority_status(plan_date)
    except ValueError as exc:
        plan_date = date.today()
        priority_status = {'complete': False, 'missing': [], 'rows': [], 'error': str(exc)}

    return render_template('pastelaria/stock_balcao.html',
                           active_tab='stock_balcao',
                           tabs=_tabs_with_urls(),
                           produtos=produtos_stock,
                           ultimo_stock=ultimo_stock,
                           stock_matrix=stock_matrix_list,
                           contagens=contagens_list,
                           today=date.today().isoformat(),
                           plan_date=plan_date,
                           priority_status=priority_status,
                           stock_stores=stock_config['stores'],
                           can_generate_priority_plan=_can_generate_priority_plan(
                               session.get('user') or {}
                           ),
                           msg=msg,
                           msg_type=msg_type)


@pastelaria_bp.route('/planear', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def planear():
    produtos = get_produtos_pastelaria() or []
    produtos_normais = [p for p in produtos if p.strip().casefold() != 'bolo']
    week_start = _week_start(request.values.get('week_start'))
    week_days = _week_days(week_start)
    msg = None
    msg_type = None

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'save_plano':
            count = 0
            for day in week_days:
                day_key = day.isoformat()
                for index, produto in enumerate(produtos_normais):
                    est_bol = _parse_int(
                        request.form.get(f'est_bol_{day_key}_{index}', '0')
                    )
                    est_mat = _parse_int(
                        request.form.get(f'est_mat_{day_key}_{index}', '0')
                    )
                    est_total = est_bol + est_mat
                    if est_total > 0:
                        upsert_plano_area(
                            AREA, day, produto, est_total,
                            estimada_bolhao=est_bol,
                            estimada_matosinhos=est_mat,
                            nota=request.form.get(f'nota_{day_key}_{index}', ''),
                        )
                        marcar_produto_no_plano(AREA, day, produto)
                        count += 1
            if count > 0:
                msg = f'Plano semanal guardado com {count} artigo(s)!'
                msg_type = 'success'
            else:
                msg = 'Nenhuma quantidade definida.'
                msg_type = 'warning'

        elif action == 'save_bolo':
            bolo_data_str = request.form.get('bolo_data', '')
            try:
                bolo_data = date.fromisoformat(bolo_data_str)
            except ValueError:
                bolo_data = None
            if bolo_data not in week_days:
                msg = 'Escolha um dia dentro da semana apresentada.'
                msg_type = 'warning'
            else:
                config, error = validate_bolo_configuration(
                    request.form.get('bolo_tamanho'),
                    [request.form.get(f'bolo_sabor_{index}', '') for index in range(1, 4)],
                    request.form.get('bolo_cobertura', ''),
                    db.get_bolo_tamanhos(),
                    db.get_sabores_list(),
                    [c['nome'] for c in db.get_all_coberturas() if c['ativo']],
                )
                bolo_bol = _parse_int(request.form.get('bolo_qtd_bolhao', '0'))
                bolo_mat = _parse_int(request.form.get('bolo_qtd_matosinhos', '0'))
                if error:
                    msg, msg_type = error, 'warning'
                elif bolo_bol + bolo_mat <= 0:
                    msg, msg_type = 'Indique uma quantidade de Bolo para pelo menos uma loja.', 'warning'
                else:
                    produto = db.build_bolo_product_label(
                        config['tamanho'], config['sabores'], config['cobertura']
                    )
                    upsert_plano_area(
                        AREA, bolo_data, produto, bolo_bol + bolo_mat,
                        estimada_bolhao=bolo_bol,
                        estimada_matosinhos=bolo_mat,
                        item_tipo='bolo',
                        bolo_tamanho=config['tamanho'],
                        bolo_sabor_1=config['sabores'][0],
                        bolo_sabor_2=config['sabores'][1] if len(config['sabores']) > 1 else None,
                        bolo_sabor_3=config['sabores'][2] if len(config['sabores']) > 2 else None,
                        bolo_cobertura=config['cobertura'] or None,
                        nota=request.form.get('bolo_nota', ''),
                    )
                    marcar_produto_no_plano(AREA, bolo_data, produto)
                    msg = f'Bolo adicionado ao plano de {bolo_data.strftime("%d/%m")}.'
                    msg_type = 'success'

        elif action == 'remover_produto':
            data_str = request.form.get('data', '')
            produto = request.form.get('produto', '')
            try:
                item_date = date.fromisoformat(data_str)
            except ValueError:
                item_date = None
            if produto and item_date in week_days:
                remover_produto_do_plano(AREA, item_date, produto)
                msg = f'{produto} removido do plano.'
                msg_type = 'success'

    plano_rows = get_plano_intervalo_area(AREA, week_start, week_days[-1])
    rows_by_day = {day: [] for day in week_days}
    for row in plano_rows:
        rows_by_day.setdefault(row['data'], []).append(row)
    days = []
    day_labels = ('Segunda-feira', 'Terça-feira', 'Quarta-feira',
                  'Quinta-feira', 'Sexta-feira', 'Sábado', 'Domingo')
    for day in week_days:
        rows = rows_by_day.get(day, [])
        rows_by_product = {row['produto']: row for row in rows}
        standard_rows = []
        for produto in produtos_normais:
            row = rows_by_product.get(produto, {})
            standard_rows.append({
                'nome': produto,
                'estimado_bolhao': row.get('estimado_bolhao', 0),
                'estimado_matosinhos': row.get('estimado_matosinhos', 0),
                'estimado': row.get('estimado', 0),
                'nota': row.get('nota', ''),
            })
        days.append({
            'date': day,
            'iso': day.isoformat(),
            'label': day_labels[day.weekday()],
            'rows': rows,
            'standard_rows': standard_rows,
            'bolo_rows': [row for row in rows if row.get('item_tipo') == 'bolo'],
        })

    return render_template('pastelaria/planear.html',
                           active_tab='planear',
                           tabs=_tabs_with_urls(),
                           days=days,
                           week_start=week_start,
                           previous_week=(week_start - timedelta(days=7)).isoformat(),
                           next_week=(week_start + timedelta(days=7)).isoformat(),
                           bolo_tamanhos=db.get_bolo_tamanhos(),
                           bolo_sabores=db.get_sabores_list(),
                           bolo_coberturas=[c for c in db.get_all_coberturas() if c['ativo']],
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
        if data_prevista is None:
            flash('Data prevista inválida.', 'error')
            return redirect(url_for('pastelaria.transferir'))
        loja_destino = request.form.get('loja_destino', '')
        active_store_names = {s['name'] for s in get_active_venda_stores()}
        if loja_destino not in active_store_names:
            flash('Loja de destino inválida.', 'error')
            return redirect(url_for('pastelaria.transferir'))
        import re as _re
        form_pairs = []
        for key in request.form:
            m = _re.match(r'^produto_(\d+)$', key)
            if m:
                n = int(m.group(1))
                form_pairs.append((n, request.form[key], request.form.get(f'qty_{n}', '')))

        requested = []
        for _, produto, qty_str in sorted(form_pairs, key=lambda x: x[0]):
            qty = _parse_int(qty_str)
            if produto and qty > 0:
                requested.append((produto, qty))

        configured_products = {
            produto for produto in get_produtos_pastelaria()
            if produto.strip().casefold() != 'bolo'
        }
        persisted_products = {
            row['produto'] for row in get_plano_do_dia_area(AREA, data_prevista)
        }
        persisted_products.update(
            row['produto'] for row in get_stock_producao_area_all(AREA, today)
        )
        persisted_products.update(
            row['produto'] for row in get_ultimo_stock_balcao(AREA)
        )
        allowed_products = configured_products | persisted_products
        invalid_products = sorted({
            produto for produto, _ in requested if produto not in allowed_products
        })
        if invalid_products:
            flash(
                'Artigo inválido ou não planeado para a data prevista: '
                + ', '.join(invalid_products),
                'error',
            )
            return redirect(url_for(
                'pastelaria.transferir',
                data_prevista=data_prevista.isoformat(),
            ))

        batch_id = get_or_create_pending_batch(today, 'Pastelaria', loja_destino)
        for produto, qty in requested:
            # The weekly plan is a manual dispatch decision. Digital production
            # stock is informational only and must not block or truncate it.
            criar_ordem_transferencia(
                today, 'Pastelaria', produto, qty, 'und', loja_destino,
                criado_por=username, data_prevista=data_prevista, batch_id=batch_id
            )
            ordens_count += 1
        if ordens_count > 0:
            flash(f"{ordens_count} ordem(ns) de transferência criada(s)!", "success")
        else:
            flash("Nenhuma transferência registada. Verifique as quantidades.", "info")
        return redirect(url_for(
            'pastelaria.transferir',
            data_prevista=data_prevista.isoformat(),
        ))

    data_prevista = today
    requested_date = request.args.get('data_prevista', '')
    if requested_date:
        try:
            data_prevista = date.fromisoformat(requested_date)
        except ValueError:
            pass
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
    plano_data_prevista = get_plano_do_dia_area(AREA, data_prevista)
    configured_products = [
        produto for produto in get_produtos_pastelaria()
        if produto.strip().casefold() != 'bolo'
    ]
    all_produtos = sorted(set(
        list(prod_map.keys())
        + list(balcao_map.keys())
        + configured_products
        + [row['produto'] for row in plano_data_prevista]
    ))

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

    # Every known article is selectable: the operator confirms physical
    # availability when preparing the dispatch.
    cards_transferivel = cards

    return render_template('pastelaria/transferir.html',
                           active_tab='transferir',
                           tabs=_tabs_with_urls(),
                           cards=cards,
                           cards_transferivel=cards_transferivel,
                           lojas_venda=lojas_venda,
                           data_prevista=data_prevista.isoformat())


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
        if action == 'save_minimos_stock':
            if not _can_configure_stock_minimums(session.get('user') or {}):
                abort(403)
            config = get_pastelaria_stock_minimums()
            try:
                values = _parse_stock_minimum_matrix(config, request.form)
                save_pastelaria_stock_minimums(values)
                flash('Stocks mínimos guardados.', 'success')
            except ValueError as exc:
                flash(str(exc), 'warning')
        elif action == 'add_produto_past':
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
    minimum_config = get_pastelaria_stock_minimums()
    return render_template('pastelaria/lista_produtos.html',
                           produtos_past=db.get_all_produtos_pastelaria(),
                           tipologias_list=[t['nome'] for t in db.get_all_tipologias_pastelaria()],
                           sabores_list=db.get_sabores_list(),
                           coberturas_list=[c['nome'] for c in db.get_all_coberturas()],
                           minimum_config=minimum_config,
                           can_configure_minimums=_can_configure_stock_minimums(
                               session.get('user') or {}
                           ))


@pastelaria_bp.route('/plano-prioridade/<int:plan_id>')
@perm_required('acesso_pastelaria')
def plano_prioridade(plan_id):
    plan = get_pastelaria_priority_plan(plan_id=plan_id)
    if not plan:
        flash('Plano de produção não encontrado.', 'warning')
        return redirect(url_for('pastelaria.stock_balcao'))
    return render_template(
        'pastelaria/plano_prioridade.html', plan=plan,
        active_tab='stock_balcao', tabs=_tabs_with_urls(),
    )
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
    historico_inicio = date.today() - timedelta(days=90)
    quebras_df = get_quebras_df_area("Matosinhos", AREA, data_inicio=historico_inicio)
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


@pastelaria_bp.route('/reconciliacao')
@perm_required('acesso_pastelaria')
def reconciliacao():
    from datetime import timedelta
    today = date.today()
    data_inicio_str = request.args.get('data_inicio', str(today))
    data_fim_str = request.args.get('data_fim', str(today))
    try:
        data_inicio = date.fromisoformat(data_inicio_str)
    except ValueError:
        data_inicio = today
    try:
        data_fim = date.fromisoformat(data_fim_str)
    except ValueError:
        data_fim = today
    if data_fim < data_inicio:
        data_fim = data_inicio

    rows = get_reconciliacao_pastelaria(data_inicio, data_fim)

    for r in rows:
        r['data_mat_fmt'] = r['data_mat'].strftime('%d/%m/%Y') if r['data_mat'] else None
        r['data_bol_fmt'] = r['data_bol'].strftime('%d/%m/%Y') if r['data_bol'] else None

    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    return render_template(
        'pastelaria/reconciliacao.html',
        active_tab='reconciliacao',
        tabs=_tabs_with_urls(),
        rows=rows,
        data_inicio=str(data_inicio),
        data_fim=str(data_fim),
        today=str(today),
        preset_hoje=(str(today), str(today)),
        preset_semana=(str(week_start), str(today)),
        preset_mes=(str(month_start), str(today)),
    )


@pastelaria_bp.route('/eliminar-quebra', methods=['POST'])
@perm_required('acesso_pastelaria')
def eliminar_quebra():
    id_to_delete = int(request.form['id'])
    delete_quebra_area(id_to_delete)
    flash("Registo eliminado!", "success")
    return redirect(url_for('pastelaria.registar_quebra'))
