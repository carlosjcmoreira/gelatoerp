import json
import logging
from calendar import monthrange
from datetime import date
from db.connection import get_connection, release_connection

logger = logging.getLogger(__name__)

_COLS = (
    'id', 'data', 'loja_id', 'loja', 'colaborador',
    'moedas_json', 'total_moedas', 'valor_notas', 'total_caixa', 'envelope_sobra',
    'total_vendas_pos', 'dinheiro_pos', 'cartao_pos', 'ubereats_pos', 'tpa_getnet',
    'desvio_numerario', 'desvio_tpa',
    'justificacao_desvio', 'imagem_caixa_path', 'ocr_confianca', 'registado_por',
)

_ALLOWED_SET = (
    'colaborador', 'moedas_json',
    'total_moedas', 'valor_notas', 'total_caixa', 'envelope_sobra',
    'total_vendas_pos', 'dinheiro_pos', 'cartao_pos', 'ubereats_pos', 'tpa_getnet',
    'imagem_caixa_path', 'ocr_raw', 'ocr_confianca',
)


def _get_loja_name(cur, loja_id: int) -> str | None:
    cur.execute("SELECT name FROM stores WHERE id = %s LIMIT 1", (loja_id,))
    row = cur.fetchone()
    return row[0] if row else None


def upsert_fecho_caixa(data: date, loja_id: int, fields: dict, registado_por: str = None) -> dict:
    """
    Insert or update a fecho_caixa record for the given date+loja.
    Supported keys in fields: colaborador, moedas_json, total_moedas, valor_notas,
    total_caixa, envelope_sobra, total_vendas_pos, dinheiro_pos, cartao_pos,
    ubereats_pos, tpa_getnet, imagem_caixa_path, ocr_raw, ocr_confianca.
    Returns the saved row as dict.
    """
    set_parts = []
    values = []
    insert_cols = []
    insert_vals = []

    for col in _ALLOWED_SET:
        if col not in fields:
            continue
        if col == 'ocr_raw':
            continue
        val = fields[col]
        if col == 'moedas_json' and isinstance(val, (dict, list)):
            val = json.dumps(val)
        set_parts.append(f"{col} = %s")
        values.append(val)
        insert_cols.append(col)
        insert_vals.append(val)

    set_parts.append("registado_por = %s")
    values.append(registado_por)
    set_parts.append("updated_at = NOW()")

    select_cols = ', '.join(_COLS)

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Resolve loja name so it is present on both INSERT and UPDATE
        loja_nome = _get_loja_name(cur, loja_id)
        set_parts.append("loja = %s")
        values.append(loja_nome)

        col_names = ', '.join(['data', 'loja_id', 'loja', 'registado_por'] + insert_cols)
        placeholders = ', '.join(['%s'] * (4 + len(insert_cols)))
        all_insert_vals = [data, loja_id, loja_nome, registado_por] + insert_vals
        update_str = ', '.join(set_parts)

        cur.execute(f"""
            INSERT INTO fecho_caixa ({col_names})
            VALUES ({placeholders})
            ON CONFLICT (data, loja_id) DO UPDATE SET {update_str}
            RETURNING {select_cols}
        """, all_insert_vals + values)
        row = cur.fetchone()
        conn.commit()
        return _row_to_dict(row) if row else {}
    except Exception as e:
        conn.rollback()
        logger.error("upsert_fecho_caixa failed: %s", e)
        raise
    finally:
        release_connection(conn)


def get_fecho_caixa(data: date, loja_id: int) -> dict | None:
    """Return the fecho_caixa record for a given date+loja, or None."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(_COLS)} FROM fecho_caixa WHERE data = %s AND loja_id = %s",
                    (data, loja_id))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        release_connection(conn)


def get_fecho_caixa_mensal(loja_id: int, ano: int, mes: int) -> list:
    """
    Return one dict per day of the month. Days with no record get an empty placeholder dict.
    Existing records are merged in by date.
    """
    from datetime import timedelta
    num_days = monthrange(ano, mes)[1]
    first = date(ano, mes, 1)
    last = date(ano, mes, num_days)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_COLS)} FROM fecho_caixa WHERE loja_id = %s AND data BETWEEN %s AND %s ORDER BY data",
            (loja_id, first, last)
        )
        rows_by_date = {r['data']: r for r in (_row_to_dict(row) for row in cur.fetchall())}
    finally:
        release_connection(conn)

    result = []
    for i in range(num_days):
        d = first + timedelta(days=i)
        if d in rows_by_date:
            result.append(rows_by_date[d])
        else:
            result.append({'data': d, 'data_str': str(d), '_empty': True})
    return result


def get_fecho_caixa_by_id(fecho_id: int) -> dict | None:
    """Return a fecho_caixa record by primary key, or None."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(_COLS)} FROM fecho_caixa WHERE id = %s", (fecho_id,))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        release_connection(conn)


def salvar_justificacao_fecho(fecho_id: int, justificacao: str) -> bool:
    """Save/update the deviation justification for a fecho_caixa row."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE fecho_caixa SET justificacao_desvio = %s, updated_at = NOW() WHERE id = %s",
            (justificacao, fecho_id)
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        conn.rollback()
        logger.error("salvar_justificacao_fecho failed: %s", e)
        return False
    finally:
        release_connection(conn)


def _row_to_dict(row) -> dict:
    d = dict(zip(_COLS, row))
    for k in ('total_moedas', 'valor_notas', 'total_caixa', 'envelope_sobra',
               'total_vendas_pos', 'dinheiro_pos', 'cartao_pos', 'ubereats_pos',
               'tpa_getnet', 'desvio_numerario', 'desvio_tpa', 'ocr_confianca'):
        if d.get(k) is not None:
            d[k] = float(d[k])
    if d.get('data'):
        d['data_str'] = str(d['data'])
    if isinstance(d.get('moedas_json'), str):
        try:
            d['moedas_json'] = json.loads(d['moedas_json'])
        except Exception:
            d['moedas_json'] = {}
    d.setdefault('_empty', False)
    return d
