import os
import base64
import json
import logging
from datetime import date

logger = logging.getLogger(__name__)

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


def extract_producao_sheet(image_bytes: bytes, filename: str = '') -> dict:
    """
    Extract production planning data from a photo of the daily production sheet.

    The sheet is a grid with:
    - Rows: one per sabor (gelado flavour)
    - Columns: pesagem Matosinhos (start-of-day stock weigh-in),
               estimado Bolhão, estimado Matosinhos, estimado Mouzinho, estimado B2B/Outros

    Returns a dict:
    {
      'date': 'YYYY-MM-DD' or None,
      'sabores': {
          '<nome_corrente>': {
              'pesagem_mat': float,
              'prod_bolhao': float,
              'prod_matosinhos': float,
              'prod_mouzinho': float,
              'prod_b2b': float,
          },
          ...
      },
      'error': None or str,
      'ocr_confidence': float,
    }
    """
    client = _get_anthropic_client()
    if not client:
        return _empty_result("Serviço OCR não disponível")
    if not AI_INTEGRATIONS_ANTHROPIC_BASE_URL:
        return _empty_result("Serviço OCR não configurado")

    try:
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'jpeg'
        media_type_map = {
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
        }
        media_type = media_type_map.get(ext, 'image/jpeg')

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
                logger.warning("HEIC conversion failed: %s", conv_err)
                media_type = 'image/jpeg'

        image_b64 = base64.standard_b64encode(image_bytes).decode('utf-8')

        prompt = """Analisa esta imagem de uma folha de registo de produção de gelado.

A folha tem linhas por sabor e colunas para:
- Pesagem Matosinhos: peso atual do gelado em produção (kg), também pode aparecer como "Pesagem Mat", "Mat kg", "Peso Mat" ou similar
- Produção Bolhão: quantidade produzida para Bolhão (kg)
- Produção Matosinhos: quantidade produzida para Matosinhos (kg)  
- Produção Mouzinho: quantidade produzida para Mouzinho (kg)
- Produção B2B / Outros / Eventos: quantidade produzida para B2B ou eventos (kg)

Extrai APENAS um objeto JSON válido (sem markdown, sem texto extra) com esta estrutura:

{
  "date": "YYYY-MM-DD",
  "confidence": 0.85,
  "sabores": {
    "NomeSabor": {
      "pesagem_mat": 0.0,
      "prod_bolhao": 0.0,
      "prod_matosinhos": 0.0,
      "prod_mouzinho": 0.0,
      "prod_b2b": 0.0
    }
  }
}

Regras:
- Se a data não for visível, usa null para "date".
- Para datas usa formato YYYY-MM-DD.
- Se um valor não existir ou estiver em branco, usa 0.0.
- Se um valor aparecer como soma "N+M" (ex: "2+3"), calcula e devolve o total (5.0).
- Se os valores estiverem em gramas (ex: 1500), converte para kg (1.5).
- O nome do sabor deve ser exatamente como aparece na folha.
- Inclui apenas sabores com pelo menos um valor não nulo/zero.
- confidence é a tua confiança geral na extração (0 a 1).
- Devolve APENAS o JSON, sem qualquer texto adicional."""

        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
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
                    {"type": "text", "text": prompt}
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
        confidence = float(parsed.get('confidence', 0.7))

        sabores_raw = parsed.get('sabores', {})
        sabores = {}
        for nome, vals in sabores_raw.items():
            if not isinstance(vals, dict):
                continue
            sabores[nome] = {
                'pesagem_mat': _parse_float(vals.get('pesagem_mat', 0)),
                'prod_bolhao': _parse_float(vals.get('prod_bolhao', 0)),
                'prod_matosinhos': _parse_float(vals.get('prod_matosinhos', 0)),
                'prod_mouzinho': _parse_float(vals.get('prod_mouzinho', 0)),
                'prod_b2b': _parse_float(vals.get('prod_b2b', 0)),
            }

        return {
            'date': _parse_date(parsed.get('date')),
            'sabores': sabores,
            'error': None,
            'ocr_confidence': round(confidence, 3),
        }

    except Exception as e:
        logger.error("OCR producao extraction failed: %s", e)
        return _empty_result(str(e))


def _empty_result(error: str) -> dict:
    return {
        'date': None,
        'sabores': {},
        'error': error,
        'ocr_confidence': 0.0,
    }


def _parse_float(val) -> float:
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(',', '.'))
    except Exception:
        return 0.0


def _parse_date(val) -> str:
    if not val:
        return None
    val = str(val).strip()
    if len(val) == 10 and val[4] == '-':
        return val
    return None
