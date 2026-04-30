from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_app.auth import perm_required
from datetime import date, datetime
import sys, os, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_artigos_administrativos, add_artigo_administrativo,
    update_artigo_administrativo, toggle_artigo_administrativo,
    delete_artigo_administrativo,
    criar_ordem_transferencia,
    create_invoice,
    update_invoice,
    get_suppliers,
    propose_invoice_payment,
    suggest_payment_date,
    get_payment_methods_config,
    upsert_supplier,
    get_cost_centers,
    get_cost_categories_tree,
)
from db.faturas import (
    get_invoices,
    get_invoice,
    INVOICE_STATUS_LABELS,
    DOCUMENT_TYPE_LABELS,
)
import flask_app.services.faturas as faturas_svc
from flask_app.services import ServiceError

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
@perm_required('acesso_administrativo')
def index():
    items = [{'icon': t['icon'], 'label': t['label'], 'url': url_for(t['url_endpoint'])} for t in TABS]
    return render_template('components/section_menu.html', items=items,
                           menu_title='🛒 Compras e Faturas')


@compras_bp.route('/faturas')
@perm_required('acesso_administrativo')
def faturas():
    from datetime import date as _date
    today = _date.today()
    invoices = get_invoices()
    for inv in invoices:
        if inv['status'] == 'scheduled' and inv.get('due_date') and inv['due_date'] < today:
            inv['display_status'] = 'overdue'
        else:
            inv['display_status'] = inv['status']
    return render_template('compras/faturas.html',
                           invoices=invoices,
                           status_labels=INVOICE_STATUS_LABELS,
                           document_type_labels=DOCUMENT_TYPE_LABELS,
                           today=today)


@compras_bp.route('/review-draft/<int:invoice_id>', methods=['GET', 'POST'])
@perm_required('acesso_administrativo')
def review_draft(invoice_id):
    inv = get_invoice(invoice_id)
    if not inv or inv['status'] != 'draft':
        flash('Documento não encontrado ou já submetido.', 'warning')
        return redirect(url_for('compras.faturas'))

    if request.method == 'POST':
        update_invoice(invoice_id, {
            'supplier_name': request.form.get('supplier_name', '').strip() or None,
            'supplier_nif': request.form.get('supplier_nif', '').strip() or None,
            'invoice_number': request.form.get('invoice_number', '').strip() or None,
            'amount_eur': request.form.get('amount_eur') or None,
            'vat_amount_eur': request.form.get('vat_amount_eur') or None,
            'issue_date': request.form.get('issue_date') or None,
            'due_date': request.form.get('due_date') or None,
            'document_type': request.form.get('document_type', 'fatura'),
            'status': 'pending_review',
        })
        flash('Fatura registada com sucesso.', 'success')
        return redirect(url_for('compras.faturas'))

    return render_template('compras/review_draft.html',
                           inv=inv,
                           document_type_labels=DOCUMENT_TYPE_LABELS)


@compras_bp.route('/artigos', methods=['GET', 'POST'])
@perm_required('acesso_administrativo')
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


@compras_bp.route('/nova-fatura', methods=['GET', 'POST'])
@perm_required('acesso_administrativo')
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
        supplier_name = request.form.get('supplier_name', '').strip()
        supplier_nif = request.form.get('supplier_nif', '').strip() or None
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

        if not supplier_name and document_type == 'fatura':
            flash('Nome do fornecedor é obrigatório para faturas.', 'warning')
            return redirect(url_for('compras.nova_fatura'))
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

        # Upsert supplier so their preferred payment method is stored/updated
        supplier_id = None
        if supplier_nif and payment_method:
            try:
                supplier_id = upsert_supplier(supplier_name, supplier_nif,
                                              payment_method=payment_method)
            except Exception as exc:
                logging.warning('compras.nova_fatura: upsert_supplier failed for nif=%s: %s',
                                supplier_nif, exc)

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
@perm_required('acesso_administrativo')
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
