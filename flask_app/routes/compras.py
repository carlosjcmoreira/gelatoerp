from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, jsonify
from flask_app.auth import perm_required, any_perm_required
from datetime import date, datetime
import json
import sys, os, logging, math
from io import BytesIO
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_artigos_administrativos, add_artigo_administrativo,
    update_artigo_administrativo, toggle_artigo_administrativo,
    delete_artigo_administrativo,
    criar_ordem_transferencia,
    create_invoice,
    update_invoice,
    mark_payment_executed,
    get_suppliers,
    propose_invoice_payment,
    suggest_payment_date,
    get_payment_methods_config,
    upsert_supplier,
    get_supplier_by_nif,
    get_supplier_by_name,
    get_cost_centers,
    get_cost_categories_tree,
    get_invoice_linhas,
    list_materiais,
    LOCAIS_STOCK,
    UNIDADES_MATERIAIS,
    derive_local_from_store,
    get_confirming_contracts,
)
from db.faturas import (
    get_invoices,
    count_invoices,
    get_invoice,
    get_invoice_pdf,
    save_invoice_pdf,
    get_stores_list,
    get_distinct_supplier_names,
    get_supplier_by_alias,
    get_invoice_status_labels_map,
    find_similar_suppliers,
    INVOICE_CATEGORIES,
    ONEDRIVE_SUBFOLDERS,
    DOCUMENT_TYPE_LABELS,
)
import flask_app.services.faturas as faturas_svc
from flask_app.services import ServiceError
from flask_app.utils.finance import (
    parse_date as _parse_date,
    find_duplicate_invoice_ids as _find_duplicate_invoice_ids,
)

logger = logging.getLogger(__name__)

ALLOWED_UPLOAD_EXTENSIONS = {'pdf'}
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'heic', 'heif', 'webp'}


def _ext(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

compras_bp = Blueprint('compras', __name__)

TABS = [
    {'id': 'faturas', 'label': 'Faturas', 'icon': '🧾', 'url_endpoint': 'compras.faturas'},
    {'id': 'nova_fatura', 'label': 'Registar Documento', 'icon': '➕', 'url_endpoint': 'compras.nova_fatura'},
    {'id': 'artigos', 'label': 'Artigos de Fornecimento', 'icon': '📋', 'url_endpoint': 'compras.artigos'},
    {'id': 'fornecedores', 'label': 'Fornecedores', 'icon': '🏭', 'url_endpoint': 'faturas.fornecedores'},
    {'id': 'criar_ordem', 'label': 'Criar Ordem de Transferência', 'icon': '📦', 'url_endpoint': 'compras.criar_ordem'},
]


def _get_username():
    return session.get('user', {}).get('username', 'sistema')


@compras_bp.route('/')
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — module landing page
def index():
    from db.tiles import get_tile_visibility, seed_tile_config, get_tile_labels, get_tile_icons, get_module_labels
    seed_tile_config('compras', [{'id': t['id'], 'label': t['label']} for t in TABS])
    visibility = get_tile_visibility('compras')
    labels = get_tile_labels('compras')
    icons = get_tile_icons('compras')
    custom_mod = get_module_labels().get('compras')
    items = [
        {'icon': icons.get(t['id']) or t['icon'], 'label': labels.get(t['id']) or t['label'], 'url': url_for(t['url_endpoint'])}
        for t in TABS
        if visibility.get(t['id'], True)
    ]
    return render_template('components/section_menu.html', items=items,
                           menu_title=f'🛒 {custom_mod}' if custom_mod else '🛒 Compras e Faturas')


@compras_bp.route('/faturas')
@any_perm_required('acesso_financeiro', 'acesso_compras')
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — invoice listing
def faturas():
    from datetime import date as _date, datetime as _datetime

    from urllib.parse import urlencode
    today = _date.today()
    PAGE_SIZE = 50

    # Filter session persistence
    _FILTER_KEYS = ('supplier_id', 'supplier_name', 'store_id', 'status',
                    'date_from', 'date_to', 'date_field', 'q', 'document_type', 'sem_evidencia')
    if request.args.get('clear') == '1':
        session.pop('faturas_filters', None)
        return redirect(url_for('compras.faturas'))
    _has_any_filter_param = any(request.args.get(k) for k in _FILTER_KEYS)
    if not _has_any_filter_param and 'faturas_filters' in session:
        _saved = session['faturas_filters']
        if _saved:
            return redirect(url_for('compras.faturas') + '?' + urlencode(_saved))

    supplier_id_raw = request.args.get('supplier_id', '').strip()
    supplier_filter_id = int(supplier_id_raw) if supplier_id_raw.isdigit() else None
    supplier_name_filter = request.args.get('supplier_name', '').strip()
    store_id_raw = request.args.get('store_id', '').strip()
    store_id_filter = int(store_id_raw) if store_id_raw.isdigit() else None
    status_filter = request.args.get('status', '').strip()
    date_from_raw = request.args.get('date_from', '').strip()
    date_to_raw = request.args.get('date_to', '').strip()
    date_field = request.args.get('date_field', 'issue_date').strip()
    if date_field not in ('issue_date', 'due_date', 'paid_date'):
        date_field = 'issue_date'
    q_filter = request.args.get('q', '').strip()
    order_by = request.args.get('order_by', 'issue_date').strip()
    if order_by not in ('issue_date', 'due_date', 'paid_date', 'amount_eur', 'supplier_name', 'invoice_number', 'status'):
        order_by = 'issue_date'
    order_dir = request.args.get('order_dir', 'desc').strip()
    if order_dir not in ('asc', 'desc'):
        order_dir = 'desc'
    try:
        page = max(1, int(request.args.get('page', '1') or '1'))
    except ValueError:
        page = 1

    date_from = _parse_date(date_from_raw)
    date_to = _parse_date(date_to_raw)
    document_type_filter = request.args.get('document_type', '').strip()
    if document_type_filter not in DOCUMENT_TYPE_LABELS:
        document_type_filter = ''

    # Alert for permanently failed OneDrive uploads
    try:
        from db.faturas import count_onedrive_failed as _count_od_failed
        _od_failed = _count_od_failed()
        if _od_failed > 0:
            flash(
                f'⚠️ {_od_failed} fatura(s) com falha permanente no arquivo OneDrive. '
                'Verifique as faturas marcadas com ❌.',
                'warning',
            )
    except Exception:
        pass

    sem_evidencia_raw = request.args.get('sem_evidencia', '')
    sem_evidencia_filter = sem_evidencia_raw == '1'
    # Explicit '0' means the user clicked "off" — clear it from session so navigating
    # back doesn't re-apply the filter via session restore
    if sem_evidencia_raw == '0' and 'faturas_filters' in session:
        session['faturas_filters'].pop('sem_evidencia', None)
        session.modified = True

    filter_kwargs = dict(
        supplier_id=supplier_filter_id,
        supplier_name=supplier_name_filter or None,
        store_id=store_id_filter,
        status=status_filter or None,
        date_from=date_from,
        date_to=date_to,
        date_field=date_field,
        document_type=document_type_filter or None,
        search=q_filter or None,
        sem_evidencia=True if sem_evidencia_filter else None,
    )
    total_count = count_invoices(**filter_kwargs, exclude_gov=True)
    total_pages = max(1, math.ceil(total_count / PAGE_SIZE))
    page = min(page, total_pages)
    offset = (page - 1) * PAGE_SIZE

    invoices = get_invoices(
        **filter_kwargs,
        exclude_gov=True,
        order_by=order_by,
        order_dir=order_dir,
        limit=PAGE_SIZE,
        offset=offset,
    )
    for inv in invoices:
        if inv['status'] in ('pending_review', 'scheduled') and inv.get('due_date') and inv['due_date'] < today:
            inv['display_status'] = 'overdue'
        else:
            inv['display_status'] = inv['status']

    # Duplicate detection: flag invoices where same supplier has same normalised document number
    _dup_ids = _find_duplicate_invoice_ids(invoices)
    for inv in invoices:
        inv['has_duplicate'] = inv['id'] in _dup_ids

    payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]
    suppliers = []
    all_supplier_names = get_distinct_supplier_names()
    stores = get_stores_list()
    has_filters = bool(
        supplier_filter_id or supplier_name_filter or store_id_filter
        or status_filter or date_from_raw or date_to_raw
        or document_type_filter or q_filter or sem_evidencia_filter
    )
    if has_filters:
        _filter_save = {}
        if q_filter:
            _filter_save['q'] = q_filter
        if supplier_name_filter:
            _filter_save['supplier_name'] = supplier_name_filter
        elif supplier_filter_id:
            _filter_save['supplier_id'] = supplier_filter_id
        if store_id_filter:
            _filter_save['store_id'] = store_id_filter
        if status_filter:
            _filter_save['status'] = status_filter
        if document_type_filter:
            _filter_save['document_type'] = document_type_filter
        if date_from_raw:
            _filter_save['date_from'] = date_from_raw
        if date_to_raw:
            _filter_save['date_to'] = date_to_raw
        if date_field != 'issue_date':
            _filter_save['date_field'] = date_field
        if sem_evidencia_filter:
            _filter_save['sem_evidencia'] = '1'
        session['faturas_filters'] = _filter_save
    _fqs_d = {}
    if q_filter:
        _fqs_d['q'] = q_filter
    if supplier_name_filter:
        _fqs_d['supplier_name'] = supplier_name_filter
    elif supplier_filter_id:
        _fqs_d['supplier_id'] = supplier_filter_id
    if store_id_filter:
        _fqs_d['store_id'] = store_id_filter
    if status_filter:
        _fqs_d['status'] = status_filter
    if document_type_filter:
        _fqs_d['document_type'] = document_type_filter
    if date_from_raw:
        _fqs_d['date_from'] = date_from_raw
    if date_to_raw:
        _fqs_d['date_to'] = date_to_raw
    if date_field != 'issue_date':
        _fqs_d['date_field'] = date_field
    if sem_evidencia_filter:
        _fqs_d['sem_evidencia'] = '1'
    filter_qs = '?' + urlencode(_fqs_d) if _fqs_d else '?'

    # Build sem_evidencia toggle URLs
    # Off URL uses sem_evidencia=0 (not removal) so _has_any_filter_param stays True
    # and session restore doesn't swallow the explicit "clear" intent.
    _fqs_no_ev = {k: v for k, v in _fqs_d.items() if k != 'sem_evidencia'}
    _fqs_with_ev = {**_fqs_no_ev, 'sem_evidencia': '1'}
    _fqs_ev_zero = {**_fqs_no_ev, 'sem_evidencia': '0'}
    sem_ev_off_url = '?' + urlencode(_fqs_ev_zero)
    sem_ev_on_url = '?' + urlencode(_fqs_with_ev)

    # Build base query (no status) for badge strip counts
    _bfqs_d = {k: v for k, v in _fqs_d.items() if k != 'status'}
    base_filter_qs = '?' + urlencode(_bfqs_d) if _bfqs_d else '?'
    _count_base = {k: v for k, v in filter_kwargs.items() if k != 'status'}
    status_counts = {
        'overdue': count_invoices(**_count_base, status='overdue', exclude_gov=True),
        'pending_review': count_invoices(**_count_base, status='pending_review', exclude_gov=True),
        'scheduled': count_invoices(**_count_base, status='scheduled', exclude_gov=True),
        'paid': count_invoices(**_count_base, status='paid', exclude_gov=True),
    }

    # Count invoices without evidence (ignoring current status/sem_evidencia filter)
    # sem_evidencia=True in _build_invoice_where already excludes draft+cancelled
    _count_no_status_no_ev = {k: v for k, v in _count_base.items() if k != 'sem_evidencia'}
    sem_evidencia_count = count_invoices(**_count_no_status_no_ev, sem_evidencia=True, exclude_gov=True)

    return render_template('financeiro/faturas/index.html',
                           # ── scope / URL routing ──────────────────────────
                           view='documento',
                           scope='compras',
                           back_url=url_for('compras.index'),
                           filter_action_url=url_for('compras.faturas'),
                           panel_base_url=url_for('compras.invoice_panel', invoice_id=0),
                           paid_date_url_tpl='/compras/faturas/SET_ID/set_paid_date',
                           inv_api_prefix='/compras/faturas',
                           # ── invoices / pagination ────────────────────────
                           invoices=invoices,
                           page=page,
                           total_pages=total_pages,
                           total_count=total_count,
                           page_size=PAGE_SIZE,
                           # ── filters (Financeiro naming conventions) ──────
                           search=q_filter,
                           store_id=str(store_id_filter) if store_id_filter else '',
                           supplier_name_filter=supplier_name_filter,
                           document_type_filter=document_type_filter,
                           date_from_raw=date_from_raw,
                           date_to_raw=date_to_raw,
                           date_field=date_field,
                           order_by=order_by,
                           order_dir=order_dir,
                           filter_qs=filter_qs,
                           has_filters=has_filters,
                           # Compras also keeps single-status and badge strip vars:
                           status_filter=status_filter,
                           status_counts=status_counts,
                           base_filter_qs=base_filter_qs,
                           sem_evidencia_filter=sem_evidencia_filter,
                           sem_evidencia_count=sem_evidencia_count,
                           sem_ev_on_url=sem_ev_on_url,
                           sem_ev_off_url=sem_ev_off_url,
                           # ── Financeiro defaults (not used in Compras) ────
                           statuses_filter=[],
                           show_all=False,
                           default_filter_active=False,
                           month_raw='',
                           month_options=[],
                           category_filter='',
                           centro_custo_filter=None,
                           all_categories=[],
                           cost_centers=[],
                           cost_categories_tree=[],
                           saved_views=[],
                           pending_installments=[],
                           confirming_contracts=[],
                           grupos=[],
                           grupos_cc=[],
                           grupos_cat=[],
                           # ── shared lookups ───────────────────────────────
                           stores=stores,
                           all_supplier_names=all_supplier_names,
                           payment_methods=payment_methods,
                           document_type_labels=DOCUMENT_TYPE_LABELS,
                           today=today)


@compras_bp.route('/faturas/<int:invoice_id>/set_status', methods=['POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — inline status change
def set_invoice_status(invoice_id: int):
    from flask import jsonify
    from datetime import date as _date
    data = request.get_json(silent=True) or {}
    new_status = data.get('status', '').strip()
    valid_statuses = {'pending_review', 'scheduled', 'paid', 'cancelled'}
    if new_status not in valid_statuses:
        return jsonify({'ok': False, 'error': 'Estado inválido'}), 400
    _actor = session.get('user', {}).get('username', 'sistema')
    updates = {'status': new_status}
    if new_status == 'paid':
        updates['paid_date'] = _date.today()
    else:
        updates['paid_date'] = None
    try:
        update_invoice(invoice_id, updates, changed_by=_actor)
    except Exception as e:
        logger.error('set_invoice_status error: %s', e)
        return jsonify({'ok': False, 'error': str(e) or 'Erro interno'}), 500
    status_label = get_invoice_status_labels_map().get(new_status, new_status)
    return jsonify({'ok': True, 'status': new_status, 'status_label': status_label})


@compras_bp.route('/faturas/<int:invoice_id>/set_store', methods=['POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — inline store assignment
def set_invoice_store(invoice_id: int):
    from flask import jsonify
    data = request.get_json(silent=True) or {}
    store_id_raw = data.get('store_id')
    store_id = int(store_id_raw) if store_id_raw else None
    store_name = None
    if store_id:
        for s in get_stores_list():
            if s['id'] == store_id:
                store_name = s['name']
                break
    _actor = session.get('user', {}).get('username', 'sistema')
    try:
        update_invoice(invoice_id, {'store_id': store_id}, changed_by=_actor)
    except Exception as e:
        logger.error('set_invoice_store error: %s', e)
        return jsonify({'ok': False, 'error': 'Erro interno'}), 500
    return jsonify({'ok': True, 'store_id': store_id, 'store_name': store_name})


@compras_bp.route('/faturas/<int:invoice_id>/set_paid_date', methods=['POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — inline paid-date edit
def set_invoice_paid_date(invoice_id: int):
    from datetime import datetime as _datetime
    data = request.get_json(silent=True) or {}
    paid_date_raw = (data.get('paid_date') or '').strip()
    paid_date = None
    if paid_date_raw:
        try:
            paid_date = _datetime.strptime(paid_date_raw, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'ok': False, 'error': 'Data inválida'}), 400
    _actor = session.get('user', {}).get('username', 'sistema')
    try:
        update_invoice(invoice_id, {'paid_date': paid_date}, changed_by=_actor)
    except Exception as e:
        logger.error('set_invoice_paid_date error: %s', e)
        return jsonify({'ok': False, 'error': 'Erro interno'}), 500
    paid_date_formatted = paid_date.strftime('%d/%m/%Y') if paid_date else ''
    return jsonify({'ok': True, 'paid_date_formatted': paid_date_formatted})


def _safe_return_url(raw: str) -> str:
    from urllib.parse import urlparse
    if raw:
        parsed = urlparse(raw)
        if not parsed.scheme and not parsed.netloc:
            return raw
    return url_for('compras.faturas')


@compras_bp.route('/faturas/<int:invoice_id>/panel')
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — slide-over panel view
def invoice_panel(invoice_id: int):
    from datetime import date as _date
    from db.contabilidade import (
        get_tickets_for_invoice,
        ACCOUNTING_STATUS_LABELS,
        ACCOUNTING_STATUS_BADGE,
    )
    inv = get_invoice(invoice_id)
    if not inv:
        return '<p class="text-danger p-3">Fatura não encontrada.</p>', 404
    today = _date.today()
    if inv['status'] in ('pending_review', 'scheduled') and inv.get('due_date') and inv['due_date'] < today:
        inv['display_status'] = 'overdue'
        inv['status_label'] = get_invoice_status_labels_map().get('overdue', 'Vencida')
    panel_return_url = _safe_return_url(request.args.get('return_url', ''))
    stores = get_stores_list()
    payment_methods = get_payment_methods_config()
    linhas = get_invoice_linhas(invoice_id)
    materiais = list_materiais(apenas_ativos=True)
    stock_local_derivado = derive_local_from_store(
        store_name=inv.get('store_name'), store_id=inv.get('store_id')
    )
    suppliers = get_suppliers()
    cont_tickets = get_tickets_for_invoice(invoice_id)
    acc_status = inv.get('accounting_status') or 'por_contabilizar'
    inv['accounting_status_label'] = ACCOUNTING_STATUS_LABELS.get(acc_status, acc_status)
    inv['accounting_status_badge'] = ACCOUNTING_STATUS_BADGE.get(acc_status, 'bg-secondary')
    return render_template(
        'compras/_panel.html',
        inv=inv,
        today=today,
        document_type_labels=DOCUMENT_TYPE_LABELS,
        stores=stores,
        categories=INVOICE_CATEGORIES,
        subfolders=ONEDRIVE_SUBFOLDERS,
        payment_methods=payment_methods,
        linhas=linhas,
        materiais=materiais,
        locais_stock=LOCAIS_STOCK,
        unidades_materiais=UNIDADES_MATERIAIS,
        stock_local_derivado=stock_local_derivado,
        suppliers=suppliers,
        panel_return_url=panel_return_url,
        cont_tickets=cont_tickets,
    )


def _detect_file_mimetype(data, filename: str) -> str:
    if isinstance(data, memoryview):
        data = bytes(data)
    if data.startswith(b'%PDF'):
        return 'application/pdf'
    if len(data) >= 2 and data[:2] == b'\xff\xd8':
        return 'image/jpeg'
    if len(data) >= 8 and data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    ext = (filename or '').lower().rsplit('.', 1)[-1]
    return {'pdf': 'application/pdf', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png'}.get(ext, 'application/octet-stream')


@compras_bp.route('/faturas/<int:invoice_id>/pdf')
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — view/download PDF evidence
def download_pdf(invoice_id: int):
    pdf_data, pdf_filename = get_invoice_pdf(invoice_id)
    if not pdf_data:
        return 'PDF não disponível', 404
    filename = pdf_filename or 'fatura.pdf'
    mimetype = _detect_file_mimetype(pdf_data, filename)
    as_attachment = request.args.get('dl') == '1'
    response = send_file(
        BytesIO(pdf_data),
        mimetype=mimetype,
        as_attachment=as_attachment,
        download_name=filename,
    )
    if not as_attachment:
        response.headers['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


@compras_bp.route('/review-draft/<int:invoice_id>', methods=['GET', 'POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras', 'acesso_financeiro')  # min: acesso_compras/financeiro — review and submit OCR draft
def review_draft(invoice_id):
    inv = get_invoice(invoice_id)
    if not inv or inv['status'] != 'draft':
        flash('Documento não encontrado ou já submetido.', 'warning')
        return redirect(url_for('compras.faturas'))

    if request.method == 'POST':
        errors = []

        supplier_name = request.form.get('supplier_name', '').strip() or None
        if not supplier_name:
            errors.append('O nome do fornecedor é obrigatório.')

        raw_amount = request.form.get('amount_eur', '').strip()
        amount_eur = None
        if not raw_amount:
            errors.append('O valor total é obrigatório.')
        else:
            try:
                amount_eur = float(raw_amount.replace(',', '.'))
                if amount_eur < 0:
                    errors.append('O valor total não pode ser negativo.')
            except ValueError:
                errors.append('Valor total inválido.')

        raw_vat = request.form.get('vat_amount_eur', '').strip()
        vat_amount_eur = None
        if raw_vat:
            try:
                vat_amount_eur = float(raw_vat.replace(',', '.'))
                if vat_amount_eur < 0:
                    errors.append('O valor do IVA não pode ser negativo.')
            except ValueError:
                errors.append('Valor de IVA inválido.')

        raw_issue = request.form.get('issue_date', '').strip()
        raw_due = request.form.get('due_date', '').strip()
        issue_date = None
        due_date = None
        if raw_issue:
            try:
                from datetime import date as _date
                issue_date = _date.fromisoformat(raw_issue)
            except ValueError:
                errors.append('Data de emissão inválida.')
        if raw_due:
            try:
                from datetime import date as _date
                due_date = _date.fromisoformat(raw_due)
            except ValueError:
                errors.append('Data de vencimento inválida.')

        doc_type = request.form.get('document_type', 'fatura')
        if doc_type not in DOCUMENT_TYPE_LABELS:
            doc_type = 'fatura'

        centro_custo_raw = request.form.get('centro_custo_id', '').strip()
        try:
            centro_custo_id = int(centro_custo_raw) if centro_custo_raw else None
        except ValueError:
            centro_custo_id = None
        categoria_custo_raw = request.form.get('categoria_custo_id', '').strip()
        try:
            categoria_custo_id = int(categoria_custo_raw) if categoria_custo_raw else None
        except ValueError:
            categoria_custo_id = None
        notes = request.form.get('notes', '').strip() or None

        ja_paga = request.form.get('ja_paga') == 'on'
        payment_method = request.form.get('payment_method', '').strip() or None
        raw_paid_date = request.form.get('paid_date', '').strip()
        paid_date = None
        if ja_paga:
            if raw_paid_date:
                try:
                    from datetime import date as _date
                    paid_date = _date.fromisoformat(raw_paid_date)
                except ValueError:
                    errors.append('Data de pagamento inválida.')
            else:
                paid_date = date.today()

        if errors:
            for e in errors:
                flash(e, 'warning')
            inv.update({
                'supplier_name': supplier_name,
                'supplier_nif': request.form.get('supplier_nif', '').strip() or None,
                'invoice_number': request.form.get('invoice_number', '').strip() or None,
                'amount_eur': amount_eur,
                'vat_amount_eur': vat_amount_eur,
                'issue_date': issue_date,
                'due_date': due_date,
                'document_type': doc_type,
                'centro_custo_id': centro_custo_id,
                'categoria_custo_id': categoria_custo_id,
                'notes': notes,
            })
            payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]
            cost_centers = get_cost_centers(ativo_only=True)
            cost_categories_tree = get_cost_categories_tree()
            _ocr_raw = inv.get('ocr_raw') or {}
            if isinstance(_ocr_raw, str):
                try:
                    _ocr_raw = json.loads(_ocr_raw)
                except (json.JSONDecodeError, TypeError):
                    _ocr_raw = {}
            _sugg_name_err = supplier_name or inv.get('supplier_name') or ''
            _similar_err = find_similar_suppliers(_sugg_name_err) if _sugg_name_err and not inv.get('supplier_id') else []
            return render_template('compras/review_draft.html',
                                   inv=inv,
                                   document_type_labels=DOCUMENT_TYPE_LABELS,
                                   payment_methods=payment_methods,
                                   cost_centers=cost_centers,
                                   cost_categories_tree=cost_categories_tree,
                                   today=date.today(),
                                   categoria_auto_detected=bool(_ocr_raw.get('auto_categoria_custo')),
                                   similar_suppliers=_similar_err,
                                   supplier_unregistered=(not inv.get('supplier_id') and bool(_sugg_name_err) and len(_similar_err) == 0))

        new_status = 'paid' if ja_paga else 'pending_review'
        _compras_actor = session.get('user', {}).get('username', 'sistema:compras')
        supplier_nif_clean = request.form.get('supplier_nif', '').strip() or None
        supplier_action = request.form.get('supplier_action', '').strip()

        # Resolve supplier_id for invoice-type documents leaving draft state.
        # update_invoice guards against moving to post-draft states without a linked supplier.
        supplier_id = inv.get('supplier_id')
        _supplier_created = False
        # Never use the company's own NIF to resolve a supplier
        from db.faturas import OWN_COMPANY_NIFS, _normalize_nif as _nif_norm
        _nif_for_lookup = supplier_nif_clean if _nif_norm(supplier_nif_clean) not in OWN_COMPANY_NIFS else None
        if doc_type in {'fatura', 'nota_credito', 'nota_debito'} and not supplier_id:
            # 1. Try exact match (by NIF, alias, or name)
            _exact = None
            if _nif_for_lookup:
                _exact = get_supplier_by_nif(_nif_for_lookup)
                if not _exact:
                    _exact = get_supplier_by_alias(supplier_name or '', supplier_nif_clean)
            if not _exact and supplier_name:
                _exact = get_supplier_by_name(supplier_name)
                if not _exact:
                    _exact = get_supplier_by_alias(supplier_name)

            if _exact:
                supplier_id = _exact['id']
            elif supplier_action.startswith('associate:'):
                # User explicitly chose an existing supplier
                _assoc_id = supplier_action[len('associate:'):]
                if _assoc_id.isdigit():
                    supplier_id = int(_assoc_id)
            elif supplier_action == 'create':
                # User explicitly chose to create a new supplier
                supplier_id = upsert_supplier(name=supplier_name, nif=_nif_for_lookup)
                _supplier_created = True
            else:
                # No exact match and no explicit action — save draft data, don't auto-create
                update_invoice(invoice_id, {
                    'supplier_name': supplier_name,
                    'supplier_nif': supplier_nif_clean,
                    'invoice_number': request.form.get('invoice_number', '').strip() or None,
                    'amount_eur': amount_eur,
                    'vat_amount_eur': vat_amount_eur,
                    'issue_date': issue_date.isoformat() if issue_date else None,
                    'due_date': due_date.isoformat() if due_date else None,
                    'document_type': doc_type,
                    'centro_custo_id': centro_custo_id,
                    'categoria_custo_id': categoria_custo_id,
                    'notes': notes,
                }, changed_by=_compras_actor)
                flash('Selecciona um fornecedor existente ou cria um novo antes de registar.', 'warning')
                return redirect(url_for('compras.review_draft', invoice_id=invoice_id))

        update_invoice(invoice_id, {
            'supplier_id': supplier_id,
            'supplier_name': supplier_name,
            'supplier_nif': supplier_nif_clean,
            'invoice_number': request.form.get('invoice_number', '').strip() or None,
            'amount_eur': amount_eur,
            'vat_amount_eur': vat_amount_eur,
            'issue_date': issue_date.isoformat() if issue_date else None,
            'due_date': due_date.isoformat() if due_date else None,
            'document_type': doc_type,
            'status': new_status,
            'paid_date': paid_date.isoformat() if paid_date else None,
            'payment_method': payment_method if ja_paga else None,
            'centro_custo_id': centro_custo_id,
            'categoria_custo_id': categoria_custo_id,
            'notes': notes,
        }, changed_by=_compras_actor)
        if _supplier_created:
            flash(f"Fornecedor '{supplier_name}' criado automaticamente. Verifica em Fornecedores se é duplicado.", 'warning')
        if ja_paga:
            # Overwrite the automatic audit record with the real user's identity
            _username = session.get('user', {}).get('username', 'sistema:compras')
            try:
                mark_payment_executed(
                    invoice_id,
                    paid_date or date.today(),
                    _username,
                    payment_method=payment_method,
                    notes='Registado como pago na revisão do documento',
                )
            except Exception as _pe:
                logger.warning('review_draft: mark_payment_executed failed for inv=%s: %s', invoice_id, _pe)
            flash('Fatura registada e marcada como paga.', 'success')
        else:
            flash('Fatura registada com sucesso.', 'success')
        return redirect(url_for('compras.faturas'))

    cost_centers = get_cost_centers(ativo_only=True)
    cost_categories_tree = get_cost_categories_tree()
    payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]
    _ocr_raw = inv.get('ocr_raw') or {}
    if isinstance(_ocr_raw, str):
        try:
            _ocr_raw = json.loads(_ocr_raw)
        except (json.JSONDecodeError, TypeError):
            _ocr_raw = {}

    # Supplier suggestion logic: only for invoice-type docs without a linked supplier
    _similar_suppliers = []
    _supplier_unregistered = False
    _inv_doc_type = inv.get('document_type', 'fatura')
    if not inv.get('supplier_id') and _inv_doc_type in {'fatura', 'nota_credito', 'nota_debito'}:
        _ocr_name = inv.get('supplier_name') or ''
        if _ocr_name:
            _similar_suppliers = find_similar_suppliers(_ocr_name)
            _supplier_unregistered = len(_similar_suppliers) == 0

    return render_template('compras/review_draft.html',
                           inv=inv,
                           document_type_labels=DOCUMENT_TYPE_LABELS,
                           payment_methods=payment_methods,
                           cost_centers=cost_centers,
                           cost_categories_tree=cost_categories_tree,
                           today=date.today(),
                           categoria_auto_detected=bool(_ocr_raw.get('auto_categoria_custo')),
                           similar_suppliers=_similar_suppliers,
                           supplier_unregistered=_supplier_unregistered)


@compras_bp.route('/faturas/<int:invoice_id>/marcar-paga', methods=['POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — record payment
def marcar_paga(invoice_id):
    inv = get_invoice(invoice_id)
    if not inv or inv['status'] == 'paid':
        flash('Fatura não encontrada ou já marcada como paga.', 'warning')
        return redirect(url_for('compras.faturas'))

    payment_method = request.form.get('payment_method', '').strip() or None
    raw_paid_date = request.form.get('paid_date', '').strip()
    paid_date = None
    if raw_paid_date:
        try:
            paid_date = date.fromisoformat(raw_paid_date)
        except ValueError:
            flash('Data de pagamento inválida.', 'warning')
            return redirect(url_for('compras.faturas'))
    else:
        paid_date = date.today()

    username = session.get('user', {}).get('username', 'sistema:compras')
    try:
        mark_payment_executed(invoice_id, paid_date, username, payment_method=payment_method)
    except Exception as e:
        logger.error('marcar_paga inv=%s: %s', invoice_id, e, exc_info=True)
        flash('Erro ao registar pagamento (ver logs).', 'danger')
        back = request.form.get('_return_url', '').strip()
        return redirect(back if back else url_for('compras.faturas'))
    flash('Pagamento registado com sucesso.', 'success')
    back = request.form.get('_return_url', '').strip()
    return redirect(back if back else url_for('compras.faturas'))


@compras_bp.route('/faturas/<int:invoice_id>/attach-pdf', methods=['POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — attach PDF evidence
def attach_pdf(invoice_id: int):
    back = request.form.get('_return_url', '').strip() or url_for('compras.faturas')
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(back)
    pdf_file = request.files.get('pdf_file')
    if not pdf_file or not pdf_file.filename:
        flash('Nenhum ficheiro seleccionado.', 'warning')
        return redirect(back)
    if _ext(pdf_file.filename) != 'pdf':
        flash('Apenas ficheiros PDF são aceites.', 'warning')
        return redirect(back)
    pdf_data = pdf_file.read()
    if not pdf_data.startswith(b'%PDF'):
        flash('Ficheiro não é um PDF válido.', 'warning')
        return redirect(back)
    save_invoice_pdf(invoice_id, pdf_data, pdf_file.filename or 'fatura.pdf')
    flash('PDF anexado com sucesso.', 'success')
    return redirect(back)


@compras_bp.route('/faturas/<int:invoice_id>/upload-chunk', methods=['POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — chunked PDF upload
def upload_chunk(invoice_id: int):
    import re, shutil
    upload_id = request.form.get('upload_id', '')
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', upload_id):
        return jsonify({'ok': False, 'error': 'upload_id inválido'}), 400
    try:
        chunk_index = int(request.form.get('chunk_index', -1))
        total_chunks = int(request.form.get('total_chunks', 0))
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'Parâmetros inválidos'}), 400
    if chunk_index < 0 or total_chunks < 1 or chunk_index >= total_chunks:
        return jsonify({'ok': False, 'error': 'Índice de chunk inválido'}), 400
    chunk_file = request.files.get('chunk')
    if not chunk_file:
        return jsonify({'ok': False, 'error': 'Dados do chunk em falta'}), 400
    chunk_data = chunk_file.read()
    if len(chunk_data) > 4 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Chunk demasiado grande'}), 400
    chunk_dir = f'/tmp/pdf_upload_{upload_id}'
    os.makedirs(chunk_dir, exist_ok=True)
    chunk_path = os.path.join(chunk_dir, f'chunk_{chunk_index:06d}')
    with open(chunk_path, 'wb') as f:
        f.write(chunk_data)
    return jsonify({'ok': True, 'chunk': chunk_index})


@compras_bp.route('/faturas/<int:invoice_id>/finalize-upload', methods=['POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — finalize chunked upload
def finalize_upload(invoice_id: int):
    import re, glob, shutil
    upload_id = request.form.get('upload_id', '')
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', upload_id):
        return jsonify({'ok': False, 'error': 'upload_id inválido'}), 400
    try:
        total_chunks = int(request.form.get('total_chunks', 0))
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'Parâmetros inválidos'}), 400
    filename = request.form.get('filename', 'fatura.pdf').strip()
    if _ext(filename) != 'pdf':
        filename = 'fatura.pdf'
    back = request.form.get('_return_url', '').strip() or url_for('compras.faturas')
    chunk_dir = f'/tmp/pdf_upload_{upload_id}'
    chunk_files = sorted(glob.glob(os.path.join(chunk_dir, 'chunk_*')))
    if len(chunk_files) != total_chunks:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        return jsonify({'ok': False, 'error': f'Esperados {total_chunks} chunks, recebidos {len(chunk_files)}'}), 400
    pdf_data = b''
    for cf in chunk_files:
        with open(cf, 'rb') as f:
            pdf_data += f.read()
    shutil.rmtree(chunk_dir, ignore_errors=True)
    if not pdf_data.startswith(b'%PDF'):
        return jsonify({'ok': False, 'error': 'Ficheiro não é um PDF válido'}), 400
    if len(pdf_data) > 55 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Ficheiro demasiado grande (máx. 50 MB)'}), 400
    inv = get_invoice(invoice_id)
    if not inv:
        return jsonify({'ok': False, 'error': 'Fatura não encontrada'}), 404
    save_invoice_pdf(invoice_id, pdf_data, filename or 'fatura.pdf')
    flash('PDF anexado com sucesso.', 'success')
    return jsonify({'ok': True, 'redirect': back})


@compras_bp.route('/artigos', methods=['GET', 'POST'])
@perm_required('acesso_administrativo')  # intentionally admin-only — supply-article catalogue config
def artigos():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            fornecedor = request.form.get('fornecedor', '').strip()
            produto = request.form.get('produto', '').strip()
            if fornecedor and produto:
                success = add_artigo_administrativo(fornecedor, produto)
                if success:
                    flash(f'Artigo "{produto}" adicionado!', 'success')
                else:
                    flash('Artigo já existe.', 'warning')
            else:
                flash('Preencha fornecedor e produto.', 'warning')

        elif action == 'toggle':
            artigo_id = int(request.form.get('artigo_id', 0))
            ativo = request.form.get('ativo') == '1'
            toggle_artigo_administrativo(artigo_id, ativo)

        elif action == 'delete':
            artigo_id = int(request.form.get('artigo_id', 0))
            delete_artigo_administrativo(artigo_id)
            flash('Artigo eliminado!', 'success')

        return redirect(url_for('compras.artigos'))

    artigos_list = get_artigos_administrativos(apenas_ativos=False)
    fornecedores = sorted(set(a['fornecedor'] for a in artigos_list))
    return render_template('compras/artigos.html',
                           artigos=artigos_list, fornecedores=fornecedores)


@compras_bp.route('/nova-fatura/upload-chunk', methods=['POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — chunked upload for new invoice
def nova_fatura_upload_chunk():
    import re, shutil
    upload_id = request.form.get('upload_id', '')
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', upload_id):
        return jsonify({'ok': False, 'error': 'upload_id inválido'}), 400
    try:
        chunk_index = int(request.form.get('chunk_index', -1))
        total_chunks = int(request.form.get('total_chunks', 0))
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'Parâmetros inválidos'}), 400
    if chunk_index < 0 or total_chunks < 1 or chunk_index >= total_chunks:
        return jsonify({'ok': False, 'error': 'Índice de chunk inválido'}), 400
    chunk_file = request.files.get('chunk')
    if not chunk_file:
        return jsonify({'ok': False, 'error': 'Dados do chunk em falta'}), 400
    chunk_data = chunk_file.read()
    if len(chunk_data) > 4 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Chunk demasiado grande'}), 400
    chunk_dir = f'/tmp/pdf_upload_{upload_id}'
    os.makedirs(chunk_dir, exist_ok=True)
    chunk_path = os.path.join(chunk_dir, f'chunk_{chunk_index:06d}')
    with open(chunk_path, 'wb') as f:
        f.write(chunk_data)
    return jsonify({'ok': True, 'chunk': chunk_index})


@compras_bp.route('/nova-fatura/finalize-upload', methods=['POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — finalize upload for new invoice
def nova_fatura_finalize_upload():
    import re, glob, shutil
    upload_id = request.form.get('upload_id', '')
    if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', upload_id):
        return jsonify({'ok': False, 'error': 'upload_id inválido'}), 400
    try:
        total_chunks = int(request.form.get('total_chunks', 0))
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'Parâmetros inválidos'}), 400
    filename = request.form.get('filename', 'fatura.pdf').strip()
    if _ext(filename) != 'pdf':
        filename = 'fatura.pdf'
    chunk_dir = f'/tmp/pdf_upload_{upload_id}'
    chunk_files = sorted(glob.glob(os.path.join(chunk_dir, 'chunk_*')))
    if len(chunk_files) != total_chunks:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        return jsonify({'ok': False, 'error': f'Esperados {total_chunks} chunks, recebidos {len(chunk_files)}'}), 400
    pdf_data = b''
    for cf in chunk_files:
        with open(cf, 'rb') as f:
            pdf_data += f.read()
    shutil.rmtree(chunk_dir, ignore_errors=True)
    if not pdf_data.startswith(b'%PDF'):
        return jsonify({'ok': False, 'error': 'Ficheiro não é um PDF válido'}), 400
    if len(pdf_data) > 55 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Ficheiro demasiado grande (máx. 50 MB)'}), 400
    username = _get_username()
    try:
        invoice_id = faturas_svc.create_draft_from_pdf(pdf_data, filename, username, source='email_upload')
        return jsonify({'ok': True, 'redirect': url_for('compras.review_draft', invoice_id=invoice_id)})
    except ServiceError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400


@compras_bp.route('/nova-fatura', methods=['GET', 'POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — register new invoice (manual/OCR/photo)
def nova_fatura():
    if request.method == 'POST':
        channel = request.form.get('channel', 'manual')
        username = _get_username()

        # ── Canal 1: Upload PDF ────────────────────────────────────────────────
        if channel == 'email_upload':
            file = request.files.get('pdf_file')
            if not file or not file.filename:
                flash('Selecciona um ficheiro PDF.', 'warning')
                return redirect(url_for('compras.nova_fatura'))
            ext = _ext(file.filename)
            if ext not in ALLOWED_UPLOAD_EXTENSIONS:
                flash('Tipo não suportado. Usa um ficheiro PDF.', 'warning')
                return redirect(url_for('compras.nova_fatura'))
            file_bytes = file.read()
            try:
                invoice_id = faturas_svc.create_draft_from_pdf(file_bytes, file.filename, username, source='email_upload')
                return redirect(url_for('compras.review_draft', invoice_id=invoice_id))
            except ServiceError as exc:
                flash(str(exc), 'warning')
                return redirect(url_for('compras.nova_fatura'))

        # ── Canal 2: Fotografia ────────────────────────────────────────────────
        if channel == 'photo':
            file = request.files.get('photo_file')
            if not file or not file.filename:
                flash('Selecciona uma fotografia.', 'warning')
                return redirect(url_for('compras.nova_fatura'))
            ext = _ext(file.filename)
            if ext not in ALLOWED_IMAGE_EXTENSIONS:
                flash('Tipo não suportado. Usa JPG, PNG ou HEIC.', 'warning')
                return redirect(url_for('compras.nova_fatura'))
            file_bytes = file.read()
            try:
                invoice_id = faturas_svc.create_draft_from_image(file_bytes, file.filename, username, source='photo')
                return redirect(url_for('compras.review_draft', invoice_id=invoice_id))
            except ServiceError as exc:
                flash(str(exc), 'warning')
                return redirect(url_for('compras.nova_fatura'))

        # ── Canal 3: Entrada Manual ───────────────────────────────────────────
        invoice_number = request.form.get('invoice_number', '').strip() or None
        amount_str = request.form.get('amount_eur', '').replace(',', '.')
        vat_str = request.form.get('vat_amount_eur', '').replace(',', '.') or '0'
        amount_sem_iva_str = request.form.get('amount_sem_iva', '').replace(',', '.') or ''
        issue_date_str = request.form.get('issue_date', '')
        due_date_str = request.form.get('due_date', '')
        payment_method = request.form.get('payment_method', '').strip() or None
        notes_raw = request.form.get('notes', '').strip() or ''
        document_type = request.form.get('document_type', 'fatura')
        centro_custo_raw = request.form.get('centro_custo_id', '').strip()
        centro_custo_id = int(centro_custo_raw) if centro_custo_raw else None
        categoria_custo_raw = request.form.get('categoria_custo_id', '').strip()
        categoria_custo_id = int(categoria_custo_raw) if categoria_custo_raw else None
        from db.faturas import DOCUMENT_TYPE_LABELS as _DTL
        if document_type not in _DTL:
            document_type = 'fatura'

        # ── Resolve supplier (structured selection — no silent auto-creation) ─
        supplier_id_raw = request.form.get('supplier_id', '').strip()
        is_new_supplier = request.form.get('is_new_supplier', '').strip() == '1'
        supplier_id = None
        supplier_name = ''
        supplier_nif = None

        if supplier_id_raw.isdigit():
            # Existing supplier selected from the dropdown
            from db.faturas import get_supplier_by_id as _get_sup_by_id
            _s = _get_sup_by_id(int(supplier_id_raw))
            if _s:
                supplier_id = _s['id']
                supplier_name = _s['name']
                supplier_nif = _s.get('nif') or None
            else:
                flash('Fornecedor não encontrado. Seleciona um fornecedor válido.', 'warning')
                return redirect(url_for('compras.nova_fatura'))
        elif is_new_supplier:
            # User explicitly filled in the "Novo Fornecedor" form
            supplier_name = request.form.get('supplier_name', '').strip()
            supplier_nif = request.form.get('supplier_nif', '').strip() or None
            if not supplier_name:
                flash('Preenche o nome do fornecedor para criar um novo registo.', 'warning')
                return redirect(url_for('compras.nova_fatura'))
            if document_type == 'fatura' and not supplier_nif:
                flash('O NIF é obrigatório para criar um fornecedor em faturas.', 'warning')
                return redirect(url_for('compras.nova_fatura'))
            try:
                supplier_id = upsert_supplier(supplier_name, supplier_nif,
                                              payment_method=payment_method or None)
            except Exception as exc:
                logging.warning('compras.nova_fatura: upsert_supplier failed: %s', exc)
                flash(f'Erro ao criar fornecedor: {exc}', 'warning')
                return redirect(url_for('compras.nova_fatura'))
        elif document_type == 'fatura':
            flash('Seleciona um fornecedor existente ou cria um novo antes de registar a fatura.', 'warning')
            return redirect(url_for('compras.nova_fatura'))
        # For non-invoice document types, a supplier is optional.

        try:
            amount_eur = float(amount_str)
        except (ValueError, TypeError):
            flash('Valor total inválido.', 'warning')
            return redirect(url_for('compras.nova_fatura'))
        try:
            vat_amount_eur = float(vat_str)
        except (ValueError, TypeError):
            vat_amount_eur = 0.0

        issue_date = None
        due_date = None
        try:
            if issue_date_str:
                issue_date = datetime.strptime(issue_date_str, '%Y-%m-%d').date()
            if due_date_str:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
        except ValueError:
            pass

        # Parse optional net amount (informative)
        try:
            amount_sem_iva = float(amount_sem_iva_str) if amount_sem_iva_str else None
        except ValueError:
            amount_sem_iva = None

        # Build notes: record payment method and net amount so they're visible in detail
        notes_parts = []
        if payment_method:
            notes_parts.append(f'Método: {payment_method}')
        if amount_sem_iva is not None:
            notes_parts.append(f'Valor s/IVA: {amount_sem_iva:.2f}€')
        if notes_raw:
            notes_parts.append(notes_raw)
        notes = ' | '.join(notes_parts) or None

        try:
            invoice_id = create_invoice({
                'supplier_id': supplier_id,
                'supplier_name': supplier_name,
                'supplier_nif': supplier_nif,
                'invoice_number': invoice_number,
                'amount_eur': amount_eur,
                'vat_amount_eur': vat_amount_eur,
                'issue_date': issue_date,
                'due_date': due_date,
                'store_id': None,
                'category': None,
                'onedrive_subfolder': None,
                'onedrive_path': None,
                'onedrive_web_url': None,
                'pdf_filename': None,
                'pdf_data': None,
                'status': 'pending_review',
                'ocr_confidence': None,
                'ocr_raw': None,
                'created_by': _get_username(),
                'notes': notes,
                'document_type': document_type,
                'centro_custo_id': centro_custo_id,
                'categoria_custo_id': categoria_custo_id,
            })
        except ValueError as exc:
            flash(str(exc), 'warning')
            return redirect(url_for('compras.nova_fatura'))

        from db.faturas import DOCUMENT_TYPE_LABELS as _DTL2
        _doc_label = _DTL2.get(document_type, 'Documento')
        _entity = f' de {supplier_name}' if supplier_name else ''
        if due_date:
            try:
                suggested_date, is_fallback, _ = suggest_payment_date(
                    invoice_id, amount_eur, due_date=due_date
                )
                propose_invoice_payment(invoice_id, suggested_date, amount_eur)
                if is_fallback:
                    flash(f'{_doc_label}{_entity} registado. Vencimento: {due_date.strftime("%d/%m/%Y")}.', 'warning')
                else:
                    flash(f'{_doc_label}{_entity} registado. Data de pagamento proposta: {suggested_date.strftime("%d/%m/%Y")}.', 'success')
            except Exception as _pay_exc:
                logger.warning('suggest/propose payment failed for invoice %s: %s', invoice_id, _pay_exc)
                flash(f'{_doc_label}{_entity} registado. Não foi possível calcular data de pagamento automaticamente — agenda manualmente na fatura.', 'warning')
        else:
            flash(f'{_doc_label}{_entity} registado com sucesso!', 'success')

        return redirect(url_for('compras.index'))

    suppliers = get_suppliers()
    payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]
    cost_centers = get_cost_centers(ativo_only=True)
    cost_categories_tree = get_cost_categories_tree()
    return render_template('compras/nova_fatura.html',
                           suppliers=suppliers,
                           payment_methods=payment_methods,
                           cost_centers=cost_centers,
                           cost_categories_tree=cost_categories_tree,
                           today=str(date.today()))


@compras_bp.route('/criar-ordem', methods=['GET', 'POST'])
@any_perm_required('acesso_administrativo', 'acesso_compras')  # min: acesso_compras — create stock transfer order
def criar_ordem():
    if request.method == 'POST':
        username = session.get('user', {}).get('username', '')
        loja_destino = 'Bolhão'
        data_prevista_str = request.form.get('data_prevista', '')
        data_prevista = None
        if data_prevista_str:
            try:
                data_prevista = datetime.strptime(data_prevista_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        ordens_count = 0
        idx = 0
        while True:
            produto = request.form.get(f'produto_{idx}')
            if produto is None:
                break
            qty_str = request.form.get(f'qty_{idx}', '')
            unidade = request.form.get(f'unidade_{idx}', 'und')
            idx += 1
            try:
                qty = float(qty_str.replace(',', '.'))
            except (ValueError, TypeError):
                qty = 0
            if qty <= 0:
                continue
            criar_ordem_transferencia(date.today(), 'Compras', produto, qty, unidade, loja_destino, criado_por=username, data_prevista=data_prevista)
            ordens_count += 1
        if ordens_count > 0:
            flash(f'{ordens_count} ordem(ns) de transferência criada(s)!', 'success')
        else:
            flash('Nenhuma ordem criada. Preencha as quantidades.', 'info')
        return redirect(url_for('compras.criar_ordem'))

    artigos_list = get_artigos_administrativos(apenas_ativos=True)
    fornecedores = sorted(set(a['fornecedor'] for a in artigos_list))
    artigos_by_fornecedor = {}
    for a in artigos_list:
        artigos_by_fornecedor.setdefault(a['fornecedor'], []).append(a)
    return render_template('compras/criar_ordem.html',
                           artigos=artigos_list, fornecedores=fornecedores,
                           artigos_by_fornecedor=artigos_by_fornecedor,
                           today=str(date.today()))
