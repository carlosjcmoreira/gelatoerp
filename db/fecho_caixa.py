import json
import logging
from calendar import monthrange
from datetime import date
from db.connection import db_connection

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
    'justificacao_desvio',
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

    try:
        with db_connection() as conn:
            cur = conn.cursor()

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
        logger.error("upsert_fecho_caixa failed: %s", e)
        raise


def get_fecho_caixa(data: date, loja_id: int) -> dict | None:
    """Return the fecho_caixa record for a given date+loja, or None."""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(_COLS)} FROM fecho_caixa WHERE data = %s AND loja_id = %s",
                    (data, loja_id))
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def get_fecho_caixa_mensal(loja_id: int, ano: int, mes: int) -> list:
    """
    Return one dict per day of the month. Days with no record get an empty placeholder dict.
    Existing records are merged in by date.
    """
    from datetime import timedelta
    num_days = monthrange(ano, mes)[1]
    first = date(ano, mes, 1)
    last = date(ano, mes, num_days)
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_COLS)} FROM fecho_caixa WHERE loja_id = %s AND data BETWEEN %s AND %s ORDER BY data",
            (loja_id, first, last)
        )
        rows_by_date = {r['data']: r for r in (_row_to_dict(row) for row in cur.fetchall())}

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
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(_COLS)} FROM fecho_caixa WHERE id = %s", (fecho_id,))
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def list_fecho_caixa(loja_id: int) -> list:
    """Return all fecho_caixa records for a loja, ordered by date descending."""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_COLS)} FROM fecho_caixa WHERE loja_id = %s ORDER BY data DESC",
            (loja_id,)
        )
        return [_row_to_dict(row) for row in cur.fetchall()]


def list_fecho_caixa_all_stores(
    loja_id: int | None = None,
    data_inicio: date | None = None,
    data_fim: date | None = None,
) -> list:
    """Return fecho_caixa records across all stores, with optional filters.

    Args:
        loja_id: If given, restrict to this store.
        data_inicio: If given, only records on or after this date.
        data_fim: If given, only records on or before this date.

    Returns records ordered by date descending, then by store name.
    """
    conditions = []
    params = []

    if loja_id is not None:
        conditions.append("loja_id = %s")
        params.append(loja_id)
    if data_inicio is not None:
        conditions.append("data >= %s")
        params.append(data_inicio)
    if data_fim is not None:
        conditions.append("data <= %s")
        params.append(data_fim)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_COLS)} FROM fecho_caixa {where_clause} ORDER BY data DESC, loja ASC",
            params,
        )
        return [_row_to_dict(row) for row in cur.fetchall()]


def delete_fecho_caixa_by_id(record_id: int) -> bool:
    """Delete a fecho_caixa record by primary key. Returns True if a row was deleted."""
    try:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM fecho_caixa WHERE id = %s", (record_id,))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        logger.error("delete_fecho_caixa_by_id failed: %s", e)
        raise


def _audit_json(valores_anteriores: dict) -> str:
    """Serialise a fecho_caixa row dict to a JSON string safe for JSONB insertion."""
    import decimal

    def _default(obj):
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    return json.dumps(valores_anteriores, default=_default)


def _write_audit_cur(cur, fecho_caixa_id: int, acao: str, utilizador: str, audit_json: str) -> None:
    """Insert one audit row using an existing cursor (caller must commit)."""
    cur.execute("""
        INSERT INTO fecho_caixa_audit (fecho_caixa_id, acao, utilizador, valores_anteriores)
        VALUES (%s, %s, %s, %s)
    """, (fecho_caixa_id, acao, utilizador, audit_json))


def delete_fecho_caixa_audited(record_id: int, utilizador: str, valores_anteriores: dict) -> bool:
    """Delete a fecho_caixa record and write an audit row in a single transaction.

    Raises on failure so the caller knows neither the delete nor the audit row
    was persisted.
    """
    audit_json = _audit_json(valores_anteriores)
    try:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM fecho_caixa WHERE id = %s", (record_id,))
            deleted = cur.rowcount > 0
            if deleted:
                _write_audit_cur(cur, record_id, 'delete', utilizador, audit_json)
            conn.commit()
            return deleted
    except Exception as e:
        logger.error("delete_fecho_caixa_audited failed: %s", e)
        raise


def upsert_fecho_caixa_audited(data, loja_id: int, fields: dict, utilizador: str,
                                fecho_id: int, valores_anteriores: dict) -> dict:
    """Upsert a fecho_caixa record and write an 'edit' audit row in a single transaction.

    fecho_id and valores_anteriores must reflect the existing record (captured
    before calling this function).  Raises on failure.
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
    values.append(utilizador)
    set_parts.append("updated_at = NOW()")

    select_cols = ', '.join(_COLS)
    audit_json = _audit_json(valores_anteriores)

    try:
        with db_connection() as conn:
            cur = conn.cursor()

            loja_nome = _get_loja_name(cur, loja_id)
            set_parts.append("loja = %s")
            values.append(loja_nome)

            col_names = ', '.join(['data', 'loja_id', 'loja', 'registado_por'] + insert_cols)
            placeholders = ', '.join(['%s'] * (4 + len(insert_cols)))
            all_insert_vals = [data, loja_id, loja_nome, utilizador] + insert_vals
            update_str = ', '.join(set_parts)

            cur.execute(f"""
                INSERT INTO fecho_caixa ({col_names})
                VALUES ({placeholders})
                ON CONFLICT (data, loja_id) DO UPDATE SET {update_str}
                RETURNING {select_cols}
            """, all_insert_vals + values)
            row = cur.fetchone()

            _write_audit_cur(cur, fecho_id, 'edit', utilizador, audit_json)
            conn.commit()
        return _row_to_dict(row) if row else {}
    except Exception as e:
        logger.error("upsert_fecho_caixa_audited failed: %s", e)
        raise


def get_fecho_caixa_audit(fecho_caixa_id: int) -> list:
    """Return audit entries for a fecho_caixa record, newest first."""
    try:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, fecho_caixa_id, acao, utilizador, timestamp, valores_anteriores
                FROM fecho_caixa_audit
                WHERE fecho_caixa_id = %s
                ORDER BY timestamp DESC
            """, (fecho_caixa_id,))
            rows = cur.fetchall()
        result = []
        for row in rows:
            entry = {
                'id': row[0],
                'fecho_caixa_id': row[1],
                'acao': row[2],
                'utilizador': row[3],
                'timestamp': row[4],
                'valores_anteriores': row[5] if isinstance(row[5], dict) else json.loads(row[5]),
            }
            result.append(entry)
        return result
    except Exception as e:
        logger.error("get_fecho_caixa_audit failed: %s", e)
        return []


def salvar_justificacao_fecho(fecho_id: int, justificacao: str) -> bool:
    """Save/update the deviation justification for a fecho_caixa row."""
    try:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE fecho_caixa SET justificacao_desvio = %s, updated_at = NOW() WHERE id = %s",
                (justificacao, fecho_id)
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        logger.error("salvar_justificacao_fecho failed: %s", e)
        return False


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
