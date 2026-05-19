import os
import base64
import json
import logging
from io import BytesIO

logger = logging.getLogger(__name__)

# Canonical set of valid document_type values — kept in sync with DOCUMENT_TYPE_LABELS in db/faturas.py
_VALID_DOC_TYPES = frozenset({'fatura', 'nota_credito', 'nota_debito', 'nota_pagamento_imposto', 'outro'})

# Uses Replit AI Integrations (Anthropic) — no personal API key required
AI_INTEGRATIONS_ANTHROPIC_API_KEY = os.environ.get("AI_INTEGRATIONS_ANTHROPIC_API_KEY")
AI_INTEGRATIONS_ANTHROPIC_BASE_URL = os.environ.get("AI_INTEGRATIONS_ANTHROPIC_BASE_URL")


def _get_anthropic_client():
    try:
        from anthropic import Anthropic
        return Anthropic(
            api_key=AI_INTEGRATIONS_ANTHROPIC_API_KEY,
            base_url=AI_INTEGRATIONS_ANTHROPIC_BASE_URL,
        )
    except ImportError:
        return None


def _pdf_bytes_to_base64_images(pdf_bytes: bytes) -> list:
    """Convert first pages of a PDF to base64 JPEG images via Pillow."""
    images_b64 = []
    try:
        from PIL import Image
        import struct

        # Try pdf2image if available
        try:
            import pdf2image
            images = pdf2image.convert_from_bytes(pdf_bytes, first_page=1, last_page=2, dpi=150)
            for img in images:
                buf = BytesIO()
                img.save(buf, format='JPEG', quality=85)
                images_b64.append(base64.b64encode(buf.getvalue()).decode())
            return images_b64
        except Exception:
            pass

        # Fallback: embed PDF directly as base64 (Claude can read PDFs)
        return []
    except Exception as e:
        logger.warning("Could not convert PDF to images: %s", e)
        return []


def extract_invoice_fields(pdf_bytes: bytes, pdf_filename: str = '') -> dict:
    """
    Extract invoice fields from a PDF using Anthropic Vision.
    Returns a dict with extracted fields and per-field confidence scores.
    """
    client = _get_anthropic_client()

    if not client:
        return _empty_extraction("Anthropic client not available")

    if not AI_INTEGRATIONS_ANTHROPIC_BASE_URL:
        return _empty_extraction("OCR service not configured")

    try:
        # Claude natively reads PDFs via base64
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode('utf-8')

        prompt = """Analisa este documento PDF. Pode ser uma fatura de fornecedor privado OU um documento de pagamento emitido por uma entidade pública portuguesa (AT, Segurança Social, etc.).

PASSO 1 — Identifica o tipo:
- Se contém termos como "Declaração Mensal de Remunerações", "Retenções na Fonte", "Contribuições", "Segurança Social", "IRS", "IRC", "Imposto do Selo", "Importância a pagar", "Referência para pagamento" como guia estatal, "DGSS", "Autoridade Tributária" → é um documento governamental.
- Caso contrário → é uma fatura de fornecedor privado.

PASSO 2 — Devolve APENAS um objeto JSON válido (sem markdown, sem texto extra):

{
  "document_type_hint": "nota_pagamento_imposto",
  "supplier_name": "nome do emitente",
  "supplier_nif": null,
  "invoice_number": "referência de pagamento ou número da fatura",
  "amount_eur": 0.00,
  "vat_amount_eur": 0.00,
  "issue_date": "YYYY-MM-DD",
  "due_date": "YYYY-MM-DD",
  "payment_method_hint": null,
  "payment_terms_hint": null,
  "supplier_iban": null,
  "confidence": {
    "supplier_name": 0.9,
    "supplier_nif": 0.9,
    "invoice_number": 0.9,
    "amount_eur": 0.9,
    "vat_amount_eur": 0.9,
    "issue_date": 0.9,
    "due_date": 0.9
  }
}

Regras gerais:
- Se um campo não for encontrado, usa null.
- Para datas, usa formato YYYY-MM-DD.
- confidence é um valor entre 0 e 1 por campo (1 = certeza absoluta).
- Devolve APENAS o JSON, sem qualquer texto adicional.

Regras para documentos GOVERNAMENTAIS (AT, Segurança Social, etc.):
- document_type_hint = "nota_pagamento_imposto"
- supplier_name = nome da entidade emissora (ex: "Autoridade Tributária e Aduaneira", "Segurança Social / IGFSS")
- supplier_nif = null (o NIF visível no documento é da empresa que paga, não do emitente estatal)
- invoice_number = a "Referência para pagamento" Multibanco (ex: "156490263043606") — só dígitos, sem pontos ou espaços
- amount_eur = "Importância a pagar" ou "Valor a pagar" ou soma total de contribuições
- vat_amount_eur = 0.00 (documentos estatais não têm IVA separado)
- issue_date = data de receção da declaração ou data do documento
- due_date = "Data limite de pagamento" se indicada, caso contrário null
- payment_method_hint = null
- payment_terms_hint = null
- supplier_iban = null

Regras para FATURAS de fornecedor privado:
- document_type_hint = "fatura" (ou "nota_credito" se for nota de crédito, "nota_debito" se for nota de débito, "outro" nos restantes casos)
- supplier_name = nome do fornecedor
- supplier_nif = NIF/NIPC do fornecedor (dígitos e prefixo de país opcional, ex: "PT501234567" ou "501234567"; sem pontos, espaços ou traços)
- invoice_number = número da fatura
- amount_eur = total a pagar incluindo IVA
- vat_amount_eur = valor do IVA
- payment_method_hint: "transferencia", "debito_direto", "confirming" ou "numerario" se detetado, null caso contrário
- payment_terms_hint: "a_pronto", "15_dias", "30_dias", "60_dias" ou "final_mes" se detetado, null caso contrário
- supplier_iban: IBAN do fornecedor se visível (ex: "PT50..."), null caso contrário"""

        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }]
        )

        raw_text = message.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        parsed = json.loads(raw_text)
        confidences = parsed.get('confidence', {})
        avg_confidence = sum(v for v in confidences.values() if v is not None) / max(len(confidences), 1)

        raw_doc_type = parsed.get('document_type_hint')
        document_type_hint = raw_doc_type if raw_doc_type in _VALID_DOC_TYPES else 'fatura'
        logger.info("OCR PDF classification: document_type_hint=%r (raw=%r) confidence=%.2f",
                    document_type_hint, raw_doc_type, avg_confidence)

        return {
            'document_type_hint': document_type_hint,
            'supplier_name': parsed.get('supplier_name'),
            'supplier_nif': _clean_nif(parsed.get('supplier_nif')),
            'invoice_number': parsed.get('invoice_number'),
            'amount_eur': _parse_float(parsed.get('amount_eur')),
            'vat_amount_eur': _parse_float(parsed.get('vat_amount_eur')),
            'issue_date': _parse_date(parsed.get('issue_date')),
            'due_date': _parse_date(parsed.get('due_date')),
            'payment_method_hint': parsed.get('payment_method_hint'),
            'payment_terms_hint': parsed.get('payment_terms_hint'),
            'supplier_iban': parsed.get('supplier_iban'),
            'confidence': confidences,
            'ocr_confidence': round(avg_confidence, 3),
            'error': None,
        }

    except Exception as e:
        logger.error("OCR extraction failed: %s", e)
        return _empty_extraction(str(e))


def _empty_extraction(error: str) -> dict:
    return {
        'document_type_hint': None,
        'supplier_name': None,
        'supplier_nif': None,
        'invoice_number': None,
        'amount_eur': None,
        'vat_amount_eur': None,
        'issue_date': None,
        'due_date': None,
        'payment_method_hint': None,
        'payment_terms_hint': None,
        'supplier_iban': None,
        'confidence': {},
        'ocr_confidence': 0.0,
        'error': error,
    }


def extract_invoice_fields_from_image(image_bytes: bytes, filename: str = '') -> dict:
    """
    Extract invoice fields from an image (photo) using Anthropic Vision.
    Returns a dict with extracted fields and per-field confidence scores.
    """
    client = _get_anthropic_client()

    if not client:
        return _empty_extraction("Anthropic client not available")

    if not AI_INTEGRATIONS_ANTHROPIC_BASE_URL:
        return _empty_extraction("OCR service not configured")

    try:
        # Determine media type from filename
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'jpeg'
        media_type_map = {
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'png': 'image/png', 'gif': 'image/gif',
            'webp': 'image/webp',
        }
        media_type = media_type_map.get(ext, 'image/jpeg')

        # HEIC/HEIF (iPhone native format) not supported by Claude Vision API;
        # attempt conversion to JPEG using Pillow if available.
        if ext in ('heic', 'heif'):
            try:
                from PIL import Image
                from io import BytesIO
                img = Image.open(BytesIO(image_bytes))
                buf = BytesIO()
                img.convert('RGB').save(buf, format='JPEG', quality=85)
                image_bytes = buf.getvalue()
                media_type = 'image/jpeg'
            except Exception as conv_err:
                logger.warning("HEIC conversion failed, passing as-is: %s", conv_err)
                media_type = 'image/jpeg'

        image_b64 = base64.standard_b64encode(image_bytes).decode('utf-8')

        prompt = """Analisa esta imagem. Pode ser uma fatura de fornecedor privado, guia de entrega, ou um documento de pagamento emitido por uma entidade pública portuguesa (AT, Segurança Social, etc.).

PASSO 1 — Identifica o tipo:
- Se contém termos como "Declaração Mensal de Remunerações", "Retenções na Fonte", "Contribuições", "Segurança Social", "IRS", "IRC", "Importância a pagar", "Referência para pagamento" como guia estatal, "Autoridade Tributária" → é um documento governamental.
- Caso contrário → é uma fatura ou guia de fornecedor privado.

PASSO 2 — Devolve APENAS um objeto JSON válido (sem markdown, sem texto extra):

{
  "document_type_hint": "fatura",
  "supplier_name": "nome do emitente",
  "supplier_nif": null,
  "invoice_number": "referência de pagamento ou número da fatura/guia",
  "amount_eur": 0.00,
  "vat_amount_eur": 0.00,
  "issue_date": "YYYY-MM-DD",
  "due_date": "YYYY-MM-DD",
  "payment_method_hint": null,
  "payment_terms_hint": null,
  "supplier_iban": null,
  "confidence": {
    "supplier_name": 0.9,
    "supplier_nif": 0.9,
    "invoice_number": 0.9,
    "amount_eur": 0.9,
    "vat_amount_eur": 0.9,
    "issue_date": 0.9,
    "due_date": 0.9
  }
}

Regras gerais:
- Se um campo não for encontrado, usa null.
- Para datas, usa formato YYYY-MM-DD.
- confidence é um valor entre 0 e 1 por campo.
- Devolve APENAS o JSON, sem qualquer texto adicional.

Regras para documentos GOVERNAMENTAIS:
- document_type_hint = "nota_pagamento_imposto"
- supplier_name = nome da entidade emissora (ex: "Autoridade Tributária e Aduaneira", "Segurança Social")
- supplier_nif = null
- invoice_number = a "Referência para pagamento" (só dígitos)
- amount_eur = "Importância a pagar" ou total de contribuições
- vat_amount_eur = 0.00
- due_date = "Data limite de pagamento" se indicada

Regras para FATURAS/GUIAS de fornecedor privado:
- document_type_hint = "fatura" (ou "nota_credito", "nota_debito", "outro" conforme o documento)
- supplier_name = nome do fornecedor
- supplier_nif = NIF do fornecedor (dígitos e prefixo de país opcional, ex: "PT501234567" ou "501234567"; sem pontos, espaços ou traços)
- invoice_number = número da fatura ou guia
- amount_eur = total a pagar incluindo IVA
- vat_amount_eur = valor do IVA
- payment_method_hint: "transferencia", "debito_direto", "confirming" ou "numerario" se detetado, null caso contrário
- payment_terms_hint: "a_pronto", "15_dias", "30_dias", "60_dias" ou "final_mes" se detetado, null caso contrário
- supplier_iban: IBAN do fornecedor se visível, null caso contrário"""

        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }]
        )

        raw_text = message.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        parsed = json.loads(raw_text)
        confidences = parsed.get('confidence', {})
        avg_confidence = sum(v for v in confidences.values() if v is not None) / max(len(confidences), 1)

        raw_doc_type = parsed.get('document_type_hint')
        document_type_hint = raw_doc_type if raw_doc_type in _VALID_DOC_TYPES else 'fatura'
        logger.info("OCR image classification: document_type_hint=%r (raw=%r) confidence=%.2f",
                    document_type_hint, raw_doc_type, avg_confidence)

        return {
            'document_type_hint': document_type_hint,
            'supplier_name': parsed.get('supplier_name'),
            'supplier_nif': _clean_nif(parsed.get('supplier_nif')),
            'invoice_number': parsed.get('invoice_number'),
            'amount_eur': _parse_float(parsed.get('amount_eur')),
            'vat_amount_eur': _parse_float(parsed.get('vat_amount_eur')),
            'issue_date': _parse_date(parsed.get('issue_date')),
            'due_date': _parse_date(parsed.get('due_date')),
            'payment_method_hint': parsed.get('payment_method_hint'),
            'payment_terms_hint': parsed.get('payment_terms_hint'),
            'supplier_iban': parsed.get('supplier_iban'),
            'confidence': confidences,
            'ocr_confidence': round(avg_confidence, 3),
            'error': None,
        }

    except Exception as e:
        logger.error("Image OCR extraction failed: %s", e)
        return _empty_extraction(str(e))


def _clean_nif(val) -> str:
    if not val:
        return None
    cleaned = str(val).strip().replace(' ', '').replace('-', '')
    return cleaned.upper() or None


def _parse_float(val) -> float:
    if val is None:
        return None
    try:
        return float(str(val).replace(',', '.'))
    except Exception:
        return None


def _parse_date(val) -> str:
    if not val:
        return None
    val = str(val).strip()
    if len(val) == 10 and val[4] == '-':
        return val
    return val
