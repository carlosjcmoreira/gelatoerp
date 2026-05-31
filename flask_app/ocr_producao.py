import os
import base64
import json
import logging
from datetime import date

logger = logging.getLogger(__name__)

AI_INTEGRATIONS_ANTHROPIC_API_KEY = os.environ.get("AI_INTEGRATIONS_ANTHROPIC_API_KEY")
AI_INTEGRATIONS_ANTHROPIC_BASE_URL = os.environ.get("AI_INTEGRATIONS_ANTHROPIC_BASE_URL")

_CONFIDENCE_RETRY_THRESHOLD = 0.75

_PRODUCAO_PROMPT = """Analisa esta fotografia de uma folha de planeamento de produção de gelado artesanal.

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
      "prod_b2b": 0.0,
      "confidence": 0.85
    }
  }
}

REGRAS GERAIS:
- Se a data não for visível, usa null para "date".
- Para datas usa o formato YYYY-MM-DD.
- Se um valor estiver em branco ou ausente, usa 0.0.
- O nome do sabor deve ser exactamente como aparece escrito na folha (ver tabela de abreviações abaixo).
- Inclui APENAS sabores com pelo menos um valor diferente de zero.
- "confidence" global é a tua confiança na extracção completa (0.0 a 1.0).
- "confidence" por sabor é a tua confiança na leitura daquele sabor específico (0.0 a 1.0).
- Devolve APENAS o JSON, sem qualquer texto adicional.

LEITURA OBRIGATÓRIA — COLUNA POR COLUNA:
A folha tem EXACTAMENTE 5 colunas numéricas, separadas por linhas verticais.
Para cada linha (sabor), conta as colunas rigorosamente da esquerda para a direita:
  • 1ª coluna numérica (mais à esquerda)  → pesagem_mat
  • 2ª coluna numérica                    → prod_bolhao
  • 3ª coluna numérica                    → prod_matosinhos
  • 4ª coluna numérica                    → prod_mouzinho
  • 5ª coluna numérica (mais à direita)   → prod_b2b

ATENÇÃO CRÍTICA — COLUNAS FREQUENTEMENTE VAZIAS:
As colunas PRODUÇÃO MOUZINHO (4ª) e PRODUÇÃO B2B (5ª) estão frequentemente em branco.
Se não existir nenhum número claramente escrito nessas colunas para um sabor, usa 0.0.
NÃO coloques valores nessas colunas a não ser que haja números claramente visíveis e escritos.
Quando em dúvida se um número pertence à 3ª ou 4ª coluna, verifica o alinhamento vertical
com outros números na mesma coluna noutras linhas. Uma coluna vazia mantém-se vazia em TODAS as linhas.

FORMATO DOS NÚMEROS — MUITO IMPORTANTE:
A folha usa dois formatos mistos na mesma página. Aplica estas regras sem excepção:

  Formato A — COM vírgula decimal: é sempre kg directamente
    ex: "1,906" → 1.906 kg | "4,525" → 4.525 kg | "0,650" → 0.650 kg

  Formato B — inteiro SEM vírgula: está sempre em gramas, divide por 1000
    ex: "4525"  → 4.525 kg | "1774" → 1.774 kg | "3936" → 3.936 kg
        "4205"  → 4.205 kg | "4210" → 4.210 kg | "650"  → 0.650 kg

  Inteiro pequeno < 100: é kg directamente
    ex: "8" → 8.0 kg | "12" → 12.0 kg | "45" → 45.0 kg

NÃO apliques nenhum limite máximo de kg por célula.
NÃO descartes nem modifiques valores por parecerem "demasiado altos".
Lê todos os números exactamente como aparecem e aplica apenas as regras acima.

NOMES DE SABORES — ABREVIAÇÕES CONHECIDAS:
Usa sempre o nome completo no JSON, mesmo que a folha use abreviações:
  • "Stroc. Ruby" / "Strac. Ruby" / "Stracciatella R." → "Stracciatella Ruby"
  • "Stracciatella" (sem Ruby) → "Stracciatella"
  • "Pistac." / "Pistachio" → "Pistacchio"
  • "Pist. V." / "Pistac. V." / "Pistachio V." / "Pistacchio V." / "Pistacchio Vegan" → "Pistacchio V."
  • "Noc." / "Nocciola" / "Nocciolato" → "Nocciolato"
  • "Fior de Leite" / "Flor de Leite" / "Fior di Latte" → "Fior di Latte"
  • "Arachide" → "Amendoim"
  • "Tiramisu" / "Tiramisù" / "Jiromisu" / "Tiromisu" → "Tiramisu"
  • "Choc. Branco" → "Chocolate Branco"
  • "Dulce de Leche" / "Doce Leite" → "Doce de Leite"
  • "Extra Noir" → "Extra Noir"
  • "Ricot. Noz Mel" / "Ricota Noz Mel" → "Ricota, Noz e Mel"
  • "Noz Pecan Maple" / "Noc. Pecan Maple" → "Noz Pecan e Maple"
  • "Stroc. Ruby" / "Strac. Ruby" / "Stracciatella Ruby" → "Stracciatella Ruby"

ATENÇÃO — ESCRITA MANUAL:
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

SOMA DE PARCELAS (notação "N+M") — REGRA CRÍTICA:
  • Alguns valores estão escritos como "1,158+2,400" ou "4205+4210" indicando duas pesagens somadas.
  • A soma INTEIRA pertence à coluna onde foi escrita — NÃO dividas as parcelas por colunas diferentes.
  • CORRECTO: "2118+4108" na coluna PESAGEM → pesagem_mat = 2.118+4.108 = 6.226 (prod_bolhao = 0.0)
  • ERRADO:   "2118+4108" na coluna PESAGEM → pesagem_mat = 2.118, prod_bolhao = 4.108  ← NÃO FAÇAS ISTO
  • CORRECTO: "906+4045" na coluna PESAGEM  → pesagem_mat = 0.906+4.045 = 4.951 (prod_bolhao = 0.0)
  • Aplica as regras de formato (inteiro=gramas÷1000, vírgula=kg directo) a cada parcela antes de somar.
  • Nunca devolvas uma string com "+" no JSON — apenas o resultado numérico final.
  • O número após "+" é sempre parte da mesma célula — não pertence à coluna seguinte.

NOTAÇÃO "(TSF)" E ANOTAÇÕES ENTRE PARÊNTESES:
  • Valores seguidos de "(TSF)", "(Transf.)", "(T)" ou anotações similares indicam transferências.
  • Lê o número normalmente e ignora a anotação entre parênteses.
  • ex: "4108(TSF)" → 4.108 kg (inteiro sem vírgula → divide por 1000)
  • ex: "7660(TSF)" → 7.660 kg

ZEROS E TRAÇOS:
  • Um "0" ou "—" numa célula significa zero (0.0); não confundas com valor em falta.
  • Uma célula vazia é também 0.0."""


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
              'confidence': float,  # per-sabor OCR confidence (0.0–1.0)
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

        result = _single_ocr_call(client, image_b64, media_type)

        if not result.get('error') and result.get('ocr_confidence', 1.0) < _CONFIDENCE_RETRY_THRESHOLD:
            logger.info(
                "OCR confidence %.3f below threshold %.2f — retrying",
                result['ocr_confidence'], _CONFIDENCE_RETRY_THRESHOLD,
            )
            retry = _single_ocr_call(client, image_b64, media_type)
            if not retry.get('error') and retry.get('ocr_confidence', 0) > result.get('ocr_confidence', 0):
                logger.info(
                    "OCR retry improved confidence: %.3f → %.3f",
                    result['ocr_confidence'], retry['ocr_confidence'],
                )
                result = retry

        return result

    except Exception as e:
        logger.error("OCR producao extraction failed: %s", e)
        return _empty_result(str(e))


_SYSTEM_PROMPT = (
    "És um especialista em leitura e extracção de dados de tabelas manuscritas "
    "de produção de gelado artesanal. A tua tarefa é extrair valores numéricos "
    "de células de uma grelha, respeitando rigorosamente as fronteiras das colunas. "
    "Devolves SEMPRE JSON válido, sem texto adicional."
)


def _single_ocr_call(client, image_b64: str, media_type: str) -> dict:
    """Make a single OCR API call and return a parsed result dict."""
    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=_SYSTEM_PROMPT,
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
                    {"type": "text", "text": _PRODUCAO_PROMPT}
                ]
            }]
        )

        actual_model = getattr(message, 'model', 'unknown')
        logger.info("OCR producao: model requested=claude-sonnet-4-5 actual=%s", actual_model)

        raw_text = message.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        parsed = json.loads(raw_text)
        global_confidence = float(parsed.get('confidence', 0.7))

        from sabor_utils import normalise_sabor
        sabores_raw = parsed.get('sabores', {})
        sabores = {}
        for nome, vals in sabores_raw.items():
            if not isinstance(vals, dict):
                continue
            canonical = normalise_sabor(nome)
            sabor_conf = float(vals.get('confidence', global_confidence))
            entry = {
                'pesagem_mat': _parse_float(vals.get('pesagem_mat', 0)),
                'prod_bolhao': _parse_float(vals.get('prod_bolhao', 0)),
                'prod_matosinhos': _parse_float(vals.get('prod_matosinhos', 0)),
                'prod_mouzinho': _parse_float(vals.get('prod_mouzinho', 0)),
                'prod_b2b': _parse_float(vals.get('prod_b2b', 0)),
                'confidence': round(sabor_conf, 3),
            }
            if canonical in sabores:
                existing = sabores[canonical]
                merged = {k: existing[k] + entry[k] for k in entry if k != 'confidence'}
                merged['confidence'] = round(min(existing['confidence'], entry['confidence']), 3)
                sabores[canonical] = merged
            else:
                sabores[canonical] = entry

        sabores = _sanitise_kg_values(sabores)
        sabores = _detect_column_shift(sabores)

        return {
            'date': _parse_date(parsed.get('date')),
            'sabores': sabores,
            'error': None,
            'ocr_confidence': round(global_confidence, 3),
        }

    except Exception as e:
        logger.error("_single_ocr_call failed: %s", e)
        return _empty_result(str(e))


def _sanitise_kg_values(sabores: dict) -> dict:
    """Auto-correct kg values that are clearly in grams (OCR missed the /1000 conversion).

    Rule: if a numeric field value V > 50 and V/1000 is in [0.1, 25],
    replace V with V/1000, log a WARNING, and cap that sabor's confidence at 0.5.

    Upper bound is 25 kg to handle large pesagem values (e.g. Pistacchio: 17+ kg stockroom weight).
    """
    _KG_FIELDS = ('pesagem_mat', 'prod_bolhao', 'prod_matosinhos', 'prod_mouzinho', 'prod_b2b')
    _MAX_PLAUSIBLE_KG = 50.0
    _MIN_CORRECTED_KG = 0.1
    _MAX_CORRECTED_KG = 25.0

    for nome, vals in sabores.items():
        corrected_fields = []
        for field in _KG_FIELDS:
            v = vals.get(field, 0.0)
            if v > _MAX_PLAUSIBLE_KG:
                corrected = v / 1000.0
                if _MIN_CORRECTED_KG <= corrected <= _MAX_CORRECTED_KG:
                    logger.warning(
                        "OCR kg sanity: sabor=%r field=%s value=%.3f > %g kg — "
                        "auto-correcting to %.3f kg (÷1000). Manual review recommended.",
                        nome, field, v, _MAX_PLAUSIBLE_KG, corrected,
                    )
                    vals[field] = round(corrected, 3)
                    corrected_fields.append(field)
        if corrected_fields:
            vals['confidence'] = min(vals.get('confidence', 1.0), 0.5)

    return sabores


def _detect_column_shift(sabores: dict) -> dict:
    """Detect and correct systematic column-shift errors.

    When a model mis-aligns columns by one position, many sabores end up with
    prod_mouzinho > 0 while prod_bolhao ≈ 0 (or vice-versa for other shifts).

    Heuristic: if ≥70% of sabores with any production value have prod_mouzinho > 0
    but prod_bolhao == 0, this is a strong signal that Matosinhos values landed in
    the Mouzinho column and Bolhão values landed in the Matosinhos column.
    In that case, shift: bolhao←matosinhos, matosinhos←mouzinho, mouzinho←b2b, b2b←0.
    """
    prod_fields = ('prod_bolhao', 'prod_matosinhos', 'prod_mouzinho', 'prod_b2b')
    sabores_with_prod = [
        v for v in sabores.values()
        if any(v.get(f, 0) > 0 for f in prod_fields)
    ]

    if len(sabores_with_prod) < 3:
        return sabores

    mouzinho_positive = sum(1 for v in sabores_with_prod if v.get('prod_mouzinho', 0) > 0)
    bolhao_zero = sum(1 for v in sabores_with_prod if v.get('prod_bolhao', 0) == 0)
    total = len(sabores_with_prod)

    if (mouzinho_positive / total) >= 0.70 and (bolhao_zero / total) >= 0.70:
        logger.warning(
            "OCR column-shift detected: %d/%d sabores have prod_mouzinho>0 but prod_bolhao==0. "
            "Shifting columns: matosinhos→bolhao, mouzinho→matosinhos, b2b→mouzinho, 0→b2b.",
            mouzinho_positive, total,
        )
        for vals in sabores.values():
            old_bolhao = vals.get('prod_bolhao', 0)
            old_mat = vals.get('prod_matosinhos', 0)
            old_mouz = vals.get('prod_mouzinho', 0)
            old_b2b = vals.get('prod_b2b', 0)
            vals['prod_bolhao'] = old_mat
            vals['prod_matosinhos'] = old_mouz
            vals['prod_mouzinho'] = old_b2b
            vals['prod_b2b'] = 0.0
            if old_bolhao > 0:
                logger.warning(
                    "OCR column-shift: discarding prod_bolhao=%.3f that was in wrong position.",
                    old_bolhao,
                )
            vals['confidence'] = min(vals.get('confidence', 1.0), 0.4)

    return sabores


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
