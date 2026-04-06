import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
from db.cache import ttl_cache, invalidate, invalidate_prefix
from db.stores import get_store_id_by_name
from db.producao import get_sabores_mapping
import pandas as pd
import json
import os

def generate_lote_pastelaria(data: date) -> str:
    return f"P{data.strftime('%d%m%y')}"

def generate_lote_confeitaria(data: date) -> str:
    return f"C{data.strftime('%d%m%y')}"

def add_producao_pastelaria(data: date, loja: str, produto: str, quantidade: int):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    lote = generate_lote_pastelaria(data)
    cursor.execute('''
        INSERT INTO producao_pastelaria (data, loja, produto, quantidade, lote, store_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    ''', (data, loja, produto, quantidade, lote, store_id))
    conn.commit()
    release_connection(conn)
    return lote

def get_pastelaria_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    conn = get_connection()
    query = "SELECT * FROM producao_pastelaria WHERE 1=1"
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
    query += " ORDER BY data DESC"
    df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    return df

def get_pastelaria_stock() -> pd.DataFrame:
    conn = get_connection()
    query = """
        SELECT loja, produto, SUM(quantidade) as stock
        FROM producao_pastelaria
        GROUP BY loja, produto
        ORDER BY loja, produto
    """
    df = pd.read_sql_query(query, conn)
    release_connection(conn)
    return df

def add_producao_confeitaria(data: date, loja: str, produto: str, quantidade: int):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    lote = generate_lote_confeitaria(data)
    cursor.execute('''
        INSERT INTO producao_confeitaria (data, loja, produto, quantidade, lote, store_id)
        VALUES (%s, %s, %s, %s, %s, %s)
    ''', (data, loja, produto, quantidade, lote, store_id))
    conn.commit()
    release_connection(conn)
    return lote

def get_confeitaria_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    conn = get_connection()
    query = "SELECT * FROM producao_confeitaria WHERE 1=1"
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
    query += " ORDER BY data DESC"
    df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    return df

def get_confeitaria_stock() -> pd.DataFrame:
    conn = get_connection()
    query = """
        SELECT loja, produto, SUM(quantidade) as stock
        FROM producao_confeitaria
        GROUP BY loja, produto
        ORDER BY loja, produto
    """
    df = pd.read_sql_query(query, conn)
    release_connection(conn)
    return df

def add_contagem_stock(data: date, loja: str, produto: str, quantidade: int, tipo: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO contagem_stock (data, loja, produto, quantidade, tipo) VALUES (%s, %s, %s, %s, %s)",
        (data, loja, produto, quantidade, tipo)
    )
    conn.commit()
    release_connection(conn)

def get_contagem_stock_df(tipo: str, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    conn = get_connection()
    query = "SELECT * FROM contagem_stock WHERE tipo = %s"
    params = [tipo]
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " ORDER BY data DESC, produto"
    df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    return df

def add_ajuste_producao(ano: int, mes: int, quantidade_kg: float, descricao: str = None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO ajustes_producao (ano, mes, quantidade_kg, descricao) VALUES (%s, %s, %s, %s)",
        (ano, mes, quantidade_kg, descricao)
    )
    conn.commit()
    release_connection(conn)

def get_ajustes_producao(ano: int = None) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    if ano:
        cursor.execute("SELECT id, ano, mes, quantidade_kg, descricao, created_at FROM ajustes_producao WHERE ano = %s ORDER BY mes", (ano,))
    else:
        cursor.execute("SELECT id, ano, mes, quantidade_kg, descricao, created_at FROM ajustes_producao ORDER BY ano, mes")
    rows = cursor.fetchall()
    release_connection(conn)
    return [{'id': r[0], 'ano': r[1], 'mes': r[2], 'quantidade_kg': r[3], 'descricao': r[4], 'created_at': r[5]} for r in rows]

def delete_ajuste_producao(ajuste_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM ajustes_producao WHERE id = %s", (ajuste_id,))
    conn.commit()
    release_connection(conn)

def delete_contagem_stock(contagem_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM contagem_stock WHERE id = %s", (contagem_id,))
    conn.commit()
    release_connection(conn)

def get_produtos_confeitaria() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT nome FROM produtos_confeitaria WHERE ativo = TRUE ORDER BY nome")
    produtos = [row[0] for row in cursor.fetchall()]
    release_connection(conn)
    return produtos

def add_produto_confeitaria(nome: str):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO produtos_confeitaria (nome) VALUES (%s)", (nome,))
        conn.commit()
        success = True
    except psycopg2.IntegrityError:
        conn.rollback()
        success = False
    release_connection(conn)
    return success

def update_produto_confeitaria(id: int, nome: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE produtos_confeitaria SET nome = %s WHERE id = %s", (nome, id))
    conn.commit()
    release_connection(conn)

def delete_produto_confeitaria(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM produtos_confeitaria WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)

def get_all_produtos_confeitaria() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, ativo FROM produtos_confeitaria ORDER BY nome")
    produtos = [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]
    release_connection(conn)
    return produtos

@ttl_cache('motivos_quebra', ttl=300)
def get_motivos_quebra() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT nome FROM motivos_quebra WHERE ativo = TRUE ORDER BY nome")
    motivos = [row[0] for row in cursor.fetchall()]
    release_connection(conn)
    return motivos

def get_all_motivos_quebra() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, ativo FROM motivos_quebra ORDER BY nome")
    motivos = [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]
    release_connection(conn)
    return motivos

def add_motivo_quebra(nome: str):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO motivos_quebra (nome) VALUES (%s)", (nome,))
        conn.commit()
        success = True
    except psycopg2.IntegrityError:
        conn.rollback()
        success = False
    release_connection(conn)
    invalidate('motivos_quebra')
    return success

def update_motivo_quebra(id: int, nome: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE motivos_quebra SET nome = %s WHERE id = %s", (nome, id))
    conn.commit()
    release_connection(conn)
    invalidate('motivos_quebra')

def delete_motivo_quebra(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM motivos_quebra WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)
    invalidate('motivos_quebra')

def get_produtos_pastelaria() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT tipologia, sabor, cobertura FROM produtos_pastelaria WHERE ativo = TRUE ORDER BY tipologia, sabor")
    produtos = []
    for row in cursor.fetchall():
        tipologia = row[0] or ''
        sabor = row[1] or ''
        cobertura = row[2] or ''
        parts = [tipologia]
        if sabor:
            parts.append(sabor)
        if cobertura:
            parts.append(cobertura)
        produtos.append(', '.join(parts))
    release_connection(conn)
    return produtos

def get_all_produtos_pastelaria() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, tipologia, sabor, cobertura, ativo FROM produtos_pastelaria ORDER BY tipologia, sabor")
    produtos = [{'id': row[0], 'tipologia': row[1] or '', 'sabor': row[2] or '', 'cobertura': row[3] or '', 'ativo': row[4]} for row in cursor.fetchall()]
    release_connection(conn)
    return produtos

def add_produto_pastelaria(tipologia: str, sabor: str = '', cobertura: str = ''):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO produtos_pastelaria (tipologia, sabor, cobertura) VALUES (%s, %s, %s)", (tipologia, sabor, cobertura))
        conn.commit()
        success = True
    except psycopg2.IntegrityError:
        conn.rollback()
        success = False
    release_connection(conn)
    return success

def update_produto_pastelaria(id: int, tipologia: str, sabor: str = '', cobertura: str = ''):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE produtos_pastelaria SET tipologia = %s, sabor = %s, cobertura = %s WHERE id = %s", (tipologia, sabor, cobertura, id))
    conn.commit()
    release_connection(conn)

def delete_produto_pastelaria(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM produtos_pastelaria WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)


def delete_produtos_pastelaria_bulk(ids: list):
    if not ids:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM produtos_pastelaria WHERE id = ANY(%s)", (ids,))
    deleted = cursor.rowcount
    conn.commit()
    release_connection(conn)
    return deleted

def get_gelado_por_tipologia() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, tipologia, quantidade_gelado_g FROM gelado_por_tipologia ORDER BY tipologia")
    items = [{'id': row[0], 'tipologia': row[1], 'quantidade_gelado_g': row[2] or 0} for row in cursor.fetchall()]
    release_connection(conn)
    return items

def add_gelado_por_tipologia(tipologia: str, quantidade_gelado_g: float = 0):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO gelado_por_tipologia (tipologia, quantidade_gelado_g) VALUES (%s, %s)", (tipologia, quantidade_gelado_g))
        conn.commit()
        success = True
    except psycopg2.IntegrityError:
        conn.rollback()
        success = False
    release_connection(conn)
    return success

def update_gelado_por_tipologia(id: int, tipologia: str, quantidade_gelado_g: float = 0):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE gelado_por_tipologia SET tipologia = %s, quantidade_gelado_g = %s WHERE id = %s", (tipologia, quantidade_gelado_g, id))
    conn.commit()
    release_connection(conn)

def delete_gelado_por_tipologia(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM gelado_por_tipologia WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)

def get_gelado_peso_by_tipologia(tipologia_nome: str) -> float:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT quantidade_gelado_g FROM gelado_por_tipologia WHERE tipologia = %s", (tipologia_nome,))
    row = cursor.fetchone()
    release_connection(conn)
    return row[0] if row else 0

def update_gelado_peso_by_tipologia(tipologia_nome: str, quantidade_gelado_g: float = 0):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO gelado_por_tipologia (tipologia, quantidade_gelado_g) VALUES (%s, %s) ON CONFLICT (tipologia) DO UPDATE SET quantidade_gelado_g = %s", (tipologia_nome, quantidade_gelado_g, quantidade_gelado_g))
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
    release_connection(conn)

def get_coberturas() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT nome FROM coberturas WHERE ativo = TRUE ORDER BY nome")
    items = [row[0] for row in cursor.fetchall()]
    release_connection(conn)
    return items

def get_all_coberturas() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, ativo FROM coberturas ORDER BY nome")
    items = [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]
    release_connection(conn)
    return items

def add_cobertura(nome: str):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO coberturas (nome) VALUES (%s)", (nome,))
        conn.commit()
        success = True
    except psycopg2.IntegrityError:
        conn.rollback()
        success = False
    release_connection(conn)
    return success

def update_cobertura(id: int, nome: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE coberturas SET nome = %s WHERE id = %s", (nome, id))
    conn.commit()
    release_connection(conn)

def delete_cobertura(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM coberturas WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)

def get_produtos_rececao() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT nome, tipo FROM produtos_rececao WHERE ativo = TRUE ORDER BY nome")
    produtos = [{'nome': row[0], 'tipo': row[1]} for row in cursor.fetchall()]
    release_connection(conn)
    return produtos

def get_all_produtos_rececao() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, tipo, ativo FROM produtos_rececao ORDER BY nome")
    produtos = [{'id': row[0], 'nome': row[1], 'tipo': row[2], 'ativo': row[3]} for row in cursor.fetchall()]
    release_connection(conn)
    return produtos

def add_produto_rececao(nome: str, tipo: str):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO produtos_rececao (nome, tipo) VALUES (%s, %s)", (nome, tipo))
        conn.commit()
        success = True
    except psycopg2.IntegrityError:
        conn.rollback()
        success = False
    release_connection(conn)
    return success

def update_produto_rececao(id: int, nome: str, tipo: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE produtos_rececao SET nome = %s, tipo = %s WHERE id = %s", (nome, tipo, id))
    conn.commit()
    release_connection(conn)

def delete_produto_rececao(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM produtos_rececao WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)

@ttl_cache('receitas_gelado', ttl=600)
def get_receitas_gelado() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT nome FROM receitas_gelado WHERE ativo = TRUE ORDER BY nome")
    receitas = [row[0] for row in cursor.fetchall()]
    release_connection(conn)
    return receitas

def get_all_receitas_gelado() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, nome_corrente, ativo, COALESCE(conta_eurokg, TRUE) FROM receitas_gelado ORDER BY COALESCE(nome_corrente, nome)")
    receitas = [{'id': row[0], 'nome': row[1], 'nome_corrente': row[2] or '', 'ativo': row[3], 'conta_eurokg': row[4]} for row in cursor.fetchall()]
    release_connection(conn)
    return receitas

def add_receita_gelado(nome: str, nome_corrente: str = None):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO receitas_gelado (nome, nome_corrente) VALUES (%s, %s)", (nome, nome_corrente))
        conn.commit()
        success = True
    except psycopg2.IntegrityError:
        conn.rollback()
        success = False
    release_connection(conn)
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')
    return success

def update_receita_gelado(id: int, nome: str, nome_corrente: str = None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE receitas_gelado SET nome = %s, nome_corrente = %s WHERE id = %s", (nome, nome_corrente, id))
    conn.commit()
    release_connection(conn)
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def update_receita_gelado_ativo(id: int, ativo: bool):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE receitas_gelado SET ativo = %s WHERE id = %s", (ativo, id))
    conn.commit()
    release_connection(conn)
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def update_receita_gelado_conta_eurokg(id: int, conta_eurokg: bool):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE receitas_gelado SET conta_eurokg = %s WHERE id = %s", (conta_eurokg, id))
    conn.commit()
    release_connection(conn)
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def delete_receita_gelado(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM receitas_gelado WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def add_rececao_mercadoria(data: date, loja: str, tipo_produto: str, quantidade: float, unidade: str, produto: str = None, sabor: str = None, lote: str = None):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO rececao_mercadoria (data, loja, tipo_produto, produto, sabor, lote, quantidade, unidade, store_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (data, loja, tipo_produto, produto, sabor, lote, quantidade, unidade, store_id))
    conn.commit()
    release_connection(conn)

def get_rececao_mercadoria_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    conn = get_connection()
    query = "SELECT * FROM rececao_mercadoria WHERE 1=1"
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
    query += " ORDER BY data DESC, id DESC"
    df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    return df

def delete_rececao_mercadoria(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM rececao_mercadoria WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)

def add_venda_detalhe(data: date, loja: str, produto: str, quantidade: int, categoria: str = None, valor_euros: float = None):
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO vendas_detalhe (data, loja, produto, categoria, quantidade, valor_euros, store_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    ''', (data, loja, produto, categoria, quantidade, valor_euros, store_id))
    conn.commit()
    release_connection(conn)


def add_venda_detalhe_batch(records: list) -> int:
    """Insert multiple vendas_detalhe records in a single transaction.

    Each record is a dict with keys: data, loja, produto, quantidade,
    categoria (optional), valor_euros (optional).
    Returns the number of rows inserted.
    """
    if not records:
        return 0
    store_id_cache = {}
    rows = []
    for rec in records:
        loja = rec['loja']
        if loja not in store_id_cache:
            store_id_cache[loja] = get_store_id_by_name(loja)
        rows.append((
            rec['data'],
            loja,
            rec['produto'],
            rec.get('categoria'),
            rec['quantidade'],
            rec.get('valor_euros'),
            store_id_cache[loja],
        ))
    conn = get_connection()
    cursor = conn.cursor()
    cursor.executemany(
        '''INSERT INTO vendas_detalhe (data, loja, produto, categoria, quantidade, valor_euros, store_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s)''',
        rows
    )
    conn.commit()
    release_connection(conn)
    return len(rows)


def get_vendas_detalhe_df(loja: str = None, data_inicio: date = None, data_fim: date = None, categoria: str = None, limit: int = 500) -> list:
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    query = "SELECT * FROM vendas_detalhe WHERE 1=1"
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
    if categoria:
        query += " AND categoria = %s"
        params.append(categoria)
    query += " ORDER BY data DESC, id DESC"
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    release_connection(conn)
    return rows

def add_stock_gelado(data: date, loja: str, sabor: str, quantidade_kg: float, tipo: str, local: str = None):
    if quantidade_kg > 50:
        quantidade_kg = quantidade_kg / 1000.0
    store_id = get_store_id_by_name(loja)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo, local, store_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    ''', (data, loja, sabor, quantidade_kg, tipo, local, store_id))
    conn.commit()
    release_connection(conn)

def upsert_stock_gelado_matosinhos(data: date, sabor: str, quantidade_kg: float) -> None:
    """
    Insert a stock_gelado record for Matosinhos/inicio if one doesn't already
    exist for that (data, loja, sabor, tipo) combination.  Used when saving
    the production plan so pesagens entered there are reflected in the
    Euro/kg Pesagens page.
    """
    store_id = get_store_id_by_name('Matosinhos')
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo, store_id)
        SELECT %s, %s, %s, %s, %s, %s
        WHERE NOT EXISTS (
            SELECT 1 FROM stock_gelado
            WHERE data = %s AND loja = %s AND sabor = %s AND tipo = %s
        )
    ''', (
        data, 'Matosinhos', sabor, quantidade_kg, 'inicio', store_id,
        data, 'Matosinhos', sabor, 'inicio',
    ))
    conn.commit()
    release_connection(conn)


def get_pesagem_comparison(loja: str, tipo: str, data_atual: date = None, local: str = None):
    if data_atual is None:
        data_atual = date.today()
    conn = get_connection()
    cursor = conn.cursor()

    local_filter = " AND local = %s" if local else ""

    def p(*dates):
        # params: loja, tipo, date(s), [local]
        params = [loja, tipo] + list(dates)
        if local:
            params.append(local)
        return params

    cursor.execute(f"""
        SELECT DISTINCT data FROM stock_gelado
        WHERE loja = %s AND tipo = %s AND data < %s
          AND sabor IS NOT NULL
          {local_filter}
        ORDER BY data DESC LIMIT 1
    """, p(data_atual))
    row = cursor.fetchone()
    last_date = row[0] if row else None

    cursor.execute(f"""
        SELECT sabor, SUM(quantidade_kg) FROM stock_gelado
        WHERE loja = %s AND tipo = %s AND data = %s
          AND sabor IS NOT NULL
          {local_filter}
        GROUP BY sabor
    """, p(data_atual))
    hoje = {r[0]: float(r[1]) for r in cursor.fetchall()}

    anterior = {}
    if last_date:
        cursor.execute(f"""
            SELECT sabor, SUM(quantidade_kg) FROM stock_gelado
            WHERE loja = %s AND tipo = %s AND data = %s
              AND sabor IS NOT NULL
              {local_filter}
            GROUP BY sabor
        """, p(last_date))
        anterior = {r[0]: float(r[1]) for r in cursor.fetchall()}

    release_connection(conn)
    return last_date, anterior, hoje

def get_stock_gelado_df(loja: str = None, tipo: str = None, data_inicio: date = None, data_fim: date = None) -> list:
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    query = "SELECT * FROM stock_gelado WHERE 1=1"
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if tipo:
        query += " AND tipo = %s"
        params.append(tipo)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " ORDER BY data DESC, id DESC"
    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    release_connection(conn)
    return rows

def get_stock_gelado_by_id(stock_id: int):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=DictCursor)
    cursor.execute("SELECT * FROM stock_gelado WHERE id = %s", (stock_id,))
    row = cursor.fetchone()
    release_connection(conn)
    return dict(row) if row else None

def delete_stock_gelado(stock_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM stock_gelado WHERE id = %s", (stock_id,))
    conn.commit()
    release_connection(conn)

def get_pesagens_recentes(n: int = 3) -> dict:
    """Return the last n pesagem dates and per-sabor kg.
    Matosinhos: reads pesagem_matosinhos from plano_producao (morning weigh-in during planning).
    Bolhão: reads from stock_gelado tipo=fim (end-of-day stock check)."""
    conn = get_connection()
    cursor = conn.cursor()
    result = {}

    # --- Matosinhos: read from plano_producao.pesagem_matosinhos ---
    cursor.execute("""
        SELECT DISTINCT data FROM plano_producao
        WHERE pesagem_matosinhos > 0 AND sabor IS NOT NULL
        ORDER BY data DESC LIMIT %s
    """, (n,))
    datas_mat = [r[0] for r in cursor.fetchall()]
    pivot_mat = {}
    if datas_mat:
        cursor.execute("""
            SELECT data, sabor, pesagem_matosinhos
            FROM plano_producao
            WHERE pesagem_matosinhos > 0 AND sabor IS NOT NULL AND data = ANY(%s)
            ORDER BY sabor, data
        """, (datas_mat,))
        for r in cursor.fetchall():
            d, s, kg = r[0], r[1], float(r[2])
            if s not in pivot_mat:
                pivot_mat[s] = {}
            pivot_mat[s][d] = kg
    result['matosinhos'] = {'loja': 'Matosinhos', 'tipo': 'inicio', 'datas': datas_mat, 'pivot': pivot_mat}

    # --- Bolhão: read from stock_gelado tipo=fim ---
    cursor.execute("""
        SELECT DISTINCT data FROM stock_gelado
        WHERE loja = 'Bolhão' AND tipo = 'fim' AND sabor IS NOT NULL
        ORDER BY data DESC LIMIT %s
    """, (n,))
    datas_bol = [r[0] for r in cursor.fetchall()]
    pivot_bol = {}
    if datas_bol:
        cursor.execute("""
            SELECT data, sabor, SUM(quantidade_kg)
            FROM stock_gelado
            WHERE loja = 'Bolhão' AND tipo = 'fim' AND sabor IS NOT NULL AND data = ANY(%s)
            GROUP BY data, sabor
            ORDER BY sabor, data
        """, (datas_bol,))
        for r in cursor.fetchall():
            d, s, kg = r[0], r[1], float(r[2])
            if s not in pivot_bol:
                pivot_bol[s] = {}
            pivot_bol[s][d] = kg
    result['bolhao'] = {'loja': 'Bolhão', 'tipo': 'fim', 'datas': datas_bol, 'pivot': pivot_bol}

    release_connection(conn)
    return result


def get_latest_stock_by_sabor(loja: str, tipo: str) -> pd.DataFrame:
    today = date.today()
    
    mapping = get_sabores_mapping()
    reverse_mapping = {}
    for nome_receita, nome_corrente in mapping.items():
        reverse_mapping[nome_receita] = nome_corrente
        reverse_mapping[nome_corrente] = nome_corrente
    
    sabores_correntes = sorted(set(mapping.values()))
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sg.id, sg.sabor, sg.quantidade_kg, sg.data
        FROM stock_gelado sg
        WHERE sg.loja = %s AND sg.tipo = %s
        ORDER BY sg.id DESC
    """, (loja, tipo))
    rows = cursor.fetchall()
    release_connection(conn)
    
    before_today = {}
    today_data = {}
    
    for row_id, sabor_raw, qtd, data in rows:
        sabor = reverse_mapping.get(sabor_raw, sabor_raw)
        if data < today:
            if sabor not in before_today:
                before_today[sabor] = {'quantidade_kg': qtd, 'data': data}
        elif data == today:
            if sabor not in today_data:
                today_data[sabor] = qtd
    
    result = []
    for sabor in sabores_correntes:
        bt = before_today.get(sabor, {})
        result.append({
            'sabor': sabor,
            'quantidade_kg': bt.get('quantidade_kg', 0),
            'data': bt.get('data', None),
            'pesagem_hoje': today_data.get(sabor, None)
        })
    
    return pd.DataFrame(result)

def add_stock_gelado_batch(data: date, loja: str, stocks: list, tipo: str):
    conn = get_connection()
    cursor = conn.cursor()
    for stock in stocks:
        if stock['quantidade'] is not None and stock['quantidade'] >= 0:
            cursor.execute('''
                INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo)
                VALUES (%s, %s, %s, %s, %s)
            ''', (data, loja, stock['sabor'], stock['quantidade'], tipo))
    conn.commit()
    release_connection(conn)

def get_vendas_bolhao_dashboard_data(loja: str = 'Bolhão') -> list:
    today = date.today()
    yesterday = today - timedelta(days=1)

    mapping = get_sabores_mapping()
    reverse_mapping = {}
    for nome_receita, nome_corrente in mapping.items():
        reverse_mapping[nome_receita] = nome_corrente
        reverse_mapping[nome_corrente] = nome_corrente

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT DISTINCT ON (sabor) sabor, quantidade_kg
        FROM stock_gelado
        WHERE loja = %s AND tipo IN ('fim', 'inicio') AND data = %s
        ORDER BY sabor, CASE WHEN tipo = 'fim' THEN 0 ELSE 1 END, id DESC
    """, (loja, yesterday))
    pesagem_ontem_raw = {}
    for sabor_raw, qtd in cursor.fetchall():
        sabor = reverse_mapping.get(sabor_raw, sabor_raw)
        pesagem_ontem_raw[sabor] = qtd

    cursor.execute("""
        SELECT sabor, SUM(quantidade) as total
        FROM rececao_mercadoria
        WHERE loja = %s AND data = %s AND sabor IS NOT NULL
        AND unidade = 'kg'
        GROUP BY sabor
    """, (loja, today))
    rececao_hoje_raw = {}
    for sabor_raw, total in cursor.fetchall():
        sabor = reverse_mapping.get(sabor_raw, sabor_raw)
        rececao_hoje_raw[sabor] = rececao_hoje_raw.get(sabor, 0) + float(total)

    cursor.execute("""
        SELECT DISTINCT ON (sabor) sabor, quantidade_kg
        FROM stock_gelado
        WHERE loja = %s AND tipo IN ('fim', 'inicio') AND data = %s
        ORDER BY sabor, CASE WHEN tipo = 'fim' THEN 0 ELSE 1 END, id DESC
    """, (loja, today))
    pesagem_hoje_raw = {}
    for sabor_raw, qtd in cursor.fetchall():
        sabor = reverse_mapping.get(sabor_raw, sabor_raw)
        pesagem_hoje_raw[sabor] = qtd

    release_connection(conn)

    results = []
    all_sabores = sorted(set(pesagem_ontem_raw.keys()) | set(rececao_hoje_raw.keys()))
    for sabor in all_sabores:
        results.append({
            'Sabor': sabor,
            'Pesagem Ontem (kg)': round(pesagem_ontem_raw.get(sabor, 0), 3),
            'Recebido Hoje (kg)': round(rececao_hoje_raw.get(sabor, 0), 3),
            'Pesagem Fim Dia (kg)': round(pesagem_hoje_raw[sabor], 3) if sabor in pesagem_hoje_raw else None
        })

    return results


def get_regras_negocio(area: str = None) -> list:
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    if area:
        cursor.execute("SELECT * FROM regras_negocio WHERE area = %s ORDER BY palavra_chave", (area,))
    else:
        cursor.execute("SELECT * FROM regras_negocio ORDER BY area, palavra_chave")
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]

def add_regra_negocio(area: str, palavra_chave: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO regras_negocio (area, palavra_chave) VALUES (%s, %s)", (area, palavra_chave))
        conn.commit()
        release_connection(conn)
        return True
    except:
        conn.rollback()
        release_connection(conn)
        return False

def update_regra_negocio(regra_id: int, palavra_chave: str) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE regras_negocio SET palavra_chave = %s WHERE id = %s", (palavra_chave, regra_id))
            conn.commit()
            return True
        except:
            conn.rollback()
            return False

def delete_regra_negocio(regra_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM regras_negocio WHERE id = %s", (regra_id,))
    conn.commit()
    release_connection(conn)

def sync_produtos_vendas_config():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT produto FROM vendas_detalhe ORDER BY produto")
    produtos = [r[0] for r in cursor.fetchall()]
    
    cursor.execute("SELECT palavra_chave FROM regras_negocio WHERE area = 'gelado_kpi'")
    palavras_gelado = [r[0].lower() for r in cursor.fetchall()]
    cursor.execute("SELECT palavra_chave FROM regras_negocio WHERE area = 'pastelaria'")
    palavras_pastelaria = [r[0].lower() for r in cursor.fetchall()]
    cursor.execute("SELECT palavra_chave FROM regras_negocio WHERE area = 'confeitaria'")
    palavras_confeitaria = [r[0].lower() for r in cursor.fetchall()]
    
    for produto in produtos:
        p_lower = produto.lower()
        is_gelado = any(kw in p_lower for kw in palavras_gelado) if palavras_gelado else False
        is_pastelaria = any(kw in p_lower for kw in palavras_pastelaria) if palavras_pastelaria else False
        is_confeitaria = any(kw in p_lower for kw in palavras_confeitaria) if palavras_confeitaria else False
        
        cursor.execute("""
            INSERT INTO produtos_vendas_config (produto, gelado_kpi, pastelaria, confeitaria)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (produto) DO NOTHING
        """, (produto, is_gelado, is_pastelaria, is_confeitaria))
    
    conn.commit()
    release_connection(conn)

def get_produtos_vendas_config() -> list:
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM produtos_vendas_config ORDER BY produto")
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]

def update_produto_vendas_config(produto_id: int, gelado_kpi: bool, pastelaria: bool, confeitaria: bool):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE produtos_vendas_config 
        SET gelado_kpi = %s, pastelaria = %s, confeitaria = %s 
        WHERE id = %s
    """, (gelado_kpi, pastelaria, confeitaria, produto_id))
    conn.commit()
    release_connection(conn)

def update_produtos_vendas_config_batch(updates: list):
    conn = get_connection()
    cursor = conn.cursor()
    for u in updates:
        cursor.execute("""
            UPDATE produtos_vendas_config 
            SET gelado_kpi = %s, pastelaria = %s, confeitaria = %s 
            WHERE id = %s
        """, (u['gelado_kpi'], u['pastelaria'], u['confeitaria'], u['id']))
    conn.commit()
    release_connection(conn)

def get_produtos_by_area(area: str) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    if area == 'confeitaria':
        cursor.execute("SELECT produto FROM produtos_vendas_config WHERE confeitaria = TRUE ORDER BY produto")
    elif area == 'pastelaria':
        cursor.execute("SELECT produto FROM produtos_vendas_config WHERE pastelaria = TRUE ORDER BY produto")
    else:
        cursor.execute("SELECT produto FROM produtos_vendas_config WHERE gelado_kpi = TRUE ORDER BY produto")
    rows = cursor.fetchall()
    release_connection(conn)
    return [r[0] for r in rows]

def delete_vendas_detalhe_by_dates(data_inicio: date, data_fim: date, loja: str = None) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    if loja:
        cursor.execute("DELETE FROM vendas_detalhe WHERE data >= %s AND data <= %s AND loja = %s", (data_inicio, data_fim, loja))
    else:
        cursor.execute("DELETE FROM vendas_detalhe WHERE data >= %s AND data <= %s", (data_inicio, data_fim))
    deleted = cursor.rowcount
    conn.commit()
    release_connection(conn)
    return deleted

def get_gramas_gelado() -> list:
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM gramas_gelado ORDER BY artigo")
    rows = cursor.fetchall()
    release_connection(conn)
    return [dict(r) for r in rows]

def update_gramas_gelado(artigo_id: int, artigo: str, gramas: float) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE gramas_gelado SET artigo = %s, gramas = %s WHERE id = %s", (artigo, gramas, artigo_id))
            conn.commit()
            return True
        except:
            conn.rollback()
            return False

def add_gramas_gelado(artigo: str, gramas: float) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO gramas_gelado (artigo, gramas) VALUES (%s, %s)", (artigo, gramas))
            conn.commit()
            return True
        except:
            conn.rollback()
            return False

def delete_gramas_gelado(artigo_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM gramas_gelado WHERE id = %s", (artigo_id,))
    conn.commit()
    release_connection(conn)

def get_consumo_gelado_mensal(loja: str = None) -> pd.DataFrame:
    conn = get_connection()
    
    gramas_df = pd.read_sql_query("SELECT artigo, gramas FROM gramas_gelado", conn)
    
    query = """
        SELECT 
            vd.produto,
            TO_CHAR(vd.data, 'YYYY-MM') as mes,
            SUM(vd.quantidade) as quantidade_vendida
        FROM vendas_detalhe vd
        INNER JOIN produtos_vendas_config pvc ON vd.produto = pvc.produto
        WHERE pvc.gelado_kpi = TRUE
    """
    params = []
    if loja:
        query += " AND vd.loja = %s"
        params.append(loja)
    query += " GROUP BY vd.produto, TO_CHAR(vd.data, 'YYYY-MM') ORDER BY vd.produto, mes"
    
    vendas_df = pd.read_sql_query(query, conn, params=params)
    release_connection(conn)
    
    if vendas_df.empty:
        return vendas_df
    
    def find_gramas(produto):
        for _, row in gramas_df.iterrows():
            if row['artigo'].lower() in produto.lower():
                return row['gramas']
        return 0
    
    vendas_df['gramas_por_unidade'] = vendas_df['produto'].apply(find_gramas)
    vendas_df['consumo_kg'] = (vendas_df['quantidade_vendida'] * vendas_df['gramas_por_unidade']) / 1000
    
    return vendas_df


def get_product_dashboard_summary(area: str) -> pd.DataFrame:
    if area == 'pastelaria':
        prod_table = 'producao_pastelaria'
        config_field = 'pastelaria'
    else:
        prod_table = 'producao_confeitaria'
        config_field = 'confeitaria'

    today = date.today()
    week_ago = today - timedelta(days=7)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(f"SELECT produto FROM produtos_vendas_config WHERE {config_field} = TRUE ORDER BY produto")
    produtos = [row[0] for row in cursor.fetchall()]

    if not produtos:
        if area == 'pastelaria':
            cursor.execute("SELECT nome FROM produtos_pastelaria WHERE ativo = TRUE ORDER BY nome")
        else:
            cursor.execute("SELECT nome FROM produtos_confeitaria WHERE ativo = TRUE ORDER BY nome")
        produtos = [row[0] for row in cursor.fetchall()]

    if not produtos:
        release_connection(conn)
        return pd.DataFrame()

    cursor.execute(f"""
        SELECT DISTINCT ON (produto) produto, data, quantidade
        FROM {prod_table}
        ORDER BY produto, data DESC, id DESC
    """)
    all_prod_map = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    last_prod_map = {}
    for p in produtos:
        if p in all_prod_map:
            last_prod_map[p] = all_prod_map[p]
        else:
            for prod_name, val in all_prod_map.items():
                if p.lower() in prod_name.lower() or prod_name.lower() in p.lower():
                    last_prod_map[p] = val
                    break

    cursor.execute("""
        SELECT produto, loja, SUM(quantidade) as total
        FROM vendas_detalhe
        WHERE LOWER(produto) IN (SELECT LOWER(produto) FROM produtos_vendas_config WHERE """ + config_field + """ = TRUE)
        AND data >= %s AND data <= %s
        GROUP BY produto, loja
    """, (week_ago, today))
    vendas_semana = {}
    for row in cursor.fetchall():
        produto_venda = row[0]
        loja = row[1]
        total = int(row[2]) if row[2] else 0
        if produto_venda not in vendas_semana:
            vendas_semana[produto_venda] = {}
        vendas_semana[produto_venda][loja] = total

    cursor.execute("""
        SELECT DISTINCT ON (produto, loja) produto, loja, quantidade
        FROM contagem_stock
        WHERE tipo = %s
        ORDER BY produto, loja, data DESC, id DESC
    """, (area,))
    stock_map = {}
    for row in cursor.fetchall():
        produto_stock = row[0]
        loja = row[1]
        qtd = int(row[2]) if row[2] else 0
        if produto_stock not in stock_map:
            stock_map[produto_stock] = {}
        stock_map[produto_stock][loja] = qtd

    release_connection(conn)

    results = []
    for produto in produtos:
        lp = last_prod_map.get(produto)
        ultima_prod = f"{lp[1]} un ({lp[0].strftime('%d/%m')})" if lp else "-"

        vendas_mat = 0
        vendas_bol = 0
        for vp, lojas in vendas_semana.items():
            if produto.lower() in vp.lower() or vp.lower() in produto.lower():
                vendas_mat += lojas.get('Matosinhos', 0)
                vendas_bol += lojas.get('Bolhão', 0)

        stock_mat = stock_map.get(produto, {}).get('Matosinhos', 0)
        stock_bol = stock_map.get(produto, {}).get('Bolhão', 0)

        results.append({
            'Produto': produto,
            'Última Produção': ultima_prod,
            'Vendas Semana Mat.': int(vendas_mat),
            'Vendas Semana Bol.': int(vendas_bol),
            'Stock Mat.': int(stock_mat),
            'Stock Bol.': int(stock_bol),
        })

    return pd.DataFrame(results)

def get_tipologias_pastelaria() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT nome FROM tipologias_pastelaria WHERE ativo = TRUE ORDER BY nome")
    items = [row[0] for row in cursor.fetchall()]
    release_connection(conn)
    return items

def get_all_tipologias_pastelaria() -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nome, ativo FROM tipologias_pastelaria ORDER BY nome")
    items = [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]
    release_connection(conn)
    return items

def add_tipologia_pastelaria(nome: str):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO tipologias_pastelaria (nome) VALUES (%s)", (nome,))
        conn.commit()
        success = True
    except psycopg2.IntegrityError:
        conn.rollback()
        success = False
    release_connection(conn)
    return success

def delete_tipologia_pastelaria(id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tipologias_pastelaria WHERE id = %s", (id,))
    conn.commit()
    release_connection(conn)

