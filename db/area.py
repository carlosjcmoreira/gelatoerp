import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger

def get_ultimo_stock_balcao(area: str) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'pastelaria':
            cursor.execute("""
                SELECT pg_advisory_xact_lock(
                    hashtextextended('pastelaria-count:' || data::text, 0)
                )
                FROM (
                    SELECT DISTINCT data
                    FROM contagem_stock
                    WHERE tipo='pastelaria'
                      AND EXTRACT(DOW FROM data)=0
                ) sundays
            """)
        if area == 'pastelaria':
            cursor.execute("""
                WITH latest_identity AS (
                    SELECT DISTINCT ON (
                               COALESCE(cs.produto_pastelaria_id::text, 'text:' || cs.produto),
                               cs.loja
                           )
                           COALESCE(
                               NULLIF(CONCAT_WS(', ',
                                   NULLIF(BTRIM(p.tipologia), ''),
                                   NULLIF(BTRIM(p.sabor), ''),
                                   NULLIF(BTRIM(p.cobertura), '')
                               ), ''),
                               cs.produto
                           ) AS produto,
                           cs.loja, cs.quantidade, cs.data, cs.id,
                           cs.produto_pastelaria_id
                    FROM contagem_stock cs
                    LEFT JOIN produtos_pastelaria p
                      ON p.id=cs.produto_pastelaria_id
                    WHERE cs.tipo = %s
                      AND cs.origem = 'contagem'
                    ORDER BY COALESCE(
                                 cs.produto_pastelaria_id::text,
                                 'text:' || cs.produto
                             ),
                             cs.loja, cs.data DESC, cs.id DESC
                )
                SELECT DISTINCT ON (produto, loja)
                       produto, loja, quantidade, data
                FROM latest_identity
                ORDER BY produto, loja,
                         (produto_pastelaria_id IS NOT NULL) DESC,
                         data DESC, id DESC
            """, (area,))
        else:
            cursor.execute("""
                SELECT DISTINCT ON (produto, loja) produto, loja, quantidade, data
                FROM contagem_stock
                WHERE tipo = %s
                ORDER BY produto, loja, data DESC, id DESC
            """, (area,))
        rows = cursor.fetchall()
    return [{'produto': r[0], 'loja': r[1], 'quantidade': int(r[2]), 'data': r[3]} for r in rows]


def _plano_table(area: str) -> str:
    if area == 'pastelaria':
        return 'plano_producao_pastelaria'
    return 'plano_producao_confeitaria'


def _stock_prod_table(area: str) -> str:
    if area == 'pastelaria':
        return 'stock_producao_pastelaria'
    return 'stock_producao_confeitaria'


def _resolve_pastelaria_product_id(cursor, produto: str):
    cursor.execute("""
        SELECT MIN(id)
        FROM produtos_pastelaria
        WHERE CONCAT_WS(', ',
            NULLIF(BTRIM(tipologia), ''),
            NULLIF(BTRIM(sabor), ''),
            NULLIF(BTRIM(cobertura), '')
        ) = %s
        HAVING COUNT(*) = 1
    """, (produto,))
    row = cursor.fetchone()
    return row[0] if row else None


def _pastelaria_write_product_id(cursor, produto: str):
    product_id = _resolve_pastelaria_product_id(cursor, produto)
    if product_id is None and not produto.startswith('Bolo — '):
        raise ValueError(
            f'Produto de Pastelaria sem identidade única: {produto}'
        )
    return product_id


def upsert_plano_area(area: str, data: date, produto: str, producao_estimada: int,
                      estimada_bolhao: int = 0, estimada_matosinhos: int = 0,
                      item_tipo: str = 'standard', bolo_tamanho: str = None,
                      bolo_sabor_1: str = None, bolo_sabor_2: str = None,
                      bolo_sabor_3: str = None, bolo_cobertura: str = None,
                      nota: str = None):
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'pastelaria':
            product_id = _pastelaria_write_product_id(cursor, produto)
            conflict_target = (
                "(data, produto_pastelaria_id) "
                "WHERE produto_pastelaria_id IS NOT NULL"
                if product_id is not None else
                "(data, produto) WHERE produto_pastelaria_id IS NULL"
            )
            conflict_sql = f"""
                ON CONFLICT {conflict_target}
                DO UPDATE SET
                    producao_estimada = EXCLUDED.producao_estimada,
                    producao_estimada_bolhao =
                        EXCLUDED.producao_estimada_bolhao,
                    producao_estimada_matosinhos =
                        EXCLUDED.producao_estimada_matosinhos,
                    item_tipo = EXCLUDED.item_tipo,
                    bolo_tamanho = EXCLUDED.bolo_tamanho,
                    bolo_sabor_1 = EXCLUDED.bolo_sabor_1,
                    bolo_sabor_2 = EXCLUDED.bolo_sabor_2,
                    bolo_sabor_3 = EXCLUDED.bolo_sabor_3,
                    bolo_cobertura = EXCLUDED.bolo_cobertura,
                    nota = COALESCE(EXCLUDED.nota, {tbl}.nota),
                    updated_at = NOW()
            """
            cursor.execute(f"""
                INSERT INTO {tbl} (
                    data, produto, producao_estimada,
                    producao_estimada_bolhao, producao_estimada_matosinhos,
                    item_tipo, bolo_tamanho, bolo_sabor_1, bolo_sabor_2,
                    bolo_sabor_3, bolo_cobertura, nota,
                    produto_pastelaria_id
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s
                )
                {conflict_sql}
            """, (data, produto, producao_estimada, estimada_bolhao,
                  estimada_matosinhos, item_tipo, bolo_tamanho, bolo_sabor_1,
                  bolo_sabor_2, bolo_sabor_3, bolo_cobertura, nota, product_id))
        else:
            cursor.execute(f"""
                INSERT INTO {tbl} (data, produto, producao_estimada)
                VALUES (%s, %s, %s)
                ON CONFLICT (data, produto) DO UPDATE SET
                    producao_estimada = EXCLUDED.producao_estimada,
                    updated_at = NOW()
            """, (data, produto, producao_estimada))
        conn.commit()


def marcar_produto_no_plano(area: str, data: date, produto: str):
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        product_id = (
            _resolve_pastelaria_product_id(cursor, produto)
            if area == 'pastelaria' else None
        )
        identity_clause = (
            "produto_pastelaria_id = %s" if product_id is not None
            else "produto = %s"
        )
        cursor.execute(f"""
            UPDATE {tbl} SET no_plano = TRUE, updated_at = NOW()
            WHERE data = %s AND {identity_clause}
        """, (data, product_id if product_id is not None else produto))
        conn.commit()


def remover_produto_do_plano(area: str, data: date, produto: str):
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        product_id = (
            _resolve_pastelaria_product_id(cursor, produto)
            if area == 'pastelaria' else None
        )
        identity_clause = (
            "produto_pastelaria_id = %s" if product_id is not None
            else "produto = %s"
        )
        cursor.execute(f"""
            UPDATE {tbl} SET no_plano = FALSE, updated_at = NOW()
            WHERE data = %s AND {identity_clause}
        """, (data, product_id if product_id is not None else produto))
        conn.commit()


def get_plano_do_dia_area(area: str, data: date) -> list:
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'pastelaria':
            cursor.execute(f"""
                SELECT COALESCE(
                           NULLIF(CONCAT_WS(', ',
                               NULLIF(BTRIM(p.tipologia), ''),
                               NULLIF(BTRIM(p.sabor), ''),
                               NULLIF(BTRIM(p.cobertura), '')
                           ), ''), plan.produto
                       ), producao_estimada, producao_real,
                       COALESCE(status, 'pendente'), COALESCE(nota, ''),
                       COALESCE(producao_estimada_bolhao, 0), COALESCE(producao_estimada_matosinhos, 0),
                       COALESCE(item_tipo, 'standard'), bolo_tamanho,
                       bolo_sabor_1, bolo_sabor_2, bolo_sabor_3, bolo_cobertura
                FROM {tbl} plan
                LEFT JOIN produtos_pastelaria p
                  ON p.id=plan.produto_pastelaria_id
                WHERE data = %s AND no_plano = TRUE
                ORDER BY 1
            """, (data,))
            rows = cursor.fetchall()
            return [
                {
                    'produto': r[0],
                    'estimado': int(r[1] or 0),
                    'real': int(r[2]) if r[2] is not None else None,
                    'status': r[3],
                    'nota': r[4],
                    'estimado_bolhao': int(r[5]),
                    'estimado_matosinhos': int(r[6]),
                    'item_tipo': r[7],
                    'bolo_tamanho': r[8],
                    'bolo_sabores': [s for s in (r[9], r[10], r[11]) if s],
                    'bolo_cobertura': r[12],
                }
                for r in rows
            ]
        else:
            cursor.execute(f"""
                SELECT produto, producao_estimada, producao_real
                FROM {tbl}
                WHERE data = %s AND no_plano = TRUE
                ORDER BY produto
            """, (data,))
            rows = cursor.fetchall()
            return [
                {
                    'produto': r[0],
                    'estimado': int(r[1] or 0),
                    'real': int(r[2]) if r[2] is not None else None,
                }
                for r in rows
            ]


def get_plano_intervalo_area(area: str, data_inicio: date, data_fim: date) -> list:
    """Return visible plan rows for an inclusive date range."""
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'pastelaria':
            cursor.execute(f"""
                SELECT plan.data,
                       COALESCE(
                           NULLIF(CONCAT_WS(', ',
                               NULLIF(BTRIM(p.tipologia), ''),
                               NULLIF(BTRIM(p.sabor), ''),
                               NULLIF(BTRIM(p.cobertura), '')
                           ), ''), plan.produto
                       ),
                       producao_estimada, producao_real,
                       COALESCE(status, 'pendente'), COALESCE(nota, ''),
                       COALESCE(producao_estimada_bolhao, 0),
                       COALESCE(producao_estimada_matosinhos, 0),
                       COALESCE(item_tipo, 'standard'), bolo_tamanho,
                       bolo_sabor_1, bolo_sabor_2, bolo_sabor_3, bolo_cobertura
                FROM {tbl} plan
                LEFT JOIN produtos_pastelaria p
                  ON p.id=plan.produto_pastelaria_id
                WHERE plan.data BETWEEN %s AND %s AND no_plano = TRUE
                ORDER BY plan.data, 2
            """, (data_inicio, data_fim))
            rows = cursor.fetchall()
            return [
                {
                    'data': r[0],
                    'produto': r[1],
                    'estimado': int(r[2] or 0),
                    'real': int(r[3]) if r[3] is not None else None,
                    'status': r[4],
                    'nota': r[5],
                    'estimado_bolhao': int(r[6] or 0),
                    'estimado_matosinhos': int(r[7] or 0),
                    'item_tipo': r[8],
                    'bolo_tamanho': r[9],
                    'bolo_sabores': [s for s in (r[10], r[11], r[12]) if s],
                    'bolo_cobertura': r[13],
                }
                for r in rows
            ]
        cursor.execute(f"""
            SELECT data, produto, producao_estimada, producao_real
            FROM {tbl}
            WHERE data BETWEEN %s AND %s AND no_plano = TRUE
            ORDER BY data, produto
        """, (data_inicio, data_fim))
        return [
            {'data': r[0], 'produto': r[1], 'estimado': int(r[2] or 0),
             'real': int(r[3]) if r[3] is not None else None}
            for r in cursor.fetchall()
        ]


def get_plano_produto(area: str, data: date, produto: str) -> dict:
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        product_id = (
            _resolve_pastelaria_product_id(cursor, produto)
            if area == 'pastelaria' else None
        )
        identity_clause = (
            "produto_pastelaria_id = %s" if product_id is not None
            else "produto = %s"
        )
        cursor.execute(f"""
            SELECT producao_estimada, producao_real, no_plano
            FROM {tbl}
            WHERE data = %s AND {identity_clause}
        """, (data, product_id if product_id is not None else produto))
        row = cursor.fetchone()
    if row:
        return {'estimado': int(row[0] or 0), 'real': int(row[1]) if row[1] is not None else None, 'no_plano': row[2]}
    return None


def update_producao_real_area(area: str, data: date, produto: str, producao_real: int):
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        product_id = (
            _resolve_pastelaria_product_id(cursor, produto)
            if area == 'pastelaria' else None
        )
        identity_clause = (
            "produto_pastelaria_id = %s" if product_id is not None
            else "produto = %s"
        )
        identity_value = product_id if product_id is not None else produto
        if area == 'pastelaria' and producao_real > 0:
            cursor.execute(f"""
                UPDATE {tbl}
                SET producao_real = %s, status = 'concluido', updated_at = NOW()
                WHERE data = %s AND {identity_clause}
            """, (producao_real, data, identity_value))
        else:
            cursor.execute(f"""
                UPDATE {tbl}
                SET producao_real = %s, updated_at = NOW()
                WHERE data = %s AND {identity_clause}
            """, (producao_real, data, identity_value))
        conn.commit()


def update_plano_status_area(area: str, data: date, produto: str, status: str, nota: str = ''):
    tbl = _plano_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        product_id = (
            _resolve_pastelaria_product_id(cursor, produto)
            if area == 'pastelaria' else None
        )
        identity_clause = (
            "produto_pastelaria_id = %s" if product_id is not None
            else "produto = %s"
        )
        cursor.execute(f"""
            UPDATE {tbl}
            SET status = %s, nota = %s, updated_at = NOW()
            WHERE data = %s AND {identity_clause}
        """, (
            status, nota, data,
            product_id if product_id is not None else produto,
        ))
        conn.commit()


def upsert_stock_producao_area(area: str, data: date, produto: str, quantidade: int):
    tbl = _stock_prod_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'pastelaria':
            product_id = _pastelaria_write_product_id(cursor, produto)
            conflict_target = (
                "(data, produto_pastelaria_id) "
                "WHERE produto_pastelaria_id IS NOT NULL"
                if product_id is not None else
                "(data, produto) WHERE produto_pastelaria_id IS NULL"
            )
            conflict_sql = f"""
                ON CONFLICT {conflict_target}
                DO UPDATE SET
                    quantidade = GREATEST(
                        {tbl}.quantidade + EXCLUDED.quantidade, 0
                    ),
                    updated_at = NOW()
            """
            cursor.execute(f"""
                INSERT INTO {tbl} (
                    data, produto, quantidade, produto_pastelaria_id
                )
                VALUES (
                    %s, %s, GREATEST(%s, 0),
                    %s
                )
                {conflict_sql}
            """, (data, produto, quantidade, product_id))
        else:
            cursor.execute(f"""
                INSERT INTO {tbl} (data, produto, quantidade)
                VALUES (%s, %s, GREATEST(%s, 0))
                ON CONFLICT (data, produto) DO UPDATE SET
                    quantidade = GREATEST(
                        {tbl}.quantidade + EXCLUDED.quantidade, 0
                    ),
                    updated_at = NOW()
            """, (data, produto, quantidade))
        conn.commit()


def get_stock_producao_area_all(area: str, data: date = None) -> list:
    tbl = _stock_prod_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'pastelaria':
            cursor.execute(f"""
                SELECT COALESCE(
                           NULLIF(CONCAT_WS(', ',
                               NULLIF(BTRIM(p.tipologia), ''),
                               NULLIF(BTRIM(p.sabor), ''),
                               NULLIF(BTRIM(p.cobertura), '')
                           ), ''), MAX(stock.produto)
                       ) AS produto,
                       SUM(stock.quantidade) AS total
                FROM {tbl} stock
                LEFT JOIN produtos_pastelaria p
                  ON p.id=stock.produto_pastelaria_id
                WHERE stock.quantidade > 0
                GROUP BY COALESCE(
                             stock.produto_pastelaria_id::text,
                             'text:' || stock.produto
                         ),
                         p.tipologia, p.sabor, p.cobertura
                HAVING SUM(stock.quantidade) > 0
                ORDER BY produto
            """)
        else:
            cursor.execute(f"""
                SELECT produto, SUM(quantidade) as total
                FROM {tbl}
                WHERE quantidade > 0
                GROUP BY produto
                HAVING SUM(quantidade) > 0
                ORDER BY produto
            """)
        rows = cursor.fetchall()
    return [{'produto': r[0], 'quantidade': int(r[1])} for r in rows]


def get_stock_producao_area(area: str, data: date, produto: str) -> int:
    tbl = _stock_prod_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        product_id = (
            _resolve_pastelaria_product_id(cursor, produto)
            if area == 'pastelaria' else None
        )
        identity_clause = (
            "produto_pastelaria_id = %s" if product_id is not None
            else "produto = %s"
        )
        cursor.execute(f"""
            SELECT COALESCE(SUM(quantidade), 0) FROM {tbl}
            WHERE {identity_clause} AND quantidade > 0
        """, (product_id if product_id is not None else produto,))
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def reduzir_stock_producao_area(area: str, data: date, produto: str, quantidade: int) -> bool:
    tbl = _stock_prod_table(area)
    with db_connection() as conn:
        cursor = conn.cursor()
        product_id = (
            _resolve_pastelaria_product_id(cursor, produto)
            if area == 'pastelaria' else None
        )
        identity_clause = (
            "produto_pastelaria_id = %s" if product_id is not None
            else "produto = %s"
        )
        cursor.execute(f"""
            SELECT id, quantidade FROM {tbl}
            WHERE {identity_clause} AND quantidade > 0
            ORDER BY data ASC
        """, (product_id if product_id is not None else produto,))
        rows = cursor.fetchall()
        remaining = quantidade
        updated = False
        for row_id, row_qty in rows:
            if remaining <= 0:
                break
            subtract = min(remaining, int(row_qty))
            cursor.execute(f"""
                UPDATE {tbl}
                SET quantidade = GREATEST(quantidade - %s, 0), updated_at = NOW()
                WHERE id = %s
            """, (subtract, row_id))
            remaining -= subtract
            updated = True
        conn.commit()
    return updated


def get_reconciliacao_pastelaria(data_inicio: date, data_fim: date) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            WITH prod AS (
                SELECT COALESCE(pp.produto_pastelaria_id::text,
                                'text:' || pp.produto) AS identity_key,
                       COALESCE(
                           NULLIF(CONCAT_WS(', ',
                               NULLIF(BTRIM(p.tipologia), ''),
                               NULLIF(BTRIM(p.sabor), ''),
                               NULLIF(BTRIM(p.cobertura), '')
                           ), ''), MAX(pp.produto)
                       ) AS produto,
                       COALESCE(SUM(pp.producao_real), 0) AS total_produzido
                FROM plano_producao_pastelaria pp
                LEFT JOIN produtos_pastelaria p
                  ON p.id=pp.produto_pastelaria_id
                WHERE pp.data BETWEEN %s AND %s
                  AND pp.producao_real IS NOT NULL
                GROUP BY identity_key, p.tipologia, p.sabor, p.cobertura
            ),
            transf AS (
                SELECT COALESCE(ot.produto_pastelaria_id::text,
                                'text:' || ot.produto) AS identity_key,
                       COALESCE(
                           NULLIF(CONCAT_WS(', ',
                               NULLIF(BTRIM(p.tipologia), ''),
                               NULLIF(BTRIM(p.sabor), ''),
                               NULLIF(BTRIM(p.cobertura), '')
                           ), ''), MAX(ot.produto)
                       ) AS produto,
                       COALESCE(SUM(ot.quantidade), 0) AS total_transferido
                FROM ordens_transferencia ot
                LEFT JOIN produtos_pastelaria p
                  ON p.id=ot.produto_pastelaria_id
                WHERE ot.area_origem = 'Pastelaria'
                  AND ot.data BETWEEN %s AND %s
                GROUP BY identity_key, p.tipologia, p.sabor, p.cobertura
            ),
            stock AS (
                SELECT COALESCE(sp.produto_pastelaria_id::text,
                                'text:' || sp.produto) AS identity_key,
                       COALESCE(
                           NULLIF(CONCAT_WS(', ',
                               NULLIF(BTRIM(p.tipologia), ''),
                               NULLIF(BTRIM(p.sabor), ''),
                               NULLIF(BTRIM(p.cobertura), '')
                           ), ''), MAX(sp.produto)
                       ) AS produto,
                       COALESCE(SUM(sp.quantidade), 0) AS stock_producao
                FROM stock_producao_pastelaria sp
                LEFT JOIN produtos_pastelaria p
                  ON p.id=sp.produto_pastelaria_id
                WHERE sp.quantidade > 0
                GROUP BY identity_key, p.tipologia, p.sabor, p.cobertura
            ),
            balcao AS (
                SELECT DISTINCT ON (
                           COALESCE(cs.produto_pastelaria_id::text,
                                    'text:' || cs.produto),
                           cs.loja
                       )
                       COALESCE(cs.produto_pastelaria_id::text,
                                'text:' || cs.produto) AS identity_key,
                       COALESCE(
                           NULLIF(CONCAT_WS(', ',
                               NULLIF(BTRIM(p.tipologia), ''),
                               NULLIF(BTRIM(p.sabor), ''),
                               NULLIF(BTRIM(p.cobertura), '')
                           ), ''), cs.produto
                       ) AS produto,
                       cs.loja, cs.quantidade, cs.data
                FROM contagem_stock cs
                LEFT JOIN produtos_pastelaria p
                  ON p.id=cs.produto_pastelaria_id
                WHERE cs.tipo = 'pastelaria'
                ORDER BY identity_key, cs.loja, cs.data DESC, cs.id DESC
            ),
            balcao_mat AS (
                SELECT identity_key, produto,
                       quantidade AS balcao_matosinhos, data AS data_mat
                FROM balcao WHERE loja = 'Matosinhos'
            ),
            balcao_bol AS (
                SELECT identity_key, produto,
                       quantidade AS balcao_bolhao, data AS data_bol
                FROM balcao WHERE loja = 'Bolhão'
            ),
            todas_fontes AS (
                SELECT identity_key, produto FROM prod
                UNION ALL SELECT identity_key, produto FROM transf
                UNION ALL SELECT identity_key, produto FROM stock
                UNION ALL SELECT identity_key, produto FROM balcao
            ),
            todos AS (
                SELECT identity_key, MAX(produto) AS produto
                FROM todas_fontes
                GROUP BY identity_key
            )
            SELECT
                t.produto,
                COALESCE(p.total_produzido, 0)   AS total_produzido,
                COALESCE(tr.total_transferido, 0) AS total_transferido,
                COALESCE(s.stock_producao, 0)     AS stock_producao,
                COALESCE(bm.balcao_matosinhos, 0) AS balcao_matosinhos,
                bm.data_mat,
                COALESCE(bb.balcao_bolhao, 0)     AS balcao_bolhao,
                bb.data_bol
            FROM todos t
            LEFT JOIN prod     p  ON p.identity_key  = t.identity_key
            LEFT JOIN transf   tr ON tr.identity_key = t.identity_key
            LEFT JOIN stock    s  ON s.identity_key  = t.identity_key
            LEFT JOIN balcao_mat bm ON bm.identity_key = t.identity_key
            LEFT JOIN balcao_bol bb ON bb.identity_key = t.identity_key
            ORDER BY t.produto
        """, (data_inicio, data_fim, data_inicio, data_fim))
        rows = cursor.fetchall()
    return [
        {
            'produto': r[0],
            'total_produzido': int(r[1]),
            'total_transferido': int(r[2]),
            'stock_producao': int(r[3]),
            'balcao_matosinhos': int(r[4]),
            'data_mat': r[5],
            'balcao_bolhao': int(r[6]),
            'data_bol': r[7],
        }
        for r in rows
    ]


# ── Crédito ──────────────────────────────────────────────────────────────────

