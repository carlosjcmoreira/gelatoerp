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

        prompt = """Analisa esta fotografia de uma folha de planeamento de produção de gelado artesanal.

A folha tem uma linha por sabor (gelado) e 5 colunas numéricas:
1. PESAGEM MATOSINHOS — peso atual do gelado no início do dia (kg)
2. PRODUÇÃO BOLHÃO — kg produzidos para a loja Bolhão
3. PRODUÇÃO MATOSINHOS — kg produzidos para a loja Matosinhos
4. PRODUÇÃO MOUZINHO — kg produzidos para a loja Mouzinho
5. PRODUÇÃO B2B / OUTROS / EVENTOS — kg produzidos para B2B ou eventos

Extrai APENAS um objeto JSON válido (sem markdown, sem texto extra) com esta estrutura:

{
  "date": "YYYY-MM-DD",
  "confidence": 0.90,
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

REGRAS GERAIS:
- Se a data não for visível, usa null para "date".
- Para datas usa o formato YYYY-MM-DD.
- Se um valor estiver em branco ou ausente, usa 0.0.
- Se os valores estiverem claramente em gramas (ex: 1500), converte para kg (1.5).
- O nome do sabor deve ser exactamente como aparece escrito na folha.
- Inclui APENAS sabores com pelo menos um valor diferente de zero.
- "confidence" é a tua confiança global na extracção (0.0 a 1.0).
- Devolve APENAS o JSON, sem qualquer texto adicional.

ATENÇÃO — ESCRITA MANUAL (muito importante):
Esta folha é preenchida à mão. Sê extremamente cuidadoso com os seguintes erros frequentes de leitura:

DÍGITOS AMBÍGUOS — verifica sempre o contexto do número inteiro antes de decidir:
  • "1" vs "4": em manuscrito são frequentemente confundidos. O "1" tem traço recto; o "4" tem ângulo no topo.
  • "0" vs "8": o "0" é uma elipse simples; o "8" tem o nó a meio.
  • "3" vs "2": o "3" tem duas curvas à direita; o "2" tem base plana.
  • "5" vs "6": o "5" tem o topo plano; o "6" tem a cauda fechada em baixo.
  • "7" vs "4": ambos têm traço diagonal, mas o "7" é mais simples.
  • "9" vs "4": o "9" tem cauda descendente; o "4" tem ângulo no topo.

ZEROS/DÍGITOS INICIAIS — nunca elimines dígitos iniciais:
  • Valores como "2,476" NÃO devem ser lidos como "0,476" — verifica se existe um "2" antes da vírgula.
  • Valores escritos como "0,xxx" têm mesmo o zero no início; não os ignores.

VÍRGULA DECIMAL — o separador decimal é sempre a vírgula (formato português):
  • "1,906" = 1.906 kg (não "1906" nem "1,9")
  • "0,650" = 0.650 kg

SOMA DE PARCELAS (notação "N+M"):
  • Alguns valores podem estar escritos como "1,158+2,400" indicando duas pesagens.
  • Se encontrares esta notação, CALCULA o total e devolve apenas o número final (ex: 3.558).
  • Nunca devolvas uma string com "+" no JSON — apenas o resultado numérico.

ZEROS E TRAÇOS:
  • Um "0" ou "—" numa célula significa zero (0.0); não confundas com valor em falta.
  • Uma célula vazia é também 0.0.

CONTEXTO DO NÚMERO:
  • Os pesos de gelado nesta folha estão normalmente entre 0.0 e 10.0 kg por célula.
  • Valores acima de 15 kg numa única célula são muito raros — confirma se não é um erro de leitura.
  • Se tiveres dúvida entre dois dígitos, escolhe o que produz um valor no intervalo 0.0–10.0 kg."""

        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
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
    """Parse a numeric value that may be:
    - a number (int or float): returned directly.
    - a string with comma as decimal separator: e.g. "1,508" → 1.508
    - a simple addition expression: e.g. "1,508+4,815" → 6.323
    Returns 0.0 on any failure.
    """
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(',', '.')
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    import re
    parts = re.split(r'\+', s)
    try:
        total = sum(float(p.strip()) for p in parts if p.strip())
        return total
    except Exception:
        return 0.0


def _parse_date(val) -> str:
    if not val:
        return None
    val = str(val).strip()
    if len(val) == 10 and val[4] == '-':
        return val
    return None
