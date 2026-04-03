from flask import Blueprint, render_template, url_for, request, redirect, flash, session
from flask_app.auth import perm_required
from datetime import date
from collections import defaultdict
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import get_ordens_transferencia
import db.materiais as mat_db

logistica_bp = Blueprint('logistica', __name__)

TABS = [
    {'id': 'transferencias', 'label': 'Transferências Agendadas', 'icon': '📅', 'url_endpoint': 'logistica.transferencias_agendadas'},
    {'id': 'ordens', 'label': 'Ordens de Transferência', 'icon': '📄', 'url_endpoint': 'logistica.ordens'},
    {'id': 'stock_materiais', 'label': 'Stock de Materiais', 'icon': '📦', 'url_endpoint': 'logistica.stock_materiais'},
    {'id': 'historico_movimentos', 'label': 'Histórico de Movimentos', 'icon': '🕒', 'url_endpoint': 'logistica.historico_movimentos'},
]


@logistica_bp.route('/')
@perm_required('acesso_administrativo')
def index():
    items = [{'icon': t['icon'], 'label': t['label'], 'url': url_for(t['url_endpoint'])} for t in TABS]
    return render_template('components/section_menu.html', items=items)


@logistica_bp.route('/transferencias-agendadas')
@perm_required('acesso_administrativo')
def transferencias_agendadas():
    todas = get_ordens_transferencia()
    grupos = defaultdict(list)
    for o in todas:
        dp = o.get('data_prevista') or o['data']
        key = (dp, o['loja_destino'])
        grupos[key].append(o)

    transferencias = []
    for (dp, loja), ordens in sorted(grupos.items(), key=lambda x: x[0][0], reverse=True):
        n_pendentes = sum(1 for o in ordens if o['status'] == 'pendente')
        n_confirmadas = sum(1 for o in ordens if o['status'] == 'confirmada')
        n_rejeitadas = sum(1 for o in ordens if o['status'] == 'rejeitada')
        areas = sorted(set(o['area_origem'] for o in ordens))
        transferencias.append({
            'data_prevista': dp,
            'loja_destino': loja,
            'ordens': ordens,
            'n_total': len(ordens),
            'n_pendentes': n_pendentes,
            'n_confirmadas': n_confirmadas,
            'n_rejeitadas': n_rejeitadas,
            'areas': areas,
        })

    return render_template('logistica/transferencias_agendadas.html',
                           transferencias=transferencias)


@logistica_bp.route('/ordens')
@perm_required('acesso_administrativo')
def ordens():
    todas_ordens = get_ordens_transferencia()
    return render_template('logistica/ordens.html', ordens=todas_ordens)


# ── Stock de Materiais ──────────────────────────────────────────────────────────

@logistica_bp.route('/stock-materiais', methods=['GET'])
@perm_required('acesso_administrativo')
def stock_materiais():
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
        local=local,
        locais=mat_db.LOCAIS_STOCK,
        categorias=mat_db.CATEGORIAS_MATERIAIS,
        por_categoria=por_categoria,
        stock=stock,
        modo='ver',
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
        local=local,
        locais=mat_db.LOCAIS_STOCK,
        categorias=mat_db.CATEGORIAS_MATERIAIS,
        por_categoria=por_categoria,
        stock=stock,
        modo='contagem',
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


# ── Histórico de Movimentos ─────────────────────────────────────────────────────

@logistica_bp.route('/historico-movimentos', methods=['GET'])
@perm_required('acesso_administrativo')
def historico_movimentos():
    local = request.args.get('local', '') or None
    tipo = request.args.get('tipo', '') or None

    if local and local not in mat_db.LOCAIS_STOCK:
        local = None

    TIPOS_VALIDOS = ('entrada', 'saida', 'contagem', 'ajuste')
    if tipo and tipo not in TIPOS_VALIDOS:
        tipo = None

    PER_PAGE = 50
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1

    total = mat_db.get_movimentos_stock_count(local=local, tipo=tipo)
    total_pages = max(1, -(-total // PER_PAGE))  # ceiling division
    page = min(page, total_pages)
    offset = (page - 1) * PER_PAGE

    movimentos = mat_db.get_movimentos_stock(
        local=local, tipo=tipo, limit=PER_PAGE, offset=offset
    )

    return render_template(
        'logistica/historico_movimentos.html',
        movimentos=movimentos,
        local=local or '',
        tipo=tipo or '',
        locais=mat_db.LOCAIS_STOCK,
        tipos=TIPOS_VALIDOS,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=PER_PAGE,
    )
