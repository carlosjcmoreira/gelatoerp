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
    get_suppliers,
    propose_invoice_payment,
    suggest_payment_date,
    get_payment_methods_config,
    upsert_supplier,
)

compras_bp = Blueprint('compras', __name__)

TABS = [
    {'id': 'faturas', 'label': 'Faturas', 'icon': '🧾', 'url_endpoint': 'faturas.index'},
    {'id': 'nova_fatura', 'label': 'Registar Fatura', 'icon': '➕', 'url_endpoint': 'compras.nova_fatura'},
    {'id': 'artigos', 'label': 'Artigos de Fornecimento', 'icon': '📋', 'url_endpoint': 'compras.artigos'},
    {'id': 'criar_ordem', 'label': 'Criar Ordem de Transferência', 'icon': '📦', 'url_endpoint': 'compras.criar_ordem'},
]


def _get_username():
    return session.get('user', {}).get('username', 'sistema')


@compras_bp.route('/')
@perm_required('acesso_administrativo')
def index():
    items = [{'icon': t['icon'], 'label': t['label'], 'url': url_for(t['url_endpoint'])} for t in TABS]
    return render_template('components/section_menu.html', items=items)


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
        if document_type not in ('fatura', 'nota_credito'):
            document_type = 'fatura'

        if not supplier_name:
            flash('Nome do fornecedor é obrigatório.', 'warning')
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
        })

        if due_date:
            suggested_date, is_fallback, _ = suggest_payment_date(
                invoice_id, amount_eur, due_date=due_date
            )
            propose_invoice_payment(invoice_id, suggested_date, amount_eur)
            if is_fallback:
                flash(f'Fatura de {supplier_name} registada. Vencimento: {due_date.strftime("%d/%m/%Y")}.', 'warning')
            else:
                flash(f'Fatura de {supplier_name} registada. Data de pagamento proposta: {suggested_date.strftime("%d/%m/%Y")}.', 'success')
        else:
            flash(f'Fatura de {supplier_name} registada com sucesso!', 'success')

        return redirect(url_for('compras.index'))

    suppliers = get_suppliers()
    payment_methods = [m for m in get_payment_methods_config() if m.get('ativo')]
    return render_template('compras/nova_fatura.html',
                           suppliers=suppliers,
                           payment_methods=payment_methods,
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
