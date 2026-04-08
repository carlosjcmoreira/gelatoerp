import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from datetime import date
from flask import Blueprint, render_template, session, url_for
from flask_app.auth import login_required
from flask_app.services.navigation import compute_nav_pages
import database as db
from db import dashboard as dash

home_bp = Blueprint('home', __name__)


def _build_widgets(user: dict, nav_pages: list) -> list:
    """Build the list of dashboard widget dicts based on user permissions.

    Each widget dict has: id, icon, label, url, stats (list of {label, value, cls}).
    An optional `_error` key is set to True when data could not be loaded.
    """
    widgets = []
    is_gestor = bool(user.get('acesso_gestor'))

    if user.get('acesso_eurokg') or is_gestor:
        data = dash.widget_eurokg()
        stats = []
        if data.get('_error'):
            stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            if data['preco_atual'] is not None:
                stats.append({'label': 'Preço/kg atual', 'value': f"{data['preco_atual']:.2f} €/kg", 'cls': ''})
            if data['ultima_pesagem']:
                dias = data['dias_sem_pesagem']
                cls = 'text-warning' if dias and dias > 7 else ''
                stats.append({'label': 'Última pesagem', 'value': data['ultima_pesagem'].strftime('%d/%m/%Y'), 'cls': cls})
        widgets.append({
            'id': 'eurokg', 'icon': '📊', 'label': 'Euro/kg',
            'url': url_for('eurokg.index'), 'stats': stats,
        })

    if user.get('acesso_producao') or is_gestor:
        data = dash.widget_producao_gelado()
        stats = []
        if data.get('_error'):
            stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            kg = data['kg_hoje']
            cls = 'text-success' if kg > 0 else 'text-muted'
            stats.append({'label': 'Produzido hoje', 'value': f"{kg:.1f} kg", 'cls': cls})
            if data['ultima_producao']:
                stats.append({'label': 'Última produção', 'value': data['ultima_producao'].strftime('%d/%m/%Y'), 'cls': ''})
        widgets.append({
            'id': 'producao', 'icon': '🍨', 'label': 'Produção Gelado',
            'url': url_for('producao.index'), 'stats': stats,
        })

    if user.get('acesso_pastelaria') or is_gestor:
        data = dash.widget_pastelaria()
        stats = []
        if data.get('_error'):
            stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            cls = 'text-success' if data['itens_hoje'] > 0 else 'text-muted'
            stats.append({'label': 'Itens hoje', 'value': str(data['itens_hoje']), 'cls': cls})
            if data['ultima_producao']:
                stats.append({'label': 'Última produção', 'value': data['ultima_producao'].strftime('%d/%m/%Y'), 'cls': ''})
        widgets.append({
            'id': 'pastelaria', 'icon': '🍡', 'label': 'Produção Pastelaria',
            'url': url_for('pastelaria.index'), 'stats': stats,
        })

    if user.get('acesso_confeitaria') or is_gestor:
        data = dash.widget_confeitaria()
        stats = []
        if data.get('_error'):
            stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            cls = 'text-success' if data['itens_hoje'] > 0 else 'text-muted'
            stats.append({'label': 'Itens hoje', 'value': str(data['itens_hoje']), 'cls': cls})
            stats.append({'label': 'Produtos ativos', 'value': str(data['n_produtos']), 'cls': ''})
        widgets.append({
            'id': 'confeitaria', 'icon': '🍪', 'label': 'Produção Confeitaria',
            'url': url_for('confeitaria.index'), 'stats': stats,
        })

    if user.get('acesso_administrativo') or is_gestor:
        data = dash.widget_faturas()
        stats = []
        if data.get('_error'):
            stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            cls = 'text-danger' if data['pending'] > 0 else 'text-muted'
            stats.append({'label': 'Para rever', 'value': str(data['pending']), 'cls': cls})
            cls2 = 'text-warning' if data['vencidas'] > 0 else ''
            stats.append({'label': 'Agendadas / vencidas', 'value': f"{data['agendadas']} / {data['vencidas']}", 'cls': cls2})
        widgets.append({
            'id': 'faturas', 'icon': '🛍️', 'label': 'Compras e Faturas',
            'url': url_for('compras.index'), 'stats': stats,
        })

        data2 = dash.widget_logistica()
        stats2 = []
        if data2.get('_error'):
            stats2.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            cls = 'text-warning' if data2['pendentes'] > 0 else 'text-muted'
            stats2.append({'label': 'Pendentes', 'value': str(data2['pendentes']), 'cls': cls})
            stats2.append({'label': 'Em curso', 'value': str(data2['em_curso']), 'cls': ''})
        widgets.append({
            'id': 'logistica', 'icon': '🚚', 'label': 'Logística',
            'url': url_for('logistica.index'), 'stats': stats2,
        })

    if is_gestor:
        data = dash.widget_gestor()
        stats = []
        if data.get('_error'):
            stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            stats.append({'label': 'Utilizadores ativos', 'value': str(data['ativos']), 'cls': ''})
            if data['inativos']:
                stats.append({'label': 'Inativos', 'value': str(data['inativos']), 'cls': 'text-muted'})
        widgets.append({
            'id': 'gestor', 'icon': '👔', 'label': 'Gestor',
            'url': url_for('gestor.index'), 'stats': stats,
        })

    if user.get('acesso_financeiro') or is_gestor:
        data = dash.widget_financeiro()
        stats = []
        if data.get('_error'):
            stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            stats.append({'label': 'Contratos crédito', 'value': str(data['creditos']), 'cls': ''})
            stats.append({'label': 'Confirming ativos', 'value': str(data['confirming']), 'cls': ''})
        widgets.append({
            'id': 'financeiro', 'icon': '💰', 'label': 'Financeiro',
            'url': url_for('financeiro.index'), 'stats': stats,
        })

    try:
        venda_stores = db.get_active_venda_stores()
        vendas_store_ids = set(user.get('vendas_store_ids') or [])
        for s in venda_stores:
            if is_gestor or s['id'] in vendas_store_ids:
                data = dash.widget_vendas(s['id'], s['name'])
                stats = []
                if data.get('_error'):
                    stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
                else:
                    if data['vendas_hoje'] is not None:
                        stats.append({'label': 'Vendas hoje', 'value': f"{data['vendas_hoje']:.2f} €", 'cls': 'text-success'})
                    else:
                        stats.append({'label': 'Vendas hoje', 'value': 'Não registado', 'cls': 'text-muted'})
                    if data['ultima_data']:
                        stats.append({'label': 'Último registo', 'value': data['ultima_data'].strftime('%d/%m/%Y'), 'cls': ''})
                widgets.append({
                    'id': f"vendas_{s['id']}", 'icon': '🛒',
                    'label': f"Vendas {s['name']}", 'url': url_for('vendas.index', loja_id=s['id']),
                    'stats': stats,
                })
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("home dashboard: failed to load vendas stores: %s", exc)

    if user.get('acesso_eventos') or is_gestor:
        data = dash.widget_eventos()
        stats = []
        if data.get('_error'):
            stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            stats.append({'label': 'Confirmados (7 dias)', 'value': str(data['proximos_confirmados']), 'cls': ''})
            if data['proximo_nome']:
                d_str = data['proximo_data'].strftime('%d/%m') if data['proximo_data'] else ''
                stats.append({'label': 'Próximo', 'value': f"{data['proximo_nome'][:24]} {d_str}", 'cls': ''})
            if data['leads_pendentes']:
                stats.append({'label': 'Leads pendentes', 'value': str(data['leads_pendentes']), 'cls': 'text-warning'})
        widgets.append({
            'id': 'eventos', 'icon': '🎪', 'label': 'Eventos',
            'url': url_for('eventos.index'), 'stats': stats,
        })

    return widgets


@home_bp.route('/')
@login_required
def index():
    user = session['user']
    pages = compute_nav_pages(user)

    if not pages:
        return render_template('home.html', no_access=True)

    widgets = _build_widgets(user, pages)
    today = date.today()
    return render_template('dashboard.html', widgets=widgets, today=today)
