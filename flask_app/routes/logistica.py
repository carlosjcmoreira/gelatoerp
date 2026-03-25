from flask import Blueprint, render_template, url_for
from flask_app.auth import perm_required
from datetime import date
from collections import defaultdict
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import get_ordens_transferencia

logistica_bp = Blueprint('logistica', __name__)

TABS = [
    {'id': 'transferencias', 'label': 'Transferências Agendadas', 'icon': '📅', 'url_endpoint': 'logistica.transferencias_agendadas'},
    {'id': 'ordens', 'label': 'Ordens de Transferência', 'icon': '📄', 'url_endpoint': 'logistica.ordens'},
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
