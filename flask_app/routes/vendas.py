from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, abort
from flask_app.auth import login_required
from datetime import date, datetime, timedelta
from collections import defaultdict
import sys, os, logging, math
logger = logging.getLogger(__name__)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_sabores_list, get_sabores_mapping, get_produtos_rececao,
    get_motivos_quebra, get_produtos_pastelaria, get_produtos_confeitaria,
    get_vendas_bolhao_dashboard_data,
    add_quebra, get_quebras_df, delete_quebra,
    add_stock_gelado, get_stock_gelado_df, get_stock_gelado_by_id,
    delete_stock_gelado, update_stock_gelado,
    add_stock_gelado_carapinas, get_carapinas_for_stock_ids,
    get_ordens_transferencia,
    get_active_venda_stores, get_vendas_module_stores, get_store_by_id,
    upsert_fecho_caixa, get_fecho_caixa, get_fecho_caixa_mensal, get_fecho_caixa_by_id, salvar_justificacao_fecho,
    list_fecho_caixa, list_fecho_caixa_all_stores, delete_fecho_caixa_by_id,
    upsert_fecho_caixa_audited, delete_fecho_caixa_audited, get_fecho_caixa_audit,
    get_all_receitas_gelado, update_receita_gelado, update_receita_gelado_ativo,
)

import flask_app.services.vendas as vendas_svc
from flask_app.services import ServiceError
from flask_app.utils.finance import parse_date as _shared_parse_date
from db.plano import (
    get_ordem_transferencia_by_id, criar_transferencia_entre_lojas,
    get_latest_pesagem_por_sabor, get_effective_stock_por_sabor,
    confirmar_ordens_transferencia_batch,
    reportar_problema_ordens_transferencia_batch,
)
from db.pastelaria import (
    get_pastelaria_sunday_count_grid,
    get_pastelaria_store_count_grid,
    get_pastelaria_count_submission_history,
    save_pastelaria_sunday_counts,
    save_pastelaria_store_counts,
    get_open_pesagem_draft,
    save_pesagem_draft,
    register_pesagem_draft,
    confirm_pesagem_draft,
    get_pesagem_batch_receipt,
    get_pesagem_audit_history,
    restore_stock_gelado,
)
from db.confeitaria import (
    get_confeitaria_store_count_grid,
    save_confeitaria_store_counts,
    get_confeitaria_count_submission_history,
)
from sabor_utils import normalise_sabor
from db.weighing_status import get_daily_weighing_statuses, portugal_today

vendas_bp = Blueprint('vendas', __name__)

TAB_DEFS = [
    {'id': 'dashboard', 'label': 'Resumo Diário', 'icon': '📊', 'endpoint': 'vendas.dashboard'},
    {'id': 'compras_loja', 'label': 'Compras da Loja', 'icon': '🛍️', 'endpoint': 'vendas.compras_loja'},
    {'id': 'contagem_pastelaria', 'label': 'Contagem Pastelaria', 'icon': '🍰', 'endpoint': 'vendas.contagem_pastelaria'},
    {'id': 'contagem_confeitaria', 'label': 'Contagem Confeitaria', 'icon': '🍪', 'endpoint': 'vendas.contagem_confeitaria'},
    {'id': 'transferencias', 'label': 'Receção de Mercadoria', 'icon': '📦', 'endpoint': 'vendas.transferencias'},
    {'id': 'transferir_gelado', 'label': 'Transferir Gelado', 'icon': '📤', 'endpoint': 'vendas.transferir_gelado'},
    {'id': 'quebras', 'label': 'Registar Quebras', 'icon': '⚠️', 'endpoint': 'vendas.quebras'},
    {'id': 'pesagem', 'label': 'Pesagem Fim de Dia', 'icon': '⚖️', 'endpoint': 'vendas.pesagem'},
    {'id': 'fecho_caixa', 'label': 'Fecho de Caixa', 'icon': '💵', 'endpoint': 'vendas.fecho_caixa'},
    {'id': 'sabores_ativos', 'label': 'Sabores Ativos', 'icon': '✅', 'endpoint': 'vendas.sabores_ativos'},
    {'id': 'fecho_historico', 'label': 'Histórico Caixa', 'icon': '📋', 'endpoint': 'vendas.fecho_historico', 'gestor_only': True},
]

_LOJA_ONLY_TILE_IDS = {'transferencias', 'transferir_gelado'}
_EOD_ONLY_TILE_IDS = {'pesagem'}


def supports_vendas_confeitaria_count(store):
    """Confeitaria counts are available to every active Vendas store."""
    return bool(store) and bool(store.get('is_active', True)) and bool(
        store.get('supports_vendas', True)
    )


def get_supported_vendas_tile_ids(store, include_gestor_only=True):
    """Return the canonical vendas tile IDs supported by one store profile."""
    store_type = (store or {}).get('store_type', 'loja')
    requires_eod = (store or {}).get('requires_eod_weighing', True)
    supported = []
    seen = set()
    for tab in TAB_DEFS:
        tile_id = tab['id']
        if tile_id in seen:
            continue
        if not include_gestor_only and tab.get('gestor_only', False):
            continue
        if tile_id in _LOJA_ONLY_TILE_IDS and store_type != 'loja':
            continue
        if tile_id in _EOD_ONLY_TILE_IDS and not requires_eod:
            continue
        if (
            tile_id == 'contagem_confeitaria'
            and not supports_vendas_confeitaria_count(store)
        ):
            continue
        seen.add(tile_id)
        supported.append(tile_id)
    return supported


def _parse_manual_pesagem_entries(raw_entries):
    if not isinstance(raw_entries, list):
        raise ValueError('O rascunho de pesagens é inválido.')
    if len(raw_entries) > 200:
        raise ValueError('O rascunho não pode ter mais de 200 entradas.')

    entries = []
    seen = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError('O rascunho contém uma entrada inválida.')
        sabor = normalise_sabor(str(raw.get('sabor') or '').strip())
        if not sabor:
            raise ValueError('Todas as entradas precisam de um sabor.')
        try:
            entry_date = datetime.strptime(
                str(raw.get('data') or '').strip(), '%Y-%m-%d'
            ).date()
            quantidade_kg = round(float(raw.get('quantidade_kg')), 3)
        except (ValueError, TypeError):
            raise ValueError(
                f'A entrada de {sabor} tem uma data ou quantidade inválida.'
            )
        if quantidade_kg < 0:
            raise ValueError(
                f'A quantidade de {sabor} não pode ser negativa.'
            )
        if not math.isfinite(quantidade_kg) or quantidade_kg >= 1000:
            raise ValueError(
                f'A quantidade de {sabor} não é um número válido em kg.'
            )
        key = (entry_date, sabor.casefold())
        if key in seen:
            raise ValueError(
                f'{sabor} aparece mais do que uma vez em '
                f'{entry_date.strftime("%d/%m/%Y")}.'
            )
        seen.add(key)
        entries.append({
            'data': entry_date,
            'sabor': sabor,
            'quantidade_kg': quantidade_kg,
            'suspeito': bool(raw.get('suspeito')),
        })
    return entries


def _parse_transfer_receipt_ids(raw_ids):
    if not isinstance(raw_ids, str) or not raw_ids.strip():
        raise ValueError("Indique as ordens de transferência.")
    parts = raw_ids.split(',')
    if len(parts) > 1000:
        raise ValueError("O lote contém demasiadas ordens.")

    order_ids = []
    seen = set()
    for part in parts:
        value = part.strip()
        if not value or not value.isascii() or not value.isdigit():
            raise ValueError("O lote contém um identificador inválido.")
        order_id = int(value)
        if order_id <= 0:
            raise ValueError("O lote contém um identificador inválido.")
        if order_id in seen:
            raise ValueError("O mesmo identificador de ordem foi repetido.")
        seen.add(order_id)
        order_ids.append(order_id)
    return order_ids


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
        stores = get_vendas_module_stores()
        if stores:
            return stores[0]['id'], stores[0]['name']

    return None, 'Bolhão'


def _get_compras_loja_store():
    """Resolve the canonical store context for the Compras da Loja area.

    An explicitly selected store is accepted only when the user is authorized
    for it.  The resolved store is validated by its stable ID and capabilities.
    """
    user = session.get('user', {})
    is_gestor = bool(user.get('acesso_gestor'))
    requested_values = [
        value.strip()
        for value in (
            request.args.get('loja_id', ''),
            request.form.get('loja_id', ''),
            request.form.get('_loja_id', ''),
        )
        if str(value or '').strip()
    ]
    if len(set(requested_values)) > 1:
        abort(403)
    requested = requested_values[0] if requested_values else ''

    if is_gestor:
        if requested:
            try:
                store = get_store_by_id(int(requested))
            except (TypeError, ValueError):
                store = None
        else:
            loja_id, _ = _get_user_loja()
            store = get_store_by_id(loja_id) if loja_id else None
    else:
        assigned_store_ids = [
            int(store_id) for store_id in (user.get('vendas_store_ids') or [])
            if str(store_id).isdigit()
        ]
        store_ids = set(assigned_store_ids)
        if requested:
            try:
                requested_id = int(requested)
            except (TypeError, ValueError):
                abort(403)
            if requested_id not in store_ids:
                abort(403)
            store = get_store_by_id(requested_id)
        else:
            store = (
                get_store_by_id(assigned_store_ids[0])
                if assigned_store_ids else None
            )

    if (
        not store
        or store.get('is_active', True) is False
        or store.get('supports_vendas', True) is False
    ):
        return None
    return store


def _get_compras_loja_articles():
    """Return every active catalogue article for store requests."""
    from db.artigos import get_artigos_administrativos

    return [
        artigo for artigo in get_artigos_administrativos(apenas_ativos=True)
        if artigo.get('ativo')
    ]


def _build_tabs(active_id, loja_id=None):
    from db.tiles import get_tile_visibility, get_tile_labels, get_tile_icons
    visibility = get_tile_visibility('vendas', store_id=loja_id)
    labels = get_tile_labels('vendas', store_id=loja_id)
    icons = get_tile_icons('vendas', store_id=loja_id)

    user = session.get('user', {})
    is_gestor = bool(user.get('acesso_gestor'))

    # Determine store capabilities to filter tabs
    store = get_store_by_id(loja_id) if loja_id else None

    # Tab visibility rules by store profile
    supported_ids = set(get_supported_vendas_tile_ids(store))

    tabs = []
    for t in TAB_DEFS:
        gestor_only = t.get('gestor_only', False)
        if gestor_only and not is_gestor:
            continue
        if not gestor_only and not visibility.get(t['id'], True):
            continue
        if t['id'] not in supported_ids:
            continue
        kwargs = {}
        if loja_id:
            kwargs['loja_id'] = loja_id
        tabs.append({
            'id': t['id'],
            'label': labels.get(t['id']) or t['label'],
            'icon': icons.get(t['id']) or t['icon'],
            'url': url_for(t['endpoint'], **kwargs),
            'active': t['id'] == active_id,
        })
    return tabs


def _check_store_capability(loja_id, capability: str):
    """Return True if the resolved store supports the given tab capability.
    capability: 'loja_only' → requires store_type='loja'
                'eod'       → requires requires_eod_weighing=True"""
    if not loja_id:
        return True  # fallback: allow (unknown store, let logic surface errors)
    store = get_store_by_id(loja_id)
    if not store:
        return True
    if capability == 'loja_only':
        return store.get('store_type') == 'loja'
    if capability == 'eod':
        return bool(store.get('requires_eod_weighing'))
    return True


def _check_vendas_access():
    """Return True if the current user has vendas or gestor access."""
    user = session.get('user', {})
    if user.get('acesso_gestor'):
        return True
    vendas_store_ids = user.get('vendas_store_ids') or []
    return len(vendas_store_ids) > 0


def _previous_day_weighing_alert(loja_id, loja_nome):
    previous_day = portugal_today() - timedelta(days=1)
    status = get_daily_weighing_statuses(
        [previous_day], [loja_nome]
    ).get((loja_nome, previous_day))
    if not status or status['state'] not in ('missing', 'draft'):
        return None
    alert = dict(status)
    alert['url'] = url_for(
        'vendas.pesagem',
        loja_id=loja_id,
        data=previous_day.isoformat(),
    )
    return alert


def _user_owns_loja(loja_nome):
    """Return True if user is gestor or their vendas_store_ids includes the store with loja_nome."""
    user = session.get('user', {})
    if user.get('acesso_gestor'):
        return True
    vendas_store_ids = user.get('vendas_store_ids') or []
    if not vendas_store_ids:
        return False
    stores = get_vendas_module_stores()
    for store in stores:
        if store['name'] == loja_nome and store['id'] in vendas_store_ids:
            return True
    return False


def _user_owns_stock_record(record):
    if not record:
        return False
    user = session.get('user', {})
    if user.get('acesso_gestor'):
        return True
    store_id = record.get('store_id')
    if store_id is not None:
        return store_id in (user.get('vendas_store_ids') or [])
    return _user_owns_loja(record.get('loja', ''))


def _count_store_request_id():
    """Return the single explicit store ID supplied for a pastry count.

    The page URL and POST body both carry the selected store. Reject malformed
    or conflicting values rather than silently resolving a different store.
    """
    supplied_ids = []
    for source in (request.args, request.form):
        for key in ('loja_id', '_loja_id'):
            if key not in source:
                continue
            for raw_value in source.getlist(key):
                raw_value = str(raw_value or '').strip()
                if not raw_value:
                    abort(403)
                try:
                    supplied_ids.append(int(raw_value))
                except (TypeError, ValueError):
                    abort(403)

    if len(set(supplied_ids)) > 1:
        abort(403)
    return supplied_ids[0] if supplied_ids else None


def _get_count_store():
    """Resolve and validate the selected store for the pastry count screen."""
    user = session.get('user', {})
    requested_store_id = _count_store_request_id()
    is_gestor = bool(user.get('acesso_gestor'))

    if is_gestor:
        if requested_store_id is None:
            loja_id, _loja_nome = _get_user_loja()
        else:
            loja_id = requested_store_id
    else:
        store_ids = []
        for raw_store_id in user.get('vendas_store_ids') or []:
            try:
                store_ids.append(int(raw_store_id))
            except (TypeError, ValueError):
                continue

        if requested_store_id is not None:
            if requested_store_id not in set(store_ids):
                abort(403)
            loja_id = requested_store_id
        else:
            loja_id = store_ids[0] if store_ids else None

    store = get_store_by_id(loja_id) if loja_id is not None else None
    if (
        not store
        or store.get('is_active', True) is False
        or store.get('supports_vendas', True) is False
    ):
        if requested_store_id is not None:
            abort(403)
        return None, None, None
    return store['id'], store['name'], store


def _default_count_sunday():
    today = date.today()
    return today - timedelta(days=(today.weekday() + 1) % 7)


def _parse_count_sunday(raw):
    try:
        selected = date.fromisoformat(str(raw or ''))
    except (TypeError, ValueError):
        selected = _default_count_sunday()
    if selected.weekday() != 6:
        raise ValueError('Escolha um domingo para preencher a grelha de contagem.')
    return selected


@vendas_bp.route('/')
@login_required
def index():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))
    loja_id, loja_nome = _get_user_loja()
    from db.tiles import get_module_labels
    custom_mod = get_module_labels().get('vendas')
    tabs = _build_tabs(None, loja_id)
    weighing_alert = _previous_day_weighing_alert(loja_id, loja_nome)
    items = [
        {
            'id': t['id'],
            'icon': t['icon'],
            'label': t['label'],
            'url': t['url'],
        }
        for t in tabs
    ]
    if weighing_alert:
        for item in items:
            if item['id'] == 'pesagem':
                item['url'] = weighing_alert['url']
                item['badge'] = {
                    'cls': 'bg-danger',
                    'text': 'Ontem por resolver',
                }
                item['description'] = (
                    f'{weighing_alert["label"]} · '
                    f'{weighing_alert["data_fmt"]}'
                )
    mod_text = custom_mod if custom_mod else 'Vendas'
    return render_template('components/section_menu.html', items=items,
                           menu_title=f'🛍️ {mod_text} — {loja_nome}')


@vendas_bp.route('/compras-loja', methods=['GET', 'POST'])
@login_required
def compras_loja():
    """Landing area for store purchasing workflows.

    This task deliberately provides navigation, context, and empty states;
    weekly orders, urgent requests, and counts are implemented separately.
    """
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    store = _get_compras_loja_store()
    if not store:
        flash('Não foi possível identificar uma loja Vendas ativa.', 'warning')
        return redirect(url_for('home.index'))

    sections = {
        'semanal': {
            'label': 'Encomenda semanal',
            'icon': '📅',
            'empty': 'Ainda não existem encomendas semanais para esta loja.',
        },
        'urgente': {
            'label': 'Pedido urgente',
            'icon': '⚡',
            'empty': 'Ainda não existem pedidos urgentes para esta loja.',
        },
        'contagem': {
            'label': 'Contagem',
            'icon': '🔢',
            'empty': 'Ainda não existem contagens registadas para esta loja.',
        },
        'rececao': {
            'label': 'Receção de pedidos',
            'icon': '📦',
            'empty': 'Ainda não existem envios de Compras para esta loja.',
        },
        'historico': {
            'label': 'Histórico',
            'icon': '📋',
            'empty': 'Ainda não existe histórico de pedidos ou contagens para esta loja.',
        },
    }
    active_section = request.args.get('secao', 'semanal').strip().lower()
    if active_section not in sections:
        active_section = 'semanal'
        flash('Secção inválida. A mostrar a encomenda semanal.', 'info')

    from db.encomendas_semanais import (
        get_available_weekly_articles,
        get_or_create_weekly_order,
        get_weekly_order_for_store,
        next_planning_sunday,
        normalise_planning_sunday,
        save_weekly_draft,
        submit_weekly_order,
        weekly_cycle_dates,
        lisboa_today,
    )
    from db.pedidos_urgentes import (
        URGENT_REASON_LABELS,
        create_urgent_order,
        get_available_urgent_articles,
        list_urgent_orders,
    )
    from db.contagens_compras import (
        get_available_count_articles,
        get_count_history,
        get_count_draft,
        get_or_create_count_draft,
        lisboa_today as lisboa_count_today,
        normalise_count_date,
        save_count_draft,
        submit_count,
    )

    cycle_raw = (
        request.args.get('ciclo')
        or request.form.get('ciclo')
        or next_planning_sunday()
    )
    try:
        planning_sunday = normalise_planning_sunday(cycle_raw)
    except ValueError as exc:
        flash(str(exc), 'warning')
        planning_sunday = next_planning_sunday()
    username = session.get('user', {}).get('username', 'sistema')
    weekly_order = None
    weekly_articles = []
    urgent_articles = []
    urgent_orders = []
    urgent_weekly_order = None
    count_date = lisboa_count_today()
    count_draft = None
    count_articles = []
    count_history = []
    shipments = []
    receipt_request_keys = {}
    if active_section == 'semanal':
        weekly_order = get_or_create_weekly_order(
            store['id'], planning_sunday, actor=username
        )
        weekly_articles = get_available_weekly_articles()
        from db.compras_envios import list_shipments_for_order
        weekly_shipments = list_shipments_for_order(
            'semanal', weekly_order['id']
        )
        totals_by_article = {}
        for shipment in weekly_shipments:
            for shipped_line in shipment['linhas']:
                key = (
                    ('article', int(shipped_line['artigo_id']))
                    if shipped_line.get('artigo_id') is not None
                    else ('line', int(shipped_line['pedido_linha_id']))
                )
                totals = totals_by_article.setdefault(
                    key, {'sent': 0.0, 'received': 0.0, 'pending': 0.0}
                )
                totals['sent'] += shipped_line['quantidade_enviada']
                totals['received'] += shipped_line['quantidade_recebida']
                totals['pending'] += shipped_line['quantidade_pendente']
        for order_line in weekly_order.get('linhas', []):
            key = (
                ('article', int(order_line['artigo_id']))
                if order_line.get('artigo_id') is not None
                else ('line', int(order_line['id']))
            )
            totals = totals_by_article.get(
                key, {'sent': 0.0, 'received': 0.0, 'pending': 0.0}
            )
            order_line['quantidade_enviada'] = totals['sent']
            order_line['quantidade_recebida'] = totals['received']
            order_line['quantidade_pendente'] = max(
                0.0, float(order_line['quantidade']) - totals['sent']
            )
    elif active_section == 'urgente':
        urgent_articles = get_available_urgent_articles()
        urgent_orders = list_urgent_orders(store_id=store['id'])
        urgent_weekly_order = get_weekly_order_for_store(
            store['id'], planning_sunday, include_lines=False
        )
    elif active_section == 'contagem':
        try:
            count_date = normalise_count_date(
                request.args.get('data_contagem')
                or request.form.get('data_contagem')
                or lisboa_count_today()
            )
        except ValueError as exc:
            flash(str(exc), 'warning')
            count_date = lisboa_count_today()
        count_draft = get_or_create_count_draft(
            store['id'], count_date, username
        )
        count_articles = get_available_count_articles()
        count_history = get_count_history(store['id'])
    elif active_section == 'rececao':
        from db.compras_envios import list_shipments_for_store
        shipments = list_shipments_for_store(store['id'])
        from uuid import uuid4
        receipt_request_keys = {
            shipment['id']: str(uuid4()) for shipment in shipments
        }

    if request.method == 'POST' and active_section == 'semanal':
        action = request.form.get('action', 'guardar_rascunho').strip()
        submitted_lines = []
        for key, value in request.form.items():
            if not key.startswith('quantidade_') or not value.strip():
                continue
            article_id = key.removeprefix('quantidade_')
            submitted_lines.append({
                'artigo_id': article_id,
                'quantidade': value,
                'observacoes': request.form.get(
                    f'observacoes_{article_id}', ''
                ),
            })
        try:
            if action == 'guardar_rascunho':
                weekly_order = save_weekly_draft(
                    weekly_order['id'],
                    submitted_lines,
                    username,
                    request.form.get('observacoes', ''),
                )
                flash('Rascunho da encomenda semanal guardado.', 'success')
            elif action == 'submeter':
                if weekly_order['status'] == 'rascunho':
                    weekly_order = save_weekly_draft(
                        weekly_order['id'],
                        submitted_lines,
                        username,
                        request.form.get('observacoes', ''),
                    )
                weekly_order = submit_weekly_order(
                    weekly_order['id'], username, today=lisboa_today()
                )
                flash('Encomenda semanal submetida para Compras.', 'success')
            else:
                raise ValueError('Acção da encomenda semanal inválida.')
        except ValueError as exc:
            flash(str(exc), 'warning')
        return redirect(url_for(
            'vendas.compras_loja',
            loja_id=store['id'],
            secao='semanal',
            ciclo=planning_sunday.isoformat(),
        ))

    if request.method == 'POST' and active_section == 'urgente':
        urgent_lines = []
        for key, value in request.form.items():
            if not key.startswith('quantidade_') or not value.strip():
                continue
            article_id = key.removeprefix('quantidade_')
            urgent_lines.append({
                'artigo_id': article_id,
                'quantidade': value,
                'observacoes': request.form.get(
                    f'observacoes_{article_id}', ''
                ),
            })
        try:
            create_urgent_order(
                store_id=store['id'],
                target_date=request.form.get('data_pretendida'),
                lines=urgent_lines,
                actor=username,
                reason=request.form.get('motivo', ''),
                reason_detail=request.form.get('motivo_detalhe', ''),
                observations=request.form.get('observacoes', ''),
                weekly_order_id=request.form.get('encomenda_semanal_id') or None,
                weekly_cycle=request.form.get('ciclo') or None,
                today=lisboa_today(),
            )
            flash('Pedido urgente submetido para Compras.', 'success')
        except (TypeError, ValueError) as exc:
            flash(str(exc), 'warning')
        return redirect(url_for(
            'vendas.compras_loja',
            loja_id=store['id'],
            secao='urgente',
            ciclo=planning_sunday.isoformat(),
        ))

    if request.method == 'POST' and active_section == 'contagem':
        count_lines = []
        for key, value in request.form.items():
            if not key.startswith('quantidade_') or not value.strip():
                continue
            article_id = key.removeprefix('quantidade_')
            count_lines.append({
                'artigo_id': article_id,
                'quantidade': value,
                'observacoes': request.form.get(
                    f'observacoes_{article_id}', ''
                ),
            })
        try:
            count_id = int(request.form.get('contagem_id', ''))
            draft = get_count_draft(count_id)
            if not draft or int(draft['store_id']) != int(store['id']):
                raise ValueError('A contagem selecionada não pertence a esta loja.')
            action = request.form.get('action', 'guardar_rascunho').strip()
            if action == 'guardar_rascunho':
                save_count_draft(count_id, count_lines, username)
                flash('Rascunho da contagem guardado.', 'success')
            elif action == 'submeter':
                if draft['status'] == 'rascunho':
                    save_count_draft(count_id, count_lines, username)
                submit_count(count_id, username)
                flash('Contagem submetida para Compras.', 'success')
            else:
                raise ValueError('Acção da contagem inválida.')
        except (TypeError, ValueError) as exc:
            flash(str(exc), 'warning')
        return redirect(url_for(
            'vendas.compras_loja',
            loja_id=store['id'],
            secao='contagem',
            data_contagem=count_date.isoformat(),
        ))

    if request.method == 'POST' and active_section == 'rececao':
        from db.compras_envios import create_receipt
        receipt_lines = []
        for key, value in request.form.items():
            if not key.startswith('recebida_'):
                continue
            line_id = key.removeprefix('recebida_')
            receipt_lines.append({
                'envio_linha_id': line_id,
                'quantidade_recebida': value,
                'discrepancia': request.form.get(
                    f'discrepancia_{line_id}', ''
                ),
            })
        try:
            receipt = create_receipt(
                shipment_id=request.form.get('envio_id'),
                store_id=store['id'],
                lines=receipt_lines,
                actor=username,
                request_key=request.form.get('request_key', ''),
                observations=request.form.get('observacoes', ''),
            )
            flash(
                'Receção guardada.' if not receipt.get('duplicate')
                else 'Esta receção já tinha sido registada.',
                'success',
            )
        except (TypeError, ValueError) as exc:
            flash(str(exc), 'warning')
        return redirect(url_for(
            'vendas.compras_loja',
            loja_id=store['id'],
            secao='rececao',
        ))

    return render_template(
        'vendas/compras_loja.html',
        active_tab='compras_loja',
        tabs=_build_tabs('compras_loja', store['id']),
        loja_id=store['id'],
        loja_nome=store['name'],
        store=store,
        sections=sections,
        active_section=active_section,
        artigos_disponiveis=_get_compras_loja_articles(),
        weekly_order=weekly_order,
        weekly_articles=weekly_articles,
        weekly_cycle=weekly_cycle_dates(planning_sunday),
        urgent_articles=urgent_articles,
        urgent_orders=urgent_orders,
        urgent_weekly_order=urgent_weekly_order,
        urgent_default_date=(lisboa_today() + timedelta(days=1)),
        urgent_reason_labels=URGENT_REASON_LABELS,
        count_date=count_date,
        count_draft=count_draft,
        count_articles=count_articles,
        count_history=count_history,
        shipments=shipments,
        receipt_request_keys=receipt_request_keys,
        available_store_options=(
            [
                candidate for candidate in (
                    get_store_by_id(store_id)
                    for store_id in (
                        session.get('user', {}).get('vendas_store_ids') or []
                    )
                )
                if candidate
                and candidate.get('is_active', True) is not False
                and candidate.get('supports_vendas', True) is not False
            ]
            if not session.get('user', {}).get('acesso_gestor')
            else []
        ),
        weekly_statuses={
            'rascunho': 'Rascunho',
            'submetida': 'Submetida',
            'em_preparacao': 'Em preparação',
            'concluida': 'Concluída',
            'cancelada': 'Cancelada',
        },
    )


@vendas_bp.route('/dashboard')
@login_required
def dashboard():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    loja_id, loja_nome = _get_user_loja()

    data = vendas_svc.build_dashboard_rows(loja_nome, loja_id=loja_id)
    weighing_alert = _previous_day_weighing_alert(loja_id, loja_nome)

    return render_template('vendas/dashboard.html',
                           active_tab='dashboard',
                           tabs=_build_tabs('dashboard', loja_id),
                           loja_nome=loja_nome,
                           loja_id=loja_id,
                           dashboard_mode=data.get('mode', 'bolhao_pos'),
                           rows=data.get('rows', []),
                           total_ontem=data.get('total_ontem', '0.000'),
                           total_recebido=data.get('total_recebido', '0.000'),
                           total_fim=data.get('total_fim'),
                           fecho=data.get('fecho'),
                           weighing_alert=weighing_alert)


@vendas_bp.route('/contagem-pastelaria', methods=['GET', 'POST'])
@login_required
def contagem_pastelaria():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    loja_id, loja_nome, store = _get_count_store()
    if not store:
        return redirect(url_for('home.index'))

    error = None
    raw_date = (
        request.form.get('data_contagem') or request.form.get('data')
        or request.form.get('domingo')
        if request.method == 'POST'
        else request.args.get('data_contagem') or request.args.get('data')
        or request.args.get('domingo')
    )
    date_error = False
    try:
        count_date = _parse_count_sunday(raw_date)
        grid = get_pastelaria_sunday_count_grid(count_date, loja_id)
    except (TypeError, ValueError) as exc:
        date_error = bool(raw_date)
        error = str(exc)
        flash(str(exc), 'warning')
        count_date = _default_count_sunday()
        grid = get_pastelaria_store_count_grid(count_date, loja_id)

    if request.method == 'POST' and not date_error:
        action = request.form.get('action', '')
        legacy_save = not action
        if action not in ('guardar_grelha', ''):
            abort(405)
        values = []
        try:
            submitted_by = session.get('user', {}).get('username', '')
            for product in grid['products']:
                field_name = f"count_{product['id']}"
                if field_name not in request.form:
                    raise ValueError(
                        'A grelha recebida está incompleta. '
                        'Atualize a página antes de guardar.'
                    )
                raw_quantity = request.form.get(field_name)
                if raw_quantity is None:
                    raise ValueError(
                        'A grelha recebida está incompleta. '
                        'Atualize a página antes de guardar.'
                    )
                raw_quantity = raw_quantity.strip()
                if raw_quantity == '':
                    quantity = 0
                elif raw_quantity.isdigit():
                    quantity = int(raw_quantity)
                else:
                    raise ValueError(
                        'Use números inteiros não negativos; os campos '
                        'vazios serão guardados como 0.'
                    )
                values.append((product['id'], quantity))
            if legacy_save:
                saved = save_pastelaria_sunday_counts(
                    count_date,
                    [(product_id, loja_id, quantity) for product_id, quantity in values],
                    request.form.get('snapshot_token'),
                    store_id=loja_id,
                    submitted_by=submitted_by,
                )
            else:
                saved = save_pastelaria_store_counts(
                    count_date, loja_id, values, request.form.get('snapshot_token'),
                    submitted_by=submitted_by,
                )
            flash(f'Contagem de domingo guardada: {saved} valores.', 'success')
            return redirect(url_for(
                'vendas.contagem_pastelaria',
                loja_id=loja_id,
                data_contagem=count_date.isoformat(),
            ))
        except (TypeError, ValueError) as exc:
            error = str(exc)
            flash(str(exc), 'warning')
            grid = get_pastelaria_sunday_count_grid(count_date, loja_id)
            submitted = dict(values)
            for product in grid.get('products', []):
                if product['id'] not in submitted:
                    continue
                quantity = submitted[product['id']]
                if 'count' in product:
                    product['count'] = quantity
                if 'counts' in product:
                    product['counts'][loja_id] = quantity

    is_gestor = bool(session.get('user', {}).get('acesso_gestor'))
    count_history = (
        get_pastelaria_count_submission_history(count_date, loja_id)
        if is_gestor and not date_error else []
    )
    return render_template(
        'vendas/contagem_pastelaria.html',
        active_tab='contagem_pastelaria',
        tabs=_build_tabs('contagem_pastelaria', loja_id),
        loja_id=loja_id,
        loja_nome=loja_nome,
        count_date=count_date,
        count_grid=grid,
        # Compatibility aliases for older integrations and tests.
        grid=grid,
        selected_date=count_date,
        error=error,
        is_gestor=is_gestor,
        count_history=count_history,
        vendas_stores=get_vendas_module_stores(),
    )


@vendas_bp.route('/contagem-confeitaria', methods=['GET', 'POST'])
@login_required
def contagem_confeitaria():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    loja_id, loja_nome, store = _get_count_store()
    if not store:
        return redirect(url_for('home.index'))
    if not supports_vendas_confeitaria_count(store):
        abort(403)

    error = None
    raw_date = (
        request.form.get('data_contagem')
        if request.method == 'POST'
        else request.args.get('data_contagem')
    )
    date_error = False
    try:
        count_date = date.fromisoformat(raw_date) if raw_date else date.today()
    except (TypeError, ValueError):
        date_error = True
        error = 'Escolha uma data válida para a contagem.'
        flash(error, 'warning')
        count_date = date.today()

    try:
        grid = get_confeitaria_store_count_grid(count_date, loja_id)
    except ValueError as exc:
        if 'Loja inválida' in str(exc):
            abort(403)
        raise

    if request.method == 'POST' and not date_error:
        if request.form.get('action') != 'guardar_grelha':
            abort(405)
        values = []
        try:
            expected_fields = {
                f"count_{product['id']}" for product in grid['products']
            }
            submitted_fields = {
                field for field in request.form
                if field.startswith('count_')
            }
            if submitted_fields != expected_fields:
                raise ValueError(
                    'A grelha recebida está incompleta. '
                    'Atualize a página antes de guardar.'
                )

            for product in grid['products']:
                field_name = f"count_{product['id']}"
                raw_values = request.form.getlist(field_name)
                if len(raw_values) != 1:
                    raise ValueError(
                        'A grelha recebida está incompleta. '
                        'Atualize a página antes de guardar.'
                    )
                raw_quantity = raw_values[0].strip()
                if raw_quantity == '':
                    quantity = 0
                elif raw_quantity.isascii() and raw_quantity.isdigit():
                    quantity = int(raw_quantity)
                else:
                    raise ValueError(
                        'Use números inteiros não negativos; os campos '
                        'vazios serão guardados como 0.'
                    )
                if quantity > 2_147_483_647:
                    raise ValueError('A quantidade excede o limite permitido.')
                values.append((product['id'], quantity))

            submitted_by = session.get('user', {}).get('username', '')
            saved = save_confeitaria_store_counts(
                count_date,
                loja_id,
                values,
                request.form.get('snapshot_token'),
                submitted_by=submitted_by,
            )
            flash(
                f'Contagem de Confeitaria guardada: {saved} valores.',
                'success',
            )
            return redirect(url_for(
                'vendas.contagem_confeitaria',
                loja_id=loja_id,
                data_contagem=count_date.isoformat(),
            ))
        except (TypeError, ValueError) as exc:
            error = str(exc)
            flash(error, 'warning')
            try:
                grid = get_confeitaria_store_count_grid(count_date, loja_id)
            except ValueError as grid_exc:
                if 'Loja inválida' in str(grid_exc):
                    abort(403)
                raise
            submitted = dict(values)
            for product in grid.get('products', []):
                if product['id'] in submitted:
                    product['count'] = submitted[product['id']]

    is_gestor = bool(session.get('user', {}).get('acesso_gestor'))
    count_history = (
        get_confeitaria_count_submission_history(count_date, loja_id)
        if is_gestor and not date_error else []
    )
    vendas_stores = [
        candidate
        for candidate in get_vendas_module_stores()
        if supports_vendas_confeitaria_count(candidate)
    ]
    return render_template(
        'vendas/contagem_confeitaria.html',
        active_tab='contagem_confeitaria',
        tabs=_build_tabs('contagem_confeitaria', loja_id),
        loja_id=loja_id,
        loja_nome=loja_nome,
        count_date=count_date,
        count_grid=grid,
        error=error,
        is_gestor=is_gestor,
        count_history=count_history,
        vendas_stores=vendas_stores,
    )


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

    historico_inicio = date.today() - timedelta(days=90)
    quebras_df = get_quebras_df(loja_nome, data_inicio=historico_inicio)
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
    if not _check_store_capability(loja_id, 'eod'):
        return redirect(url_for('vendas.index', loja_id=loja_id))
    sabores = get_sabores_list()
    mapping = get_sabores_mapping()
    reverse_mapping = {}
    for nome_receita, nome_corrente in mapping.items():
        reverse_mapping[nome_receita] = nome_corrente
        reverse_mapping[nome_corrente] = nome_corrente

    def _parse_date_form(key='data'):
        return _shared_parse_date(request.form.get(key, '')) or date.today()

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
            qtds_parsed = [_parse_kg_str(q) for q in qtds]
            pesagem_kg = round(sum(qtds_parsed), 3)

            if pesagem_kg >= 0 and sabor:
                try:
                    user = session.get('user', {})
                    stock_id = add_stock_gelado(
                        data_reg, loja_nome, sabor, pesagem_kg, 'fim',
                        actor_id=user.get('id'),
                        actor_username=user.get('username') or 'sistema',
                        origin='vendas_single',
                    )
                    individual = [v for v in qtds_parsed if v > 0]
                    if len(individual) > 1:
                        add_stock_gelado_carapinas(stock_id, individual)
                    flash(
                        f'Pesagem de {pesagem_kg:.3f} kg de {sabor} registada!',
                        'success',
                    )
                except ValueError as exc:
                    flash(str(exc), 'error')
            else:
                flash('Insira um valor válido.', 'error')
            return redirect(url_for('vendas.pesagem', loja_id=loja_id, data=str(data_reg)))

        elif action == 'add_bulk':
            sabores_list = request.form.getlist('sabor[]')
            qtds_list = request.form.getlist('quantidade[]')
            data_reg = _parse_date_form()
            entries = []
            for sabor, qtd_str in zip(sabores_list, qtds_list):
                sabor = sabor.strip()
                if not sabor:
                    continue
                try:
                    pesagem_kg = round(float(qtd_str.replace(',', '.')), 3)
                except (ValueError, TypeError):
                    continue
                if pesagem_kg >= 0:
                    entries.append({'data': data_reg, 'sabor': sabor, 'quantidade_kg': pesagem_kg, 'tipo': 'fim'})
            bulk_saved = 0
            skipped_dup = 0
            if entries:
                try:
                    from database import add_stock_gelado_bulk
                    user = session.get('user', {})
                    existing_set = set()
                    existing_records = get_stock_gelado_df(
                        loja=loja_nome, tipo='fim',
                        data_inicio=data_reg, data_fim=data_reg
                    )
                    for r in existing_records:
                        existing_set.add((r['data'], r['sabor']))
                    new_entries = []
                    for e in entries:
                        key = (e['data'], e['sabor'])
                        if key not in existing_set:
                            new_entries.append(e)
                            existing_set.add(key)
                    skipped_dup = len(entries) - len(new_entries)
                    bulk_saved = add_stock_gelado_bulk(
                        new_entries,
                        loja_nome,
                        actor_id=user.get('id'),
                        actor_username=user.get('username') or 'sistema',
                        origin='vendas_ocr',
                    ) if new_entries else 0
                    if bulk_saved > 0 and skipped_dup == 0:
                        flash(f'{bulk_saved} pesagem(ns) registada(s) via fotografia!', 'success')
                    elif bulk_saved > 0:
                        flash(f'{bulk_saved} pesagem(ns) registada(s). {skipped_dup} já existiam e foram ignoradas.', 'success')
                    elif skipped_dup > 0:
                        flash(f'Todas as {skipped_dup} entradas já existiam — nenhum registo duplicado foi criado.', 'info')
                    else:
                        flash('Nenhuma pesagem válida para registar.', 'error')
                except ValueError as exc:
                    flash(str(exc), 'error')
                except Exception:
                    flash('Erro ao guardar as pesagens — nenhum registo foi guardado. Tente novamente.', 'error')
            else:
                flash('Nenhuma pesagem válida para registar.', 'error')
            return redirect(url_for('vendas.pesagem', loja_id=loja_id, data=str(data_reg)))

        elif action == 'add_bulk_manual':
            sabores_list = request.form.getlist('sabor[]')
            qtds_list = request.form.getlist('quantidade[]')
            datas_list = request.form.getlist('data[]')
            entries = []
            last_data = date.today()
            for i, (sabor, qtd_str) in enumerate(zip(sabores_list, qtds_list)):
                sabor = sabor.strip()
                if not sabor:
                    continue
                try:
                    pesagem_kg = round(float(qtd_str.replace(',', '.')), 3)
                except (ValueError, TypeError):
                    continue
                if pesagem_kg < 0:
                    continue
                raw_d = datas_list[i] if i < len(datas_list) else ''
                try:
                    entry_date = datetime.strptime(raw_d.strip(), '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    entry_date = date.today()
                last_data = entry_date
                entries.append({'data': entry_date, 'sabor': sabor, 'quantidade_kg': pesagem_kg, 'tipo': 'fim'})
            bulk_saved = 0
            skipped_dup = 0
            if entries:
                try:
                    from database import add_stock_gelado_bulk
                    user = session.get('user', {})
                    all_dates = set(e['data'] for e in entries)
                    existing_set = set()
                    existing_records = get_stock_gelado_df(
                        loja=loja_nome, tipo='fim',
                        data_inicio=min(all_dates), data_fim=max(all_dates)
                    )
                    for r in existing_records:
                        existing_set.add((r['data'], r['sabor']))
                    new_entries = []
                    for e in entries:
                        key = (e['data'], e['sabor'])
                        if key not in existing_set:
                            new_entries.append(e)
                            existing_set.add(key)
                    skipped_dup = len(entries) - len(new_entries)
                    bulk_saved = add_stock_gelado_bulk(
                        new_entries,
                        loja_nome,
                        actor_id=user.get('id'),
                        actor_username=user.get('username') or 'sistema',
                        origin='vendas_manual_bulk',
                    ) if new_entries else 0
                    if bulk_saved > 0 and skipped_dup == 0:
                        flash(f'{bulk_saved} pesagem(ns) confirmada(s) e registada(s)!', 'success')
                    elif bulk_saved > 0:
                        flash(f'{bulk_saved} pesagem(ns) registada(s). {skipped_dup} já existiam e foram ignoradas.', 'success')
                    elif skipped_dup > 0:
                        flash(f'Todas as {skipped_dup} entradas já existiam — nenhum registo duplicado foi criado.', 'info')
                    else:
                        flash('Nenhuma pesagem foi guardada. Verifique os dados e tente novamente.', 'error')
                except ValueError as exc:
                    flash(str(exc), 'error')
                except Exception:
                    flash('Erro ao guardar as pesagens — nenhum registo foi guardado. Tente novamente.', 'error')
            else:
                flash('Nenhuma pesagem válida para registar.', 'error')
            redirect_kwargs = dict(loja_id=loja_id, data=str(last_data))
            if bulk_saved > 0 or skipped_dup > 0:
                redirect_kwargs['draft_cleared'] = '1'
            return redirect(url_for('vendas.pesagem', **redirect_kwargs))

        elif action == 'delete':
            s_id = request.form.get('id')
            if s_id:
                from database import get_stock_gelado_by_id
                record = get_stock_gelado_by_id(int(s_id))
                if _user_owns_stock_record(record):
                    try:
                        user = session.get('user', {})
                        delete_stock_gelado(
                            int(s_id),
                            request.form.get('reason', ''),
                            actor_id=user.get('id'),
                            actor_username=user.get('username') or 'sistema',
                        )
                        flash(
                            'Pesagem desativada. Pode ser reposta no histórico.',
                            'success',
                        )
                    except ValueError as exc:
                        flash(str(exc), 'error')
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
                    user = session.get('user', {})
                    deleted = delete_stock_gelado_by_date(
                        loja_nome,
                        day,
                        request.form.get('reason', ''),
                        request.form.get('expected_count'),
                        actor_id=user.get('id'),
                        actor_username=user.get('username') or 'sistema',
                    )
                    flash(
                        f'Pesagens de {day.strftime("%d/%m/%Y")} '
                        f'desativadas ({deleted} registo(s)).',
                        'success',
                    )
                except (ValueError, TypeError) as exc:
                    flash(str(exc) or 'Data inválida.', 'error')
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
                if _user_owns_stock_record(record):
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
                        try:
                            user = session.get('user', {})
                            update_stock_gelado(
                                s_id,
                                nova_kg,
                                loja=record.get('loja'),
                                nova_data=nova_data,
                                actor_id=user.get('id'),
                                actor_username=(
                                    user.get('username') or 'sistema'
                                ),
                                reason=request.form.get('reason'),
                                expected_store_id=loja_id,
                            )
                            flash(
                                f'Pesagem actualizada para {nova_kg:.3f} kg.',
                                'success',
                            )
                        except ValueError as exc:
                            flash(str(exc), 'error')
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
    if pesagens_hoje:
        carap_map = get_carapinas_for_stock_ids([p['id'] for p in pesagens_hoje])
        for p in pesagens_hoje:
            p['carapinas'] = carap_map.get(p['id'], [])

    receipt = None
    receipt_id = request.args.get('receipt', '').strip()
    if receipt_id:
        try:
            candidate = get_pesagem_batch_receipt(receipt_id, loja_nome)
            if candidate and candidate.get('status') == 'confirmed':
                receipt = candidate
        except (ValueError, TypeError):
            receipt = None

    daily_status = get_daily_weighing_statuses(
        [data_sel], [loja_nome]
    ).get((loja_nome, data_sel))
    audit_events = get_pesagem_audit_history(
        loja_id,
        data_inicio=data_sel,
        data_fim=data_sel,
        limit=100,
    )

    return render_template('vendas/pesagem.html',
                           active_tab='pesagem',
                           tabs=_build_tabs('pesagem', loja_id),
                           loja_nome=loja_nome,
                           loja_id=loja_id,
                           sabores=sabores,
                           pesagens_hoje=pesagens_hoje,
                           receipt=receipt,
                           daily_status=daily_status,
                           audit_events=audit_events,
                           can_restore=bool(
                               session.get('user', {}).get('acesso_gestor')
                           ),
                           data_sel=data_sel,
                           today=str(portugal_today()))


@vendas_bp.route('/pesagem/rascunho', methods=['GET', 'PUT'])
@login_required
def pesagem_rascunho():
    if not _check_vendas_access():
        return jsonify({'ok': False, 'error': 'Sem acesso'}), 403
    loja_id, loja_nome = _get_user_loja()
    if not _check_store_capability(loja_id, 'eod'):
        return jsonify({
            'ok': False,
            'error': 'Sem suporte para pesagem nesta loja',
        }), 403

    if request.method == 'GET':
        draft = get_open_pesagem_draft(loja_nome)
        return jsonify({'ok': True, 'draft': draft})

    payload = request.get_json(silent=True) or {}
    try:
        entries = _parse_manual_pesagem_entries(payload.get('entries', []))
        user = session.get('user', {})
        draft = save_pesagem_draft(
            payload.get('batch_id'),
            payload.get('revision'),
            loja_nome,
            loja_id,
            user.get('id'),
            user.get('username') or 'sistema',
            entries,
        )
        return jsonify({'ok': True, 'draft': draft})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 409
    except Exception:
        logger.exception(
            'Falha ao guardar rascunho de pesagens para %s',
            loja_nome,
        )
        return jsonify({
            'ok': False,
            'error': (
                'Não foi possível sincronizar o rascunho. '
                'Os dados continuam guardados neste dispositivo.'
            ),
        }), 500


@vendas_bp.route('/pesagem/rascunho/confirmar', methods=['POST'])
@login_required
def confirmar_pesagem_rascunho():
    if not _check_vendas_access():
        return jsonify({'ok': False, 'error': 'Sem acesso'}), 403
    loja_id, loja_nome = _get_user_loja()
    if not _check_store_capability(loja_id, 'eod'):
        return jsonify({
            'ok': False,
            'error': 'Sem suporte para pesagem nesta loja',
        }), 403

    payload = request.get_json(silent=True) or {}
    batch_id = str(payload.get('batch_id') or '').strip()
    if not batch_id:
        return jsonify({
            'ok': False,
            'error': 'O identificador do lote está em falta.',
        }), 400
    try:
        receipt = confirm_pesagem_draft(
            batch_id,
            payload.get('revision'),
            loja_nome,
            session.get('user', {}).get('username') or 'sistema',
            session.get('user', {}).get('id'),
        )
        return jsonify({'ok': True, 'receipt': receipt})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 409
    except Exception:
        logger.exception(
            'Falha ao confirmar lote de pesagens %s para %s',
            batch_id,
            loja_nome,
        )
        return jsonify({
            'ok': False,
            'error': (
                'O lote não foi confirmado. O rascunho foi mantido; '
                'verifique a ligação e tente novamente.'
            ),
        }), 500


@vendas_bp.route('/pesagem/rascunho/registar', methods=['POST'])
@login_required
def registar_pesagem_rascunho():
    if not _check_vendas_access():
        return jsonify({'ok': False, 'error': 'Sem acesso'}), 403
    loja_id, loja_nome = _get_user_loja()
    if not _check_store_capability(loja_id, 'eod'):
        return jsonify({
            'ok': False,
            'error': 'Sem suporte para pesagem nesta loja',
        }), 403

    payload = request.get_json(silent=True) or {}
    batch_id = str(payload.get('batch_id') or '').strip()
    if not batch_id:
        return jsonify({
            'ok': False,
            'error': 'O identificador do lote está em falta.',
        }), 400
    try:
        draft = register_pesagem_draft(
            batch_id,
            payload.get('revision'),
            loja_nome,
            session.get('user', {}).get('username') or 'sistema',
            session.get('user', {}).get('id'),
        )
        return jsonify({'ok': True, 'draft': draft})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 409
    except Exception:
        logger.exception(
            'Falha ao registar lote de pesagens %s para %s',
            batch_id,
            loja_nome,
        )
        return jsonify({
            'ok': False,
            'error': (
                'As pesagens não foram registadas. O lote foi mantido; '
                'verifique a ligação e tente novamente.'
            ),
        }), 500


@vendas_bp.route('/pesagem/historico')
@login_required
def pesagem_historico():
    if not _check_vendas_access():
        return jsonify({'ok': False, 'error': 'Sem acesso'}), 403

    loja_id, loja_nome = _get_user_loja()
    if not _check_store_capability(loja_id, 'eod'):
        return jsonify({'ok': False, 'error': 'Sem suporte para pesagem nesta loja'}), 403
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
    all_row_ids = []
    for r in hist_rows:
        row_id = r['id']
        all_row_ids.append(row_id)
        _by_day[r['data']].append({
            'id': row_id,
            'sabor': reverse_mapping.get(r.get('sabor', ''), r.get('sabor', '')),
            'quantidade_kg': float(r['quantidade_kg']),
        })
    carap_map = get_carapinas_for_stock_ids(all_row_ids) if all_row_ids else {}
    dias = []
    for d, linhas in sorted(_by_day.items(), reverse=True):
        for l in linhas:
            l['carapinas'] = carap_map.get(l['id'], [])
        dias.append({
            'data_str': d.strftime('%d/%m/%Y'),
            'data_iso': d.isoformat(),
            'total_kg': round(sum(l['quantidade_kg'] for l in linhas), 3),
            'linhas': sorted(linhas, key=lambda x: x['sabor']),
        })
    return jsonify({'ok': True, 'dias': dias})


@vendas_bp.route('/pesagem/auditoria/restaurar', methods=['POST'])
@login_required
def restaurar_pesagem():
    user = session.get('user', {})
    if not user.get('acesso_gestor'):
        abort(403)
    loja_id, loja_nome = _get_user_loja()
    try:
        stock_id = int(request.form.get('stock_id', ''))
        record = get_stock_gelado_by_id(stock_id, include_inactive=True)
        if not record or (
            (
                record.get('store_id') is not None
                and record.get('store_id') != loja_id
            )
            or (
                record.get('store_id') is None
                and record.get('loja') != loja_nome
            )
        ):
            abort(404)
        restored = restore_stock_gelado(
            stock_id,
            request.form.get('reason', ''),
            actor_id=user.get('id'),
            actor_username=user.get('username') or 'sistema',
        )
        flash('Pesagem reposta e novamente incluída nos cálculos.', 'success')
        data_redirect = restored['data'].isoformat()
    except ValueError as exc:
        flash(str(exc), 'error')
        data_redirect = request.form.get('data', '')
    return redirect(url_for(
        'vendas.pesagem',
        loja_id=loja_id,
        data=data_redirect or None,
    ))


@vendas_bp.route('/transferencias', methods=['GET', 'POST'])
@login_required
def transferencias():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    loja_id, loja_nome, store = _get_count_store()
    if not store:
        flash('Não foi possível identificar uma loja Vendas ativa.', 'warning')
        return redirect(url_for('home.index'))
    if not _check_store_capability(loja_id, 'loja_only'):
        return redirect(url_for('vendas.index', loja_id=loja_id))

    if request.method == 'POST':
        action = request.form.get('action')
        username = session.get('user', {}).get('username', '')

        try:
            if action in ('confirmar', 'reportar_problema'):
                submitted_ids = request.form.getlist('ordem_id')
                if len(submitted_ids) != 1:
                    raise ValueError(
                        "Indique uma única ordem de transferência."
                    )
                order_ids = _parse_transfer_receipt_ids(submitted_ids[0])
                if len(order_ids) != 1:
                    raise ValueError(
                        "Indique uma única ordem de transferência."
                    )
                ordem_id = order_ids[0]
                ordem = get_ordem_transferencia_by_id(ordem_id)
                if not ordem or ordem.get('loja_destino') != loja_nome:
                    flash(
                        'Sem permissão para processar esta transferência.',
                        'warning',
                    )
                elif action == 'confirmar':
                    result = confirmar_ordens_transferencia_batch(
                        [ordem_id], loja_nome, username
                    )
                    if result['updated_count']:
                        flash(
                            'Receção aceite. O stock já estava atualizado.',
                            'success',
                        )
                    else:
                        flash('Transferência já processada anteriormente.', 'warning')
                else:
                    motivo = request.form.get('motivo_problema', '').strip()
                    if not motivo:
                        raise ValueError('Indique o problema encontrado.')
                    result = reportar_problema_ordens_transferencia_batch(
                        [ordem_id], loja_nome, username, motivo
                    )
                    if result['updated_count']:
                        flash('Problema reportado. O stock não foi alterado.', 'info')
                    else:
                        flash('Transferência já processada anteriormente.', 'warning')
            elif action == 'confirmar_batch':
                submitted_ids = request.form.getlist('ordem_ids')
                if len(submitted_ids) != 1:
                    raise ValueError("Indique um único lote de ordens.")
                order_ids = _parse_transfer_receipt_ids(submitted_ids[0])
                result = confirmar_ordens_transferencia_batch(
                    order_ids, loja_nome, username
                )
                if result['updated_count']:
                    flash(
                        f'Receção aceite: {result["updated_count"]} artigo(s).',
                        'success',
                    )
                else:
                    flash(
                        'Receção já processada anteriormente.',
                        'warning',
                    )
            elif action == 'reportar_problema_batch':
                submitted_ids = request.form.getlist('ordem_ids')
                if len(submitted_ids) != 1:
                    raise ValueError("Indique um único lote de ordens.")
                order_ids = _parse_transfer_receipt_ids(submitted_ids[0])
                motivo = request.form.get('motivo_problema', '').strip()
                if not motivo:
                    raise ValueError('Indique o problema encontrado.')
                result = reportar_problema_ordens_transferencia_batch(
                    order_ids, loja_nome, username, motivo
                )
                if result['updated_count']:
                    flash(
                        f'Problema reportado em {result["updated_count"]} artigo(s). '
                        'O stock não foi alterado.',
                        'info',
                    )
                else:
                    flash(
                        'Este problema já tinha sido reportado.',
                        'warning',
                    )
            else:
                raise ValueError('Ação de receção inválida.')
        except ValueError as exc:
            flash(str(exc), 'warning')
        except Exception as exc:
            logger.exception(
                'Erro ao processar receção de transferência action=%s',
                action,
            )
            flash('Erro interno ao processar a transferência. Tente novamente.', 'danger')

        return redirect(url_for('vendas.transferencias', loja_id=loja_id))

    recentes = get_ordens_transferencia(loja_destino=loja_nome)
    pendentes = [
        o for o in recentes
        if o['status'] == 'confirmada'
        and o.get('rececao_estado') == 'por_verificar'
        and o.get('destino_tipo') == 'loja'
    ]
    confirmadas = [o for o in recentes if o not in pendentes][:20]

    batch_map = {}
    for o in pendentes:
        batch_key = o.get('batch_id') or f'_solo_{o["id"]}'
        if batch_key not in batch_map:
            batch_map[batch_key] = {
                'batch_id': o.get('batch_id'),
                'areas_origem': set(),
                'datas_previstas': set(),
                'datas': set(),
                'criadores': set(),
                'ordens': [],
            }
        grupo = batch_map[batch_key]
        grupo['areas_origem'].add(o['area_origem'])
        grupo['datas_previstas'].add(o.get('data_prevista') or o['data'])
        grupo['datas'].add(o['data'])
        if o.get('criado_por'):
            grupo['criadores'].add(o['criado_por'])
        grupo['ordens'].append(o)

    for g in batch_map.values():
        g['area_origem'] = (
            next(iter(g['areas_origem']))
            if len(g['areas_origem']) == 1
            else 'Misto'
        )
        g['data_prevista'] = (
            next(iter(g['datas_previstas']))
            if len(g['datas_previstas']) == 1
            else None
        )
        g['data'] = (
            next(iter(g['datas']))
            if len(g['datas']) == 1
            else None
        )
        g['criado_por'] = (
            next(iter(g['criadores']))
            if len(g['criadores']) == 1
            else None
        )
        g['ordem_ids_csv'] = ','.join(str(o['id']) for o in g['ordens'])

    grupos_pendentes = sorted(
        batch_map.values(),
        key=lambda g: (
            g['data_prevista'] or date.min,
            g['data'] or date.min,
        ),
    )

    return render_template('vendas/transferencias.html',
                           active_tab='transferencias',
                           tabs=_build_tabs('transferencias', loja_id),
                           loja_nome=loja_nome,
                           loja_id=loja_id,
                           grupos_pendentes=grupos_pendentes,
                           pendentes=pendentes,
                           confirmadas=confirmadas)


@vendas_bp.route('/transferir-gelado', methods=['GET', 'POST'])
@login_required
def transferir_gelado():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))

    loja_id, loja_nome = _get_user_loja()
    if not _check_store_capability(loja_id, 'loja_only'):
        return redirect(url_for('vendas.index', loja_id=loja_id))

    if request.method == 'POST':
        sabor = request.form.get('sabor', '').strip()
        destino_tipo = request.form.get('destino_tipo', 'loja').strip().lower()
        loja_destino = request.form.get('loja_destino', '').strip()
        destino_nome = request.form.get('destino_nome', '').strip()
        username = session.get('user', {}).get('username', 'system')

        try:
            quantidade_kg = round(float(request.form.get('quantidade', '0').replace(',', '.')), 3)
        except (ValueError, TypeError):
            quantidade_kg = 0.0

        if not sabor:
            flash('Seleccione um sabor.', 'error')
            return redirect(url_for('vendas.transferir_gelado', loja_id=loja_id))
        if quantidade_kg <= 0:
            flash('A quantidade deve ser superior a zero.', 'error')
            return redirect(url_for('vendas.transferir_gelado', loja_id=loja_id))
        active_store_names = {s['name'] for s in get_active_venda_stores()}
        if destino_tipo == 'b2b':
            if not destino_nome:
                flash('Indique a entidade destinatária para a transferência B2B.', 'error')
                return redirect(url_for('vendas.transferir_gelado', loja_id=loja_id))
            if len(destino_nome) > 255:
                flash('A entidade destinatária não pode ter mais de 255 caracteres.', 'error')
                return redirect(url_for('vendas.transferir_gelado', loja_id=loja_id))
            loja_destino = 'B2B'
        elif destino_tipo == 'loja':
            destino_nome = None
            if not loja_destino or loja_destino not in active_store_names:
                flash('Seleccione uma loja de destino válida.', 'error')
                return redirect(url_for('vendas.transferir_gelado', loja_id=loja_id))
        else:
            flash('Tipo de destino inválido.', 'error')
            return redirect(url_for('vendas.transferir_gelado', loja_id=loja_id))
        if destino_tipo == 'loja' and loja_destino == loja_nome:
            flash('A loja de destino não pode ser a mesma que a loja de origem.', 'error')
            return redirect(url_for('vendas.transferir_gelado', loja_id=loja_id))

        stock_efectivo = get_effective_stock_por_sabor(loja_nome)
        stock_disponivel = stock_efectivo.get(sabor, {}).get('kg', 0.0)
        if quantidade_kg > stock_disponivel:
            transferido_kg = stock_efectivo.get(sabor, {}).get('transferido_kg', 0.0)
            pesagem_kg = stock_disponivel + transferido_kg
            msg = (
                f'Quantidade ({quantidade_kg:.3f} kg) superior ao stock disponível '
                f'({stock_disponivel:.3f} kg de {sabor})'
            )
            if transferido_kg > 0:
                msg += f' — pesagem: {pesagem_kg:.3f} kg, já transferido: {transferido_kg:.3f} kg'
            flash(msg + '.', 'error')
            return redirect(url_for('vendas.transferir_gelado', loja_id=loja_id))

        try:
            ordem_id = criar_transferencia_entre_lojas(
                data=date.today(),
                sabor=sabor,
                loja_origem=loja_nome,
                loja_destino=loja_destino,
                quantidade_kg=quantidade_kg,
                criado_por=username,
                destino_tipo=destino_tipo,
                destino_nome=destino_nome,
            )
            destino_label = destino_nome if destino_tipo == 'b2b' else loja_destino
            flash(
                f'Transferência de {quantidade_kg:.3f} kg de {sabor} para {destino_label} '
                f'criada com sucesso (ordem #{ordem_id}).', 'success'
            )
        except Exception:
            logger.exception(
                'Erro ao criar transferência loja_origem=%s sabor=%s', loja_nome, sabor
            )
            flash('Erro interno ao criar a transferência. Tente novamente.', 'danger')

        return redirect(url_for('vendas.transferir_gelado', loja_id=loja_id))

    stock_efectivo = get_effective_stock_por_sabor(loja_nome)
    sabores_com_stock = sorted(
        [
            {'sabor': s, 'kg': v['kg'], 'data': v['data'], 'transferido_kg': v.get('transferido_kg', 0.0)}
            for s, v in stock_efectivo.items()
            if v['kg'] > 0 or v.get('transferido_kg', 0.0) > 0
        ],
        key=lambda x: x['sabor'],
    )

    all_lojas = get_active_venda_stores()
    lojas_destino = [l for l in all_lojas if l['name'] != loja_nome]

    from db.plano import get_ordens_transferencia as _get_ordens
    saidas = _get_ordens(loja_origem=loja_nome, limit=50)

    return render_template(
        'vendas/transferir_gelado.html',
        active_tab='transferir_gelado',
        tabs=_build_tabs('transferir_gelado', loja_id),
        loja_nome=loja_nome,
        loja_id=loja_id,
        sabores_com_stock=sabores_com_stock,
        lojas_destino=lojas_destino,
        today=str(date.today()),
        saidas=saidas,
    )


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
    # Cash-close data lives in fecho_caixa only. KPI/liquidity reports read
    # from vendas_detalhe (Gestor uploads); the legacy vendas table is no
    # longer written here.
    upsert_fecho_caixa(data_sel, loja_id, fields, username)

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
    if not _check_store_capability(loja_id, 'eod'):
        return jsonify({'ok': False, 'error': 'Sem suporte para pesagem nesta loja'}), 403

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
            # Cash-close data lives in fecho_caixa only. KPI/liquidity reports
            # read from vendas_detalhe (Gestor uploads); the legacy vendas table
            # is no longer written here.
            upsert_fecho_caixa(data_sel, loja_id, fields, username)

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


@vendas_bp.route('/fecho-historico')
@login_required
def fecho_historico():
    user = session.get('user', {})
    if not user.get('acesso_gestor'):
        flash('Acesso restrito a gestores.', 'danger')
        return redirect(url_for('home.index'))

    loja_id, loja_nome = _get_user_loja()

    # Filter parameters
    filtro_loja_id = None
    filtro_loja_raw = request.args.get('filtro_loja_id', '').strip()
    if filtro_loja_raw:
        try:
            filtro_loja_id = int(filtro_loja_raw)
        except (ValueError, TypeError):
            pass

    filtro_data_inicio = None
    filtro_data_fim = None
    data_inicio_raw = request.args.get('data_inicio', '').strip()
    data_fim_raw = request.args.get('data_fim', '').strip()
    try:
        if data_inicio_raw:
            filtro_data_inicio = datetime.strptime(data_inicio_raw, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        pass
    try:
        if data_fim_raw:
            filtro_data_fim = datetime.strptime(data_fim_raw, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        pass

    registos = list_fecho_caixa_all_stores(
        loja_id=filtro_loja_id,
        data_inicio=filtro_data_inicio,
        data_fim=filtro_data_fim,
    )

    all_stores = get_vendas_module_stores()

    return render_template('vendas/fecho_historico.html',
                           active_tab='fecho_historico',
                           loja_id=loja_id,
                           loja_nome=loja_nome,
                           registos=registos,
                           all_stores=all_stores,
                           filtro_loja_id=filtro_loja_id,
                           filtro_data_inicio=data_inicio_raw,
                           filtro_data_fim=data_fim_raw,
                           tabs=_build_tabs('fecho_historico', loja_id))


@vendas_bp.route('/fecho-historico/<int:record_id>/editar', methods=['GET', 'POST'])
@login_required
def fecho_historico_editar(record_id):
    user = session.get('user', {})
    if not user.get('acesso_gestor'):
        flash('Acesso restrito a gestores.', 'danger')
        return redirect(url_for('home.index'))

    registo = get_fecho_caixa_by_id(record_id)
    if not registo:
        flash('Registo não encontrado.', 'danger')
        return redirect(url_for('vendas.fecho_historico'))

    loja_id, loja_nome = _get_user_loja()
    registo_loja_id = registo.get('loja_id')
    registo_loja_nome = registo.get('loja') or loja_nome

    if request.method == 'POST':
        def _parse_dec(name):
            v = request.form.get(name, '').strip().replace(',', '.')
            try:
                return float(v) if v else None
            except ValueError:
                return None

        # Include None for submitted fields so manager can clear existing values.
        # Only skip fields absent from the form altogether.
        _numeric = ['total_moedas', 'valor_notas', 'total_caixa', 'envelope_sobra',
                    'total_vendas_pos', 'dinheiro_pos', 'cartao_pos', 'ubereats_pos', 'tpa_getnet']
        _text = ['colaborador', 'justificacao_desvio']
        fields = {}
        for name in _numeric:
            if name in request.form:
                fields[name] = _parse_dec(name)
        for name in _text:
            if name in request.form:
                fields[name] = request.form.get(name, '').strip() or None

        valores_anteriores = {k: v for k, v in registo.items() if k not in ('_empty',)}
        upsert_fecho_caixa_audited(
            registo['data'], registo['loja_id'], fields, user.get('username', 'gestor'),
            registo['id'], valores_anteriores,
        )
        flash('Registo atualizado com sucesso.', 'success')
        return redirect(url_for('vendas.fecho_historico'))

    audit_log = get_fecho_caixa_audit(record_id)
    return render_template('vendas/fecho_historico_editar.html',
                           active_tab='fecho_historico',
                           loja_id=loja_id,
                           loja_nome=registo_loja_nome,
                           registo=registo,
                           audit_log=audit_log)


@vendas_bp.route('/fecho-historico/<int:record_id>/eliminar', methods=['POST'])
@login_required
def fecho_historico_eliminar(record_id):
    user = session.get('user', {})
    if not user.get('acesso_gestor'):
        flash('Acesso restrito a gestores.', 'danger')
        return redirect(url_for('home.index'))

    registo = get_fecho_caixa_by_id(record_id)
    if not registo:
        flash('Registo não encontrado.', 'danger')
        return redirect(url_for('vendas.fecho_historico'))

    loja_id, loja_nome = _get_user_loja()

    data_str = registo['data_str'] if registo.get('data_str') else str(registo.get('data', ''))

    valores_anteriores = {k: v for k, v in registo.items() if k not in ('_empty',)}
    delete_fecho_caixa_audited(record_id, user.get('username', 'gestor'), valores_anteriores)
    flash(f'Registo de {data_str} ({registo.get("loja", "")}) eliminado.', 'success')
    return redirect(url_for('vendas.fecho_historico'))


@vendas_bp.route('/sabores-ativos', methods=['GET', 'POST'])
@login_required
def sabores_ativos():
    if not _check_vendas_access():
        return redirect(url_for('home.index'))
    loja_id, loja_nome = _get_user_loja()

    if request.method == 'POST':
        changes = 0
        receitas_list = get_all_receitas_gelado()
        for r in receitas_list:
            new_nome_corrente = request.form.get(f'nome_corrente_{r["id"]}', '').strip() or None
            new_ativo = request.form.get(f'ativo_{r["id"]}') == 'on'
            nome_corrente_changed = new_nome_corrente != (r['nome_corrente'] or None)
            ativo_changed = new_ativo != r['ativo']
            if nome_corrente_changed:
                update_receita_gelado(r['id'], r['nome'], new_nome_corrente)
            if ativo_changed:
                update_receita_gelado_ativo(r['id'], new_ativo)
            if nome_corrente_changed or ativo_changed:
                changes += 1
        if changes > 0:
            flash(f"{changes} sabor(es) atualizado(s)!", "success")
        else:
            flash("Nenhuma alteração detetada.", "info")
        return redirect(url_for('vendas.sabores_ativos', loja_id=loja_id))

    receitas_list = get_all_receitas_gelado()
    receitas_list.sort(key=lambda r: (not r['ativo'], (r['nome_corrente'] or r['nome']).lower()))
    return render_template('vendas/sabores_ativos.html',
                           loja_id=loja_id,
                           loja_nome=loja_nome,
                           receitas=receitas_list)
