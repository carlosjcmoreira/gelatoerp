"""Navigation service: computes the list of accessible modules for a user.

Shared between the Jinja2 context processor (injected globally into all
templates) and the home route (used to compute the redirect target).
"""
import logging
from flask import g, url_for
import database as db

logger = logging.getLogger(__name__)

MOBILE_NAV_PRIMARY_COUNT = 4

_PAGE_DEFS = [
    ('acesso_eurokg',        '📊', 'Euro/kg',             'Euro/kg',    'eurokg.index',     '/eurokg'),
    ('acesso_producao',      '🍨', 'Produção Gelado',      'Gelado',     'producao.index',   '/producao'),
    ('acesso_pastelaria',    '🍡', 'Produção Pastelaria',  'Pastelaria', 'pastelaria.index', '/pastelaria'),
    ('acesso_confeitaria',   '🍪', 'Produção Confeitaria', 'Confeit.',   'confeitaria.index','/confeitaria'),
    ('acesso_compras',       '🛍️', 'Compras e Faturas',    'Compras',    'compras.index',    '/compras'),
    ('acesso_administrativo','🚚', 'Logística',            'Logística',  'logistica.index',  '/logistica'),
    ('acesso_tarefas',       '✅', 'Tarefas',              'Tarefas',    'tarefas.index',    '/tarefas'),
    ('acesso_gestor',        '👔', 'Gestor',               'Gestor',     'gestor.index',     '/gestor'),
    ('acesso_financeiro',    '💰', 'Financeiro',           'Financeiro', 'financeiro.index', '/financeiro'),
    ('acesso_gestor',        '🤖', 'Agente Scoopy',        'Scoopy IA',  'agente.index',     '/agente'),
    ('acesso_contabilidade', '📒', 'Contabilidade',        'Contab.',    'contabilidade.index', '/contabilidade'),
]


def compute_nav_pages(user: dict) -> list:
    """Return list of navigable module dicts for *user*.

    The first entry is always the dashboard (home) link.
    Each dict has: icon, label, short_label, url, prefix.
    Vendas entries additionally carry a loja_id key for precise active-state
    detection when multiple stores share the /vendas prefix.

    Must be called inside a Flask request/application context (url_for).
    """
    # The home route also calls this function before rendering the template.
    # Keep the result request-local: a process-wide cache would risk serving
    # one user's permissions to another, while recalculating it doubles the
    # navigation/configuration lookups on the dashboard.
    cached = g.get('_nav_pages')
    if cached is not None:
        return cached

    try:
        from db.tiles import get_module_labels, get_module_icons
        module_labels = get_module_labels()
        module_icons = get_module_icons()
    except Exception as exc:
        logger.warning("compute_nav_pages: failed to load module_labels: %s", exc)
        module_labels = {}
        module_icons = {}

    pages = []

    pages.append({
        'icon': '🏠', 'label': 'Dashboard', 'short_label': 'Início',
        'url': url_for('home.index'), 'prefix': '/',
        'is_home': True,
    })

    for perm, icon, label, short_label, route, prefix in _PAGE_DEFS:
        if user.get(perm) or user.get('acesso_gestor'):
            module_key = prefix.lstrip('/')
            custom = module_labels.get(module_key)
            custom_icon = module_icons.get(module_key)
            pages.append({
                'icon': custom_icon if custom_icon else icon,
                'label': custom if custom else label,
                'short_label': custom[:7] if custom else short_label,
                'url': url_for(route), 'prefix': prefix,
            })

    try:
        venda_stores = db.get_vendas_module_stores()
        is_gestor = user.get('acesso_gestor')
        vendas_store_ids = set(user.get('vendas_store_ids') or [])
        custom_vendas = module_labels.get('vendas')
        custom_vendas_icon = module_icons.get('vendas', '🛒')
        for s in venda_stores:
            if is_gestor or s['id'] in vendas_store_ids:
                sname = s['name']
                label_v = f'{custom_vendas} {sname}' if custom_vendas else f'Vendas {sname}'
                pages.append({
                    'icon': custom_vendas_icon,
                    'label': label_v,
                    'short_label': sname[:7],
                    'url': url_for('vendas.index', loja_id=s['id']),
                    'prefix': '/vendas',
                    'loja_id': str(s['id']),
                })
    except Exception as exc:
        logger.warning("compute_nav_pages: failed to load vendas stores: %s", exc)

    if user.get('acesso_gestor'):
        try:
            for s in db.get_active_landing_stores():
                pages.append({
                    'icon': '🏪',
                    'label': s['name'],
                    'short_label': s['name'][:7],
                    'url': url_for('store_placeholder.index', store_id=s['id']),
                    'prefix': f"/loja/{s['id']}",
                })
        except Exception as exc:
            logger.warning("compute_nav_pages: failed to load landing stores: %s", exc)

    if user.get('acesso_eventos') or user.get('acesso_gestor'):
        custom_eventos = module_labels.get('eventos')
        custom_eventos_icon = module_icons.get('eventos', '🎪')
        pages.append({
            'icon': custom_eventos_icon,
            'label': custom_eventos if custom_eventos else 'Eventos',
            'short_label': (custom_eventos[:7] if custom_eventos else 'Eventos'),
            'url': url_for('eventos.index'),
            'prefix': '/eventos',
        })

    g._nav_pages = pages
    return pages
