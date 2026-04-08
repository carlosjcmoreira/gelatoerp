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
        ('acesso_eurokg', 'eurokg.index'),
        ('acesso_producao', 'producao.index'),
        ('acesso_pastelaria', 'pastelaria.index'),
        ('acesso_confeitaria', 'confeitaria.index'),
        ('acesso_administrativo', 'compras.index'),
        ('acesso_administrativo', 'logistica.index'),
        ('acesso_gestor', 'gestor.index'),
        ('acesso_financeiro', 'financeiro.index'),
    ]

    seen_routes = set()
    for perm, route in page_defs:
        if route not in seen_routes and (user.get(perm) or user.get('acesso_gestor')):
            seen_routes.add(route)
            pages.append(url_for(route))

    try:
        venda_stores = db.get_active_venda_stores()
        is_gestor = user.get('acesso_gestor')
        vendas_store_ids = set(user.get('vendas_store_ids') or [])
        for s in venda_stores:
            if is_gestor or s['id'] in vendas_store_ids:
                pages.append(url_for('vendas.index', loja_id=s['id']))
    except Exception:
        pass

    if user.get('acesso_eventos') or user.get('acesso_gestor'):
        pages.append(url_for('eventos.index'))

    if not pages:
        return render_template('home.html', no_access=True)

    return redirect(pages[0])
