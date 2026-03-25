import json
from datetime import date
from db.connection import db_connection


def upsert_weather_data_batch(records: list):
    """Insert or update weather records. Each record must have store_id, fonte, data."""
    if not records:
        return
    with db_connection() as conn:
        cursor = conn.cursor()
        for rec in records:
            store_id = rec.get("store_id")
            fonte = rec.get("fonte")
            data_val = rec.get("data")
            if not (store_id and fonte and data_val):
                continue
            cursor.execute("""
                INSERT INTO weather_data
                    (store_id, fonte, data, temperatura_max, temperatura_min,
                     precipitacao_mm, vento_kmh, uv_index, condicao, score, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (store_id, fonte, data) DO UPDATE SET
                    temperatura_max = EXCLUDED.temperatura_max,
                    temperatura_min = EXCLUDED.temperatura_min,
                    precipitacao_mm = EXCLUDED.precipitacao_mm,
                    vento_kmh = EXCLUDED.vento_kmh,
                    uv_index = EXCLUDED.uv_index,
                    condicao = EXCLUDED.condicao,
                    score = EXCLUDED.score,
                    updated_at = NOW()
            """, (
                store_id, fonte, data_val,
                rec.get("temperatura_max"), rec.get("temperatura_min"),
                rec.get("precipitacao_mm"), rec.get("vento_kmh"),
                rec.get("uv_index"), rec.get("condicao"), rec.get("score")
            ))
        conn.commit()


def get_weather_data_for_store(store_id: int, data_inicio: date = None, data_fim: date = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        params = [store_id]
        where = "WHERE store_id = %s"
        if data_inicio:
            where += " AND data >= %s"
            params.append(data_inicio)
        if data_fim:
            where += " AND data <= %s"
            params.append(data_fim)
        cursor.execute(f"""
            SELECT id, store_id, fonte, data, temperatura_max, temperatura_min,
                   precipitacao_mm, vento_kmh, uv_index, condicao, score, updated_at
            FROM weather_data {where}
            ORDER BY data, fonte
        """, params)
        rows = cursor.fetchall()
    return [
        {
            'id': r[0], 'store_id': r[1], 'fonte': r[2], 'data': r[3],
            'temperatura_max': r[4], 'temperatura_min': r[5],
            'precipitacao_mm': r[6], 'vento_kmh': r[7],
            'uv_index': r[8], 'condicao': r[9], 'score': r[10], 'updated_at': r[11]
        } for r in rows
    ]


def get_weather_composite_by_store(store_id: int, data_inicio: date = None, data_fim: date = None) -> list:
    """Returns composite score per day for a store, with divergence indicator."""
    with db_connection() as conn:
        cursor = conn.cursor()
        params = [store_id]
        where = "WHERE store_id = %s AND score IS NOT NULL"
        if data_inicio:
            where += " AND data >= %s"
            params.append(data_inicio)
        if data_fim:
            where += " AND data <= %s"
            params.append(data_fim)
        cursor.execute(f"""
            SELECT data,
                   COUNT(DISTINCT fonte) AS num_fontes,
                   ROUND(AVG(score)::numeric, 1) AS composite_score,
                   ROUND((MAX(score) - MIN(score))::numeric, 1) AS divergencia,
                   ROUND(AVG(temperatura_max)::numeric, 1) AS temp_max_media,
                   ROUND(AVG(temperatura_min)::numeric, 1) AS temp_min_media,
                   ROUND(AVG(precipitacao_mm)::numeric, 1) AS precip_media
            FROM weather_data {where}
            GROUP BY data
            ORDER BY data
        """, params)
        rows = cursor.fetchall()
    return [
        {
            'data': r[0], 'num_fontes': r[1], 'composite_score': float(r[2]) if r[2] else None,
            'divergencia': float(r[3]) if r[3] else 0.0,
            'temp_max_media': float(r[4]) if r[4] else None,
            'temp_min_media': float(r[5]) if r[5] else None,
            'precip_media': float(r[6]) if r[6] else None,
        } for r in rows
    ]


def get_weather_data_all_stores_today() -> list:
    today = date.today()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT w.store_id, s.name as store_name, w.fonte, w.data,
                   w.temperatura_max, w.temperatura_min, w.precipitacao_mm,
                   w.vento_kmh, w.condicao, w.score, w.updated_at
            FROM weather_data w
            JOIN stores s ON w.store_id = s.id
            WHERE w.data = %s
            ORDER BY s.name, w.fonte
        """, (today,))
        rows = cursor.fetchall()
    return [
        {
            'store_id': r[0], 'store_name': r[1], 'fonte': r[2], 'data': r[3],
            'temperatura_max': r[4], 'temperatura_min': r[5], 'precipitacao_mm': r[6],
            'vento_kmh': r[7], 'condicao': r[8], 'score': r[9], 'updated_at': r[10]
        } for r in rows
    ]


def delete_weather_data_by_store_fonte(store_id: int, fonte: str = None,
                                        data_inicio: date = None, data_fim: date = None) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        params = [store_id]
        where = "WHERE store_id = %s"
        if fonte:
            where += " AND fonte = %s"
            params.append(fonte)
        if data_inicio:
            where += " AND data >= %s"
            params.append(data_inicio)
        if data_fim:
            where += " AND data <= %s"
            params.append(data_fim)
        cursor.execute(f"DELETE FROM weather_data {where}", params)
        deleted = cursor.rowcount
        conn.commit()
    return deleted


# ---------------------------------------------------------------------------
# Fase 6 — Histórico de Vendas: sales_historico
# ---------------------------------------------------------------------------

def add_sales_historico_batch(records: list) -> int:
    """Bulk insert historical sales records. Returns count inserted."""
    if not records:
        return 0
    inserted = 0
    with db_connection() as conn:
        cursor = conn.cursor()
        for rec in records:
            store_id = rec.get("store_id")
            pos_store_code = rec.get("pos_store_code")
            data_val = rec.get("data")
            loja = rec.get("loja", "")
            produto = rec.get("produto", "")
            categoria = rec.get("categoria", "")
            quantidade = rec.get("quantidade", 0)
            valor = rec.get("valor_euros", 0.0)
            ano = rec.get("ano_referencia")
            if not (data_val and produto and ano):
                continue
            cursor.execute("""
                INSERT INTO sales_historico
                    (store_id, pos_store_code, data, loja, produto, categoria,
                     quantidade, valor_euros, ano_referencia)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (store_id, pos_store_code, data_val, loja, produto, categoria,
                  quantidade, valor, ano))
            inserted += 1
        conn.commit()
    return inserted


def get_sales_historico_summary(ano_referencia: int = None, store_id: int = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        params = []
        where = "WHERE 1=1"
        if ano_referencia:
            where += " AND ano_referencia = %s"
            params.append(ano_referencia)
        if store_id:
            where += " AND store_id = %s"
            params.append(store_id)
        cursor.execute(f"""
            SELECT ano_referencia, loja, store_id,
                   COUNT(*) as total_registos,
                   SUM(quantidade) as total_qty,
                   SUM(valor_euros) as total_valor,
                   MIN(data) as data_inicio,
                   MAX(data) as data_fim
            FROM sales_historico {where}
            GROUP BY ano_referencia, loja, store_id
            ORDER BY ano_referencia DESC, loja
        """, params)
        rows = cursor.fetchall()
    return [
        {
            'ano_referencia': r[0], 'loja': r[1], 'store_id': r[2],
            'total_registos': r[3], 'total_qty': int(r[4]) if r[4] else 0,
            'total_valor': float(r[5]) if r[5] else 0.0,
            'data_inicio': r[6], 'data_fim': r[7]
        } for r in rows
    ]


def get_sales_historico_anos() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT ano_referencia FROM sales_historico ORDER BY ano_referencia DESC")
        return [r[0] for r in cursor.fetchall()]


def delete_sales_historico(ano_referencia: int, store_id: int = None) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        params = [ano_referencia]
        where = "WHERE ano_referencia = %s"
        if store_id:
            where += " AND store_id = %s"
            params.append(store_id)
        cursor.execute(f"DELETE FROM sales_historico {where}", params)
        deleted = cursor.rowcount
        conn.commit()
    return deleted


def get_sales_historico_yoy_by_month(store_id: int, ano_referencia: int) -> list:
    """Returns monthly totals from historical sales for YoY comparison."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT EXTRACT(MONTH FROM data)::int AS mes,
                   SUM(quantidade) AS total_qty,
                   SUM(valor_euros) AS total_valor,
                   COUNT(*) AS registos
            FROM sales_historico
            WHERE store_id = %s AND ano_referencia = %s
            GROUP BY mes
            ORDER BY mes
        """, (store_id, ano_referencia))
        rows = cursor.fetchall()
    return [
        {
            'mes': r[0], 'total_qty': int(r[1]) if r[1] else 0,
            'total_valor': float(r[2]) if r[2] else 0.0, 'registos': r[3]
        } for r in rows
    ]


def log_sales_historico_import(user_id, filename, ano_detetado, ano_formulario,
                                 registos_importados, linhas_ignoradas,
                                 lojas_importadas, erros_loja, notas=None):
    """Record a historico import operation in sales_historico_import_log."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sales_historico_import_log
                (user_id, filename, ano_detetado, ano_formulario,
                 registos_importados, linhas_ignoradas,
                 lojas_importadas, erros_loja, notas)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            user_id, filename, ano_detetado, ano_formulario,
            registos_importados, linhas_ignoradas,
            json.dumps(lojas_importadas), json.dumps(erros_loja), notas
        ))
        conn.commit()


def get_sales_historico_import_logs(limit: int = 10) -> list:
    """Returns recent import log entries, newest first."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT l.id, l.created_at, l.filename, l.ano_detetado, l.ano_formulario,
                   l.registos_importados, l.linhas_ignoradas,
                   l.lojas_importadas, l.erros_loja, l.notas,
                   u.username
            FROM sales_historico_import_log l
            LEFT JOIN users u ON l.user_id = u.id
            ORDER BY l.created_at DESC
            LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
    return [
        {
            'id': r[0],
            'created_at': r[1],
            'filename': r[2],
            'ano_detetado': r[3],
            'ano_formulario': r[4],
            'registos_importados': r[5],
            'linhas_ignoradas': r[6],
            'lojas_importadas': r[7] if isinstance(r[7], list) else json.loads(r[7] or '[]'),
            'erros_loja': r[8] if isinstance(r[8], list) else json.loads(r[8] or '[]'),
            'notas': r[9],
            'username': r[10],
        }
        for r in rows
    ]
