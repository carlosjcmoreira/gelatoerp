import os
import base64
import json
import logging
from io import BytesIO

logger = logging.getLogger(__name__)


def _get_openai_client():
    try:
        from openai import OpenAI
        return OpenAI(
            api_key=os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY"),
            base_url=os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL"),
        )
    except Exception:
        return None


_PROMPT = """Analisa esta imagem que pode ser um relatório de fecho de caixa, talão POS ou talão TPA de um ponto de venda.
Extrai os valores financeiros e devolve APENAS um objeto JSON válido (sem markdown, sem texto extra):

{
  "data": null,
  "colaborador": null,
  "total_moedas": null,
  "valor_notas": null,
  "total_caixa": null,
  "envelope_sobra": null,
  "total_vendas_pos": null,
  "dinheiro_pos": null,
  "cartao_pos": null,
  "ubereats_pos": null,
  "tpa_getnet": null,
  "confidence": {
    "total_moedas": 0.0,
    "valor_notas": 0.0,
    "total_vendas_pos": 0.0,
    "dinheiro_pos": 0.0,
    "cartao_pos": 0.0,
    "tpa_getnet": 0.0
  }
}

Definições dos campos:
- data: data do documento no formato dd-mm-yyyy (ex: 24-03-2026). null se não visível.
- colaborador: nome ou identificador do colaborador/operador que fez o fecho (string, ou null)
- total_moedas: total de moedas contadas manualmente (numerário — moedas)
- valor_notas: total de notas contadas manualmente (numerário — notas)
- total_caixa: total de numerário em caixa (moedas + notas)
- envelope_sobra: valor em envelope ou sobra/troco mantido em caixa
- total_vendas_pos: total de vendas registado no POS/sistema de ponto de venda
- dinheiro_pos: valor de dinheiro/numerário registado no POS (pago em dinheiro)
- cartao_pos: valor de pagamentos por cartão registado no POS (cartão, MB, contactless)
- ubereats_pos: valor de vendas Uber Eats registado no POS
- tpa_getnet: total de transações no terminal físico TPA/Getnet

Regras:
- Se um campo não for visível ou não existir no documento, usa null.
- Todos os valores são em euros (€), sem símbolo, apenas número decimal com ponto (ex: 123.45).
- confidence é um valor entre 0 e 1 (1 = certeza absoluta).
- Devolve APENAS o JSON, sem qualquer texto adicional."""


def ocr_caixa(image_bytes: bytes, filename: str = '') -> dict:
    """
    Extract POS end-of-day cash summary from a photo using OpenAI Vision (gpt-4o, detail=high).
    Returns extracted numeric fields and confidence.
    """
    client = _get_openai_client()
    if not client:
        return _empty_ocr("OpenAI OCR service not configured")

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'jpeg'
    mime_map = {
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
    }
    mime_type = mime_map.get(ext, 'image/jpeg')

    if ext in ('heic', 'heif'):
        try:
            from PIL import Image
            img = Image.open(BytesIO(image_bytes))
            buf = BytesIO()
            img.convert('RGB').save(buf, format='JPEG', quality=85)
            image_bytes = buf.getvalue()
            mime_type = 'image/jpeg'
        except Exception as e:
            logger.warning("HEIC conversion failed: %s", e)
            mime_type = 'image/jpeg'

    image_b64 = base64.standard_b64encode(image_bytes).decode('utf-8')
    data_url = f"data:{mime_type};base64,{image_b64}"

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "high"},
                    },
                    {"type": "text", "text": _PROMPT},
                ]
            }]
        )

        raw_text = response.choices[0].message.content.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        parsed = json.loads(raw_text)
        confidences = parsed.get('confidence', {})
        conf_vals = [v for v in confidences.values() if v is not None]
        avg_conf = sum(conf_vals) / max(len(conf_vals), 1)

        return {
            'data': parsed.get('data'),
            'colaborador': parsed.get('colaborador'),
            'total_moedas': _f(parsed.get('total_moedas')),
            'valor_notas': _f(parsed.get('valor_notas')),
            'total_caixa': _f(parsed.get('total_caixa')),
            'envelope_sobra': _f(parsed.get('envelope_sobra')),
            'total_vendas_pos': _f(parsed.get('total_vendas_pos')),
            'dinheiro_pos': _f(parsed.get('dinheiro_pos')),
            'cartao_pos': _f(parsed.get('cartao_pos')),
            'ubereats_pos': _f(parsed.get('ubereats_pos')),
            'tpa_getnet': _f(parsed.get('tpa_getnet')),
            'confidence': confidences,
            'ocr_confianca': round(avg_conf, 3),
            'error': None,
        }
    except Exception as e:
        logger.error("Fecho caixa OCR failed: %s", e)
        return _empty_ocr(str(e))


def _f(val):
    if val is None:
        return None
    try:
        return float(str(val).replace(',', '.'))
    except Exception:
        return None


def _empty_ocr(error: str) -> dict:
    return {
        'data': None,
        'colaborador': None,
        'total_moedas': None,
        'valor_notas': None,
        'total_caixa': None,
        'envelope_sobra': None,
        'total_vendas_pos': None,
        'dinheiro_pos': None,
        'cartao_pos': None,
        'ubereats_pos': None,
        'tpa_getnet': None,
        'confidence': {},
        'ocr_confianca': 0.0,
        'error': error,
    }
