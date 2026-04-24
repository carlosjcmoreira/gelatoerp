import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import logging
from datetime import date
from flask import Blueprint, render_template, session, url_for
from flask_app.auth import login_required
from flask_app.services.navigation import compute_nav_pages
import database as db
from db import dashboard as dash

logger = logging.getLogger(__name__)

home_bp = Blueprint('home', __name__)


def _build_widgets(user: dict) -> list:
    """Build the list of dashboard widget dicts based on user permissions.

    Each widget dict has: id, icon, label, url, stats (list of {label, value, cls}).
    """
    widgets = []
    is_gestor = bool(user.get('acesso_gestor'))

    if user.get('acesso_eurokg') or is_gestor:
        data = dash.widget_eurokg()
        stats = []
        if data.get('_error'):
            stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            if data['media_euro_kg'] is not None:
                stats.append({'label': 'Média €/kg (mês)', 'value': f"{data['media_euro_kg']:.2f} €/kg", 'cls': ''})
            else:
                stats.append({'label': 'Média €/kg (mês)', 'value': 'Sem dados', 'cls': 'text-muted'})
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
            n = data['transferencias_pendentes']
            cls2 = 'text-warning' if n > 0 else 'text-muted'
            stats.append({'label': 'Transferências pendentes', 'value': str(n), 'cls': cls2})
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
            stats.append({'label': 'Stock acumulado', 'value': str(data['stock_total']), 'cls': ''})
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
            stats.append({'label': 'Por rever', 'value': str(data['pending']), 'cls': cls})
            cls2 = 'text-warning' if data['vencidas'] > 0 else ''
            stats.append({'label': 'Agendadas / vencidas', 'value': f"{data['agendadas']} / {data['vencidas']}", 'cls': cls2})
            stats.append({'label': 'Pagas este mês', 'value': str(data['pagas_mes']), 'cls': 'text-muted'})
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
            if data.get('ultimo_login'):
                stats.append({'label': 'Último login', 'value': data['ultimo_login'].strftime('%d/%m/%Y %H:%M'), 'cls': ''})
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
            if data['confirming'] > 0:
                stats.append({'label': 'Confirming (exposição)', 'value': f"{data['exposicao']:,.0f} €", 'cls': ''})
            else:
                stats.append({'label': 'Confirming ativos', 'value': str(data['confirming']), 'cls': 'text-muted'})
        widgets.append({
            'id': 'financeiro', 'icon': '💰', 'label': 'Financeiro',
            'url': url_for('financeiro.index'), 'stats': stats,
        })

    try:
        venda_stores = db.get_vendas_module_stores()
        vendas_store_ids = set(user.get('vendas_store_ids') or [])
        for s in venda_stores:
            if is_gestor or s['id'] in vendas_store_ids:
                data = dash.widget_vendas(s['id'], s['name'])
                stats = []
                if data.get('_error'):
                    stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
                else:
                    is_producao = s.get('store_type') == 'producao'
                    if is_producao:
                        if data['registado_hoje']:
                            total_str = f"{data['vendas_hoje']:.2f} €" if data['vendas_hoje'] is not None else '—'
                            stats.append({'label': 'Fecho de caixa', 'value': f'Fechado — {total_str}', 'cls': 'text-success'})
                        else:
                            stats.append({'label': 'Fecho de caixa', 'value': 'Não registado', 'cls': 'text-muted'})
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
        logger.warning("home dashboard: failed to load vendas stores: %s", exc)

    if user.get('acesso_eventos') or is_gestor:
        data = dash.widget_eventos()
        stats = []
        if data.get('_error'):
            stats.append({'label': 'Sem dados', 'value': '—', 'cls': 'text-muted'})
        else:
            stats.append({'label': 'Confirmados (7 dias)', 'value': str(data['proximos_confirmados']), 'cls': ''})
            if data['proximo_nome']:
                d_str = data['proximo_data'].strftime('%d/%m') if data['proximo_data'] else ''
                stats.append({'label': 'Próximo', 'value': f"{data['proximo_nome'][:22]} {d_str}".strip(), 'cls': ''})
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

    real_pages = [p for p in pages if not p.get('is_home')]
    if not real_pages:
        return render_template('home.html', no_access=True)

    widgets = _build_widgets(user)
    today = date.today()
    return render_template('dashboard.html', widgets=widgets, today=today)
