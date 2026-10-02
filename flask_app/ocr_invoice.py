import os
import base64
import json
import logging
from io import BytesIO

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


def _get_anthropic_client():
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=ANTHROPIC_API_KEY)
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

    if not ANTHROPIC_API_KEY:
        return _empty_extraction("OCR service not configured (ANTHROPIC_API_KEY missing)")

    try:
        # Claude natively reads PDFs via base64
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode('utf-8')

        prompt = """Analisa este documento PDF que é uma fatura de fornecedor.
Extrai os seguintes campos e devolve APENAS um objeto JSON válido (sem markdown, sem texto extra):

{
  "supplier_name": "nome do fornecedor",
  "supplier_nif": "NIF/NIPC do fornecedor (apenas dígitos, sem espaços ou pontos)",
  "invoice_number": "número da fatura",
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

Regras:
- Se um campo não for encontrado, usa null.
- Para datas, usa formato YYYY-MM-DD. Se apenas ano/mês visível, tenta inferir ou devolve null.
- amount_eur é o total a pagar (incluindo IVA).
- vat_amount_eur é apenas o valor de IVA.
- confidence é um valor entre 0 e 1 por campo (1 = certeza absoluta).
- NIF deve ter apenas dígitos, sem pontos ou espaços.
- payment_method_hint: se detetares menção a método de pagamento (transferência, débito direto, confirming, numerário), devolve um dos valores: "transferencia", "debito_direto", "confirming", "numerario". Caso contrário null.
- payment_terms_hint: se detetares prazo de pagamento (ex: "a pronto", "30 dias", "60 dias", "final do mês"), devolve um dos valores: "a_pronto", "15_dias", "30_dias", "60_dias", "final_mes". Caso contrário null.
- supplier_iban: se encontrares um IBAN do fornecedor no documento, extrai-o (ex: "PT50..."). Caso contrário null.
- Devolve APENAS o JSON, sem qualquer texto adicional."""

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

        return {
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

    if not ANTHROPIC_API_KEY:
        return _empty_extraction("OCR service not configured (ANTHROPIC_API_KEY missing)")

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

        prompt = """Analisa esta imagem que pode ser uma fatura, guia de entrega ou documento de compra.
Extrai os seguintes campos e devolve APENAS um objeto JSON válido (sem markdown, sem texto extra):

{
  "supplier_name": "nome do fornecedor",
  "supplier_nif": "NIF/NIPC do fornecedor (apenas dígitos, sem espaços ou pontos)",
  "invoice_number": "número da fatura ou guia",
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

Regras:
- Se um campo não for encontrado, usa null.
- Para datas, usa formato YYYY-MM-DD.
- amount_eur é o total a pagar (incluindo IVA).
- vat_amount_eur é apenas o valor de IVA.
- confidence é um valor entre 0 e 1 por campo.
- NIF deve ter apenas dígitos, sem pontos ou espaços.
- payment_method_hint: se detetares método de pagamento, devolve um de: "transferencia", "debito_direto", "confirming", "numerario". Caso contrário null.
- payment_terms_hint: se detetares prazo, devolve um de: "a_pronto", "15_dias", "30_dias", "60_dias", "final_mes". Caso contrário null.
- supplier_iban: extrai o IBAN do fornecedor se visível. Caso contrário null.
- Devolve APENAS o JSON, sem qualquer texto adicional."""

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

        return {
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
    return ''.join(c for c in str(val) if c.isdigit())


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
