import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger, db_retry
from db.cache import ttl_cache, ttl_cache_args, invalidate_prefix
from db.stores import get_store_id_by_name
import pandas as pd
import json

def get_target_by_month(month: int) -> float:
    high_season = [5, 6, 7, 8, 9]
    if month in high_season:
        return 24.0
    return 25.0

def add_producao(data: date, loja: str, quantidade_kg: float, tipo: str = 'producao', sabor: str = None):
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO producao (data, loja, quantidade_kg, tipo, sabor, store_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (data, loja, quantidade_kg, tipo, sabor, store_id))
        conn.commit()

def add_quebra(data: date, loja: str, quantidade_kg: float, motivo: str = None, sabor: str = None, lote: str = None):
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
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
            return
        cursor.execute('''
            INSERT INTO quebras (data, loja, quantidade_kg, motivo, sabor, lote, store_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (data, loja, quantidade_kg, motivo, sabor, lote, store_id))
        conn.commit()

def add_quebra_area(data: date, loja: str, quantidade: float, area: str, produto: str = None, lote: str = None, motivo: str = None):
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO quebras (data, loja, quantidade_kg, motivo, sabor, lote, store_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (data, loja, quantidade, motivo, f"{area}:{produto}" if produto else area, lote, store_id))
        conn.commit()

def get_quebras_df_area(loja: str, area: str, data_inicio: date = None, data_fim: date = None):
    query = """
        SELECT id, data, lote, quantidade_kg, motivo, sabor
        FROM quebras
        WHERE loja = %s AND sabor LIKE %s
    """
    params = [loja, f"{area}:%"]
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " ORDER BY data DESC, id DESC"
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

def delete_quebra_area(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM quebras WHERE id = %s", (id,))
        conn.commit()


@ttl_cache_args('sabores_list', ttl=600)
@db_retry
def get_sabores_list(apenas_eurokg: bool = False) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        if apenas_eurokg:
            cursor.execute("SELECT nome_corrente FROM receitas_gelado WHERE ativo = TRUE AND conta_eurokg = TRUE AND nome_corrente IS NOT NULL AND nome_corrente != '' ORDER BY nome_corrente")
        else:
            cursor.execute("SELECT nome_corrente FROM receitas_gelado WHERE ativo = TRUE AND nome_corrente IS NOT NULL AND nome_corrente != '' ORDER BY nome_corrente")
        return [row[0] for row in cursor.fetchall()]

@ttl_cache('sabores_mapping', ttl=600)
@db_retry
def get_sabores_mapping() -> dict:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome, nome_corrente FROM receitas_gelado WHERE ativo = TRUE AND nome_corrente IS NOT NULL AND nome_corrente != ''")
        return {row[0]: row[1] for row in cursor.fetchall()}

def get_producao_sabor_overview() -> pd.DataFrame:
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT DISTINCT sabor FROM (
                SELECT sabor FROM producao WHERE loja = 'Matosinhos' AND sabor IS NOT NULL
                UNION
                SELECT sabor FROM stock_gelado WHERE sabor IS NOT NULL
            ) AS combined ORDER BY sabor
        """)
        sabores = [r[0] for r in cursor.fetchall()]

        cursor.execute("""
            SELECT MAX(data) FROM stock_gelado
            WHERE loja = 'Bolhão' AND tipo IN ('fim', 'inicio')
        """)
        ultima_data_pesagem_bolhao = cursor.fetchone()[0]

        cursor.execute("""
            SELECT MAX(data) FROM stock_gelado
            WHERE loja = 'Matosinhos' AND tipo = 'inicio'
        """)
        ultima_data_pesagem_mat = cursor.fetchone()[0]

        cursor.execute("""
            SELECT DISTINCT ON (sabor) sabor, data, quantidade_kg
            FROM producao
            WHERE loja = 'Matosinhos'
            ORDER BY sabor, data DESC, id DESC
        """)
        ultima_producao = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}

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
            except Exception:
                pass
        return 0

    df['consumo_medio'] = df.apply(calc_consumo, axis=1)

    df['stock_total'] = df['stock_matosinhos'] + df['stock_bolhao']
    df = df[(df['stock_matosinhos'] > 0) | (df['stock_bolhao'] > 0)]

    return df

@ttl_cache('sabores_excluidos', ttl=600)
def get_sabores_excluidos_eurokg() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(nome_corrente, nome) FROM receitas_gelado WHERE conta_eurokg = FALSE")
        return [row[0] for row in cursor.fetchall()]

def get_producao_total_by_period(loja: str = None, data_inicio: date = None, data_fim: date = None, para_eurokg: bool = True) -> float:
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
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return cursor.fetchone()[0]

def get_producao_plano_matosinhos_total(data_inicio: date = None, data_fim: date = None) -> float:
    """Returns total real Matosinhos production from plano_producao module by period.
    Applies the same sabor exclusion as the euro/kg calculations.
    Excludes sabores marked as excluded from euro/kg."""
    excluidos = get_sabores_excluidos_eurokg()
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
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return float(cursor.fetchone()[0])


def get_producao_plano_matosinhos_daily(data_inicio: date = None, data_fim: date = None) -> dict:
    """Returns daily real Matosinhos production from plano_producao module.
    Returns a dict of {date: total_kg}.
    Applies the same sabor exclusion as the euro/kg calculations."""
    excluidos = get_sabores_excluidos_eurokg()
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
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return {row[0]: float(row[1]) for row in cursor.fetchall()}


def get_producao_plano_matosinhos_by_month(year: int) -> dict:
    """Returns monthly real Matosinhos production from plano_producao module for a given year.
    Returns a dict of {month: total_kg}.
    Applies the same sabor exclusion as the euro/kg calculations."""
    excluidos = get_sabores_excluidos_eurokg()
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
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return {int(row[0]): float(row[1]) for row in cursor.fetchall()}


@db_retry
def get_producao_daily_totals(loja: str = None, data_inicio: date = None, data_fim: date = None, para_eurokg: bool = True) -> pd.DataFrame:
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
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

@db_retry
def get_producao_df(loja: str = None, data_inicio: date = None, data_fim: date = None, para_eurokg: bool = True) -> pd.DataFrame:
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
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

@db_retry
def get_quebras_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
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
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

@db_retry
def get_vendas_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    """Return daily sales totals aggregated from vendas_detalhe (Gestor uploads)."""
    query = """
        SELECT data, loja, SUM(valor_euros) AS valor_euros
        FROM vendas_detalhe
        WHERE 1=1
    """
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
    query += " GROUP BY data, loja ORDER BY data"
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

def get_ajuste_producao_mes(ano: int, mes: int) -> float:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(SUM(quantidade_kg), 0) FROM ajustes_producao WHERE ano = %s AND mes = %s", (ano, mes))
        return float(cursor.fetchone()[0])


def get_vendas_filtradas_df(area: str, loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    valid_areas = ('gelado_kpi', 'pastelaria', 'confeitaria')
    if area not in valid_areas:
        return pd.DataFrame(columns=['data', 'loja', 'valor_euros'])

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

    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)


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

    with db_connection() as conn:
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

    with db_connection() as conn:
        cursor = conn.cursor()

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

    if loja_db == 'Matosinhos':
        producao = get_producao_plano_matosinhos_total(first_day, last_day) or 0
        producao_ajustada = producao
        ajuste = 0.0
    else:
        producao = get_producao_total_by_period(None, first_day, last_day) or 0
        ajuste = get_ajuste_producao_mes(year, month)
        producao_ajustada = max(0, producao - ajuste)

    with db_connection() as conn_t:
        cur_t = conn_t.cursor()
        cur_t.execute("""
            SELECT loja_destino, COALESCE(SUM(quantidade_kg), 0)
            FROM transferencias
            WHERE data >= %s AND data <= %s
            GROUP BY loja_destino
        """, (first_day, last_day))
        transf_totals = {}
        for row in cur_t.fetchall():
            transf_totals[row[0]] = float(row[1])

    transf_bolhao = transf_totals.get('Bolhão', 0)

    vendas_query = """
        SELECT COALESCE(SUM(vd.valor_euros), 0)
        FROM vendas_detalhe vd
        INNER JOIN produtos_vendas_config pvc ON vd.produto = pvc.produto
        WHERE pvc.gelado_kpi = TRUE AND vd.data >= %s AND vd.data <= %s
    """
    vendas_params = [first_day, last_day]
    if loja_db:
        vendas_query += " AND vd.loja = %s"
        vendas_params.append(loja_db)

    quebras_query = """
        SELECT COALESCE(SUM(quantidade_kg), 0) FROM quebras
        WHERE data >= %s AND data <= %s
    """
    quebras_params = [first_day, last_day]
    if loja_db:
        quebras_query += " AND loja = %s"
        quebras_params.append(loja_db)
    sabores_gelado_q = get_sabores_list(apenas_eurokg=True)
    if sabores_gelado_q:
        placeholders = ','.join(['%s'] * len(sabores_gelado_q))
        quebras_query += f" AND sabor IN ({placeholders})"
        quebras_params.extend(sabores_gelado_q)

    with db_connection() as conn2:
        cur2 = conn2.cursor()
        cur2.execute(vendas_query, vendas_params)
        vendas = float(cur2.fetchone()[0])
        cur2.execute(quebras_query, quebras_params)
        quebras = float(cur2.fetchone()[0])

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
        'stock_final': round(stock_final_total, 3),
        'stock_final_date': stock_final_date,
        'producao': round(float(producao_ajustada), 3),
        'ajuste': round(float(ajuste), 3),
        'transferencias': round(float(transf_bolhao), 3),
        'entrada': round(float(entrada), 3),
        'quebras': round(float(quebras), 3),
        'consumo': round(consumo, 3),
        'vendas': round(float(vendas), 2),
        'kpi': round(kpi, 2),
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

    with db_connection() as conn:
        cursor = conn.cursor()

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
            'ajuste': round(float(ajuste), 3),
        }

    return results

def get_quebra_by_id(id: int):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM quebras WHERE id = %s", (id,))
        row = cursor.fetchone()
    return dict(row) if row else None

def delete_quebra(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM quebras WHERE id = %s", (id,))
        conn.commit()


def delete_quebras_bulk(ids: list) -> int:
    if not ids:
        return 0
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM quebras WHERE id = ANY(%s)", (ids,))
        deleted = cursor.rowcount
        conn.commit()
    return deleted

def delete_producao(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM producao WHERE id = %s", (id,))
        conn.commit()


def import_producao_csv(df: pd.DataFrame, loja: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        for _, row in df.iterrows():
            try:
                data = pd.to_datetime(row.get('data', row.get('Data', row.iloc[0]))).date()
                quantidade = float(row.get('quantidade_kg', row.get('Quantidade', row.get('kg', row.iloc[1]))))
                cursor.execute('''
                    INSERT INTO producao (data, loja, quantidade_kg, tipo)
                    VALUES (%s, %s, %s, %s)
                ''', (data, loja, quantidade, 'producao'))
            except Exception:
                continue
        conn.commit()

def import_producao_calybrabox(df: pd.DataFrame, loja: str, unit_is_grams: bool = False):
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
        return {'imported': imported, 'updated': updated, 'skipped': skipped, 'novas_receitas': 0, 'receitas_novas_nomes': []}

    with db_connection() as conn:
        cursor = conn.cursor()

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
            "SELECT id, data, sabor, quantidade_kg FROM producao WHERE loja = %s AND tipo IN ('producao', 'balança') AND data >= %s AND data <= %s",
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
                inserts.append((data, loja, quantidade, 'balança', sabor))
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

    invalidate_prefix('kpi_annual')
    invalidate_prefix('kpi_monthly')
    invalidate_prefix('kpi_by_day')
    return {'imported': imported, 'updated': updated, 'skipped': skipped, 'novas_receitas': len(novas_receitas), 'receitas_novas_nomes': novas_receitas}

def delete_producao_by_date_range(data_inicio: date, data_fim: date, loja: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM producao WHERE data >= %s AND data <= %s AND loja = %s",
            (data_inicio, data_fim, loja)
        )
        deleted = cursor.rowcount
        conn.commit()
    return deleted

def get_producao_total_by_date(loja: str = None) -> list:
    with db_connection() as conn:
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
        return [dict(r) for r in cursor.fetchall()]


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
    with db_connection() as conn:
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


def get_producao_history_by_day(n_days: int = 30) -> list:
    """Return production records grouped by day for the last n_days.

    Returns a list of dicts ordered by data DESC:
      [{'data': date, 'total_kg': float, 'tipos': list[str],
        'linhas': [{'id': int, 'sabor': str, 'quantidade_kg': float}, ...]}, ...]

    ``tipos`` is the set of distinct ``tipo`` values for the day, which indicates
    how the production was registered (e.g. 'ocr', 'manual', 'balança', 'producao').
    """
    from db.connection import db_connection
    from psycopg2.extras import RealDictCursor
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, data, sabor, ROUND(quantidade_kg::numeric, 3) AS quantidade_kg, tipo
            FROM producao
            WHERE data >= CURRENT_DATE - %s::interval
              AND sabor IS NOT NULL
              AND sabor != ''
            ORDER BY data DESC, sabor, id
        """, (f'{n_days} days',))
        rows = [dict(r) for r in cursor.fetchall()]

    days = []
    current_date = None
    current_linhas = []
    current_tipos: set = set()
    for row in rows:
        if row['data'] != current_date:
            if current_date is not None:
                days.append({
                    'data': current_date,
                    'total_kg': round(sum(float(l['quantidade_kg']) for l in current_linhas), 3),
                    'tipos': sorted(current_tipos),
                    'linhas': current_linhas,
                })
            current_date = row['data']
            current_linhas = []
            current_tipos = set()
        current_linhas.append({'id': row['id'], 'sabor': row['sabor'], 'quantidade_kg': float(row['quantidade_kg'])})
        if row.get('tipo'):
            current_tipos.add(row['tipo'])
    if current_date is not None:
        days.append({
            'data': current_date,
            'total_kg': round(sum(float(l['quantidade_kg']) for l in current_linhas), 3),
            'tipos': sorted(current_tipos),
            'linhas': current_linhas,
        })
    return days


def delete_producao_record(record_id: int) -> bool:
    """Delete a single production record by its ID. Returns True if a row was deleted."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM producao WHERE id = %s", (record_id,))
        deleted = cursor.rowcount
        conn.commit()
    return deleted > 0


from db.plano import *  # noqa: F401,F403
from db.area import *   # noqa: F401,F403
