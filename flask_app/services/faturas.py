"""
Faturas Service — orchestrates OCR, draft creation, supplier upsert, and OneDrive archiving.

Routes call these functions after parsing the HTTP request; the service handles
all DB and external-API coordination, returning data or raising ServiceError.
"""
import json
import logging
from datetime import date

logger = logging.getLogger(__name__)

# ── Government document → cost category mapping ───────────────────────────────
# Each entry: (list of lowercase supplier-name substrings, category name fragment to search)
# The fragment is matched case-insensitively against cost_categories.name.
_GOVT_CATEGORY_MAP = [
    (
        ['segurança social', 'seguranca social', 'igfss', 'seg. social', 'seg social',
         'seguridade social', 'instituto gestão financeira', 'dgss'],
        'pessoal',
    ),
    (
        ['autoridade tributária', 'autoridade tributaria', 'at -', 'at–', 'at —',
         'finanças', 'financas', 'dgci', 'irs', 'irc', 'imposto rendimento',
         'retenções na fonte', 'retencoes na fonte', 'dmr', 'imposto do selo'],
        'imposto',
    ),
]


def _infer_categoria_custo_id(ocr: dict):
    """Return a cost_categories.id for a government document, or None.

    Only triggers when ``document_type_hint == 'nota_pagamento_imposto'``.
    Searches ``supplier_name`` for known keywords and then looks up the first
    active cost category whose name contains the corresponding fragment.
    """
    if ocr.get('document_type_hint') != 'nota_pagamento_imposto':
        return None

    supplier_name = (ocr.get('supplier_name') or '').lower()

    fragment = None
    for keywords, frag in _GOVT_CATEGORY_MAP:
        if any(kw in supplier_name for kw in keywords):
            fragment = frag
            break

    if not fragment:
        return None

    try:
        from database import get_cost_categories
        cats = get_cost_categories(ativo_only=True)
        for cat in cats:
            if fragment in (cat.get('name') or '').lower():
                return cat['id']
    except Exception as exc:
        logger.warning('Could not infer categoria_custo_id: %s', exc)

    return None


# ── OCR + draft creation ──────────────────────────────────────────────────────

def _build_draft_data(ocr: dict, supplier, file_bytes: bytes, filename: str,
                      username: str, source: str = 'email_upload') -> dict:
    """Build draft invoice data dict from OCR results and optional supplier lookup."""
    from database import suggest_onedrive_subfolder, calculate_due_date

    suggested_subfolder = suggest_onedrive_subfolder(
        supplier_nif=ocr.get('supplier_nif'),
        store_id=supplier.get('store_id') if supplier else None,
    )

    # Calculate due_date from supplier payment_terms if supplier exists; else use OCR value
    if supplier and supplier.get('payment_terms'):
        issue_date = ocr.get('issue_date')
        due_date = calculate_due_date(issue_date, supplier['payment_terms'])
        if due_date:
            due_date = due_date.isoformat() if hasattr(due_date, 'isoformat') else due_date
        else:
            due_date = ocr.get('due_date')
    else:
        due_date = ocr.get('due_date')

    # Store supplier IBAN hint from OCR if provided
    ocr_raw = {}
    if ocr.get('confidence'):
        ocr_raw = ocr['confidence']
    if ocr.get('payment_method_hint'):
        ocr_raw['payment_method_hint'] = ocr['payment_method_hint']
    if ocr.get('payment_terms_hint'):
        ocr_raw['payment_terms_hint'] = ocr['payment_terms_hint']
    if ocr.get('supplier_iban'):
        ocr_raw['supplier_iban'] = ocr['supplier_iban']
    if ocr.get('document_type_hint'):
        ocr_raw['document_type_hint'] = ocr['document_type_hint']

    from db.faturas import DOCUMENT_TYPE_LABELS as _DTL
    raw_hint = ocr.get('document_type_hint', 'fatura')
    document_type = raw_hint if raw_hint in _DTL else 'fatura'
    if raw_hint and raw_hint != document_type:
        logger.warning("OCR returned unknown document_type_hint %r — defaulting to 'fatura'", raw_hint)

    inferred_categoria_custo_id = _infer_categoria_custo_id(ocr)
    if inferred_categoria_custo_id is not None:
        ocr_raw['auto_categoria_custo'] = True

    return {
        'supplier_id': supplier['id'] if supplier else None,
        'supplier_name': (supplier['name'] if supplier else None) or ocr.get('supplier_name'),
        'supplier_nif': (supplier['nif'] if supplier else None) or ocr.get('supplier_nif'),
        'invoice_number': ocr.get('invoice_number'),
        'amount_eur': ocr.get('amount_eur'),
        'vat_amount_eur': ocr.get('vat_amount_eur'),
        'issue_date': ocr.get('issue_date'),
        'due_date': due_date,
        'store_id': supplier.get('store_id') if supplier else None,
        'category': ocr.get('category'),
        'onedrive_subfolder': suggested_subfolder,
        'onedrive_path': None,
        'onedrive_web_url': None,
        'pdf_filename': filename,
        'pdf_data': file_bytes,
        'status': 'draft',
        'ocr_confidence': ocr.get('ocr_confidence'),
        'ocr_raw': json.dumps(ocr_raw) if ocr_raw else None,
        'created_by': username,
        'notes': None,
        'document_type': document_type,
        'source': source,
        'categoria_custo_id': inferred_categoria_custo_id,
    }


def create_draft_from_pdf(pdf_bytes: bytes, pdf_filename: str, username: str,
                          source: str = 'email_upload') -> int:
    """
    Run OCR on ``pdf_bytes``, look up the supplier by NIF, suggest an OneDrive
    subfolder, and persist a draft invoice.

    Returns the new ``invoice_id``.
    Raises ``ServiceError`` on any failure.
    """
    from flask_app.services import ServiceError
    from flask_app.ocr_invoice import extract_invoice_fields
    from database import get_supplier_by_nif, create_invoice

    try:
        ocr = extract_invoice_fields(pdf_bytes, pdf_filename)
    except Exception as exc:
        raise ServiceError(f'Erro ao processar PDF com OCR: {exc}') from exc

    supplier = None
    if ocr.get('supplier_nif'):
        try:
            supplier = get_supplier_by_nif(ocr['supplier_nif'])
        except Exception:
            pass

    draft_data = _build_draft_data(ocr, supplier, pdf_bytes, pdf_filename, username, source)

    try:
        invoice_id = create_invoice(draft_data)
    except Exception as exc:
        raise ServiceError(f'Erro ao guardar rascunho: {exc}') from exc

    return invoice_id


def create_draft_from_image(image_bytes: bytes, filename: str, username: str,
                             source: str = 'photo') -> int:
    """
    Run OCR on an image (photo of delivery note / invoice), look up the supplier,
    and persist a draft invoice.

    Returns the new ``invoice_id``.
    Raises ``ServiceError`` on any failure.
    """
    from flask_app.services import ServiceError
    from flask_app.ocr_invoice import extract_invoice_fields_from_image
    from database import get_supplier_by_nif, create_invoice

    try:
        ocr = extract_invoice_fields_from_image(image_bytes, filename)
    except Exception as exc:
        raise ServiceError(f'Erro ao processar imagem com OCR: {exc}') from exc

    supplier = None
    if ocr.get('supplier_nif'):
        try:
            supplier = get_supplier_by_nif(ocr['supplier_nif'])
        except Exception:
            pass

    draft_data = _build_draft_data(ocr, supplier, image_bytes, filename, username, source)

    try:
        invoice_id = create_invoice(draft_data)
    except Exception as exc:
        raise ServiceError(f'Erro ao guardar rascunho: {exc}') from exc

    return invoice_id


# ── Save reviewed invoice ─────────────────────────────────────────────────────

def _parse_date(val: str):
    if not val:
        return None
    from datetime import datetime
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


def save_reviewed_invoice(invoice_id: int, form: dict, changed_by: str = 'sistema') -> dict:
    """
    Apply reviewed form data to an existing draft invoice, upsert the supplier,
    and optionally archive to OneDrive.

    ``form`` is a plain dict with the same keys as the HTML form fields.

    Returns a dict:
        ``{'warning': str | None}``
    The caller should flash success (or warning if ``warning`` is set).
    Raises ``ServiceError`` on hard failures.
    """
    from flask_app.services import ServiceError
    from database import (
        get_invoice, get_invoice_pdf,
        upsert_supplier, update_invoice, update_invoice_onedrive,
    )

    inv = get_invoice(invoice_id)
    if not inv:
        raise ServiceError('Fatura não encontrada.')

    # Capture the OCR-extracted supplier name before any updates so we can use it
    # to bulk-link other invoices that share the same OCR text when an existing
    # supplier is selected from the dropdown.
    _ocr_supplier_name = (inv.get('supplier_name') or '').strip() or None

    supplier_name = form.get('supplier_name', '').strip()
    supplier_nif = ''.join(c for c in form.get('supplier_nif', '') if c.isdigit())
    invoice_number = form.get('invoice_number', '').strip()
    amount_eur = _parse_float(form.get('amount_eur', ''))
    vat_amount_eur = _parse_float(form.get('vat_amount_eur', ''))
    issue_date = _parse_date(form.get('issue_date', ''))
    due_date = _parse_date(form.get('due_date', ''))
    store_id_raw = form.get('store_id', '') or None
    store_id = int(store_id_raw) if store_id_raw else None
    category = form.get('category', '').strip()
    onedrive_subfolder = form.get('onedrive_subfolder', '').strip()
    notes = form.get('notes', '').strip()
    document_type = form.get('document_type', 'fatura')
    from db.faturas import DOCUMENT_TYPE_LABELS as _DTL
    if document_type not in _DTL:
        document_type = 'fatura'
    centro_custo_raw = form.get('centro_custo_id', '').strip()
    centro_custo_id = int(centro_custo_raw) if centro_custo_raw else None
    categoria_custo_raw = form.get('categoria_custo_id', '').strip()
    categoria_custo_id = int(categoria_custo_raw) if categoria_custo_raw else None

    supplier_payment_method = form.get('supplier_payment_method', '').strip() or None
    supplier_payment_terms = form.get('supplier_payment_terms', '').strip() or None
    supplier_iban = form.get('supplier_iban', '').strip() or None
    is_new_supplier = form.get('is_new_supplier', '') == '1'
    existing_supplier_id_str = form.get('existing_supplier_id', '').strip()

    # For existing suppliers: extract OCR-detected IBAN from stored OCR payload
    # so it can be persisted to the supplier record (COALESCE ensures existing value is kept if OCR has none)
    ocr_iban_hint = None
    if not is_new_supplier:
        try:
            raw = inv.get('ocr_raw') or {}
            if isinstance(raw, str):
                import json as _json
                raw = _json.loads(raw)
            if isinstance(raw, dict):
                ocr_iban_hint = raw.get('supplier_iban') or None
        except Exception:
            pass

    # Server-side validation for new supplier required fields (only for faturas,
    # and only when NOT selecting an existing supplier via the dropdown)
    if is_new_supplier and not existing_supplier_id_str and document_type == 'fatura':
        if not supplier_name or not supplier_nif:
            raise ServiceError('Nome e NIF do fornecedor são obrigatórios.')
        if not supplier_payment_method:
            raise ServiceError('Método de pagamento é obrigatório para novo fornecedor.')
        if not supplier_payment_terms:
            raise ServiceError('Prazo de pagamento é obrigatório para novo fornecedor.')

    DOCUMENT_TYPES_INVOICE = {'fatura', 'nota_credito', 'nota_debito'}

    supplier_id = inv.get('supplier_id')

    # If an existing supplier was selected from the dropdown, use it directly
    if existing_supplier_id_str:
        try:
            supplier_id = int(existing_supplier_id_str)
        except ValueError:
            raise ServiceError('ID de fornecedor inválido.')
    elif document_type in DOCUMENT_TYPES_INVOICE and not supplier_id and not supplier_name:
        raise ServiceError('Seleciona ou cria um fornecedor antes de guardar este tipo de documento.')

    if not existing_supplier_id_str and supplier_nif and supplier_name:
        try:
            if is_new_supplier:
                supplier_id = upsert_supplier(
                    name=supplier_name,
                    nif=supplier_nif,
                    category=category or None,
                    store_id=store_id,
                    payment_method=supplier_payment_method,
                    payment_terms=supplier_payment_terms,
                    iban=supplier_iban,
                )
            else:
                # Existing supplier: update IBAN from OCR hint if detected; keep other fields via COALESCE
                supplier_id = upsert_supplier(
                    name=supplier_name,
                    nif=supplier_nif,
                    category=category or None,
                    store_id=store_id,
                    iban=ocr_iban_hint,
                )
        except Exception as exc:
            raise ServiceError(f'Erro ao guardar fornecedor: {exc}') from exc

    # Recalculate due_date from supplier payment_terms if available
    if supplier_payment_terms and issue_date:
        # New supplier: use form-submitted payment_terms
        from database import calculate_due_date
        calc_due = calculate_due_date(issue_date, supplier_payment_terms)
        if calc_due:
            due_date = calc_due
    elif not is_new_supplier and issue_date:
        # Existing supplier: always re-compute from stored payment_terms
        try:
            from database import get_supplier_by_nif, calculate_due_date
            existing = get_supplier_by_nif(supplier_nif) if supplier_nif else None
            if existing and existing.get('payment_terms'):
                calc_due = calculate_due_date(issue_date, existing['payment_terms'])
                if calc_due:
                    due_date = calc_due
        except Exception:
            pass

    onedrive_path = inv.get('onedrive_path')
    onedrive_web_url = inv.get('onedrive_web_url')
    onedrive_warning = None

    if onedrive_subfolder and not onedrive_path:
        try:
            pdf_data, pdf_filename = get_invoice_pdf(invoice_id)
            if pdf_data:
                from flask_app.onedrive_archive import upload_invoice_pdf
                result = upload_invoice_pdf(
                    pdf_data,
                    pdf_filename or inv.get('pdf_filename', 'fatura.pdf'),
                    onedrive_subfolder,
                    issue_date,
                )
                onedrive_path = result.get('onedrive_path')
                onedrive_web_url = result.get('web_url')
                onedrive_warning = result.get('warning')
        except Exception as exc:
            logger.warning('OneDrive upload failed for invoice %s: %s', invoice_id, exc)
            onedrive_warning = str(exc)

    # If no supplier_id yet but we have a name, try auto-lookup by name
    if not supplier_id and supplier_name:
        try:
            from database import get_supplier_by_name as _gsbn
            matched = _gsbn(supplier_name)
            if matched:
                supplier_id = matched['id']
        except Exception:
            pass

    # Enforce: invoice-type documents must have a resolved supplier before leaving draft
    DOCUMENT_TYPES_INVOICE = {'fatura', 'nota_credito', 'nota_debito'}
    if document_type in DOCUMENT_TYPES_INVOICE and not supplier_id:
        raise ServiceError(
            'Seleciona ou cria um fornecedor antes de guardar este tipo de documento.'
        )

    _old_status = inv.get('status')
    try:
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
            'onedrive_path': onedrive_path,
            'status': 'pending_review',
            'notes': notes or None,
            'document_type': document_type,
            'centro_custo_id': centro_custo_id,
            'categoria_custo_id': categoria_custo_id,
        }, changed_by=changed_by)
    except Exception as exc:
        raise ServiceError(f'Erro ao actualizar fatura: {exc}') from exc

    # Insert a distinct audit marker so OCR reviews are visually distinguishable from generic edits
    if _old_status == 'draft':
        try:
            from db.connection import db_connection as _dbc
            with _dbc() as _conn:
                _conn.cursor().execute(
                    "INSERT INTO invoice_audit_log "
                    "(invoice_id, campo_alterado, valor_anterior, valor_novo, alterado_por) "
                    "VALUES (%s, 'revisão_ocr', 'draft', 'pending_review', %s)",
                    (invoice_id, changed_by),
                )
                _conn.commit()
        except Exception as _ae:
            logger.warning('save_reviewed_invoice: revisão_ocr audit insert failed for inv=%s: %s', invoice_id, _ae)

    # Auto-link other invoices with the same supplier name (matches by registered name)
    if supplier_id:
        try:
            from database import link_invoices_to_supplier_by_name as _link
            _link(supplier_id)
        except Exception as exc:
            logger.warning('link_invoices_to_supplier_by_name failed after save for supplier %s: %s', supplier_id, exc)

    # When the user selected an existing supplier from the dropdown, also bulk-link
    # all invoices that share the original OCR-detected name text — these may differ
    # from the registered supplier name (e.g. "Foo Bar Ltd" vs "Foo Bar, Lda.").
    if existing_supplier_id_str and supplier_id and _ocr_supplier_name:
        try:
            from database import bulk_link_invoices_by_name as _bulk_link
            linked = _bulk_link(_ocr_supplier_name, supplier_id)
            if linked:
                logger.info(
                    'bulk_link_invoices_by_name: linked %d invoice(s) for name=%r supplier_id=%s',
                    linked, _ocr_supplier_name, supplier_id,
                )
        except Exception as exc:
            logger.warning(
                'bulk_link_invoices_by_name failed for name=%r supplier_id=%s: %s',
                _ocr_supplier_name, supplier_id, exc,
            )

    if onedrive_web_url and onedrive_web_url != inv.get('onedrive_web_url'):
        try:
            update_invoice_onedrive(invoice_id, onedrive_path, onedrive_subfolder,
                                    onedrive_web_url=onedrive_web_url)
        except Exception as exc:
            logger.warning('OneDrive URL update failed for invoice %s: %s', invoice_id, exc)

    return {'warning': onedrive_warning}
