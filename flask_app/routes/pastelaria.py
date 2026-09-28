from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, abort
from flask_app.auth import perm_required
import sys, os
import json
from uuid import uuid4
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
    add_quebra_area, get_quebras_df_area, delete_quebra_area,
    get_active_venda_stores,
    get_reconciliacao_pastelaria,
)
from db.plano import criar_ordens_transferencia_pastelaria
from db.pastelaria_stock import (
    PastelariaStockError,
    get_pastelaria_stock_options,
    get_pastelaria_stock_movements,
    register_pastelaria_stock_movement,
)
from datetime import date, timedelta
from db.pastelaria import (
    get_pastelaria_stock_minimums,
    save_pastelaria_stock_minimums,
    get_pastelaria_sunday_count_grid,
    save_pastelaria_sunday_counts,
    get_pastelaria_store_count_grid,
    save_pastelaria_store_counts,
    get_pastelaria_priority_status,
    generate_pastelaria_priority_plan,
    get_pastelaria_priority_plan,
    list_pastelaria_priority_plans,
    get_pastelaria_intelligence,
)
from db.doseamento import set_typology_dose

pastelaria_bp = Blueprint('pastelaria', __name__)

AREA = 'pastelaria'
PASTELARIA_CUSTOM_FLAVOUR_OPTION = '__pastelaria_outro_sabor__'

TABS = [
    {
        'id': 'stock_balcao',
        'label': 'Visão de Stock',
        'icon': '📦',
        'endpoint': 'pastelaria.stock_balcao',
        'description': 'Consultar contagens físicas das lojas e respetivo histórico.',
    },
    {
        'id': 'inteligencia',
        'label': 'Rotação e Sazonalidade',
        'icon': '📈',
        'endpoint': 'pastelaria.inteligencia',
        'description': 'Analisar rotação, vendas e sazonalidade.',
    },
    {
        'id': 'planear',
        'label': 'Planear Produção',
        'icon': '📋',
        'endpoint': 'pastelaria.planear',
        'description': 'Gerar prioridades a partir de contagens e mínimos.',
    },
    {
        'id': 'stock_producao',
        'label': 'Stock de Produção',
        'icon': '📦',
        'endpoint': 'pastelaria.stock_producao',
        'description': 'Confirmar saldos e registar produção ou correções.',
    },
    {
        'id': 'transferir',
        'label': 'Transferir para Loja',
        'icon': '🔄',
        'endpoint': 'pastelaria.transferir',
        'description': 'Transferir artigos com saldo de produção disponível.',
    },
    {
        'id': 'quebra',
        'label': 'Registar Quebra',
        'icon': '⚠️',
        'endpoint': 'pastelaria.registar_quebra',
        'description': 'Registar perdas de artigos de Pastelaria.',
    },
    {
        'id': 'reconciliacao',
        'label': 'Reconciliação',
        'icon': '📊',
        'endpoint': 'pastelaria.reconciliacao',
        'description': 'Conferir diferenças entre registos de Pastelaria.',
    },
    {
        'id': 'gerir_produtos',
        'label': 'Gerir Produtos',
        'icon': '🍡',
        'endpoint': 'pastelaria.gerir_produtos',
        'description': 'Adicionar e gerir produtos de Pastelaria.',
    },
]


def _build_stock_matrix(latest_stock, products, stores):
    stock_matrix = {}
    for stock in latest_stock:
        product = stock['produto']
        row = stock_matrix.setdefault(product, {
            'produto': product,
            'stores': {},
        })
        row['stores'][stock['loja']] = {
            'quantidade': stock['quantidade'],
            'data': stock['data'],
        }

    for product in products:
        stock_matrix.setdefault(product, {
            'produto': product,
            'stores': {},
        })

    for row in stock_matrix.values():
        for store in stores:
            row['stores'].setdefault(store['name'], {
                'quantidade': None,
                'data': None,
            })

    return sorted(stock_matrix.values(), key=lambda row: row['produto'])


def _missing_sunday_counts(count_status):
    """Group unrecorded active product/store cells for display."""
    missing_by_store = []
    for store in count_status['stores']:
        products = [
            product['nome']
            for product in count_status['products']
            if product['counts'].get(store['id']) is None
        ]
        if products:
            missing_by_store.append({
                'store_id': store['id'],
                'store_name': store['name'],
                'products': products,
            })
    return missing_by_store


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


def _parse_sunday_count_matrix(config, form):
    values = []
    for product in config['products']:
        for store in config['stores']:
            raw = form.get(f"count_{product['id']}_{store['id']}")
            if raw is None or not raw.strip().isdigit():
                raise ValueError(
                    'Preencha todas as contagens com números inteiros não negativos.'
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


def _default_sunday():
    today = date.today()
    return today - timedelta(days=(today.weekday() + 1) % 7)


def _parse_sunday(value):
    try:
        selected = date.fromisoformat(str(value or ''))
    except (TypeError, ValueError):
        selected = _default_sunday()
    if selected.weekday() != 6:
        raise ValueError('Escolha um domingo para verificar as contagens.')
    return selected


def _parse_pastelaria_count_date(value):
    try:
        return date.fromisoformat(str(value or date.today().isoformat()))
    except (TypeError, ValueError):
        raise ValueError('Escolha uma data válida para a contagem.') from None


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
        {
            'icon': icons.get(t['id']) or t['icon'],
            'label': labels.get(t['id']) or t['label'],
            'url': url_for(t['endpoint']),
            'description': t['description'],
        }
        for t in TABS
        if visibility.get(t['id'], True)
    ]
    return render_template('components/section_menu.html', items=items,
                           menu_title=f'🍰 {custom_mod}' if custom_mod else '🍰 Produção Pastelaria')


@pastelaria_bp.route('/stock-balcao', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def stock_balcao():
    if request.method == 'POST':
        if request.form.get('action') != 'gerar_plano':
            abort(405)
        return redirect(url_for(
            'pastelaria.planear',
            data_contagem=request.form.get('data_plano') or _default_sunday().isoformat(),
        ))

    stock_config = get_pastelaria_stock_minimums()
    ultimo_stock = get_ultimo_stock_balcao(AREA)
    stores = stock_config['stores']
    products = get_produtos_pastelaria() or []
    stock_matrix_list = _build_stock_matrix(ultimo_stock, products, stores)

    try:
        history_start = (
            date.fromisoformat(request.args['data_inicio'])
            if request.args.get('data_inicio') else None
        )
        history_end = (
            date.fromisoformat(request.args['data_fim'])
            if request.args.get('data_fim') else None
        )
        if history_start and history_end and history_start > history_end:
            raise ValueError
    except (TypeError, ValueError):
        flash('Intervalo de histórico inválido.', 'warning')
        history_start = history_end = None
    contagens = get_contagem_stock_df(
        AREA, data_inicio=history_start, data_fim=history_end,
    )
    history_store = request.args.get('loja', '').strip()
    if history_store and not contagens.empty:
        contagens = contagens[contagens['loja'] == history_store]
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

    try:
        status_date = _parse_sunday(request.args.get('data_estado'))
        count_status = get_pastelaria_sunday_count_grid(status_date)
    except ValueError as exc:
        status_date = _default_sunday()
        count_status = get_pastelaria_sunday_count_grid(status_date)
        count_status['error'] = str(exc)
    store_status = []
    for store in count_status['stores']:
        store_id = store['id']
        completed = count_status['completed_by_store'].get(store_id, 0)
        total = count_status['total_by_store'].get(store_id, 0)
        store_status.append({
            'id': store_id,
            'name': store['name'],
            'completed': completed,
            'total': total,
            'complete': total > 0 and completed == total,
        })
    missing_counts = _missing_sunday_counts(count_status)

    return render_template('pastelaria/stock_balcao.html',
                           active_tab='stock_balcao',
                           tabs=_tabs_with_urls(),
                           produtos=products,
                           stock_matrix=stock_matrix_list,
                           contagens=contagens_list,
                           stock_stores=stores,
                           history_start=history_start,
                           history_end=history_end,
                           history_store=history_store,
                           status_date=status_date,
                           count_status=count_status,
                           store_status=store_status,
                           missing_counts=missing_counts)


@pastelaria_bp.route('/contagem-stock', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def contagem_stock():
    """Let a Pastelaria user count any active sales store on any date."""
    stores = get_pastelaria_stock_minimums()['stores']
    store_ids = {int(store['id']) for store in stores}
    raw_store_id = request.values.get('loja_id', '').strip()
    raw_count_date = request.values.get('data_contagem', '')
    selected_store_id = None
    count_date = date.today()
    grid = None
    error = None
    date_error = False

    try:
        count_date = _parse_pastelaria_count_date(raw_count_date)
    except ValueError as exc:
        error = str(exc)
        date_error = True

    if raw_store_id:
        try:
            selected_store_id = int(raw_store_id)
        except (TypeError, ValueError):
            error = 'Loja inválida.'
    elif stores:
        selected_store_id = int(stores[0]['id'])

    if selected_store_id is not None and selected_store_id not in store_ids:
        error = 'Loja inválida ou inativa.'
    if not stores:
        error = 'Não existem lojas ativas disponíveis para contagem.'

    if not error and selected_store_id is not None:
        try:
            grid = get_pastelaria_store_count_grid(
                count_date, selected_store_id, allow_non_sunday=True,
            )
        except (TypeError, ValueError) as exc:
            error = str(exc)

    if request.method == 'POST' and not error and grid:
        values = []
        try:
            for product in grid['products']:
                field = f"count_{product['id']}"
                if field not in request.form:
                    raise ValueError(
                        'Preencha todas as contagens com números inteiros não negativos.'
                    )
                raw_quantity = request.form[field].strip()
                if raw_quantity and not raw_quantity.isdigit():
                    raise ValueError(
                        'Preencha todas as contagens com números inteiros não negativos.'
                    )
                values.append((product['id'], int(raw_quantity) if raw_quantity else 0))
            saved = save_pastelaria_store_counts(
                count_date,
                selected_store_id,
                values,
                request.form.get('snapshot_token'),
                allow_non_sunday=True,
                submitted_by=session.get('user', {}).get('username', ''),
            )
            store_name = grid['store']['name']
            flash(
                f'Contagem de {store_name} guardada: {saved} valores.',
                'success',
            )
            return redirect(url_for(
                'pastelaria.contagem_stock',
                loja_id=selected_store_id,
                data_contagem=count_date.isoformat(),
            ))
        except (TypeError, ValueError) as exc:
            error = str(exc)
            try:
                grid = get_pastelaria_store_count_grid(
                    count_date, selected_store_id, allow_non_sunday=True,
                )
            except (TypeError, ValueError):
                grid = None
            submitted = dict(values)
            for product in (grid or {}).get('products', []):
                if product['id'] in submitted:
                    product['count'] = submitted[product['id']]

    return render_template(
        'pastelaria/contagem_stock.html',
        active_tab='stock_balcao',
        tabs=_tabs_with_urls(),
        stores=stores,
        selected_store_id=selected_store_id,
        count_date=count_date,
        grid=grid,
        error=error,
        date_error=date_error,
    )


@pastelaria_bp.route('/inteligencia')
@perm_required('acesso_pastelaria')
def inteligencia():
    today = date.today()
    try:
        data_inicio = date.fromisoformat(request.args.get('inicio', ''))
    except ValueError:
        data_inicio = today - timedelta(days=365)
    try:
        data_fim = date.fromisoformat(request.args.get('fim', ''))
    except ValueError:
        data_fim = today
    filters = {
        'loja': request.args.get('loja', '').strip(),
        'produto': request.args.get('produto', '').strip(),
        'tipologia': request.args.get('tipologia', '').strip(),
        'tipologia_venda': request.args.get('tipologia_venda', '').strip(),
        'estado': request.args.get('estado', 'todos').strip(),
    }
    error = None
    try:
        intelligence = get_pastelaria_intelligence(
            data_inicio, data_fim, **filters
        )
    except ValueError as exc:
        error = str(exc)
        data_inicio = today - timedelta(days=365)
        data_fim = today
        filters = {
            'loja': '', 'produto': '', 'tipologia': '',
            'tipologia_venda': '', 'estado': 'todos',
        }
        intelligence = get_pastelaria_intelligence(data_inicio, data_fim)
    rotation_daily = {}
    for row in intelligence['intervals']:
        key = row['fim'].isoformat()
        rotation_daily[key] = (
            rotation_daily.get(key, 0) + row['consumo_estimado']
        )
    chart_figure = {
        'data': [
            {
                'type': 'scatter', 'mode': 'lines+markers',
                'name': 'Vendas Pastelaria (€)',
                'x': [row['data'].isoformat() for row in intelligence['sales_series']],
                'y': [row['valor'] for row in intelligence['sales_series']],
                'line': {'color': '#197f78', 'width': 2},
                'hovertemplate': '%{x}<br>%{y:.2f} €<extra></extra>',
            },
            {
                'type': 'bar', 'name': 'Consumo estimado (un.)',
                'x': list(rotation_daily),
                'y': list(rotation_daily.values()),
                'marker': {'color': '#d89a57'},
                'yaxis': 'y2',
                'hovertemplate': '%{x}<br>%{y} un.<extra></extra>',
            },
        ],
        'layout': {
            'legend': {'orientation': 'h', 'y': 1.12},
            'yaxis': {'title': 'Vendas (€)', 'rangemode': 'tozero'},
            'yaxis2': {
                'title': 'Consumo estimado (un.)', 'overlaying': 'y',
                'side': 'right', 'rangemode': 'tozero',
            },
            'barmode': 'group',
        },
    }
    return render_template(
        'pastelaria/inteligencia.html',
        active_tab='inteligencia',
        tabs=_tabs_with_urls(),
        intelligence=intelligence,
        dashboard_json=json.dumps(chart_figure),
        data_inicio=data_inicio,
        data_fim=data_fim,
        filters=filters,
        error=error,
    )


@pastelaria_bp.route('/planear', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def planear():
    msg = None
    msg_type = None
    raw_date = request.form.get('data_contagem') if request.method == 'POST' else request.args.get('data_contagem')
    date_error = False
    try:
        count_date = _parse_sunday(raw_date)
    except ValueError as exc:
        date_error = bool(raw_date)
        count_date = _default_sunday()
        msg, msg_type = str(exc), 'warning'

    if request.method == 'POST' and not date_error:
        action = request.form.get('action', '')
        if action == 'gerar_plano':
            try:
                plan_id = generate_pastelaria_priority_plan(
                    count_date, (session.get('user') or {}).get('username'),
                )
                return redirect(url_for('pastelaria.plano_prioridade', plan_id=plan_id))
            except (TypeError, ValueError) as exc:
                msg, msg_type = str(exc), 'warning'

    try:
        priority_status = get_pastelaria_priority_status(count_date)
    except (TypeError, ValueError) as exc:
        if date_error:
            priority_status = get_pastelaria_priority_status(_default_sunday())
        else:
            priority_status = {
                'complete': False,
                'rows': [],
                'missing': [],
                'missing_minimums': [],
            }
        msg, msg_type = str(exc), 'warning'
    history = list_pastelaria_priority_plans()
    return render_template(
        'pastelaria/planear.html',
        active_tab='planear',
        tabs=_tabs_with_urls(),
        count_date=count_date,
        priority_status=priority_status,
        history=history,
        msg=msg,
        msg_type=msg_type,
    )


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


@pastelaria_bp.route('/stock-producao', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def stock_producao():
    if request.method == 'POST':
        identity_key = request.form.get('identity_key', '').strip()
        tipo = request.form.get('tipo', '').strip()
        data_str = request.form.get('data', '').strip()
        motivo = request.form.get('motivo', '').strip()
        quantidade_str = request.form.get('quantidade', '').strip()
        try:
            data_movimento = date.fromisoformat(data_str)
        except ValueError:
            flash('Indique uma data válida para o movimento.', 'error')
            return redirect(url_for('pastelaria.stock_producao'))
        try:
            quantidade = int(quantidade_str)
        except (TypeError, ValueError):
            flash('A quantidade tem de ser um número inteiro.', 'error')
            return redirect(url_for('pastelaria.stock_producao'))

        try:
            result = register_pastelaria_stock_movement(
                identity_key=identity_key,
                tipo=tipo,
                quantidade=quantidade,
                data=data_movimento,
                responsavel=session.get('user', {}).get('username', ''),
                motivo=motivo,
            )
        except PastelariaStockError as exc:
            flash(str(exc), 'error')
            return redirect(url_for('pastelaria.stock_producao'))

        flash(
            f"Movimento guardado. Saldo confirmado de {result['produto']}: "
            f"{result['saldo']}.",
            'success',
        )
        return redirect(url_for('pastelaria.stock_producao'))

    return render_template(
        'pastelaria/stock_producao.html',
        active_tab='stock_producao',
        tabs=_tabs_with_urls(),
        options=get_pastelaria_stock_options(),
        movements=get_pastelaria_stock_movements(),
        today=date.today().isoformat(),
    )


@pastelaria_bp.route('/transferir', methods=['GET', 'POST'])
@perm_required('acesso_pastelaria')
def transferir():
    today = date.today()

    if request.method == 'POST':
        username = session.get('user', {}).get('username', '')
        request_key = request.form.get('request_key', '').strip()
        if not request_key:
            flash('Formulário de transferência inválido. Atualize a página.', 'error')
            return redirect(url_for('pastelaria.transferir'))

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
        invalid_lines = False
        for _, produto, qty_str in sorted(form_pairs, key=lambda x: x[0]):
            produto = (produto or '').strip()
            qty_str = (qty_str or '').strip()
            if not produto and not qty_str:
                continue
            if not produto or not qty_str:
                invalid_lines = True
                break
            try:
                qty = int(qty_str)
            except (TypeError, ValueError):
                invalid_lines = True
                break
            if qty <= 0:
                invalid_lines = True
                break
            requested.append({'identity_key': produto, 'quantidade': qty})
        if invalid_lines:
            flash(
                'Cada linha preenchida tem de incluir um artigo e uma '
                'quantidade inteira superior a zero.',
                'error',
            )
            return redirect(url_for(
                'pastelaria.transferir',
                data_prevista=data_prevista.isoformat(),
            ))
        if not requested:
            flash('Indique pelo menos um artigo e uma quantidade.', 'info')
            return redirect(url_for(
                'pastelaria.transferir',
                data_prevista=data_prevista.isoformat(),
            ))

        configured_products = {
            produto for produto in get_produtos_pastelaria()
            if produto.strip().casefold() != 'bolo'
        }
        persisted_plan_products = {
            row['produto'] for row in get_plano_do_dia_area(AREA, data_prevista)
        }
        cake_enabled = any(
            product.strip().casefold() == 'bolo individual'
            for product in configured_products
        )
        active_cake_configurations = {
            product for product in persisted_plan_products
            if cake_enabled and product.startswith('Bolo — ')
        }
        stock_options = get_pastelaria_stock_options()
        options_by_key = {
            item['identity_key']: item for item in stock_options
        }
        allowed_keys = {
            item['identity_key']
            for item in stock_options
            if item['ativo']
            and item['kind'] == 'catalogue'
            and item['produto'] in configured_products
        }
        allowed_keys.update(
            item['identity_key']
            for item in stock_options
            if item['ativo']
            and item['kind'] == 'cake'
            and item['produto'] in active_cake_configurations
        )
        invalid_keys = sorted({
            line['identity_key'] for line in requested
            if line['identity_key'] not in allowed_keys
        })
        if invalid_keys:
            invalid_products = [
                options_by_key[key]['produto'] if key in options_by_key else key
                for key in invalid_keys
            ]
            flash(
                'Artigo inválido ou não planeado para a data prevista: '
                + ', '.join(invalid_products),
                'error',
            )
            return redirect(url_for(
                'pastelaria.transferir',
                data_prevista=data_prevista.isoformat(),
            ))

        try:
            result = criar_ordens_transferencia_pastelaria(
                data=today,
                loja_destino=loja_destino,
                lines=requested,
                criado_por=username,
                data_prevista=data_prevista,
                request_key=request_key,
            )
        except PastelariaStockError as exc:
            flash(str(exc), 'error')
            return redirect(url_for(
                'pastelaria.transferir',
                data_prevista=data_prevista.isoformat(),
            ))

        order_count = len(result['order_ids'])
        if result['replayed']:
            flash(
                f"Esta submissão já tinha sido processada "
                f"({order_count} ordem(ns)); o stock não foi debitado novamente.",
                'info',
            )
        elif order_count:
            flash(
                f"{order_count} ordem(ns) de transferência criada(s)!",
                'success',
            )
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

    prod_map = {}
    for stock in stock_prod:
        label = stock['produto']
        prod_map[label] = prod_map.get(label, 0) + stock['quantidade']

    plano_data_prevista = get_plano_do_dia_area(AREA, data_prevista)
    configured_products = [
        produto for produto in get_produtos_pastelaria()
        if produto.strip().casefold() != 'bolo'
    ]
    cake_enabled = any(
        product.strip().casefold() == 'bolo individual'
        for product in configured_products
    )
    active_cake_configurations = {
        row['produto'] for row in plano_data_prevista
        if cake_enabled and row['produto'].startswith('Bolo — ')
    }
    stock_options = get_pastelaria_stock_options()
    options_by_label = {}
    for option in stock_options:
        options_by_label.setdefault(option['produto'], []).append(option)

    transfer_options = [
        item for item in stock_options
        if item['ativo']
        and item['kind'] == 'catalogue'
        and item['produto'] in set(configured_products)
    ]
    transfer_options.extend(
        item for item in stock_options
        if item['ativo']
        and item['kind'] == 'cake'
        and item['produto'] in active_cake_configurations
    )
    transfer_options.sort(
        key=lambda item: (item['produto'].casefold(), item['identity_key'])
    )

    duplicate_labels = {
        label for label, items in options_by_label.items()
        if len(items) > 1
    }
    cards = []
    displayed_labels = set()
    for item in stock_options:
        label = item['produto']
        if not label:
            continue
        balcao = balcao_map.get(label, {})
        balcao_mat = balcao.get('Matosinhos', {})
        balcao_bol = balcao.get('Bolhão', {})
        data_mat = balcao_mat.get('data')
        data_bol = balcao_bol.get('data')
        identity_hint = ''
        if label in duplicate_labels and item.get('produto_pastelaria_id'):
            identity_hint = f"ID {item['produto_pastelaria_id']}"
        cards.append({
            'produto': label,
            'identity_hint': identity_hint,
            'identity_key': item['identity_key'],
            'stock_prod': prod_map.get(label, 0),
            'saldo_producao': item['saldo'],
            'saldo_confirmado': item['saldo_inicial_confirmado'],
            'balcao_matosinhos': balcao_mat.get('quantidade', 0),
            'balcao_bolhao': balcao_bol.get('quantidade', 0),
            'data_balcao_matosinhos': data_mat.strftime('%d/%m') if data_mat else '-',
            'data_balcao_bolhao': data_bol.strftime('%d/%m') if data_bol else '-',
        })
        displayed_labels.add(label)

    reference_labels = set(prod_map)
    physical_labels = set(balcao_map)
    legacy_only_labels = (
        reference_labels | physical_labels
        | set(configured_products)
        | active_cake_configurations
        | {row['produto'] for row in plano_data_prevista}
    ) - displayed_labels
    for label in sorted(legacy_only_labels):
        balcao = balcao_map.get(label, {})
        balcao_mat = balcao.get('Matosinhos', {})
        balcao_bol = balcao.get('Bolhão', {})
        data_mat = balcao_mat.get('data')
        data_bol = balcao_bol.get('data')
        cards.append({
            'produto': label,
            'identity_hint': '',
            'identity_key': '',
            'stock_prod': prod_map.get(label, 0),
            'saldo_producao': 0,
            'saldo_confirmado': False,
            'balcao_matosinhos': balcao_mat.get('quantidade', 0),
            'balcao_bolhao': balcao_bol.get('quantidade', 0),
            'data_balcao_matosinhos': data_mat.strftime('%d/%m') if data_mat else '-',
            'data_balcao_bolhao': data_bol.strftime('%d/%m') if data_bol else '-',
        })
    cards.sort(key=lambda item: (item['produto'].casefold(), item['identity_hint']))

    cards_transferivel = [
        {
            **item,
            'pode_transferir': (
                item['saldo_inicial_confirmado'] and item['saldo'] > 0
            ),
            'identity_hint': (
                f" — ID {item['produto_pastelaria_id']}"
                if item['produto'] in duplicate_labels
                and item.get('produto_pastelaria_id')
                else ''
            ),
        }
        for item in transfer_options
    ]

    return render_template('pastelaria/transferir.html',
                           active_tab='transferir',
                           tabs=_tabs_with_urls(),
                           cards=cards,
                           cards_transferivel=cards_transferivel,
                           lojas_venda=lojas_venda,
                           data_prevista=data_prevista.isoformat(),
                           request_key=str(uuid4()))


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
            config = get_pastelaria_stock_minimums()
            try:
                values = _parse_stock_minimum_matrix(config, request.form)
                save_pastelaria_stock_minimums(values)
                flash('Stocks mínimos guardados.', 'success')
            except ValueError as exc:
                flash(str(exc), 'warning')
        elif action == 'save_product_states':
            products = db.get_all_produtos_pastelaria()
            try:
                states = []
                for product in products:
                    value = request.form.get(f"state_{product['id']}")
                    if value not in {'active', 'inactive'}:
                        raise ValueError(
                            'Escolha Ativo ou Inativo para todos os produtos.'
                        )
                    states.append((product['id'], value == 'active'))
                actor = session.get('user') or {}
                save_args = [states, request.form.get('state_token', '')]
                # Authenticated sessions always include the database user id.
                # Keep the old two-argument call compatible with lightweight
                # legacy/test sessions that predate that session field.
                if actor.get('id') is not None:
                    save_args.extend([
                        actor.get('id'),
                        actor.get('username') or actor.get('nome'),
                    ])
                changed = db.save_produtos_pastelaria_active(*save_args)
                flash(
                    f'Estado guardado em {changed} produto(s).',
                    'success',
                )
            except ValueError as exc:
                flash(str(exc), 'warning')
        elif action == 'add_produto_past':
            tip = request.form.get('novo_tipologia', '')
            sabor_choice = request.form.get('novo_sabor_past', '')
            cob = request.form.get('novo_cob_past', '')
            if tip:
                try:
                    if sabor_choice == PASTELARIA_CUSTOM_FLAVOUR_OPTION:
                        sabor = request.form.get(
                            'novo_sabor_past_custom', ''
                        ).strip()
                        if not sabor:
                            raise ValueError(
                                'Indique o nome do sabor excecional.'
                            )
                        if len(sabor) > 255:
                            raise ValueError(
                                'O sabor não pode exceder 255 caracteres.'
                            )
                    elif sabor_choice:
                        if sabor_choice not in db.get_sabores_list():
                            raise ValueError(
                                'Escolha um sabor ativo ou a opção '
                                '«Outro sabor (exceção)».'
                            )
                        sabor = sabor_choice
                    else:
                        sabor = ''
                except ValueError as exc:
                    flash(str(exc), 'warning')
                else:
                    success = db.add_produto_pastelaria(tip, sabor, cob)
                    flash(
                        'Produto adicionado!' if success
                        else 'Produto já existe.',
                        'success' if success else 'warning',
                    )
            else:
                flash('Por favor, selecione uma tipologia.', 'warning')
        elif action == 'delete_bulk_produtos_past':
            # Keep the legacy action as a protected no-op. Product removal is
            # deliberately reversible so minimums and historical records stay
            # attached to the catalogue identity.
            flash(
                'A eliminação permanente de produtos está bloqueada. '
                'Marque o produto como Inativo.',
                'warning',
            )
        return redirect(url_for('pastelaria.produtos'))
    minimum_config = get_pastelaria_stock_minimums()
    products = db.get_all_produtos_pastelaria()
    return render_template('pastelaria/lista_produtos.html',
                           produtos_past=products,
                           tipologias_list=[t['nome'] for t in db.get_all_tipologias_pastelaria()],
                           sabores_list=db.get_sabores_list(),
                           coberturas_list=[c['nome'] for c in db.get_all_coberturas()],
                           minimum_config=minimum_config,
                           state_change_history=db.get_pastelaria_product_state_history(),
                           state_token=db.pastelaria_product_state_token(
                               products
                            ),
                            custom_flavour_option=(
                                PASTELARIA_CUSTOM_FLAVOUR_OPTION
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
                set_typology_dose(
                    nome,
                    qtd_new,
                    session.get('user', {}).get('username', 'sistema'),
                    source='Pastelaria',
                )
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
        active_products = set(get_produtos_pastelaria() or [])
        if produto not in active_products:
            flash(
                'Produto inválido ou inativo. Atualize a página e tente novamente.',
                'error',
            )
        elif quantidade > 0:
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
