from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import login_required
from datetime import date, datetime, timedelta
from collections import defaultdict
import sys, os, logging
logger = logging.getLogger(__name__)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_sabores_list, get_sabores_mapping, get_produtos_rececao,
    get_motivos_quebra, get_produtos_pastelaria, get_produtos_confeitaria,
    get_vendas_bolhao_dashboard_data,
    add_quebra, get_quebras_df, delete_quebra,
    add_stock_gelado, get_stock_gelado_df, delete_stock_gelado, update_stock_gelado,
    get_ordens_transferencia, confirmar_ordem_transferencia, rejeitar_ordem_transferencia,
    get_active_venda_stores, get_store_by_id,
    upsert_fecho_caixa, get_fecho_caixa, get_fecho_caixa_mensal, get_fecho_caixa_by_id, salvar_justificacao_fecho,
)

import flask_app.services.vendas as vendas_svc
from flask_app.services import ServiceError
from db.plano import get_ordem_transferencia_by_id

vendas_bp = Blueprint('vendas', __name__)

TAB_DEFS = [
    {'id': 'dashboard', 'label': 'Resumo Diário', 'icon': '📊', 'endpoint': 'vendas.dashboard'},
    {'id': 'transferencias', 'label': 'Receção de Mercadoria', 'icon': '📦', 'endpoint': 'vendas.transferencias'},
    {'id': 'quebras', 'label': 'Registar Quebras', 'icon': '⚠️', 'endpoint': 'vendas.quebras'},
    {'id': 'pesagem', 'label': 'Pesagem Fim de Dia', 'icon': '⚖️', 'endpoint': 'vendas.pesagem'},
    {'id': 'fecho_caixa', 'label': 'Fecho de Caixa', 'icon': '💵', 'endpoint': 'vendas.fecho_caixa'},
]


def _get_user_loja():
    """Resolve the store for the current session user.
    Gestor users may pass ?loja_id= query param to switch store.
    Returns (loja_id, loja_name)."""
    user = session.get('user', {})
    is_gestor = user.get('acesso_gestor')

    # Gestor may override via query param
    if is_gestor:
        override = request.args.get('loja_id') or request.form.get('_loja_id')
        if override:
            try:
                store = get_store_by_id(int(override))
                if store:
                    return store['id'], store['name']
            except (ValueError, TypeError):
                pass

    # Non-gestor: use first store from their vendas_store_ids, falling back to loja_id
    vendas_store_ids = user.get('vendas_store_ids') or []
    if vendas_store_ids:
        # Respect explicit loja_id query param if it's in the user's allowed stores
        override = request.args.get('loja_id') or request.form.get('_loja_id')
        if override:
            try:
                oid = int(override)
                if oid in vendas_store_ids:
                    store = get_store_by_id(oid)
                    if store:
                        return store['id'], store['name']
            except (ValueError, TypeError):
                pass
        store = get_store_by_id(vendas_store_ids[0])
        if store:
            return store['id'], store['name']

    if is_gestor:
        stores = get_active_venda_stores()
        if stores:
            return stores[0]['id'], stores[0]['name']

    return None, 'Bolhão'


def _build_tabs(active_id, loja_id=None):
    from db.tiles import get_tile_visibility
    visibility = get_tile_visibility('vendas')
    tabs = []
    for t in TAB_DEFS:
        if not visibility.get(t['id'], True):
            continue
        kwargs = {}
        if loja_id:
            kwargs['loja_id'] = loja_id
        tabs.append({
            'id': t['id'],
            'label': t['label'],
            'icon': t['icon'],
            'url': url_for(t['endpoint'], **kwargs),
            'active': t['id'] == active_id,
        })
    return tabs


def _check_vendas_access():
    """Return True if the current user has vendas or gestor access."""
    user = session.get('user', {})
    if user.get('acesso_gestor'):
        return True
    vendas_store_ids = user.get('vendas_store_ids') or []
    return len(vendas_store_ids) > 0


def _user_owns_loja(loja_nome):
    """Return True if user is gestor or their vendas_store_ids includes the store with loja_nome."""
    user = session.get('user', {})
    if user.get('acesso_gestor'):
        return True
    vendas_store_ids = user.get('vendas_store_ids') or []
    if not vendas_store_ids:
        return False
    stores = get_active_venda_stores()
    for store in stores:
        if store['name'] == loja_nome and store['id'] in vendas_store_ids:
            return True
    return False


@vendas_bp.route('/')
@login_required
def index():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))
    loja_id, loja_nome = _get_user_loja()
    kwargs = {'loja_id': loja_id} if loja_id else {}
    from db.tiles import get_tile_visibility
    visibility = get_tile_visibility('vendas')
    items = [
        {'icon': t['icon'], 'label': t['label'], 'url': url_for(t['endpoint'], **kwargs)}
        for t in TAB_DEFS
        if visibility.get(t['id'], True)
    ]
    return render_template('components/section_menu.html', items=items,
                           menu_title='🛍️ Vendas Bolhão')


@vendas_bp.route('/dashboard')
@login_required
def dashboard():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    loja_id, loja_nome = _get_user_loja()

    data = vendas_svc.build_dashboard_rows(loja_nome)

    return render_template('vendas/dashboard.html',
                           active_tab='dashboard',
                           tabs=_build_tabs('dashboard', loja_id),
                           loja_nome=loja_nome,
                           loja_id=loja_id,
                           rows=data['rows'],
                           total_ontem=data['total_ontem'],
                           total_recebido=data['total_recebido'],
                           total_fim=data['total_fim'])


@vendas_bp.route('/quebras', methods=['GET', 'POST'])
@login_required
def quebras():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    loja_id, loja_nome = _get_user_loja()
    sabores = get_sabores_list()
    motivos = get_motivos_quebra()
    produtos_past = get_produtos_pastelaria()
    produtos_conf = get_produtos_confeitaria()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            tipo = request.form.get('tipo_produto', 'Gelado')
            produto = request.form.get('produto', '')
            data_quebra_str = request.form.get('data', str(date.today()))
            data_quebra = datetime.strptime(data_quebra_str, '%Y-%m-%d').date()
            lote = request.form.get('lote', '') or ''
            motivo = request.form.get('motivo', '')
            try:
                quantidade = float(request.form.get('quantidade', '0').replace(',', '.'))
            except (ValueError, TypeError):
                quantidade = 0

            try:
                msg = vendas_svc.registar_quebra_venda(loja_nome, data_quebra, tipo,
                                                        produto, quantidade, lote, motivo)
                flash(msg, 'success')
            except ServiceError as e:
                flash(str(e), 'error')
            return redirect(url_for('vendas.quebras', loja_id=loja_id))

        elif action == 'delete':
            q_id = request.form.get('id')
            if q_id:
                try:
                    vendas_svc.apagar_quebra_venda(int(q_id), loja_nome)
                    flash('Registo eliminado!', 'success')
                except ServiceError as e:
                    flash(str(e), 'error')
            return redirect(url_for('vendas.quebras', loja_id=loja_id))

    quebras_df = get_quebras_df(loja_nome)
    historico = []
    if not quebras_df.empty:
        cols = ['id', 'data', 'sabor', 'lote', 'quantidade_kg', 'motivo']
        cols = [c for c in cols if c in quebras_df.columns]
        for _, r in quebras_df.iterrows():
            historico.append({c: r[c] for c in cols})

    return render_template('vendas/quebras.html',
                           active_tab='quebras',
                           tabs=_build_tabs('quebras', loja_id),
                           loja_nome=loja_nome,
                           loja_id=loja_id,
                           sabores=sabores,
                           motivos=motivos,
                           produtos_past=produtos_past,
                           produtos_conf=produtos_conf,
                           historico=historico,
                           today=str(date.today()))


@vendas_bp.route('/pesagem', methods=['GET', 'POST'])
@login_required
def pesagem():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    loja_id, loja_nome = _get_user_loja()
    sabores = get_sabores_list()
    mapping = get_sabores_mapping()
    reverse_mapping = {}
    for nome_receita, nome_corrente in mapping.items():
        reverse_mapping[nome_receita] = nome_corrente
        reverse_mapping[nome_corrente] = nome_corrente

    def _parse_date_form(key='data'):
        raw = request.form.get(key, '').strip()
        try:
            return datetime.strptime(raw, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return date.today()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            sabor = request.form.get('sabor', '')
            data_reg = _parse_date_form()
            def _parse_kg_str(s):
                try:
                    return float((s or '0').replace(',', '.'))
                except (ValueError, TypeError):
                    return 0.0
            qtds = request.form.getlist('quantidade[]')
            pesagem_kg = round(sum(_parse_kg_str(q) for q in qtds), 3)

            if pesagem_kg >= 0 and sabor:
                add_stock_gelado(data_reg, loja_nome, sabor, pesagem_kg, 'fim')
                flash(f'Pesagem de {pesagem_kg:.3f} kg de {sabor} registada!', 'success')
            else:
                flash('Insira um valor válido.', 'error')
            return redirect(url_for('vendas.pesagem', loja_id=loja_id, data=str(data_reg)))

        elif action == 'add_bulk':
            sabores_list = request.form.getlist('sabor[]')
            qtds_list = request.form.getlist('quantidade[]')
            data_reg = _parse_date_form()
            saved = 0
            for sabor, qtd_str in zip(sabores_list, qtds_list):
                sabor = sabor.strip()
                if not sabor:
                    continue
                try:
                    pesagem_kg = float(qtd_str.replace(',', '.'))
                except (ValueError, TypeError):
                    continue
                if pesagem_kg >= 0:
                    add_stock_gelado(data_reg, loja_nome, sabor, pesagem_kg, 'fim')
                    saved += 1
            if saved:
                flash(f'{saved} pesagem(ns) registada(s) via fotografia!', 'success')
            else:
                flash('Nenhuma pesagem válida para registar.', 'error')
            return redirect(url_for('vendas.pesagem', loja_id=loja_id, data=str(data_reg)))

        elif action == 'delete':
            s_id = request.form.get('id')
            if s_id:
                from database import get_stock_gelado_by_id
                record = get_stock_gelado_by_id(int(s_id))
                if record and _user_owns_loja(record.get('loja', '')):
                    delete_stock_gelado(int(s_id))
                    flash('Pesagem eliminada!', 'success')
                else:
                    flash('Sem permissão para eliminar este registo.', 'error')
            data_redirect = request.form.get('data', '').strip()
            return redirect(url_for('vendas.pesagem', loja_id=loja_id,
                                    data=data_redirect if data_redirect else None))

        elif action == 'delete_day':
            from database import delete_stock_gelado_by_date
            raw_data = request.form.get('data', '').strip()
            if raw_data and _user_owns_loja(loja_nome):
                try:
                    from datetime import datetime as _dt
                    day = _dt.strptime(raw_data, '%Y-%m-%d').date()
                    deleted = delete_stock_gelado_by_date(loja_nome, day)
                    flash(f'Pesagens de {day.strftime("%d/%m/%Y")} eliminadas ({deleted} registo(s)).', 'success')
                except (ValueError, TypeError):
                    flash('Data inválida.', 'error')
            else:
                flash('Sem permissão ou data em falta.', 'error')
            return redirect(url_for('vendas.pesagem', loja_id=loja_id))

        elif action == 'edit':
            s_id_raw = request.form.get('id', '').strip()
            data_redirect = request.form.get('data_redirect', '').strip() or request.form.get('data', '').strip()
            try:
                s_id = int(s_id_raw)
            except (ValueError, TypeError):
                s_id = None
            if s_id:
                from database import get_stock_gelado_by_id
                record = get_stock_gelado_by_id(s_id)
                if record and _user_owns_loja(record.get('loja', '')):
                    try:
                        nova_kg = round(float(request.form.get('quantidade', '0').replace(',', '.')), 3)
                        if nova_kg < 0:
                            raise ValueError
                    except (ValueError, TypeError):
                        flash('Valor inválido para edição.', 'error')
                    else:
                        nova_data = None
                        data_edit_str = request.form.get('data_edit', '').strip()
                        if data_edit_str:
                            try:
                                nova_data = datetime.strptime(data_edit_str, '%Y-%m-%d').date()
                                data_redirect = data_edit_str
                            except (ValueError, TypeError):
                                flash('Data inválida — verifique o formato.', 'error')
                                return redirect(url_for('vendas.pesagem', loja_id=loja_id,
                                                        data=data_redirect if data_redirect else None))
                        update_stock_gelado(s_id, nova_kg, loja=record.get('loja'), nova_data=nova_data)
                        flash(f'Pesagem actualizada para {nova_kg:.3f} kg.', 'success')
                else:
                    flash('Sem permissão para editar este registo.', 'error')
            return redirect(url_for('vendas.pesagem', loja_id=loja_id,
                                    data=data_redirect if data_redirect else None))

    # GET — determine form date context (used by add/OCR date inputs)
    data_str = request.args.get('data', '').strip()
    try:
        data_sel = datetime.strptime(data_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        data_sel = date.today()

    # Current date's records for "Pesagens Registadas" section
    stock_rows = get_stock_gelado_df(loja=loja_nome, tipo='fim', data_inicio=data_sel, data_fim=data_sel)
    pesagens_hoje = [
        {
            'id': r['id'],
            'sabor': reverse_mapping.get(r.get('sabor', ''), r.get('sabor', '')),
            'quantidade_kg': float(r['quantidade_kg']),
        }
        for r in stock_rows
    ]

    return render_template('vendas/pesagem.html',
                           active_tab='pesagem',
                           tabs=_build_tabs('pesagem', loja_id),
                           loja_nome=loja_nome,
                           loja_id=loja_id,
                           sabores=sabores,
                           pesagens_hoje=pesagens_hoje,
                           data_sel=data_sel,
                           today=str(date.today()))


@vendas_bp.route('/pesagem/historico')
@login_required
def pesagem_historico():
    if not _check_vendas_access():
        return jsonify({'ok': False, 'error': 'Sem acesso'}), 403

    loja_id, loja_nome = _get_user_loja()
    mapping = get_sabores_mapping()
    reverse_mapping = {}
    for nome_receita, nome_corrente in mapping.items():
        reverse_mapping[nome_receita] = nome_corrente
        reverse_mapping[nome_corrente] = nome_corrente

    data_inicio_hist = date.today() - timedelta(days=30)
    hist_rows = get_stock_gelado_df(
        loja=loja_nome, tipo='fim',
        data_inicio=data_inicio_hist, data_fim=date.today()
    )
    _by_day = defaultdict(list)
    for r in hist_rows:
        _by_day[r['data']].append({
            'id': r['id'],
            'sabor': reverse_mapping.get(r.get('sabor', ''), r.get('sabor', '')),
            'quantidade_kg': float(r['quantidade_kg']),
        })
    dias = [
        {
            'data_str': d.strftime('%d/%m/%Y'),
            'data_iso': d.isoformat(),
            'total_kg': round(sum(l['quantidade_kg'] for l in linhas), 3),
            'linhas': sorted(linhas, key=lambda x: x['sabor']),
        }
        for d, linhas in sorted(_by_day.items(), reverse=True)
    ]
    return jsonify({'ok': True, 'dias': dias})


@vendas_bp.route('/transferencias', methods=['GET', 'POST'])
@login_required
def transferencias():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    loja_id, loja_nome = _get_user_loja()

    if request.method == 'POST':
        action = request.form.get('action')
        ordem_id = request.form.get('ordem_id')
        username = session.get('user', {}).get('username', '')

        try:
            if action == 'confirmar' and ordem_id:
                ordem = get_ordem_transferencia_by_id(int(ordem_id))
                if ordem and _user_owns_loja(ordem.get('loja_destino', '')):
                    updated = confirmar_ordem_transferencia(int(ordem_id), username)
                    if updated:
                        flash('Transferência confirmada e stock atualizado!', 'success')
                    else:
                        flash('Transferência já processada anteriormente.', 'warning')
                else:
                    flash('Sem permissão para confirmar esta transferência.', 'warning')
            elif action == 'rejeitar' and ordem_id:
                motivo = request.form.get('motivo_rejeicao', '').strip() or None
                ordem = get_ordem_transferencia_by_id(int(ordem_id))
                if ordem and _user_owns_loja(ordem.get('loja_destino', '')):
                    updated = rejeitar_ordem_transferencia(int(ordem_id), username, motivo=motivo)
                    if updated:
                        flash('Transferência rejeitada.', 'info')
                    else:
                        flash('Transferência já processada anteriormente.', 'warning')
                else:
                    flash('Sem permissão para rejeitar esta transferência.', 'warning')
            elif action == 'confirmar_batch':
                ids_str = request.form.get('ordem_ids', '')
                ids = [int(x) for x in ids_str.split(',') if x.strip().isdigit()]
                success = 0
                for oid in ids:
                    ordem = get_ordem_transferencia_by_id(oid)
                    if ordem and _user_owns_loja(ordem.get('loja_destino', '')):
                        if confirmar_ordem_transferencia(oid, username):
                            success += 1
                if success:
                    flash(f'Lote confirmado: {success} artigo(s) atualizado(s)!', 'success')
                else:
                    flash('Sem transferências a confirmar ou já processadas.', 'warning')
            elif action == 'rejeitar_batch':
                ids_str = request.form.get('ordem_ids', '')
                motivo = request.form.get('motivo_rejeicao', '').strip() or None
                ids = [int(x) for x in ids_str.split(',') if x.strip().isdigit()]
                success = 0
                for oid in ids:
                    ordem = get_ordem_transferencia_by_id(oid)
                    if ordem and _user_owns_loja(ordem.get('loja_destino', '')):
                        if rejeitar_ordem_transferencia(oid, username, motivo=motivo):
                            success += 1
                if success:
                    flash(f'Lote rejeitado: {success} artigo(s).', 'info')
                else:
                    flash('Sem transferências a rejeitar ou já processadas.', 'warning')
        except Exception as exc:
            logger.exception('Erro ao processar transferência ordem_id=%s action=%s', ordem_id, action)
            flash('Erro interno ao processar a transferência. Tente novamente.', 'danger')

        return redirect(url_for('vendas.transferencias', loja_id=loja_id))

    pendentes = get_ordens_transferencia(status='pendente', loja_destino=loja_nome)
    recentes = get_ordens_transferencia(loja_destino=loja_nome)
    confirmadas = [o for o in recentes if o['status'] != 'pendente'][:20]

    batch_map = {}
    for o in pendentes:
        batch_key = o.get('batch_id') or f'_solo_{o["id"]}'
        if batch_key not in batch_map:
            batch_map[batch_key] = {
                'batch_id': o.get('batch_id'),
                'area_origem': o['area_origem'],
                'data_prevista': o.get('data_prevista') or o['data'],
                'data': o['data'],
                'criado_por': o.get('criado_por'),
                'ordens': [],
            }
        batch_map[batch_key]['ordens'].append(o)

    for g in batch_map.values():
        g['ordem_ids_csv'] = ','.join(str(o['id']) for o in g['ordens'])

    grupos_pendentes = sorted(batch_map.values(), key=lambda g: (g['data_prevista'], g['data']))

    return render_template('vendas/transferencias.html',
                           active_tab='transferencias',
                           tabs=_build_tabs('transferencias', loja_id),
                           loja_nome=loja_nome,
                           loja_id=loja_id,
                           grupos_pendentes=grupos_pendentes,
                           pendentes=pendentes,
                           confirmadas=confirmadas)


@vendas_bp.route('/fecho-caixa/ocr', methods=['POST'])
@login_required
def fecho_caixa_ocr():
    """JSON endpoint: receive image, run OCR, persist prefill, return extracted fields."""
    if not _check_vendas_access():
        return jsonify({'ok': False, 'error': 'Sem acesso'}), 403

    loja_id, loja_nome = _get_user_loja()
    username = session.get('user', {}).get('username', 'system')

    data_str = request.form.get('data') or str(date.today())
    try:
        data_sel = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        data_sel = date.today()

    img = request.files.get('imagem_caixa')
    if not img or img.filename == '':
        return jsonify({'ok': False, 'error': 'Imagem em falta'}), 400

    img_bytes = img.read()
    from flask_app.services.fecho_caixa import ocr_caixa
    resultado = ocr_caixa(img_bytes, img.filename)

    if resultado.get('error'):
        return jsonify({'ok': False, 'error': resultado['error']}), 422

    # Save image to disk
    import uuid
    upload_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'uploads', 'fecho_caixa')
    os.makedirs(upload_dir, exist_ok=True)
    ext = img.filename.rsplit('.', 1)[-1].lower() if '.' in img.filename else 'jpg'
    fname = f"{data_str}_{loja_id}_{uuid.uuid4().hex[:8]}.{ext}"
    fpath = os.path.join(upload_dir, fname)
    with open(fpath, 'wb') as f:
        f.write(img_bytes)
    rel_path = f"fecho_caixa/{fname}"

    fields = {k: v for k, v in {
        'colaborador': resultado.get('colaborador'),
        'total_moedas': resultado.get('total_moedas'),
        'valor_notas': resultado.get('valor_notas'),
        'total_caixa': resultado.get('total_caixa'),
        'envelope_sobra': resultado.get('envelope_sobra'),
        'total_vendas_pos': resultado.get('total_vendas_pos'),
        'dinheiro_pos': resultado.get('dinheiro_pos'),
        'cartao_pos': resultado.get('cartao_pos'),
        'ubereats_pos': resultado.get('ubereats_pos'),
        'tpa_getnet': resultado.get('tpa_getnet'),
        'imagem_caixa_path': rel_path,
        'ocr_confianca': resultado.get('ocr_confianca'),
    }.items() if v is not None}
    upsert_fecho_caixa(data_sel, loja_id, fields, username)

    # Sync total_vendas_pos into vendas table (same as manual save)
    if fields.get('total_vendas_pos') is not None:
        try:
            from db.producao import add_venda
            add_venda(data_sel, loja_nome, fields['total_vendas_pos'])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("add_venda sync (OCR) failed: %s", e)

    return jsonify({
        'ok': True,
        'confianca': resultado.get('ocr_confianca', 0),
        'data_ocr': resultado.get('data'),
        'fields': {
            'colaborador': resultado.get('colaborador'),
            'total_moedas': resultado.get('total_moedas'),
            'valor_notas': resultado.get('valor_notas'),
            'total_caixa': resultado.get('total_caixa'),
            'envelope_sobra': resultado.get('envelope_sobra'),
            'total_vendas_pos': resultado.get('total_vendas_pos'),
            'dinheiro_pos': resultado.get('dinheiro_pos'),
            'cartao_pos': resultado.get('cartao_pos'),
            'ubereats_pos': resultado.get('ubereats_pos'),
            'tpa_getnet': resultado.get('tpa_getnet'),
        }
    })


@vendas_bp.route('/pesagem/ocr', methods=['POST'])
@login_required
def pesagem_ocr():
    """JSON endpoint: receive weighing sheet photo, run OCR, return extracted rows."""
    if not _check_vendas_access():
        return jsonify({'ok': False, 'error': 'Sem acesso'}), 403

    loja_id, loja_nome = _get_user_loja()

    img = request.files.get('imagem')
    if not img or img.filename == '':
        return jsonify({'ok': False, 'error': 'Imagem em falta'}), 400

    img_bytes = img.read()

    from flask_app.services.pesagem_ocr import ocr_pesagem
    resultado = ocr_pesagem(img_bytes, img.filename)

    if not resultado.get('ok'):
        return jsonify({'ok': False, 'error': 'Falha ao processar imagem'}), 422

    # Persist uploaded image for audit/debug traceability
    try:
        import uuid
        upload_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'uploads', 'pesagem_ocr')
        os.makedirs(upload_dir, exist_ok=True)
        ext = img.filename.rsplit('.', 1)[-1].lower() if '.' in img.filename else 'jpg'
        fname = f"{date.today()}_{loja_id}_{uuid.uuid4().hex[:8]}.{ext}"
        with open(os.path.join(upload_dir, fname), 'wb') as fh:
            fh.write(img_bytes)
    except Exception as save_err:
        import logging as _log
        _log.getLogger(__name__).warning("pesagem OCR image save failed: %s", save_err)

    return jsonify({
        'ok': True,
        'confidence': resultado.get('confidence', 0.0),
        'data_ocr': resultado.get('data'),
        'linhas': resultado.get('linhas', []),
    })


@vendas_bp.route('/fecho-caixa', methods=['GET', 'POST'])
@login_required
def fecho_caixa():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    loja_id, loja_nome = _get_user_loja()
    user = session.get('user', {})
    username = user.get('username', 'system')

    # Date selection
    data_str = request.args.get('data') or request.form.get('data') or str(date.today())
    try:
        data_sel = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        data_sel = date.today()

    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action != 'save':
            flash('Ação desconhecida.', 'warning')
            return redirect(url_for('vendas.fecho_caixa', loja_id=loja_id, data=data_str))

        else:
            def _parse_dec(name):
                v = request.form.get(name, '').strip().replace(',', '.')
                try:
                    return float(v) if v else None
                except ValueError:
                    return None

            # Build moedas_json from denomination fields
            denominacoes = ['0.01', '0.02', '0.05', '0.10', '0.20', '0.50', '1.00', '2.00']
            moedas = {}
            for d in denominacoes:
                key = f"moeda_{d.replace('.', '_')}"
                qty = request.form.get(key, '').strip()
                try:
                    moedas[d] = int(qty) if qty else 0
                except ValueError:
                    moedas[d] = 0
            total_moedas_calc = sum(float(d) * q for d, q in moedas.items())
            total_moedas_form = _parse_dec('total_moedas')
            # Use form total if provided, else calculated
            total_moedas_final = total_moedas_form if total_moedas_form is not None else (round(total_moedas_calc, 2) if any(moedas.values()) else None)

            fields = {
                'colaborador': request.form.get('colaborador', '').strip() or None,
                'moedas_json': moedas if any(moedas.values()) else None,
                'total_moedas': total_moedas_final,
                'valor_notas': _parse_dec('valor_notas'),
                'total_caixa': _parse_dec('total_caixa'),
                'envelope_sobra': _parse_dec('envelope_sobra'),
                'total_vendas_pos': _parse_dec('total_vendas_pos'),
                'dinheiro_pos': _parse_dec('dinheiro_pos'),
                'cartao_pos': _parse_dec('cartao_pos'),
                'ubereats_pos': _parse_dec('ubereats_pos'),
                'tpa_getnet': _parse_dec('tpa_getnet'),
            }
            # Remove None-valued optional fields so they don't overwrite existing data
            fields = {k: v for k, v in fields.items() if v is not None}
            upsert_fecho_caixa(data_sel, loja_id, fields, username)

            # Sync total_vendas_pos into vendas table
            if fields.get('total_vendas_pos') is not None:
                try:
                    from db.producao import add_venda
                    add_venda(data_sel, loja_nome, fields['total_vendas_pos'])
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning("add_venda sync failed: %s", e)

            flash('Fecho de caixa guardado!', 'success')
            return redirect(url_for('vendas.fecho_caixa', loja_id=loja_id, data=data_str))

    registo = get_fecho_caixa(data_sel, loja_id)

    # Monthly reconciliation
    ano, mes = data_sel.year, data_sel.month
    registos_mes = get_fecho_caixa_mensal(loja_id, ano, mes)

    registos_mes_com_dados = [r for r in registos_mes if not r.get('_empty')]
    totais_mes = {
        'total_moedas': sum(r['total_moedas'] or 0 for r in registos_mes_com_dados),
        'valor_notas': sum(r['valor_notas'] or 0 for r in registos_mes_com_dados),
        'total_vendas_pos': sum(r['total_vendas_pos'] or 0 for r in registos_mes_com_dados),
        'dinheiro_pos': sum(r['dinheiro_pos'] or 0 for r in registos_mes_com_dados),
        'cartao_pos': sum(r['cartao_pos'] or 0 for r in registos_mes_com_dados),
        'ubereats_pos': sum(r['ubereats_pos'] or 0 for r in registos_mes_com_dados),
        'tpa_getnet': sum(r['tpa_getnet'] or 0 for r in registos_mes_com_dados),
        'envelope_sobra': sum(r['envelope_sobra'] or 0 for r in registos_mes_com_dados),
        'desvio_numerario': sum(r['desvio_numerario'] or 0 for r in registos_mes_com_dados),
        'desvio_tpa': sum(r['desvio_tpa'] or 0 for r in registos_mes_com_dados),
    }

    import calendar
    meses_pt = ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']

    return render_template('vendas/fecho_caixa.html',
                           active_tab='fecho_caixa',
                           loja_nome=loja_nome,
                           loja_id=loja_id,
                           data_sel=data_sel,
                           data_str=str(data_sel),
                           registo=registo,
                           registos_mes=registos_mes,
                           totais_mes=totais_mes,
                           mes_nome=meses_pt[mes],
                           ano=ano,
                           today=str(date.today()))


@vendas_bp.route('/fecho-caixa/justificar', methods=['POST'])
@login_required
def fecho_caixa_justificar():
    if not _check_vendas_access():
        return jsonify({'ok': False, 'error': 'Sem acesso'}), 403

    fecho_id = request.json.get('fecho_id')
    justificacao = request.json.get('justificacao', '').strip()

    if not fecho_id:
        return jsonify({'ok': False, 'error': 'ID inválido'}), 400

    try:
        fecho_id = int(fecho_id)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'ID inválido'}), 400

    # Verify the record belongs to the user's loja before updating
    user_loja_id, _ = _get_user_loja()
    # Also accept loja_id from request body (sent by JS for multi-store gestor users)
    req_loja_id = request.json.get('loja_id')
    effective_loja_id = user_loja_id or (int(req_loja_id) if req_loja_id else None)

    registo = get_fecho_caixa_by_id(fecho_id)
    if not registo:
        return jsonify({'ok': False, 'error': 'Registo não encontrado'}), 404
    if effective_loja_id and registo.get('loja_id') != effective_loja_id:
        return jsonify({'ok': False, 'error': 'Sem acesso a este registo'}), 403

    ok = salvar_justificacao_fecho(fecho_id, justificacao)
    return jsonify({'ok': ok})
