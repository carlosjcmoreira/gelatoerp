import logging
import json
import base64
import tempfile
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from flask_app.auth import perm_required
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_credit_contracts, get_credit_contract, upsert_credit_contract,
    delete_credit_contract, deactivate_credit_contract,
    get_bank_balance_entries, insert_bank_balance_entry, delete_bank_balance_entry,
    get_credit_dashboard, get_prestacoes_calendar,
    get_payment_revisions, add_payment_revision,
    get_confirming_contracts, get_confirming_dashboard,
    create_confirming_parcela, get_confirming_parcelas, update_confirming_parcela_estado,
)

logger = logging.getLogger(__name__)

credito_bp = Blueprint('credito', __name__)

TIPOS_CONTRATO = [
    ('leasing', 'Leasing'),
    ('credito_automovel', 'Crédito Automóvel'),
    ('emprestimo', 'Empréstimo'),
    ('overdraft', 'Conta Caucionada'),
    ('confirming', 'Confirming'),
]

LOJAS = ['Matosinhos', 'Bolhão', 'Geral']

EXTRACTION_PROMPT = """Analisa o seguinte documento bancário português e extrai os dados do contrato de crédito.

Responde APENAS com um JSON válido (sem markdown, sem ```), com os seguintes campos:
{
  "contratos": [
    {
      "tipo": "leasing" | "credito_automovel" | "emprestimo" | "overdraft" | "confirming",
      "label": "descrição curta do contrato, ex: Leasing Carrinha BPI",
      "banco": "nome do banco ou entidade financeira",
      "capital_inicial": número ou null,
      "saldo_divida": número ou null,
      "tan": número (percentagem, ex: 3.5) ou null,
      "prestacao_mensal": número ou null,
      "dia_debito": número (dia do mês) ou null,
      "data_inicio": "YYYY-MM-DD" ou null,
      "data_fim": "YYYY-MM-DD" ou null,
      "plafond": número ou null (só para conta caucionada/overdraft),
      "notas": "informações adicionais relevantes"
    }
  ]
}

Regras:
- Se o documento contiver múltiplos contratos, inclui todos no array.
- tipo deve ser um dos valores indicados. Usa "leasing" para leasings/renting, "credito_automovel" para crédito auto, "emprestimo" para empréstimos de médio/longo prazo, "overdraft" para contas caucionadas ou linhas de crédito, "confirming" para linhas de confirming bancário.
- Valores monetários em euros, sem símbolo (ex: 15000.00).
- TAN em percentagem (ex: 3.5 para 3.5%).
- Se não conseguires extrair um campo, usa null.
- Não inventes dados — usa apenas o que está no documento.
"""


def _extract_text_from_pdf(file_bytes):
    """Extract text from PDF bytes using pdfplumber."""
    try:
        import pdfplumber
        import io
        text_parts = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row:
                            text_parts.append(' | '.join(str(cell or '') for cell in row))
        return '\n'.join(text_parts)
    except Exception as e:
        logger.error('Erro ao extrair texto do PDF: %s', e, exc_info=True)
        return None


def _parse_document_with_ai(text=None, image_b64=None, mime_type=None):
    """Send document content to GPT for structured extraction."""
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
    )

    messages = [{"role": "system", "content": EXTRACTION_PROMPT}]

    if text:
        messages.append({"role": "user", "content": f"Documento:\n\n{text[:12000]}"})
    elif image_b64 and mime_type:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "Analisa esta imagem do documento bancário e extrai os dados do contrato."},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
            ]
        })
    else:
        return None

    # the newest OpenAI model is "gpt-5" which was released August 7, 2025.
    # do not change this unless explicitly requested by the user
    response = client.chat.completions.create(
        model="gpt-5",
        messages=messages,
        max_completion_tokens=4096,
    )

    content = response.choices[0].message.content or ""
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.error('Resposta AI não é JSON válido: %s', content[:500])
        return None


@credito_bp.route('/')
@perm_required('acesso_gestor')
def index():
    dash = get_credit_dashboard()
    return render_template('credito/index.html', dash=dash)


@credito_bp.route('/contratos')
@perm_required('acesso_gestor')
def contratos():
    estado = request.args.get('estado', 'ativo')
    contracts = get_credit_contracts(estado=estado if estado != 'todos' else None)
    edit_id = request.args.get('edit', type=int)
    edit_contract = get_credit_contract(edit_id) if edit_id else None
    return render_template(
        'credito/contratos.html',
        contracts=contracts,
        edit_contract=edit_contract,
        tipos=TIPOS_CONTRATO,
        lojas=LOJAS,
        estado_filtro=estado,
    )


@credito_bp.route('/contratos/guardar', methods=['POST'])
@perm_required('acesso_gestor')
def guardar_contrato():
    contract_id = request.form.get('contract_id', type=int)
    data = {
        'tipo': request.form.get('tipo'),
        'label': request.form.get('label', '').strip(),
        'banco': request.form.get('banco', '').strip(),
        'loja_associada': request.form.get('loja_associada', '').strip(),
        'capital_inicial': request.form.get('capital_inicial') or None,
        'saldo_divida': request.form.get('saldo_divida') or None,
        'tan': request.form.get('tan') or None,
        'prestacao_mensal': request.form.get('prestacao_mensal') or None,
        'dia_debito': request.form.get('dia_debito') or None,
        'data_inicio': request.form.get('data_inicio') or None,
        'data_fim': request.form.get('data_fim') or None,
        'plafond': request.form.get('plafond') or None,
        'estado': request.form.get('estado', 'ativo'),
        'notas': request.form.get('notas', '').strip(),
    }
    if not data['label']:
        flash('O campo «Label / Nome» é obrigatório.', 'error')
        return redirect(url_for('credito.contratos'))
    try:
        upsert_credit_contract(data, contract_id)
        flash('Contrato guardado com sucesso.', 'success')
    except Exception as e:
        logger.error('Erro ao guardar contrato id=%s: %s', contract_id, e, exc_info=True)
        flash('Não foi possível guardar o contrato. Tente novamente.', 'error')
    return redirect(url_for('credito.contratos'))


@credito_bp.route('/contratos/<int:contract_id>/desativar', methods=['POST'])
@perm_required('acesso_gestor')
def desativar_contrato(contract_id):
    try:
        deactivate_credit_contract(contract_id)
        flash('Contrato desativado.', 'success')
    except Exception as e:
        logger.error('Erro ao desativar contrato id=%s: %s', contract_id, e, exc_info=True)
        flash('Não foi possível desativar o contrato. Tente novamente.', 'error')
    return redirect(url_for('credito.contratos'))


@credito_bp.route('/contratos/<int:contract_id>/eliminar', methods=['POST'])
@perm_required('acesso_gestor')
def eliminar_contrato(contract_id):
    try:
        delete_credit_contract(contract_id)
        flash('Contrato eliminado.', 'success')
    except Exception as e:
        logger.error('Erro ao eliminar contrato id=%s: %s', contract_id, e, exc_info=True)
        flash('Não foi possível eliminar o contrato. Tente novamente.', 'error')
    return redirect(url_for('credito.contratos'))


@credito_bp.route('/contratos/<int:contract_id>')
@perm_required('acesso_gestor')
def contrato_detalhe(contract_id):
    contract = get_credit_contract(contract_id)
    if not contract:
        flash('Contrato não encontrado.', 'error')
        return redirect(url_for('credito.contratos'))
    revisions = get_payment_revisions(contract_id)
    return render_template(
        'credito/contrato_detalhe.html',
        contract=contract,
        revisions=revisions,
    )


@credito_bp.route('/contratos/<int:contract_id>/revisao', methods=['POST'])
@perm_required('acesso_gestor')
def guardar_revisao(contract_id):
    contract = get_credit_contract(contract_id)
    if not contract:
        flash('Contrato não encontrado.', 'error')
        return redirect(url_for('credito.contratos'))
    data_inicio = request.form.get('data_inicio', '').strip()
    prestacao_raw = request.form.get('prestacao', '').strip()
    if not data_inicio or not prestacao_raw:
        flash('Data de início e prestação são obrigatórios.', 'error')
        return redirect(url_for('credito.contrato_detalhe', contract_id=contract_id))
    try:
        prestacao = float(prestacao_raw)
        if prestacao <= 0:
            raise ValueError
    except ValueError:
        flash('Prestação inválida.', 'error')
        return redirect(url_for('credito.contrato_detalhe', contract_id=contract_id))
    data = {
        'data_inicio': data_inicio,
        'prestacao': prestacao,
        'tan': request.form.get('tan') or None,
        'euribor': request.form.get('euribor') or None,
        'spread': request.form.get('spread') or None,
        'notas': request.form.get('notas', '').strip() or None,
    }
    try:
        add_payment_revision(contract_id, data)
        flash('Revisão de prestação registada com sucesso.', 'success')
    except Exception as e:
        logger.error('Erro ao guardar revisão contrato id=%s: %s', contract_id, e, exc_info=True)
        flash('Não foi possível guardar a revisão. Tente novamente.', 'error')
    return redirect(url_for('credito.contrato_detalhe', contract_id=contract_id))


@credito_bp.route('/calendario')
@perm_required('acesso_gestor')
def calendario():
    events = get_prestacoes_calendar(weeks=13)
    return render_template('credito/calendario.html', events=events)


@credito_bp.route('/caucionada')
@perm_required('acesso_gestor')
def caucionada():
    from datetime import date
    overdraft_contracts = [c for c in get_credit_contracts(estado='ativo') if c['tipo'] == 'overdraft']
    entries = get_bank_balance_entries(limit=52)
    return render_template(
        'credito/caucionada.html',
        overdraft_contracts=overdraft_contracts,
        entries=entries,
        today=date.today(),
    )


@credito_bp.route('/caucionada/registar', methods=['POST'])
@perm_required('acesso_gestor')
def registar_saldo():
    contrato_id = request.form.get('contrato_id', type=int)
    contract = get_credit_contract(contrato_id) if contrato_id else None
    data = {
        'data': request.form.get('data'),
        'loja': request.form.get('loja', '').strip(),
        'saldo': request.form.get('saldo'),
        'contrato_id': contrato_id,
        'plafond': contract['plafond'] if contract else None,
        'tan': contract['tan'] if contract else None,
        'notas': request.form.get('notas', '').strip(),
    }
    if not data['saldo'] or not data['data']:
        flash('Data e saldo são obrigatórios.', 'error')
        return redirect(url_for('credito.caucionada'))
    try:
        insert_bank_balance_entry(data)
        flash('Saldo bancário registado com sucesso.', 'success')
    except Exception as e:
        logger.error('Erro ao registar saldo bancário: %s', e, exc_info=True)
        flash('Não foi possível registar o saldo. Tente novamente.', 'error')
    return redirect(url_for('credito.caucionada'))


@credito_bp.route('/caucionada/<int:entry_id>/eliminar', methods=['POST'])
@perm_required('acesso_gestor')
def eliminar_saldo(entry_id):
    try:
        delete_bank_balance_entry(entry_id)
        flash('Registo eliminado.', 'success')
    except Exception as e:
        logger.error('Erro ao eliminar registo de saldo id=%s: %s', entry_id, e, exc_info=True)
        flash('Não foi possível eliminar o registo. Tente novamente.', 'error')
    return redirect(url_for('credito.caucionada'))


@credito_bp.route('/confirming')
@perm_required('acesso_gestor')
def confirming():
    from datetime import date
    dash = get_confirming_dashboard()
    return render_template(
        'credito/confirming.html',
        dash=dash,
        today=date.today(),
    )


@credito_bp.route('/confirming/parcela/<int:parcela_id>/estado', methods=['POST'])
@perm_required('acesso_gestor')
def atualizar_parcela_estado(parcela_id):
    novo_estado = request.form.get('estado', '').strip()
    ESTADOS_VALIDOS = ('scheduled', 'confirmed', 'paid', 'settled', 'cancelled')
    if novo_estado not in ESTADOS_VALIDOS:
        flash('Estado inválido.', 'warning')
        return redirect(url_for('credito.confirming'))
    try:
        update_confirming_parcela_estado(parcela_id, novo_estado)
        flash(f'Parcela actualizada para «{novo_estado}».', 'success')
    except Exception as e:
        logger.error('Erro ao actualizar parcela id=%s: %s', parcela_id, e, exc_info=True)
        flash('Não foi possível actualizar a parcela. Tente novamente.', 'error')
    return redirect(url_for('credito.confirming'))


@credito_bp.route('/upload')
@perm_required('acesso_gestor')
def upload():
    extracted = session.pop('extracted_contracts', None)
    return render_template(
        'credito/upload.html',
        tipos=TIPOS_CONTRATO,
        lojas=LOJAS,
        extracted=extracted,
    )


ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'webp', 'gif'}
MAX_FILE_SIZE = 20 * 1024 * 1024


@credito_bp.route('/upload/processar', methods=['POST'])
@perm_required('acesso_gestor')
def processar_upload():
    if 'documento' not in request.files:
        flash('Nenhum ficheiro selecionado.', 'error')
        return redirect(url_for('credito.upload'))

    file = request.files['documento']
    if not file or not file.filename:
        flash('Nenhum ficheiro selecionado.', 'error')
        return redirect(url_for('credito.upload'))

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        flash(f'Formato não suportado. Use PDF, PNG, JPG ou WEBP.', 'error')
        return redirect(url_for('credito.upload'))

    file_bytes = file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        flash('Ficheiro demasiado grande (máximo 20 MB).', 'error')
        return redirect(url_for('credito.upload'))

    try:
        result = None
        if ext == 'pdf':
            text = _extract_text_from_pdf(file_bytes)
            if not text or len(text.strip()) < 20:
                image_b64 = base64.b64encode(file_bytes).decode('utf-8')
                result = _parse_document_with_ai(image_b64=image_b64, mime_type='application/pdf')
            else:
                result = _parse_document_with_ai(text=text)
        else:
            mime_map = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp', 'gif': 'image/gif'}
            image_b64 = base64.b64encode(file_bytes).decode('utf-8')
            result = _parse_document_with_ai(image_b64=image_b64, mime_type=mime_map.get(ext, 'image/jpeg'))

        if not result or 'contratos' not in result or not result['contratos']:
            flash('Não foi possível extrair dados do documento. Verifique se é um documento bancário válido.', 'error')
            return redirect(url_for('credito.upload'))

        session['extracted_contracts'] = result['contratos']
        flash(f'{len(result["contratos"])} contrato(s) extraído(s) do documento. Reveja os dados abaixo e confirme.', 'success')
        return redirect(url_for('credito.upload'))

    except Exception as e:
        logger.error('Erro ao processar documento: %s', e, exc_info=True)
        flash('Erro ao processar o documento. Tente novamente.', 'error')
        return redirect(url_for('credito.upload'))


@credito_bp.route('/upload/confirmar', methods=['POST'])
@perm_required('acesso_gestor')
def confirmar_upload():
    contracts_json = request.form.get('contracts_json', '[]')
    try:
        contracts = json.loads(contracts_json)
    except json.JSONDecodeError:
        flash('Erro nos dados do formulário. Por favor, analise o documento novamente.', 'error')
        return redirect(url_for('credito.upload'))

    # Build the data for every contract from the submitted form fields,
    # overriding OCR values with whatever the user edited.
    all_data = []
    for idx, c in enumerate(contracts):
        skip = request.form.get(f'skip_{idx}')
        data = {
            'skip': bool(skip),
            'tipo': request.form.get(f'tipo_{idx}', c.get('tipo', 'emprestimo')),
            'label': request.form.get(f'label_{idx}', c.get('label', '')).strip(),
            'banco': request.form.get(f'banco_{idx}', c.get('banco', '')).strip(),
            'loja_associada': request.form.get(f'loja_{idx}', '').strip(),
            'capital_inicial': request.form.get(f'capital_inicial_{idx}') or c.get('capital_inicial'),
            'saldo_divida': request.form.get(f'saldo_divida_{idx}') or c.get('saldo_divida'),
            'tan': request.form.get(f'tan_{idx}') or c.get('tan'),
            'prestacao_mensal': request.form.get(f'prestacao_mensal_{idx}') or c.get('prestacao_mensal'),
            'dia_debito': request.form.get(f'dia_debito_{idx}') or c.get('dia_debito'),
            'data_inicio': request.form.get(f'data_inicio_{idx}') or c.get('data_inicio'),
            'data_fim': request.form.get(f'data_fim_{idx}') or c.get('data_fim'),
            'plafond': request.form.get(f'plafond_{idx}') or c.get('plafond'),
            'estado': 'ativo',
            'notas': request.form.get(f'notas_{idx}', c.get('notas', '')).strip(),
        }
        all_data.append(data)

    # Validate before writing anything.
    errors = []
    for idx, d in enumerate(all_data):
        if d['skip']:
            continue
        if not d['label']:
            errors.append(f'Contrato {idx + 1}: o campo «Label / Nome» é obrigatório.')

    if errors:
        # Restore user-edited values to session so the review page keeps all edits.
        restored = [{k: v for k, v in d.items() if k not in ('skip', 'estado')} for d in all_data]
        session['extracted_contracts'] = restored
        for msg in errors:
            flash(msg, 'error')
        return redirect(url_for('credito.upload'))

    # All valid — create contracts.
    created = 0
    for d in all_data:
        if d['skip']:
            continue
        db_data = {k: v for k, v in d.items() if k != 'skip'}
        try:
            upsert_credit_contract(db_data)
            created += 1
        except Exception as e:
            logger.error('Erro ao criar contrato do upload label=%s: %s', d['label'], e, exc_info=True)
            flash(f'Erro ao guardar contrato «{d["label"]}». Verifique os dados e tente novamente.', 'error')

    if created:
        flash(f'{created} contrato(s) criado(s) com sucesso a partir do documento.', 'success')
    else:
        flash('Nenhum contrato foi criado. Verifique os dados e tente novamente.', 'error')
    return redirect(url_for('credito.contratos'))
