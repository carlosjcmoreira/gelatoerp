"""OCR service for end-of-day gelado weighing sheets (Rastreabilidade do Produto Pronto).

Uses Claude claude-sonnet-4-5 via the Anthropic API.  Each row of the first table
(Gelado) is extracted with three fields:
  • nome_produto   — product / flavour name as written on the sheet
  • peso_remanescente — remaining weight (kg, decimal comma notation)
  • peso_acrescido    — added weight (kg, or null if blank / zero / \"Ø\")

The total_kg per row is always   peso_remanescente + (peso_acrescido or 0).
The Granite/Altro section at the bottom of the form is ignored.
"""
from __future__ import annotations

import base64
import json
import logging

logger = logging.getLogger(__name__)

_PROMPT = r"""Analisa esta fotografia de uma folha de rastreabilidade NIVA' intitulada
"RASTREABILIDADE DO PRODUTO PRONTO: GELADO/OUTRO".

A folha tem uma tabela de gelado com as colunas:
  • "Nome do produto" — nome do sabor
  • "Peso remanescente" — peso restante em kg (vírgula como separador decimal, ex: 2,740)
  • "Peso acrescido"    — peso adicionado em kg, ou vazio / "Ø" / "0" / traço se não houve

Extrai APENAS as linhas da tabela de GELADO (a primeira tabela — ignora completamente a
secção "GRANITE/ALTRO" que aparece depois).

Devolve APENAS um objeto JSON válido (sem markdown, sem texto extra):

{
  "data": null,
  "confidence": 0.85,
  "linhas": [
    {
      "nome_produto": "Café",
      "peso_remanescente": 1.495,
      "peso_acrescido": null
    },
    {
      "nome_produto": "Cremino",
      "peso_remanescente": 2.740,
      "peso_acrescido": 2.855
    }
  ]
}

REGRAS GERAIS:
- "data" — data visível no topo da folha em formato YYYY-MM-DD. null se não visível.
- "confidence" — confiança global na extracção (0.0 a 1.0).
- "linhas" — uma entrada por linha da tabela de gelado que tenha pelo menos um valor.
- "nome_produto" — exactamente como está escrito, capitalização preservada.
- "peso_remanescente" — sempre um número. Se a célula mostrar "0" ou "Ø", usa 0.0.
- "peso_acrescido" — número se preenchido, null se vazio, traço ou "Ø".
- Nunca incluas linhas completamente vazias ou de cabeçalho.
- Ignora a secção "GRANITE/ALTRO" e tudo o que esteja abaixo dela.

ATENÇÃO — ESCRITA MANUAL (muito importante):
A folha é preenchida à mão. Lê com extremo cuidado:
- Separador decimal é sempre a VÍRGULA (português): "1,495" = 1.495 kg.
- "Ø" ou "0" numa célula de peso = 0.0 (não null).
- Célula de "Peso acrescido" vazia = null.
- Notação "+N,NNN" (ex: "+2,855") no campo "Peso acrescido" — usa apenas o número (2.855).
- Dígitos ambíguos frequentes: 1 vs 4, 0 vs 8, 3 vs 2, 7 vs 4.
- Valores típicos por célula: 0.0 a 8.0 kg. Valores acima de 15 kg são provavelmente erros.
- Nunca elimines dígitos à esquerda: "2,476" ≠ "0,476".

Devolve APENAS o JSON, sem qualquer texto adicional."""


from flask_app.ai_clients import anthropic_client as _get_client


def ocr_pesagem(image_bytes: bytes, filename: str = '') -> dict:
    """Extract weighing rows from a photo of a Rastreabilidade sheet.

    Returns:
        {
            'ok': True,
            'data': 'YYYY-MM-DD' or None,
            'confidence': float,
            'linhas': [
                {'nome_produto': str, 'peso_remanescente': float,
                 'peso_acrescido': float|None, 'total_kg': float},
                ...
            ],
            'error': None or str,
        }
    """
    client = _get_client()
    if not client:
        return _err("Serviço OCR não configurado (ANTHROPIC_API_KEY em falta)")

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'jpeg'
    media_map = {
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif',
    }
    media_type = media_map.get(ext, 'image/jpeg')

    if ext in ('heic', 'heif'):
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(image_bytes))
            buf = BytesIO()
            img.convert('RGB').save(buf, format='JPEG', quality=85)
            image_bytes = buf.getvalue()
            media_type = 'image/jpeg'
        except Exception as e:
            logger.warning("HEIC conversion failed: %s", e)
            media_type = 'image/jpeg'

    image_b64 = base64.standard_b64encode(image_bytes).decode('utf-8')

    try:
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
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )

        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        parsed = json.loads(raw)
        linhas_raw = parsed.get("linhas", [])
        linhas = []
        for row in linhas_raw:
            nome = str(row.get("nome_produto") or "").strip()
            if not nome:
                continue
            rem = _num(row.get("peso_remanescente"))
            acre = _num(row.get("peso_acrescido"))
            if rem is None:
                rem = 0.0
            total = round(rem + (acre or 0.0), 3)
            linhas.append({
                "nome_produto": nome,
                "peso_remanescente": rem,
                "peso_acrescido": acre,
                "total_kg": total,
            })

        return {
            "ok": True,
            "data": parsed.get("data"),
            "confidence": float(parsed.get("confidence", 0.7)),
            "linhas": linhas,
            "error": None,
        }

    except Exception as exc:
        logger.exception("pesagem OCR failed")
        return _err(str(exc))


def _num(val):
    if val is None:
        return None
    try:
        return float(str(val).replace(',', '.'))
    except Exception:
        return None


def _err(msg: str) -> dict:
    return {"ok": False, "data": None, "confidence": 0.0, "linhas": [], "error": msg}
