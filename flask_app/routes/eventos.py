import os
import sys
from datetime import datetime, date
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from flask_app.auth import login_required, perm_required

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import database as db
from database import (
    get_all_eventos, get_evento_by_id, create_evento, update_evento, delete_evento,
    upsert_evento_items, registar_pagamento_evento, get_eventos_recebimentos,
)
from db.pagamentos import VAT_RATES

eventos_bp = Blueprint('eventos', __name__)


def _parse_taxa_iva(raw):
    """Parse a per-line IVA rate submitted as a percentage (e.g. '13'). Defaults to
    the configured events/catering estimate rate when missing or invalid."""
    try:
        pct = float((raw or '').replace(',', '.'))
        if pct < 0 or pct > 100:
            raise ValueError
        return round(pct / 100, 4)
    except (TypeError, ValueError):
        return VAT_RATES['events_catering']

STATUS_LABELS = {
    'lead':          'Lead',
    'contacted':     'Contactado',
    'proposal_sent': 'Proposta Enviada',
    'negotiating':   'Em Negociação',
    'won':           'Adjudicado',
    'lost':          'Perdido',
    'cancelled':     'Cancelado',
}

STATUS_COLORS = {
    'lead':          'secondary',
    'contacted':     'info',
    'proposal_sent': 'primary',
    'negotiating':   'warning',
    'won':           'success',
    'lost':          'danger',
    'cancelled':     'dark',
}

VALID_TRANSITIONS = {
    'lead':          ['contacted', 'lost', 'cancelled'],
    'contacted':     ['proposal_sent', 'negotiating', 'lost', 'cancelled'],
    'proposal_sent': ['negotiating', 'won', 'lost', 'cancelled'],
    'negotiating':   ['won', 'lost', 'cancelled'],
    'won':           ['cancelled'],
    'lost':          ['lead'],
    'cancelled':     ['lead'],
}

LEAD_TRANSITIONS = {
    'lead':      ['contacted', 'lost', 'cancelled'],
    'contacted': ['lost', 'cancelled'],
    'lost':      ['lead'],
    'cancelled': ['lead'],
}

EVENTO_STATUS_LABELS = {
    'proposta': ('Proposta', 'secondary'),
    'adjudicado': ('Adjudicado', 'success'),
    'cancelado': ('Cancelado', 'danger'),
    'concluido': ('Concluído', 'primary'),
}

PAYMENT_STATUS_LABELS = {
    'pending': ('Pendente', 'warning'),
    'received': ('Recebido', 'success'),
    'partial': ('Parcial', 'info'),
    'overdue': ('Em atraso', 'danger'),
}


def _parse_event_type(form):
    radio = form.get('event_type_radio', '').strip()
    if radio == 'Outro':
        return form.get('event_type_other', '').strip()
    return radio

TABS = [
    {'id': 'dashboard', 'label': 'Dashboard', 'icon': '📊', 'url_endpoint': 'eventos.dashboard'},
    {'id': 'pipeline',  'label': 'Pipeline',  'icon': '📋', 'url_endpoint': 'eventos.pipeline'},
    {'id': 'leads',     'label': 'Leads do Formulário', 'icon': '📥', 'url_endpoint': 'eventos.leads'},
    {'id': 'clientes',  'label': 'Clientes',  'icon': '👥', 'url_endpoint': 'eventos.clientes'},
    {'id': 'artigos',   'label': 'Artigos',   'icon': '🏷️', 'url_endpoint': 'eventos.artigos'},
    {'id': 'recebimentos', 'label': 'Recebimentos', 'icon': '💶', 'url_endpoint': 'eventos.recebimentos'},
]


def _get_tabs():
    from db.tiles import get_tile_visibility, get_tile_labels, get_tile_icons
    visibility = get_tile_visibility('eventos')
    labels = get_tile_labels('eventos')
    icons = get_tile_icons('eventos')
    return [
        {'id': t['id'], 'label': labels.get(t['id']) or t['label'], 'icon': icons.get(t['id']) or t['icon'], 'url': url_for(t['url_endpoint'])}
        for t in TABS
        if visibility.get(t['id'], True)
    ]


def _compute_payment_status(evento):
    """Auto-compute overdue status for eventos table entries."""
    ps = evento.get('payment_status', 'pending')
    if ps in ('received', 'cancelled'):
        return ps
    if evento.get('expected_payment_date') and evento['expected_payment_date'] < date.today() and ps == 'pending':
        return 'overdue'
    return ps


def _parse_decimal(s):
    try:
        return float(str(s).replace(',', '.').strip())
    except (ValueError, AttributeError):
        return None


@eventos_bp.route('/')
@login_required
def index():
    from db.tiles import get_module_labels
    custom_mod = get_module_labels().get('eventos')
    menu_title = f'🎪 {custom_mod}' if custom_mod else '🎪 Eventos'
    tabs = _get_tabs()
    tiles = [{'icon': t['icon'], 'label': t['label'], 'url': t['url']} for t in tabs]
    return render_template('eventos/index.html', tiles=tiles, menu_title=menu_title)


# ── Dashboard ──────────────────────────────────────────────────────────────────

@eventos_bp.route('/dashboard')
@login_required
def dashboard():
    stats = db.get_pipeline_dashboard()
    tabs = _get_tabs()
    all_statuses = db.EVENT_STATUSES
    return render_template('eventos/dashboard.html',
                           stats=stats,
                           all_statuses=all_statuses,
                           status_labels=STATUS_LABELS,
                           status_colors=STATUS_COLORS,
                           tabs=tabs,
                           active_tab='dashboard')


# ── Pipeline ───────────────────────────────────────────────────────────────────

@eventos_bp.route('/pipeline')
@login_required
def pipeline():
    status_filter = request.args.get('status', '')
    events = db.get_events(status=status_filter if status_filter else None)
    all_statuses = db.EVENT_STATUSES
    tabs = _get_tabs()
    return render_template('eventos/pipeline.html',
                           events=events,
                           status_filter=status_filter,
                           all_statuses=all_statuses,
                           status_labels=STATUS_LABELS,
                           status_colors=STATUS_COLORS,
                           valid_transitions=VALID_TRANSITIONS,
                           tabs=tabs,
                           active_tab='pipeline')


# ── Event detail / edit ────────────────────────────────────────────────────────

@eventos_bp.route('/evento/novo', methods=['GET', 'POST'])
@login_required
def novo_evento():
    if request.method == 'POST':
        def _int_or_none(v):
            try:
                return int(v) if v else None
            except ValueError:
                return None

        def _date_or_none(v):
            try:
                return datetime.strptime(v, '%Y-%m-%d').date() if v else None
            except ValueError:
                return None

        data = {
            'lead_id': _int_or_none(request.form.get('lead_id')),
            'event_name': request.form.get('event_name', '').strip(),
            'event_type': _parse_event_type(request.form),
            'event_date': _date_or_none(request.form.get('event_date')),
            'event_time': request.form.get('event_time', '').strip(),
            'event_end_time': request.form.get('event_end_time', '').strip(),
            'estimated_guests': _int_or_none(request.form.get('estimated_guests')),
            'venue': request.form.get('venue', '').strip(),
            'venue_address': request.form.get('venue_address', '').strip(),
            'client_name': request.form.get('client_name', '').strip(),
            'client_email': request.form.get('client_email', '').strip(),
            'client_phone': request.form.get('client_phone', '').strip(),
            'status': 'lead',
            'internal_notes': request.form.get('internal_notes', '').strip(),
        }
        event_id = db.create_event(data)
        client_id = _int_or_none(request.form.get('client_id'))
        if client_id:
            db.link_event_client(event_id, client_id)
        flash('Evento criado com sucesso!', 'success')
        return redirect(url_for('eventos.evento_detail', event_id=event_id))

    tabs = _get_tabs()
    return render_template('eventos/evento_form.html',
                           event=None,
                           status_labels=STATUS_LABELS,
                           tabs=tabs,
                           active_tab='pipeline')


@eventos_bp.route('/evento/<int:event_id>', methods=['GET', 'POST'])
@login_required
def evento_detail(event_id):
    event = db.get_event(event_id)
    if not event:
        flash('Evento não encontrado.', 'error')
        return redirect(url_for('eventos.pipeline'))

    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action == 'save':
            def _int_or_none(v):
                try:
                    return int(v) if v else None
                except ValueError:
                    return None

            def _date_or_none(v):
                try:
                    return datetime.strptime(v, '%Y-%m-%d').date() if v else None
                except ValueError:
                    return None

            data = {
                'event_name': request.form.get('event_name', '').strip(),
                'event_type': _parse_event_type(request.form),
                'event_date': _date_or_none(request.form.get('event_date')),
                'event_time': request.form.get('event_time', '').strip(),
                'event_end_time': request.form.get('event_end_time', '').strip(),
                'estimated_guests': _int_or_none(request.form.get('estimated_guests')),
                'venue': request.form.get('venue', '').strip(),
                'venue_address': request.form.get('venue_address', '').strip(),
                'client_name': request.form.get('client_name', '').strip(),
                'client_email': request.form.get('client_email', '').strip(),
                'client_phone': request.form.get('client_phone', '').strip(),
                'status': event['status'],
                'loss_reason': event['loss_reason'],
                'internal_notes': request.form.get('internal_notes', '').strip(),
            }
            db.update_event(event_id, data)
            def _int_or_none2(v):
                try:
                    return int(v) if v else None
                except ValueError:
                    return None
            client_id = _int_or_none2(request.form.get('client_id'))
            if client_id:
                db.link_event_client(event_id, client_id)
            flash('Evento guardado.', 'success')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'transition':
            new_status = request.form.get('new_status', '')
            loss_reason = request.form.get('loss_reason', '').strip()
            ok, msg = db.transition_event_status(event_id, new_status, loss_reason)
            if ok:
                flash(f'Estado alterado para «{STATUS_LABELS.get(new_status, new_status)}».', 'success')
            else:
                flash(f'Erro: {msg}', 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'delete':
            db.delete_event(event_id)
            flash('Evento eliminado.', 'success')
            return redirect(url_for('eventos.pipeline'))

    quote_items = db.get_quote_items(event_id)
    artigos = db.get_artigos_evento(apenas_ativos=True)
    tabs = _get_tabs()
    transitions = VALID_TRANSITIONS.get(event['status'], [])
    return render_template('eventos/evento_detail.html',
                           event=event,
                           quote_items=quote_items,
                           artigos=artigos,
                           status_labels=STATUS_LABELS,
                           status_colors=STATUS_COLORS,
                           valid_transitions=transitions,
                           tabs=tabs,
                           active_tab='pipeline')


# ── Quote items ────────────────────────────────────────────────────────────────

@eventos_bp.route('/evento/<int:event_id>/quote', methods=['POST'])
@login_required
def quote_action(event_id):
    event = db.get_event(event_id)
    if not event:
        return redirect(url_for('eventos.pipeline'))

    action = request.form.get('action', '')

    if action == 'add_item':
        artigo_codigo = request.form.get('artigo_codigo', '').strip()
        descricao = request.form.get('descricao', '').strip()
        try:
            quantidade = float(request.form.get('quantidade', '1').replace(',', '.'))
        except ValueError:
            quantidade = 1.0
        try:
            preco_unitario = float(request.form.get('preco_unitario', '0').replace(',', '.'))
        except ValueError:
            preco_unitario = 0.0
        taxa_iva = _parse_taxa_iva(request.form.get('taxa_iva'))

        if not descricao and artigo_codigo:
            artigos = {a['codigo']: a['nome'] for a in db.get_artigos_evento()}
            descricao = artigos.get(artigo_codigo, artigo_codigo)

        if descricao:
            db.add_quote_item(event_id, artigo_codigo or None, descricao, quantidade, preco_unitario, taxa_iva)
            if event['status'] == 'won':
                db.recalc_event_invoice(event_id)
            flash('Artigo adicionado.', 'success')
        else:
            flash('Preencha a descrição do artigo.', 'warning')

    elif action == 'delete_item':
        item_id = int(request.form.get('item_id', 0))
        db.delete_quote_item(item_id, event_id)
        if event['status'] == 'won':
            db.recalc_event_invoice(event_id)
        flash('Artigo removido.', 'success')

    elif action == 'edit_item':
        item_id = int(request.form.get('item_id', 0))
        descricao = request.form.get('descricao', '').strip()
        try:
            quantidade = float(request.form.get('quantidade', '1').replace(',', '.'))
        except ValueError:
            quantidade = 1.0
        try:
            preco_unitario = float(request.form.get('preco_unitario', '0').replace(',', '.'))
        except ValueError:
            preco_unitario = 0.0
        taxa_iva = _parse_taxa_iva(request.form.get('taxa_iva'))
        db.update_quote_item(item_id, event_id, descricao, quantidade, preco_unitario, taxa_iva)
        if event['status'] == 'won':
            db.recalc_event_invoice(event_id)
        flash('Artigo actualizado.', 'success')

    return redirect(url_for('eventos.evento_detail', event_id=event_id))


@eventos_bp.route('/backfill-iva', methods=['GET', 'POST'])
@perm_required('acesso_financeiro')
def backfill_iva():
    """Reviewed admin action to set the real taxa_iva on past 'won' events whose
    quote_items still have taxa_iva NULL, based on real invoicing records (paper
    invoices / accounting), so historical VAT periods stop showing as estimated."""
    if request.method == 'POST':
        event_id = int(request.form.get('event_id', 0))
        raw_taxa_iva = request.form.get('taxa_iva')
        try:
            pct = float((raw_taxa_iva or '').replace(',', '.'))
            if pct < 0 or pct > 100:
                raise ValueError
            taxa_iva = round(pct / 100, 4)
        except (TypeError, ValueError):
            flash('Indique uma taxa de IVA real válida (com base na fatura/documento contabilístico) — não foi aplicada nenhuma alteração.', 'error')
            return redirect(url_for('eventos.backfill_iva'))

        event = db.get_event(event_id)
        if not event or event['status'] != 'won':
            flash('Evento não encontrado ou não adjudicado.', 'warning')
        else:
            updated = db.bulk_set_taxa_iva(event_id, taxa_iva)
            flash(f'Taxa de IVA de {round(taxa_iva * 100)}% aplicada a {updated} artigo(s) do evento "{event["event_name"]}".', 'success')
        return redirect(url_for('eventos.backfill_iva'))

    events = db.get_won_events_missing_taxa_iva()
    return render_template('eventos/backfill_iva.html', events=events)


# ── Leads ──────────────────────────────────────────────────────────────────────

@eventos_bp.route('/leads')
@login_required
def leads():
    status_filter = request.args.get('status', '')
    leads_list = db.get_leads(status=status_filter if status_filter else None)
    tabs = _get_tabs()
    return render_template('eventos/leads.html',
                           leads=leads_list,
                           status_filter=status_filter,
                           status_labels=STATUS_LABELS,
                           status_colors=STATUS_COLORS,
                           tabs=tabs,
                           active_tab='leads')


@eventos_bp.route('/leads/nova', methods=['GET', 'POST'])
@login_required
def nova_lead():
    if request.method == 'POST':
        def _int_or_none(v):
            try:
                return int(v) if v else None
            except ValueError:
                return None

        def _date_or_none(v):
            try:
                return datetime.strptime(v, '%Y-%m-%d').date() if v else None
            except ValueError:
                return None

        data = {
            'submitted_at': datetime.now(),
            'source': 'manual',
            'google_sheet_row_id': None,
            'event_type': _parse_event_type(request.form),
            'event_date': _date_or_none(request.form.get('event_date')),
            'event_time': request.form.get('event_time', '').strip(),
            'event_end_time': request.form.get('event_end_time', '').strip(),
            'estimated_guests_raw': request.form.get('estimated_guests', '').strip(),
            'estimated_guests': _int_or_none(request.form.get('estimated_guests')),
            'venue': request.form.get('venue', '').strip(),
            'venue_address': request.form.get('venue_address', '').strip(),
            'client_name': request.form.get('client_name', '').strip(),
            'client_email': request.form.get('client_email', '').strip(),
            'client_phone': request.form.get('client_phone', '').strip(),
            'marketing_consent': request.form.get('marketing_consent') == '1',
            'referral_source': request.form.get('referral_source', '').strip(),
            'notes': request.form.get('notes', '').strip(),
            'internal_notes': request.form.get('internal_notes', '').strip(),
            'status': 'lead',
        }
        lead_id = db.create_lead(data)
        flash('Lead criada com sucesso!', 'success')
        return redirect(url_for('eventos.lead_detail', lead_id=lead_id))

    tabs = _get_tabs()
    return render_template('eventos/lead_form.html',
                           lead=None,
                           status_labels=STATUS_LABELS,
                           tabs=tabs,
                           active_tab='leads')


@eventos_bp.route('/leads/<int:lead_id>', methods=['GET', 'POST'])
@login_required
def lead_detail(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        flash('Lead não encontrada.', 'error')
        return redirect(url_for('eventos.leads'))

    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action == 'save':
            def _int_or_none(v):
                try:
                    return int(v) if v else None
                except ValueError:
                    return None

            def _date_or_none(v):
                try:
                    return datetime.strptime(v, '%Y-%m-%d').date() if v else None
                except ValueError:
                    return None

            data = {
                'event_type': _parse_event_type(request.form),
                'event_date': _date_or_none(request.form.get('event_date')),
                'event_time': request.form.get('event_time', '').strip(),
                'event_end_time': request.form.get('event_end_time', '').strip(),
                'estimated_guests_raw': request.form.get('estimated_guests', '').strip(),
                'estimated_guests': _int_or_none(request.form.get('estimated_guests')),
                'venue': request.form.get('venue', '').strip(),
                'venue_address': request.form.get('venue_address', '').strip(),
                'client_name': request.form.get('client_name', '').strip(),
                'client_email': request.form.get('client_email', '').strip(),
                'client_phone': request.form.get('client_phone', '').strip(),
                'marketing_consent': request.form.get('marketing_consent') == '1',
                'referral_source': request.form.get('referral_source', '').strip(),
                'notes': request.form.get('notes', '').strip(),
                'internal_notes': request.form.get('internal_notes', '').strip(),
                'status': lead['status'],
            }
            db.update_lead(lead_id, data)
            flash('Lead guardada.', 'success')
            return redirect(url_for('eventos.lead_detail', lead_id=lead_id))

        elif action == 'transition':
            new_status = request.form.get('new_status', '')
            loss_reason = request.form.get('loss_reason', '').strip()
            current_status = lead['status']
            valid = LEAD_TRANSITIONS.get(current_status, [])
            if new_status not in valid:
                flash('Transição inválida.', 'error')
            elif new_status == 'lost' and not loss_reason:
                flash('É obrigatório indicar o motivo de perda.', 'error')
            else:
                db.update_lead(lead_id, {
                    **{k: lead[k] for k in [
                        'event_type','event_date','event_time','event_end_time',
                        'estimated_guests_raw','estimated_guests','venue','venue_address',
                        'client_name','client_email','client_phone','marketing_consent',
                        'referral_source','notes','internal_notes'
                    ]},
                    'status': new_status,
                    'loss_reason': loss_reason if new_status == 'lost' else (None if new_status == 'lead' else lead.get('loss_reason')),
                })
                flash(f'Estado alterado para «{STATUS_LABELS.get(new_status, new_status)}».', 'success')
            return redirect(url_for('eventos.lead_detail', lead_id=lead_id))

        elif action == 'convert':
            event_id = db.convert_lead_to_event(lead_id)
            if event_id:
                flash('Lead convertida em evento!', 'success')
                return redirect(url_for('eventos.evento_detail', event_id=event_id))
            else:
                flash('Erro ao converter lead.', 'error')
                return redirect(url_for('eventos.lead_detail', lead_id=lead_id))

    lead_transitions = LEAD_TRANSITIONS
    tabs = _get_tabs()
    return render_template('eventos/lead_detail.html',
                           lead=lead,
                           status_labels=STATUS_LABELS,
                           status_colors=STATUS_COLORS,
                           valid_transitions=lead_transitions.get(lead['status'], []),
                           tabs=tabs,
                           active_tab='leads')


# ── Google Sheets sync ─────────────────────────────────────────────────────────

@eventos_bp.route('/sync-sheets', methods=['POST'])
@login_required
def sync_sheets():
    try:
        from flask_app.google_sheets_sync import sync_leads_from_sheet
        inserted, updated, errors = sync_leads_from_sheet()
        if errors:
            flash(f'Sync concluído com erros: {inserted} novas, {updated} actualizadas, {errors} erros.', 'warning')
        else:
            flash(f'Sync concluído: {inserted} novas leads, {updated} actualizadas.', 'success')
    except Exception as e:
        flash(f'Erro ao sincronizar com Google Sheets: {e}', 'error')
    return redirect(url_for('eventos.leads'))


# ── Artigos de evento ──────────────────────────────────────────────────────────

@eventos_bp.route('/artigos', methods=['GET', 'POST'])
@login_required
def artigos():
    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'add':
            codigo = request.form.get('codigo', '').strip().lower().replace(' ', '_')
            nome = request.form.get('nome', '').strip()
            unidade = request.form.get('unidade', 'un').strip()
            cost_tier = request.form.get('cost_tier', 'medium').strip()
            try:
                preco_base = float(request.form.get('preco_base', '0').replace(',', '.'))
            except ValueError:
                preco_base = 0.0
            if codigo and nome:
                db.upsert_artigo_evento(codigo, nome, unidade, preco_base, cost_tier=cost_tier)
                flash(f'Artigo «{nome}» adicionado.', 'success')
            else:
                flash('Preencha código e nome.', 'warning')

        elif action == 'edit':
            artigo_id = int(request.form.get('artigo_id', 0))
            codigo = request.form.get('codigo', '').strip().lower().replace(' ', '_')
            nome = request.form.get('nome', '').strip()
            unidade = request.form.get('unidade', 'un').strip()
            cost_tier = request.form.get('cost_tier', 'medium').strip()
            try:
                preco_base = float(request.form.get('preco_base', '0').replace(',', '.'))
            except ValueError:
                preco_base = 0.0
            ativo = request.form.get('ativo') == '1'
            if codigo and nome:
                db.upsert_artigo_evento(codigo, nome, unidade, preco_base, ativo=ativo, artigo_id=artigo_id, cost_tier=cost_tier)
                flash('Artigo actualizado.', 'success')
            else:
                flash('Preencha código e nome.', 'warning')

        elif action == 'toggle':
            artigo_id = int(request.form.get('artigo_id', 0))
            ativo = request.form.get('ativo') == '1'
            db.toggle_artigo_evento(artigo_id, ativo)

        return redirect(url_for('eventos.artigos'))

    artigos_list = db.get_artigos_evento(apenas_ativos=False)
    tabs = _get_tabs()
    return render_template('eventos/artigos.html',
                           artigos=artigos_list,
                           tabs=tabs,
                           active_tab='artigos')


# ── Clientes de evento ──────────────────────────────────────────────────────────

@eventos_bp.route('/clientes', methods=['GET', 'POST'])
@login_required
def clientes():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'create':
            data = {
                'name': request.form.get('name', '').strip(),
                'email': request.form.get('email', '').strip() or None,
                'phone': request.form.get('phone', '').strip() or None,
                'marketing_consent': request.form.get('marketing_consent') == '1',
                'notes': request.form.get('notes', '').strip() or None,
            }
            if data['name']:
                db.create_event_client(data)
                flash('Cliente registado.', 'success')
            else:
                flash('O nome é obrigatório.', 'warning')
        elif action == 'edit':
            client_id = int(request.form.get('client_id', 0))
            data = {
                'name': request.form.get('name', '').strip(),
                'email': request.form.get('email', '').strip() or None,
                'phone': request.form.get('phone', '').strip() or None,
                'marketing_consent': request.form.get('marketing_consent') == '1',
                'notes': request.form.get('notes', '').strip() or None,
            }
            if data['name'] and client_id:
                db.update_event_client(client_id, data)
                flash('Cliente actualizado.', 'success')
            else:
                flash('O nome é obrigatório.', 'warning')
        return redirect(url_for('eventos.clientes'))

    search_q = request.args.get('q', '').strip()
    marketing_only = request.args.get('marketing_only', '0') == '1'
    clients = db.get_event_clients(marketing_only=marketing_only, search=search_q or None)
    tabs = _get_tabs()
    return render_template('eventos/clientes.html',
                           clients=clients,
                           search_q=search_q,
                           marketing_only=marketing_only,
                           tabs=tabs,
                           active_tab='clientes')


@eventos_bp.route('/clientes/search')
@login_required
def clientes_search():
    q = request.args.get('q', '').strip()
    results = db.search_event_clients(q) if q else []
    return jsonify([dict(r) for r in results])


@eventos_bp.route('/clientes/<int:client_id>', methods=['GET', 'POST'])
@login_required
def cliente_detail(client_id):
    client = db.get_event_client(client_id)
    if not client:
        flash('Cliente não encontrado.', 'error')
        return redirect(url_for('eventos.clientes'))
    if request.method == 'POST':
        data = {
            'name': request.form.get('name', '').strip(),
            'email': request.form.get('email', '').strip() or None,
            'phone': request.form.get('phone', '').strip() or None,
            'marketing_consent': request.form.get('marketing_consent') == '1',
            'notes': request.form.get('notes', '').strip() or None,
        }
        if data['name']:
            db.update_event_client(client_id, data)
            flash('Cliente actualizado.', 'success')
        else:
            flash('O nome é obrigatório.', 'warning')
        return redirect(url_for('eventos.cliente_detail', client_id=client_id))
    client_events = db.get_client_events(client_id)
    tabs = _get_tabs()
    return render_template('eventos/cliente_detail.html',
                           client=client,
                           client_events=client_events,
                           tabs=tabs,
                           active_tab='clientes')


# ── Recebimentos de eventos (Fase 2) ───────────────────────────────────────────

@eventos_bp.route('/recebimentos/novo', methods=['GET', 'POST'])
@login_required
def novo_recebimento_evento():
    if request.method == 'POST':
        cliente = request.form.get('cliente', '').strip()
        if not cliente:
            flash('Nome do cliente é obrigatório.', 'error')
            return redirect(url_for('eventos.novo_recebimento_evento'))
        event_date_str = request.form.get('event_date', '')
        try:
            event_date = date.fromisoformat(event_date_str)
        except ValueError:
            flash('Data do evento inválida.', 'error')
            return redirect(url_for('eventos.novo_recebimento_evento'))
        invoice_amount = _parse_decimal(request.form.get('invoice_amount_eur', ''))
        expected_payment_str = request.form.get('expected_payment_date', '').strip()
        expected_payment = None
        if expected_payment_str:
            try:
                expected_payment = date.fromisoformat(expected_payment_str)
            except ValueError:
                pass
        data = {
            'cliente': cliente,
            'descricao': request.form.get('descricao', '').strip(),
            'event_date': event_date,
            'local': request.form.get('local', '').strip(),
            'status': request.form.get('status', 'proposta'),
            'invoice_amount_eur': invoice_amount,
            'expected_payment_date': expected_payment,
            'notas': request.form.get('notas', '').strip(),
        }
        evento_id = create_evento(data)
        items = _parse_items_from_form(request.form)
        if items:
            upsert_evento_items(evento_id, items)
        flash(f'Evento "{cliente}" criado com sucesso!', 'success')
        return redirect(url_for('eventos.detalhe_evento', evento_id=evento_id))
    tabs = _get_tabs()
    return render_template('eventos/form_evento.html',
                           active_tab='recebimentos', tabs=tabs,
                           evento=None, items=[],
                           STATUS_LABELS=EVENTO_STATUS_LABELS,
                           today=str(date.today()),
                           title='Novo Evento de Produção')


@eventos_bp.route('/recebimentos/evento/<int:evento_id>')
@login_required
def detalhe_evento(evento_id):
    evento, items = get_evento_by_id(evento_id)
    if not evento:
        flash('Evento não encontrado.', 'error')
        return redirect(url_for('eventos.recebimentos'))
    evento['status_label'], evento['status_badge'] = EVENTO_STATUS_LABELS.get(evento['status'], (evento['status'], 'secondary'))
    ps = _compute_payment_status(evento)
    evento['payment_status_computed'] = ps
    evento['payment_status_label'], evento['payment_status_badge'] = PAYMENT_STATUS_LABELS.get(ps, (ps, 'secondary'))
    production_items = [i for i in items if i.get('is_production_item')]
    other_items = [i for i in items if not i.get('is_production_item')]
    tabs = _get_tabs()
    return render_template('eventos/detalhe_evento.html',
                           active_tab='recebimentos', tabs=tabs,
                           evento=evento, items=items,
                           production_items=production_items,
                           other_items=other_items,
                           STATUS_LABELS=EVENTO_STATUS_LABELS,
                           PAYMENT_STATUS_LABELS=PAYMENT_STATUS_LABELS,
                           today=str(date.today()))


@eventos_bp.route('/recebimentos/evento/<int:evento_id>/editar', methods=['GET', 'POST'])
@login_required
def editar_evento(evento_id):
    evento, items = get_evento_by_id(evento_id)
    if not evento:
        flash('Evento não encontrado.', 'error')
        return redirect(url_for('eventos.recebimentos'))
    if request.method == 'POST':
        cliente = request.form.get('cliente', '').strip()
        if not cliente:
            flash('Nome do cliente é obrigatório.', 'error')
            return redirect(url_for('eventos.editar_evento', evento_id=evento_id))
        event_date_str = request.form.get('event_date', '')
        try:
            event_date = date.fromisoformat(event_date_str)
        except ValueError:
            flash('Data do evento inválida.', 'error')
            return redirect(url_for('eventos.editar_evento', evento_id=evento_id))
        invoice_amount = _parse_decimal(request.form.get('invoice_amount_eur', ''))
        expected_payment_str = request.form.get('expected_payment_date', '').strip()
        expected_payment = None
        if expected_payment_str:
            try:
                expected_payment = date.fromisoformat(expected_payment_str)
            except ValueError:
                pass
        data = {
            'cliente': cliente,
            'descricao': request.form.get('descricao', '').strip(),
            'event_date': event_date,
            'local': request.form.get('local', '').strip(),
            'status': request.form.get('status', 'proposta'),
            'invoice_amount_eur': invoice_amount,
            'expected_payment_date': expected_payment,
            'notas': request.form.get('notas', '').strip(),
        }
        update_evento(evento_id, data)
        new_items = _parse_items_from_form(request.form)
        upsert_evento_items(evento_id, new_items)
        flash('Evento atualizado!', 'success')
        return redirect(url_for('eventos.detalhe_evento', evento_id=evento_id))
    tabs = _get_tabs()
    return render_template('eventos/form_evento.html',
                           active_tab='recebimentos', tabs=tabs,
                           evento=evento, items=items,
                           STATUS_LABELS=EVENTO_STATUS_LABELS,
                           today=str(date.today()),
                           title='Editar Evento de Produção')


@eventos_bp.route('/recebimentos/evento/<int:evento_id>/eliminar', methods=['POST'])
@login_required
def eliminar_evento(evento_id):
    delete_evento(evento_id)
    flash('Evento eliminado.', 'success')
    return redirect(url_for('eventos.recebimentos'))


@eventos_bp.route('/recebimentos/evento/<int:evento_id>/pagamento', methods=['GET', 'POST'])
@login_required
def registar_pagamento(evento_id):
    evento, items = get_evento_by_id(evento_id)
    if not evento:
        flash('Evento não encontrado.', 'error')
        return redirect(url_for('eventos.recebimentos'))
    if request.method == 'POST':
        payment_amount = _parse_decimal(request.form.get('payment_amount_eur', ''))
        payment_date_str = request.form.get('payment_date', '').strip()
        payment_status = request.form.get('payment_status', 'received')
        payment_date_val = None
        if payment_date_str:
            try:
                payment_date_val = date.fromisoformat(payment_date_str)
            except ValueError:
                pass
        registar_pagamento_evento(evento_id, payment_amount, payment_date_val, payment_status)
        flash('Pagamento registado!', 'success')
        return redirect(url_for('eventos.recebimentos'))
    ps = _compute_payment_status(evento)
    evento['payment_status_computed'] = ps
    evento['payment_status_label'], evento['payment_status_badge'] = PAYMENT_STATUS_LABELS.get(ps, (ps, 'secondary'))
    tabs = _get_tabs()
    return render_template('eventos/pagamento.html',
                           active_tab='recebimentos', tabs=tabs,
                           evento=evento,
                           PAYMENT_STATUS_LABELS=PAYMENT_STATUS_LABELS,
                           today=str(date.today()))


@eventos_bp.route('/recebimentos')
@login_required
def recebimentos():
    payment_filter = request.args.get('payment_status', '')
    eventos = get_eventos_recebimentos()
    today = date.today()
    resultado = []
    for e in eventos:
        ps = _compute_payment_status(e)
        if payment_filter and ps != payment_filter:
            continue
        e['payment_status_computed'] = ps
        e['payment_status_label'], e['payment_status_badge'] = PAYMENT_STATUS_LABELS.get(ps, (ps, 'secondary'))
        resultado.append(e)
    total_esperado = sum(float(e['invoice_amount_eur'] or 0) for e in resultado)
    total_recebido = sum(float(e['payment_amount_eur'] or 0) for e in resultado if e['payment_status_computed'] == 'received')
    total_pendente = sum(float(e['invoice_amount_eur'] or 0) for e in resultado if e['payment_status_computed'] in ('pending', 'partial', 'overdue'))
    tabs = _get_tabs()
    return render_template('eventos/recebimentos.html',
                           active_tab='recebimentos', tabs=tabs,
                           eventos=resultado,
                           payment_filter=payment_filter,
                           PAYMENT_STATUS_LABELS=PAYMENT_STATUS_LABELS,
                           total_esperado=total_esperado,
                           total_recebido=total_recebido,
                           total_pendente=total_pendente,
                           today=str(today))


def _parse_items_from_form(form):
    items = []
    idx = 0
    while True:
        descricao = form.get(f'item_descricao_{idx}', '').strip()
        if not descricao and form.get(f'item_descricao_{idx}') is None:
            break
        if descricao:
            qty_raw = form.get(f'item_quantidade_{idx}', '1')
            try:
                qty = float(str(qty_raw).replace(',', '.'))
            except (ValueError, AttributeError):
                qty = 1.0
            items.append({
                'descricao': descricao,
                'quantidade': qty,
                'unidade': form.get(f'item_unidade_{idx}', 'un').strip() or 'un',
                'is_production_item': form.get(f'item_prod_{idx}') == 'on',
            })
        idx += 1
        if idx > 50:
            break
    return items
