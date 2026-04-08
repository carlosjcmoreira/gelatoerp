"""Navigation service: computes the list of accessible modules for a user.

Shared between the Jinja2 context processor (injected globally into all
templates) and the home route (used to compute the redirect target).
"""
import logging
from flask import url_for
import database as db

logger = logging.getLogger(__name__)

MOBILE_NAV_PRIMARY_COUNT = 4

_PAGE_DEFS = [
    ('acesso_eurokg',        '📊', 'Euro/kg',             'Euro/kg',    'eurokg.index',     '/eurokg'),
    ('acesso_producao',      '🍨', 'Produção Gelado',      'Gelado',     'producao.index',   '/producao'),
    ('acesso_pastelaria',    '🍡', 'Produção Pastelaria',  'Pastelaria', 'pastelaria.index', '/pastelaria'),
    ('acesso_confeitaria',   '🍪', 'Produção Confeitaria', 'Confeit.',   'confeitaria.index','/confeitaria'),
    ('acesso_administrativo','🛍️', 'Compras e Faturas',    'Compras',    'compras.index',    '/compras'),
    ('acesso_administrativo','🚚', 'Logística',            'Logística',  'logistica.index',  '/logistica'),
    ('acesso_gestor',        '👔', 'Gestor',               'Gestor',     'gestor.index',     '/gestor'),
    ('acesso_financeiro',    '💰', 'Financeiro',           'Financeiro', 'financeiro.index', '/financeiro'),
]


def compute_nav_pages(user: dict) -> list:
    """Return list of navigable module dicts for *user*.

    Each dict has: icon, label, short_label, url, prefix.
    Vendas entries additionally carry a loja_id key for precise active-state
    detection when multiple stores share the /vendas prefix.

    Must be called inside a Flask request/application context (url_for).
    """
    pages = []

    for perm, icon, label, short_label, route, prefix in _PAGE_DEFS:
        if user.get(perm) or user.get('acesso_gestor'):
            pages.append({
                'icon': icon, 'label': label, 'short_label': short_label,
                'url': url_for(route), 'prefix': prefix,
            })

    try:
        venda_stores = db.get_active_venda_stores()
        is_gestor = user.get('acesso_gestor')
        vendas_store_ids = set(user.get('vendas_store_ids') or [])
        for s in venda_stores:
            if is_gestor or s['id'] in vendas_store_ids:
                sname = s['name']
                pages.append({
                    'icon': '🛒',
                    'label': f'Vendas {sname}',
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
        pages.append({
            'icon': '🎪',
            'label': 'Eventos',
            'short_label': 'Eventos',
            'url': url_for('eventos.index'),
            'prefix': '/eventos',
        })

    return pages
