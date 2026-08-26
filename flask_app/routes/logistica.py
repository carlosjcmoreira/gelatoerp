from flask import Blueprint, render_template, url_for, request, redirect, flash, session
from flask_app.auth import perm_required
from datetime import date, datetime
from collections import defaultdict
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import get_ordens_transferencia
from db.plano import get_ordens_transferencia_with_events
from db.artigos import get_artigos_administrativos
import db.materiais as mat_db

logistica_bp = Blueprint('logistica', __name__)

TABS = [
    {'id': 'transferencias', 'label': 'Transferências', 'icon': '🚚', 'url_endpoint': 'logistica.transferencias'},
    {'id': 'stock_materiais', 'label': 'Stock de Materiais', 'icon': '📦', 'url_endpoint': 'logistica.stock_materiais'},
]


@logistica_bp.route('/')
@perm_required('acesso_administrativo')
def index():
    from db.tiles import get_tile_visibility, get_tile_labels, get_tile_icons, get_module_labels
    visibility = get_tile_visibility('logistica')
    labels = get_tile_labels('logistica')
    icons = get_tile_icons('logistica')
    custom_mod = get_module_labels().get('logistica')
    items = [
        {'icon': icons.get(t['id']) or t['icon'], 'label': labels.get(t['id']) or t['label'], 'url': url_for(t['url_endpoint'])}
        for t in TABS
        if visibility.get(t['id'], True)
    ]
    return render_template('components/section_menu.html', items=items,
                           menu_title=f'🚚 {custom_mod}' if custom_mod else '🚚 Logística')


# ── Transferências (unified: Ativas + Histórico) ────────────────────────────────

@logistica_bp.route('/transferencias')
@perm_required('acesso_administrativo')
def transferencias():
    tab = request.args.get('tab', 'ativas')
    if tab not in ('ativas', 'historico'):
        tab = 'ativas'

    # ── "Ativas" tab data ──────────────────────────────────────────────────────
    transferencias_data = []
    if tab == 'ativas':
        todas = get_ordens_transferencia()
        grupos = defaultdict(list)
        for o in todas:
            dp = o.get('data_prevista') or o['data']
            destino_label = (
                f"B2B · {o['destino_nome']}"
                if o.get('destino_tipo') == 'b2b'
                else o['loja_destino']
            )
            key = (dp, destino_label)
            grupos[key].append(o)

        for (dp, loja), ordens in sorted(grupos.items(), key=lambda x: x[0][0], reverse=True):
            n_pendentes = sum(1 for o in ordens if o['status'] == 'pendente')
            n_confirmadas = sum(1 for o in ordens if o['status'] == 'confirmada')
            n_rejeitadas = sum(1 for o in ordens if o['status'] == 'rejeitada')
            areas = sorted(set(o['area_origem'] for o in ordens))
            transferencias_data.append({
                'data_prevista': dp,
                'loja_destino': loja,
                'ordens': ordens,
                'n_total': len(ordens),
                'n_pendentes': n_pendentes,
                'n_confirmadas': n_confirmadas,
                'n_rejeitadas': n_rejeitadas,
                'areas': areas,
            })

    # ── "Histórico" tab data ───────────────────────────────────────────────────
    ordens_result = {'ordens': [], 'total': 0, 'page': 1, 'per_page': 50, 'total_pages': 1}
    areas = ['Gelado', 'Pastelaria', 'Confeitaria', 'Compras']
    statuses = [('pendente', 'Pendente'), ('confirmada', 'Confirmada'), ('rejeitada', 'Rejeitada')]
    lojas = []
    filtro_area = ''
    filtro_status = ''
    filtro_loja = ''
    filtro_data_inicio = ''
    filtro_data_fim = ''

    if tab == 'historico':
        area_origem = request.args.get('area_origem', '').strip() or None
        status = request.args.get('status', '').strip() or None
        loja_destino = request.args.get('loja_destino', '').strip() or None
        data_inicio_str = request.args.get('data_inicio', '').strip()
        data_fim_str = request.args.get('data_fim', '').strip()

        filtro_area = area_origem or ''
        filtro_status = status or ''
        filtro_loja = loja_destino or ''
        filtro_data_inicio = data_inicio_str
        filtro_data_fim = data_fim_str

        data_inicio = None
        data_fim = None
        try:
            if data_inicio_str:
                data_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
        except ValueError:
            pass
        try:
            if data_fim_str:
                data_fim = datetime.strptime(data_fim_str, '%Y-%m-%d').date()
        except ValueError:
            pass

        PER_PAGE = 50
        try:
            page = max(1, int(request.args.get('page', 1)))
        except (ValueError, TypeError):
            page = 1

        ordens_result = get_ordens_transferencia_with_events(
            status=status,
            loja_destino=loja_destino,
            area_origem=area_origem,
            data_inicio=data_inicio,
            data_fim=data_fim,
            page=page,
            per_page=PER_PAGE,
        )
        todas_ordens = get_ordens_transferencia()
        lojas = sorted({
            o['loja_destino']
            for o in todas_ordens
            if o.get('destino_tipo') != 'b2b'
        })
        if any(o.get('destino_tipo') == 'b2b' for o in todas_ordens):
            lojas.append('B2B')

    return render_template(
        'logistica/transferencias.html',
        tab=tab,
        transferencias=transferencias_data,
        ordens=ordens_result['ordens'],
        total=ordens_result['total'],
        page=ordens_result['page'],
        per_page=ordens_result['per_page'],
        total_pages=ordens_result['total_pages'],
        areas=areas,
        lojas=lojas,
        statuses=statuses,
        filtro_area=filtro_area,
        filtro_status=filtro_status,
        filtro_loja=filtro_loja,
        filtro_data_inicio=filtro_data_inicio,
        filtro_data_fim=filtro_data_fim,
    )


# ── Backwards-compatible redirects ─────────────────────────────────────────────

@logistica_bp.route('/transferencias-agendadas')
@perm_required('acesso_administrativo')
def transferencias_agendadas():
    return redirect(url_for('logistica.transferencias', tab='ativas'), 302)


@logistica_bp.route('/ordens')
@perm_required('acesso_administrativo')
def ordens():
    args = dict(request.args)
    args['tab'] = 'historico'
    return redirect(url_for('logistica.transferencias', **args), 302)


# ── Stock de Materiais (unified: Stock Atual + Histórico + Catálogo) ───────────

@logistica_bp.route('/stock-materiais', methods=['GET'])
@perm_required('acesso_administrativo')
def stock_materiais():
    tab = request.args.get('tab', 'stock')
    if tab not in ('stock', 'historico'):
        tab = 'stock'

    local = request.args.get('local', mat_db.LOCAIS_STOCK[0])
    if local not in mat_db.LOCAIS_STOCK:
        local = mat_db.LOCAIS_STOCK[0]

    stock = mat_db.get_stock_atual(local)
    por_categoria = {}
    for item in stock:
        cat = item['categoria']
        por_categoria.setdefault(cat, []).append(item)

    # Catálogo de artigos de fornecimento
    artigos_raw = get_artigos_administrativos(apenas_ativos=True)
    artigos_por_fornecedor = {}
    for a in artigos_raw:
        artigos_por_fornecedor.setdefault(a['fornecedor'], []).append(a['produto'])

    # Histórico data
    movimentos = []
    mov_total = 0
    mov_page = 1
    mov_total_pages = 1
    filtro_local_hist = ''
    filtro_tipo_hist = ''
    TIPOS_VALIDOS = ('entrada', 'saida', 'contagem', 'ajuste')

    if tab == 'historico':
        filtro_local_hist = request.args.get('local_hist', '') or None
        filtro_tipo_hist = request.args.get('tipo', '') or None

        if filtro_local_hist and filtro_local_hist not in mat_db.LOCAIS_STOCK:
            filtro_local_hist = None
        if filtro_tipo_hist and filtro_tipo_hist not in TIPOS_VALIDOS:
            filtro_tipo_hist = None

        PER_PAGE_MOV = 50
        try:
            mov_page = max(1, int(request.args.get('page', 1)))
        except (ValueError, TypeError):
            mov_page = 1

        mov_total = mat_db.get_movimentos_stock_count(local=filtro_local_hist, tipo=filtro_tipo_hist)
        mov_total_pages = max(1, -(-mov_total // PER_PAGE_MOV))
        mov_page = min(mov_page, mov_total_pages)
        offset = (mov_page - 1) * PER_PAGE_MOV

        movimentos = mat_db.get_movimentos_stock(
            local=filtro_local_hist, tipo=filtro_tipo_hist,
            limit=PER_PAGE_MOV, offset=offset
        )

    return render_template(
        'logistica/stock_materiais.html',
        tab=tab,
        local=local,
        locais=mat_db.LOCAIS_STOCK,
        categorias=mat_db.CATEGORIAS_MATERIAIS,
        por_categoria=por_categoria,
        stock=stock,
        modo='ver',
        artigos_por_fornecedor=artigos_por_fornecedor,
        movimentos=movimentos,
        mov_total=mov_total,
        mov_page=mov_page,
        mov_total_pages=mov_total_pages,
        filtro_local_hist=filtro_local_hist or '',
        filtro_tipo_hist=filtro_tipo_hist or '',
        tipos=TIPOS_VALIDOS,
    )


@logistica_bp.route('/stock-materiais/contagem', methods=['GET'])
@perm_required('acesso_administrativo')
def stock_materiais_contagem():
    local = request.args.get('local', mat_db.LOCAIS_STOCK[0])
    if local not in mat_db.LOCAIS_STOCK:
        local = mat_db.LOCAIS_STOCK[0]

    stock = mat_db.get_stock_atual(local)
    por_categoria = {}
    for item in stock:
        cat = item['categoria']
        por_categoria.setdefault(cat, []).append(item)

    return render_template(
        'logistica/stock_materiais.html',
        tab='stock',
        local=local,
        locais=mat_db.LOCAIS_STOCK,
        categorias=mat_db.CATEGORIAS_MATERIAIS,
        por_categoria=por_categoria,
        stock=stock,
        modo='contagem',
        artigos_por_fornecedor={},
        movimentos=[],
        mov_total=0,
        mov_page=1,
        mov_total_pages=1,
        filtro_local_hist='',
        filtro_tipo_hist='',
        tipos=(),
    )


@logistica_bp.route('/stock-materiais/post', methods=['POST'])
@perm_required('acesso_administrativo')
def stock_materiais_post():
    utilizador = session.get('user', {}).get('username', 'system')
    action = request.form.get('action', '')
    local = request.form.get('local', '')

    if local not in mat_db.LOCAIS_STOCK:
        flash('Local inválido.', 'danger')
        return redirect(url_for('logistica.stock_materiais'))

    if action == 'registar_contagem':
        stock = mat_db.get_stock_atual(local)
        registados = 0
        for item in stock:
            mid = item['material_id']
            val = request.form.get(f'qtd_{mid}', '').strip()
            if val == '':
                continue
            try:
                qtd = float(val.replace(',', '.'))
            except ValueError:
                continue
            if qtd < 0:
                continue
            mat_db.add_movimento_stock(
                material_id=mid,
                local=local,
                tipo='contagem',
                quantidade=qtd,
                utilizador=utilizador,
                notas=request.form.get('notas_contagem', '').strip() or None,
            )
            registados += 1
        if registados:
            flash(f'Contagem registada para {registados} material(is) em {local}.', 'success')
        else:
            flash('Nenhum valor introduzido na contagem.', 'warning')

    elif action == 'ajuste':
        try:
            mid = int(request.form.get('material_id', 0) or 0)
        except (ValueError, TypeError):
            mid = 0
        tipo = request.form.get('tipo_ajuste', 'entrada')
        notas = request.form.get('notas_ajuste', '').strip()
        val = request.form.get('quantidade_ajuste', '').strip()

        if not mid:
            flash('Material inválido.', 'danger')
        elif tipo not in ('entrada', 'saida'):
            flash('Tipo de ajuste inválido.', 'danger')
        elif not notas:
            flash('A nota é obrigatória no ajuste manual.', 'warning')
        else:
            try:
                qtd = float(val.replace(',', '.'))
                if qtd <= 0:
                    raise ValueError
            except (ValueError, AttributeError):
                flash('Quantidade inválida (deve ser > 0).', 'warning')
            else:
                mat_db.add_movimento_stock(
                    material_id=mid,
                    local=local,
                    tipo=tipo,
                    quantidade=qtd,
                    utilizador=utilizador,
                    notas=notas,
                )
                flash(f'Ajuste de {tipo} registado.', 'success')

    return redirect(url_for('logistica.stock_materiais', local=local))


# ── Histórico de Movimentos (redirect to unified stock page) ───────────────────

@logistica_bp.route('/historico-movimentos', methods=['GET'])
@perm_required('acesso_administrativo')
def historico_movimentos():
    args = dict(request.args)
    args['tab'] = 'historico'
    if 'local' in args:
        args['local_hist'] = args.pop('local')
    return redirect(url_for('logistica.stock_materiais', **args), 302)
