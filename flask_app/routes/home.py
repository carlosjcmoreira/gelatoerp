import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import database as db
from flask import Blueprint, render_template, session, redirect, url_for
from flask_app.auth import login_required

home_bp = Blueprint('home', __name__)


@home_bp.route('/')
@login_required
def index():
    user = session['user']
    pages = []

    page_defs = [
        ('acesso_eurokg', '📊', 'Euro/kg', 'eurokg.index'),
        ('acesso_producao', '🍨', 'Produção Gelado', 'producao.index'),
        ('acesso_pastelaria', '🍡', 'Produção Pastelaria', 'pastelaria.index'),
        ('acesso_confeitaria', '🍪', 'Produção Confeitaria', 'confeitaria.index'),
        ('acesso_administrativo', '🛍️', 'Compras e Faturas', 'compras.index'),
        ('acesso_administrativo', '🚚', 'Logística', 'logistica.index'),
        ('acesso_gestor', '👔', 'Gestor', 'gestor.index'),
        ('acesso_financeiro', '💰', 'Financeiro', 'financeiro.index'),
    ]

    for perm, icon, label, route in page_defs:
        if user.get(perm) or user.get('acesso_gestor'):
            pages.append({'icon': icon, 'label': label, 'url': url_for(route)})

    # Vendas tiles: one per active 'loja' store, filtered by user's vendas_store_ids
    try:
        venda_stores = db.get_active_venda_stores()
        is_gestor = user.get('acesso_gestor')
        vendas_store_ids = set(user.get('vendas_store_ids') or [])

        for s in venda_stores:
            if is_gestor or s['id'] in vendas_store_ids:
                pages.append({
                    'icon': '🛒',
                    'label': f"Vendas {s['name']}",
                    'url': url_for('vendas.index', loja_id=s['id']),
                })
    except Exception:
        pass

    # Store-driven landing tiles (shows_on_landing=True, gestor only)
    if user.get('acesso_gestor'):
        try:
            landing_stores = db.get_active_landing_stores()
            for s in landing_stores:
                pages.append({'icon': '🏪', 'label': s['name'], 'url': url_for('store_placeholder.index', store_id=s['id'])})
        except Exception:
            pass

    # Eventos: controlled by acesso_eventos (gestor always sees it)
    if user.get('acesso_eventos') or user.get('acesso_gestor'):
        pages.append({'icon': '🎪', 'label': 'Eventos', 'url': url_for('eventos.index')})

    if not pages:
        return render_template('home.html', pages=[], no_access=True)

    # Auto-redirect for vendas-only users with a single page
    non_eventos = [p for p in pages if p.get('label') != 'Eventos']
    if len(non_eventos) == 1 and non_eventos[0].get('label', '').startswith('Vendas '):
        return redirect(non_eventos[0]['url'])

    return render_template('home.html', pages=pages, no_access=False)
