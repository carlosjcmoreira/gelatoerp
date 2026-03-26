import os
import sys
import json
import logging
from datetime import date, datetime
from io import BytesIO

from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, session, send_file, jsonify)
from flask_app.auth import perm_required

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_invoices, get_invoice, get_invoice_pdf, create_invoice, update_invoice,
    delete_invoice, confirm_invoice_payment, mark_payment_executed,
    get_suppliers, get_supplier_by_nif, upsert_supplier, delete_supplier,
    get_stores_list, update_invoice_onedrive, suggest_onedrive_subfolder,
    get_contas_por_fornecedor,
    INVOICE_STATUS_LABELS, ONEDRIVE_SUBFOLDERS, INVOICE_CATEGORIES,
    DOCUMENT_TYPE_LABELS, PAYMENT_METHOD_LABELS, PAYMENT_TERMS_LABELS,
    calculate_due_date,
    get_confirming_contracts, create_confirming_parcela,
    get_payment_methods_config,
)

import flask_app.services.faturas as faturas_svc
from flask_app.services import ServiceError

logger = logging.getLogger(__name__)

faturas_bp = Blueprint('faturas', __name__)

ALLOWED_EXTENSIONS = {'pdf', 'xlsx', 'xls'}
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'heic', 'heif', 'webp'}


def _ext(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


def _parse_date(val: str):
    if not val:
        return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(val.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(val: str):
    if not val:
        return None
    try:
        return float(str(val).replace(',', '.').strip())
    except ValueError:
        return None


# ── Index ──────────────────────────────────────────────────────────────────────

@faturas_bp.route('/')
@perm_required('acesso_gestor')
def index():
    from urllib.parse import urlencode
    view = request.args.get('view', 'documento')
    today = date.today()

    confirming_contracts = get_confirming_contracts()
    payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]

    if view == 'fornecedor':
        grupos = get_contas_por_fornecedor()
        for grupo in grupos:
            for inv in grupo.get('invoices', []):
                if inv['status'] == 'scheduled' and inv.get('due_date') and inv['due_date'] < today:
                    inv['display_status'] = 'overdue'
                    inv['status_label'] = 'Vencida'
                else:
                    inv['display_status'] = inv['status']
        return render_template(
            'financeiro/faturas/index.html',
            view='fornecedor',
            grupos=grupos,
            today=today,
            status_labels=INVOICE_STATUS_LABELS,
            invoices=[],
            confirming_contracts=confirming_contracts,
            payment_methods=payment_methods,
        )

    status_filter = request.args.get('status', '')
    order_by = request.args.get('order_by', 'due_date')
    order_dir = request.args.get('order_dir', 'asc')
    search = request.args.get('q', '').strip()
    store_id = request.args.get('store_id', '')

    invoices = get_invoices(
        status=status_filter or None,
        store_id=int(store_id) if store_id else None,
        search=search or None,
        order_by=order_by,
        order_dir=order_dir,
    )

    for inv in invoices:
        if inv['status'] == 'scheduled' and inv['due_date'] and inv['due_date'] < today:
            inv['display_status'] = 'overdue'
            inv['status_label'] = 'Vencida'
        else:
            inv['display_status'] = inv['status']

    filter_qs = '?' + urlencode({k: v for k, v in {
        'q': search, 'status': status_filter, 'store_id': store_id,
    }.items()})

    stores = get_stores_list()
    return render_template(
        'financeiro/faturas/index.html',
        view='documento',
        invoices=invoices,
        status_filter=status_filter,
        order_by=order_by,
        order_dir=order_dir,
        search=search,
        store_id=store_id,
        stores=stores,
        status_labels=INVOICE_STATUS_LABELS,
        today=today,
        filter_qs=filter_qs,
        confirming_contracts=confirming_contracts,
        payment_methods=payment_methods,
    )


def _safe_return_url(raw: str) -> str:
    """Accept only relative same-origin URLs to prevent open redirects."""
    from urllib.parse import urlparse
    if raw:
        parsed = urlparse(raw)
        if not parsed.scheme and not parsed.netloc:
            return raw
    return url_for('faturas.index')


_BULK_ALLOWED_STATUSES = {'pending_review', 'scheduled', 'paid', 'cancelled'}
_BULK_DATE_REQUIRED = {'scheduled', 'paid'}


@faturas_bp.route('/bulk', methods=['POST'])
@perm_required('acesso_gestor')
def bulk_action():
    ids = request.form.getlist('ids', type=int)
    action = request.form.get('action', '')
    new_status = request.form.get('status', '')
    return_url = _safe_return_url(request.form.get('return_url', ''))

    if not ids:
        flash('Nenhuma fatura selecionada.', 'warning')
        return redirect(return_url)

    if action == 'change_status' and new_status:
        if new_status not in _BULK_ALLOWED_STATUSES:
            flash('Estado inválido.', 'warning')
            return redirect(return_url)

        # Validate and parse bulk_date for states that require it
        bulk_date = _parse_date(request.form.get('bulk_date', ''))
        if new_status in _BULK_DATE_REQUIRED and not bulk_date:
            label = INVOICE_STATUS_LABELS.get(new_status, new_status)
            flash(f'É obrigatório indicar a data ao alterar para «{label}».', 'warning')
            return redirect(return_url)

        # Payment method (optional, for scheduled/paid)
        bulk_payment_method = request.form.get('bulk_payment_method', '').strip() or None
        bulk_confirming_id_raw = request.form.get('bulk_confirming_contract_id', '').strip()
        bulk_confirming_id = int(bulk_confirming_id_raw) if bulk_confirming_id_raw.isdigit() else None

        label = INVOICE_STATUS_LABELS.get(new_status, new_status)
        current_user = session.get('user', {}).get('username', 'system')
        ok = 0
        fail = 0
        for inv_id in ids:
            try:
                if new_status == 'paid':
                    inv_data = get_invoice(inv_id)
                    mark_payment_executed(inv_id, bulk_date, current_user,
                                         payment_method=bulk_payment_method,
                                         confirming_contract_id=bulk_confirming_id)
                    if bulk_payment_method == 'confirming' and bulk_confirming_id and inv_data:
                        try:
                            amt = inv_data.get('amount_eur')
                            create_confirming_parcela(
                                inv_id, bulk_confirming_id, float(amt or 0), bulk_date,
                            )
                        except Exception as pe:
                            logger.warning('create_confirming_parcela paid inv=%s: %s', inv_id, pe)
                elif new_status == 'scheduled':
                    inv_data = get_invoice(inv_id)
                    amt = inv_data.get('amount_eur') if inv_data else None
                    confirm_invoice_payment(inv_id, bulk_date, amt, current_user,
                                           payment_method=bulk_payment_method,
                                           confirming_contract_id=bulk_confirming_id)
                    if bulk_payment_method == 'confirming' and bulk_confirming_id and inv_data:
                        try:
                            create_confirming_parcela(
                                inv_id, bulk_confirming_id,
                                float(amt or 0), bulk_date,
                            )
                        except Exception as pe:
                            logger.warning('create_confirming_parcela inv=%s: %s', inv_id, pe)
                else:
                    update_invoice(inv_id, {'status': new_status})
                ok += 1
            except Exception as e:
                logger.error('bulk change_status id=%s: %s', inv_id, e)
                fail += 1
        msg = f'{ok} fatura(s) alterada(s) para «{label}».'
        if fail:
            msg += f' {fail} falharam (ver logs).'
        flash(msg, 'success' if not fail else 'warning')
    elif action == 'delete':
        ok = 0
        fail = 0
        for inv_id in ids:
            try:
                delete_invoice(inv_id)
                ok += 1
            except Exception as e:
                logger.error('bulk delete id=%s: %s', inv_id, e)
                fail += 1
        msg = f'{ok} fatura(s) eliminada(s).'
        if fail:
            msg += f' {fail} falharam (ver logs).'
        flash(msg, 'success' if not fail else 'warning')
    else:
        flash('Acção inválida.', 'warning')

    return redirect(return_url)


# ── Upload & OCR ───────────────────────────────────────────────────────────────

@faturas_bp.route('/upload', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def upload():
    stores = get_stores_list()
    subfolders = ONEDRIVE_SUBFOLDERS
    categories = INVOICE_CATEGORIES
    suppliers = get_suppliers()

    if request.method == 'POST':
        channel = request.form.get('channel', 'email_upload')

        if channel == 'photo':
            file = request.files.get('photo_file')
            if not file or not file.filename:
                flash('Selecciona uma fotografia.', 'warning')
                return redirect(url_for('faturas.upload'))
            ext = _ext(file.filename)
            if ext not in ALLOWED_IMAGE_EXTENSIONS:
                flash('Tipo de ficheiro não suportado. Usa uma imagem (JPG, PNG, HEIC, etc.).', 'warning')
                return redirect(url_for('faturas.upload'))
            file_bytes = file.read()
            filename = file.filename
            username = session.get('user', {}).get('username', '')
            try:
                invoice_id = faturas_svc.create_draft_from_image(file_bytes, filename, username, source='photo')
            except ServiceError as e:
                flash(str(e), 'warning')
                return redirect(url_for('faturas.upload'))
            return redirect(url_for('faturas.review', invoice_id=invoice_id))

        # Default: email_upload channel
        file = request.files.get('pdf_file')
        if not file or not file.filename:
            flash('Selecciona um ficheiro PDF.', 'warning')
            return redirect(url_for('faturas.upload'))

        ext = _ext(file.filename)
        if ext not in ALLOWED_EXTENSIONS:
            flash('Tipo de ficheiro não suportado. Usa PDF ou Excel.', 'warning')
            return redirect(url_for('faturas.upload'))

        # Excel import path
        if ext in ('xlsx', 'xls'):
            return _handle_excel_import(file, ext)

        # PDF path: run OCR, create draft
        pdf_bytes = file.read()
        pdf_filename = file.filename
        username = session.get('user', {}).get('username', '')
        try:
            invoice_id = faturas_svc.create_draft_from_pdf(pdf_bytes, pdf_filename, username, source='email_upload')
        except ServiceError as e:
            flash(str(e), 'warning')
            return redirect(url_for('faturas.upload'))
        return redirect(url_for('faturas.review', invoice_id=invoice_id))

    return render_template(
        'financeiro/faturas/upload.html',
        stores=stores,
    )


# ── Review draft (GET) ─────────────────────────────────────────────────────────

@faturas_bp.route('/review/<int:invoice_id>')
@perm_required('acesso_gestor')
def review(invoice_id):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'danger')
        return redirect(url_for('faturas.upload'))

    stores = get_stores_list()

    # Rebuild ocr-like dict from stored data for the template
    ocr_raw = inv.get('ocr_raw') or {}
    if isinstance(ocr_raw, str):
        try:
            ocr_raw = json.loads(ocr_raw)
        except (json.JSONDecodeError, TypeError):
            ocr_raw = {}

    ocr = {
        'supplier_name': inv.get('supplier_name'),
        'supplier_nif': inv.get('supplier_nif'),
        'invoice_number': inv.get('invoice_number'),
        'amount_eur': inv.get('amount_eur'),
        'vat_amount_eur': inv.get('vat_amount_eur'),
        'issue_date': inv.get('issue_date').isoformat() if inv.get('issue_date') else None,
        'due_date': inv.get('due_date').isoformat() if inv.get('due_date') else None,
        'ocr_confidence': inv.get('ocr_confidence'),
        'confidence': ocr_raw,
        'error': None,
    }

    supplier = None
    if inv.get('supplier_nif'):
        supplier = get_supplier_by_nif(inv['supplier_nif'])

    return render_template(
        'financeiro/faturas/review.html',
        inv=inv,
        ocr=ocr,
        supplier=supplier,
        pdf_filename=inv.get('pdf_filename', 'fatura.pdf'),
        stores=stores,
        subfolders=ONEDRIVE_SUBFOLDERS,
        categories=INVOICE_CATEGORIES,
        suppliers=get_suppliers(),
        suggested_subfolder=inv.get('onedrive_subfolder'),
        document_type_labels=DOCUMENT_TYPE_LABELS,
        payment_method_labels=PAYMENT_METHOD_LABELS,
        payment_terms_labels=PAYMENT_TERMS_LABELS,
    )


# ── Save after review ──────────────────────────────────────────────────────────

@faturas_bp.route('/save', methods=['POST'])
@perm_required('acesso_gestor')
def save():
    invoice_id = request.form.get('invoice_id', '').strip()
    if not invoice_id:
        flash('ID de fatura em falta. Tenta de novo.', 'danger')
        return redirect(url_for('faturas.upload'))
    invoice_id = int(invoice_id)

    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'danger')
        return redirect(url_for('faturas.upload'))

    form_data = {
        'supplier_name': request.form.get('supplier_name', ''),
        'supplier_nif': request.form.get('supplier_nif', ''),
        'invoice_number': request.form.get('invoice_number', ''),
        'amount_eur': request.form.get('amount_eur', ''),
        'vat_amount_eur': request.form.get('vat_amount_eur', ''),
        'issue_date': request.form.get('issue_date', ''),
        'due_date': request.form.get('due_date', ''),
        'store_id': request.form.get('store_id', ''),
        'category': request.form.get('category', ''),
        'onedrive_subfolder': request.form.get('onedrive_subfolder', ''),
        'notes': request.form.get('notes', ''),
        'document_type': request.form.get('document_type', 'fatura'),
        'supplier_payment_method': request.form.get('supplier_payment_method', ''),
        'supplier_payment_terms': request.form.get('supplier_payment_terms', ''),
        'supplier_iban': request.form.get('supplier_iban', ''),
        'is_new_supplier': request.form.get('is_new_supplier', ''),
    }
    try:
        result = faturas_svc.save_reviewed_invoice(invoice_id, form_data)
    except ServiceError as e:
        flash(str(e), 'danger')
        return redirect(url_for('faturas.review', invoice_id=invoice_id))

    if result.get('warning'):
        flash(f'Fatura guardada. Aviso OneDrive: {result["warning"]}', 'warning')
    else:
        flash('Fatura guardada com sucesso!', 'success')

    return redirect(url_for('faturas.detail', invoice_id=invoice_id))


# ── Cancel draft ───────────────────────────────────────────────────────────────

@faturas_bp.route('/cancel_draft/<int:invoice_id>')
@perm_required('acesso_gestor')
def cancel_draft(invoice_id):
    inv = get_invoice(invoice_id)
    if inv and inv.get('status') == 'draft':
        delete_invoice(invoice_id)
    return redirect(url_for('faturas.upload'))


# ── Detail ─────────────────────────────────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>')
@perm_required('acesso_gestor')
def detail(invoice_id: int):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))
    stores = get_stores_list()
    subfolders = ONEDRIVE_SUBFOLDERS
    categories = INVOICE_CATEGORIES
    suppliers = get_suppliers()
    today = date.today()
    payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]
    confirming_contracts = get_confirming_contracts()
    return render_template(
        'financeiro/faturas/detail.html',
        inv=inv,
        stores=stores,
        subfolders=subfolders,
        categories=categories,
        suppliers=suppliers,
        status_labels=INVOICE_STATUS_LABELS,
        document_type_labels=DOCUMENT_TYPE_LABELS,
        today=today,
        payment_methods=payment_methods,
        confirming_contracts=confirming_contracts,
    )


# ── Edit ───────────────────────────────────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>/edit', methods=['POST'])
@perm_required('acesso_gestor')
def edit(invoice_id: int):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))

    supplier_name = request.form.get('supplier_name', '').strip()
    supplier_nif = ''.join(c for c in request.form.get('supplier_nif', '') if c.isdigit())
    invoice_number = request.form.get('invoice_number', '').strip()
    amount_eur = _parse_float(request.form.get('amount_eur', ''))
    vat_amount_eur = _parse_float(request.form.get('vat_amount_eur', ''))
    issue_date = _parse_date(request.form.get('issue_date', ''))
    due_date = _parse_date(request.form.get('due_date', ''))
    store_id = request.form.get('store_id', '') or None
    if store_id:
        store_id = int(store_id)
    category = request.form.get('category', '').strip()
    onedrive_subfolder = request.form.get('onedrive_subfolder', '').strip()
    notes = request.form.get('notes', '').strip()
    status = request.form.get('status', inv['status'])
    document_type = request.form.get('document_type', inv.get('document_type', 'fatura'))
    if document_type not in DOCUMENT_TYPE_LABELS:
        document_type = 'fatura'

    supplier_id = inv.get('supplier_id')
    if supplier_nif and supplier_name:
        supplier_id = upsert_supplier(
            name=supplier_name,
            nif=supplier_nif,
            category=category or None,
            store_id=store_id,
        )

    update_invoice(invoice_id, {
        'supplier_id': supplier_id,
        'supplier_name': supplier_name or None,
        'supplier_nif': supplier_nif or None,
        'invoice_number': invoice_number or None,
        'amount_eur': amount_eur,
        'vat_amount_eur': vat_amount_eur,
        'issue_date': issue_date,
        'due_date': due_date,
        'store_id': store_id,
        'category': category or None,
        'onedrive_subfolder': onedrive_subfolder or None,
        'status': status,
        'notes': notes or None,
        'document_type': document_type,
    })

    flash('Fatura actualizada.', 'success')
    return redirect(url_for('faturas.detail', invoice_id=invoice_id))


# ── Quick actions ──────────────────────────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>/confirmar', methods=['POST'])
@perm_required('acesso_gestor')
def confirmar(invoice_id: int):
    confirmed_date = _parse_date(request.form.get('confirmed_date', ''))
    if not confirmed_date:
        flash('É obrigatório indicar a data de confirmação.', 'warning')
        return redirect(url_for('faturas.detail', invoice_id=invoice_id))
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))
    if inv.get('status') not in ('pending_review', 'scheduled', 'overdue'):
        flash('Estado da fatura não permite confirmação de data.', 'warning')
        return redirect(url_for('faturas.detail', invoice_id=invoice_id))
    amount_eur = inv.get('amount_eur')
    current_user = session.get('user', {}).get('username', 'system')
    payment_method = request.form.get('payment_method', '').strip() or None
    confirming_id_raw = request.form.get('confirming_contract_id', '').strip()
    confirming_id = int(confirming_id_raw) if confirming_id_raw.isdigit() else None
    confirm_invoice_payment(invoice_id, confirmed_date, amount_eur, current_user,
                            payment_method=payment_method,
                            confirming_contract_id=confirming_id)
    if payment_method == 'confirming' and confirming_id:
        try:
            create_confirming_parcela(invoice_id, confirming_id, float(amount_eur or 0), confirmed_date)
        except Exception as e:
            logger.warning('create_confirming_parcela invoice=%s: %s', invoice_id, e)
    flash('Data confirmada. Fatura agendada.', 'success')
    return redirect(url_for('faturas.detail', invoice_id=invoice_id))


@faturas_bp.route('/<int:invoice_id>/pagar', methods=['POST'])
@perm_required('acesso_gestor')
def pagar(invoice_id: int):
    paid_date = _parse_date(request.form.get('paid_date', ''))
    if not paid_date:
        flash('É obrigatório indicar a data de pagamento.', 'warning')
        return redirect(url_for('faturas.detail', invoice_id=invoice_id))
    current_user = session.get('user', {}).get('username', 'system')
    payment_method = request.form.get('payment_method', '').strip() or None
    confirming_id_raw = request.form.get('confirming_contract_id', '').strip()
    confirming_id = int(confirming_id_raw) if confirming_id_raw.isdigit() else None
    mark_payment_executed(invoice_id, paid_date, current_user,
                          payment_method=payment_method,
                          confirming_contract_id=confirming_id)
    if payment_method == 'confirming' and confirming_id:
        inv = get_invoice(invoice_id)
        if inv:
            amount_eur = inv.get('amount_eur')
            try:
                create_confirming_parcela(invoice_id, confirming_id, float(amount_eur or 0), paid_date)
            except Exception as e:
                logger.warning('create_confirming_parcela pagar invoice=%s: %s', invoice_id, e)
    flash('Fatura marcada como paga.', 'success')
    return redirect(url_for('faturas.detail', invoice_id=invoice_id))


@faturas_bp.route('/<int:invoice_id>/arquivar-onedrive', methods=['POST'])
@perm_required('acesso_gestor')
def arquivar_onedrive(invoice_id: int):
    inv = get_invoice(invoice_id)
    if not inv:
        flash('Fatura não encontrada.', 'warning')
        return redirect(url_for('faturas.index'))

    pdf_data, pdf_filename = get_invoice_pdf(invoice_id)
    if not pdf_data:
        flash('PDF não disponível para arquivo.', 'warning')
        return redirect(url_for('faturas.detail', invoice_id=invoice_id))

    subfolder = request.form.get('onedrive_subfolder') or inv.get('onedrive_subfolder') or 'Gestão'
    from flask_app.onedrive_archive import upload_invoice_pdf
    result = upload_invoice_pdf(pdf_data, pdf_filename or 'fatura.pdf', subfolder, inv.get('issue_date'))

    if result.get('onedrive_path'):
        update_invoice_onedrive(invoice_id, result['onedrive_path'], subfolder,
                                onedrive_web_url=result.get('web_url'))
        flash(f'PDF arquivado no OneDrive: {result["onedrive_path"]}', 'success')
    else:
        flash(f'Erro: {result.get("warning", "Falha ao arquivar.")}', 'warning')

    return redirect(url_for('faturas.detail', invoice_id=invoice_id))


@faturas_bp.route('/<int:invoice_id>/eliminar', methods=['POST'])
@perm_required('acesso_gestor')
def eliminar(invoice_id: int):
    delete_invoice(invoice_id)
    flash('Fatura eliminada.', 'success')
    return redirect(url_for('faturas.index'))


# ── PDF download ───────────────────────────────────────────────────────────────

@faturas_bp.route('/<int:invoice_id>/pdf')
@perm_required('acesso_gestor')
def download_pdf(invoice_id: int):
    pdf_data, pdf_filename = get_invoice_pdf(invoice_id)
    if not pdf_data:
        flash('PDF não disponível.', 'warning')
        return redirect(url_for('faturas.detail', invoice_id=invoice_id))
    return send_file(
        BytesIO(pdf_data),
        mimetype='application/pdf',
        as_attachment=False,
        download_name=pdf_filename or 'fatura.pdf',
    )


# ── Suppliers management ───────────────────────────────────────────────────────

@faturas_bp.route('/fornecedores', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def fornecedores():
    stores = get_stores_list()
    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'save':
            name = request.form.get('name', '').strip()
            nif = ''.join(c for c in request.form.get('nif', '') if c.isdigit())
            category = request.form.get('category', '').strip()
            store_id = request.form.get('store_id', '') or None
            if store_id:
                store_id = int(store_id)
            notes = request.form.get('notes', '').strip()
            payment_method = request.form.get('payment_method', '').strip() or None
            payment_terms = request.form.get('payment_terms', '').strip() or None
            iban = request.form.get('iban', '').strip() or None
            if not name or not nif:
                flash('Nome e NIF são obrigatórios.', 'warning')
            else:
                upsert_supplier(name=name, nif=nif, category=category or None,
                                store_id=store_id, notes=notes or None,
                                payment_method=payment_method,
                                payment_terms=payment_terms, iban=iban)
                flash(f'Fornecedor "{name}" guardado.', 'success')
            return redirect(url_for('faturas.fornecedores'))

        elif action == 'delete':
            supplier_id = int(request.form.get('supplier_id', 0))
            ok = delete_supplier(supplier_id)
            if ok:
                flash('Fornecedor eliminado.', 'success')
            else:
                flash('Não é possível eliminar: fornecedor tem faturas associadas.', 'warning')
            return redirect(url_for('faturas.fornecedores'))

    suppliers = get_suppliers()
    categories = INVOICE_CATEGORIES
    return render_template(
        'financeiro/faturas/fornecedores.html',
        suppliers=suppliers,
        categories=categories,
        stores=stores,
        payment_method_labels=PAYMENT_METHOD_LABELS,
        payment_terms_labels=PAYMENT_TERMS_LABELS,
    )


# ── Excel import ───────────────────────────────────────────────────────────────

def _handle_excel_import(file, ext='xlsx'):
    import openpyxl
    from io import BytesIO

    username = session.get('user', {}).get('username', '')
    file_bytes = file.read()
    wb_closeable = None

    try:
        if ext == 'xls':
            # xlrd (via pandas) supports legacy .xls; convert to uniform row tuples
            import pandas as pd
            df = pd.read_excel(BytesIO(file_bytes), dtype=object, keep_default_na=True)
            raw_headers = [str(c).strip().lower() for c in df.columns]

            def _pd_val(v):
                if v is None:
                    return None
                try:
                    if pd.isna(v):
                        return None
                except (TypeError, ValueError):
                    pass
                return v

            headers = raw_headers
            rows_list = [tuple(_pd_val(v) for v in row) for _, row in df.iterrows()]
            rows_iter = iter(rows_list)
        else:
            wb = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
            wb_closeable = wb
            ws = wb.active
            _rows = ws.iter_rows(values_only=True)
            try:
                header_row = next(_rows)
            except StopIteration:
                wb.close()
                flash('Ficheiro Excel vazio.', 'warning')
                return redirect(url_for('faturas.upload'))
            headers = [str(c or '').strip().lower() for c in header_row]
            rows_iter = _rows
    except Exception as e:
        flash(f'Erro ao ler Excel: {e}', 'warning')
        return redirect(url_for('faturas.upload'))

    # Map Excel column name → internal field key.
    # When the same internal key appears more than once (e.g. empresa + fornecedor),
    # the first column found wins.
    COL_MAP = {
        'fornecedor': 'supplier_name',
        'supplier_name': 'supplier_name',
        'empresa': 'supplier_name',
        'nome comercial': 'supplier_name_alt',   # fallback if supplier_name empty
        'nif': 'supplier_nif',
        'supplier_nif': 'supplier_nif',
        'fatura': 'invoice_number',
        'invoice_number': 'invoice_number',
        'número': 'invoice_number',
        'numero': 'invoice_number',
        'valor': 'amount_eur',
        'valor real': 'amount_eur',
        'amount_eur': 'amount_eur',
        'total': 'amount_eur',
        'iva': 'vat_amount_eur',
        'vat_amount_eur': 'vat_amount_eur',
        'data emissão': 'issue_date',
        'data emissao': 'issue_date',
        'issue_date': 'issue_date',
        'data': 'issue_date',
        'data vencimento': 'due_date',
        'vencimento': 'due_date',
        'due_date': 'due_date',
        'categoria': 'category',
        'category': 'category',
        'subpasta': 'onedrive_subfolder',
        'onedrive_subfolder': 'onedrive_subfolder',
        'estado': 'status',
        'status': 'status',
        'notas': 'notes',
        'notes': 'notes',
        'descrição': 'notes',
        'descricao': 'notes',
        'centro custo': 'centro_custo',
    }

    # Build column-index lookup: internal_key → column index (first match wins)
    col_idx = {}
    for i, h in enumerate(headers):
        key = COL_MAP.get(h)
        if key and key not in col_idx:
            col_idx[key] = i

    # Portuguese → internal status names
    STATUS_PT = {
        'pago': 'paid',
        'pendente': 'pending_review',
        'pendente revisão': 'pending_review',
        'pendente revisao': 'pending_review',
        'agendado': 'scheduled',
        'agendada': 'scheduled',
        'cancelado': 'cancelled',
        'cancelada': 'cancelled',
    }

    def _get(row, key):
        i = col_idx.get(key)
        return row[i] if i is not None and i < len(row) else None

    def _cell_str(v):
        if v is None:
            return ''
        if isinstance(v, float) and v == int(v):
            return str(int(v))
        return str(v).strip()

    def _cell_date(v):
        if v is None:
            return None
        if hasattr(v, 'date'):
            return v.date()
        return _parse_date(_cell_str(v))

    imported = 0
    errors = []
    consecutive_empty = 0

    for idx, row in enumerate(rows_iter, start=2):
        # Early-stop: break after 5 consecutive rows where all mapped columns are empty.
        # A single gap row (all-None) is skipped; 5 in a row means we've reached the
        # end of actual data (avoids iterating up to 1 048 560 trailing empty rows).
        mapped_vals = [row[i] for i in col_idx.values() if i < len(row)]
        if not any(v is not None for v in mapped_vals):
            consecutive_empty += 1
            if consecutive_empty >= 5:
                break
            continue
        consecutive_empty = 0

        try:
            supplier_name = _cell_str(_get(row, 'supplier_name'))
            if not supplier_name:
                supplier_name = _cell_str(_get(row, 'supplier_name_alt'))
            supplier_nif_raw = _cell_str(_get(row, 'supplier_nif'))
            supplier_nif = ''.join(c for c in supplier_nif_raw if c.isdigit())

            if not supplier_name:
                continue

            # Amount: determine document_type before taking abs value
            # Detect nota_credito: invoice_number contains "NC" OR raw amount is positive
            amount_raw = _get(row, 'amount_eur')
            amount_eur_raw = _parse_float(_cell_str(amount_raw))
            invoice_number_raw = _cell_str(_get(row, 'invoice_number'))
            nc_by_number = 'nc' in invoice_number_raw.lower()
            nc_by_amount = (amount_eur_raw is not None and amount_eur_raw > 0)
            document_type = 'nota_credito' if (nc_by_number or nc_by_amount) else 'fatura'
            amount_eur = abs(amount_eur_raw) if amount_eur_raw is not None else None

            vat_amount_eur = _parse_float(_cell_str(_get(row, 'vat_amount_eur')))

            issue_date = _cell_date(_get(row, 'issue_date'))
            due_date = _cell_date(_get(row, 'due_date'))

            # Status: try Portuguese map first, then direct match, else default
            status_raw = _cell_str(_get(row, 'status')).lower()
            status = STATUS_PT.get(status_raw, status_raw)
            if status not in INVOICE_STATUS_LABELS:
                status = 'pending_review'

            # Notes: combine notes column + Centro Custo (if present)
            notes_parts = []
            notes_val = _cell_str(_get(row, 'notes'))
            if notes_val:
                notes_parts.append(notes_val)
            centro = _cell_str(_get(row, 'centro_custo'))
            if centro:
                notes_parts.append(f'Centro Custo: {centro}')
            notes = ' | '.join(notes_parts) or None

            supplier_id = None
            if supplier_nif and supplier_name:
                supplier_id = upsert_supplier(name=supplier_name, nif=supplier_nif)

            invoice_number = invoice_number_raw or None

            inv_data = {
                'supplier_id': supplier_id,
                'supplier_name': supplier_name or None,
                'supplier_nif': supplier_nif or None,
                'invoice_number': invoice_number,
                'amount_eur': amount_eur,
                'vat_amount_eur': vat_amount_eur,
                'issue_date': issue_date,
                'due_date': due_date,
                'store_id': None,
                'category': _cell_str(_get(row, 'category')) or None,
                'onedrive_subfolder': _cell_str(_get(row, 'onedrive_subfolder')) or None,
                'onedrive_path': None,
                'onedrive_web_url': None,
                'pdf_filename': None,
                'pdf_data': None,
                'status': status,
                'ocr_confidence': None,
                'ocr_raw': None,
                'created_by': username,
                'notes': notes,
                'document_type': document_type,
            }
            create_invoice(inv_data)
            imported += 1
        except Exception as e:
            errors.append(f'Linha {idx}: {e}')

    if wb_closeable is not None:
        wb_closeable.close()

    if errors:
        flash(f'{imported} faturas importadas. Erros: {"; ".join(errors[:5])}', 'warning')
    else:
        flash(f'{imported} faturas importadas com sucesso!', 'success')
    return redirect(url_for('faturas.index'))
