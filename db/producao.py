import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
from db.cache import ttl_cache, ttl_cache_args, invalidate_prefix
import pandas as pd
import json

def get_target_by_month(month: int) -> float:
    high_season = [5, 6, 7, 8, 9]
    if month in high_season:
        return 24.0
    return 25.0

def add_producao(data: date, loja: str, quantidade_kg: float, tipo: str = 'producao', sabor: str = None):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO producao (data, loja, quantidade_kg, tipo, sabor, store_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    ''', (data, loja, quantidade_kg, tipo, sabor, store_id))
    conn.commit()
    release_connection(conn)

def add_quebra(data: date, loja: str, quantidade_kg: float, motivo: str = None, sabor: str = None, lote: str = None):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id FROM quebras
        WHERE data = %s AND loja = %s AND sabor IS NOT DISTINCT FROM %s AND motivo IS NOT DISTINCT FROM %s
          AND created_at >= NOW() - INTERVAL '10 seconds'
        LIMIT 1
    ''', (data, loja, sabor, motivo))
    if cursor.fetchone():
        logger.warning(
            "add_quebra: registo duplicado ignorado (data=%s loja=%s sabor=%s motivo=%s)",
            data, loja, sabor, motivo)
        release_connection(conn)
        return
    cursor.execute('''
        INSERT INTO quebras (data, loja, quantidade_kg, motivo, sabor, lote, store_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    ''', (data, loja, quantidade_kg, motivo, sabor, lote, store_id))
    conn.commit()
    release_connection(conn)

def add_quebra_area(data: date, loja: str, quantidade: float, area: str, produto: str = None, lote: str = None, motivo: str = None):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO quebras (data, loja, quantidade_kg, motivo, sabor, lote, store_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    ''', (data, loja, quantidade, motivo, f"{area}:{produto}" if produto else area, lote, store_id))
    conn.commit()
    release_connection(conn)

def get_quebras_df_area(loja: str, area: str):
    conn = get_connection()
    query = f"""
        SELECT id, data, lote, quantidade_kg, motivo, sabor
        FROM quebras
        WHERE loja = %s AND sabor LIKE %s
        ORDER BY data DESC, id DESC
    """
    df = pd.read_sql_query(query, conn, params=[loja, f"{area}:%"])
    release_connection(conn)
    return df

def delete_quebra_area(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM quebras WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)

def add_venda(data: date, loja: str, valor_euros: float):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO vendas (data, loja, valor_euros, store_id)
        VALUES (%s, %s, %s, %s)
    ''', (data, loja, valor_euros, store_id))
    conn.commit()
    release_connection(conn)

def add_stock_inicial(data: date, loja: str, quantidade_kg: float):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO stock_inicial (data, loja, quantidade_kg, store_id)
        VALUES (%s, %s, %s, %s)
    ''', (data, loja, quantidade_kg, store_id))
    conn.commit()
    release_connection(conn)

def add_rececao_stock(data: date, loja: str, quantidade_kg: float, origem: str = None, sabor: str = None):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO rececao_stock (data, loja, quantidade_kg, origem, sabor, store_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    ''', (data, loja, quantidade_kg, origem, sabor, store_id))
    conn.commit()
    release_connection(conn)

@ttl_cache_args('sabores_list', ttl=600)
def get_sabores_list(apenas_eurokg: bool = False) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    if apenas_eurokg:
        cursor.execute("SELECT nome_corrente FROM receitas_gelado WHERE ativo = TRUE AND conta_eurokg = TRUE AND nome_corrente IS NOT NULL AND nome_corrente != '' ORDER BY nome_corrente")
    else:
        cursor.execute("SELECT nome_corrente FROM receitas_gelado WHERE ativo = TRUE AND nome_corrente IS NOT NULL AND nome_corrente != '' ORDER BY nome_corrente")
    sabores = [row[0] for row in cursor.fetchall()]
    release_connection(conn)
    return sabores

def get_receita_by_nome_corrente(nome_corrente: str) -> str:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT nome FROM receitas_gelado WHERE nome_corrente = %s AND ativo = TRUE", (nome_corrente,))
    row = cursor.fetchone()
    release_connection(conn)
    return row[0] if row else nome_corrente

def get_nome_corrente_by_receita(nome_receita: str) -> str:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(nome_corrente, nome) FROM receitas_gelado WHERE nome = %s AND ativo = TRUE", (nome_receita,))
    row = cursor.fetchone()
    release_connection(conn)
    return row[0] if row else nome_receita

@ttl_cache('sabores_mapping', ttl=600)
def get_sabores_mapping() -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT nome, nome_corrente FROM receitas_gelado WHERE ativo = TRUE AND nome_corrente IS NOT NULL AND nome_corrente != ''")
    mapping = {row[0]: row[1] for row in cursor.fetchall()}
    release_connection(conn)
    return mapping

def get_producao_by_sabor_and_days(loja: str = None) -> pd.DataFrame:
    conn = get_connection()
    from datetime import timedelta
    today = date.today()
    day_minus_1 = today - timedelta(days=1)
    day_minus_2 = today - timedelta(days=2)
    day_minus_3 = today - timedelta(days=3)
    
    query = """
        SELECT 
            sabor,
            SUM(CASE WHEN data = %s THEN quantidade_kg ELSE 0 END) as dia_1,
            SUM(CASE WHEN data = %s THEN quantidade_kg ELSE 0 END) as dia_2,
            SUM(CASE WHEN data = %s THEN quantidade_kg ELSE 0 END) as dia_3
        FROM producao
        WHERE sabor IS NOT NULL
    """
    params = [day_minus_1, day_minus_2, day_minus_3]
    
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    
    query += " GROUP BY sabor ORDER BY sabor"
    
    df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    return df

def get_producao_sabor_overview() -> pd.DataFrame:
    conn = get_connection()

    cursor = conn.cursor()

    # Query 1 — lista de sabores
    cursor.execute("""
        SELECT DISTINCT sabor FROM (
            SELECT sabor FROM producao WHERE loja = 'Matosinhos' AND sabor IS NOT NULL
            UNION
            SELECT sabor FROM stock_gelado WHERE sabor IS NOT NULL
        ) AS combined ORDER BY sabor
    """)
    sabores = [r[0] for r in cursor.fetchall()]

    # Query 2 — última data de pesagem Bolhão
    cursor.execute("""
        SELECT MAX(data) FROM stock_gelado
        WHERE loja = 'Bolhão' AND tipo IN ('fim', 'inicio')
    """)
    ultima_data_pesagem_bolhao = cursor.fetchone()[0]

    # Query 3 — última data de pesagem Matosinhos
    cursor.execute("""
        SELECT MAX(data) FROM stock_gelado
        WHERE loja = 'Matosinhos' AND tipo = 'inicio'
    """)
    ultima_data_pesagem_mat = cursor.fetchone()[0]

    # Query 4 — última produção por sabor (Matosinhos)
    cursor.execute("""
        SELECT DISTINCT ON (sabor) sabor, data, quantidade_kg
        FROM producao
        WHERE loja = 'Matosinhos'
        ORDER BY sabor, data DESC, id DESC
    """)
    ultima_producao = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}

    # Queries 5-7 — stock Matosinhos (apenas se existe data de pesagem)
    stock_mat_pesagem = {}
    producao_depois_mat = {}
    quebras_mat_map = {}
    if ultima_data_pesagem_mat:
        cursor.execute("""
            SELECT DISTINCT ON (sabor) sabor, quantidade_kg FROM stock_gelado
            WHERE loja = 'Matosinhos' AND tipo = 'inicio' AND data = %s
            ORDER BY sabor, id DESC
        """, (ultima_data_pesagem_mat,))
        stock_mat_pesagem = {r[0]: float(r[1]) for r in cursor.fetchall()}

        cursor.execute("""
            SELECT sabor, SUM(quantidade_kg) FROM producao
            WHERE loja = 'Matosinhos' AND data >= %s
            GROUP BY sabor
        """, (ultima_data_pesagem_mat,))
        producao_depois_mat = {r[0]: float(r[1]) for r in cursor.fetchall()}

        cursor.execute("""
            SELECT sabor, SUM(quantidade_kg) FROM quebras
            WHERE loja = 'Matosinhos' AND data >= %s
            GROUP BY sabor
        """, (ultima_data_pesagem_mat,))
        quebras_mat_map = {r[0]: float(r[1]) for r in cursor.fetchall()}

    # Queries 8-11 — stock Bolhão (apenas se existe data de pesagem)
    pesagem_bolhao_map = {}
    rececao_stock_bolhao = {}
    rececao_mercadoria_bolhao = {}
    quebras_bolhao_map = {}
    if ultima_data_pesagem_bolhao:
        cursor.execute("""
            SELECT DISTINCT ON (sabor) sabor, quantidade_kg
            FROM stock_gelado
            WHERE loja = 'Bolhão' AND tipo IN ('fim', 'inicio') AND data = %s
            ORDER BY sabor, CASE WHEN tipo = 'fim' THEN 0 ELSE 1 END, id DESC
        """, (ultima_data_pesagem_bolhao,))
        pesagem_bolhao_map = {r[0]: float(r[1]) for r in cursor.fetchall()}

        cursor.execute("""
            SELECT sabor, SUM(quantidade_kg) FROM rececao_stock
            WHERE loja = 'Bolhão' AND data > %s
            GROUP BY sabor
        """, (ultima_data_pesagem_bolhao,))
        rececao_stock_bolhao = {r[0]: float(r[1]) for r in cursor.fetchall()}

        cursor.execute("""
            SELECT sabor, SUM(quantidade) FROM rececao_mercadoria
            WHERE loja = 'Bolhão' AND unidade = 'kg' AND data > %s
            GROUP BY sabor
        """, (ultima_data_pesagem_bolhao,))
        rececao_mercadoria_bolhao = {r[0]: float(r[1]) for r in cursor.fetchall()}

        cursor.execute("""
            SELECT sabor, SUM(quantidade_kg) FROM quebras
            WHERE loja = 'Bolhão' AND data > %s
            GROUP BY sabor
        """, (ultima_data_pesagem_bolhao,))
        quebras_bolhao_map = {r[0]: float(r[1]) for r in cursor.fetchall()}

    release_connection(conn)

    # Combinação em Python — sem mais DB calls
    result_data = []
    for sabor in sabores:
        prod_entry = ultima_producao.get(sabor)
        ultima_data = prod_entry[0] if prod_entry else None
        ultima_quantidade = prod_entry[1] if prod_entry else None

        if ultima_data_pesagem_mat:
            stock_matosinhos = stock_mat_pesagem.get(sabor, 0)
            stock_matosinhos += producao_depois_mat.get(sabor, 0)
            stock_matosinhos -= quebras_mat_map.get(sabor, 0)
            if stock_matosinhos < 0:
                stock_matosinhos = 0
            data_stock_matosinhos = ultima_data_pesagem_mat
        else:
            stock_matosinhos = None
            data_stock_matosinhos = None

        if ultima_data_pesagem_bolhao:
            pesagem_bolhao = pesagem_bolhao_map.get(sabor, 0)
            rececao_bolhao = (
                rececao_stock_bolhao.get(sabor, 0) +
                rececao_mercadoria_bolhao.get(sabor, 0)
            )
            quebras_bolhao = quebras_bolhao_map.get(sabor, 0)
            stock_bolhao = pesagem_bolhao + rececao_bolhao - quebras_bolhao
            if stock_bolhao < 0:
                stock_bolhao = 0
            data_pesagem_bolhao = ultima_data_pesagem_bolhao
        else:
            stock_bolhao = None
            data_pesagem_bolhao = None

        result_data.append({
            'sabor': sabor,
            'ultima_data': ultima_data,
            'ultima_quantidade': ultima_quantidade,
            'stock_matosinhos': stock_matosinhos,
            'data_stock_matosinhos': data_stock_matosinhos,
            'stock_bolhao': stock_bolhao,
            'data_stock_bolhao': data_pesagem_bolhao
        })

    df = pd.DataFrame(result_data)
    
    if df.empty:
        return df
    
    df['ultima_quantidade'] = pd.to_numeric(df['ultima_quantidade'], errors='coerce').fillna(0)
    df['stock_matosinhos'] = pd.to_numeric(df['stock_matosinhos'], errors='coerce').fillna(0)
    df['stock_bolhao'] = pd.to_numeric(df['stock_bolhao'], errors='coerce').fillna(0)
    
    today_date = date.today()
    def calc_consumo(row):
        if row['ultima_data'] and row['ultima_quantidade'] > 0:
            try:
                if isinstance(row['ultima_data'], date):
                    ultima = row['ultima_data']
                else:
                    ultima = pd.to_datetime(row['ultima_data']).date()
                dias = (today_date - ultima).days
                if dias > 0:
                    return round(row['ultima_quantidade'] / dias, 2)
            except:
                pass
        return 0
    
    df['consumo_medio'] = df.apply(calc_consumo, axis=1)
    
    df['stock_total'] = df['stock_matosinhos'] + df['stock_bolhao']
    df = df[(df['stock_matosinhos'] > 0) | (df['stock_bolhao'] > 0)]
    
    return df

def get_last_rececao_by_sabor(loja: str = "Bolhão") -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sabor, MAX(data) as ultima_data
        FROM rececao_stock
        WHERE loja = %s AND sabor IS NOT NULL
        GROUP BY sabor
    """, (loja,))
    result = {row[0]: row[1] for row in cursor.fetchall()}
    release_connection(conn)
    return result

@ttl_cache('sabores_excluidos', ttl=600)
def get_sabores_excluidos_eurokg() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(nome_corrente, nome) FROM receitas_gelado WHERE conta_eurokg = FALSE AND ativo = TRUE")
    sabores = [row[0] for row in cursor.fetchall()]
    release_connection(conn)
    return sabores

def _build_exclusion_filter(sabores_excluidos: list, field: str = 'sabor') -> tuple:
    if not sabores_excluidos:
        return '', []
    placeholders = ','.join(['%s'] * len(sabores_excluidos))
    clause = f" AND ({field} IS NULL OR {field} NOT IN ({placeholders}))"
    return clause, list(sabores_excluidos)

def get_producao_total_by_period(loja: str = None, data_inicio: date = None, data_fim: date = None, para_eurokg: bool = True) -> float:
    conn = get_connection()
    query = "SELECT COALESCE(SUM(quantidade_kg), 0) FROM producao WHERE 1=1"
    params = []
    if para_eurokg:
        excluidos = get_sabores_excluidos_eurokg()
        if excluidos:
            placeholders = ','.join(['%s'] * len(excluidos))
            query += f" AND (sabor IS NOT NULL AND sabor NOT IN ({placeholders}))"
            params.extend(excluidos)
        else:
            query += " AND sabor IS NOT NULL"
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    cursor = conn.cursor()
    cursor.execute(query, params)
    result = cursor.fetchone()[0]
    release_connection(conn)
    return result

def get_producao_plano_matosinhos_total(data_inicio: date = None, data_fim: date = None) -> float:
    """Returns total real Matosinhos production from plano_producao module by period.
    Applies the same sabor exclusion as the euro/kg calculations.
    Excludes sabores marked as excluded from euro/kg."""
    excluidos = get_sabores_excluidos_eurokg()
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT COALESCE(SUM(producao_real_matosinhos), 0)
        FROM plano_producao
        WHERE producao_real_matosinhos IS NOT NULL
    """
    params = []
    if excluidos:
        placeholders = ','.join(['%s'] * len(excluidos))
        query += f" AND (sabor IS NOT NULL AND sabor NOT IN ({placeholders}))"
        params.extend(excluidos)
    else:
        query += " AND sabor IS NOT NULL"
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    cursor.execute(query, params)
    result = float(cursor.fetchone()[0])
    release_connection(conn)
    return result


def get_producao_plano_matosinhos_daily(data_inicio: date = None, data_fim: date = None) -> dict:
    """Returns daily real Matosinhos production from plano_producao module.
    Returns a dict of {date: total_kg}.
    Applies the same sabor exclusion as the euro/kg calculations."""
    excluidos = get_sabores_excluidos_eurokg()
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT data, COALESCE(SUM(producao_real_matosinhos), 0) as total_kg
        FROM plano_producao
        WHERE producao_real_matosinhos IS NOT NULL
    """
    params = []
    if excluidos:
        placeholders = ','.join(['%s'] * len(excluidos))
        query += f" AND (sabor IS NOT NULL AND sabor NOT IN ({placeholders}))"
        params.extend(excluidos)
    else:
        query += " AND sabor IS NOT NULL"
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " GROUP BY data ORDER BY data"
    cursor.execute(query, params)
    result = {row[0]: float(row[1]) for row in cursor.fetchall()}
    release_connection(conn)
    return result


def get_producao_plano_matosinhos_by_month(year: int) -> dict:
    """Returns monthly real Matosinhos production from plano_producao module for a given year.
    Returns a dict of {month: total_kg}.
    Applies the same sabor exclusion as the euro/kg calculations."""
    excluidos = get_sabores_excluidos_eurokg()
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT EXTRACT(MONTH FROM data)::int as mes, COALESCE(SUM(producao_real_matosinhos), 0)
        FROM plano_producao
        WHERE producao_real_matosinhos IS NOT NULL
          AND EXTRACT(YEAR FROM data) = %s
    """
    params = [year]
    if excluidos:
        placeholders = ','.join(['%s'] * len(excluidos))
        query += f" AND (sabor IS NOT NULL AND sabor NOT IN ({placeholders}))"
        params.extend(excluidos)
    else:
        query += " AND sabor IS NOT NULL"
    query += " GROUP BY mes ORDER BY mes"
    cursor.execute(query, params)
    result = {int(row[0]): float(row[1]) for row in cursor.fetchall()}
    release_connection(conn)
    return result


def get_producao_daily_totals(loja: str = None, data_inicio: date = None, data_fim: date = None, para_eurokg: bool = True) -> pd.DataFrame:
    conn = get_connection()
    query = "SELECT data, SUM(quantidade_kg) as total_kg FROM producao WHERE 1=1"
    params = []
    if para_eurokg:
        excluidos = get_sabores_excluidos_eurokg()
        if excluidos:
            placeholders = ','.join(['%s'] * len(excluidos))
            query += f" AND (sabor IS NOT NULL AND sabor NOT IN ({placeholders}))"
            params.extend(excluidos)
        else:
            query += " AND sabor IS NOT NULL"
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " GROUP BY data ORDER BY data"
    df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    return df

def get_producao_df(loja: str = None, data_inicio: date = None, data_fim: date = None, para_eurokg: bool = True) -> pd.DataFrame:
    conn = get_connection()
    query = "SELECT * FROM producao WHERE 1=1"
    params = []
    if para_eurokg:
        excluidos = get_sabores_excluidos_eurokg()
        if excluidos:
            placeholders = ','.join(['%s'] * len(excluidos))
            query += f" AND (sabor IS NOT NULL AND sabor NOT IN ({placeholders}))"
            params.extend(excluidos)
        else:
            query += " AND sabor IS NOT NULL"
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    return df

def get_quebras_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    conn = get_connection()
    query = "SELECT * FROM quebras WHERE 1=1"
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    return df

def get_vendas_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    conn = get_connection()
    query = "SELECT * FROM vendas WHERE 1=1"
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    return df

def get_stock_inicial_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    excluidos = get_sabores_excluidos_eurokg()
    conn = get_connection()
    excl_clause = ''
    excl_params = []
    if excluidos:
        placeholders = ','.join(['%s'] * len(excluidos))
        excl_clause = f" AND (sabor IS NOT NULL AND sabor NOT IN ({placeholders}))"
        excl_params = list(excluidos)
    else:
        excl_clause = " AND sabor IS NOT NULL"
    query = f"""
        SELECT data, loja, SUM(quantidade_kg) as quantidade_kg
        FROM stock_gelado
        WHERE tipo = 'inicio'{excl_clause}
    """
    params = list(excl_params)
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " GROUP BY data, loja ORDER BY data"
    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        query_legacy = "SELECT * FROM stock_inicial WHERE 1=1"
        params_legacy = []
        if loja:
            query_legacy += " AND loja = %s"
            params_legacy.append(loja)
        if data_inicio:
            query_legacy += " AND data >= %s"
            params_legacy.append(data_inicio)
        if data_fim:
            query_legacy += " AND data <= %s"
            params_legacy.append(data_fim)
        df = pd.read_sql_query(query_legacy, conn, params=params_legacy)
    release_connection(conn)
    return df

def get_rececao_stock_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    conn = get_connection()
    query = "SELECT id, data, loja, quantidade_kg, sabor FROM rececao_stock WHERE 1=1"
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    df = pd.read_sql_query(query, conn, params=params)
    
    query_merc = """
        SELECT id, data, loja, quantidade as quantidade_kg, sabor 
        FROM rececao_mercadoria 
        WHERE unidade = 'kg'
    """
    params_merc = []
    if loja:
        query_merc += " AND loja = %s"
        params_merc.append(loja)
    if data_inicio:
        query_merc += " AND data >= %s"
        params_merc.append(data_inicio)
    if data_fim:
        query_merc += " AND data <= %s"
        params_merc.append(data_fim)
    df_merc = pd.read_sql_query(query_merc, conn, params=params_merc)
    
    release_connection(conn)
    
    if not df_merc.empty:
        df = pd.concat([df, df_merc], ignore_index=True)
    
    return df

def get_ajuste_producao_mes(ano: int, mes: int) -> float:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(SUM(quantidade_kg), 0) FROM ajustes_producao WHERE ano = %s AND mes = %s", (ano, mes))
    total = float(cursor.fetchone()[0])
    release_connection(conn)
    return total


def get_vendas_filtradas_df(area: str, loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    valid_areas = ('gelado_kpi', 'pastelaria', 'confeitaria')
    if area not in valid_areas:
        return pd.DataFrame(columns=['data', 'loja', 'valor_euros'])

    conn = get_connection()

    query = f"""
        SELECT vd.data, vd.loja, SUM(vd.valor_euros) as valor_euros
        FROM vendas_detalhe vd
        INNER JOIN produtos_vendas_config pvc ON vd.produto = pvc.produto
        WHERE pvc.{area} = TRUE
    """
    params = []
    if loja:
        query += " AND vd.loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND vd.data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND vd.data <= %s"
        params.append(data_fim)
    query += " GROUP BY vd.data, vd.loja ORDER BY vd.data"

    df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    return df


@ttl_cache_args('kpi_by_day', ttl=600)
def calculate_kpi_by_day(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    loja_db = loja if loja and loja != 'Porto (Global)' else None

    excluidos = get_sabores_excluidos_eurokg()

    vendas_gelado_df = get_vendas_filtradas_df('gelado_kpi', loja_db, data_inicio, data_fim)
    if loja_db == 'Matosinhos':
        producao_plano_daily = get_producao_plano_matosinhos_daily(data_inicio, data_fim)
        producao_df = pd.DataFrame()
    else:
        producao_df = get_producao_df(None, data_inicio, data_fim, para_eurokg=True)
        producao_plano_daily = {}
    quebras_df = get_quebras_df(loja_db, data_inicio, data_fim)
    if not quebras_df.empty:
        sabores_gelado = get_sabores_list(apenas_eurokg=True)
        quebras_df = quebras_df[quebras_df['sabor'].isin(sabores_gelado)]

    conn = get_connection()
    cursor = conn.cursor()

    transf_query = "SELECT data, loja_destino, SUM(quantidade_kg) as total FROM transferencias"
    transf_params = []
    transf_clauses = []
    if data_inicio:
        transf_clauses.append("data >= %s")
        transf_params.append(data_inicio)
    if data_fim:
        transf_clauses.append("data <= %s")
        transf_params.append(data_fim)
    if transf_clauses:
        transf_query += " WHERE " + " AND ".join(transf_clauses)
    transf_query += " GROUP BY data, loja_destino ORDER BY data"
    cursor.execute(transf_query, transf_params)
    transf_by_date = {}
    for row in cursor.fetchall():
        d, dest, total = row[0], row[1], float(row[2])
        if d not in transf_by_date:
            transf_by_date[d] = {}
        transf_by_date[d][dest] = transf_by_date[d].get(dest, 0) + total

    lojas_config = []
    if loja_db is None or loja_db == 'Matosinhos':
        lojas_config.append(('Matosinhos', 'inicio'))
    if loja_db is None or loja_db == 'Bolhão':
        lojas_config.append(('Bolhão', 'fim'))

    stock_per_store = {}
    if excluidos:
        excl_placeholders = ','.join(['%s'] * len(excluidos))
        stock_excl_clause = f" AND (sabor IS NOT NULL AND sabor NOT IN ({excl_placeholders}))"
    else:
        stock_excl_clause = " AND sabor IS NOT NULL"
    for loja_name, tipo in lojas_config:
        extended_inicio = data_inicio - timedelta(days=2) if data_inicio else None
        extended_fim = data_fim + timedelta(days=2) if data_fim else None
        query = f"""
            SELECT data, SUM(quantidade_kg) as total
            FROM stock_gelado
            WHERE loja = %s AND tipo = %s{stock_excl_clause}
        """
        params = [loja_name, tipo] + list(excluidos)
        if extended_inicio:
            query += " AND data >= %s"
            params.append(extended_inicio)
        if extended_fim:
            query += " AND data <= %s"
            params.append(extended_fim)
        query += " GROUP BY data ORDER BY data"
        cursor.execute(query, params)
        store_dates = {}
        for row in cursor.fetchall():
            store_dates[row[0]] = float(row[1])
        stock_per_store[loja_name] = store_dates

    release_connection(conn)

    sorted_dates_per_store = {}
    for loja_name in stock_per_store:
        sorted_dates_per_store[loja_name] = sorted(stock_per_store[loja_name].keys())

    def _get_stock_ini_fim(d_date):
        stock_ini = 0.0
        stock_fim = 0.0
        has_data = False
        complete = True

        for loja_name, tipo in lojas_config:
            store_dates = stock_per_store.get(loja_name, {})
            s_dates = sorted_dates_per_store.get(loja_name, [])
            if not s_dates:
                continue

            if loja_name == 'Matosinhos':
                if d_date in store_dates:
                    stock_ini += store_dates[d_date]
                    has_data = True
                    next_dates = [sd for sd in s_dates if sd > d_date]
                    if next_dates:
                        stock_fim += store_dates[next_dates[0]]
                    else:
                        complete = False
            elif loja_name == 'Bolhão':
                prev_dates = [sd for sd in s_dates if sd < d_date]
                if prev_dates:
                    stock_ini += store_dates[prev_dates[-1]]
                    has_data = True
                if d_date in store_dates:
                    stock_fim += store_dates[d_date]
                    has_data = True
                elif prev_dates:
                    complete = False

        return stock_ini, stock_fim, has_data, complete

    pesagem_dates = set()
    for loja_name, tipo in lojas_config:
        for d in stock_per_store.get(loja_name, {}):
            if data_inicio and d < data_inicio:
                continue
            if data_fim and d > data_fim:
                continue
            pesagem_dates.add(d)

    all_dates = set(pesagem_dates)
    for df in [vendas_gelado_df, producao_df, quebras_df]:
        if not df.empty:
            for d in df['data'].unique():
                d_val = d if isinstance(d, date) else pd.to_datetime(d).date()
                if data_inicio and d_val < data_inicio:
                    continue
                if data_fim and d_val > data_fim:
                    continue
                all_dates.add(d_val)
    for d in producao_plano_daily:
        if data_inicio and d < data_inicio:
            continue
        if data_fim and d > data_fim:
            continue
        all_dates.add(d)
    for d in transf_by_date:
        if data_inicio and d < data_inicio:
            continue
        if data_fim and d > data_fim:
            continue
        all_dates.add(d)

    if not all_dates:
        return pd.DataFrame(columns=['data', 'vendas', 'consumo_kg', 'producao_kg', 'transferencias_kg', 'entrada_kg', 'stock_ini_kg', 'stock_final_kg', 'quebras_kg', 'kpi', 'target'])

    results = []
    for d in sorted(all_dates):
        d_date = d if isinstance(d, date) else pd.to_datetime(d).date()

        vendas = vendas_gelado_df[vendas_gelado_df['data'] == d]['valor_euros'].sum() if not vendas_gelado_df.empty else 0
        if loja_db == 'Matosinhos':
            producao_total = producao_plano_daily.get(d_date, 0)
        else:
            producao_total = producao_df[producao_df['data'] == d]['quantidade_kg'].sum() if not producao_df.empty else 0
        quebras = quebras_df[quebras_df['data'] == d]['quantidade_kg'].sum() if not quebras_df.empty else 0

        transf_dia = transf_by_date.get(d_date, {})
        transf_bolhao = transf_dia.get('Bolhão', 0)
        transf_matosinhos = transf_dia.get('Matosinhos', 0)

        stock_ini, stock_fim, has_stock, complete = _get_stock_ini_fim(d_date)

        if not has_stock and vendas == 0 and producao_total == 0 and quebras == 0 and not transf_dia:
            continue

        if not complete and has_stock and stock_ini > 0:
            continue

        if loja_db is None:
            entrada = producao_total
            transf_display = transf_bolhao + transf_matosinhos
        elif loja_db == 'Bolhão':
            entrada = transf_bolhao
            transf_display = transf_bolhao
        else:
            entrada = producao_total - transf_bolhao
            transf_display = transf_bolhao

        consumo = stock_ini + entrada - stock_fim - quebras
        kpi = vendas / consumo if consumo > 0 else 0

        month = d_date.month
        target = get_target_by_month(month)

        results.append({
            'data': d,
            'vendas': vendas,
            'consumo_kg': round(consumo, 3),
            'producao_kg': producao_total,
            'transferencias_kg': round(transf_display, 3),
            'entrada_kg': round(entrada, 3),
            'stock_ini_kg': round(stock_ini, 3),
            'stock_final_kg': round(stock_fim, 3),
            'quebras_kg': quebras,
            'kpi': round(kpi, 2),
            'target': target
        })

    return pd.DataFrame(results)

@ttl_cache_args('kpi_monthly', ttl=600)
def calculate_kpi_monthly(year: int, month: int, loja: str = None):
    from calendar import monthrange
    
    excluidos = get_sabores_excluidos_eurokg()
    if excluidos:
        excl_placeholders = ','.join(['%s'] * len(excluidos))
        stock_excl = f" AND (sabor IS NOT NULL AND sabor NOT IN ({excl_placeholders}))"
    else:
        stock_excl = " AND sabor IS NOT NULL"
    
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    next_month_first = last_day + timedelta(days=1)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    loja_db = loja if loja and loja != 'Porto (Global)' else None
    
    lojas_config = []
    if loja_db is None or loja_db == 'Matosinhos':
        lojas_config.append(('Matosinhos', 'inicio'))
    if loja_db is None or loja_db == 'Bolhão':
        lojas_config.append(('Bolhão', 'fim'))
    
    stock_ini_total = 0
    stock_final_total = 0
    stock_ini_date = None
    stock_final_date = None
    
    prev_month_last = first_day - timedelta(days=1)
    
    for loja_name, tipo in lojas_config:
        if loja_name == 'Bolhão':
            cursor.execute(f"""
                SELECT data, COALESCE(SUM(quantidade_kg), 0)
                FROM stock_gelado
                WHERE loja = %s AND tipo = 'fim' AND data = %s
                  {stock_excl}
                GROUP BY data
            """, [loja_name, prev_month_last] + list(excluidos))
        else:
            cursor.execute(f"""
                SELECT data, COALESCE(SUM(quantidade_kg), 0)
                FROM stock_gelado
                WHERE loja = %s AND tipo = %s AND data >= %s AND data <= %s
                  {stock_excl}
                GROUP BY data
                ORDER BY data ASC
                LIMIT 1
            """, [loja_name, tipo, first_day, last_day] + list(excluidos))
        row = cursor.fetchone()
        if row:
            stock_ini_total += float(row[1])
            if stock_ini_date is None or row[0] < stock_ini_date:
                stock_ini_date = row[0]
        
        if loja_name == 'Matosinhos':
            cursor.execute(f"""
                SELECT data, COALESCE(SUM(quantidade_kg), 0)
                FROM stock_gelado
                WHERE loja = %s AND tipo = 'inicio' AND data = %s
                  {stock_excl}
                GROUP BY data
            """, [loja_name, next_month_first] + list(excluidos))
        else:
            cursor.execute(f"""
                SELECT data, COALESCE(SUM(quantidade_kg), 0)
                FROM stock_gelado
                WHERE loja = %s AND tipo = 'fim' AND data = %s
                  {stock_excl}
                GROUP BY data
            """, [loja_name, last_day] + list(excluidos))
        row = cursor.fetchone()
        if row:
            stock_final_total += float(row[1])
            if stock_final_date is None or row[0] > stock_final_date:
                stock_final_date = row[0]
    
    release_connection(conn)

    if loja_db == 'Matosinhos':
        producao = get_producao_plano_matosinhos_total(first_day, last_day) or 0
        producao_ajustada = producao
        ajuste = 0.0
    else:
        producao = get_producao_total_by_period(None, first_day, last_day) or 0
        ajuste = get_ajuste_producao_mes(year, month)
        producao_ajustada = max(0, producao - ajuste)

    cursor_t = get_connection()
    cur_t = cursor_t.cursor()
    cur_t.execute("""
        SELECT loja_destino, COALESCE(SUM(quantidade_kg), 0)
        FROM transferencias
        WHERE data >= %s AND data <= %s
        GROUP BY loja_destino
    """, (first_day, last_day))
    transf_totals = {}
    for row in cur_t.fetchall():
        transf_totals[row[0]] = float(row[1])
    release_connection(cursor_t)
    transf_bolhao = transf_totals.get('Bolhão', 0)

    quebras_df = get_quebras_df(loja_db, first_day, last_day)
    if not quebras_df.empty:
        sabores_gelado = get_sabores_list(apenas_eurokg=True)
        quebras_df = quebras_df[quebras_df['sabor'].isin(sabores_gelado)]
    quebras = quebras_df['quantidade_kg'].sum() if not quebras_df.empty else 0

    vendas_df = get_vendas_filtradas_df('gelado_kpi', loja_db, first_day, last_day)
    vendas = vendas_df['valor_euros'].sum() if not vendas_df.empty else 0

    if loja_db is None:
        entrada = producao_ajustada
    elif loja_db == 'Bolhão':
        entrada = transf_bolhao
    else:
        entrada = producao_ajustada - transf_bolhao

    consumo = stock_ini_total + entrada - stock_final_total - quebras
    kpi = vendas / consumo if consumo > 0 else 0

    return {
        'stock_ini': round(stock_ini_total, 3),
        'stock_ini_date': stock_ini_date,
        'producao': round(float(producao_ajustada), 3),
        'transferencias': round(float(transf_bolhao), 3),
        'entrada': round(float(entrada), 3),
        'stock_final': round(stock_final_total, 3),
        'stock_final_date': stock_final_date,
        'quebras': round(float(quebras), 3),
        'consumo': round(consumo, 3),
        'vendas': round(float(vendas), 2),
        'kpi': round(kpi, 2),
        'ajuste': round(float(ajuste), 3)
    }

@ttl_cache_args('kpi_annual', ttl=600)
def calculate_kpi_annual(year: int, loja: str = None) -> dict:
    from calendar import monthrange
    
    excluidos = get_sabores_excluidos_eurokg()
    if excluidos:
        excl_placeholders = ','.join(['%s'] * len(excluidos))
        stock_excl = f" AND (sabor IS NOT NULL AND sabor NOT IN ({excl_placeholders}))"
        prod_excl = f" AND (sabor IS NOT NULL AND sabor NOT IN ({excl_placeholders}))"
    else:
        stock_excl = " AND sabor IS NOT NULL"
        prod_excl = " AND sabor IS NOT NULL"
    
    conn = get_connection()
    cursor = conn.cursor()
    
    loja_db = loja if loja and loja != 'Porto (Global)' else None
    
    first_of_year = date(year, 1, 1)
    last_of_year = date(year, 12, 31)
    prev_year_last = date(year - 1, 12, 31)
    next_year_first = date(year + 1, 1, 1)
    
    lojas_config = []
    if loja_db is None or loja_db == 'Matosinhos':
        lojas_config.append(('Matosinhos', 'inicio'))
    if loja_db is None or loja_db == 'Bolhão':
        lojas_config.append(('Bolhão', 'fim'))
    
    all_stock = {}
    stock_per_loja = {}
    for loja_name, tipo in lojas_config:
        stock_per_loja[loja_name] = {}
        cursor.execute(f"""
            SELECT data, SUM(quantidade_kg) as total
            FROM stock_gelado
            WHERE loja = %s AND tipo = %s 
              AND data >= %s AND data <= %s
              {stock_excl}
            GROUP BY data ORDER BY data
        """, [loja_name, tipo, prev_year_last, next_year_first] + list(excluidos))
        for row in cursor.fetchall():
            d = row[0]
            val = float(row[1])
            stock_per_loja[loja_name][d] = val
            all_stock[d] = all_stock.get(d, 0) + val
    
    cursor.execute(f"""
        SELECT EXTRACT(MONTH FROM data)::int as mes, COALESCE(SUM(quantidade_kg), 0)
        FROM producao 
        WHERE EXTRACT(YEAR FROM data) = %s{prod_excl}
        GROUP BY mes ORDER BY mes
    """, [year] + list(excluidos))
    producao_by_month = {int(row[0]): float(row[1]) for row in cursor.fetchall()}
    
    cursor.execute("""
        SELECT EXTRACT(MONTH FROM data)::int as mes, COALESCE(SUM(quantidade_kg), 0)
        FROM transferencias
        WHERE EXTRACT(YEAR FROM data) = %s AND loja_destino = 'Bolhão'
        GROUP BY mes ORDER BY mes
    """, (year,))
    transf_bolhao_by_month = {int(row[0]): float(row[1]) for row in cursor.fetchall()}
    
    sabores_gelado = []
    cursor.execute("SELECT nome_corrente FROM receitas_gelado WHERE ativo = TRUE AND conta_eurokg = TRUE AND nome_corrente IS NOT NULL AND nome_corrente != ''")
    sabores_gelado = [row[0] for row in cursor.fetchall()]
    
    quebras_query = "SELECT EXTRACT(MONTH FROM data)::int as mes, COALESCE(SUM(quantidade_kg), 0) FROM quebras WHERE EXTRACT(YEAR FROM data) = %s"
    quebras_params = [year]
    if loja_db:
        quebras_query += " AND loja = %s"
        quebras_params.append(loja_db)
    if sabores_gelado:
        placeholders = ','.join(['%s'] * len(sabores_gelado))
        quebras_query += f" AND sabor IN ({placeholders})"
        quebras_params.extend(sabores_gelado)
    quebras_query += " GROUP BY mes ORDER BY mes"
    cursor.execute(quebras_query, quebras_params)
    quebras_by_month = {int(row[0]): float(row[1]) for row in cursor.fetchall()}
    
    vendas_query = """
        SELECT EXTRACT(MONTH FROM vd.data)::int as mes, COALESCE(SUM(vd.valor_euros), 0)
        FROM vendas_detalhe vd
        INNER JOIN produtos_vendas_config pvc ON vd.produto = pvc.produto
        WHERE pvc.gelado_kpi = TRUE AND EXTRACT(YEAR FROM vd.data) = %s
    """
    vendas_params = [year]
    if loja_db:
        vendas_query += " AND vd.loja = %s"
        vendas_params.append(loja_db)
    vendas_query += " GROUP BY mes ORDER BY mes"
    cursor.execute(vendas_query, vendas_params)
    vendas_by_month = {int(row[0]): float(row[1]) for row in cursor.fetchall()}
    
    cursor.execute("SELECT mes, COALESCE(SUM(quantidade_kg), 0) FROM ajustes_producao WHERE ano = %s GROUP BY mes", (year,))
    ajustes_by_month = {int(row[0]): float(row[1]) for row in cursor.fetchall()}
    
    release_connection(conn)

    if loja_db == 'Matosinhos':
        producao_plano_by_month = get_producao_plano_matosinhos_by_month(year)
    else:
        producao_plano_by_month = {}
    
    results = {}
    for month in range(1, 13):
        first_day = date(year, month, 1)
        last_day_num = monthrange(year, month)[1]
        last_day = date(year, month, last_day_num)
        prev_month_last = first_day - timedelta(days=1)
        next_month_first = last_day + timedelta(days=1)
        
        stock_ini_total = 0
        stock_final_total = 0
        stock_ini_date = None
        stock_final_date = None
        
        for loja_name, tipo in lojas_config:
            loja_stock = stock_per_loja.get(loja_name, {})
            if loja_name == 'Bolhão':
                if prev_month_last in loja_stock:
                    stock_ini_total += loja_stock[prev_month_last]
                    if stock_ini_date is None or prev_month_last < stock_ini_date:
                        stock_ini_date = prev_month_last
            else:
                for d in sorted(loja_stock.keys()):
                    if first_day <= d <= last_day:
                        stock_ini_total += loja_stock[d]
                        if stock_ini_date is None or d < stock_ini_date:
                            stock_ini_date = d
                        break
            
            if loja_name == 'Matosinhos':
                if next_month_first in loja_stock:
                    stock_final_total += loja_stock[next_month_first]
                    if stock_final_date is None or next_month_first > stock_final_date:
                        stock_final_date = next_month_first
            else:
                if last_day in loja_stock:
                    stock_final_total += loja_stock[last_day]
                    if stock_final_date is None or last_day > stock_final_date:
                        stock_final_date = last_day
        
        if loja_db == 'Matosinhos':
            producao = producao_plano_by_month.get(month, 0)
            ajuste = 0.0
            producao_ajustada = producao
        else:
            producao = producao_by_month.get(month, 0)
            ajuste = ajustes_by_month.get(month, 0)
            producao_ajustada = max(0, producao - ajuste)
        transf_bolhao = transf_bolhao_by_month.get(month, 0)
        quebras = quebras_by_month.get(month, 0)
        vendas = vendas_by_month.get(month, 0)

        if loja_db is None:
            entrada = producao_ajustada
        elif loja_db == 'Bolhão':
            entrada = transf_bolhao
        else:
            entrada = producao_ajustada - transf_bolhao

        consumo = stock_ini_total + entrada - stock_final_total - quebras
        kpi = vendas / consumo if consumo > 0 else 0

        results[month] = {
            'stock_ini': round(stock_ini_total, 3),
            'stock_ini_date': stock_ini_date,
            'producao': round(float(producao_ajustada), 3),
            'transferencias': round(float(transf_bolhao), 3),
            'entrada': round(float(entrada), 3),
            'stock_final': round(stock_final_total, 3),
            'stock_final_date': stock_final_date,
            'quebras': round(float(quebras), 3),
            'consumo': round(consumo, 3),
            'vendas': round(float(vendas), 2),
            'kpi': round(kpi, 2),
            'ajuste': round(float(ajuste), 3)
        }
    
    return results

def get_quebra_by_id(id: int):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=DictCursor)
    cursor.execute("SELECT * FROM quebras WHERE id = %s", (id,))
    row = cursor.fetchone()
    release_connection(conn)
    return dict(row) if row else None

def delete_quebra(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM quebras WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)


def delete_quebras_bulk(ids: list) -> int:
    if not ids:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM quebras WHERE id = ANY(%s)", (ids,))
    deleted = cursor.rowcount
    conn.commit()
    release_connection(conn)
    return deleted

def delete_rececao(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM rececao_stock WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)

def delete_producao(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM producao WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)

def delete_venda(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM vendas WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)

def import_producao_csv(df: pd.DataFrame, loja: str):
    conn = get_connection()
    cursor = conn.cursor()
    for _, row in df.iterrows():
        try:
            data = pd.to_datetime(row.get('data', row.get('Data', row.iloc[0]))).date()
            quantidade = float(row.get('quantidade_kg', row.get('Quantidade', row.get('kg', row.iloc[1]))))
            cursor.execute('''
                INSERT INTO producao (data, loja, quantidade_kg, tipo)
                VALUES (%s, %s, %s, %s)
            ''', (data, loja, quantidade, 'producao'))
        except Exception as e:
            continue
    conn.commit()
    release_connection(conn)

def preview_producao_calybrabox(df: pd.DataFrame, unit_is_grams: bool = False) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    preview = []

    for _, row in df.iterrows():
        try:
            date_val = row.get('Date', row.get('End date', row.get('data', None)))
            if date_val is None:
                continue
            data = pd.to_datetime(date_val).date()

            qty = row.get('Expected Quantity', row.get('Effective Quantity',
                  row.get('expected quantity', row.get('effective quantity', 0))))

            if pd.isna(qty) or qty == 0:
                continue

            quantidade = float(qty)
            if unit_is_grams:
                quantidade = quantidade / 1000.0

            descricao = str(row.get('Description', row.get('description', ''))).strip()

            sabor = None
            nova_receita = False
            if descricao:
                cursor.execute("SELECT COALESCE(nome_corrente, nome) FROM receitas_gelado WHERE UPPER(nome) = UPPER(%s) AND ativo = TRUE", (descricao,))
                match = cursor.fetchone()
                if match:
                    sabor = match[0]
                else:
                    sabor = descricao
                    nova_receita = True

            preview.append({'data': data, 'descricao': descricao, 'sabor': sabor, 'quantidade_kg': round(quantidade, 3), 'nova_receita': nova_receita})
        except Exception:
            continue

    release_connection(conn)
    return preview

def import_producao_calybrabox(df: pd.DataFrame, loja: str, unit_is_grams: bool = False):
    conn = get_connection()
    cursor = conn.cursor()
    imported = 0
    updated = 0
    skipped = 0
    novas_receitas = []

    valid_rows = []
    for _, row in df.iterrows():
        try:
            date_val = row.get('Date', row.get('End date', row.get('data', None)))
            if date_val is None:
                continue
            data = pd.to_datetime(date_val).date()
            qty = row.get('Expected Quantity', row.get('Effective Quantity',
                  row.get('expected quantity', row.get('effective quantity', 0))))
            if pd.isna(qty) or qty == 0:
                continue
            quantidade = float(qty)
            if unit_is_grams:
                quantidade = quantidade / 1000.0
            descricao = str(row.get('Description', row.get('description', ''))).strip()
            valid_rows.append((data, quantidade, descricao))
        except Exception:
            continue

    if not valid_rows:
        release_connection(conn)
        return {'imported': imported, 'updated': updated, 'skipped': skipped, 'novas_receitas': 0, 'receitas_novas_nomes': []}

    all_dates = list({r[0] for r in valid_rows})
    all_descs = list({r[2] for r in valid_rows if r[2]})

    receitas_map = {}
    if all_descs:
        upper_descs = [d.upper() for d in all_descs]
        placeholders = ','.join(['%s'] * len(upper_descs))
        cursor.execute(
            f"SELECT UPPER(nome), COALESCE(nome_corrente, nome) FROM receitas_gelado WHERE UPPER(nome) IN ({placeholders}) AND ativo = TRUE",
            upper_descs
        )
        for nome_upper, nome_corrente in cursor.fetchall():
            receitas_map[nome_upper] = nome_corrente

    min_date = min(all_dates)
    max_date = max(all_dates)
    cursor.execute(
        "SELECT id, data, sabor, quantidade_kg FROM producao WHERE loja = %s AND tipo = 'producao' AND data >= %s AND data <= %s",
        (loja, min_date, max_date)
    )
    existing_map = {}
    for eid, edata, esabor, eqty in cursor.fetchall():
        existing_map[(edata, esabor)] = (eid, float(eqty))

    inserts = []
    updates = []

    for data, quantidade, descricao in valid_rows:
        sabor = descricao if descricao else None
        if descricao:
            upper_desc = descricao.upper()
            if upper_desc in receitas_map:
                sabor = receitas_map[upper_desc]
            else:
                cursor.execute(
                    "INSERT INTO receitas_gelado (nome, nome_corrente, ativo, conta_eurokg) VALUES (%s, NULL, TRUE, TRUE) ON CONFLICT (nome) DO NOTHING",
                    (descricao,)
                )
                if cursor.rowcount > 0 and descricao not in novas_receitas:
                    novas_receitas.append(descricao)
                receitas_map[upper_desc] = sabor

        key = (data, sabor)
        if key in existing_map:
            existing_id, existing_qty = existing_map[key]
            if abs(existing_qty - quantidade) < 0.0001:
                skipped += 1
            else:
                updates.append((quantidade, existing_id))
                existing_map[key] = (existing_id, quantidade)
                updated += 1
        else:
            inserts.append((data, loja, quantidade, 'producao', sabor))
            existing_map[key] = (None, quantidade)
            imported += 1

    if updates:
        cursor.executemany("UPDATE producao SET quantidade_kg = %s WHERE id = %s", updates)
    if inserts:
        cursor.executemany(
            "INSERT INTO producao (data, loja, quantidade_kg, tipo, sabor) VALUES (%s, %s, %s, %s, %s)",
            inserts
        )

    conn.commit()
    release_connection(conn)
    invalidate_prefix('kpi_annual')
    invalidate_prefix('kpi_monthly')
    invalidate_prefix('kpi_by_day')
    return {'imported': imported, 'updated': updated, 'skipped': skipped, 'novas_receitas': len(novas_receitas), 'receitas_novas_nomes': novas_receitas}

def delete_producao_by_date(data: date, loja: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM producao WHERE data = %s AND loja = %s", (data, loja))
    deleted = cursor.rowcount
    conn.commit()
    release_connection(conn)
    return deleted

def delete_producao_by_date_range(data_inicio: date, data_fim: date, loja: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM producao WHERE data >= %s AND data <= %s AND loja = %s",
        (data_inicio, data_fim, loja)
    )
    deleted = cursor.rowcount
    conn.commit()
    release_connection(conn)
    return deleted

def get_producao_total_by_date(loja: str = None) -> list:
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    query = """
        SELECT data, SUM(quantidade_kg) as total_kg, COUNT(*) as num_registos
        FROM producao
        WHERE 1=1
    """
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    query += " GROUP BY data ORDER BY data DESC"
    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    release_connection(conn)
    return rows


def get_vendas_produto_mensal(year: int, loja: str = None) -> dict:
    """Returns sales (€) by eligible product and month for a given year.

    Only products with gelado_kpi = TRUE in produtos_vendas_config are included.
    Returns a dict with:
      - products: list of product names sorted by annual total desc
      - months: list of int month numbers 1-12
      - data: {produto: {mes: valor_euros}}
      - totais_produto: {produto: annual_total}
      - totais_mes: {mes: month_total}
      - grand_total: float
    """
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT vd.produto,
               EXTRACT(MONTH FROM vd.data)::int AS mes,
               COALESCE(SUM(vd.valor_euros), 0) AS total
        FROM vendas_detalhe vd
        INNER JOIN produtos_vendas_config pvc ON vd.produto = pvc.produto
        WHERE pvc.gelado_kpi = TRUE
          AND EXTRACT(YEAR FROM vd.data)::int = %s
    """
    params = [year]
    if loja:
        query += " AND vd.loja = %s"
        params.append(loja)
    query += " GROUP BY vd.produto, mes ORDER BY vd.produto, mes"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    release_connection(conn)

    months = list(range(1, 13))
    data: dict = {}
    totais_produto: dict = {}
    totais_mes: dict = {m: 0.0 for m in months}

    for produto, mes, total in rows:
        total = float(total)
        if produto not in data:
            data[produto] = {m: 0.0 for m in months}
            totais_produto[produto] = 0.0
        data[produto][int(mes)] = total
        totais_produto[produto] += total
        totais_mes[int(mes)] += total

    products = sorted(data.keys(), key=lambda p: totais_produto[p], reverse=True)
    grand_total = sum(totais_produto.values())

    return {
        'products': products,
        'months': months,
        'data': data,
        'totais_produto': totais_produto,
        'totais_mes': totais_mes,
        'grand_total': grand_total,
    }


from db.plano import *  # noqa: F401,F403
from db.area import *   # noqa: F401,F403

