import os
import sys
import hashlib
import secrets
import time
from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from flask_app.auth import login_required, perm_required
from flask_app.services.event_portal import (
    cleanup_expired_portal_proofs, resolve_event_address, save_private_portal_proof,
    validate_portal_proof,
)
from flask_app.services.event_portal_brand import (
    default_portal_brand, save_public_portal_logo, validate_brand_form,
    validate_portal_logo,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import database as db
from database import (
    get_all_eventos, get_evento_by_id, create_evento, update_evento, delete_evento,
    upsert_evento_items, registar_pagamento_evento, get_eventos_recebimentos,
)
from db.pagamentos import VAT_RATES
from db.stores import get_all_stores, get_store

eventos_bp = Blueprint('eventos', __name__)


def _parse_taxa_iva(raw, artigo_codigo=None):
    """Parse a per-line IVA rate submitted as a percentage (e.g. '13'). Defaults to
    the editable event pricing configuration when missing or invalid."""
    try:
        pct = float((raw or '').replace(',', '.'))
        if pct < 0 or pct > 100:
            raise ValueError
        return round(pct / 100, 4)
    except (TypeError, ValueError):
        return db.get_default_quote_taxa_iva(artigo_codigo)

STATUS_LABELS = {
    'novos':         'Novos',
    'orcamentado':   'Orçamentado',
    'enviado':       'Enviado',
    'adjudicado':    'Adjudicado',
    'rejeitado':     'Rejeitado',
    'sinalizado':    'Sinalizado',
    'realizado':     'Realizado',
    'faturado':      'Faturado',
    'recebido':      'Recebido',
    'cancelado':     'Cancelado',
}

STATUS_COLORS = {
    'novos':         'secondary',
    'orcamentado':   'info',
    'enviado':       'primary',
    'adjudicado':    'success',
    'rejeitado':     'danger',
    'sinalizado':    'success',
    'realizado':     'primary',
    'faturado':      'info',
    'recebido':      'success',
    'cancelado':     'dark',
}

VALID_TRANSITIONS = {
    'novos':         ['orcamentado', 'rejeitado', 'cancelado'],
    'orcamentado':   ['enviado', 'rejeitado', 'cancelado'],
    'enviado':       ['orcamentado', 'adjudicado', 'rejeitado', 'cancelado'],
    'adjudicado':    ['sinalizado', 'cancelado'],
    'sinalizado':    ['realizado', 'cancelado'],
    'realizado':     ['faturado', 'cancelado'],
    'faturado':      ['recebido', 'cancelado'],
    'rejeitado':     [],
    'recebido':      [],
    'cancelado':     [],
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


def _current_actor():
    user = session.get('user') or {}
    if isinstance(user, dict):
        return user.get('email') or user.get('username') or user.get('name')
    return str(user) if user else None


def _portal_csrf_token():
    token = session.get('event_portal_csrf')
    if not token:
        token = secrets.token_urlsafe(24)
        session['event_portal_csrf'] = token
    return token


def _require_portal_csrf():
    supplied = request.form.get('csrf_token', '')
    expected = session.get('event_portal_csrf', '')
    if not expected or not secrets.compare_digest(supplied, expected):
        raise ValueError('A página expirou. Atualize e tente novamente.')


def _admin_portal_brand_csrf_token():
    token = session.get('event_portal_brand_csrf')
    if not token:
        token = secrets.token_urlsafe(24)
        session['event_portal_brand_csrf'] = token
    return token


def _require_admin_portal_brand_csrf():
    supplied = request.form.get('csrf_token', '')
    expected = session.get('event_portal_brand_csrf', '')
    if not expected or not secrets.compare_digest(supplied, expected):
        raise ValueError('A página expirou. Atualize e tente novamente.')


def _portal_email():
    expires_at = session.get('event_portal_access_until', 0)
    email = session.get('event_portal_email')
    verified = session.get('event_portal_verified') is True
    if not email or not verified or not isinstance(expires_at, (float, int)) or expires_at < time.time():
        session.pop('event_portal_email', None)
        session.pop('event_portal_access_until', None)
        session.pop('event_portal_verified', None)
        session.pop('event_portal_event_id', None)
        return None
    return email


def _portal_event_id():
    event_id = session.get('event_portal_event_id')
    return event_id if isinstance(event_id, int) and event_id > 0 else None


def _portal_ip_fingerprint():
    remote = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    return hashlib.sha256(remote.encode('utf-8')).hexdigest() if remote else None


def _portal_end_time(start, duration_minutes):
    if not start:
        return None
    try:
        start_at = datetime.strptime(start, '%H:%M')
        return (start_at + timedelta(minutes=int(duration_minutes))).strftime('%H:%M')
    except (ValueError, TypeError):
        return None


def _portal_login_required():
    email = _portal_email()
    if email:
        return email
    flash('Indique o email e o código de consulta para ver os seus pedidos.', 'warning')
    return None


# ── Public customer event portal ───────────────────────────────────────────────

@eventos_bp.route('/pedido-evento', methods=['GET', 'POST'])
def portal_request():
    brand = db.get_default_portal_brand()
    if request.method == 'POST':
        try:
            _require_portal_csrf()
            dates = request.form.getlist('occurrence_date[]')
            start_times = request.form.getlist('occurrence_start[]')
            venues = request.form.getlist('occurrence_venue[]')
            addresses = request.form.getlist('occurrence_address[]')
            occurrences = []
            for index, raw_date in enumerate(dates):
                if not raw_date:
                    continue
                try:
                    event_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
                except ValueError:
                    raise ValueError('Uma das datas do evento não é válida.')
                if event_date < date.today():
                    raise ValueError('Escolha uma data futura.')
                venue = (venues[index] if index < len(venues) else '').strip()
                address = (addresses[index] if index < len(addresses) else '').strip()
                if not address:
                    raise ValueError('Indique a morada de cada ocorrência.')
                location = resolve_event_address(address)
                duration = request.form.get('duration_minutes', '180')
                occurrences.append({
                    'event_date': event_date,
                    'service_start_time': start_times[index] if index < len(start_times) else None,
                    'service_end_time': _portal_end_time(
                        start_times[index] if index < len(start_times) else None, duration,
                    ),
                    'expected_duration_minutes': int(duration),
                    'venue': venue or 'Local indicado pelo cliente',
                    'venue_address': address,
                    'latitude': location.get('latitude'),
                    'longitude': location.get('longitude'),
                    'estimated_km': location.get('round_trip_km'),
                    'logistics_notes': (
                        'Geocoding pendente de revisão manual.'
                        if location.get('manual_review') else None
                    ),
                    'service_mode': (
                        'catering' if request.form.get('service_mode') == 'catering'
                        else 'pending'
                    ),
                })
            result = db.create_portal_event_request({
                'event_name': request.form.get('event_name', '').strip(),
                'event_type': request.form.get('event_type', '').strip(),
                'estimated_guests': request.form.get('estimated_guests'),
                'servings_per_guest': request.form.get('servings_per_guest'),
                'flavours': request.form.getlist('flavours[]'),
                'occurrences': occurrences,
                'client_name': request.form.get('client_name', '').strip(),
                'client_email': request.form.get('client_email', '').strip(),
                'client_phone': request.form.get('client_phone', '').strip(),
                'marketing_consent': request.form.get('marketing_consent') == '1',
                'privacy_accepted': request.form.get('privacy_accepted') == '1',
                'referral_source': request.form.get('referral_source', '').strip(),
                'resource_preferences': request.form.getlist('resource_preferences[]'),
                'catering_requested': request.form.get('service_mode') == 'catering',
                'brand_store_id': brand.get('store_id'),
                'confirmation_message': brand.get('confirmation_message'),
            })
            email = db.normalize_portal_email(request.form.get('client_email'))
            access_code = result.get('access_code')
            if access_code:
                session['event_portal_email'] = email
                session['event_portal_verified'] = True
                session['event_portal_access_until'] = time.time() + 30 * 60
                session['event_portal_event_id'] = result['event_id']
                session['event_portal_access_code_once'] = access_code
            db.record_portal_access(email, 'request_submitted', result['event_id'], _portal_ip_fingerprint())
            preferences = request.form.getlist('resource_preferences[]')
            resource_ids = {
                resource['code']: resource['id']
                for resource in db.get_event_resources()
            }
            has_resource_risk = any(
                db.get_resource_conflicts(
                    resource_ids[preference], occurrence['event_date'],
                    occurrence['service_start_time'], occurrence['service_end_time'],
                )
                for preference in preferences if preference in resource_ids
                for occurrence in occurrences
            )
            if has_resource_risk:
                flash(
                    'A preferência de carrinho/arca tem disponibilidade limitada nessa data. '
                    'A equipa pode propor serviço pelo cliente, catering ou outra alternativa.',
                    'warning',
                )
            if access_code:
                flash('Recebemos o seu pedido. Guarde o código de consulta mostrado abaixo.', 'success')
                return redirect(url_for('eventos.portal_event', event_id=result['event_id']))
            flash('Recebemos o seu pedido. Guarde o código de consulta mostrado abaixo.', 'success')
            return redirect(url_for('eventos.portal_event', event_id=result['event_id']))
        except ValueError as exc:
            flash(str(exc), 'error')

    calendar_dates = [
        {'date': row['event_date'].isoformat(), 'risk': row['risk']}
        for row in db.get_portal_unavailable_dates()
    ]
    return render_template(
        'eventos/portal_request.html', calendar_dates=calendar_dates,
        csrf_token=_portal_csrf_token(), form=request.form, brand=brand,
    )


@eventos_bp.route('/portal-eventos', methods=['GET', 'POST'])
def portal_access():
    if request.method == 'POST':
        try:
            _require_portal_csrf()
            email = db.normalize_portal_email(request.form.get('email'))
            event_id = db.verify_portal_request_access(email, request.form.get('access_code'))
            if not event_id:
                raise ValueError('O email ou o código de consulta não estão corretos.')
            session['event_portal_email'] = email
            session['event_portal_verified'] = True
            session['event_portal_access_until'] = time.time() + 30 * 60
            session['event_portal_event_id'] = event_id
            db.record_portal_access(
                email, 'email_access_started', event_id, _portal_ip_fingerprint()
            )
            return redirect(url_for('eventos.portal_events'))
        except ValueError as exc:
            flash(str(exc), 'error')
    return render_template(
        'eventos/portal_access.html', csrf_token=_portal_csrf_token(),
        brand=db.get_default_portal_brand(),
    )


@eventos_bp.route('/portal-eventos/sair', methods=['POST'])
def portal_logout():
    try:
        _require_portal_csrf()
    except ValueError:
        return redirect(url_for('eventos.portal_access'))
    email = _portal_email()
    if email:
        db.record_portal_access(email, 'email_access_ended', ip_fingerprint=_portal_ip_fingerprint())
    session.pop('event_portal_email', None)
    session.pop('event_portal_access_until', None)
    session.pop('event_portal_verified', None)
    session.pop('event_portal_event_id', None)
    flash('A consulta dos seus pedidos terminou.', 'success')
    return redirect(url_for('eventos.portal_access'))


@eventos_bp.route('/portal-eventos/pedidos')
def portal_events():
    email = _portal_login_required()
    if not email:
        return redirect(url_for('eventos.portal_access'))
    event_id = _portal_event_id()
    event = db.get_portal_event_for_email(event_id, email) if event_id else None
    events = [event] if event else []
    brand = (
        db.get_portal_brand_config(event.get('brand_store_id'))
        if event and event.get('brand_store_id')
        else db.get_default_portal_brand()
    )
    db.record_portal_access(email, 'event_list_viewed', ip_fingerprint=_portal_ip_fingerprint())
    return render_template(
        'eventos/portal_events.html', events=events, email=email,
        csrf_token=_portal_csrf_token(), status_labels=STATUS_LABELS,
        status_colors=STATUS_COLORS, brand=brand,
    )


@eventos_bp.route('/portal-eventos/pedido/<int:event_id>')
def portal_event(event_id):
    email = _portal_login_required()
    if not email:
        return redirect(url_for('eventos.portal_access'))
    if event_id != _portal_event_id():
        flash('Este código só permite consultar o pedido associado.', 'error')
        return redirect(url_for('eventos.portal_events'))
    event = db.get_portal_event_for_email(event_id, email)
    if not event:
        flash('Não foi possível consultar este pedido com o email atual.', 'error')
        return redirect(url_for('eventos.portal_events'))
    db.record_portal_access(email, 'event_viewed', event_id, _portal_ip_fingerprint())
    return render_template(
        'eventos/portal_event.html', event=event, csrf_token=_portal_csrf_token(),
        status_labels=STATUS_LABELS, status_colors=STATUS_COLORS,
        access_code_once=session.pop('event_portal_access_code_once', None),
        brand=event.get('portal_brand') or db.get_default_portal_brand(),
    )


@eventos_bp.route('/portal-eventos/pedido/<int:event_id>/aceitar', methods=['POST'])
def portal_accept_quote(event_id):
    email = _portal_login_required()
    if not email:
        return redirect(url_for('eventos.portal_access'))
    if event_id != _portal_event_id():
        flash('Este código não permite alterar esse pedido.', 'error')
        return redirect(url_for('eventos.portal_events'))
    try:
        _require_portal_csrf()
        accepted = db.accept_portal_quote(event_id, email, request.form.get('quote_revision', ''))
        db.record_portal_access(email, 'quote_accepted' if accepted else 'quote_accept_replayed', event_id, _portal_ip_fingerprint())
        flash('Orçamento aceite. Aguarde as instruções para o sinal.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    return redirect(url_for('eventos.portal_event', event_id=event_id))


@eventos_bp.route('/portal-eventos/pedido/<int:event_id>/comprovativo', methods=['POST'])
def portal_upload_proof(event_id):
    email = _portal_login_required()
    if not email:
        return redirect(url_for('eventos.portal_access'))
    try:
        _require_portal_csrf()
        if event_id != _portal_event_id():
            raise ValueError('Este código não permite alterar esse pedido.')
        db.assert_portal_event_access(event_id, email)
        validated = validate_portal_proof(request.files.get('proof_file'))
        upload_root = os.path.join(os.path.dirname(__file__), '..', '..', 'private_uploads', 'event_proofs')
        cleanup_expired_portal_proofs(upload_root)
        metadata = save_private_portal_proof(validated, upload_root)
        try:
            db.create_portal_file(event_id, email, metadata)
        except Exception:
            try:
                os.unlink(os.path.join(upload_root, metadata['storage_name']))
            except FileNotFoundError:
                pass
            raise
        db.record_portal_access(email, 'deposit_proof_uploaded', event_id, _portal_ip_fingerprint())
        flash('Comprovativo recebido. A equipa irá validá-lo.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    except Exception:
        flash('Não foi possível guardar o comprovativo. Tente novamente.', 'error')
    return redirect(url_for('eventos.portal_event', event_id=event_id))

TABS = [
    {'id': 'dashboard', 'label': 'Dashboard', 'icon': '📊', 'url_endpoint': 'eventos.dashboard'},
    {'id': 'pipeline',  'label': 'Pipeline',  'icon': '📋', 'url_endpoint': 'eventos.pipeline'},
    {'id': 'calendario', 'label': 'Calendário', 'icon': '🗓️', 'url_endpoint': 'eventos.calendario'},
    {'id': 'leads',     'label': 'Leads do Formulário', 'icon': '📥', 'url_endpoint': 'eventos.leads'},
    {'id': 'clientes',  'label': 'Clientes',  'icon': '👥', 'url_endpoint': 'eventos.clientes'},
    {'id': 'artigos',   'label': 'Artigos',   'icon': '🏷️', 'url_endpoint': 'eventos.artigos'},
    {'id': 'configuracao', 'label': 'Configuração', 'icon': '⚙️', 'url_endpoint': 'eventos.configuracao'},
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
@perm_required('acesso_eventos')
def index():
    from db.tiles import get_module_labels
    custom_mod = get_module_labels().get('eventos')
    menu_title = f'🎪 {custom_mod}' if custom_mod else '🎪 Eventos'
    tabs = _get_tabs()
    tiles = [{'icon': t['icon'], 'label': t['label'], 'url': t['url']} for t in tabs]
    return render_template('eventos/index.html', tiles=tiles, menu_title=menu_title)


# ── Dashboard ──────────────────────────────────────────────────────────────────

@eventos_bp.route('/dashboard')
@perm_required('acesso_eventos')
def dashboard():
    stats = db.get_pipeline_dashboard()
    notifications = db.get_event_notifications()
    tabs = _get_tabs()
    all_statuses = db.EVENT_STATUSES
    return render_template('eventos/dashboard.html',
                           stats=stats,
                           all_statuses=all_statuses,
                           notifications=notifications,
                           status_labels=STATUS_LABELS,
                           status_colors=STATUS_COLORS,
                           tabs=tabs,
                           active_tab='dashboard')


# ── Pipeline ───────────────────────────────────────────────────────────────────

@eventos_bp.route('/pipeline')
@perm_required('acesso_eventos')
def pipeline():
    status_filter = request.args.get('status', '')
    filters = {
        'search': request.args.get('q', '').strip(),
        'client': request.args.get('client', '').strip(),
        'event_type': request.args.get('event_type', '').strip(),
        'date_from': request.args.get('date_from', '').strip() or None,
        'date_to': request.args.get('date_to', '').strip() or None,
        'resource_id': request.args.get('resource_id', type=int),
    }
    events = db.get_events(status=status_filter or None, **filters)
    all_statuses = db.EVENT_STATUSES
    tabs = _get_tabs()
    return render_template('eventos/pipeline.html',
                           events=events,
                           status_filter=status_filter,
                            filters=filters,
                            resources=db.get_event_resources(),
                           all_statuses=all_statuses,
                           status_labels=STATUS_LABELS,
                           status_colors=STATUS_COLORS,
                           valid_transitions=VALID_TRANSITIONS,
                           tabs=tabs,
                           active_tab='pipeline')


@eventos_bp.route('/calendario')
@perm_required('acesso_eventos')
def calendario():
    start_raw = request.args.get('inicio', '')
    try:
        start = datetime.strptime(start_raw, '%Y-%m-%d').date() if start_raw else date.today()
    except ValueError:
        start = date.today()
    end = start + timedelta(days=27)
    occurrences = db.get_event_calendar_occurrences(start, end)
    return render_template(
        'eventos/calendario.html', occurrences=occurrences, start=start, end=end,
        status_labels=STATUS_LABELS, status_colors=STATUS_COLORS,
        tabs=_get_tabs(), active_tab='calendario',
    )


# ── Event detail / edit ────────────────────────────────────────────────────────

@eventos_bp.route('/evento/novo', methods=['GET', 'POST'])
@perm_required('acesso_eventos')
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
            'status': 'novos',
            'internal_notes': request.form.get('internal_notes', '').strip(),
        }
        event_id = db.create_event(data, actor=_current_actor())
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
@perm_required('acesso_eventos')
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
            try:
                db.update_event(
                    event_id, data, actor=_current_actor(),
                    risk_acknowledged=request.form.get('acknowledge_time_conflict') == '1',
                )
            except ValueError as exc:
                flash(str(exc), 'error')
                return redirect(url_for('eventos.evento_detail', event_id=event_id))
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
            ok, msg = db.transition_event_status(
                event_id, new_status, loss_reason, actor=_current_actor()
            )
            if ok:
                flash(f'Estado alterado para «{STATUS_LABELS.get(new_status, new_status)}».', 'success')
            else:
                flash(f'Erro: {msg}', 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'validate_deposit':
            raw_amount = request.form.get('deposit_amount', '').replace(',', '.')
            proof_reference = request.form.get('deposit_proof_reference', '').strip()
            received_at = request.form.get('deposit_received_at') or None
            try:
                db.validate_event_deposit(
                    event_id, raw_amount, proof_reference=proof_reference,
                    actor=_current_actor(), received_at=received_at,
                )
                flash('Sinal validado; as ocorrências e recursos foram reservados.', 'success')
            except ValueError as exc:
                flash(str(exc), 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'reject_deposit':
            try:
                db.reject_event_deposit(
                    event_id, request.form.get('deposit_rejection_reason'),
                    actor=_current_actor(),
                )
                flash('Comprovativo rejeitado. O evento continua adjudicado até receber um sinal válido.', 'warning')
            except ValueError as exc:
                flash(str(exc), 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'mark_invoiced':
            try:
                db.mark_event_invoiced(
                    event_id, request.form.get('invoice_reference'), actor=_current_actor()
                )
                flash('Fatura associada e evento marcado como faturado.', 'success')
            except ValueError as exc:
                flash(str(exc), 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'record_receipt':
            try:
                complete = db.record_event_receipt(
                    event_id, request.form.get('receipt_amount'),
                    request.form.get('receipt_date') or date.today(),
                    payment_method=request.form.get('payment_method'),
                    payment_reference=request.form.get('payment_reference'),
                    actor=_current_actor(),
                )
                flash(
                    'Recebimento confirmado e evento encerrado.'
                    if complete else 'Recebimento parcial registado; mantém-se o saldo em aberto.',
                    'success',
                )
            except ValueError as exc:
                flash(str(exc), 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'add_occurrence':
            try:
                occurrence_date = datetime.strptime(
                    request.form.get('occurrence_date', ''), '%Y-%m-%d'
                ).date()
                occurrence_id = db.add_event_occurrence(event_id, {
                    'event_date': occurrence_date,
                    'venue': request.form.get('occurrence_venue', '').strip() or None,
                    'venue_address': request.form.get('occurrence_address', '').strip() or None,
                    'service_start_time': request.form.get('occurrence_start') or None,
                    'service_end_time': request.form.get('occurrence_end') or None,
                    'service_mode': request.form.get('service_mode') or 'pending',
                }, actor=_current_actor())
                flash(f'Ocorrência #{occurrence_id} adicionada.', 'success')
            except (TypeError, ValueError):
                flash('Preencha uma data válida para a ocorrência.', 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'update_occurrence':
            try:
                occurrence_id = int(request.form.get('occurrence_id', ''))
                occurrence_date = datetime.strptime(request.form.get('occurrence_date', ''), '%Y-%m-%d').date()
                db.update_event_occurrence(occurrence_id, {
                    'event_date': occurrence_date,
                    'venue': request.form.get('occurrence_venue', '').strip() or None,
                    'venue_address': request.form.get('occurrence_address', '').strip() or None,
                    'service_start_time': request.form.get('occurrence_start') or None,
                    'service_end_time': request.form.get('occurrence_end') or None,
                    'service_mode': request.form.get('service_mode') or 'pending',
                    'logistics_notes': request.form.get('logistics_notes', '').strip() or None,
                }, actor=_current_actor(),
                   risk_acknowledged=request.form.get('acknowledge_occurrence_conflict') == '1',
                   expected_event_id=event_id)
                flash('Ocorrência atualizada.', 'success')
            except (TypeError, ValueError) as exc:
                flash(str(exc) or 'Não foi possível atualizar a ocorrência.', 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'delete_occurrence':
            try:
                db.delete_event_occurrence(
                    int(request.form.get('occurrence_id', '')), actor=_current_actor(),
                    expected_event_id=event_id,
                )
                flash('Ocorrência removida.', 'success')
            except (TypeError, ValueError) as exc:
                flash(str(exc), 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'reserve_resource':
            try:
                occurrence_id = int(request.form.get('occurrence_id', ''))
                resource_id = int(request.form.get('resource_id', ''))
                acknowledged = request.form.get('acknowledge_conflict') == '1'
                saved, conflicts = db.reserve_event_resource(
                    occurrence_id, resource_id, actor=_current_actor(),
                    risk_acknowledged=acknowledged,
                    notes=request.form.get('conflict_reason', '').strip() or None,
                    expected_event_id=event_id,
                )
                if saved:
                    flash('Recurso registado para esta ocorrência.', 'success')
                else:
                    session['event_resource_conflicts'] = [
                        {
                            'event_name': conflict.get('event_name') or f"Evento #{conflict.get('event_id')}",
                            'event_date': str(conflict.get('event_date') or ''),
                            'service_start_time': str(conflict.get('service_start_time') or ''),
                        }
                        for conflict in conflicts
                    ]
                    flash('Existe um conflito. Reveja-o e confirme explicitamente o risco para avançar.', 'warning')
            except (TypeError, ValueError):
                flash('Selecione uma ocorrência e um recurso válidos.', 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'release_resource':
            try:
                db.release_event_resource(
                    int(request.form.get('occurrence_id', '')),
                    int(request.form.get('resource_id', '')),
                    actor=_current_actor(),
                    expected_event_id=event_id,
                )
                flash('Recurso libertado.', 'success')
            except (TypeError, ValueError) as exc:
                flash(str(exc), 'error')
            return redirect(url_for('eventos.evento_detail', event_id=event_id))

        elif action == 'delete':
            db.delete_event(event_id, actor=_current_actor())
            flash('Evento arquivado. O histórico foi preservado.', 'success')
            return redirect(url_for('eventos.pipeline'))

    quote_items = db.get_quote_items(event_id)
    quote_totals = db.get_event_quote_totals(event_id)
    artigos = db.get_artigos_evento(apenas_ativos=True)
    occurrences = db.get_event_occurrences(event_id)
    resources = db.get_event_resources()
    event_history = db.get_event_history(event_id)
    quote_versions = db.get_quote_versions(event_id)
    resource_conflicts = session.pop('event_resource_conflicts', [])
    tabs = _get_tabs()
    transitions = VALID_TRANSITIONS.get(event['status'], [])
    return render_template('eventos/evento_detail.html',
                           event=event,
                           quote_items=quote_items,
                            quote_totals=quote_totals,
                           artigos=artigos,
                           status_labels=STATUS_LABELS,
                           status_colors=STATUS_COLORS,
                           valid_transitions=transitions,
                            occurrences=occurrences,
                            resources=resources,
                            event_history=event_history,
                             quote_versions=quote_versions,
                            resource_conflicts=resource_conflicts,
                            today=date.today().isoformat(),
                           tabs=tabs,
                           active_tab='pipeline')


# ── Quote items ────────────────────────────────────────────────────────────────

@eventos_bp.route('/evento/<int:event_id>/quote', methods=['POST'])
@perm_required('acesso_eventos')
def quote_action(event_id):
    event = db.get_event(event_id)
    if not event:
        return redirect(url_for('eventos.pipeline'))

    action = request.form.get('action', '')
    if (
        action in ('add_item', 'edit_item', 'delete_item', 'add_adjustment')
        and db.normalize_event_status(event['status'])
        in ('enviado', 'adjudicado', 'sinalizado', 'realizado', 'faturado', 'recebido')
    ):
        flash('O orçamento enviado/aceite está fechado. Crie uma nova proposta antes de voltar a enviá-la.', 'warning')
        return redirect(url_for('eventos.evento_detail', event_id=event_id))

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
        taxa_iva = _parse_taxa_iva(request.form.get('taxa_iva'), artigo_codigo)

        if not descricao and artigo_codigo:
            artigos = {a['codigo']: a['nome'] for a in db.get_artigos_evento()}
            descricao = artigos.get(artigo_codigo, artigo_codigo)

        if descricao:
            db.add_quote_item(
                event_id, artigo_codigo or None, descricao, quantidade,
                preco_unitario, taxa_iva, actor=_current_actor()
            )
            if db.normalize_event_status(event['status']) in db.EVENT_FINANCIAL_STATUSES:
                db.recalc_event_invoice(event_id)
            flash('Artigo adicionado.', 'success')
        else:
            flash('Preencha a descrição do artigo.', 'warning')

    elif action == 'delete_item':
        item_id = int(request.form.get('item_id', 0))
        db.delete_quote_item(item_id, event_id, actor=_current_actor())
        if db.normalize_event_status(event['status']) in db.EVENT_FINANCIAL_STATUSES:
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
        taxa_iva = _parse_taxa_iva(request.form.get('taxa_iva'), None)
        db.update_quote_item(
            item_id, event_id, descricao, quantidade, preco_unitario,
            taxa_iva, actor=_current_actor()
        )
        if db.normalize_event_status(event['status']) in db.EVENT_FINANCIAL_STATUSES:
            db.recalc_event_invoice(event_id)
        flash('Artigo actualizado.', 'success')
    elif action == 'save_version':
        try:
            version = db.create_quote_version(
                event_id, request.form.get('version_reason', '').strip(), actor=_current_actor()
            )
            flash(f'Versão {version} do orçamento guardada.', 'success')
        except ValueError as exc:
            flash(str(exc), 'error')
    elif action == 'add_adjustment':
        reason = request.form.get('adjustment_reason', '').strip()
        try:
            amount = abs(float(request.form.get('adjustment_amount', '0').replace(',', '.')))
            if not reason or amount <= 0:
                raise ValueError('Indique um valor e justificação para o desconto/exceção.')
            db.add_quote_item(
                event_id, 'ajuste_manual', f'Desconto/exceção: {reason}', 1, -amount, 0,
                actor=_current_actor(),
            )
            if db.normalize_event_status(event['status']) in db.EVENT_FINANCIAL_STATUSES:
                db.recalc_event_invoice(event_id)
            flash('Desconto/exceção registado no orçamento.', 'success')
        except ValueError as exc:
            flash(str(exc), 'error')

    return redirect(url_for('eventos.evento_detail', event_id=event_id))


@eventos_bp.route('/configuracao', methods=['GET', 'POST'])
@perm_required('acesso_eventos')
def configuracao():
    if request.method == 'POST':
        action = request.form.get('action')
        try:
            if action == 'resource':
                db.upsert_event_resource(
                    request.form.get('code', '').strip(),
                    request.form.get('name', '').strip(),
                    request.form.get('resource_type', 'equipment').strip(),
                    request.form.get('capacity_carapinas', type=int),
                    request.form.get('capacity_flavors', type=int),
                    request.form.get('active') == '1',
                    request.form.get('notes', '').strip() or None,
                )
                flash('Meio guardado.', 'success')
            elif action == 'pricing':
                raw_rate = request.form.get('taxa_iva')
                rate = None if request.form.get('setting_type') == 'percentage' and not raw_rate else _parse_taxa_iva(raw_rate)
                db.upsert_event_pricing_setting(
                    request.form.get('key', '').strip(), request.form.get('label', '').strip(),
                    request.form.get('setting_type', 'money'), request.form.get('value_gross', '0'),
                    rate, actor=_current_actor(),
                    requires_tax_review=request.form.get('requires_tax_review') == '1',
                    active=request.form.get('active') == '1',
                )
                flash('Preço/configuração guardado.', 'success')
        except ValueError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('eventos.configuracao'))
    return render_template(
        'eventos/configuracao.html', resources=db.get_event_resources(active_only=False),
        pricing=db.get_event_pricing_settings(), tabs=_get_tabs(), active_tab='configuracao',
    )


@eventos_bp.route('/configuracao/portal-marca', methods=['GET', 'POST'])
@perm_required('acesso_administrativo')
def configuracao_portal_marca():
    """Admin-only editor for the customer-facing identity of each store."""
    stores = get_all_stores()
    store_ids = {store['id'] for store in stores}
    try:
        selected_store_id = int(request.values.get('store_id') or (stores[0]['id'] if stores else 0))
    except (TypeError, ValueError):
        selected_store_id = 0
    if selected_store_id not in store_ids:
        flash('Selecione uma loja válida para configurar a marca.', 'error')
        return redirect(url_for('eventos.configuracao'))

    brand = db.get_portal_brand_config(selected_store_id) if selected_store_id else default_portal_brand()
    if request.method == 'POST':
        try:
            _require_admin_portal_brand_csrf()
            fresh_store = get_store(selected_store_id)
            if not fresh_store:
                raise ValueError('A loja selecionada já não existe.')
            if request.form.get('is_default') == '1' and not fresh_store['is_active']:
                raise ValueError('Só uma loja ativa pode ser a marca pública predefinida.')
            values = validate_brand_form(request.form)
            logo_filename = brand.get('logo_filename')
            if request.form.get('remove_logo') == '1':
                logo_filename = None
            if request.files.get('logo_file') and request.files['logo_file'].filename:
                validated_logo = validate_portal_logo(request.files['logo_file'])
                static_root = os.path.join(os.path.dirname(__file__), '..', 'static')
                logo_filename = save_public_portal_logo(validated_logo, static_root)
            db.save_portal_brand_config(
                selected_store_id, values, logo_filename=logo_filename,
                is_default=request.form.get('is_default') == '1',
            )
            flash('A marca pública foi guardada.', 'success')
            return redirect(url_for(
                'eventos.configuracao_portal_marca', store_id=selected_store_id
            ))
        except ValueError as exc:
            flash(str(exc), 'error')
            brand = {**brand, **request.form.to_dict()}

    selected_store = next((store for store in stores if store['id'] == selected_store_id), None)
    return render_template(
        'eventos/configuracao_portal_marca.html',
        stores=stores, selected_store=selected_store, brand=brand,
        tabs=_get_tabs(), active_tab='configuracao',
        csrf_token=_admin_portal_brand_csrf_token(),
    )


@eventos_bp.route('/backfill-iva', methods=['GET', 'POST'])
@perm_required('acesso_eventos')
@perm_required('acesso_financeiro')
def backfill_iva():
    """Reviewed admin action to set the real IVA on committed events whose
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
        if not event or db.normalize_event_status(event['status']) not in db.EVENT_FINANCIAL_STATUSES:
            flash('Evento não encontrado ou não adjudicado.', 'warning')
        else:
            updated = db.bulk_set_taxa_iva(event_id, taxa_iva)
            flash(f'Taxa de IVA de {round(taxa_iva * 100)}% aplicada a {updated} artigo(s) do evento "{event["event_name"]}".', 'success')
        return redirect(url_for('eventos.backfill_iva'))

    events = db.get_won_events_missing_taxa_iva()
    return render_template('eventos/backfill_iva.html', events=events)


# ── Leads ──────────────────────────────────────────────────────────────────────

@eventos_bp.route('/leads')
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
def clientes_search():
    q = request.args.get('q', '').strip()
    results = db.search_event_clients(q) if q else []
    return jsonify([dict(r) for r in results])


@eventos_bp.route('/clientes/<int:client_id>', methods=['GET', 'POST'])
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
def eliminar_evento(evento_id):
    delete_evento(evento_id)
    flash('Evento eliminado.', 'success')
    return redirect(url_for('eventos.recebimentos'))


@eventos_bp.route('/recebimentos/evento/<int:evento_id>/pagamento', methods=['GET', 'POST'])
@perm_required('acesso_eventos')
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
@perm_required('acesso_eventos')
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
