import io
import base64
import os
import sys
import hashlib
import hmac
import secrets
import time
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, current_app, send_file, abort
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask_app.auth import login_required, perm_required
from flask_app.analytics import queue_analytics_event
from flask_app.services.event_portal import (
    cleanup_expired_portal_proofs, resolve_event_address, resolve_event_coordinates,
    save_private_portal_proof,
    validate_portal_proof, suggest_event_addresses,
)
from flask_app.services.event_portal_brand import (
    default_portal_brand, validate_brand_form,
    validate_portal_logo,
)
from flask_app.services.event_quote_pdf import render_quote_pdf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import database as db
from database import (
    get_all_eventos, get_evento_by_id, create_evento, update_evento, delete_evento,
    upsert_evento_items, registar_pagamento_evento, get_eventos_recebimentos,
)
from db.pagamentos import VAT_RATES

eventos_bp = Blueprint('eventos', __name__)


def _public_image_response(asset, legacy_subdirectory):
    """Serve durable image bytes, with a safe bridge for pre-migration files."""
    if not asset:
        abort(404)
    payload = asset.get('logo_data') if 'logo_data' in asset else asset.get('image_data')
    content_type = (
        asset.get('logo_content_type')
        if 'logo_content_type' in asset else asset.get('image_content_type')
    )
    if payload:
        raw = bytes(payload)
        response = send_file(
            io.BytesIO(raw),
            mimetype=content_type or 'application/octet-stream',
            conditional=True,
            etag=hashlib.sha256(raw).hexdigest(),
            max_age=0,
        )
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    legacy_name = asset.get('logo_filename') or asset.get('image_url')
    if legacy_name:
        filename = os.path.basename(str(legacy_name))
        legacy_root = os.path.abspath(os.path.join(
            current_app.static_folder, 'uploads', legacy_subdirectory,
        ))
        path = os.path.abspath(os.path.join(legacy_root, filename))
        if path.startswith(legacy_root + os.sep) and os.path.isfile(path):
            response = send_file(path, conditional=True, max_age=0)
            response.headers['X-Content-Type-Options'] = 'nosniff'
            return response
    abort(404)


@eventos_bp.get('/pedido-evento/marca/<int:store_id>/logo')
def portal_brand_logo(store_id):
    return _public_image_response(
        db.get_public_portal_brand_logo(store_id), 'event_portal_brands',
    )


@eventos_bp.get('/pedido-evento/meios/<int:resource_id>/imagem')
def portal_resource_image(resource_id):
    return _public_image_response(
        db.get_public_event_resource_image(resource_id), 'event_resources',
    )


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


def _portal_submitted_occurrences(form):
    """Rebuild repeated public form rows after a recoverable validation error."""
    keys = (
        'occurrence_date[]', 'occurrence_start[]', 'occurrence_venue[]',
        'occurrence_address[]', 'occurrence_address_normalized[]',
        'occurrence_address_token[]',
    )
    values = {key: form.getlist(key) for key in keys}
    count = max([len(items) for items in values.values()] + [1])
    return [
        {
            'date': values['occurrence_date[]'][index]
            if index < len(values['occurrence_date[]']) else '',
            'start': values['occurrence_start[]'][index]
            if index < len(values['occurrence_start[]']) else '',
            'venue': values['occurrence_venue[]'][index]
            if index < len(values['occurrence_venue[]']) else '',
            'address': values['occurrence_address[]'][index]
            if index < len(values['occurrence_address[]']) else '',
            'normalized_address': values['occurrence_address_normalized[]'][index]
            if index < len(values['occurrence_address_normalized[]']) else '',
            'address_token': values['occurrence_address_token[]'][index]
            if index < len(values['occurrence_address_token[]']) else '',
        }
        for index in range(count)
    ]


def _portal_submitted_phone(form):
    value = str(form.get('client_phone') or '').strip()
    match = re.match(r'^(\+\d{1,4})\s*(.*)$', value)
    return (
        (match.group(1), match.group(2))
        if match else ('+351', value)
    )


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
    supplied = request.form.get('csrf_token', '') or request.headers.get('X-CSRF-Token', '')
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


def _configuration_number(raw, label, maximum=100000):
    """Normalize editable numeric settings before touching the database."""
    if raw is None or str(raw).strip() == '':
        return None
    try:
        value = Decimal(str(raw).strip().replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{label} inválido.')
    if not value.is_finite() or value < 0 or value > maximum:
        raise ValueError(f'{label} deve estar entre 0 e {maximum}.')
    return value


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
    remote = (request.remote_addr or '').strip()
    return hashlib.sha256(remote.encode('utf-8')).hexdigest() if remote else None


def _portal_rate_limit(action, limit, window_seconds=3600):
    if not db.consume_portal_rate_limit(
        _portal_ip_fingerprint(), action, limit, window_seconds
    ):
        raise ValueError('Foram recebidos demasiados pedidos. Tente novamente mais tarde.')


def _address_serializer():
    return URLSafeTimedSerializer(
        current_app.secret_key, salt='event-portal-address-v1'
    )


def _submission_serializer():
    return URLSafeTimedSerializer(
        current_app.secret_key, salt='event-portal-submission-v1'
    )


def _new_submission_identifier():
    return _submission_serializer().dumps({
        'nonce': secrets.token_urlsafe(32),
        'csrf': hashlib.sha256(_portal_csrf_token().encode('utf-8')).hexdigest(),
    })


def _valid_submission_identifier(raw):
    token = str(raw or '').strip()
    if not token:
        raise ValueError(
            'O identificador da submissão está em falta. Atualize a página e tente novamente.'
        )
    try:
        payload = _submission_serializer().loads(token)
    except BadSignature:
        raise ValueError(
            'O identificador da submissão não é válido. Atualize a página e tente novamente.'
        )
    expected_csrf = hashlib.sha256(_portal_csrf_token().encode('utf-8')).hexdigest()
    if (
        not isinstance(payload, dict)
        or not payload.get('nonce')
        or not hmac.compare_digest(str(payload.get('csrf') or ''), expected_csrf)
    ):
        raise ValueError(
            'O identificador da submissão não pertence a esta sessão. '
            'Atualize a página e tente novamente.'
        )
    return token


def _submission_access_code(submission_identifier):
    payload = _submission_serializer().loads(submission_identifier)
    digest = hmac.new(
        str(current_app.secret_key).encode('utf-8'),
        str(payload['nonce']).encode('utf-8'),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest[:9]).decode('ascii').rstrip('=')


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


def _valid_phone(value):
    value = (value or '').strip()
    if not re.fullmatch(r'\+[1-9][0-9 .()/-]*', value):
        raise ValueError('Indique um telefone válido, incluindo o indicativo internacional.')
    digits = ''.join(ch for ch in value if ch.isdigit())
    if len(digits) < 7 or len(digits) > 15:
        raise ValueError('Indique um telefone válido, incluindo o indicativo internacional.')
    if digits.startswith('351'):
        national = digits[3:]
        if len(national) != 9 or national[0] not in ('2', '9'):
            raise ValueError('Indique um número português válido.')
    return '+' + digits


@eventos_bp.route('/pedido-evento/disponibilidade', methods=['GET'])
def portal_date_availability():
    """CSRF-protected coarse status for one requested future date."""
    try:
        _require_portal_csrf()
        requested = datetime.strptime(request.args.get('date', ''), '%Y-%m-%d').date()
        if requested < date.today() or requested > date.today() + timedelta(days=730):
            raise ValueError
        _portal_rate_limit('availability', 60)
        return jsonify({'status': db.get_portal_date_status(requested)})
    except ValueError:
        return jsonify({'error': 'Data inválida.'}), 400


@eventos_bp.route('/pedido-evento/moradas', methods=['GET'])
def portal_address_suggestions():
    """Public-safe address autocomplete; response contains no provider metadata."""
    try:
        _require_portal_csrf()
    except ValueError:
        return jsonify({'error': 'Página expirada.'}), 403
    try:
        _portal_rate_limit('address', 120)
        suggestions = suggest_event_addresses(request.args.get('q', ''))
        for suggestion in suggestions:
            suggestion['token'] = _address_serializer().dumps({
                'label': suggestion['label'],
                'latitude': suggestion['latitude'],
                'longitude': suggestion['longitude'],
            })
        return jsonify({'suggestions': suggestions})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 429


# ── Public customer event portal ───────────────────────────────────────────────

@eventos_bp.route('/pedido-evento', methods=['GET', 'POST'])
def portal_request():
    brand = db.get_default_portal_brand()
    if request.method == 'POST':
        try:
            _require_portal_csrf()
            if request.form.get('website', '').strip():
                raise ValueError('Não foi possível submeter o pedido.')
            try:
                started_at = int(request.form.get('started_at', '0'))
            except (TypeError, ValueError):
                started_at = 0
            # This is only a lightweight bot signal.  Missing/old timestamps are
            # legitimate when JavaScript is blocked or a browser restores a form.
            if started_at > 0:
                elapsed_ms = int(time.time() * 1000) - started_at
                if elapsed_ms < 1200:
                    raise ValueError('Aguarde um momento antes de enviar o pedido.')
            submission_identifier = _valid_submission_identifier(
                request.form.get('submission_identifier')
            )
            _portal_rate_limit('submission', 5)
            dates = request.form.getlist('occurrence_date[]')
            start_times = request.form.getlist('occurrence_start[]')
            venues = request.form.getlist('occurrence_venue[]')
            addresses = request.form.getlist('occurrence_address[]')
            normalized_addresses = request.form.getlist('occurrence_address_normalized[]')
            address_tokens = request.form.getlist('occurrence_address_token[]')
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
                typed_address = (addresses[index] if index < len(addresses) else '').strip()
                normalized_address = (
                    normalized_addresses[index]
                    if index < len(normalized_addresses) else ''
                ).strip()
                address = normalized_address or typed_address
                if not address:
                    raise ValueError('Indique a morada de cada ocorrência.')
                address_token = (
                    address_tokens[index] if index < len(address_tokens) else ''
                ).strip()
                if address_token:
                    try:
                        selected = _address_serializer().loads(address_token, max_age=3600)
                    except (BadSignature, SignatureExpired):
                        raise ValueError('Selecione novamente a morada sugerida.')
                    address = str(selected.get('label') or '').strip()
                    location = resolve_event_coordinates(
                        address, selected.get('latitude'), selected.get('longitude')
                    )
                else:
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
                    # The public form may hide this control, but its policy
                    # default is still an explicit service choice.
                    'service_mode': (
                        request.form.get('service_mode')
                        if request.form.get('service_mode') in ('niva_serves', 'client_serves', 'catering')
                        else 'niva_serves'
                    ),
                })
            result = db.create_portal_event_request({
                'event_name': request.form.get('event_name', '').strip(),
                'event_type': request.form.get('event_type', '').strip(),
                'customer_type': request.form.get('customer_type', '').strip(),
                'company_name': request.form.get('company_name', '').strip(),
                'nif': request.form.get('nif', '').strip(),
                'estimated_guests': request.form.get('estimated_guests'),
                'servings_per_guest': request.form.get('servings_per_guest'),
                'flavours': request.form.getlist('flavours[]'),
                'occurrences': occurrences,
                'client_name': request.form.get('client_name', '').strip(),
                'client_email': request.form.get('client_email', '').strip(),
                'client_phone': _valid_phone(request.form.get('client_phone', '')),
                'privacy_accepted': request.form.get('privacy_accepted') == '1',
                'referral_source': request.form.get('referral_source', '').strip(),
                'resource_preferences': request.form.getlist('resource_preferences[]'),
                'catering_requested': request.form.get('service_mode') == 'catering',
                'brand_store_id': brand.get('store_id'),
                'confirmation_message': brand.get('confirmation_message'),
                 'min_advance_days': brand.get('min_advance_days', 0),
                 'short_notice_warning': brand.get('short_notice_warning'),
                'submission_identifier': submission_identifier,
                'access_code': _submission_access_code(submission_identifier),
            })
            email = db.normalize_portal_email(request.form.get('client_email'))
            access_code = result.get('access_code')
            session['event_portal_email'] = email
            session['event_portal_verified'] = True
            session['event_portal_access_until'] = time.time() + 30 * 60
            session['event_portal_event_id'] = result['event_id']
            if access_code:
                session['event_portal_access_code_once'] = access_code
            if result.get('replayed'):
                flash('Este pedido já tinha sido recebido. Mostramos abaixo o pedido original.', 'success')
                return redirect(url_for('eventos.portal_event', event_id=result['event_id']))
            try:
                db.record_portal_access(
                    email, 'request_submitted', result['event_id'],
                    _portal_ip_fingerprint(),
                )
            except Exception:
                current_app.logger.exception(
                    'Pedido de evento %s criado, mas o registo de acesso falhou.',
                    result['event_id'],
                )
            try:
                queue_analytics_event(
                    'event_request_submitted',
                    occurrence_count=len(occurrences),
                    catering_requested=request.form.get('service_mode') == 'catering',
                )
            except Exception:
                current_app.logger.exception(
                    'Pedido de evento %s criado, mas o evento de analytics falhou.',
                    result['event_id'],
                )
            has_resource_risk = False
            try:
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
            except Exception:
                current_app.logger.exception(
                    'Pedido de evento %s criado, mas a verificação de conflitos falhou.',
                    result['event_id'],
                )
            if has_resource_risk:
                flash(
                    'A preferência de carrinho/arca tem disponibilidade limitada nessa data. '
                    'A equipa pode propor serviço pelo cliente, catering ou outra alternativa.',
                    'warning',
                )
            if result.get('short_notice_warning'):
                flash(result['short_notice_warning'], 'warning')
            if access_code:
                flash('Recebemos o seu pedido. Guarde o código de consulta mostrado abaixo.', 'success')
                return redirect(url_for('eventos.portal_event', event_id=result['event_id']))
            flash('Recebemos o seu pedido. Guarde o código de consulta mostrado abaixo.', 'success')
            return redirect(url_for('eventos.portal_event', event_id=result['event_id']))
        except ValueError as exc:
            flash(str(exc), 'error')

    # Never disclose the complete unavailable-date set to an unauthenticated
    # browser.  The single-date endpoint above returns only a coarse status.
    calendar_dates = []
    phone_code, phone_national = _portal_submitted_phone(request.form)
    return render_template(
        'eventos/portal_request.html', calendar_dates=calendar_dates,
        csrf_token=_portal_csrf_token(), form=request.form, brand=brand,
        submission_identifier=(
            request.form.get('submission_identifier')
            if request.method == 'POST' and request.form.get('submission_identifier')
            else _new_submission_identifier()
        ),
        submitted_occurrences=_portal_submitted_occurrences(request.form),
        phone_code=phone_code, phone_national=phone_national,
        resources=db.get_event_resources(),
        event_flavours=[
            {'id': flavour['id'], 'name': flavour['nome_corrente']}
            for flavour in db.get_portal_flavours()
        ],
        availability_url=url_for('eventos.portal_date_availability'),
        address_suggestions_url=url_for('eventos.portal_address_suggestions'),
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


@eventos_bp.route('/portal-eventos/pedido/<int:event_id>/pdf')
def portal_quote_pdf(event_id):
    email = _portal_login_required()
    if not email:
        return redirect(url_for('eventos.portal_access'))
    if event_id != _portal_event_id():
        return ('Pedido não encontrado.', 404)
    event = db.get_portal_event_for_email(event_id, email)
    if not event or event.get('customer_type') == 'empresa' or not event.get('quote_items_public'):
        return ('Proposta não disponível.', 404)
    db.record_portal_access(email, 'quote_pdf_downloaded', event_id, _portal_ip_fingerprint())
    payload = render_quote_pdf(event, event.get('portal_brand'))
    response = current_app.response_class(payload, mimetype='application/pdf')
    response.headers['Content-Disposition'] = f'attachment; filename="proposta-evento-{event_id}.pdf"'
    return response


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
        if accepted:
            queue_analytics_event('event_quote_accepted')
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
        queue_analytics_event(
            'event_deposit_proof_uploaded',
            file_type='pdf' if metadata.get('content_type') == 'application/pdf' else 'image',
        )
        flash('Comprovativo recebido. A equipa irá validá-lo.', 'success')
    except ValueError as exc:
        flash(str(exc), 'error')
    except Exception:
        flash('Não foi possível guardar o comprovativo. Tente novamente.', 'error')
    return redirect(url_for('eventos.portal_event', event_id=event_id))

TABS = [
    {'id': 'dashboard', 'label': 'Dashboard', 'icon': '📊', 'url_endpoint': 'eventos.dashboard'},
    {'id': 'pipeline',  'label': 'Pipeline de Eventos',  'icon': '📋', 'url_endpoint': 'eventos.pipeline'},
    {'id': 'calendario', 'label': 'Calendário', 'icon': '🗓️', 'url_endpoint': 'eventos.calendario'},
    {'id': 'clientes',  'label': 'Clientes',  'icon': '👥', 'url_endpoint': 'eventos.clientes'},
    {'id': 'locais',   'label': 'Locais',    'icon': '📍', 'url_endpoint': 'eventos.locais'},
    {'id': 'artigos',   'label': 'Artigos',   'icon': '🏷️', 'url_endpoint': 'eventos.artigos'},
    {'id': 'configuracao', 'label': 'Configuração', 'icon': '⚙️', 'url_endpoint': 'eventos.configuracao'},
    {'id': 'recebimentos', 'label': 'Recebimentos', 'icon': '💶', 'url_endpoint': 'eventos.recebimentos'},
]

PIPELINE_COLUMNS = (
    'event', 'client', 'date', 'type', 'venue', 'guests',
    'source', 'status', 'budget', 'email', 'phone',
)


def _pipeline_user_key():
    user = session.get('user') or {}
    if isinstance(user, dict):
        return str(user.get('id') or user.get('email') or user.get('username') or '')
    return str(user or '')


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
    sort_by = request.args.get('sort', 'date')
    if sort_by not in PIPELINE_COLUMNS:
        sort_by = 'date'
    sort_direction = request.args.get('direction', 'asc').lower()
    if sort_direction not in ('asc', 'desc'):
        sort_direction = 'asc'
    events = db.get_events(
        status=status_filter or None, sort_by=sort_by,
        sort_direction=sort_direction, **filters,
    )
    from flask_app.google_sheets_sync import get_sheet_sync_status
    sync_run = get_sheet_sync_status()
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
                            pipeline_columns=PIPELINE_COLUMNS,
                            pipeline_preferences=db.get_pipeline_view_preferences(_pipeline_user_key()),
                            sort_by=sort_by,
                            sort_direction=sort_direction,
                           sync_run=sync_run,
                           tabs=tabs,
                           active_tab='pipeline')


@eventos_bp.post('/pipeline/preferencias')
@perm_required('acesso_eventos')
def save_pipeline_preferences():
    payload = request.get_json(silent=True) or {}
    columns = payload.get('columns')
    if not isinstance(columns, list):
        return jsonify({'error': 'Formato de preferências inválido.'}), 400
    normalized = []
    for column in columns:
        if column in PIPELINE_COLUMNS and column not in normalized:
            normalized.append(column)
    if not normalized:
        return jsonify({'error': 'Selecione pelo menos uma coluna.'}), 400
    db.save_pipeline_view_preferences(_pipeline_user_key(), normalized)
    return jsonify({'columns': normalized})


@eventos_bp.get('/evento/<int:event_id>/painel')
@perm_required('acesso_eventos')
def event_panel(event_id):
    event = db.get_event(event_id)
    if not event:
        abort(404)
    return render_template(
        'eventos/_event_panel.html', event=event,
        quote_items=db.get_quote_items(event_id),
        quote_totals=db.get_event_quote_totals(event_id),
        event_history=db.get_event_history(event_id),
        venue=db.get_event_primary_venue(event_id),
        status_labels=STATUS_LABELS, status_colors=STATUS_COLORS,
    )


@eventos_bp.route('/locais', methods=['GET', 'POST'])
@perm_required('acesso_eventos')
def locais():
    if request.method == 'POST':
        try:
            venue_id = db.save_event_venue(request.form, actor=_current_actor())
            flash('Local registado.', 'success')
            return redirect(url_for('eventos.local_detail', venue_id=venue_id))
        except ValueError as exc:
            flash(str(exc), 'error')
    search_q = request.args.get('q', '').strip()
    return render_template(
        'eventos/locais.html', venues=db.get_event_venues(search_q or None),
        search_q=search_q, tabs=_get_tabs(), active_tab='locais',
    )


@eventos_bp.route('/locais/<int:venue_id>', methods=['GET', 'POST'])
@perm_required('acesso_eventos')
def local_detail(venue_id):
    venue = db.get_event_venue(venue_id)
    if not venue:
        flash('Local não encontrado.', 'error')
        return redirect(url_for('eventos.locais'))
    if request.method == 'POST':
        if request.form.get('action') == 'link_occurrence':
            try:
                db.link_event_occurrence_to_venue(
                    int(request.form.get('occurrence_id', '')), venue_id, _current_actor(),
                )
                flash('Ocorrência associada ao local. O texto histórico não foi alterado.', 'success')
            except (TypeError, ValueError) as exc:
                flash(str(exc) or 'Não foi possível associar a ocorrência.', 'error')
            return redirect(url_for('eventos.local_detail', venue_id=venue_id))
        try:
            db.save_event_venue(request.form, actor=_current_actor(), venue_id=venue_id)
            flash('Dados do local atualizados.', 'success')
            return redirect(url_for('eventos.local_detail', venue_id=venue_id))
        except ValueError as exc:
            flash(str(exc), 'error')
    return render_template(
        'eventos/local_detail.html', venue=venue,
        events=db.get_event_venue_events(venue_id),
        unlinked_occurrences=db.get_unlinked_venue_occurrences(venue_id),
        history=db.get_event_venue_history(venue_id),
        tabs=_get_tabs(), active_tab='locais', status_labels=STATUS_LABELS,
        status_colors=STATUS_COLORS,
    )


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
                'customer_type': event.get('customer_type'),
                'company_name': event.get('company_name'),
                'nif': event.get('nif'),
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
            queue_analytics_event('event_quote_version_saved')
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
    can_edit_config = bool((session.get('user') or {}).get('acesso_administrativo'))
    if request.method == 'POST' and not can_edit_config:
        return ('Acesso administrativo necessário para alterar a configuração.', 403)
    if request.method == 'POST':
        try:
            _require_admin_portal_brand_csrf()
            values = validate_brand_form(request.form)
            codes = request.form.getlist('resource_code[]')
            names = request.form.getlist('resource_name[]')
            types = request.form.getlist('resource_type[]')
            existing_images = request.form.getlist('resource_existing_image[]')
            ids = request.form.getlist('resource_id[]')
            fields = {
                key: request.form.getlist('resource_' + key + '[]')
                for key in ('capacity_carapinas', 'capacity_flavors', 'notes',
                            'active', 'public_description', 'public_capacity_flavors',
                            'width_cm', 'height_cm', 'length_cm', 'weight_kg',
                            'public_customer_requirements')
            }
            lengths = [len(codes), len(names), len(types), len(existing_images)]
            lengths += [len(values) for values in fields.values()]
            if len(set(lengths)) != 1:
                raise ValueError('A lista de meios está incompleta.')
            if len(set(c.strip().casefold() for c in codes)) != len(codes):
                raise ValueError('Os códigos dos meios devem ser únicos.')
            if any(not c.strip() for c in codes) or any(not n.strip() for n in names):
                raise ValueError('Cada meio precisa de código e nome.')
            resources, uploads = [], request.files.getlist('resource_image[]')
            if len(uploads) not in (0, len(codes)):
                raise ValueError('A lista de imagens dos meios está incompleta.')
            for i, code in enumerate(codes):
                image = existing_images[i] or None
                image_asset = None
                if i < len(uploads) and uploads[i].filename:
                    image_asset = validate_portal_logo(uploads[i])
                    image = 'database:' + secrets.token_hex(16)
                get = lambda key: request.form.getlist('resource_' + key + '[]')[i]
                resources.append({'code': code.strip(), 'name': names[i].strip(), 'resource_type': types[i],
                    'capacity_carapinas': _configuration_number(get('capacity_carapinas'), 'A capacidade'),
                    'capacity_flavors': _configuration_number(get('capacity_flavors'), 'A capacidade de sabores', 6),
                    'notes': get('notes') or None, 'active': get('active') == '1',
                    'image_url': image, 'image_asset': image_asset,
                    'public_description': get('public_description') or None,
                    'public_capacity_flavors': _configuration_number(get('public_capacity_flavors'), 'A capacidade pública', 6),
                    'width_cm': _configuration_number(get('width_cm'), 'A largura'),
                    'height_cm': _configuration_number(get('height_cm'), 'A altura'),
                    'length_cm': _configuration_number(get('length_cm'), 'O comprimento'),
                    'weight_kg': _configuration_number(get('weight_kg'), 'O peso'),
                    'public_customer_requirements': get('public_customer_requirements') or None})
            nc, nn = request.form.get('new_resource_code','').strip(), request.form.get('new_resource_name','').strip()
            if nc or nn:
                if not nc or not nn: raise ValueError('O meio precisa de código e nome.')
                if nc.casefold() in {item['code'].casefold() for item in resources}:
                    raise ValueError('Os códigos dos meios devem ser únicos.')
                resources.append({'code':nc, 'name':nn, 'resource_type':'equipment', 'active':True})
            pricing = []
            pricing_keys = request.form.getlist('pricing_key[]')
            pricing_labels = request.form.getlist('pricing_label[]')
            pricing_types = request.form.getlist('pricing_type[]')
            pricing_values = request.form.getlist('pricing_value[]')
            pricing_ivas = request.form.getlist('pricing_iva[]')
            pricing_reviews = request.form.getlist('pricing_review[]')
            pricing_active = request.form.getlist('pricing_active[]')
            if len({len(pricing_keys), len(pricing_labels), len(pricing_types),
                    len(pricing_values), len(pricing_ivas), len(pricing_reviews),
                    len(pricing_active)}) != 1:
                raise ValueError('A lista de preços está incompleta.')
            if len({key.strip().casefold() for key in pricing_keys}) != len(pricing_keys):
                raise ValueError('As chaves de preços devem ser únicas.')
            for i, key in enumerate(pricing_keys):
                setting_type = pricing_types[i]
                if setting_type not in ('money', 'percentage'):
                    raise ValueError('Tipo de preço inválido.')
                gross = _configuration_number(pricing_values[i], 'O valor do preço')
                if gross is None or (setting_type == 'percentage' and gross > 100):
                    raise ValueError('O valor do preço é inválido.')
                iva = pricing_ivas[i]
                taxa = _configuration_number(iva, 'A taxa de IVA', 100)
                pricing.append({'key':key.strip(), 'label':pricing_labels[i].strip(), 'setting_type':setting_type,
                    'value_gross':gross, 'taxa_iva':None if taxa is None else taxa / 100,
                    'requires_tax_review':pricing_reviews[i]=='1', 'active':pricing_active[i]=='1'})
            # All parsing and upload validation happens before the first DB call.
            logo_upload = None
            if request.files.get('logo_file') and request.files['logo_file'].filename:
                logo_upload = validate_portal_logo(request.files['logo_file'])
            current = db.get_default_portal_brand()
            logo = current.get('logo_filename')
            if request.form.get('remove_logo') == '1': logo = None
            if logo_upload:
                logo = secrets.token_hex(16) + '.' + logo_upload['extension']
            db.save_event_configuration(
                values, logo, resources, pricing,
                request.form.getlist('flavour_ids[]'), _current_actor(),
                logo_asset=logo_upload,
            )
            flash('Configuração guardada.', 'success')
        except ValueError as exc:
            flash(str(exc), 'error')
        except Exception:
            current_app.logger.exception('Falha ao guardar configuração de Eventos.')
            flash('Não foi possível guardar a configuração. Verifique os dados e tente novamente.', 'error')
        return redirect(url_for('eventos.configuracao'))
    return render_template(
        'eventos/configuracao.html', resources=db.get_event_resources(active_only=False),
        pricing=db.get_event_pricing_settings(), tabs=_get_tabs(), active_tab='configuracao',
        portal_flavours=db.get_event_portal_flavour_configuration(),
        brand=db.get_default_portal_brand(),
        csrf_token=_admin_portal_brand_csrf_token(),
        can_edit_config=can_edit_config,
    )


@eventos_bp.route('/configuracao/portal-marca', methods=['GET', 'POST'])
@perm_required('acesso_eventos')
def configuracao_portal_marca():
    """Admin-only editor for the single public Events form configuration.

    The database still keeps store associations because existing portal requests
    reference the brand that was used when they were submitted.  The editor,
    however, intentionally exposes only the one active public configuration.
    """
    user = session.get('user') or {}
    if not user.get('acesso_administrativo') and not user.get('acesso_gestor'):
        return redirect(url_for('home.index'))
    return redirect(url_for('eventos.configuracao'))


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
    return redirect(url_for('eventos.pipeline', **request.args))


@eventos_bp.route('/leads/nova', methods=['GET', 'POST'])
@perm_required('acesso_eventos')
def nova_lead():
    # Legacy bookmarks must never create a second, lead-only funnel.
    return redirect(url_for('eventos.pipeline'))

    if request.method == 'GET':
        return redirect(url_for('eventos.pipeline'))
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
        return redirect(url_for('eventos.pipeline'))

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
        return redirect(url_for('eventos.pipeline'))
    # Keep legacy URLs usable without exposing a second lead funnel.  A linked
    # event is authoritative; otherwise the canonical destination is pipeline.
    event_id = db.get_event_id_for_lead(lead_id)
    if event_id:
        return redirect(url_for('eventos.evento_detail', event_id=event_id))
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
        from flask_app.google_sheets_sync import enqueue_sheet_sync, start_sheet_sync_worker
        job = enqueue_sheet_sync(requested_by=_current_actor(), trigger='manual')
        start_sheet_sync_worker()
        if job and job.get('status') == 'running':
            flash('A sincronização já está em curso. Pode acompanhar o progresso nesta página.', 'info')
        else:
            flash('Sincronização iniciada. Pode continuar a trabalhar enquanto decorre.', 'success')
    except Exception as e:
        current_app.logger.exception('Não foi possível iniciar a sincronização de Eventos')
        flash('Não foi possível iniciar a sincronização. Tente novamente.', 'error')
    return redirect(url_for('eventos.pipeline'))


@eventos_bp.route('/sync-sheets/status')
@perm_required('acesso_eventos')
def sync_sheets_status():
    from flask_app.google_sheets_sync import get_sheet_sync_status
    job_id = request.args.get('job_id', type=int)
    job = get_sheet_sync_status(job_id)
    if not job:
        return jsonify({'status': 'idle'})

    def serialized(value):
        return value.isoformat() if hasattr(value, 'isoformat') else value

    return jsonify({
        'id': job['id'],
        'status': job['status'],
        'phase': job.get('phase'),
        'total_rows': job.get('total_rows') or 0,
        'processed_rows': job.get('processed_rows') or 0,
        'inserted': job.get('inserted') or 0,
        'updated': job.get('updated') or 0,
        'errors': job.get('errors') or 0,
        'error_summary': job.get('error_summary'),
        'created_at': serialized(job.get('created_at')),
        'started_at': serialized(job.get('started_at')),
        'finished_at': serialized(job.get('finished_at')),
    })


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
